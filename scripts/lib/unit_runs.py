#!/usr/bin/env python3
"""Where unit runs keep their folders, under the new name and the former one.

A /unit:direct run keeps its files in `/tmp/claude/unit/<run id>/`, marks its Claude session
in `/tmp/claude/unit/active/<session id>`, and gets the notifier instance `unit-<run id>`.
Runs started before 2026-10-15 used `delegate` in all three places and keep their folders
until they end, so every reader takes both names. The former name stops being read on
2026-10-22, once no live session owns a marker under it.

A test points a reader at its own folder with an override, which replaces both names.
"""
from __future__ import annotations

from pathlib import Path

RUN_ROOT = Path("/tmp/claude/unit")
FORMER_RUN_ROOT = Path("/tmp/claude/delegate")
INSTANCE_PREFIXES = ("unit-", "delegate-")


def run_roots(override: str | Path | None = None) -> tuple[Path, ...]:
    """The folders that hold run folders, the new name first."""
    return (Path(override),) if override else (RUN_ROOT, FORMER_RUN_ROOT)


def active_dirs(override: str | Path | None = None) -> tuple[Path, ...]:
    """The folders that hold run markers, the new name first."""
    return (Path(override),) if override else tuple(root / "active" for root in run_roots())


def marker(session_id: str, override: str | Path | None = None) -> Path:
    """The session's run marker: the first that exists, else where a new run writes it."""
    candidates = [directory / session_id for directory in active_dirs(override)]
    return next((path for path in candidates if path.is_file()), candidates[0])


def instance(run_dir: Path) -> str:
    """The run's notifier instance: `delegate-<run id>` for a folder under the former name."""
    return f"{'delegate' if run_dir.parent.name == FORMER_RUN_ROOT.name else 'unit'}-{run_dir.name}"
