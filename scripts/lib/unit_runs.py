#!/usr/bin/env python3
"""Where unit runs keep their folders, under the new name and the former one.

A /unit:direct run keeps its files in `/tmp/claude/unit/<run id>/`, marks its Claude session
in `/tmp/claude/unit/active/<session id>`, and gets the notifier instance `unit-<run id>`.
Runs started before 2026-10-15 used `delegate` in all three places and keep their folders
until they end, so every reader takes both names. The former name is read until a removal
step, on or after 2026-10-22, drops it; that step runs only while `unit_runs.py removal-check`
passes, since a run started before the rename may stay open past that date.

A test points a reader at its own folder with an override, which replaces both names.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

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


class FormerOwner(NamedTuple):
    session_id: str
    name: str
    run: str


def former_owners(running: Mapping[str, str]) -> list[FormerOwner]:
    """Each running session, by id to its name, that owns a marker under the former name."""
    try:
        markers = sorted((FORMER_RUN_ROOT / "active").iterdir())
    except OSError:
        return []
    owners: list[FormerOwner] = []
    for path in markers:
        if path.name not in running:
            continue
        try:
            run = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            run = "an unreadable marker"
        owners.append(FormerOwner(path.name, running[path.name], run))
    return owners


def removal_check(running: Mapping[str, str]) -> int:
    """Refuse the former name's removal while a running session owns a marker under it."""
    owners = former_owners(running)
    if not owners:
        print(f"no running session owns a marker under {FORMER_RUN_ROOT / 'active'}; the former name can go")
        return 0
    print(f"keep the former name: {len(owners)} running session(s) own a marker under {FORMER_RUN_ROOT / 'active'}")
    for owner in owners:
        print(f"  {owner.name or 'unnamed'} ({owner.session_id}): {owner.run}")
    return 1


def main(argv: list[str]) -> int:
    if argv != ["removal-check"]:
        print("usage: unit_runs.py removal-check", file=sys.stderr)
        return 2
    if TYPE_CHECKING:
        from ..message.sessions import running_sessions
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
        from sessions import running_sessions

    return removal_check({record["sessionId"]: record["name"] for record in running_sessions()})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
