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
from typing import override
from unittest.mock import patch

from scripts.direct import remove_workers, workers
from scripts.message import process_table
from scripts.production import staff
from scripts.production.staff_test_support import OWNER_CLI, StaffTestCase, fixture_worker, fixture_runtime_id, unit_membership
from scripts.agents import worker_lifecycle
from scripts.lib.atomic_file import write_text_atomic

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


def row(worker_id: str, name: str, kind: str = "background") -> remove_workers.AgentRow:
    return {"id": worker_id, "sessionId": fixture_runtime_id(f"{worker_id}-full"), "kind": kind, "name": name}


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
        fixture = StaffTestCase()
        fixture.setUp()
        fixture.write_json(fixture.staff_file, {"schema": 2, "rows": []})
        self.addCleanup(fixture.doCleanups)
        _ = self.enterContext(patch.object(process_table, "snapshot", return_value={}))
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "runs"
        (self.root / "active").mkdir(parents=True)
        self.removed = base / "removed"
        self.rows = base / "rows.json"
        self.claude = base / "claude"
        self.state_root = base / "shutdown"
        self.account_log = base / "accounts.jsonl"
        self.enterContext(patch.dict(os.environ, {"HOME": str(base)}))
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        _ = self.claude.write_text(STUB_CLAUDE, encoding="utf-8")
        self.claude.chmod(0o755)

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_dir(self, name: str, workers: list[str], orchestrator: str | None = None, *, root: Path | None = None) -> Path:
        selected = root or self.root
        run = selected / name
        run.mkdir()
        write_text_atomic(run / "worker_transport", "mesh\n")
        parent = unit_membership("cleanup", name)
        if orchestrator is not None:
            parent = staff.update_current_session(parent.current_session, staff.native_id(orchestrator))
            _ = (selected / "active" / orchestrator).write_text(f"{run}\n", encoding="utf-8")
        _ = (run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": parent.id}))
        roster: dict[str, object] = {}
        for worker in workers:
            member = fixture_worker("worker-" + worker, parent.id, runtime_id=fixture_runtime_id(worker + "-full"))
            roster[member.name] = {"staff_id": member.id}
        _ = (run / "mesh_roster.json").write_text(json.dumps(roster))
        return run

    def list_rows(self, rows: list[remove_workers.AgentRow]) -> None:
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
            CLAUDE_CODE_SESSION_ID=fixture_runtime_id(caller) if caller else "",
            SHUTDOWN_STATE_DIR=str(self.state_root),
            PROCESS_TABLE_PS="true",
        )
        command = [str(SCRIPT.parent.parent / "lib/py"), "-c", OWNER_CLI, str(SCRIPT), "--run-root", str(self.root)]
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

    def test_herdr_removal_retains_a_worker_without_proved_pane_evidence(self) -> None:
        run = self.run_dir("pane-unknown", ["first"], orchestrator=ORCHESTRATOR)
        _ = (run / "worker_transport").write_text("herdr\n")
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": ORCHESTRATOR, "HERDR_BIN": "/nonexistent/herdr"}):
            result, removed = self.run_remove_workers(run)
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual(removed, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertIn("/nonexistent/herdr", result.stderr)

    def test_sweep_keeps_pane_workers_out_of_claude_removal(self) -> None:
        run = self.run_dir("pane-ended", ["first"])
        _ = (run / "worker_transport").write_text("herdr\n")
        self.list_rows([row("first", "worker-first")])
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        result, removed = self.run_remove_workers()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(removed, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertIn("pane worker retained", result.stderr)

    def test_workers_of_one_parent_use_their_own_runs_removal_transport(self) -> None:
        pane_run = self.run_dir("pane-run", ["pane"], orchestrator=ORCHESTRATOR)
        mesh_run = self.run_dir("mesh-run", ["mesh"])
        parent = unit_membership("cleanup", "pane-run")
        pane_worker = next(member for member in staff.local_snapshot().find(kind="worker") if member.name == "worker-pane")
        mesh_worker = next(member for member in staff.local_snapshot().find(kind="worker") if member.name == "worker-mesh")
        mesh_worker = staff.move(mesh_worker.id, staff.ReportsTo(parent.id))
        _ = (mesh_run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": parent.id}))
        _ = (pane_run / "worker_transport").write_text("herdr\n")
        _ = (mesh_run / "worker_transport").write_text("mesh\n")
        _ = (pane_run / "progress_history_state.json").write_text(json.dumps({
            "owner_staff_id": parent.id, "pass": {"impl": {
                "instance_id": "pane-dispatch", "worker_name": pane_worker.name,
            }},
        }))
        (pane_run / "mesh_roster.json").unlink()
        self.list_rows([row("pane", pane_worker.name), row("mesh", mesh_worker.name)])
        with patch.dict(os.environ, {"CODEX_THREAD_ID": ORCHESTRATOR, "HERDR_BIN": "/nonexistent/herdr"}):
            result, removed = self.run_remove_workers([pane_run, mesh_run])
        self.assertEqual((result.returncode, removed), (8, ["mesh"]), result.stdout + result.stderr)
        self.assertIn("/nonexistent/herdr", result.stderr)
        self.assertNotIn("pane worker retained", result.stderr)
        self.assertEqual(staff.by_id(pane_worker.id), pane_worker)
        self.assertIsNone(staff.by_id(mesh_worker.id))

    def test_pane_removal_cannot_select_another_native_callers_workers(self) -> None:
        run = self.run_dir("pane-other", ["first"])
        _ = (run / "worker_transport").write_text("herdr\n")
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        result, removed = self.run_remove_workers(run)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(removed, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertIn("does not belong to the native caller", result.stderr)

    def test_retirement_retries_after_the_staff_publication_is_interrupted(self) -> None:
        fixture = StaffTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, parent, _ = fixture.tree()
        worker = fixture_worker("removed", parent.id, runtime_id=fixture_runtime_id("removed-Claude"))
        run = self.run_dir("removed-run", [worker.current_session])
        retire = staff.retire

        def publish_then_interrupt(identity: staff.StaffId) -> staff.StaffRow | None:
            _ = retire(identity)
            raise InterruptedError("lost staff acknowledgement")

        with worker_lifecycle.membership_operation(run):
            with patch.object(staff, "retire", side_effect=publish_then_interrupt):
                with self.assertRaisesRegex(InterruptedError, "lost staff acknowledgement"):
                    remove_workers.retire_removed_worker(worker.current_session)
            self.assertIsNone(staff.by_id(worker.id))
            before = fixture.staff_file.read_bytes(), fixture.staff_file.stat().st_mtime_ns
            remove_workers.retire_removed_worker(worker.current_session)
            self.assertEqual((fixture.staff_file.read_bytes(), fixture.staff_file.stat().st_mtime_ns), before)

    def test_phase_cleanup_keeps_a_claude_session_owned_by_the_unit_ledger(self) -> None:
        own = self.run_dir("own", ["held", "ephemeral"], orchestrator=ORCHESTRATOR)
        runner = staff.ensure("showrunner", name="cleanup", parent=staff.StaffRoot(),
                              runtime_id=fixture_runtime_id("cleanup-owner"))
        worker = staff.by_current_session(fixture_runtime_id("held-full"))
        assert worker is not None
        worker = staff.move(worker.id, staff.ReportsTo(runner.id))
        self.list_rows([{**row("held", "persistent writer"), "sessionId": worker.current_session},
                        row("ephemeral", "temporary writer")])
        with patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(self.root / "unit-workers")}):
            store = workers.UnitWorkerLedger(workers.ledger_path("cleanup", "own"))
            with store.locked() as ledger:
                ledger.workers[worker.name] = workers.UnitWorker(worker.name, "impl", "claude", "opus", "high", {},
                    workers.ClaudeSession(worker.current_session), workers.Paused(), [], staff_id=worker.id)
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


    def test_own_run_goes_even_while_live(self) -> None:
        own = self.run_dir("own", ["own1", "own2"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("own1", "p-impl"), row("own2", "p-test")])
        self.assertEqual(self.remove_workers(own), (0, ["own1", "own2"]))

    def test_own_run_removal_reports_phase_completion_with_a_live_director(self) -> None:
        own = self.run_dir("own", ["own1"], orchestrator=ORCHESTRATOR)
        self.list_rows([row("own1", "p-impl")])
        result, removed = self.run_remove_workers(own)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(removed, ["own1"])
        self.assertEqual(result.stdout, "removed worker p-impl (own1): its run finished this phase\n")

    def test_sweep_removal_reports_a_dead_director(self) -> None:
        _ = self.run_dir("orphaned", ["orph1"], orchestrator=GONE_ORCHESTRATOR)
        self.list_rows([row("orph1", "p-impl")])
        result, removed = self.run_remove_workers()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(removed, ["orph1"])
        self.assertEqual(result.stdout, "removed worker p-impl (orph1): unit director is not live\n")

    def test_unreadable_run_owner_does_not_stop_other_worker_removal(self) -> None:
        for transport in ("mesh", "herdr"):
            with self.subTest(transport=transport):
                bad = self.run_dir(f"bad-{transport}", [f"bad-{transport}"])
                good = self.run_dir(f"good-{transport}", [f"good-{transport}"])
                missing_owner = fixture_runtime_id(f"missing-owner-{transport}")
                _ = (bad / "progress_history_state.json").write_text(json.dumps({
                    "owner_staff_id": missing_owner,
                    "pass": {"impl": {"instance_id": "dispatch", "worker_name": f"worker-bad-{transport}"}},
                }))
                _ = (bad / "worker_transport").write_text(transport + "\n")
                _ = (bad / "heartbeat.log").write_text("beat\n")
                self.list_rows([row(f"bad-{transport}", "bad"), row(f"good-{transport}", "good")])
                selected = [good, bad] if transport == "mesh" else [good]
                result, removed = self.run_remove_workers(selected)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(removed, [f"good-{transport}"])
                self.assertIn(
                    f"anomaly: {bad}: run owner {missing_owner}: unit staff row is unavailable\n", result.stderr,
                )
                self.removed.unlink()
                (bad / "worker_transport").unlink()
                (bad / "heartbeat.log").unlink()

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
        _ = self.run_dir("new-live", ["new1"], orchestrator=ORCHESTRATOR)
        _ = self.run_dir("new-ended", ["new2"])
        _ = self.run_dir("old-live", ["old1"], orchestrator=GONE_ORCHESTRATOR, root=former)
        _ = self.run_dir("old-ended", ["old2"], root=former)
        listed = [row(name, name) for name in ("new1", "new2", "old1", "old2")]
        listed.extend([{"sessionId": ORCHESTRATOR}, {"sessionId": GONE_ORCHESTRATOR}])
        self.assertEqual(set(remove_workers.removal_targets(set(), listed, roots=(self.root, former))), {"new2", "old2"})


    def test_cleanup_uses_staff_membership_instead_of_handles_or_display_names(self) -> None:
        _ = self.run_dir("ended", ["member"])
        run = self.root / "unowned"
        run.mkdir()
        _ = (run / "workers").write_text("stranger\tproject-stranger\n")
        self.list_rows([row("member", "unrelated display"), row("stranger", f"project-{LEGACY_DEAD}-test")])
        self.assertEqual(self.remove_workers(), (0, ["member"]))

    def test_claude_cleanup_preserves_a_paused_codex_worker(self) -> None:
        run = self.run_dir("ended", ["member"])
        parent = unit_membership("cleanup", "ended")
        codex = fixture_worker("paused-codex", parent.id,
                               backend=staff.CodexBackend(self.root.parent / "codex-home"))
        _ = (run / "mesh_roster.json").write_text(json.dumps({
            codex.name: {"staff_id": codex.id, "status": "paused"},
        }))
        listed = [row("member", "Claude worker")]
        self.assertEqual(set(remove_workers.removal_targets(set(), listed, roots=(self.root,))), {"member"})
        self.list_rows(listed)
        self.assertEqual(self.remove_workers(), (0, ["member"]))
        self.assertEqual(staff.by_id(codex.id), codex)
        self.assertEqual(worker_lifecycle.read_json_object(run / "mesh_roster.json"), {
            codex.name: {"staff_id": codex.id, "status": "paused"},
        })

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
        own = self.run_dir("own", [])
        arguments = [
            os.fspath(SCRIPT),
            "--session-dir",
            os.fspath(own),
        ]
        with patch.object(sys, "argv", arguments), \
                patch.object(remove_workers, "claude_bin", return_value=os.fspath(self.claude)), \
                patch.object(remove_workers, "list_sessions", return_value=None), \
                patch.object(subprocess, "run") as commands:
            result = remove_workers.main()

        self.assertEqual(result, 1)
        commands.assert_not_called()

    def test_successful_run_cleanup_does_not_launch_an_implicit_server_sweep(self) -> None:
        own = self.run_dir("own", [], orchestrator=ORCHESTRATOR)
        arguments = [str(SCRIPT), "--run-root", str(self.root), "--session-dir", str(own)]
        with (patch.object(sys, "argv", arguments),
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

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(removed), {"first1", "second1"})
        self.assertNotIn("shutdown state could not be read", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
