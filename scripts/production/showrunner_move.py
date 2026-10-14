#!/usr/bin/env python3
"""Move Claude sessions to another account the one way a showrunner does: pause first, then prove it.

    showrunner_move.py session <session id|name> "claude N"|default [--why <text>]
    showrunner_move.py units "claude N"|default [--from "claude N"] [--why <text>]
    showrunner_move.py showrunners --from "claude N" "claude N"|default [--why <text>]

`units` moves this showrunner's own units. Each move goes through `accounts.py move`: the session is
asked to pause, stopped at its ready mark and resumed on the target, and a unit's showrunner is told
before it stops and when it is back. This script then waits for each move to end and checks the
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
    from scripts.shutdown import record
    from scripts.message import sessions
    from scripts.whoami import account
    from . import showrunners, unit_lookup
else:
    import account
    import record
    import sessions
    import showrunners
    import unit_lookup

WAIT_MINUTES = 20
POLL_SECONDS = 10.0
NOT_BACK = frozenset({"restart failed", "manual restart"})
# The endings of the lines `accounts.move_showrunners` prints for a showrunner it leaves where it is.
SKIPPED = ("already has a shutdown in progress", "--production", ".md`")


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
    source: str = ""
    why: str = ""
    wait_minutes: float = WAIT_MINUTES


def target_dir(target: str) -> Path:
    return accounts.main_config_dir() if target == "default" else accounts.account_dir(target)


def live_names() -> dict[str, tuple[str, int]]:
    """Each live session's name and pid, by session id."""
    return {item["sessionId"]: (item["name"], item["pid"]) for item in sessions.live_sessions()}


def own_units(source: str) -> list[str]:
    """The session ids of this showrunner's live units, only those on `source` when it is given."""
    slug = showrunners.own_slug()
    if not slug:
        raise accounts.AccountsError("this session runs no production, so it has no units to move")
    found = [marked.claude for marked in unit_lookup.marked_units(slug).values()
             if isinstance(marked.claude, unit_lookup.LiveClaude)]
    if source:
        wanted = target_dir(source).resolve()
        found = [claude for claude in found
                 if (directory := account.process_config_dir(claude.pid)) is not None and directory.resolve() == wanted]
    return [claude.session_id for claude in found]


def start(options: CommandLine, target: str) -> tuple[list[Started], list[str]]:
    """Ask for each move; the started sessions and one line for each move refused before it began."""
    live = live_names()
    refused: list[str] = []
    if options.kind == "session":
        moved = accounts.move(options.rest[0], target, why=options.why)
        if isinstance(moved, accounts.MoveRefused):
            return [], [f"{options.rest[0]}: not moved: {moved.reason}"]
        if isinstance(moved, accounts.MoveStartFailed):
            return [], [f"{options.rest[0]}: not moved: {(moved.stderr or moved.stdout).strip() or 'shutdown refused'}"]
        ids = [moved.session_id]
    elif options.kind == "units":
        results = accounts.move_sessions(tuple(own_units(options.source)), target, why=options.why or "moved by its showrunner")
        refused = [f"{live.get(item.session_id, (item.session_id, 0))[0]}: not moved: {item.reason}"
                   for item in results if isinstance(item, accounts.SessionMoveRefused)]
        ids = [item.session_id for item in results if isinstance(item, accounts.SessionMoveRequested)]
    else:
        moved_runners = accounts.move_showrunners(options.source, target, why=options.why)
        if isinstance(moved_runners, accounts.MoveRefused):
            return [], [f"showrunners: not moved: {moved_runners.reason}"]
        if isinstance(moved_runners, accounts.ShowrunnersMoveDryRun):
            return [], list(moved_runners.lines)
        refused = [line for line in moved_runners.stdout.splitlines() if line.endswith(SKIPPED)]
        if isinstance(moved_runners, accounts.ShowrunnersMoveStartFailed):
            refused += [f"showrunners: {line}" for line in (moved_runners.stderr or moved_runners.stdout).strip().splitlines()]
        ids = list(moved_runners.session_ids)
    return [Started(session_id, live.get(session_id, (session_id, 0))[0]) for session_id in ids], refused


def _confirmed(session_id: str, since: str) -> bool:
    return any(entry["kind"] == "confirmed" and entry["subject"].startswith(session_id + ": ") and entry["at"] >= since
               for entry in moves.read_moves())


def prove(session_id: str, wanted: Path) -> Outcome:
    """Read the resumed process itself: its account folder must be the target's."""
    live = live_names().get(session_id)
    if live is None:
        return NotMoved("no live process after the move")
    directory = account.process_config_dir(live[1])
    if directory is None:
        return NotMoved(f"account of pid {live[1]} unreadable")
    if directory.resolve() != wanted.resolve():
        return NotMoved(f"pid {live[1]} runs on {directory}, not {wanted}")
    return Proven(live[1], directory)


def settle(started: Started, wanted: Path, since: str, deadline: float, *,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """Wait for the session's move to end, then prove where it came back."""
    if started.session_id == os.environ.get("CLAUDE_CODE_SESSION_ID", ""):
        return MovesItself()
    while True:
        saved = next((item for item in record.all_session_records()
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
    _ = parser.add_argument("--from", dest="source", default="")
    _ = parser.add_argument("--why", default="")
    _ = parser.add_argument("--wait-minutes", type=float, default=WAIT_MINUTES)
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    options = cast(CommandLine, parser.parse_args(argv))
    wanted_args = 2 if options.kind == "session" else 1
    if len(options.rest) != wanted_args:
        parser.error(f"{options.kind} takes " + ("<session> <target>" if wanted_args == 2 else "<target>"))
    if options.kind == "showrunners" and not options.source:
        parser.error("showrunners requires --from \"claude N\"")
    if options.kind == "session" and options.source:
        parser.error("--from applies to units and showrunners")
    target = options.rest[-1]
    since = datetime.now(timezone.utc).isoformat()
    try:
        wanted = target_dir(target)
        started, lines = start(options, target)
        deadline = time.monotonic() + options.wait_minutes * 60
        outcomes = [(item, settle(item, wanted, since, deadline)) for item in started]
    except (accounts.AccountsError, record.InvalidRecord, OSError, ValueError) as error:
        print(f"not moved: {error}", file=sys.stderr)
        return 1
    lines += [line(item, target, outcome) for item, outcome in outcomes]
    if not lines:
        lines = [f"nothing to move to {target}"]
    print("\n".join(lines))
    return 0 if outcomes and all(not isinstance(outcome, NotMoved) for _, outcome in outcomes) and len(lines) == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
