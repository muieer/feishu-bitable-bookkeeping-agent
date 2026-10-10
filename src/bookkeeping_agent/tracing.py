"""Message-scoped, append-only diagnostics. Logging never retries business operations."""

import base64
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import traceback
import uuid
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, SecretStr
from pydantic_ai.models import Model

logger = logging.getLogger(__name__)
LOG_FAILED = "调试日志保存失败，本次未执行写入。"
LOG_FAILED_SUFFIX = " 调试日志保存失败，请检查日志目录。"
_current: ContextVar = ContextVar("bookkeeping_message_trace", default=None)
_run: ContextVar = ContextVar("bookkeeping_agent_trace", default=None)
_file_locks = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()
_SECRET_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "api_key",
    "ark_api_key",
    "app_secret",
    "lark_app_secret",
    "access_token",
    "refresh_token",
    "tenant_access_token",
    "user_access_token",
    "app_access_token",
    "token",
    "encrypt_key",
    "verification_token",
    "password",
    "secret",
}


class TraceLogError(BaseException):
    """Abort diagnostics without being converted into a retriable SDK connection error."""


def serialize(value, seen=None):
    """Preserve precision and complete public data without collecting frame locals."""
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_float": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return serialize(value.value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, BaseException):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return "[circular exception cause]"
        seen.add(id(value))
        result = {
            "type": type(value).__name__,
            "message": str(value),
            "traceback": "".join(traceback.format_exception(value)),
        }
        if hasattr(value, "errors"):
            result["validation_errors"] = serialize(value.errors(), seen)
        if value.__cause__ is not None:
            result["cause"] = serialize(value.__cause__, seen)
        elif value.__context__ is not None and not value.__suppress_context__:
            result["context"] = serialize(value.__context__, seen)
        seen.remove(id(value))
        return result
    if isinstance(value, Model):
        return {"type": type(value).__name__, "model_name": value.model_name}
    seen = set() if seen is None else seen
    if id(value) in seen:
        return "[circular reference]"
    seen.add(id(value))
    try:
        if isinstance(value, BaseModel):
            return serialize(value.model_dump(mode="python"), seen)
        if isinstance(value, dict):
            return {str(k): serialize(v, seen) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [serialize(v, seen) for v in value]
        if dataclasses.is_dataclass(value):
            return {
                f.name: serialize(getattr(value, f.name), seen)
                for f in dataclasses.fields(value)
                if not f.name.startswith("_")
            }
        if callable(value):
            return getattr(value, "__qualname__", type(value).__name__)
        return repr(value)
    finally:
        seen.remove(id(value))


def redact(value, secrets):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower().replace("-", "_") in _SECRET_KEYS else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        # Raw HTTP/CLI bodies and exception strings may themselves contain JSON.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            cleaned = redact(parsed, secrets)
            if cleaned != parsed:
                value = json.dumps(cleaned, ensure_ascii=False)
        value = re.sub(r"(?i)(Bearer\s+)[^\s\"',;]+", r"\1[REDACTED]", value)
        value = re.sub(
            r"(?i)((?:api_key|app_secret|access_token|refresh_token|verification_token|password)"
            r"\s*[=:]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            value,
        )
        return value
    return value


class MessageTrace:
    def __init__(self, store, message_id):
        self.store = store
        self.message_id = message_id
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", message_id)[:100] or "message"
        digest = hashlib.sha256(message_id.encode()).hexdigest()[:12]
        self.path = store.directory / f"{safe}--{digest}.log"
        self.delivery_id = uuid.uuid4().hex
        self.sequence = 0
        self.started = time.monotonic()
        self.failed = False
        self.mutation_submitted = False
        self.response = None
        with _locks_guard:
            self.lock = _file_locks.setdefault(self.path, threading.Lock())

    def emit(self, stage, *, status="ok", durable=False, **details):
        if self.failed:
            return False
        try:
            with self.lock:
                self.sequence += 1
                record = {
                    "timestamp": datetime.now(ZoneInfo(self.store.timezone)).isoformat(),
                    "message_id": self.message_id,
                    "delivery_id": self.delivery_id,
                    "sequence": self.sequence,
                    "stage": stage,
                    "status": status,
                    "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                    **(_run.get() or {}),
                    **(
                        {"tool_call_id": details["tool_call_id"]}
                        if "tool_call_id" in details
                        else {}
                    ),
                    "details": serialize(details),
                }
                record = redact(record, self.store.secrets)
                entry = f"\n=== {record['timestamp']} {stage} [{status}] ===\n"
                entry += json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as out:
                    out.write(entry)
                    out.flush()
                    if durable:
                        os.fsync(out.fileno())
            return True
        except Exception as exc:  # noqa: BLE001 -- Diagnostics failure must not escape as model error.
            self.failed = True
            logger.error(
                "message_log_failed message_id=%s path=%s stage=%s kind=%s",
                self.message_id,
                self.path,
                stage,
                type(exc).__name__,
            )
            return False

    def require(self, *, reconciliation=False):
        if self.failed and not (reconciliation and self.mutation_submitted):
            raise TraceLogError("message diagnostic log unavailable")


class TraceStore:
    def __init__(self, settings):
        self.directory = settings.message_log_dir.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        # Check actual write access rather than relying on permission bits alone.
        with tempfile.TemporaryFile(dir=self.directory):
            pass
        self.timezone = settings.display_timezone
        self.secrets = (
            settings.ark_api_key.get_secret_value(),
            settings.lark_app_secret.get_secret_value(),
        )
        self.pending = OrderedDict()

    def new(self, message_id=None, event_id=None):
        return MessageTrace(self, message_id or event_id or "diagnostic_" + uuid.uuid4().hex)

    def received(self, payload):
        event = payload.get("event") or {}
        message = event.get("message") or {}
        header = payload.get("header") or {}
        trace = self.new(message.get("message_id"), header.get("event_id"))
        trace.emit("message.raw_received", payload=payload, business_callback="not_observed_yet")
        self.pending.setdefault(trace.message_id, trace)
        # Bound only in-memory correlation entries, never log retention.
        while len(self.pending) > 4096:
            self.pending.popitem(last=False)
        return trace

    def claim(self, message_id):
        return self.pending.pop(message_id, None) or self.new(message_id)

    @contextmanager
    def scope(self, trace):
        token = _current.set(trace)
        try:
            yield trace
        finally:
            _current.reset(token)


def current_trace():
    return _current.get()


def emit(stage, **details):
    trace = current_trace()
    return trace.emit(stage, **details) if trace else True


def require(*, reconciliation=False):
    trace = current_trace()
    if trace:
        trace.require(reconciliation=reconciliation)


@contextmanager
def run_scope(phase):
    state = {"agent_run_id": uuid.uuid4().hex, "phase": phase, "loop": 0, "http_attempt": 0}
    token = _run.set(state)
    try:
        yield state
    finally:
        _run.reset(token)


def run_state():
    return _run.get()
