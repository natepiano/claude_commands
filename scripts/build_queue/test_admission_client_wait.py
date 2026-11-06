"""A waiting shell keeps its queue place through client failures."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import TypedDict, cast, override
import unittest
from unittest import mock


from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import snapshot_codec as codec, step_estimator
from scripts.build_queue.admission_test_support import AdmissionFixture, FakeClock
from scripts.lint import memory_admit as gate


class CheckerCall(TypedDict):
    pid: int
    action: str
    rows: dict[str, gate.Waiter]


def wait_client_main() -> int:
    """Inject estimate failures while the real client uses the private socket."""
    root = Path(os.environ["TEST_WAIT_ROOT"])
    calls = root / "calls.jsonl"
    actions = os.environ["TEST_CHECKER_ACTIONS"].split(",")
    clock = FakeClock()
    clock.epoch = time.time()
    def estimate(argv: list[str], cwd: str, profile: gate.InferFromArgv | gate.StepProfile | gate.ProgramProfile,
                 now: datetime | Callable[[], datetime], root: Path, policy: gate.AdmissionPolicy, *,
                 first_peak_bytes: int,
                 build_history_generation: Callable[[], gate.BuildHistoryGeneration | gate.NoBuildHistoryGeneration]
                 = gate.no_build_history_generation) -> gate.StepEstimate:
        _ = argv, cwd, profile, now, root, policy, first_peak_bytes, build_history_generation
        previous = calls.read_text().splitlines() if calls.exists() else []
        action = actions[min(len(previous), len(actions) - 1)]
        owner = int(sys.argv[sys.argv.index("--pid") + 1])
        waiting = cast(CheckerCall, json.loads(previous[-1]))["rows"] if previous else {}
        with calls.open("a") as stream:
            _ = stream.write(json.dumps({"pid": owner, "action": action, "rows": waiting}) + "\n")
        if action in ("fail", "silent-fail"):
            raise RuntimeError("promoted checker unavailable" if action == "fail" else "")
        if action == "admit":
            path = Path(os.environ["BUILDLOG_MEMINFO"])
            _ = path.write_text(f"MemTotal: {60 * gate.GIB // 1024} kB\nMemAvailable: {60 * gate.GIB // 1024} kB\n")
            _ = Path(os.environ["BUILDLOG_MEMORY_PRESSURE"]).write_text("full avg10=0 total=0\nsome avg10=0\n")
        peak = gate.ExpectedPeak(7 * gate.GIB,
            cast(gate.NeedSource, "future-source") if action == "unknown" else "measured", 5)
        return gate.StepEstimate(gate.StepProfile("fixture", "nextest", "tree", peak,
            estimated_at=clock.now()), gate.ReusableEstimate())
    with mock.patch.object(client, "CLOCK", clock), mock.patch.object(step_estimator, "step_estimate", side_effect=estimate):
        with mock.patch.object(sys, "argv", ["admission_client.py", *sys.argv[2:]]):
            return client.main()


class AdmissionClientWaitTests(unittest.TestCase):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    root: Path = Path()
    grants: list[wire.Admitted] = []
    grant_charges: list[tuple[tuple[str, int], ...]] = []
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.admission = self.enterContext(AdmissionFixture())
        self.root = self.admission.root
        self.admission.clock.epoch = time.time()
        self.admission.free(60 * gate.GIB)
        _ = (self.root / "pressure").write_text("full avg10=10 total=0\nsome avg10=0\n")
        self.grants = []
        self.grant_charges = []
        self.environment = {**os.environ,
            "TEST_WAIT_ROOT": str(self.root), "BUILDLOG_MEM_CHECK_RETRY_S": "2",
            "BUILDLOG_MEM_WAIT_SINCE": str(int(time.time()) - 1200),
            "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": "",
            "BUILDLOG_PEAK": "", "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_HOLD_DIR": str(self.root / "holds"), "BUILD_HOLD_RELEASE_DIR": str(self.root / "releases"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"), "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "HOME": str(self.root)}
        from scripts.lint.test_memory_admit import write_memory_config
        write_memory_config(self.root / "build_admission.conf", reconnect_seconds="1", waiting_pass_seconds="0.01")
        def reply_filter(reply: wire.Reply) -> wire.Reply:
            calls = self.root / "calls.jsonl"
            if isinstance(reply, (wire.Wait, wire.Admitted)) and calls.exists():
                observations = [cast(CheckerCall, json.loads(line)) for line in calls.read_text().splitlines()]
                observations[-1]["rows"] = {str(waiting.row["pid"]): waiting.row.copy()
                    for waiting in self.admission.server.waiters.values()}
                _ = calls.write_text("".join(json.dumps(codec.snapshot_value(row)) + "\n" for row in observations))
            if isinstance(reply, wire.Admitted):
                self.grants.append(reply)
                self.grant_charges.append(tuple((record.reservation_id, record.need)
                    for record in self.admission.server.reservations.values()))
            if isinstance(reply, wire.Wait):
                return replace(reply, reason=gate.EstimateExpired(self.admission.clock.now()))
            return reply
        self.admission.server.reply_filter = reply_filter
        _ = self.enterContext(self.admission.serving())

    def run_wait(self, actions: str, *, launch: bool = False) -> subprocess.CompletedProcess[str]:
        command = ('set -e; source "$1"; buildlog_memory_python() { '
            + 'if [[ "$1" == "$BUILDLOG_MEM_ADMIT_SCRIPT" ]]; then '
            + 'shift; "$TEST_PY" "$TEST_WAIT_CLIENT" --wait-client "$@"; else "$TEST_PY" "$@"; fi; }; '
            + 'shift; if (( $# == 0 )); then '
            + 'trap \'printf "wait elapsed: %s\\n" "$BUILDLOG_MEM_WAIT_S" >&2\' EXIT; fi; '
            + 'buildlog_wait_for_memory "$@"; printf "%s\\n" "$BUILDLOG_MEM_OUTCOME"')
        return subprocess.run(["bash", "-c", command, "test",
            str(Path(__file__).parents[1] / "lint/memory_gate.sh"), *([] if launch else ["/bin/true"])],
            env={**self.environment, "TEST_CHECKER_ACTIONS": actions,
                 "TEST_WAIT_CLIENT": str(Path(__file__).resolve()),
                 "TEST_PY": str(Path(__file__).resolve().parents[1] / "lib/py")},
            cwd=self.root, text=True, capture_output=True, check=False, timeout=30)

    def calls(self) -> list[CheckerCall]:
        return [cast(CheckerCall, json.loads(line))
            for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_unknown_source_and_failed_poll_preserve_the_real_waiting_row(self) -> None:
        result = self.run_wait("unknown,fail,hold,admit")
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
            self.assertIn("order_since", row)
            assert "order_since" in row
            places.append((path, row["pid"], row["since"], row["order_since"]))
        self.assertEqual(places[0], places[1])
        self.assertEqual(places[1], places[2])
        self.assertEqual(calls[-1]["rows"], {})
        # A reconnect can replay a grant while the client refreshes its estimate.
        self.assertEqual(len({grant.reservation for grant in self.grants}), 1)
        for grant, charges in zip(self.grants, self.grant_charges, strict=True):
            self.assertEqual(grant, self.grants[0])
            self.assertEqual(charges, ((grant.reservation, grant.need),))

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
        self.assertEqual(self.grants, [])

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
        self.assertEqual(self.admission.server.waiters, {})
        self.assertEqual(self.grants, [])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--wait-client"]:
        raise SystemExit(wait_client_main())
    _ = unittest.main()
