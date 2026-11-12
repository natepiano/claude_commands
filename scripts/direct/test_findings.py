#!/usr/bin/env python3
"""Integration tests for the unit findings ledger."""

from __future__ import annotations

from scripts.production.cli_fixture import process_boundary

from argparse import Namespace
from collections.abc import Callable
import json
import os
from pathlib import Path
from typing import cast
import unittest
from unittest.mock import patch

from scripts.direct.findings_test_support import FindingsLedgerFixture, findings
from scripts.history.routed import routed_work


class FindingsLedgerTests(FindingsLedgerFixture):
    def hold_finding(self) -> str:
        self.write_progress_state("decision-phase")
        return self.open_simplify("Remove retry behavior", held=True)

    def decided_finding(self, answer: str, words: str, *, at: int = 1_000) -> str:
        finding_id = self.hold_finding()
        _ = self.run_command("decision", "--id", finding_id, "--answer", answer,
                             "--user-words", words, at=at)
        return finding_id

    def assert_refused(self, message: str, *arguments: str) -> None:
        before = self.ledger()
        events = self.events()
        result = self.run_failing_command(*arguments)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.events(), events)

    def test_withdraw_replaces_a_decision_and_preserves_each_ruling_in_history(self) -> None:
        finding_id = self.decided_finding("declined", "Keep the retry.", at=1_010)
        gate_before = self.gate()
        expected_history: list[dict[str, object]] = []
        previous: dict[str, object] = {
            "answer": "declined", "user_words": "Keep the retry.", "timestamp_epoch": 1_010,
        }
        for at, answer, words, reason in (
            (1_020, "approved", "Remove the retry.", "The current ruling is withdrawn."),
            (1_030, "declined", "Keep the retry after all.", "The user changed the answer."),
        ):
            with self.subTest(answer=answer):
                _ = self.run_command("decision", "--id", finding_id, "--answer", answer,
                                     "--user-words", words, "--withdraw", reason, at=at)
                expected_history.append({**previous, "withdrawn_epoch": at, "withdraw_reason": reason})
                current: dict[str, object] = {"answer": answer, "user_words": words, "timestamp_epoch": at}
                entry = self.finding_rows()[0]
                self.assertEqual(entry["state"], "decided")
                self.assertEqual(entry["decision"], current)
                self.assertEqual(entry["decision_history"], expected_history)
                events = [event for event in self.events() if event["event_type"] == "finding_decision"]
                self.assertEqual(len(events), len(expected_history) + 1)
                self.assertEqual(events[-1]["withdrew"], expected_history[-1])
                self.assertEqual(events[-1]["user_words"], words)
                self.assertEqual(events[-1]["answer"], answer)
                self.assertEqual(events[-1]["timestamp_epoch"], at)
                previous = current
        self.assertEqual(self.gate(), gate_before)

    def test_retry_after_a_failed_decision_event_preserves_one_withdrawal(self) -> None:
        finding_id = self.decided_finding("declined", "Keep the retry.", at=1_010)
        decide = cast(Callable[[Namespace], None], vars(findings)["_decision"])
        args = Namespace(session_dir=str(self.session_dir), phase="", id=finding_id,
                         answer="approved", user_words="Remove the retry.", withdraw="The user changed the answer.")
        with patch.dict(os.environ, self.environment(1_020)), patch.object(
            findings, "_append_event", side_effect=OSError("The event append failed."),
        ):
            with self.assertRaisesRegex(OSError, "The event append failed"):
                decide(args)
        saved = self.ledger()
        self.assertEqual(len(self.events()), 2)
        for at in (1_030, 1_040):
            with self.subTest(at=at):
                _ = self.run_command("decision", "--id", finding_id, "--answer", "approved",
                                     "--user-words", "Remove the retry.",
                                     "--withdraw", "The user changed the answer.", at=at)
                self.assertEqual(self.ledger(), saved)
                entry = self.finding_rows()[0]
                history = cast(list[dict[str, object]], entry["decision_history"])
                self.assertEqual(len(history), 1)
                events = [event for event in self.events() if "withdrew" in event]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["withdrew"], history[0])
                self.assertEqual(events[0]["answer"], "approved")
                self.assertEqual(events[0]["user_words"], "Remove the retry.")
                self.assertEqual(events[0]["timestamp_epoch"], 1_020)

    def test_an_identical_decision_without_withdraw_changes_neither_state_nor_events(self) -> None:
        finding_id = self.decided_finding("declined", "Keep the retry.")
        before = self.ledger()
        events = self.events()
        _ = self.run_command("decision", "--id", finding_id, "--answer", "declined",
                             "--user-words", "Keep the retry.", at=1_010)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.events(), events)

    def test_a_decided_finding_requires_withdraw_before_a_new_answer(self) -> None:
        finding_id = self.decided_finding("declined", "Keep the retry.")
        for options, message in (
            ((), "use --withdraw"),
            (("--withdraw", " "), "--withdraw must not be empty"),
        ):
            with self.subTest(options=options):
                self.assert_refused(message, "decision", "--id", finding_id, "--answer", "approved",
                                    "--user-words", "Remove the retry.", *options)

    def test_withdraw_on_a_held_finding_is_refused(self) -> None:
        finding_id = self.hold_finding()
        for reason in ("No ruling exists.", ""):
            with self.subTest(reason=reason):
                self.assert_refused(
                    "--withdraw requires a decided finding", "decision", "--id", finding_id,
                    "--answer", "approved", "--user-words", "Remove the retry.", "--withdraw", reason,
                )

    def test_an_open_finding_refuses_a_decision_with_or_without_withdraw(self) -> None:
        self.write_progress_state("decision-phase")
        finding_id = self.open_finding("minor", "Remove repeated parsing")
        for options in ((), ("--withdraw", "Withdraw the ruling.")):
            with self.subTest(options=options):
                self.assert_refused(
                    "Only a held or decided finding can receive an answer", "decision", "--id", finding_id,
                    "--answer", "approved", "--user-words", "Remove repeated parsing.", *options,
                )

    def test_an_approved_decision_permits_a_closure_verdict(self) -> None:
        finding_id = self.decided_finding("approved", "Remove the retry.")
        self.assertEqual(self.run_command("verdict", "--id", finding_id, "--state", "accepted"),
                         f"{finding_id} accepted")
        entry = self.finding_rows()[0]
        self.assertEqual(entry["state"], "accepted")
        self.assertEqual(cast(dict[str, object], entry["decision"])["answer"], "approved")
        self.assertEqual(self.events()[-1]["event_type"], "finding_verdict")

    def test_a_declined_decision_refuses_a_closure_verdict(self) -> None:
        finding_id = self.decided_finding("declined", "Keep the retry.")
        for outcome in ("accepted", "still_open", "reopened"):
            with self.subTest(outcome=outcome):
                self.assert_refused("closed by the declined decision", "verdict", "--id", finding_id, "--state", outcome)

    def test_a_held_finding_refuses_a_closure_verdict(self) -> None:
        finding_id = self.hold_finding()
        for outcome in ("accepted", "still_open", "reopened"):
            with self.subTest(outcome=outcome):
                self.assert_refused("requires the user's decision", "verdict", "--id", finding_id, "--state", outcome)

    def test_changed_config_is_read_on_the_next_command_with_comments(self) -> None:
        _ = self.open_finding("blocker", "needs a repair")
        self.close_round(["F001"])
        self.write_config("MIN_REPAIR_BUDGET=4 # current floor\n")
        first = self.gate()
        self.write_config("MIN_REPAIR_BUDGET=8 # revised floor\n")
        second = self.gate()
        self.assertEqual(first["repair_budget"], 4)
        self.assertEqual(second["repair_budget"], 8)

    def test_rerun_finding_keeps_its_lens_in_ledger_and_history(self) -> None:
        self.write_progress_state("instance-rerun")
        _ = self.run_command("open", "--severity", "blocker", "--title", "Restore loses records",
                             "--caught-by", "worker", "--lens", "rerun")
        findings = self.finding_rows()
        self.assertEqual([entry["lens"] for entry in findings], ["rerun"])
        self.assertEqual([event["lens"] for event in self.events() if event["event_type"] == "finding_opened"],
                         ["rerun"])

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

        findings = self.finding_rows()
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
        live = cast(dict[str, object], json.loads(self.ledger()))
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

    # Keep process persistence coverage.
    @process_boundary
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
        findings = self.finding_rows()
        self.assertEqual([entry["lens"] for entry in findings], ["contract", ""])
        opened = [event for event in self.events() if event["event_type"] == "finding_opened"]
        self.assertEqual([event["lens"] for event in opened], ["contract", ""])

    def test_the_former_caught_by_name_is_stored_as_worker(self) -> None:
        self.write_progress_state("instance-a")
        _ = self.run_command("open", "--severity", "minor", "--title", "old prompt", "--caught-by", "delegate")
        findings = self.finding_rows()
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
        findings = self.finding_rows()
        self.assertEqual([entry["lens"] for entry in findings], expected)
        opened = [event for event in self.events() if event["event_type"] == "finding_opened"]
        self.assertEqual([event["lens"] for event in opened], expected)

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
        findings = self.finding_rows()
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
        findings = self.finding_rows()
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
        findings = self.finding_rows()
        self.assertEqual(findings[0]["state"], "open")
        self.assertEqual(findings[0]["fix_attempts"], 1)
        self.assertEqual(self.status()["rounds_completed"], 1)

    def test_two_refused_launches_and_one_landed_repair_count_one_round(self) -> None:
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry fails")
        _ = self.run_command("dispatch", "--covers", "F001")
        for launch in (1, 2):
            self.record_repair_pass(launch=launch, status="error", seconds=3)
        self.record_repair_pass(launch=3)
        _ = self.run_command("landed")
        _ = self.run_command("verdict", "--id", "F001", "--state", "still_open")
        payload = self.gate()
        self.assertEqual(payload["repair_round"], 2)
        self.assertEqual(payload["round"], 2)
        self.assertEqual(self.finding_rows()[0]["fix_attempts"], 1)
        self.assertEqual(payload["astra_review_due"], [])
        self.assertIsNone(payload["advisory"])
        self.assertEqual(payload["consecutive_same_kind"], {"kind": "fix", "count": 1})

    def test_refunded_launch_starts_spend_no_round_or_attempt(self) -> None:
        self.write_config("MAX_CONSECUTIVE_SAME_KIND_PASSES=2\n")
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry fails")
        for number in (1, 2):
            _ = self.run_command("dispatch", "--covers", "F001", at=1_000 + number * 100)
            self.record_repair_pass(status="error", seconds=3)
            _ = self.run_command("abandon", "--reason", "assign_gate refused the launch", at=1_090 + number * 100)
            payload = self.gate()
            self.assertEqual(payload["repair_round"], 1)
            self.assertEqual(payload["repair_budget"], 0)
            self.assertEqual(self.finding_rows()[0]["fix_attempts"], 0)
            self.assertEqual(payload["astra_review_due"], [])
            self.assertIsNone(payload["advisory"])
            self.assertIsNone(payload["consecutive_same_kind"])

    def test_mixed_launch_metadata_counts_one_ledger_batch(self) -> None:
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry fails")
        self.close_round(["F001"], verdict="still_open")
        records = self.events()
        for record in records:
            if record["event_type"] in {"pass_started", "pass_finished"}:
                _ = record.pop("launch_check", None)
        _ = self.history_file.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.record_repair_pass(launch=2)
        self.assertEqual(self.gate()["repair_round"], 2)
        self.assertEqual(self.run_command("dispatch", "--covers", "F001"), "round 2 covering F001")
        dispatched = [event for event in self.events() if event["event_type"] == "finding_batch_dispatched"][-1]
        self.assertEqual(dispatched["repair_round"], 2)

    def test_overlap_with_a_refunded_batch_counts_only_the_landed_batch(self) -> None:
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry fails")
        _ = self.run_command("dispatch", "--covers", "F001")
        self.record_repair_pass(status="error")
        _ = self.run_command("abandon", "--reason", "the launcher failed")
        self.close_round(["F001"], verdict="still_open")
        records = self.events()
        for record in records:
            if record["event_type"] in {"pass_started", "pass_finished"}:
                launch = cast(dict[str, object], record.pop("launch_check"))
                cause = cast(dict[str, object], launch["fix_cause"])
                record["fix_pass"] = cause["round"]
        gate: dict[str, object] = {
            "event_type": "pass_started", "phase_instance_id": "instance-a",
            "pass_instance_id": "overlapping-gate", "pass_kind": "fix", "fix_pass": 99,
            "pass_activity": "run the merge gate", "pass_started_at": 1_110, "timestamp_epoch": 1_110,
        }
        records.extend([gate, {**gate, "event_type": "pass_finished", "status": "completed",
                               "timestamp_epoch": 1_240, "pass_elapsed_seconds": 130}])
        _ = self.history_file.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.assertEqual(self.gate(at=1_500)["repair_round"], 2)

    def test_started_batches_without_dispatch_events_count_repair_rounds(self) -> None:
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        self.write_progress_state("instance-a")
        for number in range(1, 4):
            _ = self.open_finding("blocker", f"defect {number}")
            self.close_round([f"F{number:03d}"])
            retained = [event for event in self.events() if event["event_type"] != "finding_batch_dispatched"]
            _ = self.history_file.write_text("".join(json.dumps(event) + "\n" for event in retained), encoding="utf-8")
        self.assertEqual(self.status()["rounds_completed"], 3)
        self.assertEqual(self.gate()["repair_round"], 4)

    def test_gate_fix_and_unledgered_relaunch_add_no_repair_round(self) -> None:
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        self.write_progress_state("instance-a")
        _ = self.open_finding("blocker", "retry fails")
        self.close_round(["F001"], verdict="still_open")
        with self.history_file.open("a", encoding="utf-8") as stream:
            for number, activity in ((2, "run the merge gate"), (3, "relaunch the repair after a launcher fault")):
                started = 1_200 + number * 20
                event: dict[str, object] = {
                    "event_type": "pass_started", "phase_instance_id": "instance-a",
                    "pass_instance_id": f"unledgered-{number}", "pass_kind": "fix",
                    "fix_pass": number, "pass_activity": activity,
                    "pass_started_at": started, "timestamp_epoch": started,
                }
                _ = stream.write(json.dumps(event) + "\n")
                _ = stream.write(json.dumps({
                    **event, "event_type": "pass_finished", "status": "completed",
                    "timestamp_epoch": started + 10, "pass_elapsed_seconds": 10,
                }) + "\n")
        payload = self.gate(at=1_500)
        self.assertEqual(payload["repair_round"], 2)
        self.assertEqual(payload["astra_review_due"], [])
        self.assertEqual(self.run_command("dispatch", "--covers", "F001", at=1_500), "round 2 covering F001")
        dispatched = [event for event in self.events() if event["event_type"] == "finding_batch_dispatched"][-1]
        self.assertEqual(dispatched["repair_round"], 2)
        self.assertEqual(dispatched["round"], 2)

    def test_distinct_ledger_batches_still_reach_the_consecutive_fix_advisory(self) -> None:
        self.write_progress_state("instance-a")
        self.write_pass_events("instance-a", [("fix", "repair", "completed")] * 3)
        path = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        records = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        for number in range(3):
            for record in records[number * 2:number * 2 + 2]:
                record["launch_check"] = {"fix_cause": {"round": number + 1}}
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in records))
        _ = self.open_finding("blocker", "retry fails")
        payload = self.gate()
        self.assertEqual(payload["consecutive_same_kind"], {"kind": "fix", "count": 3})
        self.assertEqual(payload["advisory"], "3 consecutive fix passes ran without the phase advancing")

    def test_refunded_dispatches_do_not_reach_the_runaway_advisory(self) -> None:
        self.write_config("RUNAWAY_ROUNDS=2\n")
        _ = self.open_finding("blocker", "retry fails")
        for _ in range(2):
            _ = self.run_command("dispatch", "--covers", "F001")
            _ = self.run_command("abandon", "--reason", "assign_gate refused the launch")
        payload = self.gate()
        self.assertEqual(payload["repair_round"], 1)
        self.assertEqual(payload["repair_budget"], 0)
        self.assertIsNone(payload["advisory"])

    def test_repair_budget_uses_the_first_batch_that_was_not_refunded(self) -> None:
        for _ in range(8):
            _ = self.open_finding("blocker", "initial report")
        covers = [f"F{index:03d}" for index in range(1, 9)]
        _ = self.run_command("dispatch", "--covers", ",".join(covers))
        _ = self.run_command("abandon", "--reason", "assign_gate refused the launch")
        for finding_id in covers[1:]:
            _ = self.route_finding(finding_id)
        self.close_round(["F001"], verdict="still_open")
        self.assertEqual(self.gate()["repair_budget"], 3)

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
            "finding_id": "F002", "reason": "spec-gap", "owner": "receiving",
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

    def test_no_history_does_not_count_a_ledger_dispatch_for_routing(self) -> None:
        self.write_config("ROUTING_FROM_ROUND=2\n")
        _ = self.open_finding("blocker", "first")
        self.close_round(["F001"])
        (self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl").unlink()
        _ = self.open_finding("blocker", "second")
        payload = self.gate()
        self.assertEqual((payload["round"], payload["repair_round"]), (2, 1))
        self.assertEqual(payload["astra_review_due"], [])
        dispatched = self.run_failing_command("dispatch", "--covers", "F002")
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)

    def test_astra_is_due_at_each_new_failed_attempt_and_clears_with_a_record(self) -> None:
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
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
            rows = self.finding_rows()
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
        self.history_file = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
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
        self.write_repair_intervals(2)
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

    def test_close_route_closes_the_listed_key_from_before_a_phase_record(self) -> None:
        self.write_progress_state("")
        finding_id = self.open_finding("blocker", "missing owner rule")
        _ = self.route_finding(finding_id)
        origin_run = self.history_file.stem
        records: list[tuple[str, dict[str, object]]] = [(origin_run, event) for event in self.events()]
        outgoing, = routed_work(records, now=1_000)
        self.assertEqual(outgoing["key"], f"{origin_run}::{finding_id}")
        self.assertEqual(outgoing["phase_instance_id"], "")
        self.assertEqual(outgoing["status"], "open")
        self.assertEqual(outgoing["closure"], {})

        self.history_file: Path = self.history_file.with_name("receiving-run.jsonl")
        self.write_progress_state("receiving-instance", "6")
        evidence = "abc123; owner tests pass; review accepted"
        _ = self.run_command("close-route", "--from", outgoing["key"], "--evidence", evidence)
        records.extend((self.history_file.stem, event) for event in self.events())

        rows = routed_work(records, now=1_000)
        closed, = rows
        self.assertEqual(closed["key"], outgoing["key"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closure"]["from"], outgoing["key"])
        self.assertEqual(closed["closure"]["evidence"], evidence)
        self.assertEqual(closed["closure"]["run"], "receiving-run")
        self.assertEqual([row for row in rows if row["status"] == "open"], [])

    def test_close_route_refuses_missing_ids_and_wrong_part_counts(self) -> None:
        self.write_progress_state("receiving-instance", "6")
        for source in (
            "origin-run:origin-instance:", ":origin-instance:F001",
            "origin-run::", "::F001", " :origin-instance:F001", "origin-run:origin-instance: ",
            "origin-run", "origin-run:F001", "origin-run::F001:extra",
        ):
            with self.subTest(source=source):
                denied = self.run_failing_command("close-route", "--from", source,
                                                  "--evidence", "abc123; review accepted")
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("--from must be", denied.stderr)
                self.assertIn("<run id>::<finding id>", denied.stderr)
                self.assertEqual(self.events(), [])

    def test_a_run_without_progress_state_records_no_events(self) -> None:
        _ = self.open_finding("blocker", "null deref")
        payload = self.gate()
        self.assertEqual(payload["verdict"], "dispatch")
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    _ = unittest.main()
