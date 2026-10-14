#!/usr/bin/env python3
"""Promote a running unit director to the showrunner of its own production, under this showrunner.

Usage: promote_showrunner.py <unit> --production <child production doc> --model <id> --effort <level> [--check]

The parent showrunner runs it, once the unit has written the child's production doc and committed it.
The unit gets `/exit`; once its Claude is gone its host session is ended, and add_showrunner.py resumes
the same conversation as the child's showrunner: in tmux, with no unit mark, on its retained account,
attached to this showrunner's production. The unit's Units row is then retired and committed in this
showrunner's checkout. --check runs every refusal and stops nothing.

Exit 0 when the promoted session runs as the child's showrunner and is attached; 1 when the unit was
stopped and the promotion did not finish, with what to run printed; 2 on a refusal, with nothing stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "whoami"))

import add_showrunner
import showrunners
import unit_effort
import unit_lookup
import herdr
from session_pane import HerdrFailure, read_place, type_place, where_place
from add_unit import (Production, Refusal, cell_value, commit_paths, plan_cell_is_retired, read_production,
                      unit_headings, unit_rows, unit_table)
from scripts.whoami import account

STOP_WAIT_S = float(os.environ.get("PROMOTE_STOP_WAIT_S") or 60)
POLL_S = 0.5


class Promotion(NamedTuple):
    # The unit id, `<name>-unit`, as its row and its mark name it.
    unit: str
    parent: Production
    child: Production
    marked: unit_lookup.MarkedUnit
    claude: unit_lookup.LiveClaude
    worktree: Path
    # The unit's Plan cell value: the line that brings it back as a unit names it.
    plan: str


def _tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([unit_lookup.tmux_binary(), *arguments], capture_output=True, text=True, check=False)


def _git(where: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(where), *arguments], capture_output=True, text=True, check=False)


def active_dir() -> Path:
    return Path(os.environ.get("PLAN_DELEGATE_ACTIVE_DIR") or "/tmp/claude/delegate/active")


def _unit_row(lines: list[str], unit: str) -> dict[str, str]:
    for cells in unit_table(lines):
        if cell_value(cells.get("Unit", "")) == unit:
            if plan_cell_is_retired(cells.get("Plan", "")):
                raise Refusal(f"{unit} is retired")
            return cells
    raise Refusal(f"{unit} has no row in the Units table")


def _session_status(pid: int) -> str:
    """The session record's `status`: `busy` while a turn runs."""
    try:
        record = cast(object, json.loads((unit_lookup.sessions_dir() / f"{pid}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise Refusal(f"cannot read the unit's session record: {error}") from error
    status = cast(dict[str, object], record).get("status") if isinstance(record, dict) else None
    return status if isinstance(status, str) else ""


def _quiet(unit: str, marked: unit_lookup.MarkedUnit, claude: unit_lookup.LiveClaude) -> None:
    """Refuse a unit mid-turn or with a form, prompt or draft on screen: `/exit` would land in it."""
    if _session_status(claude.pid) == "busy":
        raise Refusal(f"{unit} is mid-turn; promote it once its turn ends")
    try:
        shown = read_place(marked.place, ansi=True)
    except (OSError, HerdrFailure) as error:
        raise Refusal(f"cannot read {unit}'s pane: {error}") from error
    verdict = unit_effort.verdict(shown)
    if isinstance(verdict, unit_effort.Skipped):
        raise Refusal(f"{unit}'s pane: {verdict.reason}")


def _settled(unit: str, claude: unit_lookup.LiveClaude, worktree: Path) -> None:
    """Refuse a unit with a run open, whose workers end only with the run, or with work uncommitted."""
    if (active_dir() / claude.session_id).exists():
        raise Refusal(f"{unit} has a /unit:delegate run open, and its workers with it; the run finishes first")
    status = _git(worktree, "status", "--porcelain")
    if status.returncode != 0:
        raise Refusal(f"cannot read {unit}'s worktree {worktree}: {status.stderr.strip()}")
    if status.stdout.strip():
        raise Refusal(f"{unit}'s worktree {worktree} has uncommitted work; it checkpoints first")


def _committed(doc: Path) -> None:
    tracked = _git(doc.parent, "ls-files", "--error-unmatch", "--", doc.name)
    if tracked.returncode != 0 or _git(doc.parent, "status", "--porcelain", "--", doc.name).stdout.strip():
        raise Refusal(f"{doc} is not committed; the unit commits it first")


def checked(name: str, child_doc: Path) -> Promotion:
    """Everything the promotion needs. Raises Refusal naming what is in the way."""
    parent_doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if parent_doc is None:
        raise Refusal("this session runs no production, so it has no unit to promote")
    parent = read_production(parent_doc)
    unit = name if name.replace("_", "-").endswith("-unit") else f"{name}-unit"
    try:
        cells = _unit_row(parent.doc.read_text(encoding="utf-8").splitlines(), unit)
        marked = unit_lookup.marked_units(parent.slug).get(unit)
    except (OSError, UnicodeError) as error:
        raise Refusal(str(error)) from error
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        raise Refusal(f"{unit} has no live session under {parent.slug}")
    claude = marked.claude
    _quiet(unit, marked, claude)
    worktree = Path(cell_value(cells.get("Worktree", ""))).expanduser()
    _settled(unit, claude, worktree)
    child = read_production(child_doc)
    _committed(child.doc)
    # Only the unit's tmux session is exempt from the destination's tmux name collision.
    directory = recorded_account_directory(marked)
    ending = marked.label if isinstance(marked.place, unit_lookup.TmuxPlace) else ""
    _ = add_showrunner.checked(child, parent.slug, ending=ending, start_in=worktree, config_dir=directory)
    return Promotion(unit, parent, child, marked, claude, worktree, cell_value(cells.get("Plan", "")))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(promotion: Promotion) -> bool:
    """Type `/exit` and end its host session once Claude is gone; False when it outlived STOP_WAIT_S.

    Two processes on one session corrupt it, so nothing may resume it while this one runs.
    """
    place = promotion.marked.place
    try:
        type_place(place, "/exit")
    except (OSError, HerdrFailure) as error:
        raise Refusal(f"cannot type /exit into {promotion.unit}'s pane: {error}") from error
    deadline = time.monotonic() + STOP_WAIT_S
    while _alive(promotion.claude.pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    if isinstance(place, unit_lookup.TmuxPlace):
        ended = _tmux("kill-session", "-t", place.pane)
        if ended.returncode:
            raise Refusal(f"cannot end {promotion.unit}'s session: {ended.stderr.strip()}")
    else:
        try:
            herdr.stop_session(promotion.marked.label)
        except HerdrFailure as error:
            raise Refusal(f"cannot end {promotion.unit}'s session: {error}") from error
    return True


def recorded_account_directory(marked: unit_lookup.MarkedUnit) -> Path:
    if isinstance(marked.place, unit_lookup.TmuxPlace):
        held = _tmux("show-options", "-v", "-t", f"={marked.label}:", "@claude_config_dir")
        if held.returncode != 0 or not held.stdout.strip():
            raise Refusal("no account recorded for this unit; resume with --account before promoting")
        directory = Path(held.stdout.strip()).expanduser()
    else:
        if not isinstance(marked.claude, unit_lookup.LiveClaude):
            raise Refusal(f"{marked.unit}: no live Claude to read its account")
        directory = account.process_config_dir(marked.claude.pid)
        if directory is None:
            raise Refusal(f"{marked.unit}: cannot read its live Claude's account environment")
    if not directory.is_dir():
        raise Refusal(f"recorded resume account directory {directory} is missing")
    return directory


def account_line() -> str:
    return "same account, no move"


def retire(promotion: Promotion) -> None:
    """Mark the unit's Units row retired, promoted to the child's showrunner, and commit the doc."""
    doc = promotion.parent.doc
    lines = doc.read_text(encoding="utf-8").split("\n")
    headings = unit_headings(lines)
    _, rows = unit_rows(lines)
    plan_at = headings.index("Plan") + 1
    for index, line in enumerate(lines):
        if line not in rows:
            continue
        parts = line.split("|")
        if cell_value(parts[headings.index("Unit") + 1].strip()) != promotion.unit:
            continue
        date = datetime.now(promotion.parent.zone).strftime("%Y-%m-%d")
        note = f"(retired {date}: promoted to showrunner of {promotion.child.slug})"
        parts[plan_at] = f" {note} {parts[plan_at].strip()} "
        lines[index] = "|".join(parts)
        break
    _ = doc.write_text("\n".join(lines), encoding="utf-8")
    commit_paths(promotion.parent, [doc], f"production({promotion.parent.slug}): retire {promotion.unit}, "
                 + f"promoted to showrunner of {promotion.child.slug}")


def restore_line(promotion: Promotion, retained: Path) -> str:
    """The command that resumes the stopped session as the unit it was."""
    return "~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py " + shlex.join([
        "--production", str(promotion.parent.doc), promotion.unit.removesuffix("-unit"), "--plan", promotion.plan,
        "--resume", promotion.claude.session_id, "--cwd", str(promotion.worktree), "--config-dir", str(retained)])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Promote a running unit to the showrunner of its own production.")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--production", required=True, type=Path, help="the child's production doc")
    _ = parser.add_argument("--model", required=True, help="pass your own model id")
    _ = parser.add_argument("--effort", required=True, help="pass your own effort level")
    _ = parser.add_argument("--check", action="store_true", help="run every refusal and stop nothing")
    args = parser.parse_args(argv)
    try:
        promotion = checked(cast(str, args.unit), cast(Path, args.production))
        retained = recorded_account_directory(promotion.marked)
        moved = account_line()
    except Refusal as refusal:
        print(f"promote_showrunner: {refusal}", file=sys.stderr)
        return 2
    unit, slug = promotion.unit, promotion.child.slug
    if cast(bool, args.check):
        host = "tmux" if isinstance(promotion.marked.place, unit_lookup.TmuxPlace) else "herdr"
        print(f"check: ok — would stop {unit} ({host} {promotion.marked.label}), resume it as the showrunner of "
              + f"{slug} in {promotion.worktree} under {promotion.parent.slug}, and retire its row; {moved}")
        return 0
    try:
        stopped = stop(promotion)
    except Refusal as refusal:
        print(f"promote_showrunner: {refusal}", file=sys.stderr)
        return 2
    if not stopped:
        print(f"{unit}'s Claude (pid {promotion.claude.pid}) still runs {STOP_WAIT_S:g} s after /exit, so nothing "
              + f"was restarted. Its pane is {where_place(promotion.marked.place)}; read it with "
              + f"`~/.claude/scripts/lib/py ~/.claude/scripts/production/session_pane.py read session {promotion.claude.session_id}`.")
        return 1
    started = add_showrunner.main(["--production", str(promotion.child.doc), "--model", cast(str, args.model),
                                   "--effort", cast(str, args.effort), "--resume", promotion.claude.session_id,
                                   "--cwd", str(promotion.worktree), "--config-dir", str(retained)])
    if started == 2:
        print(f"{unit} is stopped and did not start as a showrunner; its Units row is unchanged. "
              + f"Bring it back as the unit it was with: {restore_line(promotion, retained)}")
        return 1
    try:
        retire(promotion)
    except (OSError, subprocess.CalledProcessError, Refusal) as error:
        print(f"{unit} now runs as the showrunner of {slug}, but its Units row was not retired: {error}")
        return 1
    print(f"{unit} is now the showrunner of {slug}; {moved}")
    return started


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
