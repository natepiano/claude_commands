"""Tree cancellation pauses checkpoint validation without retrying or rolling back."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from typing import cast
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import merge_checkpoint
import test_merge_checkpoint as checkpoint_fixture
from production_units import Production


class CheckpointTreeCancelTests(unittest.TestCase):
    def assert_checkpoint_cancellation(self, statuses: tuple[int, ...]) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            production = Production(root / "production.md", "fixture", "merge", root,
                                    root / "production.log", ZoneInfo("UTC"))
            command = ["verify.sh", "test", "sample"]
            results = [subprocess.CompletedProcess(command, status,
                        "verify.sh: cancelled — the tree changed while test ran, so this result "
                        + "would be stale. Resubmit the test when your edits are done.\n"
                        if status == 77 else "a test failed\n", "") for status in statuses]
            output = StringIO()
            with (patch.object(merge_checkpoint, "test_steps", return_value=[("package sample", command)]),
                  patch.object(merge_checkpoint, "run_test_command", side_effect=results) as run,
                  patch.object(merge_checkpoint, "undo_unpushed_merge") as undo,
                  redirect_stdout(output)):
                with self.assertRaises(merge_checkpoint.Stop) as stopped:
                    _ = merge_checkpoint.checkpoint_tests(production, (), "abcdef0123", (),
                            frozenset(), root / "scratch", "merged-head", frozenset())
            self.assertEqual(run.call_count, len(statuses), "cancelled verification was retried")
            undo.assert_not_called()
            self.assertEqual(stopped.exception.state, "cancelled")
            message = stopped.exception.detail + " " + stopped.exception.message
            self.assertIn("resubmit", message.lower())
            self.assertNotIn("repair", stopped.exception.message.lower())
            self.assertNotIn("red: failed", output.getvalue())
            log = (root / "scratch/merge_abcdef0_test.log").read_text()
            self.assertIn("EXIT=77", log)
            self.assertNotIn("known flake", production.log.read_text() if production.log.exists() else "")

    def test_cancelled_checkpoint_test_is_not_retried_or_rolled_back(self) -> None:
        self.assert_checkpoint_cancellation((77,))

    def test_cancelled_failure_retry_pauses_checkpoint_without_rollback(self) -> None:
        self.assert_checkpoint_cancellation((1, 77))


class CancelledHandoffResumeTests(unittest.TestCase):
    def handoff_fixture(self, resume_status: int) -> checkpoint_fixture.MergeCheckpointTests:
        fixture = checkpoint_fixture.MergeCheckpointTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.promotions = str(fixture.promote)
        fixture.production_doc()
        _ = fixture.child_production()
        fixture.write(fixture.unit, "crates/widget/Cargo.toml", '[package]\nname="widget"\nversion="0.1.0"\n')
        _ = fixture.git("add", "crates/widget/Cargo.toml", cwd=fixture.unit)
        (fixture.state / "cancel-next-test").touch()
        fixture.executable(fixture.bin / "verify.sh", "\n".join([
            "#!/usr/bin/env python3", "import json, os, sys", "from pathlib import Path",
            'state = Path(os.environ["CHECKPOINT_TEST_STATE"])',
            'with (state / "events.jsonl").open("a") as output:',
            ' output.write(json.dumps({"command": "verify.sh", "args": sys.argv[1:]}) + "\\n")',
            'marker = state / "cancel-next-test"',
            "if marker.exists():", " marker.unlink()",
            ' print("verify.sh: cancelled — the tree changed while test ran, so this result would be stale. '
            + 'Resubmit the test when your edits are done.")',
            " raise SystemExit(77)", f"raise SystemExit({resume_status})", "",
        ]))
        fixture.executable(fixture.origin / "hooks/pre-receive", "\n".join([
            "#!/usr/bin/env python3", "import json, sys", "from pathlib import Path",
            'if "refs/heads/production" in sys.stdin.read():',
            f' with Path({str(fixture.state / "events.jsonl")!r}).open("a") as output:',
            '  output.write(json.dumps({"command": "push", "args": []}) + "\\n")',
            f' marker = Path({str(fixture.state / "reject-next-push")!r})',
            " if marker.exists():", "  marker.unlink()", "  raise SystemExit(1)", "",
        ]))
        return fixture

    def assert_cancelled_handoff_kept_locally(self, fixture: checkpoint_fixture.MergeCheckpointTests,
                                             checkpoint: str, before: str) -> str:
        cancelled = fixture.run_handoff(checkpoint)
        self.assertEqual(cancelled.returncode, 77, cancelled.stdout + cancelled.stderr)
        self.assertEqual(fixture.events("verify.sh"), [["test", "widget"]])
        self.assertEqual(fixture.events("push"), [])
        self.assertEqual(fixture.events("send.py"), [])
        self.assertEqual(fixture.git("rev-parse", "production", cwd=fixture.origin), before)
        merge = fixture.git("rev-parse", "HEAD")
        self.assertEqual(fixture.git("rev-parse", "HEAD^1"), before)
        self.assertEqual(fixture.git("rev-parse", "HEAD^2"), checkpoint)
        # Resubmit in a new process with a different scratch directory: the test obligation
        # belongs to the merge, not to the original session or its disposable test log.
        fixture.env["MERGE_CHECKPOINT_SCRATCH"] = str(fixture.root / "new-session-scratch")
        return merge

    def test_resubmitted_cancelled_handoff_tests_current_tree_before_its_only_push(self) -> None:
        fixture = self.handoff_fixture(0)
        before = fixture.git("rev-parse", "HEAD")
        checkpoint = fixture.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        merge = self.assert_cancelled_handoff_kept_locally(fixture, checkpoint, before)
        resumed = fixture.run_handoff(checkpoint)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(fixture.events("verify.sh"), [["test", "widget"], ["test", "widget"]])
        commands = [cast(dict[str, object], json.loads(line))["command"] for line in
                    (fixture.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual([name for name in commands if name in ("verify.sh", "push")],
                         ["verify.sh", "verify.sh", "push"])
        self.assertEqual(fixture.git("rev-parse", "HEAD"), merge)
        self.assertEqual(fixture.git("rev-parse", "production", cwd=fixture.origin), merge)
        self.assertEqual(fixture.git("rev-parse", "HEAD", cwd=fixture.promote), merge)
        self.assertEqual(len(fixture.events("send.py")), 1)

    def test_resubmitted_cancelled_handoff_with_failing_tests_never_pushes_or_promotes(self) -> None:
        fixture = self.handoff_fixture(1)
        before = fixture.git("rev-parse", "HEAD")
        promotion_before = fixture.git("rev-parse", "HEAD", cwd=fixture.promote)
        checkpoint = fixture.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        _ = self.assert_cancelled_handoff_kept_locally(fixture, checkpoint, before)
        resumed = fixture.run_handoff(checkpoint)
        self.assertNotEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(fixture.events("verify.sh"), [["test", "widget"]] * 3)
        self.assertEqual(fixture.events("push"), [])
        self.assertEqual(fixture.events("send.py"), [])
        self.assertEqual(fixture.events("style_sync.py"), [])
        self.assertEqual(fixture.git("rev-parse", "production", cwd=fixture.origin), before)
        self.assertEqual(fixture.git("rev-parse", "HEAD", cwd=fixture.promote), promotion_before)


    def test_passed_handoff_tests_are_not_repeated_when_resuming_a_rejected_push(self) -> None:
        fixture = self.handoff_fixture(0)
        before = fixture.git("rev-parse", "HEAD")
        checkpoint = fixture.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        merge = self.assert_cancelled_handoff_kept_locally(fixture, checkpoint, before)
        (fixture.state / "reject-next-push").touch()
        rejected = fixture.run_handoff(checkpoint)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.assertEqual(fixture.events("verify.sh"), [["test", "widget"]] * 2)
        self.assertEqual(fixture.git("rev-parse", "HEAD"), merge)
        accepted = fixture.run_handoff(checkpoint)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(fixture.events("verify.sh"), [["test", "widget"]] * 2)
        self.assertEqual(len(fixture.events("push")), 2)
        self.assertEqual(fixture.git("rev-parse", "production", cwd=fixture.origin), merge)


if __name__ == "__main__":
    _ = unittest.main()
