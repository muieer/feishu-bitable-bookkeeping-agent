"""Durable send-time request accounting, including SDK retries and failed sends."""

import fcntl
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .storage import atomic_json


class RequestBudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, path: Path, limit: int):
        self.path, self.limit = path, limit

    def reserve(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A separate inode keeps the lock valid across atomic replacement and processes.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {"requests": 0}
            used = data["requests"]
            if type(used) is not int or used < 0:
                raise ValueError("invalid request counter")
            if used >= self.limit:
                raise RequestBudgetExceeded("模型 HTTP 请求额度已用完，未发送请求。")
            atomic_json(self.path, {"requests": used + 1, "limit": self.limit})
            return used + 1


@dataclass
class MessageRequestBudget:
    limit: int = 10
    requests: int = 0

    def reserve(self):
        if self.requests >= self.limit:
            raise RequestBudgetExceeded("本条消息模型请求已达上限，未发送新请求。")
        self.requests += 1
        return self.requests


_message_budget: ContextVar[MessageRequestBudget | None] = ContextVar(
    "message_budget", default=None
)


@contextmanager
def message_request_scope(limit: int = 10):
    # Nested callers share the budget; asyncio child tasks inherit the same counter.
    existing = _message_budget.get()
    budget = existing or MessageRequestBudget(limit=min(limit, 10))
    token = _message_budget.set(budget)
    try:
        yield budget
    finally:
        _message_budget.reset(token)


def reserve_message_request():
    budget = _message_budget.get()
    return budget.reserve() if budget is not None else None


def budget_exceeded(error: BaseException) -> bool:
    """SDKs may wrap a send-time budget denial in their own connection exception."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RequestBudgetExceeded):
            return True
        pending.extend(
            cause for cause in (current.__cause__, current.__context__) if cause is not None
        )
    return False
