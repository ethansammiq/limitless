"""Small filesystem helpers shared across the live stack."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash mid-write can't leave a
    torn/empty file. The state files that dedupe Discord alerts and mark CLI
    products 'seen' must survive an interrupted */2 cron tick intact."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def atomic_write_json(path: Path, obj: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent))
