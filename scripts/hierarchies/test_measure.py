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
        "ignored_n": 0, "wait_without_id_n": 0,
        "phases": list(phases), "waits": list(waits), "missing": list(missing),
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


def holder_observation(at: datetime, holder: log_records.JsonObject) -> log_records.JsonObject:
    return {"at": at.isoformat(), "epoch": at.timestamp(), "holder": holder}


def named_holder_fixture(resource_kind: str = "slot", resource: str = "natedev/slot-1") -> log_records.JsonObject:
    return {"state": "NamedHolder", "resource_kind": resource_kind, "resource": resource,
            "holder": {"state": "SessionHolder", "session_id": "other-project-director"}}


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
        kind_rows = [row for row in rows if row.measure in {"Hana wait / " + kind.value for kind in m.WaitKind}]
        self.assertEqual([(row.value, row.n) for row in kind_rows],
                         [("not recorded (HistoryNotRecorded)", 0)] * 6)
        self.assertIn("Hana wait coverage / HistoryNotRecorded", [row.measure for row in rows])
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

    def completion(self, run: str, phase: str, at: datetime, minutes: float) -> m.PhaseCompletion:
        record: log_records.JsonObject = {
            "event_type": "phase_finished", "event_id": run + ":" + phase + ":finished", "status": "completed",
            "run_id": run, "phase_instance_id": phase, "working_dir": "/archive/hana_catalyst-fixture",
            "timestamp": at.isoformat(), "phase_elapsed_seconds": minutes * 60,
        }
        return m.parse_phase(record, ())[0]

    def test_handoff_total_marks_missing_phase_history_as_partial(self) -> None:
        handoff = wait_fixture("handoff-with-incomplete-history", "WaitingForMergeGate", self.start, 10)
        for key in ("run_id", "phase_id", "phase_instance_id", "working_dir"):
            del handoff[key]
        handoff["wait_attribution"] = {"state": "ProductionHandoff", "reason": "checkpoint handoff", "candidates": []}
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [handoff], [wait_phase_fixture("HistoryNotRecorded")]))
        rows = {row.measure: row for row in m.phase_flow([], self.window, history)}
        total = rows["Hana wait / production handoffs without a phase"]
        self.assertEqual((total.value, total.n), ("10.0 min (partial)", 1))
        coverage = rows["Hana wait / coverage / HistoryNotRecorded"]
        self.assertEqual((coverage.value, coverage.n), ("1", 1))

    def test_pre_window_missing_kind_has_one_coverage_count_for_its_completed_phase(self) -> None:
        earlier = wait_fixture("earlier-kind-missing", "", self.start - timedelta(minutes=30), 10)
        current = wait_fixture("current-known-kind", "WaitingForUser", self.start, 5)
        unrelated = wait_fixture("earlier-other-phase", "", self.start - timedelta(minutes=20), 5)
        unrelated.update(phase_instance_id="not-in-denominator")
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [earlier, current, unrelated], [wait_phase_fixture(wait_n=2)]))
        event = self.completion("hana-run", "hana-phase", self.start + timedelta(hours=1), 120)
        rows = m.phase_flow([event], self.window, history)
        share = next(row for row in rows if row.measure == "Hana wait / share of phase time with a recorded kind")
        self.assertEqual((share.value, share.n), ("4.2% (partial)", 1))
        coverage = [row for row in rows if row.measure == "Hana wait / coverage / WaitKindNotRecorded"]
        self.assertEqual([(row.value, row.n) for row in coverage], [("1", 1)])
        holder_coverage = [row for row in rows if row.measure == "Hana wait holders / coverage / WaitKindNotRecorded"]
        self.assertEqual([(row.value, row.n) for row in holder_coverage], [("1", 1)])
        known = next(row for row in rows if row.measure == "Hana wait / WaitingForUser")
        self.assertEqual((known.value, known.n), ("5.0 min (partial)", 1))
        holder_known = next(row for row in rows if row.measure == "Hana wait holders / WaitingForUser / HolderNotNamed")
        self.assertEqual((holder_known.value, holder_known.n), ("5.0 min (partial)", 1))
        self.assertFalse(any(row.measure == "Hana wait / WaitKindNotRecorded" for row in rows))
        self.assertFalse(any(row.measure.startswith("Hana wait holders unnamed / WaitKindNotRecorded") for row in rows))

    def test_wait_share_uses_exact_phase_interval_despite_truncated_recorded_seconds(self) -> None:
        finished = self.start + timedelta(seconds=60.8)
        wait = wait_fixture("fractional-phase-wait", "WaitingForUser", self.start, 60.8 / 60)
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)]))
        event = m.PhaseCompletion("fractional-finish", finished, True, 60,
                                  phase=m.WaitPhaseInstance("hana-run", "hana-phase"), started=self.start)
        rows = {row.measure: row for row in m.phase_flow([event], self.window, history)}
        share = rows["Hana wait / share of phase time with a recorded kind"]
        self.assertEqual((share.value, share.n), ("100.0%", 1))
        self.assertEqual((rows["Hana phase time"].value, rows["Hana phase time"].n),
                         ("median 1.0 min; p90 1.0 min", 1))

    def test_phase_done_and_duration_counts_share_the_phase_instance_key(self) -> None:
        for seconds in (600, m.DurationNotRecorded.MISSING):
            with self.subTest(seconds=seconds):
                events = [m.PhaseCompletion(identity, self.start + timedelta(minutes=10), True, seconds,
                                            phase=m.WaitPhaseInstance("hana-run", "hana-phase"), started=self.start)
                          for identity in ("finish-first", "finish-repeated")]
                rows = {row.measure: row for row in m.phase_flow(events, self.window)}
                self.assertEqual((rows["Hana phases done"].value, rows["Hana phases done"].n), ("1", 1))
                self.assertEqual(rows["Hana phase time"].n, 0 if isinstance(seconds, m.DurationNotRecorded) else 1)

    def test_real_supplier_boundary_phase_keeps_current_missing_history_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            runs.mkdir()
            began = self.start - timedelta(hours=1)
            finished = self.start + timedelta(hours=1)
            started = self.phase_started()
            started.update(timestamp=began.isoformat(), timestamp_epoch=began.timestamp())
            early = wait_fixture("boundary-earlier", "WaitingForUser", self.start - timedelta(minutes=30), 5)
            current = wait_fixture("boundary-current", "WaitingForUser", self.start + timedelta(minutes=20), 5)
            missing = wait_fixture("boundary-missing", "WaitingForUser", self.start + timedelta(minutes=10))
            missing.update(event_type="wait_not_recorded", wait_end={"state": "NotRecorded"},
                           wait_record_state={"state": "NotRecorded", "reason": "producer could not record the wait"})
            completion: log_records.JsonObject = {
                "schema_version": 1, "event_type": "phase_finished", "event_id": "boundary-finish",
                "run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase",
                "status": "completed", "timestamp": finished.isoformat(), "timestamp_epoch": finished.timestamp(),
                "phase_elapsed_seconds": 7200,
            }
            path = runs / "hana-run.jsonl"
            _ = path.write_text("\n".join(json.dumps(record) for record in
                                         [started, early, current, missing, completion]) + "\n")
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            phases = m.read_phases(runs, problems)
            history = m.read_wait_history(root, self.window, problems, (), phases)
            self.assertIsInstance(history, m.HanaWaitHistory)
            if isinstance(history, m.HanaWaitHistory):
                self.assertEqual(sorted(wait.identity for wait in history.waits),
                                 ["boundary-current", "boundary-earlier", "boundary-missing"])
                self.assertEqual(len(history.phases), 1)
            rows = m.phase_flow(phases, self.window, history)
            share = next(row for row in rows if row.measure == "Hana wait / share of phase time with a recorded kind")
            self.assertEqual((share.value, share.n), ("8.3% (partial)", 1))
            coverage = [row for row in rows if row.measure == "Hana wait / coverage / HistoryNotRecorded"]
            self.assertEqual([(row.value, row.n) for row in coverage], [("1", 1)])
            current_total = next(row for row in rows if row.measure == "Hana wait / WaitingForUser")
            self.assertEqual((current_total.value, current_total.n), ("5.0 min (partial)", 1))
            self.assertEqual(problems, [])
            self.assertEqual(path.read_bytes(), before)

    def test_wait_share_joins_only_phases_with_recorded_time_in_the_denominator(self) -> None:
        included = wait_fixture("included", "WaitingForUser", self.start, 10)
        excluded = wait_fixture("finishes-later", "WaitingForUser", self.start, 80)
        excluded.update(run_id="later-run")
        same_phase_other_run = wait_fixture("same-phase-other-run", "WaitingForUser", self.start, 70)
        same_phase_other_run.update(run_id="other-run")
        missing_duration = wait_fixture("no-phase-duration", "WaitingForUser", self.start, 60)
        missing_duration.update(run_id="duration-missing")
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [included, excluded, same_phase_other_run, missing_duration], [wait_phase_fixture(wait_n=1)]))
        unknown = m.parse_phase({
            "event_type": "phase_finished", "status": "completed", "event_id": "unknown-duration",
            "run_id": "duration-missing", "phase_instance_id": "hana-phase", "production": "hana",
            "timestamp": (self.start + timedelta(hours=1)).isoformat(),
        }, ())[0]
        events = [self.completion("hana-run", "hana-phase", self.start + timedelta(hours=1), 100),
                  self.completion("later-run", "hana-phase", self.window.until, 100), unknown]
        rows = {row.measure: row for row in m.phase_flow(events, self.window, history)}
        self.assertEqual((rows["Hana phase time"].value, rows["Hana phase time"].n),
                         ("median 100.0 min; p90 100.0 min", 1))
        share = rows["Hana wait / share of phase time with a recorded kind"]
        self.assertEqual((share.value, share.n), ("10.0%", 1))

    def test_wait_share_counts_overlapping_kinds_once_per_phase(self) -> None:
        waits = [wait_fixture("first", "WaitingForUser", self.start, 20),
                 wait_fixture("second", "WaitingForMemoryOrSlot", self.start + timedelta(minutes=10), 20),
                 wait_fixture("nested", "WaitingForMergeGate", self.start + timedelta(minutes=12), 3)]
        history = m.parse_wait_report(wait_report_fixture(self.window, waits, [wait_phase_fixture(wait_n=3)]))
        event = self.completion("hana-run", "hana-phase", self.start + timedelta(hours=1), 60)
        rows = {row.measure: row for row in m.phase_flow([event], self.window, history)}
        share = rows["Hana wait / share of phase time with a recorded kind"]
        self.assertEqual((share.value, share.n), ("50.0%", 1))
        self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                          rows["Hana wait / WaitingForUser"].n), ("20.0 min", 1))
        self.assertEqual((rows["Hana wait / WaitingForMemoryOrSlot"].value,
                          rows["Hana wait / WaitingForMemoryOrSlot"].n), ("20.0 min", 1))

    def test_wait_union_keeps_simultaneous_time_in_distinct_phases(self) -> None:
        first = wait_fixture("first-phase", "WaitingForUser", self.start, 20)
        second = wait_fixture("second-phase", "WaitingForMemoryOrSlot", self.start, 30)
        second.update(run_id="second-run")
        second_phase = wait_phase_fixture(wait_n=1)
        second_phase.update(run_id="second-run")
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [first, second], [wait_phase_fixture(wait_n=1), second_phase]))
        finished = self.start + timedelta(hours=1)
        events = [self.completion("hana-run", "hana-phase", finished, 60),
                  self.completion("second-run", "hana-phase", finished, 60)]
        rows = {row.measure: row for row in m.phase_flow(events, self.window, history)}
        share = rows["Hana wait / share of phase time with a recorded kind"]
        self.assertEqual((share.value, share.n), ("41.7%", 2))
        self.assertEqual(rows["Hana phase time"].n, 2)

    def test_phase_free_handoff_stays_visible_without_adding_to_wait_share(self) -> None:
        phase_wait = wait_fixture("phase-wait", "WaitingForUser", self.start, 10)
        handoff = wait_fixture("separate-handoff", "WaitingForMergeGate", self.start, 90)
        for key in ("run_id", "phase_id", "phase_instance_id", "working_dir"):
            del handoff[key]
        handoff["wait_attribution"] = {"state": "ProductionHandoff", "reason": "checkpoint handoff", "candidates": []}
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [phase_wait, handoff], [wait_phase_fixture(wait_n=1)]))
        event = self.completion("hana-run", "hana-phase", self.start + timedelta(hours=1), 100)
        rows = m.phase_flow([event], self.window, history)
        share = next(row for row in rows if row.measure == "Hana wait / share of phase time with a recorded kind")
        self.assertEqual((share.value, share.n), ("10.0%", 1))
        handoff_rows = [row for row in rows if row.measure == "Hana wait / production handoff"]
        self.assertEqual(len(handoff_rows), 1)
        self.assertEqual((handoff_rows[0].value, handoff_rows[0].n), ("no phase; owner=hana-director", 1))
        self.assertIn("separate-handoff", handoff_rows[0].source)
        merge_wait = next(row for row in rows if row.measure == "Hana wait / WaitingForMergeGate")
        self.assertEqual((merge_wait.value, merge_wait.n), ("90.0 min", 1))
        handoff_total = next(row for row in rows if row.measure == "Hana wait / production handoffs without a phase")
        self.assertEqual((handoff_total.value, handoff_total.n), ("90.0 min", 1))
        self.assertIn("excluded from phase share", handoff_total.source)

    def test_real_supplier_keeps_earlier_waits_and_retired_phase_origin_for_window_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            runs.mkdir()
            began = self.start - timedelta(hours=1)
            finished = self.start + timedelta(minutes=10)
            started = self.phase_started()
            started.update(worktree="widget-enhancements", working_dir="/archive/widget-enhancements",
                           plan_doc="docs/plans/tool-based-ui-widget.md", production="tools",
                           timestamp=began.isoformat(), timestamp_epoch=began.timestamp())
            done = wait_fixture("before-window", "WaitingForSupplier", self.start - timedelta(minutes=40), 20)
            done.update(working_dir="/archive/widget-enhancements", unit="widget-enhancements")
            opened = {**done, "event_id": "before-window-open", "event_type": "wait_started",
                      "timestamp": log_records.field_text(done, "wait_started_at"),
                      "timestamp_epoch": done["wait_started_epoch"], "wait_end": {"state": "Open"}}
            completion: log_records.JsonObject = {
                "schema_version": 1, "event_type": "phase_finished", "event_id": "phase-finish",
                "run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase",
                "working_dir": "/archive/widget-enhancements", "status": "completed",
                "timestamp": finished.isoformat(), "timestamp_epoch": finished.timestamp(),
                "phase_elapsed_seconds": 70 * 60,
            }
            path = runs / "hana-run.jsonl"
            _ = path.write_text("\n".join(json.dumps(record) for record in [started, opened, done, completion]) + "\n")
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            phases = m.read_phases(runs, problems)
            history = m.read_wait_history(root, self.window, problems, (), phases)
            self.assertEqual([(phase.hana, phase.seconds) for phase in phases], [(True, 4200)])
            self.assertEqual(phases[0].phase, m.WaitPhaseInstance("hana-run", "hana-phase"))
            self.assertEqual(phases[0].started, began)
            self.assertIsInstance(history, m.HanaWaitHistory)
            if isinstance(history, m.HanaWaitHistory):
                self.assertIn("before-window", [wait.identity for wait in history.waits])
            sources = BaselineOutputTest().sources()
            sources.phases = phases
            sources.wait_history = history
            sources.problems.extend(problems)
            with patch.object(m, "load_sources", return_value=sources), redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(m.main(["baseline", "--full", "--since", self.start.isoformat(),
                                         "--until", self.window.until.isoformat()]), 0)
            output = captured.getvalue()
            rows = {row.measure: row for row in m.phase_flow(phases, self.window, history)}
            share = rows["Hana wait / share of phase time with a recorded kind"]
            self.assertEqual((share.value, share.n), ("28.6%", 1))
            self.assertIn("28.6%", output)
            self.assertIn("median 70.0 min; p90 70.0 min", output)
            self.assertEqual(problems, [])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([item.relative_to(root) for item in root.rglob("*") if item.is_file()],
                             [Path("runs/hana-run.jsonl")])

    def test_missing_phase_histories_group_only_matching_reasons(self) -> None:
        phases: list[log_records.JsonObject] = []
        for index, reason in enumerate(("phase start has no wait coverage record",) * 3 + ("older phase history",)):
            phase = wait_phase_fixture("HistoryNotRecorded")
            phase.update(run_id=f"run-{index}", phase_instance_id=f"phase-{index}", reason=reason)
            phases.append(phase)
        history = m.parse_wait_report(wait_report_fixture(self.window, [], phases))
        rows = [row for row in m.hana_wait_rows(history, self.window, ())
                if row.measure == "Hana wait coverage / HistoryNotRecorded"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.value: row.n for row in rows}, {
            "phase start has no wait coverage record": 3, "older phase history": 1,
        })
        self.assertTrue(all(row.window == self.window.label for row in rows))

    def test_wait_share_uses_the_phase_time_population_and_only_completed_known_kinds(self) -> None:
        done = wait_fixture("done", "WaitingForSupplier", self.start, 15)
        unknown_cause = wait_fixture("no-cause", "", self.start, 80)
        open_wait = wait_fixture("open", "WaitingForUser", self.start, 90)
        open_wait["wait_end"] = {"state": "Open"}
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [done, unknown_cause, open_wait], [wait_phase_fixture(wait_n=3)]))
        first = m.PhaseCompletion("first", self.start + timedelta(hours=1), True, 3600,
                                  phase=m.WaitPhaseInstance("hana-run", "hana-phase"), started=self.start)
        phases = [first, first, m.PhaseCompletion("second", self.start + timedelta(hours=1), True, 2400,
                                                   phase=m.WaitPhaseInstance("hana-run", "second-phase"),
                                                   started=self.start + timedelta(minutes=20)),
                  m.PhaseCompletion("other", self.start, False, 100000,
                                    phase=m.WaitPhaseNotRecorded("completion phase identity not recorded"),
                                    started=log_records.TimeNotRecorded.MISSING),
                  m.PhaseCompletion("late", self.window.until, True, 100000,
                                    phase=m.WaitPhaseNotRecorded("completion phase identity not recorded"),
                                    started=log_records.TimeNotRecorded.MISSING),
                  m.PhaseCompletion("undated-duration", self.start, True, m.DurationNotRecorded.MISSING,
                                    phase=m.WaitPhaseNotRecorded("completion phase identity not recorded"),
                                    started=log_records.TimeNotRecorded.MISSING)]
        rows = {row.measure: row for row in m.phase_flow(phases, self.window, history)}
        share = rows["Hana wait / share of phase time with a recorded kind"]
        self.assertEqual(share.n, rows["Hana phase time"].n)
        self.assertEqual(share.n, 2)
        self.assertEqual(share.value, "15.0% (partial)")
        count = rows["Hana wait / coverage / WaitKindNotRecorded"]
        self.assertEqual((count.value, count.n), ("1", 1))
        self.assertIn("progress_history.py", share.source)

    def test_wait_share_distinguishes_missing_history_from_a_recorded_zero(self) -> None:
        phases = [m.PhaseCompletion("one", self.start + timedelta(minutes=10), True, 600,
                                        phase=m.WaitPhaseInstance("hana-run", "hana-phase"), started=self.start)]
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
            ([wait_phase_fixture(wait_n=1)], [done], ""),
            ([wait_phase_fixture("HistoryNotRecorded")], [done], "HistoryNotRecorded"),
            ([wait_phase_fixture("UncertainCoverage")], [done], "UncertainCoverage"),
            ([wait_phase_fixture(wait_n=1)], [done, unresolved], "attribution_miss_n"),
        )
        for phases, waits, expected in variants:
            with self.subTest(expected=expected):
                history = m.parse_wait_report(wait_report_fixture(self.window, waits, phases))
                rows = {row.measure: row for row in m.phase_flow(
                    [m.PhaseCompletion("one", self.start + timedelta(minutes=10), True, 600,
                                        phase=m.WaitPhaseInstance("hana-run", "hana-phase"), started=self.start)], self.window, history)}
                share = rows["Hana wait / share of phase time with a recorded kind"]
                self.assertEqual(share.value, "20.0% (partial)" if expected else "20.0%")
                self.assertEqual(rows["Hana wait / WaitingForUser"].value,
                                 "2.0 min (partial)" if expected else "2.0 min")
                coverage = [row for name, row in rows.items() if name.startswith("Hana wait / coverage /")]
                self.assertEqual([(row.measure, row.value, row.n) for row in coverage],
                                 [("Hana wait / coverage / " + expected, "1", 1)] if expected else [])
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
                self.assertGreater(phase_path.write_text("\n".join(json.dumps(row) for row in (
                    self.phase_started(), wait_fixture("valid", "WaitingForUser", self.start, 2))) + "\n"), 0)
                event = wait_fixture("lost-id", "WaitingForUser", self.start)
                del event["wait_id"]
                event["event_type"] = event_type
                external = root / "wait_attribution.jsonl"
                self.assertGreater(external.write_text(json.dumps(event) + "\n"), 0)
                before = external.read_bytes()
                problems: list[log_records.SourceProblem] = []
                history = m.read_source(str(external), lambda: m.read_wait_history(root, self.window, problems, (), ()),
                                        m.HistoryNotRecorded(), problems)
                self.assertIsInstance(history, m.HanaWaitHistory)
                output = log_records.table("Wait rows", m.hana_wait_rows(history, self.window, ()))
                self.assertIn("source problem", output)
                self.assertIn("wait_id", output)
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
                self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                                  rows["Hana wait / WaitingForUser"].n), ("2.0 min (partial)", 1))
                self.assertEqual(external.read_bytes(), before)

    def phase_started(self) -> log_records.JsonObject:
        return {
            "schema_version": 1, "event_type": "phase_started", "event_id": "phase-start",
            "run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase",
            "worktree": "/archive/hana_catalyst-fixture",
            "production": {"state": "Recorded", "value": "hana"},
            "repository": {"state": "NotRecorded", "reason": "phase working directory is unavailable"},
            "timestamp": self.start.isoformat(), "timestamp_epoch": self.start.timestamp(),
            "wait_coverage": {"state": "Recorded"},
        }

    def test_wait_report_attributes_recorded_origins_and_earlier_text_to_hana(self) -> None:
        for field, value in (("production", "hana"), ("repository", "hana_catalyst"),
                             ("plan_doc", "docs/plans/tool-based-ui-demo.md")):
            for tagged in (True, False):
                with self.subTest(field=field, tagged=tagged):
                    phase = wait_phase_fixture(wait_n=0)
                    phase["unit"] = "/archive/other-unit"
                    phase[field] = {"state": "Recorded", "value": value} if tagged else value
                    history = m.parse_wait_report(wait_report_fixture(self.window, [], [phase]))
                    self.assertEqual([(item.run, item.instance) for item in history.phases],
                                     [("hana-run", "hana-phase")])
                    self.assertEqual(history.phases[0].state, m.CoveredWaitHistory(0))

    def test_wait_report_does_not_attribute_not_recorded_origins_by_their_reason(self) -> None:
        phase = wait_phase_fixture()
        phase["unit"] = "/archive/other-unit"
        for field in ("plan_doc", "production", "repository"):
            phase[field] = {"state": "NotRecorded", "reason": "hana origin is unavailable"}
        history = m.parse_wait_report(wait_report_fixture(self.window, [], [phase]))
        self.assertEqual(history.phases, ())
        self.assertEqual(history.waits, ())

    def test_six_wait_kinds_report_their_own_completed_minutes_and_counts(self) -> None:
        kinds = (
            "WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
            "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity",
        )
        waits = [wait_fixture(str(index), kind, self.start, index)
                 for index, kind in enumerate(kinds, 1)]
        report = wait_report_fixture(self.window, waits, [wait_phase_fixture(wait_n=6)])
        history = m.parse_wait_report(report)
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
        for kind, minutes in zip(kinds, (1, 2, 3, 4, 5, 6)):
            with self.subTest(kind=kind):
                row = rows["Hana wait / " + kind]
                self.assertEqual((row.value, row.n), (f"{minutes:.1f} min", 1))
                self.assertEqual(row.window, self.window.label)
                self.assertIn("progress_history.py", row.source)

    def test_empty_window_keeps_unrecorded_history_apart_from_a_covered_zero(self) -> None:
        absent = m.parse_wait_report(wait_report_fixture(self.window, []))
        covered = m.parse_wait_report(wait_report_fixture(self.window, [], [wait_phase_fixture()]))
        absent_rows = [row for row in m.hana_wait_rows(absent, self.window, ()) if row.measure.startswith("Hana wait / ")]
        covered_rows = [row for row in m.hana_wait_rows(covered, self.window, ()) if row.measure.startswith("Hana wait / ")]
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
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
        self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                          rows["Hana wait / WaitingForUser"].n), ("2.0 min", 1))

    def test_missing_cause_is_kept_apart_from_missing_phase_history(self) -> None:
        wait = wait_fixture("cause-absent", "", self.start, 3)
        phase = wait_phase_fixture("HistoryNotRecorded")
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [phase]))
        self.assertIsInstance(history.waits[0].kind, m.WaitKindNotRecorded)
        self.assertIsInstance(history.phases[0].state, m.HistoryNotRecorded)
        rows = m.hana_wait_rows(history, self.window, ())
        missing_cause = [row for row in rows if row.measure == "Hana wait / WaitKindNotRecorded"]
        missing_history = [row for row in rows if row.measure == "Hana wait coverage / HistoryNotRecorded"]
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
        rows = m.hana_wait_rows(history, self.window, ())
        output = log_records.table("Wait fixtures", rows)
        self.assertIn("coverage / UncertainCoverage", output)
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
        self.assertEqual([(row.value, row.n) for row in rows if row.measure == "Hana wait / coverage / UncertainCoverage"], [("2", 2)])
        self.assertEqual([(row.value, row.n) for row in rows if row.measure == "Hana wait / coverage / attribution_miss_n"], [("1", 1)])
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
        self.assertEqual((rows["Hana wait / WaitingForSupplier"].value,
                          rows["Hana wait / WaitingForSupplier"].n), ("2.0 min (partial)", 1))
        self.assertEqual(rows["Hana wait / WaitingForSupplier"].value, "2.0 min (partial)")
        self.assertIn("not recorded", rows["Hana wait / WaitingForUser"].value)
        self.assertEqual((rows["Hana wait / coverage / attribution_miss_n"].value,
                          rows["Hana wait / coverage / attribution_miss_n"].n), ("1", 1))
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
                self.assertIsInstance(history.waits[0].kind, m.WaitKindNotRecorded)
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
                for kind in ("WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
                             "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity"):
                    row = rows["Hana wait / " + kind]
                    self.assertIn("not recorded", row.value)
                    self.assertEqual(row.value, "not recorded (HistoryNotRecorded)")
                    self.assertEqual(row.n, 0)
                coverage = rows["Hana wait / coverage / WaitKindNotRecorded"]
                self.assertEqual((coverage.value, coverage.n), ("1", 1))
                self.assertEqual(rows["Hana wait / WaitKindNotRecorded"].n, 1)
                if state == "Unclosed":
                    unknown = rows["Hana wait unknown-ended / WaitKindNotRecorded"]
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
        self.assertEqual((rows["Hana wait / WaitingForQuotaRefusal"].value,
                          rows["Hana wait / WaitingForQuotaRefusal"].n), ("3.0 min", 1))

    def test_known_unit_prefix_attributes_a_deleted_worktree_wait_to_hana(self) -> None:
        wait = wait_fixture("deleted", "WaitingForSupplier", self.start, 4)
        wait.update(production="tools", working_dir="/archive/widget-enhancements-unit",
                    unit="/archive/widget-enhancements-unit")
        report = wait_report_fixture(self.window, [wait])
        history = m.parse_wait_report(report, prefixes=("widget-enhancements",))
        rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
            history = m.read_wait_history(root, self.window, problems, (), ())
            self.assertIsInstance(history, m.HanaWaitHistory)
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
            history = m.read_wait_history(root, self.window, problems, (), ())
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
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
            history = m.read_source(str(root), lambda: m.read_wait_history(root, self.window, problems, (), ()),
                                    m.HistoryNotRecorded(), problems)
            self.assertIsInstance(history, m.HistoryNotRecorded)
            self.assertFalse(root.exists())
            self.assertEqual(len(problems), 1)
            self.assertIn(str(root / "runs"), problems[0].reason)
            rows = m.hana_wait_rows(history, self.window, ())
            self.assertTrue(all("not recorded" in row.value for row in rows))

    def test_corrupt_external_history_reports_a_problem_instead_of_a_covered_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs").mkdir()
            external = root / "wait_attribution.jsonl"
            _ = external.write_text("{not JSON}\n")
            before = external.read_bytes()
            problems: list[log_records.SourceProblem] = []
            history = m.read_source(str(external), lambda: m.read_wait_history(root, self.window, problems, (), ()),
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
            history = m.read_wait_history(root, self.window, problems, (), ())
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
            self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                              rows["Hana wait / WaitingForUser"].n), ("2.0 min (partial)", 1))
            self.assertEqual(rows["Hana wait / WaitingForUser"].value, "2.0 min (partial)")
            coverage = rows["Hana wait / coverage / source_problem_n"]
            self.assertEqual((coverage.value, coverage.n), ("1", 1))
            self.assertEqual(len(problems), 1)
            self.assertIn("1 unreadable or unsupported history rows", problems[0].reason)
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_finished_time_and_candidate_wait_count_are_rejected(self) -> None:
        before_start = wait_fixture("negative", "WaitingForUser", self.start, -1)
        no_end_time = wait_fixture("absent-end", "WaitingForUser", self.start)
        no_end_time["wait_end"] = {"state": "Finished"}
        candidate = wait_phase_fixture("UncertainCoverage")
        candidate["wait_n"] = 0
        valid = wait_fixture("valid", "WaitingForUser", self.start, 2)
        for waits, phases, identity, reason in (
            ([valid, before_start], [], "negative", "before its start"),
            ([valid, no_end_time], [], "absent-end", "time"),
            ([valid], [candidate], "hana-phase", "cannot carry wait_n"),
        ):
            with self.subTest(identity=identity):
                history = m.parse_wait_report(wait_report_fixture(self.window, waits, phases))
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
                self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                                  rows["Hana wait / WaitingForUser"].n), ("2.0 min (partial)", 1))
                output = log_records.table("Wait rows", list(rows.values()))
                self.assertIn(identity, output)
                self.assertIn(reason, output)
                self.assertTrue(history.problems)


