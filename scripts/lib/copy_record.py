"""Copy locations and record reading, kept as a leaf because nix_env imports it.
scratch.py reaches herdr, which imports nix_env; only the standard library belongs here.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT_ENV = "SCRATCH_ROOT"
DEFAULT_ROOT = "/tmp/claude/scratch"
COPIES_ENV = "SCRATCH_COPIES_ROOT"
RECORD = ".scratch.json"
TEMPORARY_GATE_RECORD = ".gate-copy.json"
TASK = re.compile(r"(?P<kind>run|phase|row|gate|until|landed):(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_./-]+)?)")


@dataclass(frozen=True)
class Registered:
    task: str
    fields: dict[str, object]


@dataclass(frozen=True)
class NotRegistered:
    pass


@dataclass(frozen=True)
class UnreadableRecord:
    reason: str


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def copies_root() -> Path:
    configured = Path(os.environ.get(COPIES_ENV) or str(root().parent / "copies")).expanduser()
    return Path(os.path.realpath(configured))


def copy_folder(path: Path) -> Path | None:
    """The copy that contains path; convert the path boundary to no copy here."""
    base = copies_root()
    resolved = path.resolve()
    if resolved.is_relative_to(base):
        parts = resolved.relative_to(base).parts
        if len(parts) >= 2:
            return base / parts[0] / parts[1]
    return None


def record_path(folder: Path) -> Path | None:
    """Find the record for an editable copy, a temporary gate, or a task clone."""
    folder = folder.resolve()
    if copy_folder(folder) == folder:
        return folder / RECORD
    if folder.name == "checkout":
        path = folder.parent / TEMPORARY_GATE_RECORD
        record = read_record(path)
        if isinstance(record, Registered) and record.task.startswith("gate:"):
            return path
    if folder.name == "checkout" and folder.parent.parent == root().resolve():
        path = folder.parent / RECORD
        record = read_record(path)
        if isinstance(record, Registered) and record.fields.get("test_hana_clone") is True:
            return path
    return None


def read_record(path: Path) -> Registered | NotRegistered | UnreadableRecord:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NotRegistered()
    except (OSError, UnicodeError, ValueError) as error:
        return UnreadableRecord(f"repair record {path}: {error}")
    fields = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    task = fields.get("task")
    if not isinstance(task, str) or not TASK.fullmatch(task):
        return UnreadableRecord(f"repair record {path}: invalid task")
    return Registered(task, fields)
