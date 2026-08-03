#!/usr/bin/env python3
"""Integration tests for the plan-delegate findings ledger."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("findings.py")


class FindingsLedgerTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session_dir = self.root / "session"
        self.session_dir.mkdir()
        self.history_file = self.root / "history" / "run.jsonl"

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self, *arguments: str, at: int = 1_000) -> str:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        result = subprocess.run(
            ["python3", str(SCRIPT), *arguments, "--session-dir", str(self.session_dir)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    def run_failing_command(self, *arguments: str, at: int = 1_000) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments, "--session-dir", str(self.session_dir)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def write_progress_state(self, instance_id: str, phase_id: str = "phase-1") -> None:
        """Stand in for progress_history.py: the ledger reads phase identity from it."""
        state = {
            "history_file": str(self.history_file),
            "phase": {"instance_id": instance_id, "id": phase_id, "title": phase_id},
        }
        _ = (self.session_dir / "progress_history_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    def open_finding(self, severity: str, title: str, *, at: int = 1_000) -> str:
        return self.run_command(
            "open", "--severity", severity, "--title", title, "--caught-by", "delegate", at=at
        )

    def gate(self, *, at: int = 1_000) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.run_command("gate", at=at)))

    def status(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.run_command("status")))

    def events(self) -> list[dict[str, object]]:
        if not self.history_file.exists():
            return []
        return [
            cast(dict[str, object], json.loads(line))
            for line in self.history_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def close_round(self, covers: list[str], *, verdict: str = "accepted") -> None:
        """Dispatch a batch and record every closure verdict for it."""
        _ = self.run_command("dispatch", "--covers", ",".join(covers))
        for finding_id in covers:
            _ = self.run_command("verdict", "--id", finding_id, "--state", verdict)

    def test_ids_are_stable_across_rounds(self) -> None:
        self.assertEqual(self.open_finding("blocker", "first"), "F001")
        self.assertEqual(self.open_finding("minor", "second"), "F002")
        self.close_round(["F001", "F002"])
        self.assertEqual(self.open_finding("blocker", "third"), "F003")

    def test_first_round_gates_blocker_and_minor_but_never_nits(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        _ = self.open_finding("minor", "unused import")
        _ = self.open_finding("nit", "comment typo")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertEqual(payload["gating_severities"], ["blocker", "minor"])
        batch = cast(list[dict[str, object]], payload["batch"])
        self.assertEqual([entry["id"] for entry in batch], ["F001", "F002"])
        non_gating = cast(list[dict[str, object]], payload["non_gating_open"])
        self.assertEqual([entry["id"] for entry in non_gating], ["F003"])

    def test_severity_gate_narrows_after_the_first_round(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"])
        _ = self.open_finding("minor", "raised by the closure review")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "converged")
        self.assertEqual(payload["gating_severities"], ["blocker"])
        self.assertEqual(payload["batch"], [])
        non_gating = cast(list[dict[str, object]], payload["non_gating_open"])
        self.assertEqual([entry["id"] for entry in non_gating], ["F002"])

    def test_a_blocker_still_gates_after_the_first_round(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"])
        _ = self.open_finding("blocker", "the repair broke a caller")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        batch = cast(list[dict[str, object]], payload["batch"])
        self.assertEqual([entry["id"] for entry in batch], ["F002"])

    def test_a_partial_batch_is_refused(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        _ = self.open_finding("minor", "unused import")
        result = self.run_failing_command("dispatch", "--covers", "F001")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must repair every gating open finding together", result.stderr)
        self.assertIn("F002", result.stderr)

    def test_dispatch_refuses_unknown_ids(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        result = self.run_failing_command("dispatch", "--covers", "F001,F099")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown finding ids", result.stderr)

    def test_dispatch_refuses_when_the_gate_says_converged(self) -> None:
        _ = self.open_finding("nit", "comment typo")
        result = self.run_failing_command("dispatch", "--covers", "F001")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gate says converged", result.stderr)

    def test_gate_refuses_while_a_closure_verdict_is_missing(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        result = self.run_failing_command("gate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Record a closure verdict for F001", result.stderr)

    def test_stop_when_a_finding_fails_to_close_twice(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"], verdict="still_open")
        self.close_round(["F001"], verdict="still_open")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "stop")
        self.assertIn("F001 failed to close after 2 repair attempts", str(payload["stop_reason"]))

    def test_stop_when_an_accepted_finding_reopens_twice(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"])
        _ = self.run_command(
            "verdict", "--id", "F001", "--state", "reopened", "--evidence", "hunk in cache.rs"
        )
        self.close_round(["F001"])
        _ = self.run_command(
            "verdict", "--id", "F001", "--state", "reopened", "--evidence", "same hunk, again"
        )
        payload = self.gate()
        self.assertEqual(payload["verdict"], "stop")
        self.assertIn("reopened 2 times", str(payload["stop_reason"]))

    def test_stop_when_the_gating_count_stops_decreasing(self) -> None:
        """Each round closes its finding and the closure review opens a fresh one."""
        _ = self.open_finding("blocker", "round 1")
        self.close_round(["F001"])
        _ = self.open_finding("blocker", "round 2")
        self.close_round(["F002"])
        _ = self.open_finding("blocker", "round 3")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "stop")
        self.assertIn("has not decreased for 2 rounds", str(payload["stop_reason"]))

    def test_repair_budget_stops_a_loop_that_never_stalls(self) -> None:
        """Alternating 2 and 1 open blockers never trips the stall test.

        The budget does: two original findings buy two rounds, so the third
        gate stops. A slow bleed used to run all the way to the backstop.
        """
        for index in range(2):
            batch: list[str] = []
            for _ in range(2 if index % 2 == 0 else 1):
                batch.append(self.open_finding("blocker", f"round {index + 1}"))
            payload = self.gate()
            self.assertEqual(payload["verdict"], "dispatch", f"round {index + 1}")
            self.assertEqual(payload["repair_budget"], 0 if index == 0 else 2)
            self.close_round(batch)
        _ = self.open_finding("blocker", "one more")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "stop")
        self.assertIn("repair budget", str(payload["stop_reason"]))
        self.assertIn("2 fix rounds have run for 2 original findings", str(payload["stop_reason"]))

    def test_repair_budget_scales_with_the_original_finding_count(self) -> None:
        """Eight findings buy four rounds; two buy the floor of two."""
        for _ in range(8):
            _ = self.open_finding("blocker", "one of eight")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.close_round([f"F{index:03d}" for index in range(1, 9)])
        payload = self.gate()
        self.assertEqual(payload["repair_budget"], 4)

    def test_the_gate_reports_pass_shape_without_history(self) -> None:
        """No durable event stream in the fixture, so the pass counters are empty."""
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["passes_run"], 0)
        self.assertIsNone(payload["consecutive_same_kind"])
        self.assertEqual(payload["review_cancellations"], 0)

    def test_reopening_an_accepted_finding_requires_evidence(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"])
        result = self.run_failing_command("verdict", "--id", "F001", "--state", "reopened")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--evidence is required", result.stderr)

    def test_still_open_is_refused_for_an_accepted_finding(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"])
        result = self.run_failing_command("verdict", "--id", "F001", "--state", "still_open")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("use --state reopened", result.stderr)

    def test_reopen_is_refused_for_a_finding_that_never_closed(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        result = self.run_failing_command(
            "verdict", "--id", "F001", "--state", "reopened", "--evidence", "x"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not accepted", result.stderr)

    def test_a_new_phase_resets_the_ledger(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "phase 1 defect")
        self.close_round(["F001"])
        self.write_progress_state("instance-b", phase_id="phase-2")
        payload = self.status()
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["rounds_completed"], 0)
        self.assertEqual(payload["phase_id"], "phase-2")
        self.assertEqual(self.open_finding("blocker", "phase 2 defect"), "F001")

    def test_events_land_in_the_run_history(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"])
        _ = self.gate()
        kinds = [str(event["event_type"]) for event in self.events()]
        self.assertEqual(
            kinds,
            [
                "finding_opened",
                "finding_batch_dispatched",
                "finding_verdict",
                "finding_gate",
            ],
        )
        for event in self.events():
            self.assertEqual(event["phase_instance_id"], "instance-a")

    def test_a_run_without_progress_state_records_no_events(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    _ = unittest.main()
