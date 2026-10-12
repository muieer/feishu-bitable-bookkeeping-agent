"""Human-readable progress projected from actual business events."""

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
_observer: ContextVar = ContextVar("bookkeeping_progress", default=None)


@contextmanager
def progress_scope(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def observe(stage, details):
    observer = _observer.get()
    if observer is not None:
        try:
            observer(stage, details)
        except Exception as exc:  # noqa: BLE001 -- Presentation never controls a write.
            logger.warning("progress_observer_failed kind=%s", type(exc).__name__)


def body_element(content, *, muted=False):
    text = {"tag": "plain_text", "content": content}
    if muted:
        text["text_color"] = "grey"
    return {"tag": "div", "text": text}


@dataclass
class ProgressState:
    queued: bool = False
    clock: Callable[[], float] = time.monotonic
    received_at: float = field(init=False)
    execution_at: float | None = None
    ended_at: float | None = None
    confirmed: bool = False
    prepared: bool = False
    completed_response: str | None = None
    response: str | None = None
    title: str = "处理结果"
    status: str = "处理中"
    batch_total: int = 0
    batch_counts: dict = field(default_factory=dict)

    def __post_init__(self):
        self.received_at = self.clock()

    def start(self):
        self.queued = False
        self.execution_at = self.clock()

    def on_event(self, stage, details):
        # Capture confirmed outcomes for cancellation fallback, never project stages/details.
        if stage == "batch.started":
            self.batch_total = details["total"]
        elif stage == "table.confirmed":
            self.confirmed = True
        elif stage == "table.prepared":
            self.prepared = True
        elif stage in {"message.completed", "batch.interrupted"}:
            self.completed_response = details["response"]
            self.batch_counts = details["counts"]

    def finish(self, response, *, submitted=False, log_failed=False, interrupted=False):
        if self.ended_at is None:
            self.ended_at = self.clock()
        self.response = response
        self.title = "处理结果"
        self.status = (
            "结果待核对"
            if self.batch_counts.get("uncertain")
            or (submitted and not self.confirmed and (interrupted or log_failed))
            else "已中断"
            if interrupted
            else "处理未完成"
            if log_failed or self.batch_counts.get("failed")
            else "已完成"
        )

    def snapshot(self):
        now = self.ended_at if self.ended_at is not None else self.clock()
        elapsed = max(0, int(now - self.received_at))
        summary = f"已耗时 {elapsed} 秒"
        if self.ended_at is not None:
            summary = f"{self.status} · 耗时 {elapsed} 秒"
        elements = [body_element(summary, muted=True)]
        if self.ended_at is not None:
            elements.extend(
                body_element(line)
                for line in (self.response or "本次消息已处理。").splitlines()
                if line
            )
        return {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "width_mode": "default",
                "summary": {"content": summary},
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "vertical_spacing": "8px",
                "elements": elements,
            },
        }
