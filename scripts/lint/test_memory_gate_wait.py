"""A waiting shell keeps its queue place when a promoted checker changes."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import TypedDict, cast, override
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_admit as gate
from memory_test_support import fake_memory_readings, gate_isolation_environment


class CheckerCall(TypedDict):
    pid: int
    action: str
    rows: dict[str, gate.Waiter]


def checker_main() -> None:
    """Inject protocol/failure changes around the real check and ledger writes."""
    del sys.argv[1]  # The remaining argv is exactly memory_admit.py's protocol.
    root = Path(os.environ["TEST_WAIT_ROOT"])
    calls_path = root / "calls.jsonl"
    previous = calls_path.read_text().splitlines() if calls_path.exists() else []
    actions = os.environ["TEST_CHECKER_ACTIONS"].split(",")
    action = actions[min(len(previous), len(actions) - 1)]
    pid = int(sys.argv[3])
    if action == "admit":
        _ = fake_memory_readings(root, available_kib=60 * gate.GIB // 1024)
    output = io.StringIO()
    if action not in {"fail", "silent-fail"}:
        with patch.object(gate, "git_identity", return_value=(gate.UnknownRepository(), "fixture")), \
             patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(7 * gate.GIB, "measured", 5)), \
             patch.object(gate, "process_start_time", return_value="wait-owner"), \
             patch.object(gate, "process_table", return_value={pid: gate.ProcessIdentity(1, "wait-owner")}), \
             redirect_stdout(output):
            gate.main()
    rows = {path.name: gate.read_waiter(path) for path in (root / "admission").glob("*.waiting")}
    observation: CheckerCall = {"pid": pid, "action": action, "rows": rows}
    with calls_path.open("a") as stream:
        _ = stream.write(json.dumps(observation) + "\n")
    if action in {"fail", "silent-fail"}:
        if action == "fail":
            print("checker diagnostic context", file=sys.stderr)
            print("promoted checker unavailable", file=sys.stderr)
        raise SystemExit(1)
    fields = output.getvalue().strip().split("|")
    if action == "unknown":
        fields[3] = "future-source"
    print("|".join(fields))


class MemoryGateWaitTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "proc").mkdir()
        self.environment = {
            # The head qualifies for priority while pressure keeps it waiting.
            **os.environ, **fake_memory_readings(self.root, available_kib=26 * gate.GIB // 1024, full_avg10=11,
                                                total_kib=60 * gate.GIB // 1024),
            **gate_isolation_environment(self.root),
            "TEST_WAIT_ROOT": str(self.root), "BUILDLOG_DIR": str(self.root),
            "BUILDLOG_PROC": str(self.root / "proc"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "", "UNIT_TEAM_ROLE": "",
            "BUILDLOG_CHECK_FAILED_FILE": "", "BUILDLOG_MEM_BLOCKER": "",
            "BUILDLOG_MEM_POLL_S": "1", "BUILDLOG_MEM_CHECK_RETRY_S": "2",
            "BUILDLOG_MEM_WAIT_SINCE": "", "BUILDLOG_MEM_WAIT_FIRST_FILE": "",
            "BUILDLOG_BUILD_BUDGET_GIB": "21", "BUILDLOG_PEAK": "",
            "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": "",
        }

    def run_wait(self, actions: str, *, launch: bool = False,
                 rejoin: bool = False) -> subprocess.CompletedProcess[str]:
        # A deterministic shell clock exercises retry deadlines without sleeping.
        command = (
            'set -e; source "$1"; '
            + ('BUILDLOG_MEM_WAIT_SINCE=$TEST_QUEUE_WAIT_SINCE; ' if rejoin else '')
            + ('BUILDLOG_MEM_ADMIT_SCRIPT=$2; '
            'buildlog_memory_python() { '
            'if [[ "$1" == "$BUILDLOG_MEM_ADMIT_SCRIPT" ]]; then '
            'python3 "$1" --checker "${@:2}"; else printf "UTC\\n"; fi; }; '
            'unset SECONDS; SECONDS=0; sleeps=0; '
            'sleep() { SECONDS=$((SECONDS + 1)); sleeps=$((sleeps + 1)); '
            'if (( sleeps > 15 )); then exit 99; fi; }; '
            'shift 2; if (( $# == 0 )); then '
            'trap \'printf "wait elapsed: %s\\n" "$BUILDLOG_MEM_WAIT_S" >&2\' EXIT; fi; '
            'buildlog_wait_for_memory "$@"; printf "%s\\n" "$BUILDLOG_MEM_OUTCOME"')
        )
        return subprocess.run(
            ["bash", "-c", command, "test", str(Path(__file__).with_name("memory_gate.sh")),
             str(Path(__file__).resolve()), *([] if launch else ["/bin/true"])],
            env={**self.environment, "TEST_CHECKER_ACTIONS": actions,
                 "TEST_QUEUE_WAIT_SINCE": str(int(time.time()) - 1200)}, cwd=self.root,
            text=True, capture_output=True, check=False, timeout=30,
        )

    def calls(self) -> list[CheckerCall]:
        return [cast(CheckerCall, json.loads(line))
                for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_unknown_source_and_failed_poll_preserve_the_real_waiting_row(self) -> None:
        result = self.run_wait("unknown,fail,hold,admit", rejoin=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Granted\n")
        self.assertNotIn("step refused", result.stderr)
        self.assertIn("no history, so the fallback", result.stderr)
        retry = ("memory admission check failed (promoted checker unavailable); trying again, "
                 "the step keeps its place")
        self.assertEqual(result.stderr.splitlines().count(retry), 1)
        self.assertNotIn("checker diagnostic context", result.stderr)
        self.assertEqual(result.stderr.count("waiting for memory since "), 1)
        self.assertIn("memory free after", result.stderr)
        calls = self.calls()
        self.assertEqual([call["action"] for call in calls], ["unknown", "fail", "hold", "admit"])
        self.assertEqual(len({call["pid"] for call in calls}), 1)
        places: list[tuple[str, int, float, float]] = []
        for call in calls[:3]:
            self.assertEqual(len(call["rows"]), 1)
            path, row = next(iter(call["rows"].items()))
            self.assertEqual(row["pid"], call["pid"])
            self.assertIn("priority_since", row)
            assert "priority_since" in row
            places.append((path, row["pid"], row["since"], row["priority_since"]))
        self.assertEqual(places[0], places[1])
        self.assertEqual(places[1], places[2])
        self.assertEqual(calls[-1]["rows"], {})
        self.assertEqual(len(list((self.root / "admission").glob("*.reservation"))), 1)

    def test_continuous_failed_polls_eventually_refuse_with_status_two(self) -> None:
        result = self.run_wait("hold,fail")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("trying again, the step keeps its place", result.stderr)
        self.assertIn("memory admission check failed (promoted checker unavailable); step refused", result.stderr)
        calls = self.calls()
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(len({call["pid"] for call in calls}), 1)
        self.assertTrue(all(call["rows"] == calls[0]["rows"] for call in calls[1:]))
        self.assertEqual(list((self.root / "admission").glob("*.reservation")), [])

    def test_failed_checker_polls_cannot_extend_a_launch_wait_limit(self) -> None:
        self.environment["BUILDLOG_MEM_WAIT_LIMIT_S"] = "1"
        self.environment["BUILDLOG_MEM_CHECK_RETRY_S"] = "60"
        result = self.run_wait("hold,fail", launch=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("launch wait limit reached without admission", result.stderr)
        self.assertIn("wait elapsed: 1\n", result.stderr)
        self.assertNotIn("trying again", result.stderr)
        self.assertEqual([call["action"] for call in self.calls()], ["hold", "fail"])

    def test_successful_held_poll_resets_the_checker_failure_deadline(self) -> None:
        result = self.run_wait("hold,fail,fail,hold,fail,fail,admit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Granted\n")
        self.assertEqual(result.stderr.count("trying again, the step keeps its place"), 4)
        self.assertNotIn("step refused", result.stderr)
        self.assertEqual(len(self.calls()), 7)

    def test_parent_queue_identity_does_not_age_a_new_launch_or_step(self) -> None:
        parent_start = int(time.time()) - 1200
        parent_wait = self.root / "parent-first-wait"
        _ = parent_wait.write_text(f"{parent_start}\n")
        self.environment.update({"BUILDLOG_MEM_WAIT_SINCE": str(parent_start),
                                 "BUILDLOG_MEM_WAIT_FIRST_FILE": str(parent_wait),
                                 "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
        launch = self.run_wait("hold,fail", launch=True)
        self.assertEqual(launch.returncode, 2, launch.stderr)
        self.assertIn("wait elapsed: 1\n", launch.stderr)
        (self.root / "calls.jsonl").unlink()
        step = self.run_wait("hold,admit")
        self.assertEqual(step.returncode, 0, step.stderr)
        first, _ = self.calls()
        row, = first["rows"].values()
        self.assertGreater(row["since"], parent_start)
        self.assertEqual(parent_wait.read_text(), f"{parent_start}\n")

    def test_empty_checker_error_uses_the_retry_fallback(self) -> None:
        result = self.run_wait("hold,silent-fail,admit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory admission check failed (checker returned an error); trying again, "
                      + "the step keeps its place\n", result.stderr)
        self.assertEqual(len(self.calls()), 3)

    def test_first_checker_failure_refuses_without_retrying(self) -> None:
        result = self.run_wait("fail,admit")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("trying again", result.stderr)
        self.assertIn("memory admission check failed (promoted checker unavailable); step refused", result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.root / "admission").exists())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--checker"]:
        checker_main()
    else:
        _ = unittest.main()
