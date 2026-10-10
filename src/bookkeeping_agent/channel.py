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
from .models import IncomingMessage
from .service import BookkeepingService
from .tracing import LOG_FAILED, LOG_FAILED_SUFFIX, TraceLogError, emit

logger = logging.getLogger(__name__)


def build_channel(settings: Settings) -> FeishuChannel:
    return FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret.get_secret_value(),
        inbound=InboundConfig(emit_raw_events=True),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(delay_ms=0, long_delay_ms=0, max_messages=1),
            chat_queue=ChatQueueConfig(merge_while_busy=False),
        ),
    )


def register_handlers(channel: FeishuChannel, service: BookkeepingService) -> None:
    traces = service.traces

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
                response = "处理未完成；如已提交记账，请先核对表格，勿重复发送。"
            if response is None:
                emit("message.end", outcome="silent")
                return
            emit("reply.generated", response=response)
            payload, opts = {"text": response}, {"reply_to": message.message_id}
            emit("reply.request", chat_id=message.chat_id, payload=payload, options=opts)
            if trace.failed:
                if trace.mutation_submitted:
                    if LOG_FAILED_SUFFIX not in response:
                        response += LOG_FAILED_SUFFIX
                else:
                    response = LOG_FAILED
                payload = {"text": response}
                logger.error("reply_without_trace message_id=%s", message.message_id)
            try:
                result = await channel.send(message.chat_id, payload, opts)
                emit("reply.result", result=result)
                logger.log(
                    logging.INFO if result.success else logging.ERROR,
                    "reply_sent message_id=%s" if result.success else "reply_failed message_id=%s",
                    message.message_id,
                )
                emit(
                    "message.end",
                    outcome="replied" if result.success else "reply_failed",
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
