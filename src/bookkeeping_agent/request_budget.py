"""Durable send-time request accounting, including SDK retries and failed sends."""

import fcntl
import json
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
