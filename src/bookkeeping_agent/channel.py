import asyncio
import logging
from datetime import UTC, datetime

from lark_channel import (
    ChatQueueConfig,
    FeishuChannel,
    InboundConfig,
    SafetyConfig,
    TextBatchConfig,
)
from pydantic import ValidationError

from .config import Settings
from .feedback import CardFeedback
from .models import IncomingMessage
from .progress import progress_scope
from .service import BookkeepingService
from .tracing import LOG_FAILED, LOG_FAILED_SUFFIX, TraceLogError, emit

logger = logging.getLogger(__name__)

PROCESSING_REACTIONS = ("OneSecond",)
REACTION_TIMEOUT_SECONDS = 3


def build_channel(settings: Settings) -> FeishuChannel:
    return FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret.get_secret_value(),
        inbound=InboundConfig(emit_raw_events=True),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(delay_ms=0, long_delay_ms=0, max_messages=1),
            chat_queue=ChatQueueConfig(enabled=False, merge_while_busy=False),
        ),
    )


def register_handlers(channel: FeishuChannel, service: BookkeepingService) -> None:
    traces = service.traces
    chat_locks: dict[str, asyncio.Lock] = {}
    active_messages: set[str] = set()

    async def add_processing_reaction(message_id, emoji_type):
        emit("reaction.request", emoji_type=emoji_type)
        try:
            result = await asyncio.wait_for(
                channel.add_reaction(message_id, emoji_type), REACTION_TIMEOUT_SECONDS
            )
            emit("reaction.result", emoji_type=emoji_type, result=result)
            if result.success:
                return
        except Exception as exc:  # noqa: BLE001 -- Feedback must not prevent bookkeeping.
            emit("reaction.error", status="error", emoji_type=emoji_type, error=exc)
        logger.warning("reaction_failed message_id=%s emoji_type=%s", message_id, emoji_type)

    async def acknowledge(message_id):
        await asyncio.gather(
            *(add_processing_reaction(message_id, emoji) for emoji in PROCESSING_REACTIONS)
        )

    def on_raw(payload):
        if isinstance(payload.get("event"), dict) and "message" in payload["event"]:
            trace = traces.received(payload)
            logger.info("message_trace message_id=%s path=%s", trace.message_id, trace.path)

    def on_reject(event):
        trace = traces.claim(event.message_id)
        with traces.scope(trace):
            emit("message.sdk_rejected", event=event)
            emit("message.end", outcome="sdk_rejected", reason=event.reason)

    async def on_message(message):
        trace = traces.claim(message.message_id)
        with traces.scope(trace):
            emit("message.sdk_normalized", message=message)
            if message.sender_is_bot or message.raw_content_type != "text":
                emit("message.end", outcome="ignored", reason="bot_or_unsupported_type")
                return
            text = message.body_text.strip()
            if not text:
                emit("message.end", outcome="ignored", reason="empty_text")
                return
            try:
                incoming = IncomingMessage(
                    message_id=message.message_id,
                    user_id=message.sender_id,
                    chat_id=message.chat_id,
                    sent_at=datetime.fromtimestamp(int(message.create_time) / 1000, UTC),
                    text=text,
                )
            except (ValueError, TypeError, ValidationError) as exc:
                emit("message.invalid", status="error", error=exc)
                logger.warning("message_invalid message_id=%s", message.message_id)
                return
            journal = getattr(service, "journal", None)
            previous = journal.get(message.message_id) if journal else None
            if message.message_id in active_messages or (
                previous and previous["status"] == "success"
            ):
                emit("message.deduplicated", reason="active_or_success")
                emit("message.end", outcome="silent")
                return
            active_messages.add(message.message_id)
            lock = chat_locks.setdefault(message.chat_id, asyncio.Lock())
            feedback = CardFeedback(channel, message, queued=lock.locked())
            initial = asyncio.create_task(feedback.acknowledge())
            reaction = asyncio.create_task(acknowledge(message.message_id))
            try:
                async with lock:
                    await initial
                    feedback.state.start()
                    feedback.dirty.set()
                    with progress_scope(feedback.on_event):
                        await process_message(message, incoming, trace, feedback)
            except asyncio.CancelledError:
                if not initial.done():
                    initial.cancel()
                    await asyncio.gather(initial, return_exceptions=True)
                if feedback.state.ended_at is None:
                    response = (
                        "表格写入已成功，但处理已中断；请勿重复发送这笔消费。"
                        if feedback.state.confirmed
                        else "处理已中断，写入结果待核对；请先检查表格，勿重复发送。"
                        if trace.mutation_submitted
                        else "处理已中断，本次未执行写入。"
                    )
                    feedback.state.finish(
                        response, submitted=trace.mutation_submitted, interrupted=True
                    )
                    await feedback.deliver_final()
                    emit("message.end", outcome="interrupted")
                elif not feedback.final_delivered:
                    # Cancellation can also interrupt final delivery after the business result is known.
                    await feedback.deliver_final()
                    emit("message.end", outcome="interrupted")
                raise
            finally:
                for task in (initial, reaction):
                    if not task.done() and asyncio.current_task().cancelling():
                        task.cancel()
                await asyncio.gather(initial, reaction, return_exceptions=True)
                await feedback.stop()
                active_messages.discard(message.message_id)

    async def process_message(message, incoming, trace, feedback):
        logger.info("message_received message_id=%s", message.message_id)
        try:
            response = await service.reply(incoming)
        except TraceLogError:
            response = LOG_FAILED
        except Exception as exc:  # noqa: BLE001 -- User feedback never contains raw exceptions.
            emit("message.service_error", status="error", error=exc)
            logger.warning(
                "message_failed message_id=%s kind=%s", message.message_id, type(exc).__name__
            )
            response = (
                "表格写入已成功，但后续处理未完成；请勿重复发送这笔消费。"
                if feedback.state.confirmed
                else "处理未完成，写入结果待核对；请先检查表格，勿重复发送。"
                if trace.mutation_submitted
                else "处理未完成，本次未执行写入，请稍后重试。"
            )
        if response is None:
            # The preflight dedup normally catches this; concurrent direct service calls may win.
            if feedback.message_id:
                feedback.state.finish("这条消息已处理，本次未重复记账。")
                feedback.state.status, feedback.state.title = "已结束", "消息已处理"
                await feedback.deliver_final()
            emit("message.end", outcome="silent")
            return
        emit("reply.generated", response=response)
        feedback.state.finish(response, submitted=trace.mutation_submitted, log_failed=trace.failed)
        emit(
            "reply.request",
            chat_id=message.chat_id,
            payload={"card": feedback.state.snapshot()},
            response=response,
            options={"reply_to": message.message_id},
        )
        if trace.failed:
            if trace.mutation_submitted:
                if LOG_FAILED_SUFFIX not in response:
                    response += LOG_FAILED_SUFFIX
            else:
                response = LOG_FAILED
            feedback.state.finish(response, submitted=trace.mutation_submitted, log_failed=True)
            logger.error("reply_without_trace message_id=%s", message.message_id)
        try:
            result = await feedback.deliver_final()
            emit("reply.result", result=result)
            response = feedback.state.response
            logger.log(
                logging.INFO if result else logging.ERROR,
                "reply_sent message_id=%s" if result else "reply_failed message_id=%s",
                message.message_id,
            )
            emit(
                "message.end",
                outcome="replied" if result else "reply_failed",
                response=response,
                mutation_submitted=trace.mutation_submitted,
            )
        except Exception as exc:  # noqa: BLE001 -- Reply failure must not repeat a write.
            emit("reply.error", status="error", error=exc)
            logger.error(
                "reply_failed message_id=%s kind=%s", message.message_id, type(exc).__name__
            )
            emit("message.end", outcome="reply_failed")

    async def on_error(error):
        logger.error("channel_error kind=%s", type(error).__name__)
        emit("channel.error", status="error", error=error)

    channel.on("raw", on_raw)
    channel.on("reject", on_reject)
    channel.on("message", on_message)
    channel.on("error", on_error)
    channel.on("reconnecting", lambda *args: logger.warning("channel_reconnecting"))
    channel.on("reconnected", lambda *args: logger.info("channel_reconnected"))
