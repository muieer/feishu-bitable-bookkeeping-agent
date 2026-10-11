"""Small atomic JSON files, no database or long-term conversation memory."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .tracing import emit


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class OperationJournal:
    """Reserve message IDs before side effects; an unfinished reservation is uncertain."""

    def __init__(self, path: Path, target: str):
        self.path = path
        self.target = target
        self.entries = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(self.entries, dict):
            raise TypeError("invalid operation journal")
        if any(
            not isinstance(value, dict)
            or value.get("status") not in {"started", "uncertain", "success"}
            for value in self.entries.values()
        ):
            raise ValueError("invalid journal entry")

    def key(self, message_id: str) -> str:
        return f"{self.target}:{message_id}"

    def get(self, message_id: str) -> dict | None:
        return self.entries.get(self.key(message_id))

    def save(self, message_id: str, status: str, **metadata) -> None:
        emit(
            "journal.save_start", message_id=message_id, operation_status=status, metadata=metadata
        )
        updated = dict(self.entries)
        updated[self.key(message_id)] = {"status": status, **metadata}
        try:
            atomic_json(self.path, updated)
        except BaseException as exc:
            emit("journal.save_error", status="error", error=exc)
            raise
        self.entries = updated
        emit("journal.saved", message_id=message_id, operation_status=status, metadata=metadata)

    def reserve_operations(self, operations: dict[str, dict]) -> None:
        """Reserve every bill in a single durable file replacement before a batch call."""
        updated = dict(self.entries)
        for operation_id, metadata in operations.items():
            if self.get(operation_id):
                raise ValueError("operation already reserved")
            updated[self.key(operation_id)] = {"status": "started", **metadata}
        emit("journal.reserve_start", operations=operations, durable=True)
        atomic_json(self.path, updated)
        self.entries = updated
        emit("journal.reserved", operations=operations)