class HolderHistoryTest(unittest.TestCase):
    start: datetime = datetime(2026, 10, 29, tzinfo=timezone.utc)
    window: log_records.Window = log_records.Window(start, start + timedelta(days=1))

    def full_output(self, history: m.HanaWaitHistory | m.HistoryNotRecorded) -> str:
        sources = BaselineOutputTest().sources()
        sources.wait_history = history
        with patch.object(m, "load_sources", return_value=sources), redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(m.main(["baseline", "--full", "--since", self.start.isoformat(),
                                     "--until", self.window.until.isoformat()]), 0)
        return captured.getvalue()

    def test_full_cli_coverage_counts_are_separate_and_values_fit_a_phone(self) -> None:
        named = wait_fixture("named", "WaitingForMemoryOrSlot", self.start, 4)
        named["holders"] = [holder_observation(self.start, named_holder_fixture())]
        missing_kind = wait_fixture("missing-kind", "", self.start, 2)
        unresolved = wait_fixture("ambiguous", "WaitingForUser", self.start, 90)
        unresolved["wait_attribution"] = {"state": "UnresolvedAttribution", "reason": "ambiguous phase", "candidates": []}
        broken = wait_fixture("bad-holder", "WaitingForUser", self.start, 100)
        broken["holders"] = [holder_observation(self.start, {"state": "UnknownHolder"})]
        missing_reason = "This phase started before the producer recorded wait coverage and its saved history cannot establish a completed duration."
        candidate_path = "/archive/hana_catalyst-" + "candidate" * 12
        phases = [wait_phase_fixture(wait_n=4)]
        for index in range(2):
            phase = wait_phase_fixture("HistoryNotRecorded")
            phase.update(run_id=f"missing-{index}", phase_instance_id=f"missing-phase-{index}", reason=missing_reason)
            phases.append(phase)
        uncertain = wait_phase_fixture("UncertainCoverage")
        uncertain.update(run_id="uncertain", phase_instance_id="uncertain-phase", reason="multiple possible phase holders",
                         candidates=[{"run_id": "candidate-run", "phase_instance_id": "candidate-phase",
                                      "phase_id": "4", "working_dir": candidate_path}])
        phases.append(uncertain)
        origin = wait_phase_fixture("HistoryNotRecorded")
        origin.update(run_id="lost-origin", phase_instance_id="lost-phase", unit="unidentified-work")
        for key in ("plan_doc", "production", "repository"):
            origin[key] = {"state": "NotRecorded", "reason": key + " absent"}
        phases.append(origin)
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [named, missing_kind, unresolved, broken], phases))
        output = self.full_output(history)
        rows = [re.split(r"\s{2,}", line) for line in output.splitlines()
                if len(re.split(r"\s{2,}", line)) == 5]
        expected = {"HistoryNotRecorded": 2, "UncertainCoverage": 1, "WaitKindNotRecorded": 1,
                    "attribution_miss_n": 1, "source_problem_n": 1, "unresolved_origin_n": 1}
        for table in ("Hana wait", "Hana wait holders"):
            coverage = [cells for cells in rows if cells[0].startswith(table + " / coverage /")]
            self.assertEqual(len(coverage), len(expected))
            self.assertEqual({cells[0].removeprefix(table + " / coverage / "): cells[1:3] for cells in coverage},
                             {name: [str(count), str(count)] for name, count in expected.items()})
        values = [cells[1] for cells in rows if cells[0].startswith(("Hana wait /", "Hana wait holders /",
                                                                                "Hana wait holders unnamed /"))]
        self.assertTrue(values)
        self.assertTrue(all(len(value) <= 60 for value in values), values)
        self.assertTrue(all("partial coverage" not in value for value in values), values)
        by_label = {cells[0]: cells for cells in rows}
        self.assertEqual(by_label["Hana wait / WaitingForMemoryOrSlot"][1:3], ["4.0 min (partial)", "1"])
        self.assertEqual(by_label["Hana wait holders / WaitingForMemoryOrSlot / whole wait"][1:3],
                         ["4.0 min (partial)", "1"])
        self.assertEqual(by_label["Hana wait / WaitingForSupplier"][1], "not recorded (HistoryNotRecorded)")

        flow = output.split("Flow to Hana main (recorded phase/issue completion; local-ref merge commits)\n", 1)[1].split("\n\n", 1)[0]
        lines = flow.splitlines()
        columns = [match.start() for match in re.finditer(r"\S+", lines[0])]
        self.assertEqual(len(columns), 5)
        displayed_values = [line[columns[1]:columns[2]].strip() for line in lines[1:]]
        self.assertTrue(all(len(value) <= 60 for value in displayed_values), displayed_values)
        retained = "".join(displayed_values).replace(" ", "")
        for evidence in (missing_reason, candidate_path, "candidate-run", "candidate-phase"):
            self.assertIn(evidence.replace(" ", ""), retained)

    def test_invalid_phase_identity_keeps_valid_history_in_full_report(self) -> None:
        invalid = wait_phase_fixture()
        invalid.update(run_id=42, phase_instance_id="invalid-identity")
        report = wait_report_fixture(self.window, [wait_fixture("valid", "WaitingForUser", self.start, 2)],
                                     [wait_phase_fixture(wait_n=1), invalid])
        problems: list[log_records.SourceProblem] = []
        history = m.read_source("fixture waits", lambda: m.parse_wait_report(report),
                                m.HistoryNotRecorded(), problems)
        self.assertIsInstance(history, m.HanaWaitHistory)
        self.assertEqual(problems, [])
        if isinstance(history, m.HanaWaitHistory):
            self.assertEqual([phase.instance for phase in history.phases], ["hana-phase"])
            self.assertEqual(len(history.problems), 1)
            self.assertIn("42", history.problems[0].source)
            self.assertIn("invalid-identity", history.problems[0].source)
            self.assertIn("run_id", history.problems[0].reason)
        output = self.full_output(history)
        rows = {cells[0]: cells for line in output.splitlines()
                if len(cells := re.split(r"\s{2,}", line)) == 5}
        self.assertEqual(rows["Hana wait / WaitingForUser"][1], "2.0 min (partial)")
        self.assertEqual(rows["Hana wait / WaitingForUser"][2], "1")
        self.assertIn("invalid-identity", output)
        self.assertIn("source problem", output)

    def test_full_holder_table_prints_partial_coverage_once(self) -> None:
        missing = wait_phase_fixture("HistoryNotRecorded")
        missing.update(run_id="missing-run", phase_instance_id="missing-phase")
        wait = wait_fixture("named", "WaitingForMemoryOrSlot", self.start, 3)
        wait["holders"] = [holder_observation(self.start, named_holder_fixture())]
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [wait], [wait_phase_fixture(wait_n=1), missing]))
        output = self.full_output(history)
        holder_lines = [line for line in output.splitlines() if line.startswith("Hana wait holders /")]
        self.assertTrue(all("partial coverage" not in line for line in holder_lines))
        coverage = [line for line in holder_lines if line.startswith("Hana wait holders / coverage /")]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(re.split(r"\s{2,}", coverage[0])[:3],
                         ["Hana wait holders / coverage / HistoryNotRecorded", "1", "1"])
        totals = [line for line in holder_lines if not line.startswith("Hana wait holders / coverage /")]
        self.assertEqual(len(totals), 18)
        self.assertTrue(all("partial coverage" not in line for line in totals))
        named = next(line for line in totals if "WaitingForMemoryOrSlot / whole wait" in line)
        self.assertEqual(re.split(r"\s{2,}", named)[1:3], ["3.0 min (partial)", "1"])

    def test_full_holder_totals_separate_completed_waits_from_open_waits(self) -> None:
        completed = [wait_fixture("finished-four", "WaitingForUser", self.start, 4),
                     wait_fixture("finished-six", "WaitingForUser", self.start, 6)]
        opened = wait_fixture("open", "WaitingForUser", self.start, 100)
        opened["wait_end"] = {"state": "Unclosed"}
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [*completed, opened], [wait_phase_fixture(wait_n=3)]))
        output = self.full_output(history)
        rows = {cells[0]: cells for line in output.splitlines()
                if len(cells := re.split(r"\s{2,}", line)) == 5}
        label = "Hana wait holders / WaitingForUser / HolderNotNamed"
        self.assertEqual(rows[label][1:3], ["10.0 min; 2 done; 1 open, duration unknown", "3"])
        self.assertNotIn("100.0 min", output)

    def test_full_holder_details_group_unchanged_unnamed_histories_by_kind_and_reason(self) -> None:
        waits: list[log_records.JsonObject] = []
        for identity, kind, reason, minutes in (
            ("unnamed-first", "WaitingForUser", "producer named no holder", 2),
            ("unnamed-repeat", "WaitingForUser", "producer named no holder", 3),
            ("unnamed-old", "WaitingForUser", "recorded before holders", 4),
            ("unnamed-other-kind", "WaitingForSupplier", "producer named no holder", 5),
        ):
            wait = wait_fixture(identity, kind, self.start, minutes)
            holder: log_records.JsonObject = {"state": "HolderNotNamed", "reason": reason}
            wait["holders"] = [holder_observation(self.start, holder),
                               holder_observation(self.start + timedelta(minutes=1), holder)]
            waits.append(wait)
        changed = wait_fixture("changed-unnamed", "WaitingForUser", self.start, 6)
        changed["holders"] = [
            holder_observation(self.start, {"state": "HolderNotNamed", "reason": "initial holder absent"}),
            holder_observation(self.start + timedelta(minutes=2),
                               {"state": "HolderNotNamed", "reason": "later holder absent"}),
        ]
        named = wait_fixture("named-detail", "WaitingForMemoryOrSlot", self.start, 7)
        named["holders"] = [holder_observation(self.start, named_holder_fixture())]
        waits.extend([changed, named])
        output = self.full_output(m.parse_wait_report(wait_report_fixture(
            self.window, waits, [wait_phase_fixture(wait_n=6)])))
        groups = [re.split(r"\s{2,}", line) for line in output.splitlines()
                  if line.startswith("Hana wait holders unnamed /")]
        self.assertEqual(len(groups), 3)
        self.assertEqual({cells[0]: cells[1:3] for cells in groups}, {
            "Hana wait holders unnamed / WaitingForUser / producer named no holder": ["5.0 min", "2"],
            "Hana wait holders unnamed / WaitingForUser / recorded before holders": ["4.0 min", "1"],
            "Hana wait holders unnamed / WaitingForSupplier / producer named no holder": ["5.0 min", "1"],
        })
        for identity in ("unnamed-first", "unnamed-repeat", "unnamed-old", "unnamed-other-kind"):
            self.assertNotIn(identity, output)
        details = [line for line in output.splitlines() if line.startswith("Hana wait holder /")]
        self.assertEqual(len(details), 3)
        self.assertEqual(sum("changed-unnamed" in line for line in details), 2)
        self.assertIn("initial holder absent", output)
        self.assertIn("later holder absent", output)
        self.assertTrue(any("named-detail" in line and "session:other-project-director" in line for line in details))

    def test_each_kind_counts_whole_partial_and_unnamed_holder_waits(self) -> None:
        waits: list[log_records.JsonObject] = []
        for kind in m.WaitKind:
            whole = wait_fixture(kind.value + "-whole", kind.value, self.start, 10)
            whole["holders"] = [holder_observation(self.start, named_holder_fixture())]
            partial = wait_fixture(kind.value + "-partial", kind.value, self.start, 6)
            partial["holders"] = [
                holder_observation(self.start, {"state": "HolderNotNamed", "reason": "producer named no holder"}),
                holder_observation(self.start + timedelta(minutes=2), named_holder_fixture("memory", "natedev/ram")),
            ]
            unnamed = wait_fixture(kind.value + "-unnamed", kind.value, self.start, 4)
            unnamed["holders"] = [holder_observation(
                self.start, {"state": "HolderNotNamed", "reason": "producer named no holder"})]
            waits.extend([whole, partial, unnamed])
        history = m.parse_wait_report(wait_report_fixture(self.window, waits, [wait_phase_fixture(wait_n=18)]))
        rows = {row.measure: row for row in m.holder_rows(history, self.window, completions=())}
        for kind in m.WaitKind:
            for category, minutes in (("whole wait", 10), ("part of wait", 6), ("HolderNotNamed", 4)):
                with self.subTest(kind=kind, category=category):
                    row = rows[f"Hana wait holders / {kind.value} / {category}"]
                    self.assertEqual((row.value, row.n), (f"{minutes:.1f} min", 1))
                    self.assertEqual(row.window, self.window.label)

    def test_holder_change_retains_both_identities_times_and_resources(self) -> None:
        wait = wait_fixture("changed", "WaitingForMemoryOrSlot", self.start, 10)
        change = self.start + timedelta(minutes=4)
        unit: log_records.JsonObject = {"state": "NamedHolder", "resource_kind": "memory", "resource": "natedev/ram",
                                       "holder": {"state": "UnitHolder", "unit": "other-unit"}}
        wait["holders"] = [holder_observation(self.start, named_holder_fixture()), holder_observation(change, unit)]
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait]))
        self.assertEqual(history.waits[0].holders, m.HolderHistory((
            m.HolderObservation(self.start, m.NamedHolder(m.HolderResourceKind.SLOT, "natedev/slot-1", m.SessionHolder("other-project-director"))),
            m.HolderObservation(change, m.NamedHolder(m.HolderResourceKind.MEMORY, "natedev/ram", m.UnitHolder("other-unit"))),
        )))
        rows = m.holder_rows(history, self.window, completions=())
        details = [row for row in rows if row.measure == "Hana wait holder / changed"]
        self.assertEqual(len(details), 2)
        for text in ("session:other-project-director", "00:00:00", "00:04:00", "4.0 min"):
            self.assertIn(text, details[0].value)
        for text in ("unit:other-unit", "00:04:00", "00:10:00", "6.0 min"):
            self.assertIn(text, details[1].value)
        output = log_records.table("Holders", details)
        for text in ("slot", "natedev/slot-1", "session:other-project-director", "memory", "natedev/ram",
                     "unit:other-unit", "00:00:00", "00:04:00", "00:10:00"):
            self.assertIn(text, output)
        whole = next(row for row in rows if row.measure.endswith("WaitingForMemoryOrSlot / whole wait"))
        self.assertEqual((whole.value, whole.n), ("10.0 min", 1))

    def test_same_time_holder_observations_stay_visible_without_counting_an_unnamed_delay(self) -> None:
        wait = wait_fixture("same-time", "WaitingForMemoryOrSlot", self.start, 12)
        wait["holders"] = [
            holder_observation(self.start, {"state": "HolderNotNamed", "reason": "producer named no holder"}),
            holder_observation(self.start, named_holder_fixture()),
        ]
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)]))
        self.assertEqual(len(history.waits[0].holders.observations), 2)
        rows = m.holder_rows(history, self.window, completions=())
        details = [row for row in rows if row.measure == "Hana wait holder / same-time"]
        self.assertEqual(len(details), 2)
        self.assertIn("producer named no holder", details[0].value)
        self.assertIn("0.0 min", details[0].value)
        self.assertIn("session:other-project-director", details[1].value)
        self.assertIn("12.0 min", details[1].value)
        whole = next(row for row in rows if row.measure.endswith("WaitingForMemoryOrSlot / whole wait"))
        unnamed = next(row for row in rows if row.measure.endswith("WaitingForMemoryOrSlot / HolderNotNamed"))
        self.assertEqual((whole.value, whole.n), ("12.0 min", 1))
        self.assertEqual((unnamed.value, unnamed.n), ("0.0 min", 0))

    def test_absent_holder_history_and_explicit_unnamed_history_keep_the_reason(self) -> None:
        historical = wait_fixture("historical", "WaitingForQuotaRefusal", self.start, 3)
        historical["holders"] = [holder_observation(
            self.start, {"state": "HolderNotNamed", "reason": "recorded before holders"})]
        absent = wait_fixture("absent", "WaitingForQuotaRefusal", self.start, 1)
        unnamed = wait_fixture("unnamed", "WaitingForQuotaRefusal", self.start, 2)
        unnamed["holders"] = [holder_observation(
            self.start, {"state": "HolderNotNamed", "reason": "producer named no holder"})]
        history = m.parse_wait_report(wait_report_fixture(self.window, [historical, unnamed, absent]))
        self.assertEqual(history.waits[0].holders.observations[0].holder, m.HolderNotNamed("recorded before holders"))
        self.assertEqual(history.waits[1].holders.observations[0].holder, m.HolderNotNamed("producer named no holder"))
        self.assertEqual(history.waits[2].holders.observations[0].holder, m.HolderNotNamed("recorded before holders"))
        rows = m.holder_rows(history, self.window, completions=())
        total = next(row for row in rows if row.measure.endswith("WaitingForQuotaRefusal / HolderNotNamed"))
        self.assertEqual((total.value, total.n), ("6.0 min", 3))
        output = log_records.table("Holders", rows)
        self.assertIn("recorded before holders", output)
        self.assertIn("producer named no holder", output)

    def test_late_first_holder_observation_keeps_the_initial_unnamed_interval(self) -> None:
        wait = wait_fixture("late-holder", "ReadyWithoutCapacity", self.start, 8)
        wait["holders"] = [holder_observation(self.start + timedelta(minutes=3), named_holder_fixture("account", "account-a"))]
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait]))
        rows = m.holder_rows(history, self.window, completions=())
        partial = next(row for row in rows if row.measure.endswith("ReadyWithoutCapacity / part of wait"))
        self.assertEqual((partial.value, partial.n), ("8.0 min", 1))
        details = [row for row in rows if row.measure == "Hana wait holder / late-holder"]
        self.assertEqual(len(details), 2)
        self.assertIn("HolderNotNamed", details[0].value)
        self.assertIn("00:00:00", details[0].value)
        self.assertIn("00:03:00", details[0].value)
        self.assertIn("account-a", details[1].value)

    def test_unknown_ended_holder_wait_has_no_completed_duration(self) -> None:
        wait = wait_fixture("still-open", "WaitingForMemoryOrSlot", self.start, 100)
        wait["wait_end"] = {"state": "Open"}
        wait["holders"] = [holder_observation(self.start, named_holder_fixture())]
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait]))
        rows = m.holder_rows(history, self.window, completions=())
        named = next(row for row in rows if row.measure.endswith("WaitingForMemoryOrSlot / part of wait"))
        self.assertEqual(named.n, 1)
        self.assertEqual(named.value, "1 open, duration unknown")
        self.assertNotIn("0.0 min", named.value)
        detail = next(row for row in rows if row.measure == "Hana wait holder / still-open")
        self.assertIn("unknown", detail.value)
        self.assertNotIn("100.0", detail.value)

    def test_retired_and_unresolved_phase_origins_stay_visible_beside_valid_hana(self) -> None:
        retired = wait_phase_fixture("HistoryNotRecorded")
        retired.update(run_id="retired-run", phase_instance_id="retired-phase", unit="widget-enhancements",
                       plan_doc={"state": "Recorded", "value": "tool-based-ui-widget.md"},
                       production={"state": "NotRecorded", "reason": "production was not saved"},
                       repository={"state": "NotRecorded", "reason": "repository was not saved"})
        unresolved = wait_phase_fixture("HistoryNotRecorded")
        unresolved.update(run_id="lost-run", phase_instance_id="lost-phase", unit="unidentified-work")
        for field in ("plan_doc", "production", "repository"):
            unresolved[field] = {"state": "NotRecorded", "reason": field + " was not saved"}
        wait = wait_fixture("valid", "WaitingForUser", self.start, 2)
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [wait], [wait_phase_fixture(wait_n=1), retired, unresolved]))
        self.assertEqual({phase.instance for phase in history.phases}, {"hana-phase", "retired-phase"})
        self.assertIsInstance(next(phase.state for phase in history.phases if phase.instance == "retired-phase"),
                              m.HistoryNotRecorded)
        self.assertEqual(len(history.unresolved_origins), 1)
        self.assertIsInstance(history.unresolved_origins[0], m.UnresolvedPhaseOrigin)
        rows = m.hana_wait_rows(history, self.window, ())
        output = log_records.table("Waits", rows)
        for text in ("UnresolvedPhaseOrigin", "lost-run", "lost-phase", "was not saved", "retired-run", "retired-phase"):
            self.assertIn(text, output)
        valid = next(row for row in rows if row.measure == "Hana wait / WaitingForUser")
        self.assertEqual((valid.value, valid.n), ("2.0 min (partial)", 1))

    def test_malformed_holder_or_origin_rejects_only_its_row(self) -> None:
        for defect in ("holder-state", "resource-kind", "owner-state", "unknown-field", "origin"):
            with self.subTest(defect=defect):
                valid = wait_fixture("valid", "WaitingForUser", self.start, 2)
                broken = wait_fixture("bad-holder", "WaitingForUser", self.start, 90)
                holder = named_holder_fixture()
                if defect == "holder-state":
                    holder["state"] = "UnknownHolder"
                elif defect == "resource-kind":
                    holder["resource_kind"] = "disk"
                elif defect == "owner-state":
                    holder["holder"] = {"state": "UnknownOwner", "session_id": "other"}
                elif defect == "unknown-field":
                    holder["future_field"] = "unrecognized"
                broken["holders"] = [holder_observation(self.start, holder)]
                bad_phase = wait_phase_fixture()
                bad_phase.update(run_id="broken-run", phase_instance_id="bad-origin", production={"state": "Recorded", "value": 42})
                waits = [valid] if defect == "origin" else [valid, broken]
                phases = [wait_phase_fixture(wait_n=1), bad_phase] if defect == "origin" else [wait_phase_fixture(wait_n=2)]
                history = m.parse_wait_report(wait_report_fixture(self.window, waits, phases))
                self.assertEqual([wait.identity for wait in history.waits], ["valid"])
                self.assertTrue(history.problems)
                rows = m.hana_wait_rows(history, self.window, ())
                total = next(row for row in rows if row.measure == "Hana wait / WaitingForUser")
                self.assertEqual((total.value, total.n), ("2.0 min (partial)", 1))
                output = log_records.table("Waits", rows)
                self.assertIn("source problem", output)
                self.assertIn("bad-origin" if defect == "origin" else "bad-holder", output)

    def test_real_supplier_invalid_holder_is_named_without_losing_other_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            runs.mkdir()
            good = wait_fixture("valid", "WaitingForUser", self.start, 2)
            bad = wait_fixture("bad-holder", "WaitingForMemoryOrSlot", self.start, 90)
            opened = {**bad, "event_type": "wait_started", "event_id": "bad-holder-start",
                      "timestamp": self.start.isoformat(), "timestamp_epoch": self.start.timestamp(),
                      "holder": {"state": "NamedHolder", "resource_kind": "disk", "resource": "disk-1",
                                 "holder": {"state": "SessionHolder", "session_id": "other"}}}
            path = runs / "hana-run.jsonl"
            self.assertGreater(path.write_text("\n".join(json.dumps(event) for event in (
                WaitRecordsTest().phase_started(), good, opened, bad)) + "\n"), 0)
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            history = m.read_wait_history(root, self.window, problems, (), ())
            self.assertIsInstance(history, m.HanaWaitHistory)
            if isinstance(history, m.HanaWaitHistory):
                self.assertEqual([wait.identity for wait in history.waits], ["valid"])
            self.assertTrue(problems)
            output = log_records.table("Waits", m.hana_wait_rows(history, self.window, ()))
            self.assertIn("source problem", output)
            self.assertIn("bad-holder", output)
            rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
            self.assertEqual((rows["Hana wait / WaitingForUser"].value,
                              rows["Hana wait / WaitingForUser"].n), ("2.0 min (partial)", 1))
            self.assertEqual(path.read_bytes(), before)

    def test_missing_id_count_is_a_problem_beside_valid_waits(self) -> None:
        wait = wait_fixture("valid", "WaitingForUser", self.start, 2)
        report = wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)])
        report.update(ignored_n=3, wait_without_id_n=3)
        history = m.parse_wait_report(report)
        rows = m.hana_wait_rows(history, self.window, ())
        total = next(row for row in rows if row.measure == "Hana wait / WaitingForUser")
        self.assertEqual((total.value, total.n), ("2.0 min (partial)", 1))
        self.assertTrue(history.problems)
        output = log_records.table("Waits", rows)
        problem = next(problem for problem in history.problems if "wait_id" in problem.reason)
        self.assertIn("3 wait events without wait_id", problem.reason)
        self.assertIn(problem.reason, output)

    def test_real_supplier_holder_history_reaches_full_baseline_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            runs.mkdir()
            events: list[log_records.JsonObject] = [WaitRecordsTest().phase_started()]
            for identity, holder in (("producer-named", named_holder_fixture("account", "account-a")),
                                     ("producer-unnamed", {"state": "HolderNotNamed", "reason": "producer named no holder"})):
                done = wait_fixture(identity, "WaitingForQuotaRefusal", self.start, 10)
                opened = {**done, "event_id": identity + "-start", "event_type": "wait_started",
                          "timestamp": self.start.isoformat(), "timestamp_epoch": self.start.timestamp(), "holder": holder}
                events.append(opened)
                if identity == "producer-named":
                    change = self.start + timedelta(minutes=4)
                    events.append({**opened, "event_id": "holder-change", "event_type": "wait_holder_seen",
                                   "timestamp": change.isoformat(), "timestamp_epoch": change.timestamp(),
                                   "holder": {"state": "NamedHolder", "resource_kind": "account", "resource": "account-b",
                                              "holder": {"state": "UnitHolder", "unit": "models-unit"}}})
                events.append(done)
            path = runs / "hana-run.jsonl"
            self.assertGreater(path.write_text("\n".join(json.dumps(event) for event in events) + "\n"), 0)
            before = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            history = m.read_wait_history(root, self.window, problems, (), ())
            self.assertIsInstance(history, m.HanaWaitHistory)
            sources = BaselineOutputTest().sources()
            sources.wait_history = history
            sources.problems.extend(problems)
            with patch.object(m, "load_sources", return_value=sources), redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(m.main(["baseline", "--full", "--since", self.start.isoformat(),
                                         "--until", self.window.until.isoformat()]), 0)
            output = captured.getvalue()
            self.assertEqual(problems, [])
            for text in ("producer-named", "Hana wait holders unnamed / WaitingForQuotaRefusal / producer named no holder", "account-a", "account-b",
                         "session:other-project-director", "unit:models-unit", "producer named no holder",
                         "00:00:00", "00:04:00", "00:10:00"):
                self.assertIn(text, output)
            rows = {cells[0]: cells for line in output.splitlines() if len(cells := re.split(r"\s{2,}", line)) == 5}
            self.assertEqual(rows["Hana wait holders / WaitingForQuotaRefusal / whole wait"][1:3], ["10.0 min", "1"])
            self.assertEqual(rows["Hana wait holders / WaitingForQuotaRefusal / HolderNotNamed"][1:3], ["10.0 min", "1"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([item.relative_to(root) for item in root.rglob("*") if item.is_file()], [Path("runs/hana-run.jsonl")])


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

    def test_full_cli_omits_the_hint_and_summary_only_keeps_it(self) -> None:
        sources = self.sources()
        sources.problems = [log_records.SourceProblem("fixture source", "missing history")]
        summary = self.output(sources).splitlines()
        full = self.output(sources, full=True)
        full_summary = full.split("\n\n", 1)[0].splitlines()
        self.assertEqual(summary[-1], "Source problems: 1. Use --full to print the tables.")
        self.assertEqual(full_summary[-1], "Source problems: 1.")
        self.assertEqual(full_summary[:-1], summary[:-1])
        self.assertNotIn("Use --full", full)
        self.assertIn("Capacity on Hana", full)

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
        self.assertTrue(output.startswith(summary.replace(" Use --full to print the tables.", "") + "\nCapacity on Hana\n"))
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
        self.assertTrue(output.startswith(summary.replace(" Use --full to print the tables.", "") + "\nCapacity on Hana\n"))
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

    def test_phase_completions_read_tagged_origins_and_report_malformed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base: log_records.JsonObject = {
                "event_type": "phase_finished", "status": "completed",
                "timestamp": "2026-10-14T00:00:00Z", "phase_elapsed_seconds": 60,
            }
            missing = {"state": "NotRecorded", "reason": "hana origin is unavailable"}
            records: list[log_records.JsonObject] = []
            for field, value in (("production", "hana"), ("repository", "hana_catalyst"),
                                 ("plan_doc", "docs/plans/tool-based-ui-demo.md")):
                records.append({**base, "event_id": field, field: {"state": "Recorded", "value": value}})
            records.append({**base, "event_id": "absent", "plan_doc": missing,
                            "production": missing, "repository": missing})
            for field in ("plan_doc", "production", "repository"):
                for bad in ({"state": "Recorded"}, {"state": "NotRecorded", "reason": ""},
                            {"state": "Other"}, 7):
                    records.append({**base, field: bad})
            path = root / "events.jsonl"
            _ = path.write_text("\n".join(json.dumps(record) for record in records))
            problems: list[log_records.SourceProblem] = []
            phases = m.read_phases(root, problems)
            self.assertEqual([(phase.identity, phase.hana) for phase in phases],
                             [("production", True), ("repository", True), ("plan_doc", True), ("absent", False)])
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].source, str(path))
            self.assertIn("12 invalid phase field rows", problems[0].reason)

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
