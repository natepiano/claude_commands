"""Shared storage and text rules for one-use fallback push approval."""
from __future__ import annotations

import os
from pathlib import Path
from typing import cast


def object_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def text(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f"missing {key}")
    return value


def marker_path(session_id: str) -> Path:
    if not session_id or Path(session_id).name != session_id or session_id in (".", ".."):
        raise ValueError("invalid session_id")
    directory = Path(os.environ.get("NOTIFY_STATE_DIR", str(Path.home() / ".local/state/notify"))) / "failed"
    return directory / f"{session_id}.json"


def fallback_matches(message: str, record: dict[str, object]) -> bool:
    return " ".join(message.split()) == " ".join(text(record, "fallback").split())
