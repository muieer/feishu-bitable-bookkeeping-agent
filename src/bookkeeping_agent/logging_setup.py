"""Configure console and persistent application logging at the CLI entry point."""

import logging
from pathlib import Path


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
        force=True,
    )
    logging.getLogger("bookkeeping_agent").setLevel(logging.INFO)
