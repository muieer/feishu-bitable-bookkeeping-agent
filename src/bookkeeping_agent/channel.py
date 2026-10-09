import logging
from datetime import UTC, datetime

from lark_channel import ChatQueueConfig, FeishuChannel, SafetyConfig, TextBatchConfig
from pydantic import ValidationError

from .config import Settings
from .models import IncomingMessage
from .service import BookkeepingService

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


def register_handlers(channel: FeishuChannel, service: BookkeepingService) -> None:
    async def on_message(message):
        if message.sender_is_bot or message.raw_content_type != "text":
            return
        text = message.body_text.strip()
        if not text:
            return
        try:
            incoming = IncomingMessage(
                message_id=message.message_id,
                user_id=message.sender_id,
                chat_id=message.chat_id,
                sent_at=datetime.fromtimestamp(int(message.create_time) / 1000, UTC),
                text=text,
            )
        except (ValueError, TypeError, ValidationError):
            logger.warning("message_invalid message_id=%s", message.message_id)
            return
        logger.info("message_received message_id=%s", message.message_id)
        try:
            response = await service.reply(incoming)
        except Exception as exc:  # noqa: BLE001 -- Never include provider/CLI secrets.
            logger.warning(
                "message_failed message_id=%s kind=%s", message.message_id, type(exc).__name__
            )
            response = "处理未完成；如已提交记账，请先核对表格，勿重复发送。"
        if response is None:
            return
        try:
            result = await channel.send(
                message.chat_id, {"text": response}, {"reply_to": message.message_id}
            )
            logger.log(
                logging.INFO if result.success else logging.ERROR,
                "reply_sent message_id=%s" if result.success else "reply_failed message_id=%s",
                message.message_id,
            )
        except Exception as exc:  # noqa: BLE001 -- Reply failure must not repeat a write.
            logger.error(
                "reply_failed message_id=%s kind=%s", message.message_id, type(exc).__name__
            )

    async def on_error(error):
        logger.error("channel_error kind=%s", type(error).__name__)

    channel.on("message", on_message)
    channel.on("error", on_error)
