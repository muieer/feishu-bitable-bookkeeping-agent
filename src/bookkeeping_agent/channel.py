import asyncio
import logging

from lark_channel import ChatQueueConfig, FeishuChannel, SafetyConfig, TextBatchConfig

from .agent import QueryService
from .config import Settings
from .tools import QueryError

logger = logging.getLogger(__name__)


def build_channel(settings: Settings) -> FeishuChannel:
    return FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret.get_secret_value(),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(delay_ms=0, long_delay_ms=0, max_messages=1),
            chat_queue=ChatQueueConfig(merge_while_busy=False),
        ),
    )


def register_handlers(channel: FeishuChannel, service: QueryService, timeout: float) -> None:
    async def on_message(message):
        if message.sender_is_bot or message.raw_content_type != "text":
            return
        text = message.body_text.strip()
        if not text:
            return
        logger.info("query_received message_id=%s", message.message_id)
        try:
            response = await asyncio.wait_for(service.reply(text), timeout=timeout)
        except QueryError as exc:
            response = str(exc)
            logger.warning("query_failed message_id=%s kind=cli", message.message_id)
        except TimeoutError:
            response = "查询处理超时，请稍后重试。"
            logger.warning("query_failed message_id=%s kind=timeout", message.message_id)
        except Exception as exc:  # noqa: BLE001 -- SDK/provider boundary; return a safe error.
            # Do not log provider exceptions: they can include prompts or credentials.
            logger.warning(
                "query_failed message_id=%s kind=%s", message.message_id, type(exc).__name__
            )
            response = "查询未完成，请检查模型配置和网络连接后重试。"
        try:
            result = await channel.send(
                message.chat_id, {"text": response}, {"reply_to": message.message_id}
            )
            if result.success:
                logger.info("reply_sent message_id=%s", message.message_id)
            else:
                logger.error("reply_failed message_id=%s", message.message_id)
        except Exception as exc:  # noqa: BLE001 -- Keep the message handler alive on send failure.
            logger.error(
                "reply_failed message_id=%s kind=%s", message.message_id, type(exc).__name__
            )

    async def on_error(error):
        logger.error("channel_error kind=%s", type(error).__name__)

    channel.on("message", on_message)
    channel.on("error", on_error)
