"""Claude repair worker identity integration coverage."""

from __future__ import annotations

import unittest
import json
from typing import cast
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
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
        self.assertNotEqual(identity["name"], original_identity["name"])
        self.assertEqual(identity["family"], "claude")
        self.assertFalse((run / "mesh_roster.json").exists())

    def test_claude_fresh_refusal_preserves_artifacts_and_opens_no_pass_or_process(self) -> None:
        run = self.start_phase("claude-admission-refusal")
        summary = run / "impl_summary_impl.txt"
        _ = summary.write_text("previous summary\n")
        before = (run / "progress_history_state.json").read_bytes()
        _ = (self.root / "staff.json").write_text("{invalid staff")
        result = self.claude_launch(run, "sonnet:low")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, r"^staff: [^\n]+: not opened: [^\n]+\n$")
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
        result = self.claude_follow(run, transcript, "queued")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, r"^staff: fixture-impl: not opened: [^\n]+\n$")
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
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parent"], self.environment()["STAFF_ID"])


if __name__ == "__main__":
    _ = unittest.main()
