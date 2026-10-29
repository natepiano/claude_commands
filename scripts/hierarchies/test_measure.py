"""Fixture checks for the baseline's counts and source boundaries."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
import json
import io
import os
import re
import subprocess
from unittest.mock import patch

from scripts.hierarchies import measure as m, log_records, push_audit as audit
from scripts.production.issues import Issue


def push_fixture(at: datetime, title: str, outcome: str = "sent") -> log_records.JsonObject:
    return {"time": at.isoformat(), "source": "fixture-unit", "priority": "0", "title": title,
            "message": "No action needed.\n" + title, "outcome": outcome}


def notification_report(
    pushes: Sequence[log_records.JsonObject], reviews: Sequence[log_records.JsonObject], window: log_records.Window,
) -> audit.AuditReport:
    sources = audit.AuditSources([audit.parse_push(record) for record in pushes],
                                [review for record in reviews for review in audit.parse_review(record)], [],
                                audit.ProductionOwnership({}, {}), audit.TopLevelSessions(()))
    return audit.audit(sources, window)


def wait_fixture(
    identity: str, kind: str, start: datetime, minutes: float = 1,
) -> log_records.JsonObject:
    end = start + timedelta(minutes=minutes)
    return {
        "schema_version": 1, "event_id": identity + "-finished", "event_type": "wait_finished",
        "run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase",
        "working_dir": "/archive/hana_catalyst-fixture", "unit": "/archive/hana_catalyst-fixture",
        "production": "hana", "owner_session": "hana-director", "wait_id": identity,
        "wait_kind": kind, "wait_reason": "Admission held", "timestamp": end.isoformat(),
        "timestamp_epoch": end.timestamp(), "wait_started_at": start.isoformat(),
        "wait_started_epoch": start.timestamp(),
        "wait_end": {"state": "Finished", "at": end.isoformat(), "epoch": end.timestamp()},
        "wait_attribution": {"state": "PhaseInstance", "reason": "explicit phase attribution", "candidates": []},
    }


def wait_report_fixture(
    window: log_records.Window, waits: Sequence[log_records.JsonObject],
    phases: Sequence[log_records.JsonObject] = (), missing: Sequence[log_records.JsonObject] = (),
) -> log_records.JsonObject:
    return {
        "since": window.since.isoformat(), "until": window.until.isoformat(),
        "selection": "phase starts and wait starts in [since, until)",
        "covered_n": sum(phase.get("state") == "Covered" for phase in phases),
        "missing_n": sum(phase.get("state") == "HistoryNotRecorded" for phase in phases) + len(missing),
        "missing_phase_n": sum(phase.get("state") == "HistoryNotRecorded" for phase in phases),
        "missing_record_n": len(missing),
        "uncertain_phase_n": sum(phase.get("state") == "UncertainCoverage" for phase in phases),
        "attribution_miss_n": sum(
            log_records.field_text(log_records.object_record(wait["wait_attribution"]), "state")
            in ("UnresolvedAttribution", "UnresolvedSupplier")
            for wait in [*waits, *missing]
        ),
        "production_handoff_n": sum(
            log_records.field_text(log_records.object_record(wait["wait_attribution"]), "state")
            == "ProductionHandoff" for wait in waits
        ),
        "handoffs": [wait for wait in waits if log_records.field_text(
            log_records.object_record(wait["wait_attribution"]), "state") == "ProductionHandoff"],
        "ignored_n": 0, "phases": list(phases), "waits": list(waits), "missing": list(missing),
    }


def wait_phase_fixture(state: str = "Covered", wait_n: int = 0) -> log_records.JsonObject:
    phase: log_records.JsonObject = {
        "run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase",
        "unit": "/archive/hana_catalyst-fixture", "state": state,
        "reason": "wait coverage recorded" if state == "Covered" else "phase start has no wait coverage record",
        "candidates": [],
    }
    if state == "Covered":
        phase["wait_n"] = wait_n
    return phase


def receipt_fixture(commit: str, at: datetime) -> log_records.JsonObject:
    return {
        "event_type": "merge_receipt", "schema_version": 1, "project": "hana",
        "issue": "HI7", "phase_id": "4", "merge_commit": commit,
        "source_checkpoint": "b" * 40, "target_branch": "init/catalyst",
        "merged_at": at.isoformat(), "timestamp_epoch": at.timestamp(),
    }


class MeasuresTest(unittest.TestCase):
    start: datetime = datetime(2026, 10, 13, tzinfo=timezone.utc)
    window: log_records.Window = log_records.Window(start, start + timedelta(days=7))
    at: str = start.isoformat()

    def issue(self, name: str, **fields: str) -> Issue:
        return Issue(Path(name + ".md"), {key: [value] for key, value in fields.items()})

    def test_capacity_groups_units_and_counts_workers_without_running_directors(self) -> None:
        units = [
            m.Unit("hana", "one", "hana", "/one", True, ""),
            m.Unit("hana", "two", "hana", "/two", True, ""),
            m.Unit("tools", "three", "claude", "/three", False, ""),
        ]
        workers = [m.Worker("/one", True, True), m.Worker("/one", True, False)]
        rows = m.capacity(units, workers, "snapshot")
        self.assertEqual([row.n for row in rows], [2, 1, 2])
        self.assertEqual(rows[-1].value, "2; director not running 1")
        self.assertIn("Hana", rows[0].measure)
        self.assertIn("other", rows[1].measure)

    def test_hana_scope_rejects_a_similar_name_and_accepts_known_worktree_names(self) -> None:
        root = Path.home() / "rust"
        self.assertTrue(m.is_hana(str(root / "hana_catalyst-fixture")))
        self.assertFalse(m.is_hana("/no-such-fixture-directory/hana_catalyst-fixture"))

    def test_resource_window_excludes_end_and_separates_test_minutes(self) -> None:
        builds = [
            m.BuildStep("hana", "nextest", self.start, 120),
            m.BuildStep("hana", "check", self.start, 60),
            m.BuildStep("hana", "nextest", self.window.until, 900),
        ]
        other_window = log_records.Window(self.window.until, self.window.until + timedelta(days=1))
        tokens = [m.WindowTokenUsage(self.window, "hana", 1234, 3), m.WindowTokenUsage(other_window, "hana", 9000, 1)]
        rows = m.resource_use(builds, tokens, self.window)
        self.assertEqual([(r.value, r.n) for r in rows], [("1.0", 1), ("2.0", 1), ("1,234", 3)])

    def test_phase_flow_filters_hana_status_window_and_deduplicates_finishes(self) -> None:
        base: log_records.JsonObject = {
            "working_dir": "/deleted/tool-based-ui-tools", "event_type": "phase_finished",
            "status": "completed", "timestamp": self.at,
        }
        records: list[log_records.JsonObject] = [{**base, "event_id": str(i), "phase_elapsed_seconds": i * 60} for i in range(1, 11)]
        records.extend([
            records[0], {**base, "event_id": "failed", "status": "failed", "phase_elapsed_seconds": 10000},
            {**base, "working_dir": "/tmp/other", "event_id": "other"},
            {**base, "event_id": "late", "timestamp": self.window.until.isoformat()},
        ])
        events = [event for record in records for event in m.parse_phase(record, ())]
        rows = m.phase_flow(events, self.window)
        self.assertEqual(rows[0].n, 10)
        self.assertEqual(rows[1].value, "median 5.5 min; p90 9.0 min")
        self.assertEqual(rows[1].n, 10)
        self.assertEqual([(row.value, row.n) for row in rows[2:8]],
                         [("not recorded (HistoryNotRecorded)", 0)] * 6)
        self.assertEqual(rows[8].measure, "Hana wait coverage / HistoryNotRecorded")
        self.assertTrue(all("progress_history.py" in row.source for row in rows[2:]))

    def test_issue_flow_uses_transition_dates_and_does_not_infer_from_modified(self) -> None:
        found = [
            self.issue("done", backlog_goal="1 - Ship Hana", status="closed",
                       date_active="2026-10-14", date_closed="2026-10-15"),
            self.issue("other", backlog_goal="2 - Find Collaborators", status="closed", date_closed="2026-10-15"),
            self.issue("active", backlog_goal="1 - Ship Hana", status="open", stage="active", date_modified="2026-10-15"),
        ]
        rows = m.issue_flow(found, self.window)
        self.assertEqual(rows[0].value, "1")
        self.assertEqual(rows[1].n, 1)
        self.assertEqual(rows[2].value, "median 1440.0 min; p90 1440.0 min")
        self.assertEqual(m.issue_flow(found[2:], self.window)[0].value, "not recorded")

    def test_attention_uses_local_days_and_ignores_replay_reviews(self) -> None:
        at = log_records.required_time("2026-10-14T01:00:00Z")
        pushes = [
            push_fixture(at, "one"),
            push_fixture(at + timedelta(hours=1), "two"),
            push_fixture(self.start, "quiet", "silenced"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.jsonl"
            records = [
                {"time": "2026-10-14T01:00:01Z", "source": "fixture-unit", "verdict": "failed",
                 "sent": {"title": "one", "body": "one"}},
                {"time": "2026-10-14T02:00:01Z", "source": "fixture-unit", "verdict": "failed",
                 "sent": {"title": "two", "body": "two"}, "replay": True},
            ]
            _ = path.write_text("\n".join(json.dumps(record) for record in records))
            problems: list[log_records.SourceProblem] = []
            reviews = [review for group in audit.read_log(path, audit.parse_review, problems) for review in group]
        sources = audit.AuditSources([audit.parse_push(record) for record in pushes], reviews, problems,
                                    audit.ProductionOwnership({}, {}), audit.TopLevelSessions(()))
        messages = [m.parse_message({"time": at.isoformat(), "from": sender, "to": target})
                    for sender, target in [("unit-a", "unit-b"), ("stall-watch", "unit-a"),
                                           ("natedev", "unit-a"), ("unit-a", "user")]]
        rows = m.attention(audit.audit(sources, self.window), messages)
        selected = {row.measure: row.value for row in rows if "2026-10-13" in row.measure}
        self.assertEqual(selected["phone pushes / 2026-10-13"], "2; after failed review 1")
        self.assertEqual(selected["messages / 2026-10-13 / agent"], "2")
        self.assertEqual(selected["messages / 2026-10-13 / automation"], "1")
        self.assertEqual(selected["messages / 2026-10-13 / user-bound"], "1")

    def test_pending_reports_unknown_ages_and_unrecorded_answer_history(self) -> None:
        items = [m.WaitingItem("changes", self.start), m.WaitingItem("changes", log_records.TimeNotRecorded.MISSING)]
        rows = m.pending_attention(items, self.window.until, self.window)
        self.assertEqual(rows[0].value, "2; oldest 7.0 days (1 ages not recorded)")
        self.assertEqual([(row.value, row.n) for row in rows[-2:]], [("not recorded", 0), ("not recorded", 0)])
        self.assertTrue(all("showrunner/outstanding" in row.source for row in rows[-2:]))

    def test_missing_values_keep_separate_domain_types_and_output(self) -> None:
        missing_time = log_records.moment(None)
        rank = m.parse_rank({"name": "one", "rank": 1})
        phase = m.parse_phase({
            "event_type": "phase_finished", "status": "completed", "timestamp": self.at,
            "working_dir": "/deleted/tool-based-ui-tools",
        }, ())[0]
        sources = m.BaselineSources([], [], [], [], [phase], notification_report([], [], self.window), [], [], [rank], [], [])
        missing_values = [missing_time, phase.seconds, rank.score, sources.merges]
        for index, value in enumerate(missing_values):
            for other in missing_values[index + 1:]:
                with self.subTest(value=value, other=other):
                    self.assertNotIsInstance(value, type(other))
        self.assertFalse(self.window.contains(missing_time))
        phase_rows = m.phase_flow([phase], self.window)
        self.assertEqual([(row.value, row.n) for row in phase_rows[:2]], [("1", 1), ("not recorded", 0)])
        found = [self.issue("one", status="open", backlog_goal="1 - Ship Hana")]
        self.assertEqual(m.rank_coverage(found, [rank], [], "snapshot")[2].value, "not recorded")
        self.assertEqual(m.merge_flow(sources.merges, self.window)[0].value, "not recorded")

    def test_message_metadata_overrides_sender_names(self) -> None:
        records: list[log_records.JsonObject] = [
            {"time": self.at, "from": "cron", "to": "unit", "kind": "agent"},
            {"time": self.at, "from": "natedev", "to": "unit", "kind": "automation"},
            {"time": self.at, "from": "timer", "to": "unit", "route": {"kind": "session"}},
            {"time": self.at, "from": "unit", "to": "unit", "route": "automation"},
            {"time": self.at, "from": "unit", "to": "user", "kind": "agent"},
        ]
        messages = [m.parse_message(record) for record in records]
        rows = m.attention(notification_report([], [], self.window), messages)
        selected = {row.measure.rsplit(" / ", 1)[-1]: row.n for row in rows if "2026-10-12" in row.measure}
        self.assertEqual(selected, {"2026-10-12": 0, "agent": 2, "automation": 2, "user-bound": 1})

    def test_attention_agrees_with_audit_on_failed_rules_pairing_and_delivery(self) -> None:
        at = self.start + timedelta(days=1)
        burst_at = at + timedelta(days=1)
        rewrite_at = at + timedelta(days=2)
        pushes = [push_fixture(at, "unreviewed"), push_fixture(burst_at, "burst"),
                  push_fixture(burst_at + timedelta(seconds=2), "burst"), push_fixture(rewrite_at, "rewritten"),
                  push_fixture(rewrite_at, "cancelled", "cancelled"), push_fixture(self.window.until, "later")]
        reviews: list[log_records.JsonObject] = [
            {"time": at.isoformat(), "source": "fixture-unit", "verdict": "unreviewed: timeout",
             "failed_rules": ["context"], "sent": {"title": "unreviewed", "body": "unreviewed"}},
            {"time": burst_at.isoformat(), "source": "fixture-unit", "verdict": "failed",
             "sent": {"title": "burst", "body": "burst"}},
            {"time": rewrite_at.isoformat(), "source": "fixture-unit", "verdict": "rewrite", "failed_rules": ["context"],
             "draft": {"title": "rewritten", "body": "old body"},
             "sent": {"title": "rewritten", "body": "rewritten"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _ = (folder / "pushover.jsonl").write_text("\n".join(json.dumps(row) for row in pushes))
            _ = (folder / "review.jsonl").write_text("\n".join(json.dumps(row) for row in reviews))
            report = audit.audit(audit.load_sources(folder), self.window)
        rows = {row.measure: row for row in m.attention(report, [])}
        self.assertEqual(len(report.entries), 2)
        self.assertEqual(rows["phone pushes after failed review"].value, str(len(report.entries)))
        self.assertEqual(rows["phone pushes sent"].value, str(sum(report.pushes_by_showrunner.values())))
        self.assertEqual(rows["phone pushes sent"].n, 4)
        self.assertEqual(rows["phone pushes / 2026-10-13"].value, "1; after failed review 1")
        self.assertEqual(rows["phone pushes / 2026-10-14"].value, "2; after failed review 1")
        self.assertEqual(rows["phone pushes / 2026-10-15"].value, "1; after failed review 0")

    def test_rank_coverage_filters_goal_and_uses_twenty_ranked_open_issues(self) -> None:
        found = [
            self.issue(str(i), status="open", backlog_goal="1 - Ship Hana", unit="unit" if i % 2 else "", showrunner="hana")
            for i in range(25)
        ]
        found.append(self.issue("wrong", status="closed", backlog_goal="1 - Ship Hana", unit="unit"))
        rankings = [m.IssueRank(str(i), 25 - i, 1.0) for i in range(25)] + [m.IssueRank("wrong", 1, 1.0)]
        units = [m.Unit("tool-based-ui", "unit", "hana", "tree", True, "", "hana", m.UnitActivity.LIVE),
                 m.Unit("hana", "unlinked", "hana", "tree2", True, "", activity=m.UnitActivity.LIVE)]
        rows = m.rank_coverage(found, rankings, units, "snapshot")
        self.assertEqual(rows[0].value, "10/20")
        self.assertEqual(rows[1].value, "1/2")
        self.assertEqual(rows[2].value, "10/20 (50.0%)")

    def test_issue_closures_split_projects_exclude_declines_and_mark_merge_text(self) -> None:
        base = {"backlog_goal": "1 - Ship Hana", "status": "closed", "date_closed": "2026-10-15"}
        found = [
            self.issue("hana change", **base, project="hana", reason="merged into init/catalyst as abc1234"),
            self.issue("tool change", **base, project="cargo-port", reason="merged as def5678"),
            self.issue("declined change", **base, project="hana", reason="declined"),
            self.issue("no receipt", **base, project="hana_liminal"),
            self.issue("diegetic change", **base, project="hana_diegetic", merge_commit="123abcd"),
            self.issue("conduit change", **base, project="hana_conduit", reason="built at abc1234"),
            self.issue("undated tool", **{**base, "date_closed": "a"}, project="cargo-berth"),
        ]
        rows = {row.measure: row for row in m.issue_flow(found, self.window)}
        self.assertEqual(rows["goal 1 issues closed"].value, "5")
        self.assertEqual(rows["goal 1 issues closed / cargo-port / other"].value, "1")
        self.assertEqual(rows["goal 1 issues closed / cargo-berth / other"].value,
                         "not recorded (0 dated in window; 1 closure dates not recorded)")
        self.assertEqual(rows["goal 1 Hana-project issues closed"].value, "4")
        self.assertEqual(rows["goal 1 declined closures excluded"].value, "1")
        self.assertEqual((rows["goal 1 Hana closures naming a merge commit"].value,
                          rows["goal 1 Hana closures naming a merge commit"].n), ("2", 4))
        self.assertEqual(rows["Hana closure merge commit / hana change"].value, "yes")
        self.assertEqual(rows["Hana closure merge commit / no receipt"].value, "not recorded")
        self.assertEqual(rows["Hana closure merge commit / conduit change"].value, "not recorded")

    def test_decline_first_word_excludes_dated_and_undated_closures(self) -> None:
        window = log_records.window_bounds("2026-10-25", "2026-10-25", self.window.until)
        base = {"backlog_goal": "1 - Ship Hana", "status": "closed", "project": "hana",
                "date_active": "2026-10-24", "date_closed": "2026-10-25"}
        for reason in ("declined", "Declined", "declined: Decline all", "  DECLINED:\tDecline all  "):
            with self.subTest(reason=reason):
                found = [
                    self.issue("declined change", **base, reason=reason),
                    self.issue("undated decline", **{**base, "date_closed": ""}, reason=reason),
                    self.issue("completed change", **base, reason="not declined"),
                    self.issue("other completion", **base, reason="declinedx"),
                ]
                rows = {row.measure: row for row in m.issue_flow(found, window)}
                self.assertEqual(rows["goal 1 issues closed"].value, "2")
                self.assertEqual(rows["goal 1 issues closed / hana / Hana"].value, "2")
                self.assertEqual(rows["goal 1 declined closures excluded"].value, "1")
                self.assertEqual(rows["goal 1 active to closed"].n, 2)

    def test_rank_coverage_requires_open_issue_and_live_owner_and_weights_scores(self) -> None:
        base = {"backlog_goal": "1 - Ship Hana", "status": "open", "showrunner": "hana"}
        found = [self.issue("one", **base, unit="running"), self.issue("two", **base, unit="stale"),
                 self.issue("three", **base), self.issue("four", **base),
                 self.issue("closed", status="closed", backlog_goal="1 - Ship Hana", unit="running")]
        units = [
            m.Unit("hana", "running", "hana", "tree", True, "closed", activity=m.UnitActivity.LIVE),
            m.Unit("hana", "stale", "hana", "tree2", True, "two"),
            m.Unit("hana", "linked", "hana", "tree3", True, "[[three]]", activity=m.UnitActivity.LIVE),
            m.Unit("hana", "closed link", "hana", "tree4", True, "[closed](issues/closed.md)", activity=m.UnitActivity.LIVE),
        ]
        ranks = [m.IssueRank(name, float(i), score) for i, (name, score) in enumerate(
            [("closed", 1000.0), ("one", 70.0), ("two", 40.0), ("three", 30.0), ("four", 20.0)], 1)]
        rows = m.rank_coverage(found, ranks, units, "snapshot fixture")
        self.assertEqual([(row.value, row.n) for row in rows], [("2/4", 4), ("2/4", 4), ("100/160 (62.5%)", 4)])
        self.assertTrue(all(row.window == "snapshot fixture" for row in rows))
        self.assertEqual(m.rank_coverage(found, [m.IssueRank("one", 1)], units, "snapshot")[2].value, "not recorded")

    def test_window_bounds_keep_date_days_and_exact_local_or_zoned_times(self) -> None:
        now = log_records.required_time("2026-10-21T00:00Z")
        dates = log_records.window_bounds("2026-10-13", "2026-10-20", now)
        self.assertEqual(dates.since, log_records.required_time("2026-10-13T04:00Z"))
        self.assertEqual(dates.until, log_records.required_time("2026-10-21T04:00Z"))
        times = log_records.window_bounds("2026-10-13T17:24", "2026-10-20T17:24", now)
        self.assertEqual(times.since, log_records.required_time("2026-10-13T21:24Z"))
        self.assertEqual(times.until, log_records.required_time("2026-10-20T21:24Z"))
        self.assertEqual(times, log_records.window_bounds("2026-10-13T17:24-04:00", "2026-10-20T21:24Z", now))
        self.assertEqual(log_records.window_bounds("", "", now), log_records.Window(now - timedelta(days=7), now))
        self.assertRaises(ValueError, log_records.window_bounds, "2026-10-20T17:24", "2026-10-20T17:24", now)
        self.assertRaises(ValueError, log_records.window_bounds, "bad", "", now)
        with patch.object(m, "load_sources") as load, patch.object(m, "render", return_value="fixture"), patch("builtins.print"):
            self.assertEqual(m.main(["baseline", "--since", "2026-10-13T17:24", "--until", "2026-10-20T17:24"]), 0)
            load.assert_called_once_with(times)
        with patch.object(m, "load_sources") as load, patch.object(m, "render", return_value="fixture"), patch("builtins.print"):
            self.assertEqual(m.main(["baseline", "--since", "2026-10-13", "--until", "2026-10-20"]), 0)
            load.assert_called_once_with(dates)


class WaitRecordsTest(unittest.TestCase):
    start: datetime = datetime(2026, 10, 29, tzinfo=timezone.utc)
    window: log_records.Window = log_records.Window(start, start + timedelta(days=1))

    def test_wait_share_uses_the_phase_time_population_and_only_completed_known_kinds(self) -> None:
        done = wait_fixture("done", "WaitingForSupplier", self.start, 15)
        unknown_cause = wait_fixture("no-cause", "", self.start, 80)
        open_wait = wait_fixture("open", "WaitingForUser", self.start, 90)
        open_wait["wait_end"] = {"state": "Open"}
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [done, unknown_cause, open_wait], [wait_phase_fixture(wait_n=3)]))
        first = m.PhaseCompletion("first", self.start, True, 3600)
        phases = [first, first, m.PhaseCompletion("second", self.start, True, 2400),
                  m.PhaseCompletion("other", self.start, False, 100000),
                  m.PhaseCompletion("late", self.window.until, True, 100000),
                  m.PhaseCompletion("undated-duration", self.start, True, m.DurationNotRecorded.MISSING)]
        rows = {row.measure: row for row in m.phase_flow(phases, self.window, history)}
        share = rows["Hana wait / share of phase time with a recorded kind"]
        self.assertEqual(share.n, rows["Hana phase time"].n)
        self.assertEqual(share.n, 2)
        self.assertEqual(share.value, "15.0%; partial coverage: HistoryNotRecorded=0, UncertainCoverage=0, "
                         + "WaitCauseNotRecorded=1, attribution_miss_n=0")
        self.assertIn("progress_history.py", share.source)

    def test_wait_share_distinguishes_missing_history_from_a_recorded_zero(self) -> None:
        phases = [m.PhaseCompletion("one", self.start, True, 600)]
        covered = m.parse_wait_report(wait_report_fixture(self.window, [], [wait_phase_fixture()]))
        for history, expected in ((m.HistoryNotRecorded(), "not recorded (HistoryNotRecorded)"),
                                  (covered, "0.0%")):
            with self.subTest(history=history):
                rows = {row.measure: row for row in m.phase_flow(phases, self.window, history)}
                share = rows["Hana wait / share of phase time with a recorded kind"]
                self.assertEqual((share.value, share.n), (expected, 1))

    def test_wait_share_preserves_each_partial_coverage_state(self) -> None:
        done = wait_fixture("done", "WaitingForUser", self.start, 2)
        unresolved = wait_fixture("unresolved", "WaitingForMergeGate", self.start, 40)
        unresolved["wait_attribution"] = {"state": "UnresolvedAttribution", "reason": "two phases", "candidates": []}
        variants = (
            ([wait_phase_fixture(wait_n=1)], [done], "20.0%"),
            ([wait_phase_fixture("HistoryNotRecorded")], [done], "HistoryNotRecorded=1"),
            ([wait_phase_fixture("UncertainCoverage")], [done], "UncertainCoverage=1"),
            ([wait_phase_fixture(wait_n=1)], [done, unresolved], "attribution_miss_n=1"),
        )
        for phases, waits, expected in variants:
            with self.subTest(expected=expected):
                history = m.parse_wait_report(wait_report_fixture(self.window, waits, phases))
                rows = {row.measure: row for row in m.phase_flow(
                    [m.PhaseCompletion("one", self.start, True, 600)], self.window, history)}
                share = rows["Hana wait / share of phase time with a recorded kind"]
                self.assertTrue(share.value.startswith("20.0%"))
                self.assertIn(expected, share.value)
                kind_suffix = rows["Hana wait / WaitingForUser"].value.partition(";")[2]
                self.assertEqual(share.value.partition(";")[2], kind_suffix)
                self.assertEqual(share.n, 1)

    def test_recorded_wait_retains_supplier_identity_and_reason_states(self) -> None:
        named = wait_fixture("named", "WaitingForSupplier", self.start)
        named["expected_supplier"] = {"state": "NamedSupplier", "address": "worker:run/peer"}
        unnamed = wait_fixture("unnamed", "WaitingForUser", self.start)
        unnamed["expected_supplier"] = {"state": "NoNamedSupplier", "reason": "no supplier address in request"}
        historical = wait_fixture("historical", "WaitingForUser", self.start)
        del historical["wait_reason"]
        history = m.parse_wait_report(wait_report_fixture(self.window, [named, unnamed, historical]))
        self.assertEqual(history.waits[0].expected_supplier, m.NamedSupplier("worker:run/peer"))
        self.assertEqual(history.waits[0].reason, m.RecordedWaitReason("Admission held"))
        self.assertEqual(history.waits[1].expected_supplier, m.NoNamedSupplier("no supplier address in request"))
        self.assertEqual(history.waits[1].reason, m.RecordedWaitReason("Admission held"))
        self.assertEqual(history.waits[2].expected_supplier, m.NoNamedSupplier("expected supplier not recorded"))
        self.assertEqual(history.waits[2].reason, m.WaitReasonNotRecorded("wait reason not recorded"))

    def test_external_wait_event_without_identity_reports_a_source_problem(self) -> None:
        for event_type in ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded"):
            with self.subTest(event_type=event_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runs = root / "runs"
                runs.mkdir()
                phase_path = runs / "hana-run.jsonl"
                self.assertGreater(phase_path.write_text(json.dumps(self.phase_started()) + "\n"), 0)
                event = wait_fixture("lost-id", "WaitingForUser", self.start)
                del event["wait_id"]
                event["event_type"] = event_type
                external = root / "wait_attribution.jsonl"
                self.assertGreater(external.write_text(json.dumps(event) + "\n"), 0)
                before = external.read_bytes()
                problems: list[log_records.SourceProblem] = []
                history = m.read_source(str(external), lambda: m.read_wait_history(root, self.window, problems),
                                        m.HistoryNotRecorded(), problems)
                self.assertIsInstance(history, m.HistoryNotRecorded)
                self.assertEqual(len(problems), 1)
                self.assertIn("wait_id", problems[0].reason)
                self.assertTrue(all("not recorded" in row.value for row in m.hana_wait_rows(history, self.window)))
                self.assertEqual(external.read_bytes(), before)

    def phase_started(self) -> log_records.JsonObject:
        return {
            "schema_version": 1, "event_type": "phase_started", "event_id": "phase-start",
            "run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase",
            "worktree": "/archive/hana_catalyst-fixture", "production": "hana",
            "timestamp": self.start.isoformat(), "timestamp_epoch": self.start.timestamp(),
            "wait_coverage": {"state": "Recorded"},
        }

    def test_six_wait_kinds_report_their_own_completed_minutes_and_counts(self) -> None:
        kinds = (
            "WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
            "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity",
        )
        waits = [wait_fixture(str(index), kind, self.start, index)
                 for index, kind in enumerate(kinds, 1)]
        report = wait_report_fixture(self.window, waits, [wait_phase_fixture(wait_n=6)])
        history = m.parse_wait_report(report)
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        for kind, minutes in zip(kinds, (1, 2, 3, 4, 5, 6)):
            with self.subTest(kind=kind):
                row = rows["Hana wait / " + kind]
                self.assertEqual((row.value, row.n), (f"{minutes:.1f} min", 1))
                self.assertEqual(row.window, self.window.label)
                self.assertIn("progress_history.py", row.source)

    def test_empty_window_keeps_unrecorded_history_apart_from_a_covered_zero(self) -> None:
        absent = m.parse_wait_report(wait_report_fixture(self.window, []))
        covered = m.parse_wait_report(wait_report_fixture(self.window, [], [wait_phase_fixture()]))
        absent_rows = [row for row in m.hana_wait_rows(absent, self.window) if row.measure.startswith("Hana wait / ")]
        covered_rows = [row for row in m.hana_wait_rows(covered, self.window) if row.measure.startswith("Hana wait / ")]
        self.assertEqual(len(absent_rows), 6)
        self.assertTrue(all("HistoryNotRecorded" in row.value and row.n == 0 for row in absent_rows))
        self.assertEqual(len(covered_rows), 6)
        self.assertTrue(all(row.value == "0.0 min" and row.n == 0 for row in covered_rows))

    def test_open_and_unclosed_waits_do_not_add_completed_minutes(self) -> None:
        completed = wait_fixture("done", "WaitingForUser", self.start, 2)
        for state, event in (("Open", "wait_started"), ("Unclosed", "wait_unclosed")):
            with self.subTest(state=state):
                unknown = wait_fixture("unfinished", "WaitingForUser", self.start, 50)
                unknown.update(event_type=event, wait_end={"state": state})
                report = wait_report_fixture(self.window, [completed, unknown], [wait_phase_fixture(wait_n=2)])
                history = m.parse_wait_report(report)
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
                self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                                  rows["Hana wait / WaitingForUser"].n), ("2.0 min", 1))
                unknown_rows = [row for row in rows.values() if "unknown-ended" in row.measure]
                self.assertEqual(sum(row.n for row in unknown_rows), 1)
                self.assertTrue(all("not recorded" in row.value for row in unknown_rows))

    def test_wait_start_selects_the_window_and_excludes_the_end(self) -> None:
        waits = [
            wait_fixture("earlier", "WaitingForUser", self.start - timedelta(minutes=1), 50),
            wait_fixture("first", "WaitingForUser", self.start, 2),
            wait_fixture("later", "WaitingForUser", self.window.until, 100),
        ]
        history = m.parse_wait_report(wait_report_fixture(self.window, waits, [wait_phase_fixture(wait_n=3)]))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                          rows["Hana wait / WaitingForUser"].n), ("2.0 min", 1))

    def test_missing_cause_is_kept_apart_from_missing_phase_history(self) -> None:
        wait = wait_fixture("cause-absent", "", self.start, 3)
        phase = wait_phase_fixture("HistoryNotRecorded")
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [phase]))
        self.assertIsInstance(history.waits[0].kind, m.WaitCauseNotRecorded)
        self.assertIsInstance(history.phases[0].state, m.HistoryNotRecorded)
        rows = m.hana_wait_rows(history, self.window)
        missing_cause = [row for row in rows if "WaitCauseNotRecorded" in row.measure]
        missing_history = [row for row in rows if "HistoryNotRecorded" in row.measure]
        self.assertEqual([(row.value, row.n) for row in missing_cause], [("Admission held", 1)])
        self.assertEqual(sum(row.n for row in missing_history), 1)
        self.assertIn("phase start has no wait coverage record", missing_history[0].value)

    def test_uncertain_candidates_never_become_a_covered_zero(self) -> None:
        candidates: list[log_records.JsonObject] = [
            {"run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase"},
            {"run_id": "hana-run", "phase_id": "5", "phase_instance_id": "hana-next-phase"},
        ]
        reason = "checkpoint has two matching phases"
        missing = wait_fixture("ambiguous", "WaitingForMergeGate", self.start)
        missing.update(event_type="wait_not_recorded", wait_end={"state": "NotRecorded"},
                       wait_record_state={"state": "NotRecorded", "reason": reason},
                       wait_attribution={"state": "UnresolvedAttribution", "reason": reason,
                                         "candidates": candidates})
        del missing["phase_id"]
        del missing["phase_instance_id"]
        phases: list[log_records.JsonObject] = []
        for candidate in candidates:
            phase = wait_phase_fixture("UncertainCoverage")
            phase.update(candidate)
            phase.update(reason=reason, candidates=candidates, recorded_wait_n=0,
                         attribution_miss_ids=["ambiguous"])
            phases.append(phase)
        history = m.parse_wait_report(wait_report_fixture(self.window, [], phases, [missing]))
        rows = m.hana_wait_rows(history, self.window)
        output = log_records.table("Wait fixtures", rows)
        self.assertIn("uncertain_phase_n", output)
        self.assertIn("attribution_miss_n", output)
        self.assertIn(reason, output)
        self.assertIn("hana-next-phase", output)
        kind_rows = [row for row in rows if row.measure in {
            "Hana wait / WaitingForQuotaRefusal", "Hana wait / WaitingForMemoryOrSlot",
            "Hana wait / WaitingForMergeGate", "Hana wait / WaitingForUser",
            "Hana wait / WaitingForSupplier", "Hana wait / ReadyWithoutCapacity",
        }]
        self.assertEqual(len(kind_rows), 6)
        self.assertTrue(all("not recorded" in row.value for row in kind_rows))
        self.assertEqual([(row.value, row.n) for row in rows if row.measure == "Hana wait / uncertain_phase_n"], [("2", 2)])
        self.assertEqual([(row.value, row.n) for row in rows if row.measure == "Hana wait / attribution_miss_n"], [("1", 1)])
        self.assertTrue(all(isinstance(phase.state, m.UncertainWaitCoverage) for phase in history.phases))
        self.assertNotIn("wait_n=0", output)

    def test_completed_wait_with_unresolved_supplier_adds_no_hana_minutes(self) -> None:
        reason = "supplier address is not recorded"
        unresolved = wait_fixture("unknown-supplier", "WaitingForSupplier", self.start, 90)
        candidates: list[log_records.JsonObject] = [
            {"run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase"},
        ]
        unresolved.update(expected_supplier={"state": "NoNamedSupplier", "reason": reason},
                          wait_attribution={"state": "UnresolvedSupplier", "reason": reason,
                                            "candidates": candidates})
        known = wait_fixture("known-supplier", "WaitingForSupplier", self.start, 2)
        phase = wait_phase_fixture("UncertainCoverage")
        phase.update(reason=reason, candidates=candidates, recorded_wait_n=2,
                     attribution_miss_ids=["unknown-supplier"])
        history = m.parse_wait_report(wait_report_fixture(self.window, [known, unresolved], [phase]))
        self.assertEqual([wait.identity for wait in history.waits], ["known-supplier"])
        self.assertEqual([miss.wait_id for miss in history.attribution_misses], ["unknown-supplier"])
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForSupplier"].value.split(";", 1)[0],
                          rows["Hana wait / WaitingForSupplier"].n), ("2.0 min", 1))
        self.assertIn("partial coverage", rows["Hana wait / WaitingForSupplier"].value)
        self.assertIn("not recorded", rows["Hana wait / WaitingForUser"].value)
        self.assertEqual((rows["Hana wait / attribution_miss_n"].value,
                          rows["Hana wait / attribution_miss_n"].n), ("1", 1))
        output = log_records.table("Wait fixtures", list(rows.values()))
        self.assertIn(reason, output)
        self.assertIn("instance=hana-phase", output)

    def test_missing_cause_prevents_covered_zero_and_keeps_unknown_end_count(self) -> None:
        for state, event in (("Finished", "wait_finished"), ("Unclosed", "wait_unclosed")):
            with self.subTest(state=state):
                wait = wait_fixture("cause-absent", "", self.start, 5)
                if state != "Finished":
                    wait.update(event_type=event, wait_end={"state": state})
                history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)]))
                self.assertIsInstance(history.waits[0].kind, m.WaitCauseNotRecorded)
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
                for kind in ("WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
                             "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity"):
                    row = rows["Hana wait / " + kind]
                    self.assertIn("not recorded", row.value)
                    self.assertIn("WaitCauseNotRecorded=1", row.value)
                    self.assertEqual(row.n, 0)
                self.assertEqual(rows["Hana wait / WaitCauseNotRecorded"].n, 1)
                if state == "Unclosed":
                    unknown = rows["Hana wait unknown-ended / WaitCauseNotRecorded"]
                    self.assertEqual((unknown.value, unknown.n), ("duration not recorded", 1))

    def test_production_handoff_wait_counts_without_borrowing_a_phase(self) -> None:
        handoff = wait_fixture("handoff", "WaitingForMergeGate", self.start, 7)
        for field in ("run_id", "phase_id", "phase_instance_id", "working_dir"):
            del handoff[field]
        handoff.update(checkpoint_commit="c" * 40,
                       wait_attribution={"state": "ProductionHandoff",
                                         "reason": "child production checkpoint has no owning phase", "candidates": []})
        history = m.parse_wait_report(wait_report_fixture(self.window, [handoff]))
        self.assertEqual(history.phases, ())
        self.assertTrue(history.waits[0].production_handoff)
        self.assertEqual(history.waits[0].phase, m.ProductionHandoff("hana"))
        self.assertEqual(history.waits[0].owner, m.RecordedWaitOwner("hana-director"))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForMergeGate"].value,
                          rows["Hana wait / WaitingForMergeGate"].n), ("7.0 min", 1))
        output = log_records.table("Wait fixtures", list(rows.values()))
        self.assertEqual((rows["Hana wait / production handoff"].value,
                          rows["Hana wait / production handoff"].n), ("no phase; owner=hana-director", 1))
        self.assertIn("handoff", output)

    def test_wait_identity_keeps_recorded_phase_and_owner_in_named_states(self) -> None:
        wait = wait_fixture("known-owner", "WaitingForUser", self.start, 3)
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)]))
        self.assertEqual(history.waits[0].phase, m.WaitPhaseInstance("hana-run", "hana-phase"))
        self.assertEqual(history.waits[0].owner, m.RecordedWaitOwner("hana-director"))
        self.assertFalse(history.waits[0].production_handoff)
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                          rows["Hana wait / WaitingForUser"].n), ("3.0 min", 1))

    def test_wait_without_an_owner_has_a_named_missing_owner(self) -> None:
        wait = wait_fixture("owner-absent", "WaitingForUser", self.start)
        del wait["owner_session"]
        wait.update(event_type="wait_not_recorded", wait_end={"state": "NotRecorded"},
                    wait_record_state={"state": "NotRecorded", "reason": "wait owner session not recorded"})
        phase = wait_phase_fixture("HistoryNotRecorded")
        phase.update(reason="wait emission has a missing record", wait_missing_n=1, recorded_wait_n=0)
        history = m.parse_wait_report(wait_report_fixture(self.window, [], [phase], [wait]))
        owner = history.waits[0].owner
        self.assertIsInstance(owner, m.WaitOwnerNotRecorded)
        if isinstance(owner, m.WaitOwnerNotRecorded):
            self.assertIn("owner", owner.reason)
        self.assertEqual(history.waits[0].phase, m.WaitPhaseInstance("hana-run", "hana-phase"))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait unknown-ended / WaitingForUser"].value,
                          rows["Hana wait unknown-ended / WaitingForUser"].n), ("duration not recorded", 1))
        self.assertEqual((rows["Hana wait / WaitOwnerNotRecorded"].value,
                          rows["Hana wait / WaitOwnerNotRecorded"].n), ("wait owner session not recorded", 1))

    def test_wait_without_phase_identity_has_a_named_missing_phase(self) -> None:
        wait = wait_fixture("phase-absent", "WaitingForUser", self.start, 3)
        for field in ("run_id", "phase_id", "phase_instance_id", "wait_attribution"):
            del wait[field]
        wait["wait_attribution"] = {}
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait]))
        phase = history.waits[0].phase
        self.assertIsInstance(phase, m.WaitPhaseNotRecorded)
        if isinstance(phase, m.WaitPhaseNotRecorded):
            self.assertIn("phase", phase.reason)
        self.assertFalse(history.waits[0].production_handoff)
        self.assertEqual(history.waits[0].owner, m.RecordedWaitOwner("hana-director"))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                          rows["Hana wait / WaitingForUser"].n), ("3.0 min", 1))
        self.assertEqual(rows["Hana wait / WaitPhaseNotRecorded"].n, 1)
        self.assertIn("phase", rows["Hana wait / WaitPhaseNotRecorded"].value)

    def test_non_hana_waits_do_not_add_hana_minutes(self) -> None:
        hana = wait_fixture("hana", "WaitingForQuotaRefusal", self.start, 3)
        other = wait_fixture("other", "WaitingForQuotaRefusal", self.start, 90)
        other.update(production="tools", working_dir="/archive/other-fixture", unit="/archive/other-fixture",
                     run_id="tools-run", phase_instance_id="tools-phase")
        history = m.parse_wait_report(wait_report_fixture(self.window, [hana, other]))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForQuotaRefusal"].value,
                          rows["Hana wait / WaitingForQuotaRefusal"].n), ("3.0 min", 1))

    def test_known_unit_prefix_attributes_a_deleted_worktree_wait_to_hana(self) -> None:
        wait = wait_fixture("deleted", "WaitingForSupplier", self.start, 4)
        wait.update(production="tools", working_dir="/archive/widget-enhancements-unit",
                    unit="/archive/widget-enhancements-unit")
        report = wait_report_fixture(self.window, [wait])
        history = m.parse_wait_report(report, prefixes=("widget-enhancements",))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
        self.assertEqual((rows["Hana wait / WaitingForSupplier"].value,
                          rows["Hana wait / WaitingForSupplier"].n), ("4.0 min", 1))

    def test_wait_loader_combines_run_and_external_records_without_changing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            runs.mkdir()
            completed = wait_fixture("run-wait", "WaitingForMergeGate", self.start, 2)
            opened = {**completed, "event_type": "wait_started", "wait_end": {"state": "Open"}}
            run_file = runs / "hana-run.jsonl"
            _ = run_file.write_text("\n".join(json.dumps(event) for event in
                                             [self.phase_started(), opened, completed]) + "\n")
            handoff = wait_fixture("external-wait", "WaitingForMergeGate", self.start, 7)
            for field in ("run_id", "phase_id", "phase_instance_id", "working_dir"):
                del handoff[field]
            handoff.update(checkpoint_commit="c" * 40,
                           wait_attribution={"state": "ProductionHandoff",
                                             "reason": "child production checkpoint has no owning phase", "candidates": []})
            external = root / "wait_attribution.jsonl"
            _ = external.write_text(json.dumps(handoff) + "\n")
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            problems: list[log_records.SourceProblem] = []
            history = m.read_wait_history(root, self.window, problems)
            self.assertIsInstance(history, m.HanaWaitHistory)
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
            self.assertEqual((rows["Hana wait / WaitingForMergeGate"].value,
                              rows["Hana wait / WaitingForMergeGate"].n), ("9.0 min", 2))
            self.assertEqual(rows["Hana wait / production handoff"].n, 1)
            self.assertEqual(problems, [])
            self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)

    def test_external_handoff_history_is_read_without_a_runs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = wait_fixture("only-handoff", "WaitingForMergeGate", self.start, 7)
            for field in ("run_id", "phase_id", "phase_instance_id", "working_dir"):
                del handoff[field]
            handoff.update(checkpoint_commit="c" * 40,
                           wait_attribution={"state": "ProductionHandoff",
                                             "reason": "child production checkpoint has no owning phase", "candidates": []})
            path = root / "wait_attribution.jsonl"
            _ = path.write_text(json.dumps(handoff) + "\n")
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            history = m.read_wait_history(root, self.window, problems)
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
            self.assertEqual((rows["Hana wait / WaitingForMergeGate"].value,
                              rows["Hana wait / WaitingForMergeGate"].n), ("7.0 min", 1))
            self.assertEqual(rows["Hana wait / production handoff"].n, 1)
            self.assertEqual(problems, [])
            self.assertFalse((root / "runs").exists())
            self.assertEqual(list(root.iterdir()), [path])
            self.assertEqual(path.read_bytes(), before)

    def test_missing_wait_history_reports_a_source_problem_without_creating_a_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "absent-history"
            problems: list[log_records.SourceProblem] = []
            history = m.read_source(str(root), lambda: m.read_wait_history(root, self.window, problems),
                                    m.HistoryNotRecorded(), problems)
            self.assertIsInstance(history, m.HistoryNotRecorded)
            self.assertFalse(root.exists())
            self.assertEqual(len(problems), 1)
            self.assertIn(str(root / "runs"), problems[0].reason)
            rows = m.hana_wait_rows(history, self.window)
            self.assertTrue(all("not recorded" in row.value for row in rows))

    def test_corrupt_external_history_reports_a_problem_instead_of_a_covered_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs").mkdir()
            external = root / "wait_attribution.jsonl"
            _ = external.write_text("{not JSON}\n")
            before = external.read_bytes()
            problems: list[log_records.SourceProblem] = []
            history = m.read_source(str(external), lambda: m.read_wait_history(root, self.window, problems),
                                    m.HistoryNotRecorded(), problems)
            self.assertIsInstance(history, m.HistoryNotRecorded)
            self.assertEqual(len(problems), 1)
            self.assertIn("invalid JSON or field rows", problems[0].reason)
            self.assertIn("source unavailable", log_records.table(
                "Source problems", log_records.source_problem_rows(problems, self.window)))
            self.assertEqual(external.read_bytes(), before)

    def test_corrupt_run_row_keeps_valid_waits_and_reports_incomplete_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            runs.mkdir()
            path = runs / "hana-run.jsonl"
            _ = path.write_text("\n".join(json.dumps(record) for record in
                                         [self.phase_started(), wait_fixture("good", "WaitingForUser", self.start, 2)])
                                + "\n{bad JSON}\n")
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            history = m.read_wait_history(root, self.window, problems)
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window)}
            self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                              rows["Hana wait / WaitingForUser"].n), ("2.0 min", 1))
            self.assertEqual(len(problems), 1)
            self.assertIn("1 unreadable or unsupported history rows", problems[0].reason)
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_finished_time_and_candidate_wait_count_are_rejected(self) -> None:
        before_start = wait_fixture("negative", "WaitingForUser", self.start, -1)
        self.assertRaisesRegex(ValueError, "before its start", m.parse_wait_report,
                               wait_report_fixture(self.window, [before_start]))
        no_end_time = wait_fixture("absent-end", "WaitingForUser", self.start)
        no_end_time["wait_end"] = {"state": "Finished"}
        self.assertRaises(ValueError, m.parse_wait_report, wait_report_fixture(self.window, [no_end_time]))
        candidate = wait_phase_fixture("UncertainCoverage")
        candidate["wait_n"] = 0
        self.assertRaisesRegex(ValueError, "cannot carry wait_n", m.parse_wait_report,
                               wait_report_fixture(self.window, [], [candidate]))


class MergeReceiptsTest(unittest.TestCase):
    start: datetime = datetime(2026, 10, 29, tzinfo=timezone.utc)
    window: log_records.Window = log_records.Window(start, start + timedelta(days=1))

    def test_receipt_names_the_merge_commit_and_missing_merge_stays_visible(self) -> None:
        receipt = m.parse_receipt(receipt_fixture("a" * 40, self.start))
        merges = [m.HanaMerge("a" * 40, self.start), m.HanaMerge("c" * 40, self.start)]
        rows = m.receipt_rows([receipt], merges, self.window)
        output = log_records.table("Receipt fixtures", rows)
        self.assertIn("ReceiptMissing", output)
        self.assertIn("c" * 40, output)
        self.assertEqual((receipt.commit, receipt.checkpoint), ("a" * 40, "b" * 40))
        self.assertIsInstance(m.receipt_for_merge(merges[1], [receipt]), m.ReceiptMissing)
        coverage = next(row for row in rows if row.measure == "Hana merge receipts / origin/init/catalyst")
        self.assertEqual((coverage.value, coverage.n), ("1/2", 2))

    def test_source_checkpoint_and_unrelated_receipts_do_not_cover_a_merge(self) -> None:
        commit = "b" * 40
        wrong_commit = receipt_fixture("a" * 40, self.start)
        other_project = receipt_fixture(commit, self.start)
        other_project["project"] = "tools"
        other_branch = receipt_fixture(commit, self.start)
        other_branch["target_branch"] = "sdlc"
        receipts = [m.parse_receipt(record) for record in (wrong_commit, other_project, other_branch)]
        rows = m.receipt_rows(receipts, [m.HanaMerge(commit, self.start)], self.window)
        coverage = next(row for row in rows if row.measure == "Hana merge receipts / origin/init/catalyst")
        self.assertEqual((coverage.value, coverage.n), ("0/1", 1))
        self.assertIn("ReceiptMissing", log_records.table("Receipt fixtures", rows))

    def test_receipt_at_window_end_does_not_cover_an_in_window_merge(self) -> None:
        later = m.parse_receipt(receipt_fixture("a" * 40, self.window.until))
        rows = m.receipt_rows([later], [m.HanaMerge("a" * 40, self.start)], self.window)
        totals = {row.measure: (row.value, row.n) for row in rows}
        self.assertEqual(totals["merge receipts / all projects"], ("0", 0))
        self.assertEqual(totals["Hana merge receipts / origin/init/catalyst"], ("0/1", 1))

    def test_merge_history_absence_does_not_become_receipt_coverage_zero(self) -> None:
        receipt = m.parse_receipt(receipt_fixture("a" * 40, self.start))
        rows = m.receipt_rows([receipt], m.MergeHistoryNotRecorded.MISSING, self.window)
        coverage = next(row for row in rows if row.measure == "Hana merge receipts / origin/init/catalyst")
        self.assertIn("not recorded", coverage.value)
        self.assertEqual(coverage.n, 0)
        self.assertEqual(coverage.window, self.window.label)

    def test_receipt_loader_deduplicates_retries_and_keeps_the_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merge_receipts.jsonl"
            record = receipt_fixture("a" * 40, self.start)
            _ = path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n")
            before = path.read_bytes()
            receipts = m.read_receipts(path)
            self.assertNotIsInstance(receipts, m.HistoryNotRecorded)
            rows = {row.measure: row for row in m.receipt_rows(
                receipts, [m.HanaMerge("a" * 40, self.start)], self.window)}
            self.assertEqual((rows["merge receipts / all projects"].value,
                              rows["merge receipts / all projects"].n), ("1", 1))
            self.assertEqual((rows["Hana merge receipts / origin/init/catalyst"].value,
                              rows["Hana merge receipts / origin/init/catalyst"].n), ("1/1", 1))
            self.assertEqual(path.read_bytes(), before)

    def test_absent_receipt_file_and_a_merge_without_a_receipt_keep_distinct_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "merge_receipts.jsonl"
            receipts = m.read_receipts(path)
            self.assertIsInstance(receipts, m.HistoryNotRecorded)
            self.assertFalse(path.parent.exists())
            rows = m.receipt_rows(receipts, [m.HanaMerge("c" * 40, self.start)], self.window)
            missing = [row for row in rows if row.measure == "Hana merge / ReceiptMissing"]
            self.assertEqual([(row.value, row.n) for row in missing], [("c" * 40, 1)])
            self.assertIn("HistoryNotRecorded", rows[0].value)

    def test_corrupt_receipt_source_never_reports_a_trusted_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merge_receipts.jsonl"
            _ = path.write_text(json.dumps(receipt_fixture("a" * 40, self.start)) + "\n{bad JSON}\n")
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            receipts = m.read_source(str(path), lambda: m.read_receipts(path),
                                     m.HistoryNotRecorded("merge receipts not recorded"), problems)
            self.assertIsInstance(receipts, m.HistoryNotRecorded)
            self.assertEqual(len(problems), 1)
            rows = m.receipt_rows(receipts, [], self.window)
            self.assertIn("HistoryNotRecorded", rows[0].value)
            self.assertIn(str(path), log_records.table(
                "Source problems", log_records.source_problem_rows(problems, self.window)))
            self.assertEqual(path.read_bytes(), before)


class BaselineOutputTest(unittest.TestCase):
    window: log_records.Window = log_records.window_bounds("2026-10-21", "2026-10-27", datetime.now(timezone.utc))

    def sources(self) -> m.BaselineSources:
        found: list[Issue] = []
        for name, project, reason, closed in [
            ("hana-one", "hana", "done", "2026-10-22"),
            ("hana-two", "hana", "done", "2026-10-23"),
            ("other", "cargo-port", "done", "2026-10-24"),
            ("declined-one", "hana", "declined: Decline all", "2026-10-25"),
            ("declined-two", "hana", "Declined", "2026-10-25"),
            ("later", "hana", "done", "2026-10-28"),
        ]:
            fields = {"backlog_goal": "1 - Ship Hana", "status": "closed", "project": project,
                      "reason": reason, "date_closed": closed}
            found.append(Issue(Path(name + ".md"), {key: [value] for key, value in fields.items()}))
        start, end = self.window.since, self.window.until
        pushes = [push_fixture(start, "one"), push_fixture(start + timedelta(days=1), "two"),
                  push_fixture(start, "quiet", "silenced"), push_fixture(end, "later")]
        reviews: list[log_records.JsonObject] = [
            {"time": (start + timedelta(seconds=1)).isoformat(), "source": "fixture-unit", "verdict": "failed",
             "sent": {"title": "one", "body": "one"}},
        ]
        merges = [m.HanaMerge("one", start), m.HanaMerge("two", start + timedelta(days=1)),
                  m.HanaMerge("later", end)]
        return m.BaselineSources([], [], [], [], [], notification_report(pushes, reviews, self.window),
                                 [], found, [], [], [], merges)

    def output(self, sources: m.BaselineSources, full: bool = False) -> str:
        arguments = ["baseline", "--since", "2026-10-21", "--until", "2026-10-27"]
        if full:
            arguments.append("--full")
        output = io.StringIO()
        with patch.object(m, "load_sources", return_value=sources), redirect_stdout(output):
            result = m.main(arguments)
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_default_cli_prints_short_summary_in_user_zone_without_tables(self) -> None:
        output = self.output(self.sources())
        lines = output.splitlines()
        self.assertGreaterEqual(len(lines), 3)
        self.assertLessEqual(len(lines), 5)
        self.assertTrue(all(len(line) <= 100 for line in lines), lines)
        self.assertEqual(lines[0], "Window: 2026-10-21 00:00 to 2026-10-28 00:00 EDT (end excluded).")
        self.assertEqual(lines[1], "Hana: 2 merges to origin/init/catalyst; 2 Hana-project issues closed.")
        self.assertEqual(lines[2], "Goal 1: 3 issues closed; 2 declined closures left out.")
        self.assertEqual(lines[3], "User attention: 2 pushes sent to user; 1 after a failed text check.")
        self.assertNotIn("Capacity on Hana", output)
        self.assertNotRegex(output, r"Measure\s+Value\s+n\s+Window\s+Source")

    def test_full_cli_keeps_summary_first_and_uses_the_table_values(self) -> None:
        sources = self.sources()
        summary = self.output(sources)
        output = self.output(sources, full=True)
        self.assertTrue(output.startswith(summary + "\nCapacity on Hana\n"))
        self.assertRegex(output, r"Measure\s+Value\s+n\s+Window\s+Source")
        rows = {cells[0]: cells[1] for line in output.splitlines()
                if len(cells := re.split(r"\s{2,}", line)) == 5}
        self.assertIn(f"Goal 1: {rows['goal 1 issues closed']} issues closed; "
                      + f"{rows['goal 1 declined closures excluded']} declined closures left out.", summary)
        self.assertIn(f"Hana: {rows['Hana merges / origin/init/catalyst']} merges to origin/init/catalyst; "
                      + f"{rows['goal 1 Hana-project issues closed']} Hana-project issues closed.", summary)
        self.assertIn(f"User attention: {rows['phone pushes sent']} pushes sent to user; "
                      + f"{rows['phone pushes after failed review']} after a failed text check.", summary)

    def test_full_cli_adds_waits_and_receipts_below_the_same_five_line_summary(self) -> None:
        sources = self.sources()
        sources.wait_history = m.parse_wait_report(wait_report_fixture(
            self.window, [wait_fixture("user-wait", "WaitingForUser", self.window.since, 8)],
            [wait_phase_fixture(wait_n=1)],
        ))
        sources.merges = [m.HanaMerge("a" * 40, self.window.since), m.HanaMerge("c" * 40, self.window.since)]
        sources.receipts = (m.parse_receipt(receipt_fixture("a" * 40, self.window.since)),)
        summary = self.output(sources)
        output = self.output(sources, full=True)
        self.assertEqual(len(summary.splitlines()), 5)
        self.assertTrue(output.startswith(summary + "\nCapacity on Hana\n"))
        self.assertNotIn("Hana wait /", summary)
        rows = {cells[0]: cells for line in output.splitlines()
                if len(cells := re.split(r"\s{2,}", line)) == 5}
        self.assertEqual(rows["Hana wait / WaitingForUser"][1:3], ["8.0 min", "1"])
        expected_kinds = {
            "Hana wait / WaitingForQuotaRefusal", "Hana wait / WaitingForMemoryOrSlot",
            "Hana wait / WaitingForMergeGate", "Hana wait / WaitingForUser",
            "Hana wait / WaitingForSupplier", "Hana wait / ReadyWithoutCapacity",
            "Hana wait / share of phase time with a recorded kind",
        }
        self.assertEqual({name for name in rows if name.startswith("Hana wait / ")}, expected_kinds)
        self.assertIn("ReceiptMissing", output)
        self.assertIn("c" * 40, output)

    def test_missing_merge_history_prints_not_recorded_in_summary_and_table(self) -> None:
        sources = self.sources()
        sources.merges = m.MergeHistoryNotRecorded.MISSING
        output = self.output(sources, full=True)
        summary = output.split("\n\n", 1)[0]
        self.assertIn("Hana: not recorded merges to origin/init/catalyst; 2 Hana-project issues closed.", summary)
        self.assertTrue(all(len(line) <= 100 for line in summary.splitlines()), summary)
        self.assertRegex(output, r"Hana merges / origin/init/catalyst\s+not recorded\s+0\s+")

    def test_source_problem_count_and_full_hint_show_with_and_without_problems(self) -> None:
        sources = self.sources()
        self.assertIn("Source problems: 0. Use --full to print the tables.", self.output(sources))
        sources.problems = [log_records.SourceProblem("reviews", "missing review file"),
                            log_records.SourceProblem("builds", "cannot read build file")]
        summary = self.output(sources)
        self.assertIn("Source problems: 2. Use --full to print the tables.", summary)
        self.assertNotIn("missing review file", summary)
        output = self.output(sources, full=True)
        problem_table = output.split("Source problems (dependent counts can be incomplete)\n", 1)[1]
        self.assertEqual(problem_table.count("source unavailable"), 2)
        self.assertIn("missing review file", problem_table)
        self.assertIn("cannot read build file", problem_table)


class SourcesTest(unittest.TestCase):
    def test_merge_count_uses_fixture_ref_commit_dates_and_exact_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(arguments: list[str], at: str) -> str:
                env = dict(os.environ, GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at,
                           GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.test",
                           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.test")
                result = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
                                         "-c", "commit.gpgsign=false", *arguments],
                                        env=env, check=True, capture_output=True, text=True)
                return result.stdout.strip()

            _ = git(["init", "--initial-branch=main"], "2026-10-12T12:00:00Z")
            _ = git(["commit", "--allow-empty", "-m", "start"], "2026-10-12T12:00:00Z")
            for index, at in enumerate(("2026-10-13T21:23:59Z", "2026-10-13T21:24:00Z",
                                        "2026-10-15T12:00:00Z", "2026-10-20T21:24:00Z")):
                _ = git(["checkout", "-b", f"topic-{index}"], at)
                _ = git(["commit", "--allow-empty", "-m", "work"], at)
                if index == 2:
                    _ = git(["checkout", "-b", "nested"], at)
                    _ = git(["commit", "--allow-empty", "-m", "nested work"], at)
                    _ = git(["checkout", f"topic-{index}"], at)
                    _ = git(["merge", "--no-ff", "nested", "-m", "nested merge"], at)
                _ = git(["checkout", "main"], at)
                _ = git(["merge", "--no-ff", f"topic-{index}", "-m", "merge"], at)
            _ = git(["update-ref", "refs/remotes/origin/init/catalyst", "HEAD"], "2026-10-20T21:24:00Z")
            before = git(["rev-parse", "HEAD"], "2026-10-20T21:24:00Z")
            merges = m.read_hana_merges(repo)
            window = log_records.window_bounds("2026-10-13T17:24", "2026-10-20T17:24", log_records.required_time("2026-10-21"))
            rows = m.merge_flow(merges, window)
            self.assertEqual((rows[0].value, rows[0].n), ("2", 2))
            days = {row.measure: row.value for row in rows[1:]}
            self.assertEqual(days["Hana merges / 2026-10-13"], "1")
            self.assertEqual(days["Hana merges / 2026-10-15"], "1")
            self.assertEqual(days["Hana merges / 2026-10-20"], "0")
            self.assertEqual(sum(row.n for row in rows[1:]), rows[0].n)
            self.assertIn("--first-parent", rows[0].source)
            self.assertEqual(git(["rev-parse", "HEAD"], "2026-10-20T21:24:00Z"), before)
            self.assertEqual(m.merge_flow([], window)[0].value, "0")
            self.assertEqual(m.merge_flow(m.MergeHistoryNotRecorded.MISSING, window)[0].value, "not recorded")

    def test_running_phase_uses_newest_run_and_removes_finished_phases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def save(name: str, tree: str, at: int, events: list[log_records.JsonObject]) -> None:
                records: list[log_records.JsonObject] = [{"event_type": "run_started", "working_dir": tree, "run_started_at": at}, *events]
                _ = (root / name).write_text("\n".join(json.dumps(record) for record in records))

            phase_a: log_records.JsonObject = {"phase_instance_id": "A", "phase_id": "1", "phase_title": "First",
                                     "phase_started_at": 1}
            phase_b: log_records.JsonObject = {"phase_instance_id": "B", "phase_id": "2", "phase_title": "Second",
                                     "phase_started_at": 2}
            save("old.jsonl", "/done", 1, [{"event_type": "phase_started", **phase_a}])
            save("new.jsonl", "/done", 2, [{"event_type": "phase_started", **phase_a},
                                             {"event_type": "phase_finished", **phase_a}])
            save("live.jsonl", "/live", 1, [{"event_type": "phase_started", **phase_a},
                                              {"event_type": "phase_started", **phase_b},
                                              {"event_type": "phase_finished", **phase_b}])
            save("repeated.jsonl", "/repeated", 1, [{"event_type": "phase_started", **phase_a},
                                                      {"event_type": "phase_started", **phase_b, "phase_id": "1"},
                                                      {"event_type": "phase_finished", **phase_b, "phase_id": "1"}])
            save("finished.jsonl", "/finished", 1, [{"event_type": "phase_started", **phase_a}, {"event_type": "run_finished"}])
            problems: list[log_records.SourceProblem] = []
            self.assertEqual(m.running_phase_worktrees(root, problems), {"/live", "/repeated"})
            self.assertEqual(problems, [])

    def test_running_phase_uses_phase_id_fallback_and_ignores_missing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records: dict[str, list[log_records.JsonObject]] = {
                "fallback": [{"event_type": "phase_started", "phase_id": "1"},
                             {"event_type": "phase_started", "phase_instance_id": "", "phase_id": "2"},
                             {"event_type": "phase_finished", "phase_id": "2"}],
                "done": [{"event_type": "phase_started", "phase_id": "1"},
                         {"event_type": "phase_finished", "phase_instance_id": "", "phase_id": "1"}],
                "missing-start": [{"event_type": "phase_started"},
                                  {"event_type": "phase_started", "phase_instance_id": "", "phase_id": ""}],
                "missing-finish": [{"event_type": "phase_started", "phase_instance_id": "A"},
                                   {"event_type": "phase_finished"}],
            }
            for name, events in records.items():
                run: log_records.JsonObject = {"event_type": "run_started", "working_dir": f"/{name}", "run_started_at": 1}
                _ = (root / f"{name}.jsonl").write_text("\n".join(json.dumps(record) for record in [run, *events]))
            problems: list[log_records.SourceProblem] = []
            self.assertEqual(m.running_phase_worktrees(root, problems), {"/fallback", "/missing-finish"})
            self.assertCountEqual([problem.source for problem in problems],
                                  [str(root / "missing-start.jsonl")] * 2 + [str(root / "missing-finish.jsonl")])
            self.assertTrue(all("phase" in problem.reason and "not recorded" in problem.reason for problem in problems))

    def test_json_source_missing_and_bad_rows_report_source_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log_records.jsonl"
            problems: list[log_records.SourceProblem] = []
            self.assertEqual(m.read_source(str(path), lambda: m.json_lines(path, m.parse_message), [], problems), [])
            self.assertIn("missing", problems[0].reason)
            _ = path.write_text('{"time":"2026-10-13T00:00:00Z"}\nnot json\n')
            self.assertEqual(m.read_source(str(path), lambda: m.json_lines(path, m.parse_message), [], problems), [])
            self.assertIn("1 invalid JSON or field rows", problems[-1].reason)
            now = datetime(2026, 10, 20, tzinfo=timezone.utc)
            rows = log_records.source_problem_rows(problems, log_records.Window(now - timedelta(days=7), now))
            self.assertEqual(rows[0].source, str(path))
            self.assertIn("unreadable", rows[1].value)

    def test_fixture_loaders_read_issues_changes_questions_and_efficiency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault, state = root / "vault", root / "state"
            folders = (vault / "issues", vault / "change-review", state / "nightly-review", state / "plan-backlog",
                       state / "notify/questions", state / "showrunner/outstanding")
            for folder in folders:
                folder.mkdir(parents=True)
            _ = (vault / "issues/open.md").write_text('---\nstatus: open\ntags: [proposal]\ndate_created: "[[2026-10-01]]"\n---\n')
            _ = (vault / "issues/approved.md").write_text('---\nstatus: open\ntags: [proposal]\nunit: one\nplan: one.md\n---\n')
            _ = (vault / "change-review/one.md").write_text(
                '## To review\n\n| # | Change | Merged | How to check |\n| --- | --- | --- | --- |\n' +
                '| C1 | change | 2026-10-02 · abc | check |\n\n## Reviewed\n'
            )
            _ = (state / "plan-backlog/ledger.md").write_text("- 2026-10-02 A new plan — proposed\n")
            _ = (state / "nightly-review/ledger.md").write_text(
                '- 2026-10-03 config: speed up — proposed\n- 2026-10-04 config: done — accepted\n'
            )
            _ = (state / "notify/questions/one.json").write_text('{"question":"Held push review; not an open question"}')
            _ = (state / "showrunner/outstanding/hana.json").write_text(
                '[{"since":"2026-10-05T12:00","text":"Which?","question":"Which size?"},' +
                '{"since":"2026-10-01T12:00","text":"A reminder"}]'
            )
            found = m.read_issues(vault / "issues")
            problems: list[log_records.SourceProblem] = []
            waiting = m.read_waiting(state, vault, found, problems)
            self.assertEqual(problems, [])
            self.assertEqual([item.kind for item in waiting], ["changes", "proposals", "proposals", "efficiency items", "questions"])
            self.assertEqual(waiting[0].opened, log_records.moment("2026-10-02"))
            self.assertEqual(waiting[-1].opened, log_records.moment("2026-10-05T12:00"))
            snapshot = log_records.required_time("2026-10-07T12:00-04:00")
            window = log_records.Window(snapshot - timedelta(days=7), snapshot)
            rows = m.pending_attention(waiting, snapshot, window)
            self.assertEqual(rows[3].value, "1; oldest 2.0 days")
            self.assertIn("showrunner/outstanding", rows[3].source)

    def test_change_review_without_pending_section_reports_problem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changes = root / "vault/change-review"
            changes.mkdir(parents=True)
            path = changes / "bad.md"
            _ = path.write_text("# Review list\n\n## Reviewed\n")
            problems: list[log_records.SourceProblem] = []
            waiting = m.read_waiting(root / "state", root / "vault", [], problems)
            self.assertEqual(waiting, [])
            issue = next(problem for problem in problems if problem.source == str(path))
            self.assertIn("no `## To review` section", issue.reason)
            now = log_records.required_time("2026-10-20")
            rows = log_records.source_problem_rows([issue], log_records.Window(now - timedelta(days=7), now))
            self.assertEqual(rows[0].source, str(path))
            self.assertIn("unreadable", rows[0].value)

    def test_phase_loader_reports_each_corrupt_file_and_keeps_valid_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid: log_records.JsonObject = {
                "event_type": "phase_finished", "status": "completed", "event_id": "one",
                "timestamp": "2026-10-14T00:00:00Z", "working_dir": "/deleted/tool-based-ui-demo",
                "phase_elapsed_seconds": 60,
            }
            first, second = root / "one.jsonl", root / "two.jsonl"
            _ = first.write_text(json.dumps(valid) + "\n{bad json\n[]\n")
            _ = second.write_text("broken\n")
            problems: list[log_records.SourceProblem] = []
            phases = m.read_phases(root, problems)
            self.assertEqual([(phase.identity, phase.seconds) for phase in phases], [("one", 60)])
            counts = {problem.source: problem.reason for problem in problems}
            self.assertIn("2 invalid JSON rows", counts[str(first)])
            self.assertIn("1 invalid JSON rows", counts[str(second)])

    def test_deleted_hana_worktree_phases_use_record_provenance_and_known_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deleted_tree = root / "tool-based-ui-startup-polish"
            deleted_tree.mkdir()
            deleted_tree.rmdir()
            base: log_records.JsonObject = {
                "event_type": "phase_finished", "status": "completed", "timestamp": "2026-10-14T00:00:00Z",
                "phase_elapsed_seconds": 60,
            }
            origins: list[log_records.JsonObject] = [
                {"working_dir": str(deleted_tree)},
                {"working_dir": str(root / "unknown"), "plan_doc": "docs/plans/tool-based-ui-demo.md"},
                {"working_dir": str(root / "unknown"), "production": "hana"},
                {"working_dir": str(root / "unknown"), "repository": "/archive/hana_catalyst"},
                {"working_dir": str(root / "widget-enhancements-retired")},
                {"working_dir": str(root / "other")},
            ]
            records = [{**base, **origin, "event_id": str(index)} for index, origin in enumerate(origins)]
            _ = (root / "events.jsonl").write_text("\n".join(json.dumps(record) for record in records))
            problems: list[log_records.SourceProblem] = []
            phases = m.read_phases(root, problems, ["widget-enhancements"])
            start = log_records.required_time("2026-10-13T00:00:00Z")
            rows = m.phase_flow(phases, log_records.Window(start, start + timedelta(days=7)))
            self.assertEqual(rows[0].n, 5)
            self.assertEqual(rows[1].value, "median 1.0 min; p90 1.0 min")
            self.assertEqual(problems, [])

    def test_invalid_source_fields_report_problem_instead_of_zero_or_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rankings.jsonl"
            _ = path.write_text('{"name":"one","rank":"first"}\n')
            problems: list[log_records.SourceProblem] = []
            ranks = m.read_source(str(path), lambda: m.json_lines(path, m.parse_rank), [], problems)
            self.assertEqual(ranks, [])
            self.assertIn("1 invalid JSON or field rows", problems[0].reason)
            self.assertRaisesRegex(ValueError, "from: expected text", m.parse_message,
                                   {"from": 123, "to": "unit", "time": "2026-10-14"})
            self.assertRaisesRegex(ValueError, "timestamp", audit.parse_push, {"time": "bad stamp", "outcome": "sent"})
            self.assertRaisesRegex(ValueError, "phase_elapsed_seconds", m.parse_phase,
                                   {"event_type": "phase_finished", "status": "completed",
                                    "timestamp": "2026-10-14", "phase_elapsed_seconds": "one"}, ())

    def test_sqlite_loaders_filter_machine_join_cwd_and_keep_store_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            with closing(sqlite3.connect(path)) as db:
                _ = db.executescript(
                    'CREATE TABLE steps(repo,step,started_at,duration_s,host); ' +
                    'CREATE TABLE turns(machine,family,parent_id,agent_id,at,input,cache_write_5m,cache_write_1h,cache_read,output); ' +
                    'CREATE TABLE folders(machine,family,parent_id,agent_id,folder);'
                )
                _ = db.execute("INSERT INTO steps VALUES ('hana','nextest','2026-10-14',120,'natedev')")
                _ = db.execute("INSERT INTO steps VALUES ('hana','nextest','2026-10-14',999,'mac')")
                _ = db.execute("INSERT INTO turns VALUES ('natedev','codex','','a','2026-10-14',1,2,3,4,5)")
                _ = db.execute("INSERT INTO turns VALUES ('natedev','codex','','a','2026-10-15',1,1,1,1,1)")
                _ = db.execute("INSERT INTO turns VALUES ('mac','codex','','a','2026-10-14',9,9,9,9,9)")
                _ = db.execute("INSERT INTO turns VALUES ('natedev','codex','','a','2026-10-01',99,99,99,99,99)")
                _ = db.execute("INSERT INTO turns VALUES ('natedev','codex','','a','2026-10-20',99,99,99,99,99)")
                db.commit()
            before = path.read_bytes()
            self.assertEqual(m.read_builds(path)[0].seconds, 120)
            start = log_records.required_time("2026-10-13T00:00:00Z")
            window = log_records.Window(start, start + timedelta(days=7))
            tokens = m.read_tokens(path, window)
            self.assertEqual(tokens[0].tokens, 20)
            self.assertEqual(tokens[0].repository, "cwd not recorded")
            self.assertEqual([(turn.window, turn.turns) for turn in tokens], [(window, 2)])
            self.assertEqual(path.read_bytes(), before)

    def test_build_minutes_keep_steps_with_an_unrecorded_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "builds.sqlite"
            with closing(sqlite3.connect(path)) as db:
                _ = db.execute("CREATE TABLE steps(repo,step,started_at,duration_s,host)")
                _ = db.execute("INSERT INTO steps VALUES (NULL,'check','2026-10-14',180,'natedev')")
                _ = db.execute("INSERT INTO steps VALUES ('hana','nextest','2026-10-14',120,'natedev')")
                db.commit()
            start = log_records.required_time("2026-10-13")
            rows = m.resource_use(m.read_builds(path), [], log_records.Window(start, start + timedelta(days=7)))
            build_rows = {row.measure: (row.value, row.n) for row in rows if "minutes /" in row.measure}
            self.assertEqual(build_rows, {
                "test minutes / hana": ("2.0", 1),
                "build/check minutes / repository not recorded": ("3.0", 1),
            })

    def test_phase_loader_and_repository_worktree_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "events.jsonl").write_text(
                '{"event_type":"phase_finished","status":"completed","timestamp":"2026-10-14T00:00:00Z",' +
                '"working_dir":"/deleted/tool-based-ui-demo","phase_elapsed_seconds":60}\n'
            )
            self.assertEqual(m.read_phases(root, [])[0].seconds, 60)
            repo, tree = root / "repo", root / "tree"
            gitdir = repo / ".git/worktrees/tree"
            gitdir.mkdir(parents=True)
            tree.mkdir()
            _ = (gitdir / "commondir").write_text('../..')
            _ = (tree / ".git").write_text(f'gitdir: {gitdir}')
            self.assertEqual(m.repository(str(tree)), str(repo))


if __name__ == "__main__":
    _ = unittest.main()
