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

# findings.py has no compiled limits, so every run needs a complete config.
# These are the machine's shipped values; a test that cares about one limit
# overrides just that key on top of them.
STANDARD_CONFIG = """\
MIN_REPAIR_BUDGET=3
REPAIR_ROUNDS_PER_FINDING=0.5
RUNAWAY_ROUNDS=5
MAX_FIX_ATTEMPTS=2
MAX_REOPENS=2
STALLED_ROUNDS=2
MAX_CONSECUTIVE_SAME_KIND_PASSES=3
MAX_REVIEW_CANCELLATIONS=1
"""


class FindingsLedgerTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session_dir = self.root / "session"
        self.session_dir.mkdir()
        self.history_file = self.root / "history" / "run.jsonl"
        self.config_file = self.root / "delegate.conf"
        self.write_config("")

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self, at: int) -> dict[str, str]:
        """Pin the clock and the config so a machine's own delegate.conf cannot move a limit."""
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        # The gate reads pass events from the durable stream under this root.
        # Left unset it resolves to the machine's own history, where a phase id
        # this fixture invented would be answered by whatever happens to live
        # there.
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.root / "history")
        return environment

    def write_config(self, text: str) -> None:
        """Write the standard limits with `text` appended; a repeated key there wins."""
        _ = self.config_file.write_text(STANDARD_CONFIG + text, encoding="utf-8")

    def write_partial_config(self, text: str) -> None:
        """Write `text` as the whole file, standard limits omitted."""
        _ = self.config_file.write_text(text, encoding="utf-8")

    def run_command(self, *arguments: str, at: int = 1_000) -> str:
        result = subprocess.run(
            ["python3", str(SCRIPT), *arguments, "--session-dir", str(self.session_dir)],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment(at),
        )
        return result.stdout.strip()

    def run_failing_command(self, *arguments: str, at: int = 1_000) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments, "--session-dir", str(self.session_dir)],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(at),
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

    def write_pass_events(
        self,
        instance_id: str,
        passes: list[tuple[str, str, str]],
        slot_key: str = "team_slot",
    ) -> None:
        """Stand in for the recorder's durable stream: (kind, seat, status) rows.

        In the order the passes opened. An empty status leaves the pass open,
        which is the row a launcher still waiting on its agent contributes. An
        empty `slot_key` writes no seat field at all, which is every pass the
        back corpus recorded.
        """
        path = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for index, (kind, slot, status) in enumerate(passes):
            started: dict[str, object] = {
                "event_type": "pass_started",
                "phase_instance_id": instance_id,
                "pass_instance_id": f"pass-{index}",
                "pass_kind": kind,
            }
            if slot_key:
                started[slot_key] = slot
            lines.append(json.dumps(started))
            if status:
                finished = {**started, "event_type": "pass_finished", "status": status}
                lines.append(json.dumps(finished))
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
        """Dispatch a batch, land it, and record every closure verdict for it.

        `landed` stands in for the launcher, which is what calls it in a real run
        once the repair worker exits cleanly.
        """
        _ = self.run_command("dispatch", "--covers", ",".join(covers))
        _ = self.run_command("landed")
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
        _ = self.run_command("landed")
        result = self.run_failing_command("gate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Record a closure verdict for F001", result.stderr)

    def test_a_dispatched_repair_is_in_flight_not_fixed(self) -> None:
        """Launching a repair proves an attempt was made, never that it landed.

        This is the whole point of the in-flight state: a killed repair used to
        leave its findings labelled `fixed_pending_review`, so the next reviewer
        was handed a live defect and asked only to confirm the fix.
        """
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual(findings[0]["state"], "repair_in_flight")
        self.assertEqual(self.status()["in_flight"], ["F001"])

    def test_gate_refuses_while_a_repair_is_in_flight(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        result = self.run_failing_command("gate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still in flight for F001", result.stderr)

    def test_no_verdict_may_be_recorded_while_a_repair_is_in_flight(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        result = self.run_failing_command("verdict", "--id", "F001", "--state", "accepted")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has a repair in flight", result.stderr)

    def test_abandon_reopens_the_batch_and_refunds_the_attempt(self) -> None:
        """A repair killed before it edited anything consumed no repair.

        Charging it would spend the budget on work that never happened, which is
        how a killed dispatch turns into a spurious convergence stop.
        """
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        _ = self.run_command("abandon", "--reason", "the user stopped the launcher")
        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual(findings[0]["state"], "open")
        self.assertEqual(findings[0]["fix_attempts"], 0)
        self.assertEqual(self.status()["rounds_completed"], 0)
        self.assertEqual(self.status()["rounds_dispatched"], 1)

    def test_abandon_keeps_the_attempt_when_edits_landed(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        _ = self.run_command(
            "abandon", "--reason", "worker exited with code 1", "--edits-landed"
        )
        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual(findings[0]["state"], "open")
        self.assertEqual(findings[0]["fix_attempts"], 1)
        self.assertEqual(self.status()["rounds_completed"], 1)

    def test_an_abandoned_round_does_not_narrow_the_gate_to_blockers(self) -> None:
        """The first round is the only one that gates minors; a dead round keeps it."""
        _ = self.open_finding("blocker", "null deref")
        _ = self.open_finding("minor", "unused import")
        _ = self.run_command("dispatch", "--covers", "F001,F002")
        _ = self.run_command("abandon", "--reason", "the user stopped the launcher")
        payload = self.gate()
        self.assertEqual(payload["gating_severities"], ["blocker", "minor"])

    def test_abandon_appends_its_reason_and_leaves_the_dispatch_recorded(self) -> None:
        self.write_progress_state("phase-instance-1")
        _ = self.open_finding("blocker", "null deref")
        _ = self.run_command("dispatch", "--covers", "F001")
        _ = self.run_command("abandon", "--reason", "the user stopped the launcher")
        events = self.events()
        types = [event["event_type"] for event in events]
        self.assertIn("finding_batch_dispatched", types)
        self.assertIn("finding_batch_abandoned", types)
        abandoned = [
            event for event in events if event["event_type"] == "finding_batch_abandoned"
        ]
        self.assertEqual(abandoned[0]["reason"], "the user stopped the launcher")

    def test_abandon_refuses_when_no_repair_is_in_flight(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        result = self.run_failing_command("abandon", "--reason", "nothing to abandon")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No repair round is in flight", result.stderr)

    def test_landed_is_quiet_when_no_repair_is_in_flight(self) -> None:
        """Implementation dispatches call it too, and must not fail because of it."""
        _ = self.open_finding("blocker", "null deref")
        self.assertEqual(self.run_command("landed"), "no repair round in flight")

    def test_a_finding_that_fails_to_close_twice_is_advised_not_blocked(self) -> None:
        """The pattern is worth saying; it is not this script's call to make."""
        _ = self.open_finding("blocker", "null deref")
        self.close_round(["F001"], verdict="still_open")
        self.close_round(["F001"], verdict="still_open")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertIn("F001 failed to close after 2 repair attempts", str(payload["advisory"]))
        self.assertEqual(self.run_command("dispatch", "--covers", "F001"), "round 3 covering F001")

    def test_a_finding_that_reopens_twice_is_advised_not_blocked(self) -> None:
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
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertIn("reopened 2 times", str(payload["advisory"]))

    def test_a_gating_count_that_stops_decreasing_is_advised_not_blocked(self) -> None:
        """Each round closes its finding and the closure review opens a fresh one.

        Which is also what an honest phase looks like when every round repairs
        what it was given and the next gate finds something new, so this is
        exactly the pattern a watcher must be told about rather than stopped by.
        """
        _ = self.open_finding("blocker", "round 1")
        self.close_round(["F001"])
        _ = self.open_finding("blocker", "round 2")
        self.close_round(["F002"])
        _ = self.open_finding("blocker", "round 3")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertIn("has not decreased for 2 rounds", str(payload["advisory"]))

    def test_a_spent_repair_budget_is_advised_not_blocked(self) -> None:
        """Alternating 2 and 1 open blockers never trips the stall test.

        The budget does: two original findings buy the three-round floor, so
        the fourth gate reports it. A slow bleed is the shape this advisory
        exists to name.
        """
        for index in range(3):
            batch: list[str] = []
            for _ in range(2 if index % 2 == 0 else 1):
                batch.append(self.open_finding("blocker", f"round {index + 1}"))
            payload = self.gate()
            self.assertEqual(payload["verdict"], "dispatch", f"round {index + 1}")
            self.assertEqual(payload["repair_budget"], 0 if index == 0 else 3)
            self.close_round(batch)
        _ = self.open_finding("blocker", "one more")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertIn("repair budget", str(payload["advisory"]))
        self.assertIn("3 fix rounds have run for 2 original findings", str(payload["advisory"]))

    def test_the_repair_budget_floor_is_configurable(self) -> None:
        """delegate.conf raises the floor, and the extra round is authorized."""
        self.write_config("MIN_REPAIR_BUDGET=4\n")
        for index in range(3):
            batch: list[str] = []
            for _ in range(2 if index % 2 == 0 else 1):
                batch.append(self.open_finding("blocker", f"round {index + 1}"))
            payload = self.gate()
            self.assertEqual(payload["verdict"], "dispatch", f"round {index + 1}")
            self.assertEqual(payload["repair_budget"], 0 if index == 0 else 4)
            self.close_round(batch)
        _ = self.open_finding("blocker", "fourth round")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertIsNone(payload["advisory"])

    def test_an_unusable_config_value_stops_the_run(self) -> None:
        """A bad value is an error, not a fall back to some compiled limit."""
        self.write_config("MIN_REPAIR_BUDGET=zero\n")
        result = self.run_failing_command("gate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("MIN_REPAIR_BUDGET=zero is not a whole number", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_missing_config_key_stops_the_run(self) -> None:
        """Every limit must be set, and one run names all of the missing ones."""
        self.write_partial_config("MIN_REPAIR_BUDGET=3\n")
        result = self.run_failing_command("gate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("RUNAWAY_ROUNDS is not set", result.stderr)
        self.assertIn("MAX_REVIEW_CANCELLATIONS is not set", result.stderr)
        self.assertNotIn("MIN_REPAIR_BUDGET", result.stderr)

    def test_a_missing_config_file_stops_the_run(self) -> None:
        """No config at all is the same error, not a run on defaults."""
        self.config_file.unlink()
        result = self.run_failing_command("gate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be read", result.stderr)

    def test_repair_budget_scales_with_the_original_finding_count(self) -> None:
        """Eight findings buy four rounds; two buy the floor of three."""
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

    def test_three_seats_running_one_kind_each_do_not_trip_the_gate(self) -> None:
        """A phase team is one round with three workers, not three rounds.

        Every member records a pass of its own now, so the flat sequence reaches
        the same-kind limit on the first team phase that ever runs. The limit is
        about one seat being handed the same work repeatedly.
        """
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("impl", "impl", "completed"),
                ("impl", "test", "completed"),
                ("impl", "review", "completed"),
            ],
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["passes_run"], 3)
        self.assertEqual(payload["consecutive_same_kind"], {"kind": "impl", "count": 1})
        self.assertIsNone(payload["advisory"])

    def test_one_seat_repeating_a_kind_trips_the_gate_beside_busy_peers(self) -> None:
        """The run is counted inside a seat, so its peers cannot hide it."""
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("impl", "impl", "completed"),
                ("fix", "test", "completed"),
                ("impl", "impl", "completed"),
                ("review", "review", "completed"),
                ("impl", "impl", ""),
            ],
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["consecutive_same_kind"], {"kind": "impl", "count": 3})
        self.assertEqual(
            payload["advisory"],
            "3 consecutive impl passes ran without the phase advancing",
        )

    def test_passes_with_no_seat_are_counted_as_one_sequence(self) -> None:
        """The back corpus and every solo phase keep today's answer exactly.

        No pass recorded before seats existed carries one, so they share the
        empty bucket and the run is computed over the flat sequence.
        """
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("impl", "", "completed"),
                ("impl", "", "completed"),
                ("impl", "", "completed"),
            ],
            slot_key="",
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["consecutive_same_kind"], {"kind": "impl", "count": 3})
        self.assertEqual(
            payload["advisory"],
            "3 consecutive impl passes ran without the phase advancing",
        )

    def test_one_canceled_review_per_seat_is_not_a_review_that_cannot_finish(self) -> None:
        """Three reviewers interrupted once each is not one that never completes."""
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("review", "impl", "canceled"),
                ("review", "test", "canceled"),
                ("review", "review", "canceled"),
            ],
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["review_cancellations"], 1)
        self.assertIsNone(payload["advisory"])

    def test_repeated_cancellations_in_one_seat_still_trip_the_review_limit(self) -> None:
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("review", "", "canceled"),
                ("review", "", "canceled"),
            ],
            slot_key="",
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["review_cancellations"], 2)
        self.assertEqual(
            payload["advisory"],
            "the blind review was canceled 2 times, so it is never completing",
        )

    def test_a_seat_recorded_before_the_field_was_renamed_stays_one_sequence(self) -> None:
        """A phase that spans the rename must not read as two seats.

        The field was `team_role` for a day. A launcher whose pass opened under
        the old name and a peer opened under the new one are still two seats,
        and the seat that repeated a kind is still the one that trips.
        """
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("impl", "impl", "completed"),
                ("impl", "impl", "completed"),
                ("impl", "impl", "completed"),
            ],
            slot_key="team_role",
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["consecutive_same_kind"], {"kind": "impl", "count": 3})

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
                "finding_batch_landed",
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
