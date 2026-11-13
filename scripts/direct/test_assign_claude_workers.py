"""Claude repair worker identity integration coverage."""

from __future__ import annotations

import unittest
import json
import os
import re
from dataclasses import replace
from typing import cast
from unittest.mock import patch
from scripts.direct.assign_claude_native_test_support import ClaudeLauncherFixture
from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import CHECKOUT, fixture_worker, isolated_staff_file, transcript_fixture, stopped_worker_module


class AssignClaudeLauncherWorkerTests(ClaudeLauncherFixture):
    def test_codex_slot_uses_claude_start_and_receives_the_extract_after_publication(self) -> None:
        run = self.start_phase("codex-to-claude")
        with isolated_staff_file(self.root / "staff.json", {}):
            old = fixture_worker(self.working_dir.name + "-impl", staff.native_id(self.staff_parent),
                                 backend=staff.CodexBackend(self.root / ".codex"))
        source, evidence = transcript_fixture(old, staff.CodexBackend(self.root / ".codex"))
        reference = run / "impl_worker_impl.json"
        _ = reference.write_text(json.dumps({"staff_id": old.id, "slot": "impl", "model": "gpt-6"}))
        modules = stopped_worker_module(self.root / "switch-modules")
        environment = self.environment()
        environment["PYTHONPATH"] = os.pathsep.join((str(CHECKOUT / "scripts/lib/python_startup"), str(modules)))
        with patch.object(self, "environment", return_value=environment):
            result = self.claude_launch(run, "sonnet:low")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = self.recorded_worker(run)
        self.assertNotEqual(current.current_session, old.current_session)
        self.assertEqual(current, replace(old, current_session=current.current_session, role=staff.KnownWorkerRole("impl")))
        rows = cast(list[dict[str, object]], json.loads((self.root / "staff-at-work.json").read_text()))
        self.assertIn(current.to_json(), rows)
        prompt = (self.root / "opening-message.txt").read_text()
        extract = worker_admission.handoff_path(run, old)
        self.assertLess(prompt.index(str(extract)), prompt.index(str(source)))
        self.assertIn(evidence[0][0], extract.read_text())
        stored = cast(dict[str, object], json.loads(reference.read_text()))
        self.assertEqual((stored["staff_id"], stored["slot"]), (old.id, "impl"))

    def test_claude_repair_in_the_impl_slot_keeps_a_distinct_worker_identity(self) -> None:
        run = self.start_phase("claude-repair-identity")
        first = self.claude_launch(run, "sonnet:low")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = (run / "impl_worker_impl.json").read_bytes()
        self.open_repair_round(run)
        repair = self.claude_launch(run, "sonnet:low", kind="fix")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual((run / "impl_worker_impl.json").read_bytes(), original)
        identity = cast(dict[str, object], json.loads((run / "impl_worker_fix.json").read_text()))
        original_identity = cast(dict[str, object], json.loads(original))
        self.assertNotEqual(identity["staff_id"], original_identity["staff_id"])
        account = cast(dict[str, str], json.loads((self.root / "launch_environment.json").read_text()))
        with isolated_staff_file(self.root / "staff.json", {
            "CLAUDE_CONFIG_DIR": account["CLAUDE_CONFIG_DIR"] or str(self.root / ".claude"),
        }):
            self.assertIsInstance(staff.local_snapshot().backends[self.recorded_worker(run, "fix").id], staff.ClaudeBackend)

    def test_claude_fresh_refusal_preserves_artifacts_and_opens_no_pass_or_process(self) -> None:
        run = self.start_phase("claude-admission-refusal")
        summary = run / "impl_summary_impl.txt"
        _ = summary.write_text("previous summary\n")
        before = (run / "progress_history_state.json").read_bytes()
        _ = (self.root / "staff.json").write_text("{invalid staff")
        result = self.claude_launch(run, "sonnet:low")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertRegex(result.stderr, rf"^staff: {re.escape('impl')}: not opened: [^\n]+\n$")
        self.assertEqual(summary.read_text(), "previous summary\n")
        self.assertEqual((run / "progress_history_state.json").read_bytes(), before)
        self.assertFalse((run / "impl_claim_impl.json").exists())
        self.assertFalse((run / "impl_status_impl").exists())
        self.assertFalse((self.root / "launch_args.json").exists())
        self.assertNotIn("register:", (run / "board.log").read_text() if (run / "board.log").exists() else "")

    def test_claude_attach_refusal_leaves_the_existing_worker_and_pass_unchanged(self) -> None:
        run = self.start_phase("claude-attach-admission-refusal")
        transcript = self.claude_worker(run)
        before = (run / "progress_history_state.json").read_bytes(), transcript.read_bytes()
        _ = (self.root / "staff.json").write_text("{invalid staff")
        result = self.worker_launch(run, to="fixture-impl", behavior="queued")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertRegex(result.stderr, rf"^staff: {re.escape('impl')}: not opened: [^\n]+\n$")
        self.assertEqual(((run / "progress_history_state.json").read_bytes(), transcript.read_bytes()), before)
        self.assertFalse((run / "impl_claim_impl.json").exists())
        self.assertEqual((run / "impl_status_impl").read_text(), "implemented\n")
        self.assertFalse((self.root / "launch_args.json").exists())

    def test_claude_process_observes_its_durable_staff_parent(self) -> None:
        run = self.start_phase("claude-durable-admission")
        stub = self.root / "claude-original.py"
        _ = stub.write_text(stub.read_text().replace("if sys.argv[1] == '--bg':", """if sys.argv[1] == '--bg':
    rows = json.loads(pathlib.Path(os.environ['STAFF_FILE']).read_text())['rows']
    name = sys.argv[sys.argv.index('--name') + 1]
    (root / 'staff-at-process.json').write_text(json.dumps([row for row in rows if row['kind'] == 'worker' and row['name'] == name]))"""))
        result = self.claude_launch(run, "sonnet:low")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = cast(list[dict[str, object]], json.loads((self.root / "staff-at-process.json").read_text()))
        self.assertEqual(rows, [])
        at_work = cast(list[dict[str, object]], json.loads((self.root / "staff-at-work.json").read_text()))
        workers = [row for row in at_work if row["kind"] == "worker"]
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["parent"], self.environment()["CODEX_THREAD_ID"])
        identity = cast(dict[str, object], json.loads((run / "impl_worker_impl.json").read_text()))
        self.assertEqual(workers[0]["id"], identity["staff_id"])
        self.assertEqual((self.root / "opening-message.txt").read_bytes(), self.prompt_file.read_bytes())


if __name__ == "__main__":
    _ = unittest.main()
