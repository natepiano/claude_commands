"""Read saved guild evidence from isolated run folders after live holds have ended."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast, override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.guilds import astra_work  # noqa: E402

SCRIPT = Path(__file__).with_name("daily_read.py")
SINCE = "2026-10-21T00:00:00+00:00"
UNTIL = "2026-10-22T00:00:00+00:00"
NOON = datetime(2026, 10, 21, 12, tzinfo=UTC).timestamp()


class DailyReadTests(unittest.TestCase):
    root: Path = Path()
    runs: Path = Path()
    history: Path = Path()
    reviews: Path = Path()
    roles: Path = Path()
    productions: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.runs = self.root / "unit"
        self.history = self.root / "history"
        self.reviews = self.root / "astra"
        self.roles = self.root / "roles"
        self.productions = self.root / "productions"
        for folder in (self.runs, self.history, self.reviews, self.roles, self.productions):
            folder.mkdir()
        _ = self.write(self.root / "design.md", "# Fixture design\n")
        self.environment = {
            **os.environ, "HOME": str(self.root / "home"), "USER_ZONE": "America/New_York",
            "GUILD_DAILY_RUN_ROOTS": str(self.runs), "GUILD_DAILY_HISTORY_ROOT": str(self.history / "runs"),
            "GUILD_DAILY_ASTRA_ROOT": str(self.reviews), "GUILD_DAILY_ROLES_ROOT": str(self.roles),
            "SHOWRUNNER_STATE_DIR": str(self.productions), "GUILD_DAILY_HOST": "natedev",
            "ASTRA_ROLES_STATE": str(self.root / "ended-holds"),
        }

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return path

    def save_events(self, path: Path, events: list[dict[str, object]]) -> None:
        _ = self.write(path, "".join(json.dumps(event) + "\n" for event in events))

    def event(self, kind: str, offset: float, *, run_id: str = "run-one", instance: str = "phase-one",
              phase: str = "1", event_id: str = "", **fields: object) -> dict[str, object]:
        return {
            "spine_version": 1, "schema_version": 1, "event_type": kind, "skill": "plan-delegate",
            "event_id": event_id or f"{run_id}-{instance}-{kind}-{offset}-"
            + str(fields.get("pass_instance_id", fields.get("activity_instance_id", ""))),
            "timestamp_epoch": NOON + offset,
            "timestamp": datetime.fromtimestamp(NOON + offset, UTC).isoformat(),
            "run_id": run_id, "phase_id": phase, "phase_instance_id": instance,
            "main_agent": {"family": "claude", "session_id": "fixture-director"},
            **fields,
        }

    def phase_events(self, *, run_id: str = "run-one", instance: str = "phase-one",
                     start: float = 0, duration: float = 120, rounds: int = 0) -> list[dict[str, object]]:
        events = [self.event("run_started", start - 1, run_id=run_id, instance=instance),
                  self.event("phase_started", start, run_id=run_id, instance=instance),
                  self.event("phase_finished", start + duration, run_id=run_id, instance=instance,
                             status="completed")]
        for number in range(1, rounds + 1):
            events.append(self.event("finding_batch_dispatched", start + number, run_id=run_id,
                                     instance=instance, round=number, covered=["F001"]))
        return events

    def save_run(self, events: list[dict[str, object]], *, run_id: str = "run-one",
                 instance: str = "phase-one") -> Path:
        directory = self.runs / run_id
        history = self.history / "runs" / (run_id + ".jsonl")
        self.save_events(history, events)
        _ = self.write(directory / "director", "fixture-director\n")
        _ = self.write(directory / "progress_history_state.json", json.dumps({
            "run_id": run_id, "history_file": str(history),
            "working_dir": str(self.root / "checkout"), "branch": "fixture",
            "main_agent": {"family": "claude", "session_id": "fixture-director"},
            "phase": {"id": "1", "instance_id": instance, "status": "completed"}, "open_phases": [],
        }))
        return directory

    def assignment(self, role: Literal["astra-designer", "astra-reviewer"], directory: Path, *,
                   member: str = "fixture-designer", instance: str = "phase-one", round_number: int = 1,
                   standalone: bool = False, offset: float = 10, durable: bool = False,
                   reply: bool = True) -> Path:
        if durable:
            path = self.roles / "design-one/prompts" / f"round{round_number}.md"
            reply_path = path.with_name(f"round{round_number}_reply.md")
        elif role == "astra-designer":
            path = directory / "design/phase1" / f"round{round_number}_brief.md"
            reply_path = path.with_name(f"round{round_number}_reply.md")
        else:
            path = self.reviews / member / "prompt.md"
            reply_path = path.with_name("review.md")
            member = str(path.parent)
        work = astra_work.AstraWork(
            role=role, member=member, document=str(self.root / "design.md"),
            assigned_at_utc=datetime.fromtimestamp(NOON + offset, UTC).isoformat(),
            reply_path=str(reply_path),
            work=astra_work.StandaloneWork() if standalone else astra_work.PhaseWork(
                str(directory), directory.name, "1", instance,
                str(self.history / "runs" / (directory.name + ".jsonl"))),
        )
        _ = self.write(path, astra_work.header_line(work) + "\nSaved assignment.\n")
        if reply:
            _ = self.write(reply_path, "Saved output.\n")
        return path

    def invoke(self, guild: str, *, host: str = "natedev", as_json: bool = True) -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--guild", guild, "--since", SINCE, "--until", UNTIL,
             *(["--json"] if as_json else [])],
            env={**self.environment, "GUILD_DAILY_HOST": host}, text=True, capture_output=True,
            check=False, timeout=20,
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result.stdout

    def mapping(self, value: object) -> dict[str, object]:
        self.assertIsInstance(value, dict)
        return cast(dict[str, object], value)

    def report(self, guild: str, *, host: str = "natedev") -> dict[str, object]:
        return self.mapping(cast(object, json.loads(self.invoke(guild, host=host))))

    def rows(self, report: dict[str, object], name: str) -> list[dict[str, object]]:
        value = report[name]
        self.assertIsInstance(value, list)
        return [self.mapping(item) for item in cast(list[object], value)]

    def only_phase(self, report: dict[str, object]) -> dict[str, object]:
        phases = self.rows(report, "phases")
        self.assertEqual(len(phases), 1, phases)
        return phases[0]

    def step(self, phase: dict[str, object], label: str) -> dict[str, object]:
        matches = [row for row in self.rows(phase, "steps") if row["step"] == label]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def test_saved_design_and_review_assignments_join_phase_evidence_after_holds_end(self) -> None:
        events = self.phase_events(rounds=4)
        events.extend([
            self.event("finding_opened", 4, event_id="first-title", finding_id="F001",
                       title="Check input bounds", round=0),
            self.event("finding_opened", 5, event_id="second-title", finding_id="F002",
                       title="  CHECK   input bounds  ", round=1),
            self.event("finding_opened", 6, event_id="second-title-other-lens", finding_id="F003",
                       title="check input bounds", round=1, lens="style"),
        ])
        directory = self.save_run(events)
        brief = self.assignment("astra-designer", directory)
        _ = self.assignment("astra-designer", directory, round_number=2)
        prompt = self.assignment("astra-reviewer", directory, member="saved-review")
        for guild, source, member in (("designers", brief, "fixture-designer"),
                                      ("reviewers", prompt, str(prompt.parent))):
            with self.subTest(guild=guild):
                report = self.report(guild)
                phase = self.only_phase(report)
                self.assertEqual((phase["run_id"], phase["phase_instance_id"]), ("run-one", "phase-one"))
                self.assertEqual(phase["repair_rounds"], 4)
                self.assertEqual(self.mapping(report["counts"])["repair_rounds"], 4)
                self.assertEqual(phase["members"], [member])
                assignments = self.rows(report, "assignments")
                self.assertIn(str(source), [row["source"] for row in assignments])
                self.assertEqual(len(assignments), 2 if guild == "designers" else 1)
                self.assertTrue(all(row["outcome"] == "OutputRecorded" for row in assignments))
                repeats = [row for row in self.rows(report, "repeats") if row["state"] == "RepeatedTitle"]
                self.assertEqual(len(repeats), 1)
                self.assertEqual(set(cast(list[str], repeats[0]["event_ids"])), {"first-title", "second-title"})
                self.assertIn("RepairRoundsBeyondThree", cast(list[str], phase["flags"]))
                self.assertEqual(self.rows(report, "problems"), [])

    def test_standalone_review_has_a_member_and_output_but_no_phase_totals(self) -> None:
        directory = self.save_run(self.phase_events(rounds=4))
        prompt = self.assignment("astra-reviewer", directory, member="standalone-review", standalone=True)
        report = self.report("reviewers")
        assignments = self.rows(report, "assignments")
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["member"], str(prompt.parent))
        self.assertEqual(assignments[0]["work"], {"kind": "standalone"})
        self.assertEqual(assignments[0]["outcome"], "OutputRecorded")
        self.assertEqual(self.rows(report, "phases"), [])
        self.assertEqual(self.mapping(report["counts"])["repair_rounds"], 0)

    def test_durable_prompt_headers_are_discovered_without_live_role_records(self) -> None:
        directory = self.save_run(self.phase_events(rounds=4))
        path = self.assignment("astra-designer", directory, durable=True, member="durable-designer")
        report = self.report("designers")
        self.assertEqual(self.only_phase(report)["members"], ["durable-designer"])
        self.assertEqual(self.only_phase(report)["repair_rounds"], 4)
        self.assertEqual(self.rows(report, "assignments")[0]["source"], str(path))

    def test_roles_state_override_supplies_saved_assignments_without_daily_override(self) -> None:
        directory = self.save_run(self.phase_events())
        prompt = self.assignment("astra-designer", directory, durable=True, member="held-designer")
        del self.environment["GUILD_DAILY_ROLES_ROOT"]
        self.environment["GUILD_ROLES_STATE"] = str(self.roles)
        report = self.report("designers")
        self.assertEqual([row["member"] for row in self.rows(report, "assignments")], ["held-designer"])
        self.assertEqual(self.rows(report, "assignments")[0]["source"], str(prompt))
        self.assertEqual(self.rows(report, "assignments")[0]["outcome"], "OutputRecorded")

    def test_durable_header_requires_absolute_document_and_phase_run_directory(self) -> None:
        directory = self.save_run(self.phase_events())
        prompt = self.assignment("astra-designer", directory, durable=True)
        original = prompt.read_text()
        for absolute, relative in ((str(self.root / "design.md"), "design.md"),
                                   (str(directory), "relative-run")):
            with self.subTest(path=relative):
                _ = prompt.write_text(original.replace(absolute, relative))
                report = self.report("designers")
                self.assertEqual(self.rows(report, "assignments"), [])
                self.assertIn(("HeaderInvalid", str(prompt)),
                              [(row["state"], row["source"]) for row in self.rows(report, "problems")])

    def test_old_headerless_review_prompt_is_skipped_without_a_file_problem(self) -> None:
        directory = self.save_run(self.phase_events())
        _ = self.assignment("astra-reviewer", directory, member="current-review")
        old = self.write(self.reviews / "old-review/prompt.md", "# Legacy review\n")
        old_stamp = datetime.fromisoformat(SINCE).timestamp() - 1
        os.utime(old, (old_stamp, old_stamp))
        report = self.report("reviewers")
        self.assertNotIn(str(old), json.dumps(report))
        self.assertEqual(len(self.rows(report, "assignments")), 1)
        self.assertEqual(self.rows(report, "problems"), [])

    def test_current_headerless_review_prompt_names_missing_membership(self) -> None:
        current = self.write(self.reviews / "current-review/prompt.md", "# Legacy review\n")
        os.utime(current, (NOON, NOON))
        report = self.report("reviewers")
        self.assertIn(("MembershipNotRecorded", str(current)),
                      [(row["state"], row["source"]) for row in self.rows(report, "problems")])
        self.assertEqual(self.rows(report, "assignments"), [])
        self.assertEqual(self.mapping(report["coverage"])["partial"], True)

    def test_invalid_and_legacy_headers_are_problems_without_invented_membership(self) -> None:
        directory = self.save_run(self.phase_events())
        legacy = self.write(directory / "design/phase1/round1_brief.md", "# Old brief\n")
        invalid = self.write(directory / "design/phase1/round2_brief.md", "<!-- astra_work {broken} -->\n")
        report = self.report("designers")
        problems = self.rows(report, "problems")
        self.assertIn(("MembershipNotRecorded", str(legacy)), [(row["state"], row["source"]) for row in problems])
        self.assertIn(("HeaderInvalid", str(invalid)), [(row["state"], row["source"]) for row in problems])
        self.assertTrue(all(row["reason"] for row in problems))
        self.assertEqual(self.rows(report, "assignments"), [])
        self.assertEqual(self.rows(report, "phases"), [])

    def test_missing_reply_and_removed_history_keep_unknown_and_partial_coverage(self) -> None:
        directory = self.save_run(self.phase_events(rounds=4))
        path = self.assignment("astra-designer", directory, reply=False)
        history = self.history / "runs/run-one.jsonl"
        history.unlink()
        report = self.report("designers")
        assignments = self.rows(report, "assignments")
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["member"], "fixture-designer")
        self.assertEqual(assignments[0]["outcome"], "OutcomeUnknown")
        problems = json.dumps(report["problems"])
        self.assertIn(str(path.with_name("round1_reply.md")), problems)
        self.assertIn(str(history), problems)
        self.assertEqual(self.mapping(report["coverage"])["partial"], True)
        self.assertEqual(self.rows(report, "phases"), [])

    def test_assignment_interval_does_not_include_an_old_member(self) -> None:
        directory = self.save_run(self.phase_events())
        old = self.save_run(self.phase_events(run_id="old-run", start=-86400), run_id="old-run")
        _ = self.assignment("astra-designer", old, member="old-designer", offset=-86400)
        _ = self.assignment("astra-designer", directory, member="today-designer", round_number=2)
        report = self.report("designers")
        self.assertEqual([row["member"] for row in self.rows(report, "assignments")], ["today-designer"])
        self.assertEqual(self.only_phase(report)["members"], ["today-designer"])

    def test_duplicate_review_lenses_in_one_round_do_not_prove_a_repeat(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("finding_opened", 3, event_id="lens-code", finding_id="F001",
                       title="Check input bounds", round=0, lens="code"),
            self.event("finding_opened", 4, event_id="lens-style", finding_id="F002",
                       title="check  INPUT bounds", round=0, lens="style"),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        report = self.report("designers")
        self.assertEqual(self.rows(report, "repeats"), [])
        self.assertEqual(self.mapping(report["counts"])["repeated_titles"], 0)
        self.assertEqual(self.only_phase(report)["phase_instance_id"], "phase-one")

    def test_interval_end_excludes_later_repairs_findings_and_assignments(self) -> None:
        events = self.phase_events(rounds=1)
        events.extend([
            self.event("finding_opened", 5, event_id="current-title", finding_id="F001",
                       title="Retain context", round=0),
            self.event("pass_started", 10, pass_kind="review", pass_instance_id="current-review"),
            self.event("pass_finished", 20, pass_kind="review", pass_instance_id="current-review"),
        ])
        boundary = datetime.fromisoformat(UNTIL).timestamp() - NOON
        for offset in (boundary, boundary + 1):
            events.extend([
                self.event("finding_batch_dispatched", offset, round=4, covered=["F001"]),
                self.event("finding_opened", offset, finding_id="F002", title="Retain context", round=4),
                self.event("pass_started", offset, pass_kind="fix", pass_instance_id=f"future-{offset}",
                           fix_pass=4, pass_activity="Repair phase 9 tests"),
                self.event("pass_finished", offset + 10, pass_kind="fix", pass_instance_id=f"future-{offset}",
                           fix_pass=4, pass_activity="Repair phase 9 tests"),
            ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory, member="current-member")
        _ = self.assignment("astra-designer", directory, member="next-member", round_number=2, offset=boundary)
        report = self.report("designers")
        phase = self.only_phase(report)
        self.assertEqual(phase["repair_rounds"], 1)
        self.assertEqual(phase["mixed"], False)
        self.assertEqual(self.step(phase, "pass: review")["seconds"], 10)
        self.assertEqual(self.rows(report, "repeats"), [])
        self.assertEqual(phase["members"], ["current-member"])
        self.assertEqual([row["member"] for row in self.rows(report, "assignments")], ["current-member"])

    def test_post_repair_verdict_joins_archived_title_and_cites_both_events(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("finding_batch_landed", 20, event_id="repair-landed", round=1, landed=["F007"]),
            self.event("finding_verdict", 30, event_id="repair-still-open", round=1,
                       finding_id="F007", verdict="still_open"),
            self.event("finding_verdict", 31, event_id="same-verdict-again", round=1,
                       finding_id="F007", verdict="still_open"),
        ])
        directory = self.save_run(events)
        archive = self.write(directory / "findings_state-phase-one.json", json.dumps({
            "phase_id": "1", "phase_instance_id": "phase-one",
            "findings": {"F007": {"id": "F007", "title": "Retain the caller context", "opened_round": 0}},
            "rounds": [{"round": 1, "outcome": "landed", "landed_at": NOON + 20}],
        }))
        _ = self.write(directory / "findings_state.json", json.dumps({
            "phase_id": "2", "phase_instance_id": "another-instance",
            "findings": {"F007": {"id": "F007", "title": "A different phase title"}},
        }))
        _ = self.assignment("astra-designer", directory)
        report = self.report("designers")
        repeats = [row for row in self.rows(report, "repeats") if row["state"] == "RepeatedTitle"]
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["title"], "Retain the caller context")
        self.assertEqual(set(cast(list[str], repeats[0]["event_ids"])), {"repair-landed", "repair-still-open"})
        self.assertIn(str(archive), cast(list[str], repeats[0]["sources"]))

    def test_reopened_verdict_needs_a_landed_batch_naming_that_finding(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("finding_opened", 5, event_id="opened-context", finding_id="F001",
                       title="Preserve context", round=0),
            self.event("finding_batch_landed", 20, event_id="unrelated-repair", round=1, landed=["F002"]),
            self.event("finding_verdict", 30, event_id="reopened-context", round=1,
                       finding_id="F001", verdict="reopened"),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        unproven = self.rows(self.report("designers"), "repeats")
        self.assertEqual([row["state"] for row in unproven], ["RepeatNotProven"])
        events[-2]["landed"] = ["F001"]
        self.save_events(self.history / "runs/run-one.jsonl", events)
        proven = self.rows(self.report("designers"), "repeats")
        self.assertEqual([row["state"] for row in proven], ["RepeatedTitle"])
        self.assertEqual(set(cast(list[str], proven[0]["event_ids"])), {"unrelated-repair", "reopened-context"})

    def test_verdict_after_a_later_round_cites_the_earlier_repair(self) -> None:
        for verdict in ("reopened", "still_open"):
            with self.subTest(verdict=verdict):
                events = self.phase_events(rounds=2)
                events.extend([
                    self.event("finding_opened", 5, event_id="original-finding", finding_id="F001",
                               title="Preserve caller context", round=0),
                    self.event("finding_batch_landed", 20, event_id="first-repair", round=1, landed=["F001"]),
                    self.event("finding_batch_landed", 30, event_id="other-repair", round=2, landed=["F002"]),
                    self.event("finding_verdict", 40, event_id="later-verdict", round=2,
                               finding_id="F001", verdict=verdict),
                    self.event("finding_verdict", 41, event_id="duplicate-verdict", round=2,
                               finding_id="F001", verdict=verdict),
                ])
                directory = self.save_run(events)
                _ = self.assignment("astra-designer", directory)
                repeats = self.rows(self.report("designers"), "repeats")
                self.assertEqual(len(repeats), 1)
                self.assertEqual(repeats[0]["state"], "RepeatedTitle")
                self.assertEqual(repeats[0]["title"], "Preserve caller context")
                self.assertEqual(set(cast(list[str], repeats[0]["event_ids"])), {"first-repair", "later-verdict"})

    def test_missing_round_and_verdict_before_repair_leave_repeat_unproven(self) -> None:
        events = self.phase_events()
        opened = self.event("finding_opened", 5, finding_id="F001", title="Unknown repair order")
        events.extend([
            opened, self.event("finding_verdict", 6, finding_id="F001", verdict="reopened", round=1),
            self.event("finding_batch_landed", 20, landed=["F001"], round=1),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        report = self.report("designers")
        repeats = self.rows(report, "repeats")
        self.assertTrue(repeats)
        self.assertEqual({row["state"] for row in repeats}, {"RepeatNotProven"})
        self.assertEqual(self.mapping(report["counts"])["repeated_titles"], 0)

    def test_ambiguous_phase_instance_does_not_assign_ledger_rounds_to_either_run(self) -> None:
        first = self.save_run(self.phase_events(instance="shared-instance"), instance="shared-instance")
        _ = self.save_run(self.phase_events(run_id="run-two", instance="shared-instance"),
                          run_id="run-two", instance="shared-instance")
        _ = self.assignment("astra-designer", first, instance="shared-instance")
        ledger = [self.event("finding_batch_dispatched", number, instance="shared-instance",
                             round=number, covered=["F001"]) for number in range(1, 5)]
        for event in ledger:
            del event["run_id"]
        self.save_events(self.history / "runs/ambiguous.jsonl", ledger)
        report = self.report("designers")
        self.assertEqual(self.only_phase(report)["repair_rounds"], 0)
        self.assertIn("PhaseUnassigned", [row["state"] for row in self.rows(report, "problems")])
        self.assertEqual(self.mapping(report["counts"])["repair_rounds"], 0)

    def test_mixed_phase_activity_is_flagged_with_its_history_source(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("pass_started", 1, pass_instance_id="wrong-phase", pass_kind="fix", fix_pass=1,
                       pass_activity="Repair phase 9 tests", pass_started_at=NOON + 1),
            self.event("pass_finished", 10, pass_instance_id="wrong-phase", pass_kind="fix", fix_pass=1,
                       pass_activity="Repair phase 9 tests", pass_started_at=NOON + 1, pass_elapsed_seconds=9),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        report = self.report("designers")
        phase = self.only_phase(report)
        self.assertEqual(phase["mixed"], True)
        self.assertIn("MixedPhaseRecord", cast(list[str], phase["flags"]))
        self.assertTrue(any(str(source).startswith(str(self.history / "runs/run-one.jsonl") + ":")
                            for source in cast(list[str], phase["sources"])))
        self.assertEqual(phase["phase_instance_id"], "phase-one")

    def test_parallel_steps_share_wall_time_and_unmatched_steps_are_unknown(self) -> None:
        events = self.phase_events(duration=120)
        for label, pass_id in (("impl", "implementation"), ("review", "review")):
            events.extend([
                self.event("pass_started", 0, pass_kind=label, pass_instance_id=pass_id),
                self.event("pass_finished", 100, pass_kind=label, pass_instance_id=pass_id),
            ])
        events.extend([
            self.event("activity_started", 100, activity_label="inspect", activity_instance_id="unclosed"),
            self.event("pass_started", 100, pass_kind="fix", pass_instance_id="fix-only", fix_pass=1),
            self.event("pass_finished", 120, pass_kind="fix", pass_instance_id="fix-only", fix_pass=1),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        phase = self.only_phase(self.report("designers"))
        self.assertEqual(self.step(phase, "pass: implementation")["seconds"], 50)
        self.assertEqual(self.step(phase, "pass: review")["seconds"], 50)
        steps = self.rows(phase, "steps")
        self.assertNotIn("fix", [row["step"] for row in steps])
        self.assertNotIn("activity: inspect", [row["step"] for row in steps])
        # Give the unfinished activity its own wall interval without a fix owner.
        without_fix = [event for event in events if event.get("pass_instance_id") != "fix-only"]
        self.save_events(self.history / "runs/run-one.jsonl", without_fix)
        phase = self.only_phase(self.report("designers"))
        unknown = [row for row in self.rows(phase, "steps") if row["coverage"] == "CoverageUnknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["seconds"], 20)
        self.assertEqual(unknown[0]["speed_verdict"], "CoverageUnknown")

    def baseline_events(self, samples: int) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        previous_noon = (datetime.fromtimestamp(NOON, UTC) - timedelta(days=2)).timestamp()
        for number in range(1, samples + 1):
            run_id, instance = f"baseline-{number}", f"baseline-phase-{number}"
            start = previous_noon - NOON + number * 100
            events.extend(self.phase_events(run_id=run_id, instance=instance, start=start, duration=number))
            events.extend([
                self.event("pass_started", start, run_id=run_id, instance=instance,
                           pass_kind="review", pass_instance_id=f"review-{number}"),
                self.event("pass_finished", start + number, run_id=run_id, instance=instance,
                           pass_kind="review", pass_instance_id=f"review-{number}"),
            ])
        return events

    def test_sparse_baseline_has_no_speed_verdict_until_twenty_complete_day_samples(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("pass_started", 0, pass_kind="review", pass_instance_id="current-review"),
            self.event("pass_finished", 120, pass_kind="review", pass_instance_id="current-review"),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        baseline = self.history / "runs/baseline.jsonl"
        self.save_events(baseline, self.baseline_events(19))
        sparse = self.only_phase(self.report("designers"))
        sparse_step = self.step(sparse, "pass: review")
        self.assertEqual(sparse_step["sample_count"], 19)
        self.assertEqual(sparse_step["speed_verdict"], "InsufficientSamples")
        self.assertNotIn("SlowStep", cast(list[str], sparse["flags"]))

        self.save_events(baseline, self.baseline_events(20))
        full = self.only_phase(self.report("designers"))
        full_step = self.step(full, "pass: review")
        self.assertEqual(full_step["sample_count"], 20)
        self.assertEqual(full_step["threshold_seconds"], 18)
        self.assertEqual(full_step["speed_verdict"], "Slow")
        self.assertIn("SlowStep", cast(list[str], full["flags"]))
        self.assertEqual(full["phase_instance_id"], "phase-one")

    def test_speed_baseline_excludes_today_and_records_before_fourteen_complete_days(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("pass_started", 0, pass_kind="review", pass_instance_id="current-review"),
            self.event("pass_finished", 120, pass_kind="review", pass_instance_id="current-review"),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        baseline = self.baseline_events(19)
        for run_id, start in (("today-sample", -3600), ("expired-sample", -16 * 86400)):
            baseline.extend(self.phase_events(run_id=run_id, instance=run_id, start=start, duration=1))
            baseline.extend([
                self.event("pass_started", start, run_id=run_id, instance=run_id,
                           pass_kind="review", pass_instance_id=run_id),
                self.event("pass_finished", start + 1, run_id=run_id, instance=run_id,
                           pass_kind="review", pass_instance_id=run_id),
            ])
        self.save_events(self.history / "runs/baseline.jsonl", baseline)
        phase = self.only_phase(self.report("designers"))
        self.assertEqual(self.step(phase, "pass: review")["sample_count"], 19)
        self.assertEqual(self.step(phase, "pass: review")["speed_verdict"], "InsufficientSamples")
        self.assertNotIn("SlowStep", cast(list[str], phase["flags"]))

    def test_absent_hierarchy_writers_name_missing_waits_and_receipts(self) -> None:
        _ = self.save_run(self.phase_events())
        stray_events = self.phase_events(run_id="stray-production", instance="stray-phase", rounds=4)
        stray = self.productions / "outstanding/stray.history.jsonl"
        self.save_events(stray, stray_events)
        self.save_events(self.productions / "waits.jsonl", [self.event(
            "wait", 0, started_at_utc="2026-10-19T00:00:00+00:00", owner="fixture-head")])
        self.save_events(self.productions / "merge_receipts.jsonl", [self.event(
            "merge_receipt", 10, project="fixture", scope="units", commit="abc123", branch="main",
            merged_at_utc="2026-10-21T12:00:10+00:00")])
        self.save_events(self.productions / "merge_claims.jsonl", [self.event(
            "merge_claim", 20, project="fixture", scope="designers", commit="different", branch="main")])
        for guild in ("units", "showrunners"):
            with self.subTest(guild=guild):
                report = self.report(guild)
                waits, receipts = self.rows(report, "waits"), self.rows(report, "receipts")
                self.assertEqual([row["state"] for row in waits], ["HistoryNotRecorded"])
                self.assertEqual([row["state"] for row in receipts], ["ReceiptMissing"])
                for row in (*waits, *receipts):
                    self.assertIn("writer", str(row["reason"]).casefold())
                self.assertEqual(self.rows(report, "flagged"), [])
                self.assertNotIn("stray-production", [row["run_id"] for row in self.rows(report, "phases")])
                self.assertNotIn(str(stray), json.dumps(report))
                self.assertNotIn("null", json.dumps(report))

    def test_showrunner_history_has_no_writer_and_does_not_use_unit_phases(self) -> None:
        _ = self.save_run(self.phase_events(rounds=4))
        report = self.report("showrunners")
        self.assertEqual(self.rows(report, "phases"), [])
        self.assertEqual(self.rows(report, "repeats"), [])
        problems = self.rows(report, "problems")
        self.assertTrue(any(row["state"] == "HistoryNotRecorded"
                            and row["source"] == "WriterNotRecorded" for row in problems))
        self.assertTrue(any(row["state"] == "MembershipNotRecorded" for row in problems))
        self.assertEqual(self.mapping(report["coverage"])["partial"], True)

    def test_units_report_excludes_repeats_from_showrunner_runs(self) -> None:
        events = self.phase_events(run_id="production-run", instance="production-phase")
        events.extend([
            self.event("finding_opened", 5, run_id="production-run", instance="production-phase",
                       event_id="production-first", finding_id="F001", title="Retain evidence", round=0),
            self.event("finding_opened", 10, run_id="production-run", instance="production-phase",
                       event_id="production-repeat", finding_id="F002", title="Retain evidence", round=1),
        ])
        for event in events:
            event["skill"] = "unrelated-production"
        _ = self.save_run(events, run_id="production-run", instance="production-phase")
        units = self.report("units")
        self.assertEqual(self.rows(units, "phases"), [])
        self.assertEqual(self.rows(units, "repeats"), [])
        self.assertEqual(self.rows(units, "flagged"), [])
        self.assertEqual(self.mapping(units["counts"])["repeated_titles"], 0)

    def test_invalid_utf8_ledger_keeps_healthy_phase_and_partial_coverage(self) -> None:
        events = self.phase_events()
        events.extend([
            self.event("pass_started", 10, pass_kind="review", pass_instance_id="healthy-review"),
            self.event("pass_finished", 20, pass_kind="review", pass_instance_id="healthy-review"),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        damaged = self.history / "runs/damaged.jsonl"
        _ = damaged.write_bytes(b"\xff\xfe\n")
        report = self.report("designers")
        phase = self.only_phase(report)
        self.assertEqual(phase["run_id"], "run-one")
        self.assertEqual(self.step(phase, "pass: review")["seconds"], 10)
        self.assertEqual(self.mapping(report["coverage"])["partial"], True)
        problems = [row for row in self.rows(report, "problems") if str(damaged) in str(row["source"])]
        self.assertTrue(problems)
        self.assertTrue(all(row["reason"] for row in problems))

    def test_guild_without_interval_members_reports_missing_membership_and_roots(self) -> None:
        directory = self.save_run(self.phase_events(start=-86400))
        _ = self.assignment("astra-designer", directory, offset=-86400)
        _ = self.assignment("astra-reviewer", directory, member="old-review", offset=-86400)
        for guild in ("designers", "reviewers", "showrunners", "units"):
            with self.subTest(guild=guild):
                report = self.report(guild)
                self.assertEqual(self.mapping(report["coverage"])["members"], 0)
                self.assertEqual(self.mapping(report["coverage"])["partial"], True)
                missing = [row for row in self.rows(report, "problems") if row["state"] == "MembershipNotRecorded"]
                self.assertTrue(missing)
                evidence = json.dumps(missing)
                self.assertIn(guild, evidence)
                self.assertTrue(any(str(root) in evidence for root in (self.runs, self.history, self.reviews, self.roles)))

    def test_unit_history_is_not_assigned_to_astra_without_an_assignment_header(self) -> None:
        directory = self.save_run(self.phase_events(rounds=4))
        _ = self.write(directory / "mesh_roster.json", json.dumps({
            "implementation-worker": {"thread_id": "ordinary-implementation", "status": "ended"},
            "pool-reviewer": {"thread_id": "ordinary-pool-review", "status": "ended"},
        }))
        units = self.only_phase(self.report("units"))
        self.assertEqual(units["members"], ["fixture-director"])
        self.assertEqual(units["run_id"], "run-one")
        self.assertEqual(units["repair_rounds"], 4)
        for guild in ("designers", "reviewers"):
            with self.subTest(guild=guild):
                report = self.report(guild)
                self.assertEqual(self.rows(report, "assignments"), [])
                self.assertEqual(self.rows(report, "phases"), [])
                self.assertEqual(self.mapping(report["counts"])["repair_rounds"], 0)

    def test_repairs_count_starts_in_window_and_ignore_an_earlier_fourth_round(self) -> None:
        boundary = datetime.fromisoformat(SINCE).timestamp() - NOON
        other = self.save_run(self.phase_events(run_id="run-two", instance="phase-two", rounds=1),
                              run_id="run-two", instance="phase-two")
        _ = self.assignment("astra-designer", other, instance="phase-two", member="second-designer")
        for old_rounds, current_offsets in ((1, (boundary, 10)), (4, ())):
            with self.subTest(old_rounds=old_rounds):
                events = self.phase_events(start=boundary - 100, duration=50000)
                for number in range(1, old_rounds + 1):
                    events.append(self.event("finding_batch_dispatched", boundary - 50 + number,
                                             round=number, covered=["F001"]))
                events.extend([
                    self.event("pass_started", boundary - 50 + old_rounds, pass_kind="fix",
                               pass_instance_id="earlier-repair", fix_pass=old_rounds,
                               pass_started_at=NOON + boundary - 50 + old_rounds),
                    self.event("pass_finished", boundary + 5, pass_kind="fix",
                               pass_instance_id="earlier-repair", fix_pass=old_rounds,
                               pass_started_at=NOON + boundary - 50 + old_rounds,
                               pass_elapsed_seconds=55 - old_rounds),
                ])
                for number, offset in enumerate(current_offsets, old_rounds + 1):
                    events.append(self.event("finding_batch_dispatched", offset, round=number, covered=["F002"]))
                directory = self.save_run(events)
                _ = self.assignment("astra-designer", directory)
                report = self.report("designers")
                phase = next(row for row in self.rows(report, "phases") if row["run_id"] == "run-one")
                self.assertEqual(phase["repair_rounds"], len(current_offsets))
                repairs = self.rows(phase, "rounds")
                self.assertEqual([row["started_at"] for row in repairs],
                                 [NOON + offset for offset in current_offsets])
                self.assertEqual([row["round"] for row in repairs],
                                 list(range(old_rounds + 1, old_rounds + len(current_offsets) + 1)))
                self.assertNotIn("RepairRoundsBeyondThree", cast(list[str], phase["flags"]))
                self.assertEqual(self.mapping(report["counts"])["repair_rounds"], len(current_offsets) + 1)

    def test_plain_summary_orders_rows_and_limits_problem_details(self) -> None:
        events = self.phase_events(rounds=4)
        events.extend([
            self.event("finding_opened", 5, finding_id="F001", title="Check input bounds", round=0),
            self.event("finding_opened", 6, finding_id="F002", title="Check input bounds", round=1),
        ])
        directory = self.save_run(events)
        _ = self.assignment("astra-designer", directory)
        other = self.save_run(self.phase_events(run_id="run-two", instance="phase-two"),
                              run_id="run-two", instance="phase-two")
        _ = self.assignment("astra-designer", other, instance="phase-two", member="second-designer")
        for number in range(2, 9):
            _ = self.write(directory / "design/phase1" / f"round{number}_brief.md", "# Old brief\n")
        _ = self.write(directory / "design/phase1/round9_brief.md", "<!-- astra_work {broken} -->\n")
        lines = self.invoke("designers", as_json=False).splitlines()
        self.assertLessEqual(len(lines), 24, lines)
        for line in lines:
            self.assertNotRegex(line, r'[{}]|"[a-z_]+"\s*:', line)
        self.assertIn("natedev", lines[0])
        self.assertIn("designers", lines[0])
        self.assertIn(SINCE, lines[1])
        self.assertIn(UNTIL, lines[1])
        self.assertIn("UTC", lines[1])
        self.assertIn("2026-10-20T20:00:00-04:00", lines[2])
        self.assertIn("2026-10-21T20:00:00-04:00", lines[2])
        self.assertIn("EDT", lines[2])
        for field in ("members 2", "runs 2", "assignments 2", "phases 2", "partial yes"):
            self.assertIn(field, lines[3])
        self.assertTrue(lines[4].startswith("Counts:"), lines)
        for field in ("phases 2", "assignments 2", "repair rounds 4", "repeated titles 1", "flagged 2", "problems 8"):
            self.assertIn(field, lines[4])
        phase_lines = lines[5:7]
        for line, run, member, repairs, flag in zip(
                phase_lines, ("run-one", "run-two"), ("fixture-designer", "second-designer"),
                (4, 0), ("RepairRoundsBeyondThree", "no flags")):
            self.assertIn("Phase 1", line)
            self.assertIn(run, line)
            self.assertIn(member, line)
            self.assertIn(f"repair rounds started in window {repairs}", line)
            self.assertIn(flag, line)
        self.assertTrue(lines[7].startswith("Repeat:"), lines)
        self.assertIn("Check input bounds", lines[7])
        self.assertIn("run-one", lines[7])
        flag_lines = lines[8:10]
        self.assertTrue(all(line.startswith("Flag:") for line in flag_lines), lines)
        self.assertTrue(any("RepairRoundsBeyondThree" in line for line in flag_lines), lines)
        self.assertTrue(any("RepeatedTitle" in line for line in flag_lines), lines)
        self.assertEqual(set(lines[10:12]), {"Problems: HeaderInvalid: 1", "Problems: MembershipNotRecorded: 7"})
        self.assertTrue(all(line.startswith("problem:") for line in lines[12:17]), lines)
        for line in lines[12:17]:
            self.assertRegex(line, r"problem: (HeaderInvalid|MembershipNotRecorded): .+/round[2-9]_brief.md: .+")
        self.assertEqual(lines[17], "and 3 more")
        self.assertEqual(lines[18], "Phase totals are shared context, not an individual's delay. " +
                         "Read turns only for flagged records.")
        self.assertEqual(len(lines), 19)

    def test_json_output_is_one_full_report_without_the_plain_summary(self) -> None:
        directory = self.save_run(self.phase_events(rounds=4))
        prompt = self.assignment("astra-designer", directory)
        output = self.invoke("designers")
        report = self.mapping(cast(object, json.loads(output)))
        self.assertEqual(report["host"], "natedev")
        self.assertEqual(report["guild"], "designers")
        self.assertEqual(self.rows(report, "assignments")[0]["source"], str(prompt))
        self.assertEqual(len(self.rows(self.only_phase(report), "rounds")), 4)
        self.assertEqual(self.rows(report, "flagged")[0]["kind"], "RepairRoundsBeyondThree")
        for section in ("interval", "coverage", "counts", "repeats", "waits", "receipts", "problems"):
            self.assertIn(section, report)

    def test_both_host_reports_use_local_times_and_leave_all_evidence_unchanged(self) -> None:
        directory = self.save_run(self.phase_events())
        _ = self.assignment("astra-designer", directory)
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        for host in ("natedev", "macbook"):
            with self.subTest(host=host):
                report = self.report("designers", host=host)
                self.assertEqual(report["host"], host)
                self.assertEqual(report["guild"], "designers")
                interval = self.mapping(report["interval"])
                self.assertEqual(interval["since"], SINCE)
                self.assertEqual(interval["until"], UNTIL)
                self.assertEqual(interval["local_since"], "2026-10-20T20:00:00-04:00")
                self.assertEqual(interval["local_until"], "2026-10-21T20:00:00-04:00")
                text = self.invoke("designers", host=host, as_json=False)
                self.assertIn(host, text)
                self.assertIn("fixture-designer", text)
                self.assertIn("2026-10-20T20:00:00-04:00", text)
                self.assertIn("partial no", text)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    _ = unittest.main()
