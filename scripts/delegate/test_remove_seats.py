#!/usr/bin/env python3
"""Tests for remove_seats.py against a stub `claude` binary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.delegate import remove_seats

SCRIPT = Path(__file__).parent / "remove_seats.py"

# `agents --json` prints the rows file; `rm <id>` records the id. A missing
# rows file makes the listing fail, the way an unreachable daemon would.
STUB_CLAUDE = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  agents) cat "${STUB_ROWS}" ;;
  rm) printf '%s\\n' "$2" >> "${STUB_REMOVED}" ;;
  *) exit 2 ;;
esac
"""

ORCHESTRATOR = "aaaaaaaa-0000-0000-0000-000000000000"
GONE_ORCHESTRATOR = "bbbbbbbb-0000-0000-0000-000000000000"
LEGACY_DEAD = "c1596298-9325-4c8b-961a-ba976e73a3f5"
LEGACY_LIVE = "d2222222-2222-2222-2222-222222222222"


def row(seat_id: str, name: str, kind: str = "background") -> dict[str, str]:
    return {"id": seat_id, "sessionId": f"{seat_id}-full", "kind": kind, "name": name}


class RemoveSeatsTest(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    removed: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    rows: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    claude: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    state_root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "delegate"
        (self.root / "active").mkdir(parents=True)
        self.removed = base / "removed"
        self.rows = base / "rows.json"
        self.claude = base / "claude"
        self.state_root = base / "shutdown"
        _ = self.claude.write_text(STUB_CLAUDE, encoding="utf-8")
        self.claude.chmod(0o755)

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_dir(self, name: str, seats: list[str], orchestrator: str | None = None) -> Path:
        run = self.root / name
        run.mkdir()
        _ = (run / "seats").write_text(
            "".join(f"{seat}\tproject-{seat}\n" for seat in seats), encoding="utf-8"
        )
        if orchestrator is not None:
            _ = (self.root / "active" / orchestrator).write_text(f"{run}\n", encoding="utf-8")
        return run

    def list_rows(self, rows: list[dict[str, str]]) -> None:
        listed = [*rows, {"id": "aaaaaaaa", "sessionId": ORCHESTRATOR, "kind": "interactive", "name": "me"}]
        _ = self.rows.write_text(json.dumps(listed), encoding="utf-8")

    def write_shutdown_record(self, session_id: str, state: str) -> None:
        account = self.state_root / "owner@example.com"
        account.mkdir(parents=True)
        record: dict[str, object] = {
            "login": "owner@example.com",
            "label": "claude 2",
            "machine": "natedev",
            "state": state,
            "requested_at": "2026-10-09T21:49:10+00:00",
            "requested_by": {"kind": "terminal"},
            "scope": {"kind": "all account sessions"},
            "conductor": {"kind": "not started"},
            "force": "wait for ready",
            "entries": [
                {
                    "session": {
                        "kind": "top-level",
                        "session_id": session_id,
                        "pid": 1000,
                        "proc_start": "start-director",
                        "name": "director",
                        "cwd": "/tmp/director",
                        "status": "idle",
                        "model": {"kind": "no reply yet"},
                        "checkout": {"kind": "not a checkout"},
                        "run_dirs": [],
                        "codex_servers": [],
                        "timers": [],
                        "host": {"kind": "unknown"},
                    },
                    "timers": [],
                    "settle_message": {"kind": "not sent"},
                    "where": {"kind": "not said"},
                    "progress": {"kind": "waiting"},
                }
            ],
        }
        _ = (account / "record.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    def run_remove_seats(
        self, session_dir: Path | None = None, caller: str = ""
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        environment = os.environ.copy()
        environment.update(
            CLAUDE_BIN=str(self.claude),
            STUB_ROWS=str(self.rows),
            STUB_REMOVED=str(self.removed),
            CLAUDE_CODE_SESSION_ID=caller,
            SHUTDOWN_STATE_DIR=str(self.state_root),
        )
        command = [sys.executable, str(SCRIPT), "--delegate-root", str(self.root)]
        if session_dir is not None:
            command += ["--session-dir", str(session_dir)]
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, check=False
        )
        removed = (
            self.removed.read_text(encoding="utf-8").split()
            if self.removed.exists()
            else []
        )
        return result, sorted(removed)

    def remove_seats(self, session_dir: Path | None = None, caller: str = "") -> tuple[int, list[str]]:
        result, removed = self.run_remove_seats(session_dir, caller)
        return result.returncode, sorted(removed)

    def test_own_run_goes_even_while_live(self) -> None:
        own = self.run_dir("own", ["own1", "own2"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("own1", "p-impl"), row("own2", "p-test")])
        self.assertEqual(self.remove_seats(own), (0, ["own1", "own2"]))

    def test_live_run_is_kept_and_dead_runs_are_swept(self) -> None:
        _ = self.run_dir("live", ["live1"], orchestrator=ORCHESTRATOR)
        _ = self.run_dir("orphaned", ["orph1"], orchestrator=GONE_ORCHESTRATOR)
        _ = self.run_dir("ended", ["end1"])
        self.list_rows([row("live1", "a-impl"), row("orph1", "b-impl"), row("end1", "c-test")])
        self.assertEqual(self.remove_seats(), (0, ["end1", "orph1"]))

    def test_fresh_heartbeat_keeps_a_run_without_a_marker(self) -> None:
        fresh = self.run_dir("fresh", ["fresh1"])
        stale = self.run_dir("stale", ["stale1"])
        _ = (fresh / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        _ = (stale / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        hour_ago = time.time() - 3600
        os.utime(stale / "heartbeat.log", (hour_ago, hour_ago))
        self.list_rows([row("fresh1", "a-impl"), row("stale1", "b-impl")])
        self.assertEqual(self.remove_seats(), (0, ["stale1"]))

    def test_legacy_names_follow_their_run(self) -> None:
        _ = self.run_dir(LEGACY_LIVE, [], orchestrator=ORCHESTRATOR)
        self.list_rows([
            row("old1", f"{LEGACY_DEAD}-test"),
            row("old2", f"{LEGACY_DEAD}-impl"),
            row("kept", f"{LEGACY_LIVE}-impl"),
        ])
        self.assertEqual(self.remove_seats(), (0, ["old1", "old2"]))

    def test_only_listed_background_seats_go(self) -> None:
        _ = self.run_dir("ended", ["gone1", "inter1"])
        self.list_rows([row("inter1", "someone", kind="interactive"), row("friend", "a-friend")])
        self.assertEqual(self.remove_seats(), (0, []))

    def test_never_removes_the_calling_session(self) -> None:
        _ = self.run_dir("ended", ["self", "seat"])
        self.list_rows([row("self", "orchestrator"), row("seat", "p-impl")])
        self.assertEqual(self.remove_seats(caller="self-full"), (0, ["seat"]))

    def test_failed_listing_removes_nothing(self) -> None:
        _ = self.run_dir("ended", ["end1"])
        self.assertEqual(self.remove_seats(), (1, []))

    def test_shutdown_query_returns_named_held_sessions(self) -> None:
        completed = subprocess.CompletedProcess[str](
            ["shutdown", "held-sessions"], 0, "first\n\nsecond\n", ""
        )
        with patch.object(subprocess, "run", return_value=completed):
            result = remove_seats.shutdown_held_sessions()

        self.assertEqual(
            result,
            {"kind": "held sessions", "session_ids": {"first", "second"}},
        )

    def test_shutdown_query_failure_names_unreadable_state(self) -> None:
        completed = subprocess.CompletedProcess[str](
            ["shutdown", "held-sessions"], 2, "", "bad state\n"
        )
        with patch.object(subprocess, "run", return_value=completed):
            result = remove_seats.shutdown_held_sessions()

        self.assertEqual(
            result,
            {
                "kind": "shutdown state unreadable",
                "reason": "held-sessions exited 2: bad state",
            },
        )

    def test_environment_cannot_replace_shutdown_command(self) -> None:
        with patch.dict(
            os.environ, {"REMOVE_SEATS_SHUTDOWN_COMMAND": "/tmp/not-shutdown"}
        ):
            command = remove_seats.shutdown_command()

        self.assertEqual(
            command, [sys.executable, str(remove_seats.SHUTDOWN)]
        )

    def test_down_shutdown_record_keeps_its_directors_run(self) -> None:
        _ = self.run_dir("held", ["held1"], orchestrator=GONE_ORCHESTRATOR)
        self.write_shutdown_record(GONE_ORCHESTRATOR, "down")
        self.list_rows([row("held1", "held-test")])

        self.assertEqual(self.remove_seats(), (0, []))

    def test_restarting_shutdown_record_keeps_its_directors_run(self) -> None:
        _ = self.run_dir("held", ["held1"], orchestrator=GONE_ORCHESTRATOR)
        self.write_shutdown_record(GONE_ORCHESTRATOR, "restarting")
        self.list_rows([row("held1", "held-test")])

        self.assertEqual(self.remove_seats(), (0, []))

    def test_unreadable_shutdown_state_keeps_every_active_run(self) -> None:
        _ = self.run_dir("first", ["first1"], orchestrator=GONE_ORCHESTRATOR)
        _ = self.run_dir("second", ["second1"], orchestrator=LEGACY_DEAD)
        account = self.state_root / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{broken", encoding="utf-8")
        self.list_rows(
            [row("first1", "first-test"), row("second1", "second-test")]
        )

        result, removed = self.run_remove_seats()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(removed, [])
        self.assertIn("shutdown state could not be read", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
