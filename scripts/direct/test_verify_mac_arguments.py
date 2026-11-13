"""Test Mac arguments directly and keep complete routing cases."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import final

from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import ASSIGNED_RUNNER_SETUP, BusyClaimRecord, VerifyMacOffloadFixture, VERIFY as VERIFY
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacArgumentsTests(VerifyMacOffloadFixture):
    def test_decline_runs_same_nextest_words_locally_after_runner(self) -> None:
        cases = (
            ("sample", "--filter", "alpha", "--filter", "beta", "--features", "extra"),
            ("sample", "--filter", "alpha", "--features", "extra", "--run-ignored", "only"),
            ("--run-ignored", "only", "--filter", "alpha", "--features", "extra"),
            ("sample", "--run-ignored", "only"),
            ("--run-ignored", "only"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(
                    "test", *arguments, "--no-cache",
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
                if "--run-ignored" in arguments:
                    nextest_arguments = local[0]["args"][4:]
                    ignored = nextest_arguments.index("--run-ignored")
                    self.assertEqual(nextest_arguments[ignored:ignored + 2], ["--run-ignored", "only"])
                    self.assertNotIn("--no-tests=warn", nextest_arguments)
                    self.assertIn("--workspace", nextest_arguments)
                    if "sample" not in arguments:
                        self.assertNotIn("package(sample)", " ".join(nextest_arguments))
                self.assertNotIn("--no-cache", runner_arguments)
                self.assertFalse(runners[0]["token_held"])
                self.assertTrue(local[0]["token_held"], json.dumps(local[0]) + result.stdout + result.stderr)
                record = self.last_call()
                self.assertEqual(record["mac"], "declined")
                self.assertEqual(record["mac_reason"], "busy")
                self.assertEqual(record["mac_holder"], "other tests")
                self.assertEqual(record["mac_s"], 3.25)
                self.assertNotIn("--no-cache", record["command"])

    def test_busy_claim_what_reaches_call_holder_through_the_real_offload_result(self) -> None:
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir()
        record: BusyClaimRecord = {"claim_id": "busy-claim", "episode": "busy-episode", "attempt": "busy-attempt",
                  "submitting_session": "", "pid": os.getpid(), "proc_start": build_hold.process_start_time(os.getpid()),
                  "what": "render checks", "worktree": str(self.root),
                  "since": datetime.now(timezone.utc).isoformat()}
        _ = (state / "run.json").write_text(json.dumps(record))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            "VERIFY_MAC_RUNNER": str(VERIFY.parent.parent / "mac_test" / "offload.py")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("busy: render checks since", result.stderr)
        self.assertEqual(self.last_call()["mac_holder"], "render checks")
        self.assertEqual(self.last_call()["mac_reason"], "busy")

    def test_no_test_match_result_reaches_call_record(self) -> None:
        runner = self.root / "no-test-result.py"
        self.write_executable(runner, '''
import json
import os
import sys
import time
from pathlib import Path

from scripts.mac_test import offload

arguments = sys.argv[1:]
root = Path(os.environ["TEST_RUNNER_STATE"])
result = Path(arguments[arguments.index("--result") + 1])
''' + ASSIGNED_RUNNER_SETUP + '''
if "--assignment-file" in arguments:
    fixture_outcome = placement.AttemptDeclined("no_tests")
    fixture_evidence = placement.ExecutionEvidenceUnavailable("the private command has ended", time.time())
    fixture_cleanup_confirmed = True
with (Path(os.environ["TEST_RUNNER_STATE"]) / "runner-events.jsonl").open("a") as output:
    output.write(json.dumps({"args": sys.argv[1:], "token_held": False, "at": time.time()}) + "\\n")
result = offload.NoTestMatchedOnMac(4.5)
destination = offload.ResultFile(Path(sys.argv[sys.argv.index("--result") + 1]))
raise SystemExit(offload.write_result(result, destination))
''')
        result = self.verify("test", "sample", "--no-cache", extra={
            "VERIFY_MAC_RUNNER": str(runner), "PYTHONPATH": str(VERIFY.parents[2]),
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead",
            result.stdout,
        )
        runners = self.runner_events()
        local = self.local_runs()
        self.assertEqual(len(runners), 1)
        self.assertEqual(len(local), 1)
        runner_arguments = runners[0]["args"]
        nextest_arguments = runner_arguments[runner_arguments.index("--") + 1:]
        local_arguments = local[0]["args"][4:]
        self.assertEqual(nextest_arguments, local_arguments)
        self.assertIn("--no-tests=warn", nextest_arguments)
        self.assertIn("--no-tests=warn", local_arguments)
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "no_tests")
        self.assertEqual(record["mac_s"], 4.5)


@final
class NextestArgumentTests(unittest.TestCase):
    def test_only_plain_package_runs_allow_no_tests_on_both_machines(self) -> None:
        functions = shell_functions(VERIFY, "verify_helper", "read_metadata", "set_gate_metadata", "load_metadata",
                                    "require_member", "qualify_features", "merge_dev_features",
                                    "take_features", "take_test_targets", "compose_nextest_args")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata: dict[str, object] = {
                "workspace_root": str(root), "workspace_members": ["sample-id"],
                "packages": [{"name": "sample", "id": "sample-id", "features": {},
                              "manifest_path": str(root / "Cargo.toml"), "dependencies": [],
                              "targets": [{"name": "sample", "kind": ["lib"], "test": True,
                                           "src_path": str(root / "src/lib.rs")},
                                          {"name": "integration_suite", "kind": ["test"], "test": True,
                                           "src_path": str(root / "tests/integration_suite.rs")}]}],
            }
            script = functions + "\n" + "\n".join((
                "PY=" + shlex.quote(sys.executable),
                "VERIFY_METADATA_SCRIPT=" + shlex.quote(str(VERIFY.with_name("verify_metadata.py"))),
                "GATE_METADATA=" + shlex.quote(json.dumps(metadata)),
                "CMD=test", "DEV_FEATURES=", "METADATA_READY=0",
            )) + r'''
for selection in plain filter target; do
    FEATURE_FLAGS=() TEST_SELECTION=() NEXTEST_ARGS=()
    case "$selection" in
        plain) ARGS=(sample) ;;
        filter) ARGS=(sample --filter alpha) ;;
        target) ARGS=(sample --test integration_suite) ;;
    esac
    compose_nextest_args
    printf '%s\0' "${NEXTEST_ARGS[@]}"
    printf '\0'
done
'''
            result = subprocess.run(["bash", "-e", "-c", script], cwd=root,
                                    text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cases = (((), True), (("--filter", "alpha"), False), (("--test", "integration_suite"), False))
        answers = result.stdout.split("\0\0")[:-1]
        self.assertEqual(len(answers), len(cases))
        for (arguments, allow_empty), answer in zip(cases, answers, strict=True):
            with self.subTest(arguments=arguments):
                nextest_arguments = answer.split("\0")
                self.assertEqual("--no-tests=warn" in nextest_arguments, allow_empty)

        parser = shell_functions(VERIFY, "parse_command_arguments") + '\nCMD=$1; shift; parse_command_arguments "$@"\n'
        refusals = (
            ("test", "sample", "--run-ignored", "all"),
            ("test", "sample", "--run-ignored"),
            ("test", "sample", "--run-ignored", "only", "--test", "integration_suite"),
            ("test", "sample", "--test", "integration_suite", "--run-ignored", "only"),
            ("check", "sample", "--run-ignored", "only"),
        )
        for arguments in refusals:
            with self.subTest(refused=arguments):
                result = subprocess.run(
                    ["bash", "-e", "-c", parser, "parser-test", *arguments],
                    text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("--run-ignored only", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
