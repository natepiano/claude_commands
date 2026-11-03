#!/usr/bin/env python3
"""Tests for remove_workers.py against a stub `claude` binary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.direct import remove_workers, workers

SCRIPT = Path(__file__).parent / "remove_workers.py"

# `agents --json` prints the rows file; `rm <id>` records the id. A missing
# rows file makes the listing fail, the way an unreachable daemon would.
STUB_CLAUDE = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\t%s\\n' "$1" "${CLAUDE_CONFIG_DIR-<unset>}" >> "${STUB_ACCOUNTS}"
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


def row(worker_id: str, name: str, kind: str = "background") -> dict[str, str]:
    return {"id": worker_id, "sessionId": f"{worker_id}-full", "kind": kind, "name": name}


class RemoveWorkersTest(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    removed: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    rows: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    claude: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    state_root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    account_log: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "runs"
        (self.root / "active").mkdir(parents=True)
        self.removed = base / "removed"
        self.rows = base / "rows.json"
        self.claude = base / "claude"
        self.state_root = base / "shutdown"
        self.account_log = base / "accounts.jsonl"
        environment_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, {"HOME": str(base)})),
        )
        del environment_context
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        _ = self.claude.write_text(STUB_CLAUDE, encoding="utf-8")
        self.claude.chmod(0o755)

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_dir(self, name: str, workers: list[str], orchestrator: str | None = None) -> Path:
        run = self.root / name
        run.mkdir()
        _ = (run / "workers").write_text(
            "".join(f"{worker}\tproject-{worker}\n" for worker in workers), encoding="utf-8"
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

    def run_remove_workers(
        self, session_dir: Path | list[Path] | None = None, caller: str = ""
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        environment = os.environ.copy()
        environment.update(
            CLAUDE_BIN=str(self.claude),
            STUB_ROWS=str(self.rows),
            STUB_REMOVED=str(self.removed),
            STUB_ACCOUNTS=str(self.account_log),
            CLAUDE_CODE_SESSION_ID=caller,
            SHUTDOWN_STATE_DIR=str(self.state_root),
        )
        command = [sys.executable, str(SCRIPT), "--run-root", str(self.root)]
        for run in [session_dir] if isinstance(session_dir, Path) else session_dir or []:
            command += ["--session-dir", str(run)]
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, check=False
        )
        removed = (
            self.removed.read_text(encoding="utf-8").split()
            if self.removed.exists()
            else []
        )
        return result, sorted(removed)

    def remove_workers(self, session_dir: Path | list[Path] | None = None, caller: str = "") -> tuple[int, list[str]]:
        result, removed = self.run_remove_workers(session_dir, caller)
        return result.returncode, sorted(removed)

    def test_phase_cleanup_keeps_a_claude_session_owned_by_the_unit_ledger(self) -> None:
        own = self.run_dir("own", ["held", "ephemeral"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("held", "persistent writer"), row("ephemeral", "temporary writer")])
        with patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(self.root / "unit-workers")}):
            store = workers.UnitWorkerLedger(workers.ledger_path("production", "builders"))
            with store.locked() as ledger:
                ledger.workers["w7"] = workers.UnitWorker("w7", "impl", "claude", "opus", "high", {},
                    workers.ClaudeSession("held-full"), workers.Paused(), [])
            self.assertEqual(self.remove_workers(own), (0, ["ephemeral"]))
            self.assertTrue(store.path.exists())

    def test_named_owner_config_dir_is_inherited_by_listing_and_removal(self) -> None:
        """F006: both commands inherit the owning session's account directory."""
        own = self.run_dir("own", ["own1"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("own1", "p-impl")])
        config_dir = str(Path.home() / "account with 'quotes' and $()")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config_dir}):
            self.assertEqual(self.remove_workers(own), (0, ["own1"]))
        self.assertEqual(
            self.account_log.read_text(encoding="utf-8").splitlines(),
            [f"agents\t{config_dir}", f"rm\t{config_dir}"],
        )

    def test_default_owner_leaves_config_dir_unset_for_listing_and_removal(self) -> None:
        """F006: a default owner passes no account-directory assignment."""
        own = self.run_dir("own", ["own1"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("own1", "p-impl")])
        self.assertEqual(self.remove_workers(own), (0, ["own1"]))
        self.assertEqual(
            self.account_log.read_text(encoding="utf-8").splitlines(),
            ["agents\t<unset>", "rm\t<unset>"],
        )

    def test_cleanup_reads_an_old_run_ledger_and_keeps_its_shared_link(self) -> None:
        own = self.run_dir("old-run", ["old-worker"], orchestrator=ORCHESTRATOR)
        _ = (own / "workers").rename(own / "seats")
        self.list_rows([row("old-worker", "p-impl")])
        self.assertEqual(self.remove_workers(own), (0, ["old-worker"]))
        self.assertTrue((own / "seats").samefile(own / "workers"))
        self.assertEqual((own / "workers").read_text(), "old-worker\tproject-old-worker\n")

    def test_own_run_goes_even_while_live(self) -> None:
        own = self.run_dir("own", ["own1", "own2"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("own1", "p-impl"), row("own2", "p-test")])
        self.assertEqual(self.remove_workers(own), (0, ["own1", "own2"]))

    def test_every_given_run_goes_and_another_live_run_stays(self) -> None:
        """Run end passes each run its director holds; another director's live run is kept."""
        marked = self.run_dir("marked", ["mark1"], orchestrator=ORCHESTRATOR)
        side = self.run_dir("side", ["side1"])
        other = self.run_dir("other", ["other1"])
        for run in (side, other):
            _ = (run / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        self.list_rows([row("mark1", "a-impl"), row("side1", "b-impl"), row("other1", "c-impl")])
        self.assertEqual(self.remove_workers([marked, side]), (0, ["mark1", "side1"]))

    def test_live_run_is_kept_and_dead_runs_are_swept(self) -> None:
        _ = self.run_dir("live", ["live1"], orchestrator=ORCHESTRATOR)
        _ = self.run_dir("orphaned", ["orph1"], orchestrator=GONE_ORCHESTRATOR)
        _ = self.run_dir("ended", ["end1"])
        self.list_rows([row("live1", "a-impl"), row("orph1", "b-impl"), row("end1", "c-test")])
        self.assertEqual(self.remove_workers(), (0, ["end1", "orph1"]))

    def test_fresh_heartbeat_keeps_a_run_without_a_marker(self) -> None:
        fresh = self.run_dir("fresh", ["fresh1"])
        stale = self.run_dir("stale", ["stale1"])
        _ = (fresh / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        _ = (stale / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        hour_ago = time.time() - 3600
        os.utime(stale / "heartbeat.log", (hour_ago, hour_ago))
        self.list_rows([row("fresh1", "a-impl"), row("stale1", "b-impl")])
        self.assertEqual(self.remove_workers(), (0, ["stale1"]))

    def test_runs_under_either_folder_name_are_kept_live_or_swept(self) -> None:
        former = self.root.parent / "former"
        (former / "active").mkdir(parents=True)
        new_live = self.run_dir("new-live", ["new1"], orchestrator=ORCHESTRATOR)
        _ = self.run_dir("new-ended", ["new2"])
        old_live = former / "old-live"
        old_live.mkdir()
        _ = (old_live / "workers").write_text("old1\tproject-old1\n", encoding="utf-8")
        _ = (former / "active" / GONE_ORCHESTRATOR).write_text(f"{old_live}\n", encoding="utf-8")
        (former / "old-ended").mkdir()
        _ = (former / "old-ended" / "workers").write_text("old2\tproject-old2\n", encoding="utf-8")
        roots = (self.root, former)
        live = remove_workers.live_runs(roots, {ORCHESTRATOR, GONE_ORCHESTRATOR})
        self.assertEqual(live, {new_live.resolve(), old_live.resolve()})
        self.assertEqual(set(remove_workers.removal_targets(roots, set(), [], live)), {"new2", "old2"})

    def test_legacy_names_follow_their_run(self) -> None:
        _ = self.run_dir(LEGACY_LIVE, [], orchestrator=ORCHESTRATOR)
        self.list_rows([
            row("old1", f"{LEGACY_DEAD}-test"),
            row("old2", f"{LEGACY_DEAD}-impl"),
            row("kept", f"{LEGACY_LIVE}-impl"),
        ])
        self.assertEqual(self.remove_workers(), (0, ["old1", "old2"]))

    def test_only_listed_background_workers_go(self) -> None:
        _ = self.run_dir("ended", ["gone1", "inter1"])
        self.list_rows([row("inter1", "someone", kind="interactive"), row("friend", "a-friend")])
        self.assertEqual(self.remove_workers(), (0, []))

    def test_never_removes_the_calling_session(self) -> None:
        _ = self.run_dir("ended", ["self", "worker"])
        self.list_rows([row("self", "orchestrator"), row("worker", "p-impl")])
        self.assertEqual(self.remove_workers(caller="self-full"), (0, ["worker"]))

    def test_failed_listing_removes_nothing(self) -> None:
        _ = self.run_dir("ended", ["end1"])
        self.assertEqual(self.remove_workers(), (1, []))

    def test_failed_session_listing_never_runs_the_destructive_server_sweep(self) -> None:
        own = self.root / "own"
        arguments = [
            os.fspath(SCRIPT),
            "--session-dir",
            os.fspath(own),
            "--run-root",
            os.fspath(self.root),
        ]
        with patch.object(sys, "argv", arguments), \
                patch.object(remove_workers, "DEFAULT_ROOTS", (self.root,)), \
                patch.object(remove_workers, "claude_bin", return_value=os.fspath(self.claude)), \
                patch.object(remove_workers, "list_sessions", return_value=None), \
                patch.object(subprocess, "run") as commands:
            result = remove_workers.main()

        self.assertEqual(result, 1)
        commands.assert_not_called()

    def test_successful_run_cleanup_does_not_launch_an_implicit_server_sweep(self) -> None:
        own = self.run_dir("own", [], orchestrator=ORCHESTRATOR)
        arguments = [str(SCRIPT), "--session-dir", str(own)]
        with (patch.object(sys, "argv", arguments),
              patch.object(remove_workers, "DEFAULT_ROOTS", (self.root,)),
              patch.object(remove_workers, "list_sessions", return_value=[]),
              patch.object(subprocess, "run", side_effect=AssertionError("run cleanup launched a server sweep")) as commands):
            self.assertEqual(remove_workers.main(), 0)
        commands.assert_not_called()







    def test_stale_down_record_does_not_keep_a_dead_directors_run(self) -> None:
        _ = self.run_dir("held", ["held1"], orchestrator=GONE_ORCHESTRATOR)
        self.write_shutdown_record(GONE_ORCHESTRATOR, "down")
        self.list_rows([row("held1", "held-test")])

        self.assertEqual(self.remove_workers(), (0, ["held1"]))

    def test_stale_restarting_record_does_not_keep_a_dead_directors_run(self) -> None:
        _ = self.run_dir("held", ["held1"], orchestrator=GONE_ORCHESTRATOR)
        self.write_shutdown_record(GONE_ORCHESTRATOR, "restarting")
        self.list_rows([row("held1", "held-test")])

        self.assertEqual(self.remove_workers(), (0, ["held1"]))

    def test_unreadable_stale_shutdown_state_does_not_keep_dead_runs(self) -> None:
        _ = self.run_dir("first", ["first1"], orchestrator=GONE_ORCHESTRATOR)
        _ = self.run_dir("second", ["second1"], orchestrator=LEGACY_DEAD)
        account = self.state_root / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{broken", encoding="utf-8")
        self.list_rows(
            [row("first1", "first-test"), row("second1", "second-test")]
        )

        result, removed = self.run_remove_workers()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(removed), {"first1", "second1"})
        self.assertNotIn("shutdown state could not be read", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
