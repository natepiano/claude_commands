"""Test Claude pass records and repair outcomes."""

from __future__ import annotations

import unittest
from scripts.accounts import accounts
from scripts.production.cli_fixture import process_boundary, process_boundary_for_case
from scripts.direct import assign_claude_test_support as support
from scripts.direct.assign_claude_test_support import DIRECT_DIR as DIRECT_DIR
from scripts.lib.test_models_registry import claude_catalog_environment
from typing import cast, override
import json
import os
import subprocess
import sys
from unittest.mock import patch


class AssignClaudeLauncherWorkerTests(support.AssignClaudeLauncherFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"MODELS_CONFIG_TIER_ONLY": "1"})))
        # Keep the fixture entry unchanged for startup hooks and child observations.
        _ = self.enterContext(patch.object(self, "direct_cli_calls", not process_boundary_for_case(self)))

    def test_a_companion_fix_starts_after_the_resolver_lands_its_round(self) -> None:
        session = self.start_phase("landed-companion")
        self.open_repair_round(session)
        landed = subprocess.run(
            [sys.executable, str(DIRECT_DIR / "findings.py"), "landed",
             "--session-dir", str(session)],
            env=self.environment(), capture_output=True, text=True, check=False,
        )
        self.assertEqual(landed.returncode, 0, landed.stderr)
        result = self.checked_launch(session, "--phase", "3", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / "worker-started").exists())
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], {"round": 1, "covered": ["F001"]})

    def test_test_and_fix_workers_start_and_follow_through_assign(self) -> None:
        for kind in ("test", "fix"):
            with self.subTest(kind=kind):
                session = self.start_phase(f"{kind}-worker")
                cause = "repair the failed fixture gate" if kind == "fix" else ""
                first = self.worker_launch(session, kind=kind, slot=kind, fix_cause=cause)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                worker = self.recorded_worker(session, kind)
                self.assertEqual((session / f"impl_status_{kind}").read_text(), "implemented\n")
                self.assertIn("stub Claude log", (session / f"impl_summary_{kind}.txt").read_text())
                self.assertIn("stub Claude log", (session / f"impl_agent_{kind}.log").read_text())
                first_board = (session / "board.log").read_text().splitlines()
                self.assertEqual(sum("register:" in line for line in first_board), 1)
                self.assertEqual(sum("done:" in line for line in first_board), 1)
                self.assertTrue(any(f"worker={worker.name}" in line for line in first_board))
                started, = self.launch_records(session, "pass_started")
                finished, = self.launch_records(session, "pass_finished")
                self.assertEqual(started["pass_kind"], kind)
                self.assertEqual(started["worker_name"], worker.name)
                self.assertEqual(finished["status"], "completed")
                followed = self.worker_launch(session, to=worker.name, kind=kind, slot=kind, fix_cause=cause)
                self.assertEqual(followed.returncode, 0, followed.stdout + followed.stderr)
                self.assertEqual(self.recorded_worker(session, kind), worker)
                self.assertEqual((session / f"impl_status_{worker.name}").read_text(), "implemented\n")
                self.assertIn("Claude follow-up summary", (session / f"impl_summary_{worker.name}.txt").read_text())
                self.assertTrue((session / f"impl_agent_{worker.name}.log").read_text())
                passes = self.launch_records(session, "pass_started")
                self.assertEqual([record["pass_kind"] for record in passes], [kind, kind])
                commands = [cast(list[str], record["launcher_command"]) for record in passes]
                for command in commands:
                    self.assertEqual(command[:2], ["bash", str(self.assign_script)])
                    self.assertEqual(command[command.index("--role") + 1], kind)
                board = (session / "board.log").read_text()
                self.assertIn(f"follow-up to {worker.name}", board)
                self.assertEqual(sum("register:" in line for line in board.splitlines()), 2)
                self.assertEqual(sum("done:" in line for line in board.splitlines()), 2)

    def test_every_worker_records_its_own_pass_and_stamps_its_role(self) -> None:
        session_dir = self.start_phase("team")
        for kind, slot in (("impl", "impl"), ("impl", "test")):
            result = self.launch(session_dir, kind, slot)
            self.assertEqual(result.returncode, 0, result.stderr)

        # Two records, keyed by worker rather than by what each is doing: the
        # `test` worker opened as a second writer and is still slot `test`.
        passes = self.open_passes(session_dir)
        self.assertEqual(
            {slot: record["kind"] for slot, record in passes.items()},
            {"impl": "impl", "test": "impl"},
        )
        self.assertEqual(
            {record["status"] for record in passes.values()}, {"completed"}
        )

        # The register line carries the role, which is what fills the progress
        # table's columns before any agent has posted, and what made the
        # incident legible from the board alone once it had gone wrong.
        register = [
            line
            for line in (session_dir / "board.log").read_text(encoding="utf-8").splitlines()
            if "register:" in line
        ]
        self.assertEqual(len(register), 2, register)
        for line, expected in zip(register, ("role=impl", "role=impl")):
            self.assertIn(expected, line)

        with self.subTest(model="catalog target"):
            session = self.start_phase("catalog-records")
            environment = self.launch_environment(session)
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(accounts.setup("claude2"), [])
                _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
                    "oauthAccount": {"emailAddress": "worker@example.invalid"}}))
            expected_model = "claude-opus-assign-fixture"
            environment.update(claude_catalog_environment(self.root / ".claude2", {"opus": expected_model}))
            result = self.run_assignment(self.assign_command(session), environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            arguments = cast(list[str], json.loads((self.root / "launch_args.json").read_text()))
            self.assertEqual(arguments[arguments.index("--model") + 1], expected_model)
            self.assertIn("agent=" + expected_model, (session / "impl_agent_impl").read_text())
            started, = self.launch_records(session, "pass_started")
            self.assertEqual(cast(dict[str, object], started["called_agent"])["model"], expected_model)
            self.assertIn("claude/" + expected_model + ":high", (session / "board.log").read_text())
            self.assertIn("claude/" + expected_model + ":high", (session / "heartbeat.log").read_text())
            worker = cast(dict[str, object], json.loads((session / "impl_worker_impl.json").read_text()))
            self.assertEqual(worker["model"], expected_model)

    def test_failed_follow_up_abandons_round_and_posts_blocked(self) -> None:
        session_dir, first = self.first_worker_launch(repair=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.recorded_worker(session_dir).name

        result = self.worker_launch(
            session_dir, to=name, kind="fix", behavior="queued", resolves_round=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.repair_outcome(session_dir), "abandoned", result.stderr)
        state = cast("dict[str, object]", json.loads(
            (session_dir / "findings_state.json").read_text(encoding="utf-8")
        ))
        rounds = cast("list[dict[str, object]]", state["rounds"])
        self.assertIs(rounds[-1]["edits_landed"], True)
        self.assertEqual(self.open_passes(session_dir)[name]["status"], "error")
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("blocked:", board)

    @process_boundary
    def test_failed_landed_record_leaves_error_status(self) -> None:
        session_dir, first = self.first_worker_launch(repair=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.recorded_worker(session_dir).name
        self.observe_status_at_records(fail_landed=True, slot=name)

        result = self.worker_launch(
            session_dir, to=name, kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "landed"), "implementing\n")
        self.assertEqual(
            (session_dir / f"impl_status_{name}").read_text(encoding="utf-8"), "error\n"
        )
        self.assertIn("blocked:", (session_dir / "board.log").read_text(encoding="utf-8"))

    @process_boundary
    def test_failed_pass_write_posts_blocked_and_does_not_send(self) -> None:
        session_dir, first = self.first_worker_launch()
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.recorded_worker(session_dir).name
        recorder = self.root / "scripts/direct/progress_history.py"
        _ = recorder.write_text(recorder.read_text().replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n"
            + "import pathlib, sys\n"
            + "if sys.argv[1:2] == ['start-pass']:\n"
            + f"    pathlib.Path({str(session_dir / 'progress_history_state.json')!r}).write_text('{{}}\\n')\n",
            1,
        ))

        result = self.worker_launch(session_dir, to=name)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "delivered_message.txt").exists())
        self.assertEqual(
            (session_dir / f"impl_status_{name}").read_text(encoding="utf-8"),
            "error\n",
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("blocked:", board)

    def test_follow_up_records_pass_and_lands_its_repair_round(self) -> None:
        session_dir, first = self.first_worker_launch(repair=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.recorded_worker(session_dir).name

        result = self.worker_launch(
            session_dir, to=name, kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repair_outcome(session_dir), "landed")
        self.assertEqual(self.open_passes(session_dir)[name]["kind"], "fix")
        self.assertEqual(self.open_passes(session_dir)[name]["status"], "completed")
        self.assertEqual(self.open_passes(session_dir)[name]["worker_name"], name)
        # A renamed unit's seat works in the worktree the launcher names, not the one its thread began in.
        self.assertEqual((self.root / "follow_cwd.txt").read_text(encoding="utf-8"), str(self.working_dir))
        message = (self.root / "delivered_message.txt").read_text(encoding="utf-8")
        self.assertIn("Write the retry path.", message)
        self.assertIn(f"impl_summary_{name}.txt", message)
        self.assertIn("post", message)
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"follow-up to {name}", board)
        self.assertIn("launcher:", board)
        self.assertIn("done:", board)

    def test_launcher_passes_the_explicit_failed_gate_cause_for_a_fix(self) -> None:
        session = self.start_phase("checked-fix-cause")
        result = self.checked_launch(session, "--phase", "3", "--fix-cause",
                                     "retry tests fail after integration", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], "retry tests fail after integration")

    @process_boundary
    def test_success_status_follows_pass_and_landed_records(self) -> None:
        session_dir, first = self.first_worker_launch(repair=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.recorded_worker(session_dir).name
        self.observe_status_at_records(slot=name)

        result = self.worker_launch(
            session_dir, to=name, kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "landed"), "implementing\n")
        self.assertEqual(
            (session_dir / f"impl_status_{name}").read_text(encoding="utf-8"),
            "implemented\n",
        )

    @process_boundary
    def test_worker_error_status_follows_pass_and_abandon_records(self) -> None:
        session_dir, first = self.first_worker_launch(repair=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.recorded_worker(session_dir).name
        self.observe_status_at_records(slot=name)

        result = self.worker_launch(
            session_dir, to=name, kind="fix", behavior="queued",
            resolves_round=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "abandon"), "implementing\n")
        self.assertEqual(
            (session_dir / f"impl_status_{name}").read_text(encoding="utf-8"), "error\n"
        )


if __name__ == "__main__":
    _ = unittest.main()
