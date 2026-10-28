"""Read the progress recorder's open phases without loading its command or starting a process."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

STATE_FILENAME = "progress_history_state.json"
# Older phases remain open when a newer one starts; the current one lives under `phase`.
OPEN_PHASES_KEY = "open_phases"


def _record(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _phase_id(phase: dict[str, object]) -> str:
    value = phase.get("id")
    return value if isinstance(value, str) else ""


def open_phase_ids(state: dict[str, object]) -> list[str]:
    """The recorder's older open phases, then its current phase if still active."""
    raw = state.get(OPEN_PHASES_KEY)
    parked = cast(list[object], raw) if isinstance(raw, list) else []
    ids = [_phase_id(cast(dict[str, object], item)) for item in parked if isinstance(item, dict)]
    current = _record(state.get("phase"))
    if current.get("status") == "active":
        ids.append(_phase_id(current))
    return ids


def read_open_phase_ids(session_dir: Path) -> list[str]:
    """Read atomically published recorder state; a run without that file has no open phases."""
    path = session_dir / STATE_FILENAME
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ValueError(f"Unable to read progress state {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Invalid progress state: {path}")
    return open_phase_ids(cast(dict[str, object], value))
