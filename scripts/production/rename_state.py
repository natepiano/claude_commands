#!/usr/bin/env python3
"""Read and publish rename journals through the shared durable writer."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
from pathlib import Path
from typing import cast

from scripts.lib import atomic_file
from scripts.production import staff


class RenameRefused(ValueError):
    """A rename cannot safely complete the requested operation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


def read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise RenameRefused(f"cannot parse {path}: {error}") from error


def production_journal(identity: staff.StaffId) -> Path:
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    return root / identity / "rename.json"


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def write_text(path: Path, text: str) -> None:
    """Publish a journal through the shared durable file writer."""
    atomic_file.write_text_atomic(path, text)
