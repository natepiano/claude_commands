"""Keep simplify decisions outside repair rounds and phase closure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.direct.findings_test_support import FindingsLedgerFixture, findings
from scripts.direct.recorder_fixture import invoke_recorder


class SimplifyFindingTests(FindingsLedgerFixture):
    def complete_held_phase(self) -> str:
        self.write_progress_state("simplify-instance", "simplify")
        finding_id = self.open_simplify("Remove retry behavior", held=True)
        path = self.session_dir / "progress_history_state.json"
        progress = cast(dict[str, object], json.loads(path.read_text()))
        cast(dict[str, object], progress["phase"])["status"] = "completed"
        _ = path.write_text(json.dumps(progress))
        return finding_id

    def test_in_phase_simplify_finding_enters_the_first_repair_batch(self) -> None:
        self.write_progress_state("simplify-phase")
        finding_id = self.open_simplify("Remove repeated parsing")

        batch = cast(list[dict[str, object]], self.gate()["batch"])
        self.assertEqual([entry["id"] for entry in batch], [finding_id])
        self.close_round([finding_id])
        finding = self.finding_rows()[0]
        self.assertEqual((finding["lens"], finding["state"]), ("simplify", "accepted"))
        opened = [event for event in self.events() if event["event_type"] == "finding_opened"]
        self.assertEqual(opened[0]["lens"], "simplify")

    def test_held_finding_survives_a_repair_round_and_phase_end(self) -> None:
        environment = self.environment(1_000)
        environment.update({"UNIT_ACTIVE_DIR": str(self.root / "active"),
                            "TZ": "UTC", "USER_ZONE": "UTC"})
        for arguments in (
            ["start-run", "--working-dir", str(self.root),
             "--main-family", "codex", "--main-model", "fixture-model",
             "--main-effort", "high", "--main-session-id", "simplify-fixture"],
            ["start-phase", "--phase-id", "simplify", "--phase-title", "Simplify review"],
        ):
            result = invoke_recorder([*arguments, "--session-dir", str(self.session_dir)],
                                     environment, self.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        held_id = self.open_simplify("Remove retry behavior", held=True)
        second_held_id = self.open_simplify("Change the retry interface", held=True)
        repair_id = self.open_simplify("Remove repeated parsing")

        batch = cast(list[dict[str, object]], self.gate()["batch"])
        self.assertEqual([entry["id"] for entry in batch], [repair_id])
        rejected = self.run_failing_command("dispatch", "--covers", f"{held_id},{repair_id}")
        self.assertNotEqual(rejected.returncode, 0)
        self.close_round([repair_id])
        self.assertEqual(self.gate()["verdict"], "converged")

        result = invoke_recorder(
            ["finish-phase", "--session-dir", str(self.session_dir), "--status", "completed"],
            environment, self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = self.finding_rows()
        held = next(entry for entry in entries if entry["id"] == held_id)
        self.assertEqual(held["state"], "held_for_decision")
        self.assertEqual(held["fix_attempts"], 0)
        self.assertEqual(held["title"], "Remove retry behavior")
        ledger = cast(dict[str, object], json.loads(self.ledger()))
        persisted = cast(dict[str, dict[str, object]], ledger["findings"])[held_id]
        self.assertEqual(persisted["file"], "scripts/direct/assign.py")
        self.assertEqual(persisted["detail"], "The change removes a retry that callers use.")

        result = invoke_recorder(
            ["start-phase", "--session-dir", str(self.session_dir),
             "--phase-id", "later", "--phase-title", "Later phase"], environment, self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        later_id = self.open_simplify("Remove repeated parsing in the later phase")
        later_before = self.status()
        old_status = self.phase_status("simplify")
        held_entries = cast(list[dict[str, object]], old_status["held_for_decision"])
        self.assertEqual([entry["id"] for entry in held_entries], [held_id, second_held_id])

        for finding_id, answer, words in (
            (held_id, "approved", "Create a later phase for this change."),
            (second_held_id, "declined", "Keep the existing retry interface."),
        ):
            _ = self.run_command("decision", "--phase", "simplify", "--id", finding_id,
                                 "--answer", answer, "--user-words", words)
            parked = self.session_dir / f"findings_state-{ledger['phase_instance_id']}.json"
            previous = cast(dict[str, object], json.loads(parked.read_text()))
            entries_by_id = cast(dict[str, dict[str, object]], previous["findings"])
            decided = entries_by_id[finding_id]
            self.assertEqual(decided["state"], "decided")
            self.assertEqual(decided["fix_attempts"], 0)
            decision = cast(dict[str, object], decided["decision"])
            self.assertEqual((decision["answer"], decision["user_words"]), (answer, words))
            self.assertEqual(self.status(), later_before)
        later_batch = cast(list[dict[str, object]], self.gate()["batch"])
        self.assertEqual([entry["id"] for entry in later_batch], [later_id])
        final_status = self.phase_status("simplify")
        self.assertEqual(final_status["held_for_decision"], [])
        self.assertEqual([entry["state"] for entry in cast(list[dict[str, object]], final_status["findings"])],
                         ["decided", "decided", "accepted"])

    def test_finished_phase_refuses_repair_commands_with_or_without_a_later_phase(self) -> None:
        held_id = self.complete_held_phase()
        commands = (
            ("open", "--severity", "minor", "--title", "Late change", "--caught-by", "worker"),
            ("gate",),
            ("dispatch", "--covers", held_id),
            ("route", "--id", held_id, "--reason", "spec-gap", "--owner", "later",
             "--work-order", "docs/plan.md — Phase later — Retry", "--due", "later checkpoint"),
        )
        for parked in (False, True):
            if parked:
                self.write_progress_state("later-instance", "later")
                _ = self.open_simplify("Remove later repeated parsing")
            before = {path: path.read_bytes() for path in self.session_dir.glob("*.json")}
            phase_options = (("--phase", "simplify"),) if parked else ((), ("--phase", "simplify"))
            for command in commands:
                for options in phase_options:
                    with self.subTest(parked=parked, command=command[0], options=options):
                        result = self.run_failing_command(*command, *options)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Phase simplify is not open", result.stderr)
                        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_status_keeps_the_last_decision_without_a_later_phase(self) -> None:
        held_id = self.complete_held_phase()
        _ = self.run_command("decision", "--phase", "simplify", "--id", held_id,
                             "--answer", "declined", "--user-words", "Keep the retry behavior.")
        status = self.phase_status("simplify")
        self.assertEqual(status["held_for_decision"], [])
        self.assertEqual(cast(list[dict[str, object]], status["findings"])[0]["state"], "decided")

    def test_closed_phase_lookup_uses_the_parked_path_rule_and_the_latest_ledger(self) -> None:
        _ = self.complete_held_phase()
        ledger = cast(dict[str, object], json.loads(self.ledger()))
        self.write_progress_state("later-instance", "later")
        _ = self.open_simplify("Remove later repeated parsing")
        for instance, started_at in (("a-newer", 2_000), ("z-older", 500)):
            ledger.update({"phase_instance_id": instance, "started_at": started_at, "findings": {}})
            _ = (self.session_dir / f"archived-{instance}.json").write_text(json.dumps(ledger))

        def archived_path(session_dir: Path, instance_id: str) -> Path:
            return session_dir / f"archived-{instance_id}.json"

        with patch.object(findings, "_parked_path", side_effect=archived_path):
            identity = findings._phase_identity(self.session_dir, "simplify", closed_phase_ok=True)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(identity, ("a-newer", "simplify"))
