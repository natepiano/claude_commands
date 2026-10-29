#!/usr/bin/env python3
"""Integration tests for the unit findings ledger."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
from typing import cast
import unittest
from unittest.mock import patch

from scripts.direct.findings_test_support import FindingsLedgerFixture, findings


class FindingsLedgerTests(FindingsLedgerFixture):
    def test_event_write_syncs_by_default_and_flushes_without_sync_for_disposable_state(self) -> None:
        self.write_progress_state("phase-probe")
        append = cast(Callable[[Path, dict[str, object], dict[str, object]], None], vars(findings)["_append_event"])

        def flushed(descriptor: int) -> None:
            self.assertGreaterEqual(descriptor, 0)
            self.assertTrue(self.history_file.read_text())

        for durable in (True, False):
            with self.subTest(durable=durable), patch.dict(os.environ), patch.object(os, "fsync", side_effect=flushed) as sync:
                if durable:
                    _ = os.environ.pop("UNIT_SYNC_WRITES", None)
                else:
                    os.environ["UNIT_SYNC_WRITES"] = "0"
                append(self.session_dir, {}, {"event_type": "probe"})
                self.assertEqual(sync.call_count, int(durable))
                self.assertEqual(self.events()[0]["event_type"], "probe")
            self.history_file.unlink()

    def test_an_older_open_phase_finds_its_ledger_when_it_is_current_again(self) -> None:
        self.write_progress_state("phase-six", "6")
        self.assertEqual(self.open_finding("blocker", "six's finding"), "F001")
        self.write_progress_state("phase-eighteen", "18")
        self.assertEqual(self.open_finding("minor", "eighteen's finding"), "F001")
        self.write_progress_state("phase-six", "6")

        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual([finding["title"] for finding in findings], ["six's finding"])

    def test_a_landed_repair_for_the_older_open_phase_touches_only_its_ledger(self) -> None:
        self.write_two_open_phases()
        fifteen = self.run_command("open", "--phase", "15", "--severity", "blocker",
                                   "--title", "fifteen's finding", "--caught-by", "worker")
        sixteen = self.run_command("open", "--phase", "16", "--severity", "blocker",
                                   "--title", "sixteen's finding", "--caught-by", "worker")
        _ = self.run_command("dispatch", "--phase", "15", "--covers", fifteen)
        _ = self.run_command("dispatch", "--phase", "16", "--covers", sixteen)

        self.assertEqual(self.run_command("landed", "--phase", "15"), f"round 1 landed: {fifteen}")

        older = self.phase_status("15")
        newer = self.phase_status("16")
        self.assertEqual(older["in_flight"], [])
        self.assertEqual(newer["in_flight"], [sixteen])
        self.assertEqual([finding["title"] for finding in cast(list[dict[str, object]], newer["findings"])],
                         ["sixteen's finding"])
        live = cast(dict[str, object], json.loads(
            (self.session_dir / "findings_state.json").read_text(encoding="utf-8")))
        self.assertEqual(live["phase_id"], "16")

    def test_a_ledger_call_naming_no_phase_is_refused_while_two_are_open(self) -> None:
        self.write_two_open_phases()
        result = self.run_failing_command("open", "--severity", "blocker", "--title", "which phase",
                                          "--caught-by", "worker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2 phases are open (15, 16); name one with --phase", result.stderr)
        self.assertEqual(list(self.session_dir.glob("findings_state*.json")), [])

    def test_a_ledger_call_naming_a_closed_phase_is_refused(self) -> None:
        self.write_two_open_phases()
        result = self.run_failing_command("landed", "--phase", "14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Phase 14 is not open (open phases: 15, 16)", result.stderr)

    def test_ids_are_stable_across_rounds(self) -> None:
        self.assertEqual(self.open_finding("blocker", "first"), "F001")
        self.assertEqual(self.open_finding("minor", "second"), "F002")
        self.close_round(["F001", "F002"])
        self.assertEqual(self.open_finding("blocker", "third"), "F003")

    def test_a_finding_records_the_lens_that_caught_it(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.run_command(
            "open", "--severity", "minor", "--title", "stale caller",
            "--caught-by", "worker", "--lens", "contract",
        )
        _ = self.open_finding("minor", "main review only")
        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual([entry["lens"] for entry in findings], ["contract", ""])
        opened = [event for event in self.events() if event["event_type"] == "finding_opened"]
        self.assertEqual([event["lens"] for event in opened], ["contract", ""])

    def test_the_former_caught_by_name_is_stored_as_worker(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.run_command("open", "--severity", "minor", "--title", "old prompt", "--caught-by", "delegate")
        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual([entry["caught_by"] for entry in findings], ["worker"])
        opened = [event for event in self.events() if event["event_type"] == "finding_opened"]
        self.assertEqual([event["caught_by"] for event in opened], ["worker"])

    def test_a_finding_names_every_lens_that_raised_it(self) -> None:
        """One lens, several, or the legacy `both`, each stored as the ledger reads it."""
        self.write_progress_state("instance-a")
        for lens in ("craft", "adversary,craft", "ux", "both", "craft, ux,craft"):
            _ = self.run_command(
                "open", "--severity", "minor", "--title", f"raised by {lens}",
                "--caught-by", "worker", "--lens", lens,
            )
        expected = ["craft", "adversary,craft", "ux", "both", "craft,ux"]
        findings = cast(list[dict[str, object]], self.status()["findings"])
        self.assertEqual([entry["lens"] for entry in findings], expected)
        opened = [event for event in self.events() if event["event_type"] == "finding_opened"]
        self.assertEqual([event["lens"] for event in opened], expected)

    def test_an_unknown_lens_is_refused_and_records_nothing(self) -> None:
        self.write_progress_state("instance-a")
        for lens in ("style", "adversary,style", "both,craft", "craft,"):
            result = self.run_failing_command(
                "open", "--severity", "minor", "--title", "misspelt lens",
                "--caught-by", "worker", "--lens", lens,
            )
            self.assertEqual(result.returncode, 2, lens)
            self.assertIn("argument --lens: unknown lens", result.stderr)
        self.assertEqual(self.status()["findings"], [])
        self.assertEqual(self.events(), [])

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
        _ = self.record_astra("F001")
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

        Which is also what a converging phase looks like when every round repairs
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
        """direct.conf raises the floor, and the extra round is authorized."""
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

    def test_three_workers_running_one_kind_each_do_not_trip_the_gate(self) -> None:
        """A phase team is one round with three workers, not three rounds.

        Every member records a pass of its own now, so the flat sequence reaches
        the same-kind limit on the first team phase that ever runs. The limit is
        about one worker being handed the same work repeatedly.
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

    def test_one_worker_repeating_a_kind_trips_the_gate_beside_busy_peers(self) -> None:
        """The run is counted inside a worker, so its peers cannot hide it."""
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

    def test_passes_with_no_worker_are_counted_as_one_sequence(self) -> None:
        """The back corpus and every solo phase keep today's answer exactly.

        No pass recorded before workers existed carries one, so they share the
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

    def test_one_canceled_review_per_worker_is_not_a_review_that_cannot_finish(self) -> None:
        """Two reviewers interrupted once each is not one that never completes."""
        self.write_progress_state("phase-instance")
        self.write_pass_events(
            "phase-instance",
            [
                ("review", "impl", "canceled"),
                ("review", "test", "canceled"),
            ],
        )
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["review_cancellations"], 1)
        self.assertIsNone(payload["advisory"])

    def test_repeated_cancellations_in_one_worker_still_trip_the_review_limit(self) -> None:
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

    def test_a_worker_recorded_before_the_field_was_renamed_stays_one_sequence(self) -> None:
        """A phase that spans the rename must not read as two workers.

        The field was `team_role` for a day. A launcher whose pass opened under
        the old name and a peer opened under the new one are still two workers,
        and the worker that repeated a kind is still the one that trips.
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

    def test_a_mixed_batch_routes_the_gap_and_dispatches_the_kept_regression(self) -> None:
        self.write_progress_state("instance-a")
        self.write_repair_intervals(3)
        _ = self.open_finding("blocker", "accepted retry regressed")
        _ = self.open_finding("blocker", "owner rule is missing")
        _ = self.record_astra("F001")
        _ = self.record_astra("F002")
        _ = self.route_finding("F002")
        gate = self.gate()
        self.assertEqual((gate["round"], gate["repair_round"]), (1, 4))
        self.assertEqual([row["id"] for row in cast(list[dict[str, object]], gate["batch"])], ["F001"])
        result = self.run_failing_command(
            "dispatch", "--covers", "F001", "--keep", "F001=regression:accepted retry now loses data",
            "--accepted-checks", "Retry preserves the queued data",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.status()
        self.assertEqual(status["in_flight"], ["F001"])
        routed = cast(list[dict[str, object]], status["routed"])
        self.assertEqual([row["id"] for row in routed], ["F002"])
        self.assertEqual(routed[0]["state"], "routed")
        event, = [row for row in self.events() if row["event_type"] == "finding_routed"]
        self.assertEqual({key: event[key] for key in (
            "finding_id", "reason", "owner", "work_order", "due", "evidence", "repair_round",
            "round", "fix_attempts", "astra_approach",
        )}, {
            "finding_id": "F002", "reason": "spec-gap", "owner": "receiving-unit",
            "work_order": "docs/follow-up.md — Phase 6 — Specify retry ownership",
            "due": "Phase 6 checkpoint", "evidence": "The Work Order has no owner rule",
            "repair_round": 4, "round": 1, "fix_attempts": 0,
            "astra_approach": "Replace the handle owner",
        })
        self.assertIn("timestamp", event)
        self.assertIn("timestamp_epoch", event)
        dispatched, = [row for row in self.events() if row["event_type"] == "finding_batch_dispatched"]
        self.assertEqual((dispatched["round"], dispatched["repair_round"]), (1, 4))
        self.assertEqual(dispatched["accepted_checks"], "Retry preserves the queued data")
        self.assertEqual(dispatched["kept"], {
            "F001": {"category": "regression", "evidence": "accepted retry now loses data"},
        })
        gate_event, = [row for row in self.events() if row["event_type"] == "finding_gate"]
        self.assertEqual((gate_event["round"], gate_event["repair_round"]), (1, 4))
        self.assertFalse(any(row["event_type"] == "trial_exposure" and row["proposal"] == "P3"
                             for row in self.events()))

    def test_routing_before_round_four_keeps_the_work_open_but_out_of_the_gate(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "missing ownership rule")
        _ = self.route_finding("F001", reason="outside-kept")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "converged")
        self.assertEqual(payload["open_counts"], {"blocker": 0, "minor": 0, "nit": 0})
        self.assertEqual(payload["batch"], [])
        status = self.status()
        self.assertEqual([row["id"] for row in cast(list[dict[str, object]], status["routed"])], ["F001"])
        event, = [row for row in self.events() if row["event_type"] == "finding_routed"]
        self.assertEqual((event["repair_round"], event["round"], event["astra_approach"]), (1, 1, ""))
        self.assertEqual(event["reason"], "outside-kept")

    def test_round_four_requires_keep_for_each_finding_using_the_history_counter(self) -> None:
        self.write_progress_state("instance-a")
        self.write_repair_intervals(3)
        self.write_repair_intervals(5, instance="another-phase")
        for title in ("first regression", "second regression"):
            _ = self.open_finding("blocker", title)
        self.assertEqual((self.gate()["round"], self.gate()["repair_round"]), (1, 4))
        for finding_id in ("F001", "F002"):
            _ = self.record_astra(finding_id)
        result = self.run_failing_command("dispatch", "--covers", "F001,F002")
        self.assertNotEqual(result.returncode, 0)
        for finding_id in ("F001", "F002"):
            self.assertIn(finding_id, result.stderr)
        self.assertIn("route it with", result.stderr)
        self.assertIn("findings.py route", result.stderr)
        self.assertIn("kept category and its evidence", result.stderr)
        self.assertEqual(self.status()["in_flight"], [])
        partial = self.run_failing_command(
            "dispatch", "--covers", "F001,F002", "--keep", "F001=broken:retry raises",
        )
        self.assertNotEqual(partial.returncode, 0)
        self.assertIn("F002", partial.stderr)

    def test_no_history_uses_the_ledger_round_and_respects_configured_routing_limit(self) -> None:
        self.write_config("ROUTING_FROM_ROUND=2\n")
        _ = self.open_finding("blocker", "first")
        self.close_round(["F001"])
        _ = self.open_finding("blocker", "second")
        payload = self.gate()
        self.assertEqual((payload["round"], payload["repair_round"]), (2, 2))
        self.assertEqual(payload["astra_review_due"], ["F002"])
        _ = self.record_astra("F002")
        missing = self.run_failing_command("dispatch", "--covers", "F002")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--keep", missing.stderr)

    def test_astra_is_due_at_each_new_failed_attempt_and_clears_with_a_record(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry never closes")
        self.assertEqual(self.gate()["astra_review_due"], [])
        for attempt in range(2):
            self.close_round(["F001"], verdict="still_open")
            if attempt == 0:
                self.assertEqual(self.gate()["astra_review_due"], [])
        self.assertEqual(self.gate()["astra_review_due"], ["F001"])
        denied = self.run_failing_command("dispatch", "--covers", "F001")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("F001", denied.stderr)
        self.assertIn("astra", denied.stderr.lower())
        note = self.record_astra("F001")
        self.assertEqual(self.gate()["astra_review_due"], [])
        review, = [row for row in self.events() if row["event_type"] == "finding_astra_review"]
        self.assertEqual({key: review[key] for key in (
            "finding_id", "note", "approach", "unavailable", "fix_attempts", "repair_round",
        )}, {"finding_id": "F001", "note": str(note), "approach": "Replace the handle owner",
             "unavailable": "", "fix_attempts": 2, "repair_round": 3})
        self.close_round(["F001"], verdict="still_open")
        self.assertEqual(self.gate()["astra_review_due"], ["F001"])
        _ = self.record_astra("F001", unavailable="Astra is busy")
        self.assertEqual(self.gate()["astra_review_due"], [])
        latest = [row for row in self.events() if row["event_type"] == "finding_astra_review"][-1]
        self.assertEqual((latest["unavailable"], latest["approach"], latest["fix_attempts"]),
                         ("Astra is busy", "", 3))

    def test_round_four_also_requires_astra_before_routing_a_never_tried_finding(self) -> None:
        self.write_progress_state("instance-a")
        self.write_repair_intervals(3)
        _ = self.open_finding("blocker", "missing rule")
        self.assertEqual(self.gate()["astra_review_due"], ["F001"])
        denied = self.run_failing_command(
            "route", "--id", "F001", "--reason", "outside-kept", "--owner", "unit-b",
            "--work-order", "plan.md — Phase 6 — Define the rule", "--due", "Phase 6 checkpoint",
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("astra", denied.stderr.lower())
        _ = self.record_astra("F001")
        _ = self.route_finding("F001")
        self.assertEqual(self.gate()["verdict"], "converged")

    def test_astra_refuses_a_missing_note_without_clearing_the_due_review(self) -> None:
        self.write_progress_state("instance-a")
        self.write_repair_intervals(3)
        _ = self.open_finding("blocker", "retry")
        result = self.run_failing_command(
            "astra", "--id", "F001", "--note", str(self.root / "missing.md"),
            "--approach", "Replace the owner",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.gate()["astra_review_due"], ["F001"])
        self.assertFalse(any(row["event_type"] == "finding_astra_review" for row in self.events()))

    def test_the_failed_fix_trigger_uses_the_configured_limit(self) -> None:
        self.write_config("ASTRA_AFTER_FAILED_FIXES=1\n")
        _ = self.open_finding("blocker", "retry")
        self.close_round(["F001"], verdict="still_open")
        self.assertEqual(self.gate()["astra_review_due"], ["F001"])

    def test_route_refuses_empty_handoff_fields_and_non_open_states(self) -> None:
        _ = self.open_finding("blocker", "retry")
        options = {"--owner": "unit-b", "--work-order": "plan.md — Phase 6 — Retry", "--due": "checkpoint"}
        for empty in options:
            with self.subTest(empty=empty):
                arguments = ["route", "--id", "F001", "--reason", "spec-gap"]
                for name, value in options.items():
                    arguments.extend([name, " " if name == empty else value])
                denied = self.run_failing_command(*arguments)
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(self.gate()["verdict"], "dispatch")
        for state in ("repair_in_flight", "fixed_pending_review", "accepted"):
            if state == "repair_in_flight":
                _ = self.run_command("dispatch", "--covers", "F001")
            elif state == "fixed_pending_review":
                _ = self.run_command("landed")
            else:
                _ = self.run_command("verdict", "--id", "F001", "--state", "accepted")
            denied = self.run_failing_command(
                "route", "--id", "F001", "--reason", "spec-gap", "--owner", "unit-b",
                "--work-order", "plan.md — Phase 6 — Retry", "--due", "checkpoint",
            )
            self.assertNotEqual(denied.returncode, 0, state)
            rows = cast(list[dict[str, object]], self.status()["findings"])
            self.assertEqual(rows[0]["state"], state)

    def test_dispatch_requires_nonempty_accepted_checks(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry")
        for extra in ((), ("--accepted-checks", ""), ("--accepted-checks", "   ")):
            denied = self.run_failing_command("dispatch", "--covers", "F001", *extra,
                                              complete_dispatch=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("--accepted-checks", denied.stderr)
        self.assertFalse(any(row["event_type"] == "finding_batch_dispatched" for row in self.events()))
        _ = self.run_command("dispatch", "--covers", "F001", "--accepted-checks", "none: no accepted caller")
        dispatched, = [row for row in self.events() if row["event_type"] == "finding_batch_dispatched"]
        self.assertEqual(dispatched["accepted_checks"], "none: no accepted caller")

    def test_trial_exposures_keep_the_basis_decision_and_repair_round(self) -> None:
        self.write_progress_state("instance-a")
        for proposal, basis, decision in (
            ("P2", "Work Order changes retry lifecycle", ""),
            ("P1", "Verification names the retry tests", "director ran the commands"),
        ):
            options = ("--decision", decision) if decision else ()
            _ = self.run_command("exposure", "--proposal", proposal, "--basis", basis, *options)
        exposures = [row for row in self.events() if row["event_type"] == "trial_exposure"]
        self.assertEqual([{key: row[key] for key in ("proposal", "basis", "decision", "repair_round")}
                          for row in exposures], [
            {"proposal": "P2", "basis": "Work Order changes retry lifecycle", "decision": "", "repair_round": 1},
            {"proposal": "P1", "basis": "Verification names the retry tests",
             "decision": "director ran the commands", "repair_round": 1},
        ])
        _ = self.open_finding("blocker", "retry")
        self.close_round(["F001"], verdict="still_open")
        self.write_repair_intervals(3)
        _ = self.gate()
        p3 = [row for row in self.events() if row["event_type"] == "trial_exposure" and row["proposal"] == "P3"]
        self.assertTrue(p3)
        self.assertEqual(p3[-1]["repair_round"], 4)
        self.assertTrue(p3[-1]["basis"])

    def test_close_route_records_the_origin_and_closure_evidence(self) -> None:
        self.write_progress_state("receiving-instance", "6")
        _ = self.run_command("close-route", "--from", "origin-run:origin-instance:F003",
                             "--evidence", "abc123; retry tests pass; review accepted")
        closed, = [row for row in self.events() if row["event_type"] == "finding_route_closed"]
        self.assertEqual(closed["from"], "origin-run:origin-instance:F003")
        self.assertEqual(closed["evidence"], "abc123; retry tests pass; review accepted")
        self.assertEqual(closed["phase_instance_id"], "receiving-instance")

    def test_a_run_without_progress_state_records_no_events(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    _ = unittest.main()
