"""Real verify.sh Mac arguments with disposable tools."""
from __future__ import annotations

import unittest
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from scripts.direct.verify_mac_offload_test_support import BusyClaimRecord, VerifyMacOffloadFixture, VERIFY as VERIFY

import sys

if TYPE_CHECKING:
    from ..lib import process_start
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start


@final
class VerifyMacArgumentsTests(VerifyMacOffloadFixture):
    def test_decline_runs_same_nextest_words_locally_after_runner(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--filter",
            "beta",
            "--features",
            "extra",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "busy",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": "runner output",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runner output", result.stdout)
        runners = self.runner_events()
        local = self.local_runs()
        self.assertEqual(len(runners), 1)
        self.assertEqual(len(local), 1)
        runner_arguments = runners[0]["args"]
        self.assertEqual(runner_arguments[0], "run")
        self.assertEqual(local[0]["args"][2], "--test-threads")
        self.assertGreater(int(local[0]["args"][3]), 0)
        self.assertEqual(
            runner_arguments[runner_arguments.index("--") + 1 :],
            local[0]["args"][4:],
        )
        self.assertIn("--filter-run", runner_arguments)
        self.assertNotIn("--no-cache", runner_arguments)
        self.assertFalse(runners[0]["token_held"])
        self.assertTrue(local[0]["token_held"])
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "busy")
        self.assertEqual(record["mac_holder"], "other tests")
        self.assertEqual(record["mac_s"], 3.25)
        self.assertNotIn("--no-cache", record["command"])

    def test_busy_claim_what_reaches_call_holder_through_the_real_offload_result(self) -> None:
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir()
        record: BusyClaimRecord = {"pid": os.getpid(), "proc_start": cast(process_start.ProcessStart, process_start.process_start(os.getpid())).text,
                  "what": "render checks", "worktree": str(self.root),
                  "since": datetime.now(timezone.utc).isoformat()}
        _ = (state / "run.json").write_text(json.dumps(record))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            "VERIFY_MAC_RUNNER": str(VERIFY.parent.parent / "mac_test" / "offload.py")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("busy: render checks since", result.stderr)
        self.assertEqual(self.last_call()["mac_holder"], "render checks")
        self.assertEqual(self.last_call()["mac_reason"], "busy")

    def test_no_test_match_runs_locally_and_reaches_call_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "no_tests",
                "TEST_RUNNER_SECONDS": "4.5",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": (
                    "mac_test: no test matched on the Mac; "
                    "running on natedev instead"
                ),
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead",
            result.stdout,
        )
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "no_tests")
        self.assertEqual(record["mac_s"], 4.5)


if __name__ == "__main__":
    _ = unittest.main()
