"""Claude launcher follow integration coverage."""

from __future__ import annotations

import unittest
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import patch
from scripts.usage import agents_log
from scripts.direct.assign_claude_native_test_support import ClaudeLauncherFixture
from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import CHECKOUT, fixture_runtime_id, fixture_session, isolated_staff_file, stopped_worker_module, transcript_fixture


class AssignClaudeLauncherWorkerTests(ClaudeLauncherFixture):
    def test_failed_switch_delivery_retries_the_saved_handoff_before_the_assignment(self) -> None:
        session_dir = self.start_phase("claude-switch-retry")
        transcript = self.claude_worker(session_dir)
        row = self.recorded_worker(session_dir)
        source = self.root / "codex-source.jsonl"
        extract = worker_admission.handoff_path(session_dir, row)
        _ = extract.write_text(f"{worker_admission.HANDOFF_SOURCE_PREFIX}{source}\nKeep the source work.\n")
        with isolated_staff_file(self.root / "staff.json", {}):
            allowed = worker_admission.admit(row.name, session_dir, reference=row.id)
            request = worker_admission.assignment_id(self.prompt_file)
            worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT)

        failed = self.worker_launch(session_dir, to="fixture-impl", behavior="queued")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name),
                         worker_admission.DispatchRecord(request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT))

        retried = self.worker_launch(session_dir, to="fixture-impl", behavior="complete")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual((self.root / "delivered_to.txt").read_text(), f"session:{row.current_session}")
        opening = (self.root / "delivered_message.txt").read_text()
        assignment = self.prompt_file.read_text()
        self.assertLess(opening.index(str(extract)), opening.index(assignment))
        self.assertEqual(self.recorded_worker(session_dir), replace(row, role=staff.KnownWorkerRole("impl")))
        accepted = worker_admission.dispatch_record(allowed.run, allowed.name)
        self.assertEqual(accepted, worker_admission.DispatchRecord(request, worker_admission.SwitchState.ASSIGNMENT_ACCEPTED))

        followed = self.worker_launch(session_dir, to="fixture-impl", behavior="complete")
        self.assertEqual(followed.returncode, 0, followed.stdout + followed.stderr)
        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name), accepted)
        self.assertNotIn(str(extract), (self.root / "delivered_message.txt").read_text())

    def test_unreadable_dispatch_reports_one_anomaly_and_keeps_the_live_worker_followable(self) -> None:
        run = self.start_phase("unreadable-dispatch")
        transcript = self.claude_worker(run)
        row = self.recorded_worker(run)
        record_path = worker_admission.switch_record_path(run, row.name)
        modules = self.root / "read-failure"
        modules.mkdir()
        _ = (modules / "sitecustomize.py").write_text("\n".join((
            "import inspect, sys",
            "from pathlib import Path",
            "read_text = Path.read_text",
            "def read(path, *args, **kwargs):",
            f"    if path == Path({str(record_path)!r}) and any(frame.function == 'dispatch_record' for frame in inspect.stack()) and any(frame.function == '_read_validated_json' for frame in inspect.stack()):",
            "        Path.read_text = read_text",
            "        raise PermissionError('fixture dispatch read failed')",
            "    return read_text(path, *args, **kwargs)",
            "Path.read_text = read",
        )))
        environment = self.environment()
        environment["PYTHONPATH"] = os.pathsep.join((str(modules), str(CHECKOUT)))
        with patch.object(self, "environment", return_value=environment):
            result = self.worker_launch(run, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(),
                         [f"anomaly: {row.name}: {record_path}: worker state unreadable: fixture dispatch read failed"])
        self.assertEqual(len(transcript.read_text().splitlines()), 2)
        self.assertEqual(self.recorded_worker(run), replace(row, role=staff.KnownWorkerRole("impl")))

    def test_family_switch_continues_after_an_unknown_dispatch_state(self) -> None:
        run = self.start_phase("switch-unknown-record")
        _ = self.claude_worker(run)
        old = self.recorded_worker(run)
        with isolated_staff_file(self.root / "staff.json", {}):
            identity = staff.native_id(fixture_runtime_id("Codex source with an unknown dispatch state"))
            fixture_session(identity, staff.CodexBackend(self.root / ".codex"))
            row = staff.update_current_session(old.current_session, identity)
        source, evidence = transcript_fixture(row, staff.CodexBackend(self.root / ".codex"))
        _ = (run / "impl_worker_impl.json").write_text(json.dumps({"staff_id": row.id, "slot": "impl", "model": "gpt-6"}))
        record_path = worker_admission.switch_record_path(run, row.name)
        _ = record_path.write_text(json.dumps({"dispatch_state": "invalid"}))
        modules = stopped_worker_module(self.root / "switch-modules")
        environment = self.environment()
        environment["PYTHONPATH"] = os.pathsep.join((str(modules), str(CHECKOUT)))
        with patch.object(self, "environment", return_value=environment):
            result = self.worker_launch(run, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([line for line in result.stderr.splitlines() if line.startswith("anomaly:")],
                         [f"anomaly: {row.name}: unknown dispatch_state 'invalid'; using NO_RECORD"])
        current = self.recorded_worker(run)
        self.assertEqual(current.id, row.id)
        self.assertNotEqual(current.current_session, row.current_session)
        opening = (self.root / "delivered_message.txt").read_text()
        extract = worker_admission.handoff_path(run, row)
        self.assertLess(opening.index(str(extract)), opening.index(self.prompt_file.read_text()))
        self.assertIn(evidence[0][0], extract.read_text())
        self.assertIn(str(source), opening)

    def test_claude_follow_records_the_dispatch_role_before_delivery(self) -> None:
        session_dir = self.start_phase("claude-role-change")
        _ = self.claude_worker(session_dir)
        sender = self.root / "send"
        original = sender.read_text()
        probe = "\n".join((
            "import json, os, sys",
            f"with open({str(Path(__file__).resolve().parents[2] / 'scripts/lib/package_root.py')!r}) as package_root: exec(package_root.read(), {{'__file__': package_root.name}})",
            "from pathlib import Path",
            "from scripts.herdr import worker_panes",

            "from scripts.production import staff",
            "identity = 'abc12345-2222-4333-8444-555555555555'",
            "member = staff.by_current_session(identity)",
            "assert member is not None",
            "role = worker_panes.dispatch_role(member)",
            f"Path({str(self.root / 'delivered-role.json')!r}).write_text(json.dumps(role))",
        ))
        _ = sender.write_text(original.replace("import sys", probe + "\nimport sys", 1))
        with patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.root / "usage")}):
            agents_log.append("abc12345-2222-4333-8444-555555555555", "fix", None, datetime.now(timezone.utc))
            result = self.worker_launch(session_dir, to="fixture-impl", behavior="complete")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.root / "delivered-role.json").read_text()), "impl")
        self.assertEqual(self.open_passes(session_dir)["fixture-impl"]["status"], "completed")

    def test_claude_follow_counts_turn_completed_before_first_poll(self) -> None:
        session_dir = self.start_phase("claude-short-turn")
        transcript = self.claude_worker(session_dir)

        result = self.worker_launch(session_dir, to="fixture-impl", behavior="complete")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.open_passes(session_dir)["fixture-impl"]["status"], "completed")
        self.assertIn("impl_summary_fixture-impl.txt", (
            self.root / "delivered_message.txt"
        ).read_text(encoding="utf-8"))

    def test_claude_follow_retries_unreadable_agents_listing(self) -> None:
        session_dir = self.start_phase("claude-unreadable")
        _ = self.claude_worker(session_dir)

        result = self.worker_launch(session_dir, to="fixture-impl", behavior="unreadable_once")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.open_passes(session_dir)["fixture-impl"]["status"], "completed")

    def test_claude_queued_send_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-queued")
        transcript = self.claude_worker(session_dir)

        result = self.worker_launch(session_dir, to="fixture-impl", behavior="queued")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.open_passes(session_dir)["fixture-impl"]["status"], "error")
        self.assertIn("blocked:", (
            session_dir / "board.log"
        ).read_text(encoding="utf-8"))

    def test_claude_worker_gone_after_delivery_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-gone")
        _ = self.claude_worker(session_dir)

        result = self.worker_launch(session_dir, to="fixture-impl", behavior="gone")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.open_passes(session_dir)["fixture-impl"]["status"], "error")


if __name__ == "__main__":
    _ = unittest.main()
