"""Small atomic JSON files, no database or long-term conversation memory."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


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
        updated = dict(self.entries)
        updated[self.key(message_id)] = {"status": status, **metadata}
        atomic_json(self.path, updated)
        self.entries = updated
