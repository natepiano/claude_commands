#!/usr/bin/env python3
"""Move Claude sessions to another account the one way a showrunner does: pause first, then prove it.

    showrunner_move.py session <session id|name> <claudeN> [--why <text>]
    showrunner_move.py units|showrunners <claudeN> [--dry-run] [--why <text>]

Units and showrunners are each one group on one account: `units` and `showrunners` select the
group's account and move every live member that is not on it. Each move goes through
`accounts.py move`: the session is asked to pause, stopped at its ready mark and resumed on the
target, and a unit's showrunner is told before it stops and when it is back. This script then waits for each move to end and checks the
resumed process itself: its CLAUDE_CONFIG_DIR must be the target's. It prints one line a session and
exits 0 only when every session is proven on the target.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "whoami"))
sys.path.insert(0, str(ROOT / "scripts" / "shutdown"))
sys.path.insert(0, str(ROOT / "scripts" / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.accounts import accounts, moves  # noqa: E402

if TYPE_CHECKING:
    from scripts.shutdown import shutdown_record
    from scripts.message import sessions
    from scripts.whoami import account, note_file
else:
    import account
    import note_file
    import shutdown_record
    import sessions

WAIT_MINUTES = 20
POLL_SECONDS = 10.0
NOT_BACK = frozenset({"restart failed", "manual restart"})
# The line `accounts.move_group` prints for a showrunner it leaves where it is: one with no running session.
UNREGISTERED = "update_registration.py register"


@dataclass(frozen=True)
class Proven:
    pid: int
    config_dir: Path


@dataclass(frozen=True)
class NotMoved:
    reason: str


@dataclass(frozen=True)
class MovesItself:
    pass


Outcome = Proven | NotMoved | MovesItself


@dataclass(frozen=True)
class Started:
    session_id: str
    name: str


class CommandLine(argparse.Namespace):
    kind: Literal["session", "units", "showrunners"] = "session"
    rest: list[str] = []
    dry_run: bool = False
    why: str = ""
    wait_minutes: float = WAIT_MINUTES


def target_dir(target: str) -> Path:
    return accounts.claude_account_dir(target)


def live_names() -> dict[str, tuple[str, int]]:
    """Each live session's name and pid, by session id."""
    return {item["sessionId"]: (item["name"], item["pid"]) for item in sessions.live_sessions()}


def start(options: CommandLine, target: str) -> tuple[list[Started], list[str], list[str]]:
    """Ask for each move: the started sessions, one line for each session not moved, and the notes."""
    live = live_names()
    if options.kind == "session":
        moved = accounts.move(options.rest[0], target, why=options.why)
        if isinstance(moved, accounts.MoveRefused):
            return [], [f"{options.rest[0]}: not moved: {moved.reason}"], []
        if isinstance(moved, accounts.MoveStartFailed):
            return [], [f"{options.rest[0]}: not moved: {(moved.stderr or moved.stdout).strip() or 'shutdown refused'}"], []
        notes = [text for text in moved.stdout.splitlines()
                 if text.startswith(("no fresh usage reading for ", "to move them back, in this terminal: "))]
        return [Started(moved.session_id, live.get(moved.session_id, (moved.session_id, 0))[0])], [], notes
    group = options.kind
    moved_group = accounts.move_group(group, target, dry_run=options.dry_run, why=options.why)
    if isinstance(moved_group, accounts.MoveRefused):
        return [], [f"{group}: not moved: {moved_group.reason}"], []
    if isinstance(moved_group, accounts.GroupMoveDryRun):
        return [], [], list(moved_group.lines)
    printed = moved_group.stdout.splitlines()
    refused = [text for text in printed if UNREGISTERED in text]
    notes = [text for text in printed if text == f"{group}: no live session moves"
             or text.startswith(("no fresh usage reading for ", "to move them back, in this terminal: "))]
    if isinstance(moved_group, accounts.GroupMoveStartFailed):
        refused += [f"{group}: {text}" for text in (moved_group.stderr or moved_group.stdout).strip().splitlines()
                    if UNREGISTERED not in text and text not in notes]
    return [Started(session_id, live.get(session_id, (session_id, 0))[0]) for session_id in moved_group.session_ids], refused, notes


def _confirmed(session_id: str, since: str) -> bool:
    return any(entry["kind"] == "confirmed" and entry["subject"].startswith(session_id + ": ") and entry["at"] >= since
               for entry in moves.read_moves())


def prove(session_id: str, wanted: Path) -> Outcome:
    """Read the resumed process itself: its account folder must be the target's."""
    live = live_names().get(session_id)
    if live is None:
        return NotMoved("no live process after the move")
    setting = account.process_account_directory(account.process_config_dir(live[1]))
    if not isinstance(setting, account.ConfiguredClaudeFolder):
        return NotMoved(f"account of pid {live[1]} unreadable")
    directory = setting.path
    if directory.resolve() != wanted.resolve():
        return NotMoved(f"pid {live[1]} runs on {directory}, not {wanted}")
    return Proven(live[1], directory)


def settle(started: Started, wanted: Path, since: str, deadline: float, *,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """Wait for the session's move to end, then prove where it came back."""
    if started.session_id == os.environ.get("CLAUDE_CODE_SESSION_ID", ""):
        return MovesItself()
    while True:
        saved = next((item for item in shutdown_record.all_session_records()
                      if item["session"]["session_id"] == started.session_id), None)
        if saved is not None and saved["progress"]["kind"] in NOT_BACK:
            progress = saved["progress"]
            return NotMoved(progress["reason"] if progress["kind"] == "restart failed" else "it needs a manual restart")
        if saved is None:
            if not _confirmed(started.session_id, since):
                return NotMoved("its stop ended without a confirmed move")
            return prove(started.session_id, wanted)
        if clock() >= deadline:
            return NotMoved(f"still {saved['progress']['kind']} when this check stopped waiting")
        sleep(POLL_SECONDS)


def line(started: Started, target: str, outcome: Outcome) -> str:
    if isinstance(outcome, Proven):
        return f"{started.name}: on {target}, proven (pid {outcome.pid} runs on {outcome.config_dir})"
    if isinstance(outcome, MovesItself):
        return (f"{started.name}: this session moves too; when it is back, the restart proves its account "
                + "and tells the user if it is not on " + target)
    return f"{started.name}: not on {target}: {outcome.reason}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("kind", choices=("session", "units", "showrunners"))
    _ = parser.add_argument("rest", nargs="+")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--why", default="")
    _ = parser.add_argument("--wait-minutes", type=float, default=WAIT_MINUTES)
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    options = cast(CommandLine, parser.parse_args(argv))
    wanted_args = 2 if options.kind == "session" else 1
    if len(options.rest) != wanted_args:
        parser.error(f"{options.kind} takes " + ("<session> <target>" if wanted_args == 2 else "<target>"))
    if options.kind == "session" and options.dry_run:
        parser.error("--dry-run applies to units and showrunners")
    target = note_file.canonical_label(options.rest[-1])
    since = datetime.now(timezone.utc).isoformat()
    try:
        wanted = target_dir(target)
        started, lines, notes = start(options, target)
        deadline = time.monotonic() + options.wait_minutes * 60
        outcomes = [(item, settle(item, wanted, since, deadline)) for item in started]
    except (accounts.AccountsError, shutdown_record.InvalidRecord, OSError, ValueError) as error:
        print(f"not moved: {error}", file=sys.stderr)
        return 1
    failed = bool(lines) or any(isinstance(outcome, NotMoved) for _, outcome in outcomes)
    print("\n".join([*notes, *lines, *(line(item, target, outcome) for item, outcome in outcomes)]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
