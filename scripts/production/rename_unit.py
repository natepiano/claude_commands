#!/usr/bin/env python3
"""Rename one running production unit and every store that names its session."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, cast

import add_unit
import rename_state
import showrunners
import tmux_names


class AwaitingRename(NamedTuple):
    session_id: str
    pane: str


class RenamedInClaude(NamedTuple):
    session_id: str
    pane: str


ClaudeSide = AwaitingRename | RenamedInClaude


class RowNamesOld(NamedTuple):
    row: str


class RowNamesNew(NamedTuple):
    row: str


RowSide = RowNamesOld | RowNamesNew


class RenameWait(NamedTuple):
    seconds: int
    sleep: Callable[[float], None]


class LiveSessionRecord(NamedTuple):
    fields: dict[str, object]


class NoLiveSessionRecord(NamedTuple):
    pass


SessionRecord = LiveSessionRecord | NoLiveSessionRecord


class RenamePlan(NamedTuple):
    production: add_unit.Production
    claude: ClaudeSide
    row: RowSide
    unit: str


WAIT = RenameWait(30, time.sleep)


def _session_record(session_id: str) -> SessionRecord:
    for path in tmux_names.SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(path.stem)
            os.kill(pid, 0)
            record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if record.get("sessionId") == session_id:
            return LiveSessionRecord(record)
    return NoLiveSessionRecord()


def _was_named(session_id: str, old: str) -> bool:
    record = _session_record(session_id)
    if isinstance(record, NoLiveSessionRecord):
        return False
    former = record.fields.get("formerNames")
    return (isinstance(former, list)
            and any(isinstance(entry, dict) and cast(dict[str, object], entry).get("name") == old
                    for entry in cast(list[object], former)))


def _claude_side(old: str, new: str) -> ClaudeSide:
    sessions = tmux_names.live_sessions()
    old_sessions = [session for session in sessions if session.name == old]
    new_sessions = [session for session in sessions if session.name == new]
    if len(old_sessions) == 1 and not new_sessions:
        session = old_sessions[0]
        return AwaitingRename(session.session_id, session.pane)
    if len(new_sessions) == 1 and not old_sessions and _was_named(new_sessions[0].session_id, old):
        session = new_sessions[0]
        return RenamedInClaude(session.session_id, session.pane)
    former = sum(_was_named(session.session_id, old) for session in new_sessions)
    raise add_unit.Refusal(
        f"Claude sessions found: {len(old_sessions)} named {old}, {len(new_sessions)} named {new}, "
        + f"and {former} new-name records list {old} in formerNames"
    )


def _row_fields(row: str) -> list[str]:
    return row.strip("|").split("|")


def _row_session(row: str) -> str:
    fields = _row_fields(row)
    return add_unit.cell_value(fields[4]) if len(fields) >= 5 else ""


def _row_side(lines: list[str], claude: ClaudeSide, old: str, new: str) -> RowSide:
    _, all_rows = add_unit.unit_rows(lines)
    live_rows = add_unit.live_unit_rows(lines)
    named = [row for row in all_rows if _row_session(row) in (old, new)]
    if len(named) != 1:
        raise add_unit.Refusal(
            f"Units table has {len(named)} rows, live or retired, whose Session is {old} or {new}"
        )
    row = named[0]
    if row not in live_rows:
        raise add_unit.Refusal(f"no live Units row has Session {old} or {new}")
    session = _row_session(row)
    if session == old:
        return RowNamesOld(row)
    if isinstance(claude, RenamedInClaude) and session == new:
        return RowNamesNew(row)
    raise add_unit.Refusal(f"no live Units row fits the Claude session state; found Session {session}")


def _tmux_panes() -> dict[str, str]:
    result = subprocess.run(
        [tmux_names.TMUX, "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"tmux exited {result.returncode}"
        raise add_unit.Refusal(f"cannot read tmux sessions: {detail}")
    return {pane: name for line in result.stdout.splitlines() if "\t" in line
            for name, pane in [line.split("\t", 1)]}


def _registry_collisions(claude: ClaudeSide, old: str, new: str) -> None:
    settings = showrunners.load_settings() if showrunners.CONFIG.exists() else showrunners.defaults()
    if any(runner["session"] == new for runner in settings["showrunners"]):
        raise add_unit.Refusal(f"showrunner registry session {new} is already taken")
    units = [unit.name for runner in settings["showrunners"] for unit in runner["units"]]
    if new in units and (old in units or isinstance(claude, AwaitingRename)):
        raise add_unit.Refusal(f"showrunner registry unit {new} is already taken")


def preflight(production_path: Path, old: str, new: str) -> RenamePlan:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", new):
        raise add_unit.Refusal("new name must contain only letters, numbers, hyphens, or underscores")
    if new == old:
        raise add_unit.Refusal("new name must differ from old name")
    production = add_unit.read_production(production_path)
    branch = add_unit.git(production, "branch", "--show-current").stdout.strip()
    if branch != production.merge_branch:
        raise add_unit.Refusal(f"Showrunner checkout is not on {production.merge_branch}")
    claude = _claude_side(old, new)
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    row = _row_side(lines, claude, old, new)
    pane_names = _tmux_panes()
    if new in pane_names.values() and pane_names.get(claude.pane) != new:
        raise add_unit.Refusal(f"tmux session {new} is already taken by another pane")
    _registry_collisions(claude, old, new)
    fields = _row_fields(row.row)
    unit = add_unit.cell_value(fields[0])
    return RenamePlan(production, claude, row, unit)


def _wait_for_claude(session_id: str, new: str, wait: RenameWait) -> bool:
    record = _session_record(session_id)
    if isinstance(record, LiveSessionRecord) and record.fields.get("name") == new:
        return True
    for _ in range(wait.seconds):
        wait.sleep(1)
        record = _session_record(session_id)
        if isinstance(record, LiveSessionRecord) and record.fields.get("name") == new:
            return True
    return False


def _type_rename(claude: AwaitingRename, new: str) -> None:
    literal = subprocess.run([tmux_names.TMUX, "send-keys", "-t", claude.pane, "-l", f"/rename {new}"],
                             capture_output=True, text=True, check=False)
    if literal.returncode != 0:
        raise OSError(literal.stderr.strip() or f"tmux send-keys exited {literal.returncode}")
    enter = subprocess.run([tmux_names.TMUX, "send-keys", "-t", claude.pane, "Enter"],
                           capture_output=True, text=True, check=False)
    if enter.returncode != 0:
        raise OSError(enter.stderr.strip() or f"tmux send-keys exited {enter.returncode}")


def _replace_cell_name(cell: str, old: str, new: str) -> str:
    quoted = re.search(r"`([^`]+)`", cell)
    if quoted is not None and quoted.group(1) == old:
        return cell[:quoted.start(1)] + new + cell[quoted.end(1):]
    start = len(cell) - len(cell.lstrip())
    if cell[start:start + len(old)] != old:
        raise add_unit.Refusal(f"Session cell no longer starts with {old}")
    return cell[:start] + new + cell[start + len(old):]


def rewrite_row(production: add_unit.Production, row: str, old: str, new: str) -> None:
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    try:
        index = lines.index(row)
    except ValueError as error:
        raise add_unit.Refusal("Units row changed after preflight") from error
    cells = row.split("|")
    if len(cells) < 7:
        raise add_unit.Refusal("Units row has no Session cell")
    cells[5] = _replace_cell_name(cells[5], old, new)
    lines[index] = "|".join(cells)
    _ = production.doc.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _commit_row(plan: RenamePlan, old: str, new: str) -> list[str]:
    production = plan.production
    changed: list[str] = []
    if isinstance(plan.row, RowNamesOld):
        rewrite_row(production, plan.row.row, old, new)
        changed.append("production Units row")
    path = production.doc.relative_to(production.checkout)
    dirty = bool(add_unit.git(production, "status", "--porcelain", "--", str(path)).stdout)
    head_before = add_unit.git(production, "rev-parse", production.merge_branch).stdout.strip()
    push_needed = add_unit.remote_head(production, production.merge_branch) != head_before
    add_unit.commit_paths_and_push(
        production,
        [production.doc],
        f"production({production.slug}): {plan.unit}'s session is now {new}",
    )
    if dirty:
        changed.append("production commit")
        push_needed = True
    if push_needed:
        changed.append("merge branch push")
    return changed


def rename(production_path: Path, scratch: Path, old: str, new: str, wait: RenameWait = WAIT) -> int:
    plan = preflight(production_path, old, new)
    if isinstance(plan.claude, AwaitingRename):
        _type_rename(plan.claude, new)
        if not _wait_for_claude(plan.claude.session_id, new, wait):
            print("rename_unit: the session did not take the name within 30 seconds; nothing else was changed",
                  file=sys.stderr)
            return 1
        print("renamed: Claude session")

    tmux_result = tmux_names.rename_session(plan.claude.pane, old, new)
    if isinstance(tmux_result, tmux_names.RenameIncomplete):
        print(f"rename_unit: {tmux_result.reason}", file=sys.stderr)
        return 1
    for description in tmux_result.descriptions:
        print(f"renamed: {description}")

    for description in _commit_row(plan, old, new):
        print(f"renamed: {description}")
    try:
        state_changes = rename_state.rename_all(old, new, scratch)
    except rename_state.RenameRefused as error:
        print(f"rename_unit: {error.reason}", file=sys.stderr)
        return 1
    for description in state_changes:
        print(f"renamed: {description}")

    print("left: remote-control name — fixed at launch")
    print("left: systemd scope — fixed at launch")
    print(f"left: production LOG entry added {plan.unit} …, tmux {old} — history")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("--scratch", required=True)
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    args = parser.parse_args(argv)
    try:
        return rename(Path(cast(str, args.production)), Path(cast(str, args.scratch)),
                      cast(str, args.old), cast(str, args.new), WAIT)
    except add_unit.Refusal as error:
        print(f"rename_unit: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"rename_unit: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
