"""Runtime schedules and deadlines for the independent agent-notes parts."""
from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast


class Part(str, Enum):
    NOTES = "notes"
    QUOTA = "quota"
    PACER = "pacer"
    REFUSALS = "refusals"
    WATCHDOG = "watchdog"
    PRUNE = "prune"
    HIERARCHY = "hierarchy"
    VAULT = "vault"


@dataclass(frozen=True)
class Schedule:
    interval_seconds: float
    timeout_seconds: float


@dataclass(frozen=True)
class Settings:
    job: Schedule
    parts: dict[Part, Schedule]
    hierarchy_git_seconds: float
    hierarchy_read_seconds: float
    vault_git_seconds: float
    vault_named_paths: int
    dispatch_seconds: float
    recovery_seconds: float
    recovery_dispatch_seconds: float
    recovery_result_seconds: float


def table(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("agent-notes config: expected a table")
    return cast(dict[str, object], value)


def positive(values: dict[str, object], name: str) -> float:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or value <= 0 or not math.isfinite(value):
        raise ValueError(f"agent-notes config: {name} must be positive")
    return float(value)


def schedule(value: object) -> Schedule:
    values = table(value)
    return Schedule(positive(values, "interval_seconds"), positive(values, "timeout_seconds"))


def settings() -> Settings:
    path = Path(os.environ.get("AGENT_NOTES_JOB_CONFIG", str(Path(__file__).resolve().parents[2] / "config/agent_notes.toml")))
    values = table(tomllib.loads(path.read_text(encoding="utf-8")))
    parts = table(values.get("parts"))
    hierarchy, vault = table(values.get("hierarchy")), table(values.get("vault"))
    named = vault.get("named_paths")
    if isinstance(named, bool) or not isinstance(named, int) or named <= 0:
        raise ValueError("agent-notes config: named_paths must be a positive integer")
    return Settings(schedule(values.get("job")), {part: schedule(parts.get(part.value)) for part in Part},
                    positive(hierarchy, "git_seconds"), positive(hierarchy, "read_seconds"),
                    positive(vault, "git_seconds"), named, positive(table(values.get("watchdog")), "dispatch_seconds"),
                    positive(table(values.get("watchdog")), "recovery_seconds"),
                    positive(table(values.get("watchdog")), "pass_dispatch_seconds"),
                    positive(table(values.get("watchdog")), "result_seconds"))
