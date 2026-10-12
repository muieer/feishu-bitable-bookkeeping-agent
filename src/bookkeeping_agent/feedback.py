"""One immediate reply card, serial progress updates and bounded final delivery."""

import asyncio
import logging
import time

from .progress import ProgressState
from .tracing import LOG_FAILED, LOG_FAILED_SUFFIX, current_trace, emit

logger = logging.getLogger(__name__)
FEEDBACK_TIMEOUT_SECONDS = 3
UPDATE_INTERVAL_SECONDS = 1
HEARTBEAT_SECONDS = 1


class CardFeedback:
    def __init__(self, channel, message, *, queued=False):
        self.channel = channel
        self.message = message
        self.state = ProgressState(queued=queued)
        self.message_id = None
        self.dirty = asyncio.Event()
        self.stopped = False
        self.worker = None
        self.stop_event = asyncio.Event()
        self.last_update = time.monotonic()
        self.final_delivered = False

    def on_event(self, stage, details):
        # Network/log events are deliberately ignored by the presentation projection.
        if stage.startswith(("card.", "reply.", "reaction.", "channel.")):
            return
        before = self.state.snapshot()
        self.state.on_event(stage, details)
        if self.state.snapshot() != before:
            self.dirty.set()

    async def request(self, stage, operation, **details):
        emit(stage + ".request", **details)
        try:
            result = await asyncio.wait_for(operation(), FEEDBACK_TIMEOUT_SECONDS)
            emit(stage + ".result", result=result)
            if result.success:
                return result
        except Exception as exc:  # noqa: BLE001 -- Feedback never retries business operations.
            emit(stage + ".error", status="error", error=exc)
        logger.warning(
            "card_feedback_failed message_id=%s stage=%s", self.message.message_id, stage
        )
        return None

    async def acknowledge(self):
        card = self.state.snapshot()
        result = await self.request(
            "card.ack",
            lambda: self.channel.send(
                self.message.chat_id, {"card": card}, {"reply_to": self.message.message_id}
            ),
            payload={"card": card},
            options={"reply_to": self.message.message_id},
        )
        if result and result.message_id:
            self.message_id = result.message_id
            self.last_update = time.monotonic()
            self.worker = asyncio.create_task(self.run_updates())

    async def run_updates(self):
        while not self.stopped:
            try:
                await asyncio.wait_for(self.dirty.wait(), HEARTBEAT_SECONDS)
            except TimeoutError:
                pass
            if self.stopped:
                return
            remaining = UPDATE_INTERVAL_SECONDS - (time.monotonic() - self.last_update)
            if remaining > 0:
                try:
                    # stop() can wake the throttle immediately, without cancelling an in-flight patch.
                    await asyncio.wait_for(self.wait_stopped(), remaining)
                except TimeoutError:
                    pass
            if self.stopped:
                return
            self.dirty.clear()
            card = self.state.snapshot()
            await self.request(
                "card.update",
                lambda snapshot=card: self.channel.update_card(self.message_id, snapshot),
                message_id=self.message_id,
                card=card,
            )
            self.last_update = time.monotonic()

    async def wait_stopped(self):
        await self.stop_event.wait()

    async def stop(self):
        self.stopped = True
        self.dirty.set()
        self.stop_event.set()
        if self.worker:
            await self.worker
            self.worker = None

    async def deliver_final(self):
        # Stop/drain updates before constructing and sending the final snapshot.
        await self.stop()
        card = self.final_card()
        if self.message_id:
            result = await self.request(
                "card.final",
                lambda: self.channel.update_card(self.message_id, self.final_card()),
                message_id=self.message_id,
                card=card,
            )
            if result:
                self.final_delivered = True
                return result
        result = await self.request(
            "card.final_fallback",
            lambda: self.channel.send(
                self.message.chat_id,
                {"card": self.final_card()},
                {"reply_to": self.message.message_id},
            ),
            payload={"card": card},
        )
        if result:
            self.final_delivered = True
            return result
        result = await self.request(
            "card.text_fallback",
            lambda: self.channel.send(
                self.message.chat_id,
                {"text": self.final_text()},
                {"reply_to": self.message.message_id},
            ),
        )
        self.final_delivered = bool(result)
        return result

    def final_card(self):
        # A diagnostic write can fail even in a final-send request. Refresh before the network call.
        trace = current_trace()
        if trace and trace.failed:
            response = self.state.response or LOG_FAILED
            if trace.mutation_submitted:
                if LOG_FAILED_SUFFIX not in response:
                    response += LOG_FAILED_SUFFIX
            else:
                response = LOG_FAILED
            self.state.finish(response, submitted=trace.mutation_submitted, log_failed=True)
        return self.state.snapshot()

    def final_text(self):
        self.final_card()
        return self.state.response
