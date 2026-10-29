#!/usr/bin/env python3
"""Repair rounds count dispatches, retain phase boundaries, and explain repairs."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import unittest
from datetime import UTC, datetime
from typing import TypedDict, cast, final
from unittest.mock import patch

from scripts.history import history, rounds


BASE = 1_792_454_400.0  # 2026-10-20 00:00 UTC.


class Event(TypedDict, total=False):
    event_type: str
    timestamp_epoch: float
    run_id: str
    phase_id: str
    phase_instance_id: str
    project_plan_doc: str
    phase_title: str
    phase_started_at: float
    status: str
    pass_instance_id: str
    pass_kind: str
    pass_activity: str
    pass_started_at: float
    pass_elapsed_seconds: float
    team_slot: str
    fix_pass: int
    finding_id: str
    lens: str
    severity: str
    round: int
    covered: list[str]
    activity_label: str
    activity_text: str
    activity_instance_id: str
    activity_started_at: float
    activity_elapsed_seconds: float
    activity_result: str


class JsonRow(TypedDict):
    run: str
    run_id: str
    phase: str
    title: str
    plan: str
    rounds: int | str
    check: str
    past_landing: int | str
    round_details: list[rounds.Round]
    causes: dict[str, int]
    findings: int
    fix_seconds: float
    reviewed: bool


class JsonReport(TypedDict):
    rows: list[JsonRow]
    _meta: dict[str, object]


def event(kind: str, at: float, *, run: str = "run-a", instance: str = "instance-a",
          phase: str = "1") -> Event:
    return Event(event_type=kind, timestamp_epoch=at, run_id=run,
                 phase_instance_id=instance, phase_id=phase)


def phase_events(*, run: str = "run-a", instance: str = "instance-a", phase: str = "1",
                 start: float = BASE, finish: float = BASE + 1_000,
                 title: str = "Repair round measurement",
                 plan: str = "/repo/docs/plans/round-study.md",
                 status: str = "completed") -> list[dict[str, object]]:
    started = event("run_started", start - 1, run=run, instance=instance, phase=phase)
    started["project_plan_doc"] = plan
    opened = event("phase_started", start, run=run, instance=instance, phase=phase)
    opened["phase_title"] = title
    opened["phase_started_at"] = start
    closed = event("phase_finished", finish, run=run, instance=instance, phase=phase)
    closed["status"] = status
    return [dict(started), dict(opened), dict(closed)]


def pass_events(start: float, finish: float, *, name: str = "pass-a", slot: str = "impl",
                number: int = 1, activity: str = "Repair behavior", kind: str = "fix",
                run: str = "run-a", instance: str = "instance-a",
                phase: str = "1") -> list[dict[str, object]]:
    opened = event("pass_started", start, run=run, instance=instance, phase=phase)
    opened.update(pass_instance_id=name, pass_kind=kind, pass_activity=activity,
                  pass_started_at=start, team_slot=slot, fix_pass=number)
    closed = opened.copy()
    closed.update(event_type="pass_finished", timestamp_epoch=finish,
                  pass_elapsed_seconds=finish - start, status="completed")
    return [dict(opened), dict(closed)]


def ledger_events(number: int = 1, *, lens: str = "code", finding: str = "F1",
                  instance: str = "instance-a", at: float = BASE + 5) -> list[dict[str, object]]:
    # Ledger records have no run_id or phase_id: phase_started supplies the join.
    opened = Event(event_type="finding_opened", timestamp_epoch=at,
                   phase_instance_id=instance, finding_id=finding,
                   lens=lens, severity="blocker")
    dispatched = Event(event_type="finding_batch_dispatched", timestamp_epoch=at + 1,
                       phase_instance_id=instance, round=number, covered=[finding])
    return [dict(opened), dict(dispatched)]


def live_event(finish: float, *, label: str = "smoke", instance: str = "instance-a") -> dict[str, object]:
    finished = event("activity_finished", finish, instance=instance)
    finished.update(activity_label=label, activity_text=f"Run {label}",
                    activity_instance_id="live-a", activity_started_at=finish - 20,
                    activity_elapsed_seconds=20, activity_result="fail")
    return dict(finished)


def load_one(events: list[dict[str, object]]) -> rounds.PhaseRounds:
    phases = rounds.load_phases(events)
    if len(phases) != 1:
        raise AssertionError(f"Expected one phase, got {len(phases)}")
    return phases[0]


def restarted_batch_events() -> list[dict[str, object]]:
    """Two repaired batches, an unledgered repair, and a final dispatch without workers."""
    events = phase_events() + ledger_events(finding="CODE")
    events += pass_events(BASE + 10, BASE + 20, name="original")
    events += pass_events(BASE + 40, BASE + 50, name="restart", activity="Repair failing tests")
    events += ledger_events(at=BASE + 60, finding="UX", lens="ux")
    events += pass_events(BASE + 70, BASE + 80, name="second-batch")
    events += ledger_events(at=BASE + 90, finding="UNSTARTED")
    return events


def reviewed_summary_events() -> list[dict[str, object]]:
    """Three reviewed phases and five unreviewed phases with unequal repair time."""
    events: list[dict[str, object]] = []
    for name, reviewed, count, seconds in (
        ("reviewed-heavy", True, 3, 10),
        ("reviewed-light", True, 1, 10),
        ("reviewed-clean", True, 0, 0),
        ("unreviewed-heavy", False, 4, 100),
        ("unreviewed-clean-1", False, 0, 0),
        ("unreviewed-clean-2", False, 0, 0),
        ("unreviewed-clean-3", False, 0, 0),
        ("unreviewed-clean-4", False, 0, 0),
    ):
        events += phase_events(run=name, instance=name)
        if reviewed:
            events += pass_events(BASE + 1, BASE + 2, run=name, instance=name,
                                  name=f"{name}-review", kind="review")
        for index in range(count):
            start = BASE + 10 + index * 200
            events += pass_events(start, start + seconds, run=name, instance=name,
                                  name=f"{name}-fix-{index}", number=index + 1)
    return events


@final
class RepairRoundTests(unittest.TestCase):
    def test_restarted_fix_number_does_not_inherit_an_earlier_batch(self) -> None:
        events = ledger_events(lens="ux")
        events += pass_events(BASE + 10, BASE + 20, name="original")
        events += pass_events(BASE + 30, BASE + 40, name="restart", activity="Repair failing tests")
        for records in (events, list(reversed(events))):
            with self.subTest(reversed=records is not events):
                built = rounds.phase_rounds(records)
                self.assertEqual([(repair["cause"], repair["covered"]) for repair in built],
                                 [("review-ux", ["F1"]), ("gate", [])])
                self.assertEqual(sum(repair["fix_seconds"] for repair in built), 20)

    def test_later_batch_joins_only_the_first_matching_pass_after_dispatch(self) -> None:
        events = pass_events(BASE + 10, BASE + 20, name="before", activity="Repair merge conflict")
        events += ledger_events(at=BASE + 25, finding="LATER")
        events += pass_events(BASE + 30, BASE + 40, name="recipient")
        events += pass_events(BASE + 50, BASE + 60, name="after", activity="Repair failing lint")
        built = rounds.phase_rounds(events)
        self.assertEqual([(repair["cause"], repair["covered"]) for repair in built],
                         [("merge", []), ("review-code", ["LATER"]), ("gate", [])])

    def test_batch_during_an_open_group_joins_its_later_matching_worker(self) -> None:
        events = pass_events(BASE + 10, BASE + 20, name="before", number=2)
        events += pass_events(BASE + 30, BASE + 100, name="open", number=1)
        events += ledger_events(number=2, at=BASE + 49, finding="MID", lens="ux")
        events += pass_events(BASE + 60, BASE + 80, name="recipient", slot="test", number=2)
        events += pass_events(BASE + 120, BASE + 130, name="after", number=2)
        built = rounds.phase_rounds(events)
        self.assertEqual([(repair["cause"], repair["covered"]) for repair in built],
                         [("other", []), ("review-ux", ["MID"]), ("other", [])])
        self.assertEqual((built[1]["started_at"], built[1]["finished_at"],
                          built[1]["passes"], built[1]["fix_seconds"]),
                         (BASE + 30, BASE + 100, 2, 90))

    def test_repeated_ledger_number_without_workers_keeps_each_dispatch(self) -> None:
        events = ledger_events(finding="CODE")
        events += ledger_events(at=BASE + 25, finding="UX", lens="ux")
        built = rounds.phase_rounds(events)
        self.assertEqual([(repair["cause"], repair["covered"]) for repair in built],
                         [("review-code", ["CODE"]), ("review-ux", ["UX"])])
        self.assertEqual([repair["started_at"] for repair in built], [BASE + 6, BASE + 26])
        self.assertEqual([(repair["passes"], repair["fix_seconds"], repair["finished_at"])
                          for repair in built], [(0, 0, 0), (0, 0, 0)])

    def test_batch_after_the_last_matching_worker_stays_ledger_only(self) -> None:
        events = pass_events(BASE + 10, BASE + 20, activity="Repair failing tests")
        events += ledger_events(at=BASE + 25, finding="UNSTARTED", lens="ux")
        built = rounds.phase_rounds(events)
        self.assertEqual([(repair["cause"], repair["covered"], repair["fix_seconds"])
                          for repair in built],
                         [("gate", [], 10), ("review-ux", ["UNSTARTED"], 0)])

    def test_worker_start_at_dispatch_time_is_eligible_but_wrong_number_is_not(self) -> None:
        events = ledger_events(number=2, at=BASE + 9)
        events += pass_events(BASE + 10, BASE + 20, name="wrong-number", number=1)
        events += pass_events(BASE + 30, BASE + 40, name="matching", number=2)
        built = rounds.phase_rounds(events)
        self.assertEqual([(repair["cause"], repair["covered"]) for repair in built],
                         [("other", []), ("review-code", ["F1"])])
        exact = ledger_events(at=BASE + 9) + pass_events(BASE + 10, BASE + 20)
        one, = rounds.phase_rounds(exact)
        self.assertEqual((one["cause"], one["covered"], one["passes"]),
                         ("review-code", ["F1"], 1))

    def test_load_phases_counts_restarted_batches_without_adding_worker_time(self) -> None:
        phase = load_one(restarted_batch_events())
        self.assertEqual([(repair["cause"], repair["covered"]) for repair in phase["rounds"]],
                         [("review-code", ["CODE"]), ("gate", []),
                          ("review-ux", ["UX"]), ("review-code", ["UNSTARTED"])])
        self.assertEqual((phase["findings"], phase["fix_seconds"], phase["past_landing"]),
                         (3, 30, 1))
        self.assertEqual(phase["causes"], {"review-ux": 1, "review-code": 2, "gate": 1,
                                           "merge": 0, "live": 0, "other": 0})

    def test_phase_rounds_exposes_one_dispatch_with_all_concurrent_passes(self) -> None:
        events = ledger_events(number=3, lens="ux")
        events += pass_events(BASE + 10, BASE + 30, name="code", number=3)
        events += pass_events(BASE + 20, BASE + 50, name="ux", slot="test", number=3)
        before = copy.deepcopy(events)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            built = rounds.phase_rounds(events)
        one, = built
        self.assertEqual((one["started_at"], one["finished_at"]), (BASE + 10, BASE + 50))
        self.assertEqual((one["cause"], one["passes"], one["fix_seconds"]), ("review-ux", 2, 50))
        self.assertEqual(one["covered"], ["F1"])
        self.assertEqual(events, before)
        self.assertEqual(output.getvalue(), "")

    def test_overlapping_slots_keep_the_round_open_until_the_last_finish(self) -> None:
        events = phase_events()
        events += pass_events(BASE + 10, BASE + 30, name="a", slot="impl")
        events += pass_events(BASE + 20, BASE + 50, name="b", slot="test")
        events += pass_events(BASE + 40, BASE + 60, name="c", slot="review")
        events += pass_events(BASE + 70, BASE + 80, name="d", number=2)
        result = load_one(events)
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["fix_seconds"], 80)

    def test_ledger_round_and_matching_fix_pass_are_one_round(self) -> None:
        events = phase_events() + ledger_events()
        events += pass_events(BASE + 10, BASE + 40, number=1)
        result = load_one(events)
        self.assertEqual(len(result["rounds"]), 1)
        self.assertEqual(result["causes"]["review-code"], 1)
        self.assertEqual(result["findings"], 1)
        self.assertEqual(result["fix_seconds"], 30)

    def test_abandoned_start_does_not_absorb_a_later_completed_dispatch(self) -> None:
        events = phase_events()
        events += pass_events(BASE + 10, BASE + 20, name="abandoned", slot="test")[:1]
        events += pass_events(BASE + 30, BASE + 40, name="completed", slot="impl", number=2)
        result = load_one(events)
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["rounds"][0]["finished_at"], 0)
        self.assertEqual(result["rounds"][1]["finished_at"], BASE + 40)
        self.assertEqual(result["fix_seconds"], 10)

    def test_equal_start_unfinished_pass_joins_the_round_in_either_event_order(self) -> None:
        unfinished = pass_events(BASE + 10, BASE + 20, name="unfinished", slot="test")[:1]
        finished = pass_events(BASE + 10, BASE + 20, name="finished", slot="impl")
        later: list[dict[str, object]] = []
        for number in range(2, 5):
            later += pass_events(BASE + number * 20, BASE + number * 20 + 10,
                                 name=f"later-{number}", number=number)
        first = load_one(phase_events() + unfinished + finished + later)
        second = load_one(phase_events() + finished + unfinished + later)
        self.assertEqual(first["rounds"], second["rounds"])
        self.assertEqual(len(first["rounds"]), 4)
        self.assertEqual(first["rounds"][0]["passes"], 2)
        self.assertEqual(first["past_landing"], second["past_landing"])
        self.assertEqual(first["past_landing"], 1)

    def test_unfinished_pass_does_not_extend_the_known_round_end(self) -> None:
        events = phase_events() + pass_events(BASE + 10, BASE + 20, name="finished")
        events += pass_events(BASE + 15, BASE + 100, name="unfinished", slot="test")[:1]
        events += pass_events(BASE + 20, BASE + 30, name="next", number=2)
        result = load_one(events)
        self.assertEqual([repair["passes"] for repair in result["rounds"]], [2, 1])

    def test_ledger_joins_by_instance_and_does_not_mix_runs_or_phase_restarts(self) -> None:
        events = phase_events() + phase_events(run="run-b", instance="instance-b")
        events += phase_events(instance="instance-retry", start=BASE + 2_000, finish=BASE + 3_000)
        events += ledger_events(instance="instance-b", lens="ux-visual")
        events += pass_events(BASE + 10, BASE + 40, run="run-b", instance="instance-b")
        events += pass_events(BASE + 2_010, BASE + 2_020, instance="instance-retry")
        phases = rounds.load_phases(events)
        by_instance = {phase["phase_instance_id"]: phase for phase in phases}
        self.assertEqual(len(phases), 3)
        self.assertEqual(by_instance["instance-a"]["findings"], 0)
        self.assertEqual(len(by_instance["instance-a"]["rounds"]), 0)
        self.assertEqual(by_instance["instance-b"]["run_id"], "run-b")
        self.assertEqual(by_instance["instance-b"]["causes"]["review-ux"], 1)
        self.assertEqual(by_instance["instance-retry"]["causes"]["other"], 1)

    def test_ux_cause_uses_only_findings_covered_by_the_dispatch(self) -> None:
        events = phase_events() + ledger_events(lens="code")
        uncovered = Event(event_type="finding_opened", timestamp_epoch=BASE + 4,
                          phase_instance_id="instance-a", finding_id="UX2", lens="ux")
        events += [dict(uncovered)] + pass_events(BASE + 10, BASE + 20)
        result = load_one(events)
        self.assertEqual(result["causes"]["review-code"], 1)
        self.assertEqual(result["causes"].get("review-ux", 0), 0)
        self.assertEqual(result["findings"], 2)

    def test_any_covered_ux_lens_makes_a_review_round_ux(self) -> None:
        events = phase_events() + ledger_events(lens="code")
        ux = Event(event_type="finding_opened", timestamp_epoch=BASE + 4,
                   phase_instance_id="instance-a", finding_id="UX2", lens="visual-ux")
        events.append(dict(ux))
        for record in events:
            if record.get("event_type") == "finding_batch_dispatched":
                record["covered"] = ["F1", "UX2"]
        events += pass_events(BASE + 10, BASE + 20, activity="Repair failing tests after merge")
        result = load_one(events)
        self.assertEqual(result["causes"]["review-ux"], 1)
        self.assertEqual(sum(result["causes"].values()), 1)

    def test_gate_words_take_precedence_over_merge_and_live(self) -> None:
        for word in ("test", "tests", "lint", "clippy", "basedpyright", "gate", "failing", "broke"):
            with self.subTest(word=word):
                events = phase_events() + [live_event(BASE + 5)]
                events += pass_events(BASE + 10, BASE + 20, activity=f"Repair {word} after merge")
                self.assertEqual(load_one(events)["causes"]["gate"], 1)

    def test_merge_words_take_precedence_over_a_recent_live_check(self) -> None:
        for word in ("merge", "conflict", "port", "rebase"):
            with self.subTest(word=word):
                events = phase_events() + [live_event(BASE + 5)]
                events += pass_events(BASE + 10, BASE + 20, activity=f"Repair {word} branch alpha")
                self.assertEqual(load_one(events)["causes"]["merge"], 1)

    def test_live_cause_accepts_check_shot_and_smoke_at_the_thirty_minute_limit(self) -> None:
        for label in ("live check", "shot", "smoke"):
            with self.subTest(label=label):
                events = phase_events(finish=BASE + 3_000) + [live_event(BASE + 10, label=label)]
                events += pass_events(BASE + 1_810, BASE + 1_820)
                self.assertEqual(load_one(events)["causes"]["live"], 1)

    def test_completed_ux_review_explains_the_next_unledgered_repair(self) -> None:
        events = phase_events(phase="202")
        checked = live_event(BASE + 10, label="UX review")
        checked["activity_text"] = "CPU pane four states at width 19"
        checked["activity_result"] = "2 defects"
        events.append(checked)
        events += pass_events(BASE + 69, BASE + 120, phase="202",
                              activity="repairing phase 202 awaiting row")
        self.assertEqual(load_one(events)["causes"]["live"], 1)

    def test_old_future_and_other_phase_checks_do_not_explain_a_round(self) -> None:
        for finished, instance in ((BASE + 9, "instance-a"), (BASE + 1_820, "instance-a"),
                                   (BASE + 1_800, "instance-b")):
            with self.subTest(finished=finished, instance=instance):
                events = phase_events(finish=BASE + 3_000)
                events += [live_event(finished, instance=instance)]
                events += pass_events(BASE + 1_810, BASE + 1_815)
                target, = [phase for phase in rounds.load_phases(events)
                           if phase["phase_instance_id"] == "instance-a"]
                self.assertEqual(target["causes"]["other"], 1)

    def test_non_fix_passes_do_not_become_repair_rounds_or_fix_time(self) -> None:
        events = phase_events()
        for index, kind in enumerate(("impl", "test", "review")):
            events += pass_events(BASE + 10 + index * 20, BASE + 20 + index * 20,
                                  name=kind, kind=kind)
        result = load_one(events)
        self.assertEqual(result["rounds"], [])
        self.assertEqual(result["fix_seconds"], 0)

    def test_a_different_phase_in_any_pass_activity_marks_the_record_mixed(self) -> None:
        events = phase_events(phase="6")
        events += pass_events(BASE + 10, BASE + 20, phase="6", activity="Repair Phase 6 findings")
        events += pass_events(BASE + 30, BASE + 40, name="review", kind="review", phase="6",
                              activity="Review Phase 14 findings")
        self.assertEqual(load_one(events)["check"], "mixed")

    def test_the_same_phase_number_and_a_single_merge_keep_the_record_ok(self) -> None:
        events = phase_events(phase="6")
        events += pass_events(BASE + 10, BASE + 20, phase="6", activity="Repair Phase 6 findings")
        events += pass_events(BASE + 30, BASE + 40, name="merge", number=2, phase="6",
                              activity="merge branch alpha")
        self.assertEqual(load_one(events)["check"], "ok")

    def test_only_a_pass_span_over_seventy_two_hours_is_mixed(self) -> None:
        for span, check in ((72 * 3_600, "ok"), (72 * 3_600 + 1, "mixed")):
            with self.subTest(span=span):
                events = phase_events(finish=BASE + span + 100)
                events += pass_events(BASE + 10, BASE + 20, kind="impl")
                events += pass_events(BASE + span, BASE + span + 10, name="last")
                self.assertEqual(load_one(events)["check"], check)

    def test_distinct_merges_mark_the_record_mixed(self) -> None:
        events = phase_events(finish=BASE + 26 * 3_600)
        events += pass_events(BASE + 10, BASE + 20, activity="merge branch alpha")
        events += pass_events(BASE + 25 * 3_600 + 10, BASE + 25 * 3_600 + 20,
                              name="second", number=2,
                              activity="merge branch beta")
        result = load_one(events)
        self.assertEqual(result["causes"]["merge"], 2)
        self.assertEqual(result["check"], "mixed")

    def test_concurrent_merge_passes_with_different_texts_keep_the_record_ok(self) -> None:
        events = phase_events()
        events += pass_events(BASE + 10, BASE + 30, activity="merge branch alpha")
        events += pass_events(BASE + 20, BASE + 40, name="second", slot="test",
                              activity="resolve conflicts from beta")
        result = load_one(events)
        self.assertEqual(result["causes"]["merge"], 1)
        self.assertEqual(result["check"], "ok")

    def test_merge_rounds_within_twenty_four_hours_keep_the_record_ok(self) -> None:
        for hours in (2, 24):
            with self.subTest(hours=hours):
                events = phase_events(finish=BASE + 25 * 3_600)
                events += pass_events(BASE + 10, BASE + 20, activity="merge branch alpha")
                events += pass_events(BASE + hours * 3_600 + 10, BASE + hours * 3_600 + 20,
                                      name="second", number=2, activity="merge branch beta")
                result = load_one(events)
                self.assertEqual(result["causes"]["merge"], 2)
                self.assertEqual(result["check"], "ok")

    def test_repairing_the_same_merge_twice_keeps_the_record_ok(self) -> None:
        events = phase_events()
        events += pass_events(BASE + 10, BASE + 20, activity="merge branch alpha")
        events += pass_events(BASE + 30, BASE + 40, name="second", number=2,
                              activity="merge branch alpha")
        result = load_one(events)
        self.assertEqual(result["causes"]["merge"], 2)
        self.assertEqual(result["check"], "ok")

    def test_past_landing_counts_rounds_after_the_third_dispatch(self) -> None:
        for count, expected in ((2, 0), (3, 0), (4, 1), (6, 3)):
            with self.subTest(count=count):
                events = phase_events()
                for index in range(count):
                    events += pass_events(BASE + 10 + index * 20, BASE + 20 + index * 20,
                                          name=f"fix-{index}", number=index + 1)
                result = load_one(events)
                self.assertEqual(len(result["rounds"]), count)
                self.assertEqual(result["past_landing"], expected)

    def test_phase_boundaries_and_metadata_survive_without_mutating_input(self) -> None:
        title = "A complete phase title which exceeds forty characters for the table"
        events = phase_events(title=title) + pass_events(BASE + 10, BASE + 20)
        before = copy.deepcopy(events)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = load_one(events)
        self.assertEqual((result["run_id"], result["phase_id"], result["phase_instance_id"]),
                         ("run-a", "1", "instance-a"))
        self.assertEqual((result["started_at"], result["finished_at"]), (BASE, BASE + 1_000))
        self.assertEqual(result["title"], title)
        self.assertEqual(result["plan"], "round-study.md")
        self.assertEqual(events, before)
        self.assertEqual(output.getvalue(), "")


@final
class RoundsCommandTests(unittest.TestCase):
    def test_summary_excludes_unreviewed_phases_from_median_and_shares(self) -> None:
        report = self.report(reviewed_summary_events())
        meta = report["_meta"]
        self.assertEqual(meta["median_rounds"], 1)
        self.assertEqual(meta["reviewed_phases"], 3)
        self.assertEqual(meta["unreviewed_phases"], 5)
        self.assertEqual(meta["heavy_phases"], 1)
        self.assertAlmostEqual(cast(float, meta["heavy_phase_share"]), 100 / 3)
        self.assertEqual(meta["heavy_fix_seconds"], 30)
        self.assertEqual(meta["reviewed_fix_seconds"], 40)
        self.assertAlmostEqual(cast(float, meta["heavy_fix_share"]), 75)
        self.assertEqual(meta["phases"], 8)
        self.assertEqual(meta["fix_seconds"], 440)
        self.assertEqual(meta["rounds"], 8)
        self.assertEqual(meta["causes"], {"review-ux": 0, "review-code": 0, "gate": 0,
                                         "merge": 0, "live": 0, "other": 8})
        # Limiting displayed rows must not change the population being summarized.
        self.assertEqual(self.report(reviewed_summary_events(), top=1)["_meta"], meta)

    def test_rows_retain_unreviewed_repairs_and_reviewed_zero_round_phases(self) -> None:
        rows = {row["run_id"]: row for row in self.report(reviewed_summary_events())["rows"]}
        self.assertEqual(len(rows), 8)
        self.assertEqual({name for name, row in rows.items() if row["reviewed"]},
                         {"reviewed-heavy", "reviewed-light", "reviewed-clean"})
        self.assertEqual(rows["reviewed-clean"]["rounds"], 0)
        self.assertEqual(rows["unreviewed-heavy"]["rounds"], 4)
        self.assertEqual(rows["unreviewed-heavy"]["fix_seconds"], 400)

    def test_text_names_the_reviewed_population_and_counts_unreviewed_separately(self) -> None:
        events = reviewed_summary_events()
        args = argparse.Namespace(since="", until="", status="all", top=25,
                                  plan="", format="table", skill="")
        tally = {"unreadable": 0, "future_spine": 0, "unknown_stage": 0}
        output = io.StringIO()
        with patch.object(rounds, "_events", return_value=(
                [("unit-history", record) for record in events], tally)), \
                contextlib.redirect_stdout(output):
            rounds.run(args)
        lines = output.getvalue().splitlines()
        self.assertTrue(any("8 phases counted; 3 reviewed; median 1 rounds over reviewed phases"
                            in line for line in lines), output.getvalue())
        self.assertTrue(any("5 phases with no review recorded (not in the median or shares)"
                            in line for line in lines), output.getvalue())

    def test_an_empty_reviewed_set_has_zero_statistics_but_keeps_all_repair_time(self) -> None:
        events = phase_events()
        for index in range(4):
            events += pass_events(BASE + 10 + index * 20, BASE + 20 + index * 20,
                                  name=f"fix-{index}", number=index + 1)
        report = self.report(events)
        meta = report["_meta"]
        for key in ("reviewed_phases", "median_rounds", "heavy_phases", "heavy_phase_share",
                    "heavy_fix_seconds", "reviewed_fix_seconds", "heavy_fix_share"):
            with self.subTest(field=key):
                self.assertEqual(meta[key], 0)
        self.assertEqual(meta["unreviewed_phases"], 1)
        self.assertEqual(meta["fix_seconds"], 40)
        self.assertEqual(meta["rounds"], 4)
        self.assertFalse(report["rows"][0]["reviewed"])

    def test_review_identity_uses_run_and_instance_when_phase_numbers_repeat(self) -> None:
        events = phase_events(instance="first", phase="7", title="First attempt")
        events += phase_events(instance="second", phase="7", title="Second attempt",
                               start=BASE + 2_000, finish=BASE + 3_000)
        events += pass_events(BASE + 10, BASE + 20, instance="first", phase="7", kind="review")
        events += phase_events(run="other-run", instance="first", phase="7", title="Other run")
        rows = {row["title"]: row for row in self.report(events)["rows"]}
        self.assertTrue(rows["First attempt"]["reviewed"])
        self.assertFalse(rows["Second attempt"]["reviewed"])
        self.assertFalse(rows["Other run"]["reviewed"])
        self.assertEqual(self.report(events)["_meta"]["reviewed_phases"], 1)

    def test_interrupted_review_start_counts_but_a_finish_without_a_start_does_not(self) -> None:
        events = phase_events(instance="interrupted", title="Interrupted review")
        events += pass_events(BASE + 10, BASE + 20, instance="interrupted", kind="review")[:1]
        events += phase_events(instance="finish-only", title="Legacy finish only")
        events += pass_events(BASE + 10, BASE + 20, instance="finish-only", kind="review")[1:]
        rows = {row["title"]: row for row in self.report(events)["rows"]}
        self.assertTrue(rows["Interrupted review"]["reviewed"])
        self.assertEqual(rows["Interrupted review"]["rounds"], 0)
        self.assertFalse(rows["Legacy finish only"]["reviewed"])

    def test_phase_window_does_not_clip_review_evidence_or_include_an_older_phase(self) -> None:
        events = phase_events(start=BASE + 400, finish=BASE + 1_000)
        events += pass_events(BASE + 800, BASE + 900, kind="review")
        events += phase_events(run="old", instance="old", start=BASE, finish=BASE + 1_000)
        events += pass_events(BASE + 450, BASE + 460, run="old", instance="old", kind="review")
        report = self.report(events, since="2026-10-20T00:05:00+00:00",
                             until="2026-10-20T00:10:00+00:00", cli=True)
        row, = report["rows"]
        self.assertEqual(row["run"], "run-a")
        self.assertTrue(row["reviewed"])
        self.assertEqual(report["_meta"]["reviewed_phases"], 1)
        self.assertEqual(report["_meta"]["unreviewed_phases"], 0)

    def test_json_cli_reports_each_batch_once_after_fix_numbers_restart(self) -> None:
        report = self.report(restarted_batch_events(), cli=True)
        row, = report["rows"]
        self.assertEqual(row["rounds"], 4)
        self.assertEqual([(repair["round"], repair["cause"], repair["covered"])
                          for repair in row["round_details"]],
                         [(1, "review-code", ["CODE"]), (2, "gate", []),
                          (3, "review-ux", ["UX"]), (4, "review-code", ["UNSTARTED"])])
        self.assertEqual((row["findings"], row["fix_seconds"], row["past_landing"]), (3, 30, 1))
        self.assertEqual(row["causes"], {"review-ux": 1, "review-code": 2, "gate": 1,
                                        "merge": 0, "live": 0, "other": 0})
        self.assertEqual(report["_meta"]["rounds"], 4)
        self.assertEqual(report["_meta"]["causes"], row["causes"])

    def test_runs_rejects_until_before_reading_events(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["history.py", "runs", "--until", "1d"]), \
                patch.object(history, "_cmd_runs"), \
                contextlib.redirect_stderr(output), \
                self.assertRaises(SystemExit) as stopped:
            history.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("unrecognized arguments: --until 1d", output.getvalue())

    def report(self, events: list[dict[str, object]], *, since: str = "", until: str = "",
               plan: str = "", top: int = 25, status: str = "all", cli: bool = False) -> JsonReport:
        args = argparse.Namespace(since=since, until=until, status=status, top=top,
                                  plan=plan, format="json", skill="")
        collected = [("unit-history", record) for record in events]
        tally = {"unreadable": 0, "future_spine": 0, "unknown_stage": 0}
        output = io.StringIO()
        with patch.object(rounds, "_events", return_value=(collected, tally)), contextlib.redirect_stdout(output):
            if cli:
                arguments = ["history.py", "rounds", "--since", since, "--until", until,
                             "--status", status, "--top", str(top), "--plan", plan, "--format", "json"]
                with patch("sys.argv", arguments):
                    history.main()
            else:
                rounds.run(args)
        return cast(JsonReport, cast(object, json.loads(output.getvalue())))

    def test_until_excludes_later_phases_without_truncating_an_included_phase(self) -> None:
        cutoff = "2026-10-20T00:05:00+00:00"
        events = phase_events(finish=BASE + 800) + pass_events(BASE + 400, BASE + 500)
        events += phase_events(run="run-b", instance="later", start=BASE + 600, finish=BASE + 900)
        events += pass_events(BASE + 610, BASE + 620, run="run-b", instance="later")
        rows = self.report(events, until=cutoff, cli=True)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run"], "run-a")
        self.assertEqual(int(str(rows[0]["rounds"])), 1)

    def test_since_until_and_plan_select_the_phase_start_window(self) -> None:
        events = phase_events(start=BASE + 100) + pass_events(BASE + 110, BASE + 120)
        events += phase_events(run="run-b", instance="selected", start=BASE + 400,
                               plan="/repo/docs/plans/selected.md")
        events += pass_events(BASE + 410, BASE + 420, run="run-b", instance="selected")
        events += phase_events(run="run-c", instance="late", start=BASE + 700)
        rows = self.report(events, since="2026-10-20T00:05:00+00:00",
                           until="2026-10-20T00:10:00+00:00", plan="selected")["rows"]
        self.assertEqual([(row["run"], row["plan"]) for row in rows], [("run-b", "selected.md")])

    def test_until_accepts_relative_durations_like_since(self) -> None:
        now = datetime.now(UTC).timestamp()
        old = now - 2 * 86_400
        recent = now - 3_600
        events = phase_events(start=old, finish=old + 100)
        events += pass_events(old + 10, old + 20)
        events += phase_events(run="recent", instance="recent", start=recent, finish=recent + 100)
        events += pass_events(recent + 10, recent + 20, run="recent", instance="recent")
        rows = self.report(events, until="1d")["rows"]
        self.assertEqual([row["run"] for row in rows], ["run-a"])

    def test_round_sort_then_fix_time_and_top_keep_mixed_records(self) -> None:
        events: list[dict[str, object]] = []
        for run, count, seconds in (("short", 2, 10), ("long", 2, 30), ("single", 1, 200)):
            events += phase_events(run=run, instance=run)
            for index in range(count):
                events += pass_events(BASE + 10 + index * 300, BASE + 10 + index * 300 + seconds,
                                      run=run, instance=run, name=f"{run}-{index}", number=index + 1,
                                      activity="Repair Phase 9 behavior" if run == "long" else "Repair behavior")
        rows = self.report(events, top=2)["rows"]
        self.assertEqual([row["run"] for row in rows], ["long", "short"])
        self.assertEqual(rows[0]["check"], "mixed")

    def test_json_exposes_past_landing_and_limits_the_title_to_forty_characters(self) -> None:
        title = "x" * 50
        events = phase_events(title=title)
        for index in range(5):
            events += pass_events(BASE + 10 + index * 20, BASE + 20 + index * 20,
                                  name=f"fix-{index}", number=index + 1)
        row, = self.report(events)["rows"]
        self.assertEqual(int(str(row["past_landing"])), 2)
        self.assertEqual(row["title"], title[:40])

    def test_status_filter_uses_phase_completion_status(self) -> None:
        events = phase_events(status="failed") + pass_events(BASE + 10, BASE + 20)
        events += phase_events(run="run-b", instance="completed")
        events += pass_events(BASE + 10, BASE + 20, run="run-b", instance="completed")
        rows = self.report(events, status="completed")["rows"]
        self.assertEqual([row["run"] for row in rows], ["run-b"])


if __name__ == "__main__":
    _ = unittest.main()
