"""Fixture checks for the baseline's counts and source boundaries."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
import json
import io
import os
import subprocess
from typing import cast
from unittest.mock import patch

from scripts.hierarchies import measure as m, log_records, push_audit as audit
from scripts.hierarchies.test_log_records import phone_overflow_lines, read_report_blocks, summary_lines
from scripts.lib.user_zone import user_zone
from scripts.lib import unit_runs
from scripts.production.issues import Issue
from scripts.usage import collect


def row_cells(output: str) -> dict[str, list[str]]:
    return {row.measure: [row.measure, row.value, str(row.n), row.window, row.source]
            for row in read_report_blocks(output)}


BASELINE_SUMMARY_PREFIXES = ("Window:", "Hana:", "Goal 1:", "User attention:", "Source problems:")


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


def write_token_ledger(
    path: Path, folders: Sequence[tuple[str, str]], changes: Sequence[tuple[str, str, str]],
    turns: Sequence[tuple[str, str, str, str | None, int]],
) -> None:
    with closing(sqlite3.connect(path)) as db:
        _ = db.executescript(
            "CREATE TABLE turns(machine,family,parent_id,agent_id,at,role,input,cache_write_5m,cache_write_1h,cache_read,output); "
            + "CREATE TABLE folders(machine,family,parent_id,agent_id,folder); "
            + "CREATE TABLE account_role_changes(machine,at,role,label);"
        )
        _ = db.executemany("INSERT INTO folders VALUES ('natedev','codex','',?,?)", folders)
        _ = db.executemany("INSERT INTO account_role_changes VALUES ('natedev',?,?,?)", changes)
        _ = db.executemany("INSERT INTO turns VALUES (?,'codex','',?,?,?,?,0,0,0,0)", turns)
        db.commit()


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
                output = log_records.blocks("Wait rows", m.hana_wait_rows(history, self.window, ()))
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
        output = log_records.blocks("Wait fixtures", rows)
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

    def test_candidate_cells_count_distinct_notes_in_first_seen_order(self) -> None:
        first: log_records.JsonObject = {"run_id": "hana-run", "phase_id": "4", "phase_instance_id": "hana-phase"}
        second: log_records.JsonObject = {"run_id": "hana-run", "phase_id": "5", "phase_instance_id": "hana-next"}
        first_label = "run=hana-run; phase=4; instance=hana-phase; cwd=not recorded"
        second_label = "run=hana-run; phase=5; instance=hana-next; cwd=not recorded"
        for candidates, expected in (([first] * 6, first_label),
                                     ([first, second, first], first_label + " | " + second_label)):
            with self.subTest(expected=expected):
                phase = wait_phase_fixture("UncertainCoverage")
                phase.update(reason="ambiguous coverage", candidates=candidates, recorded_wait_n=0)
                missing = wait_fixture("ambiguous-candidates", "WaitingForUser", self.start)
                missing["wait_attribution"] = {"state": "UnresolvedAttribution", "reason": "ambiguous attribution",
                                                "candidates": candidates}
                history = m.parse_wait_report(wait_report_fixture(self.window, [missing], [phase]))
                output = log_records.blocks("Waits", m.hana_wait_rows(history, self.window, ()))
                rows = read_report_blocks(output)
                for label, reason in (("Hana wait coverage / UncertainCoverage", "ambiguous coverage"),
                                      ("Hana wait attribution / Unresolved", "ambiguous attribution")):
                    cells = [row for row in rows if row.measure == label]
                    self.assertEqual([(row.value, row.n) for row in cells],
                                     [(reason + "; candidates: " + expected, 1)])

    def test_full_wait_report_collapses_twenty_six_phases_and_three_waits_without_merging_other_reasons(self) -> None:
        candidates: list[log_records.JsonObject] = [
            {"run_id": "hana-run", "phase_id": str(index), "phase_instance_id": f"candidate-{index}",
             "working_dir": "" if index == 0 else f"/archive/hana-{index}"}
            for index in range(6)
        ]
        phases: list[log_records.JsonObject] = []
        for index in range(26):
            phase = wait_phase_fixture("UncertainCoverage")
            phase.update(phase_instance_id=f"phase-{index}", reason="overlapping phase records",
                         candidates=candidates * 26)
            phases.append(phase)
        different = wait_phase_fixture("UncertainCoverage")
        different.update(phase_instance_id="different-phase", reason="another coverage reason", candidates=candidates)
        phases.append(different)
        waits: list[log_records.JsonObject] = []
        for index in range(4):
            wait = wait_fixture(f"miss-{index}", "WaitingForUser", self.start)
            wait["wait_attribution"] = {
                "state": "UnresolvedAttribution", "reason": "ambiguous owner" if index < 3 else "different owner",
                "candidates": candidates * 26,
            }
            waits.append(wait)
        history = m.parse_wait_report(wait_report_fixture(self.window, waits, phases))
        rows = m.hana_wait_rows(history, self.window, ())
        sources = m.BaselineSources([], [], [], [], [], notification_report([], [], self.window),
                                    [], [], [], [], [], [], history)
        output = m.render(sources, self.window, self.window.until, full=True)
        parsed = read_report_blocks(output)
        shared_source = "progress_history.py waits; phase starts and wait starts in window; completed durations only"
        labels = [f"run=hana-run; phase={index}; instance=candidate-{index}; cwd="
                  + ("not recorded" if index == 0 else f"/archive/hana-{index}") for index in range(6)]
        detail = " | ".join(labels)
        grouped_phases = [row for row in parsed if row.measure == "Hana wait coverage / UncertainCoverage"]
        self.assertEqual(grouped_phases, [
            log_records.MeasureRow("Hana wait coverage / UncertainCoverage",
                                   "overlapping phase records; candidates: " + detail, 26, self.window.label,
                                   shared_source + "; phases: "
                                   + ", ".join(f"hana-run/phase-{index}" for index in range(26))),
            log_records.MeasureRow("Hana wait coverage / UncertainCoverage",
                                   "another coverage reason; candidates: " + detail, 1, self.window.label,
                                   shared_source + "; phases: hana-run/different-phase"),
        ])
        attribution = [row for row in parsed if row.measure == "Hana wait attribution / Unresolved"]
        self.assertEqual(attribution, [
            log_records.MeasureRow("Hana wait attribution / Unresolved", "ambiguous owner; candidates: " + detail,
                                   3, self.window.label, shared_source + "; waits: miss-0, miss-1, miss-2"),
            log_records.MeasureRow("Hana wait attribution / Unresolved", "different owner; candidates: " + detail,
                                   1, self.window.label, shared_source + "; waits: miss-3"),
        ])
        original = next(row for row in rows if row.measure == "Hana wait coverage / UncertainCoverage")
        for measure, quantities in (
            ("Hana wait coverage / UncertainCoverage", [
                log_records.Diagnostic(26, "overlapping phase records; candidates: " + detail),
                log_records.Diagnostic(1, "another coverage reason; candidates: " + detail),
            ]),
            ("Hana wait attribution / Unresolved", [
                log_records.Diagnostic(3, "ambiguous owner; candidates: " + detail),
                log_records.Diagnostic(1, "different owner; candidates: " + detail),
            ]),
        ):
            with self.subTest(measure=measure):
                self.assertEqual([row.quantity for row in rows if row.measure == measure], quantities)
        self.assertEqual(original.source, shared_source + "; phases: "
                         + ", ".join(f"hana-run/phase-{index}" for index in range(26)))
        self.assertEqual(len(history.phases), 27)
        self.assertEqual(len(history.attribution_misses), 4)
        self.assertNotRegex(output, r"\(\d+ times\)")
        self.assertNotRegex(output, r"cwd=(?:\s*\||\s*$)")
        self.assertEqual(phone_overflow_lines(output), [])

    def test_unresolved_origins_and_source_problems_group_only_equal_reasons(self) -> None:
        phases: list[log_records.JsonObject] = []
        waits: list[log_records.JsonObject] = []
        for index in range(4):
            phase = wait_phase_fixture()
            phase.update(run_id="unknown-run", phase_instance_id=f"unknown-{index}", unit="unknown-unit")
            for field in ("plan_doc", "production", "repository"):
                phase[field] = {"state": "NotRecorded", "reason": "origin absent" if index < 3 else "older origin absent"}
            phases.append(phase)
            wait = wait_fixture(f"bad-{index}", "invalid-kind" if index < 3 else "other-invalid-kind", self.start)
            waits.append(wait)
        history = m.parse_wait_report(wait_report_fixture(self.window, waits, phases))
        diagnostics = m.hana_wait_rows(history, self.window, ())
        output = log_records.blocks("Wait diagnostics", diagnostics)
        rows = read_report_blocks(output)
        source = "progress_history.py waits; phase starts and wait starts in window; completed durations only"
        origins = [row for row in rows if row.measure == "Hana wait origin / UnresolvedPhaseOrigin"]
        self.assertEqual(origins, [
            log_records.MeasureRow("Hana wait origin / UnresolvedPhaseOrigin", "; ".join(["origin absent"] * 3),
                                   3, self.window.label,
                                   source + "; phases: unknown-run/unknown-0, unknown-run/unknown-1, unknown-run/unknown-2"),
            log_records.MeasureRow("Hana wait origin / UnresolvedPhaseOrigin", "; ".join(["older origin absent"] * 3),
                                   1, self.window.label, source + "; phases: unknown-run/unknown-3"),
        ])
        problems = [row for row in rows if row.measure == "Hana wait / source problem"]
        self.assertEqual(problems, [
            log_records.MeasureRow("Hana wait / source problem", "unknown wait kind: invalid-kind", 3,
                                   self.window.label, source + "; waits: wait bad-0, wait bad-1, wait bad-2"),
            log_records.MeasureRow("Hana wait / source problem", "unknown wait kind: other-invalid-kind", 1,
                                   self.window.label, source + "; waits: wait bad-3"),
        ])
        for measure, quantities in (
            ("Hana wait origin / UnresolvedPhaseOrigin", [
                log_records.Diagnostic(3, "; ".join(["origin absent"] * 3)),
                log_records.Diagnostic(1, "; ".join(["older origin absent"] * 3)),
            ]),
            ("Hana wait / source problem", [
                log_records.Diagnostic(3, "unknown wait kind: invalid-kind"),
                log_records.Diagnostic(1, "unknown wait kind: other-invalid-kind"),
            ]),
        ):
            with self.subTest(measure=measure):
                self.assertEqual([row.quantity for row in diagnostics if row.measure == measure], quantities)
        self.assertEqual(phone_overflow_lines(output), [])

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
        output = log_records.blocks("Wait fixtures", list(rows.values()))
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
        output = log_records.blocks("Wait fixtures", list(rows.values()))
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
            self.assertIn("source unavailable", log_records.blocks(
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
                output = log_records.blocks("Wait rows", list(rows.values()))
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

    def test_named_then_malformed_holder_keeps_partial_holder_coverage_and_phase_minutes(self) -> None:
        wait = wait_fixture("holder-became-malformed", "WaitingForMemoryOrSlot", self.start, 10)
        wait["holders"] = [holder_observation(self.start, named_holder_fixture()), holder_observation(
            self.start + timedelta(minutes=4), {"state": "MalformedHolder", "reason": "holder schema changed",
                                               "raw_value": {"private-resource": "never-print-this"}})]
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)]))
        self.assertEqual(history.problems, ())
        self.assertEqual(m.holder_coverage(history.waits[0]), m.HolderCoverage.PART)
        event = m.PhaseCompletion("holder-phase-finished", self.start + timedelta(minutes=10), True, 600,
                                  phase=m.WaitPhaseInstance("hana-run", "hana-phase"), started=self.start)
        rows = m.phase_flow([event], self.window, history)
        by_name = {row.measure: row for row in rows}
        for label, expected in (("Hana wait / WaitingForMemoryOrSlot", "10.0 min"),
                                ("Hana wait holders / WaitingForMemoryOrSlot / part of wait", "10.0 min"),
                                ("Hana wait / share of phase time with a recorded kind", "100.0%")):
            self.assertEqual((by_name[label].value, by_name[label].n), (expected, 1))
        details = [row for row in rows if row.measure == "Hana wait holder / holder-became-malformed"]
        self.assertEqual(len(details), 2)
        self.assertIn("MalformedHolder: holder schema changed", details[1].value)
        self.assertIn("6.0 min", details[1].value)
        self.assertFalse(any("coverage / source_problem_n" in row.measure for row in rows))
        output = log_records.blocks("Flow to Hana main", rows)
        for raw in ("private-resource", "never-print-this", "raw_value"):
            self.assertNotIn(raw, output)

    def test_legacy_invalid_holder_reason_becomes_malformed_without_a_source_problem(self) -> None:
        wait = wait_fixture("legacy-holder", "WaitingForUser", self.start, 3)
        wait["holders"] = [holder_observation(
            self.start, {"state": "HolderNotNamed", "reason": "invalid holder record: x"})]
        history = m.parse_wait_report(wait_report_fixture(self.window, [wait], [wait_phase_fixture(wait_n=1)]))
        self.assertEqual(history.problems, ())
        self.assertEqual(history.waits[0].holders.observations[0].holder, m.MalformedHolder("invalid holder record: x"))
        rows = {row.measure: row for row in m.holder_rows(history, self.window, ())}
        row = rows["Hana wait holders unnamed / WaitingForUser / MalformedHolder: invalid holder record: x"]
        self.assertEqual((row.value, row.n), ("3.0 min", 1))

    def test_malformed_holder_invalid_metadata_rejects_only_its_wait(self) -> None:
        invalid_records: tuple[log_records.JsonObject, ...] = (
            {"state": "MalformedHolder", "reason": "invalid holder", "unknown": "never-print-this"},
            {"state": "MalformedHolder", "reason": "   ", "raw_value": "never-print-this"},
        )
        for fields in invalid_records:
            with self.subTest(fields=fields):
                valid = wait_fixture("valid", "WaitingForUser", self.start, 2)
                broken = wait_fixture("bad-malformed-holder", "WaitingForUser", self.start, 90)
                broken["holders"] = [holder_observation(self.start, fields)]
                history = m.parse_wait_report(wait_report_fixture(self.window, [valid, broken], [wait_phase_fixture(wait_n=2)]))
                self.assertEqual([wait.identity for wait in history.waits], ["valid"])
                self.assertEqual(len(history.problems), 1)
                rows = {row.measure: row for row in m.hana_wait_rows(history, self.window, ())}
                self.assertEqual((rows["Hana wait / WaitingForUser"].value, rows["Hana wait / WaitingForUser"].n),
                                 ("2.0 min (partial)", 1))
                self.assertEqual((rows["Hana wait / coverage / source_problem_n"].value,
                                  rows["Hana wait / coverage / source_problem_n"].n), ("1", 1))
                output = log_records.blocks("Flow to Hana main", list(rows.values()))
                self.assertIn("bad-malformed-holder", output)
                self.assertNotIn("never-print-this", output)

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
        rows = [[row.measure, row.value, str(row.n), row.window, row.source] for row in read_report_blocks(output)]
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
        self.assertTrue(all(len(value) <= log_records.PHONE_WIDTH for value in values), values)
        self.assertTrue(all("partial coverage" not in value for value in values), values)
        by_label = {cells[0]: cells for cells in rows}
        self.assertEqual(by_label["Hana wait / WaitingForMemoryOrSlot"][1:3], ["4.0 min (partial)", "1"])
        self.assertEqual(by_label["Hana wait holders / WaitingForMemoryOrSlot / whole wait"][1:3],
                         ["4.0 min (partial)", "1"])
        self.assertEqual(by_label["Hana wait / WaitingForSupplier"][1], "not recorded (HistoryNotRecorded)")

        self.assertEqual(phone_overflow_lines(output), [])
        retained = "".join(row.value for row in read_report_blocks(output)).replace(" ", "")
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
        rows = row_cells(output)
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
        holder_rows = [row for row in read_report_blocks(output) if row.measure.startswith("Hana wait holders /")]
        self.assertNotIn("partial coverage", repr(holder_rows))
        coverage = [row for row in holder_rows if row.measure.startswith("Hana wait holders / coverage /")]
        self.assertEqual(len(coverage), 1)
        self.assertEqual((coverage[0].measure, coverage[0].value, coverage[0].n),
                         ("Hana wait holders / coverage / HistoryNotRecorded", "1", 1))
        totals = [row for row in holder_rows if not row.measure.startswith("Hana wait holders / coverage /")]
        self.assertEqual(len(totals), 18)
        self.assertNotIn("partial coverage", repr(totals))
        self.assertTrue(all(row.window == self.window.label for row in holder_rows))
        self.assertEqual(coverage[0].source, "progress_history.py waits; incomplete coverage counts")
        self.assertTrue(all(row.source == "progress_history.py waits holders; total completed wait minutes by holder coverage; unknown ends excluded"
                            for row in totals))
        named = next(row for row in totals if "WaitingForMemoryOrSlot / whole wait" in row.measure)
        self.assertEqual((named.value, named.n), ("3.0 min (partial)", 1))

    def test_full_holder_totals_separate_completed_waits_from_open_waits(self) -> None:
        completed = [wait_fixture("finished-four", "WaitingForUser", self.start, 4),
                     wait_fixture("finished-six", "WaitingForUser", self.start, 6)]
        opened = wait_fixture("open", "WaitingForUser", self.start, 100)
        opened["wait_end"] = {"state": "Unclosed"}
        history = m.parse_wait_report(wait_report_fixture(
            self.window, [*completed, opened], [wait_phase_fixture(wait_n=3)]))
        output = self.full_output(history)
        rows = row_cells(output)
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
        groups = [[row.measure, row.value, str(row.n), row.window, row.source] for row in read_report_blocks(output)
                  if row.measure.startswith("Hana wait holders unnamed /")]
        self.assertEqual(len(groups), 3)
        self.assertEqual({cells[0]: cells[1:3] for cells in groups}, {
            "Hana wait holders unnamed / WaitingForUser / producer named no holder": ["5.0 min", "2"],
            "Hana wait holders unnamed / WaitingForUser / recorded before holders": ["4.0 min", "1"],
            "Hana wait holders unnamed / WaitingForSupplier / producer named no holder": ["5.0 min", "1"],
        })
        for identity in ("unnamed-first", "unnamed-repeat", "unnamed-old", "unnamed-other-kind"):
            self.assertNotIn(identity, output)
        details = [row for row in read_report_blocks(output) if row.measure.startswith("Hana wait holder /")]
        self.assertEqual(len(details), 3)
        self.assertEqual(sum("changed-unnamed" in row.measure for row in details), 2)
        self.assertIn("initial holder absent", output)
        self.assertIn("later holder absent", output)
        self.assertTrue(any("named-detail" in row.measure and "session:other-project-director" in row.value
                            for row in details))

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
        output = log_records.blocks("Holders", details)
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
        output = log_records.blocks("Holders", rows)
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
        output = log_records.blocks("Waits", rows)
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
                output = log_records.blocks("Waits", rows)
                self.assertIn("source problem", output)
                self.assertIn("bad-origin" if defect == "origin" else "bad-holder", output)

    def test_real_supplier_invalid_holder_is_named_without_losing_other_waits(self) -> None:
        invalid_object: log_records.JsonObject = {
            "state": "NamedHolder", "resource_kind": "disk", "resource": "disk-1",
            "holder": {"state": "SessionHolder", "session_id": "other"},
        }
        for holder in (invalid_object, "slot-7"):
            with self.subTest(holder=holder), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runs = root / "runs"
                runs.mkdir()
                good = wait_fixture("valid", "WaitingForUser", self.start, 2)
                bad = wait_fixture("bad-holder", "WaitingForMemoryOrSlot", self.start, 90)
                opened: log_records.JsonObject = {
                    **bad, "event_type": "wait_started", "event_id": "bad-holder-start",
                    "timestamp": self.start.isoformat(), "timestamp_epoch": self.start.timestamp(), "holder": holder,
                }
                path = runs / "hana-run.jsonl"
                self.assertGreater(path.write_text("\n".join(json.dumps(event) for event in (
                    WaitRecordsTest().phase_started(), good, opened, bad)) + "\n"), 0)
                before = path.read_bytes()
                problems: list[log_records.SourceProblem] = []
                history = m.read_wait_history(root, self.window, problems, (), ())
                self.assertIsInstance(history, m.HanaWaitHistory)
                if isinstance(history, m.HanaWaitHistory):
                    self.assertEqual({wait.identity for wait in history.waits}, {"valid", "bad-holder"})
                    malformed = next(wait for wait in history.waits if wait.identity == "bad-holder")
                    self.assertIsInstance(malformed.holders.observations[0].holder, m.MalformedHolder)
                    self.assertEqual(history.problems, ())
                self.assertEqual(problems, [])
                rows = m.phase_flow([], self.window, history)
                by_name = {row.measure: row for row in rows}
                for label, minutes in (("Hana wait / WaitingForUser", "2.0 min"),
                                       ("Hana wait / WaitingForMemoryOrSlot", "90.0 min"),
                                       ("Hana wait holders / WaitingForMemoryOrSlot / HolderNotNamed", "90.0 min")):
                    self.assertEqual((by_name[label].value, by_name[label].n), (minutes, 1))
                groups = [row for row in rows if row.measure.startswith(
                    "Hana wait holders unnamed / WaitingForMemoryOrSlot / MalformedHolder: ")]
                self.assertEqual(len(groups), 1)
                self.assertEqual((groups[0].value, groups[0].n), ("90.0 min", 1))
                if isinstance(holder, str):
                    self.assertEqual(groups[0].measure,
                                     "Hana wait holders unnamed / WaitingForMemoryOrSlot / MalformedHolder: holder must be an object")
                else:
                    self.assertIn("invalid holder record:", groups[0].measure)
                output = log_records.blocks("Flow to Hana main", rows)
                self.assertNotIn("source problem", output)
                for raw in ("disk-1", "other", "slot-7", "raw_value"):
                    self.assertNotIn(raw, output)
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
        output = log_records.blocks("Waits", rows)
        problem = next(problem for problem in history.problems if "wait_id" in problem.reason)
        self.assertIn("3 wait events without wait_id", problem.reason)
        self.assertIn(problem.reason, [row.value for row in read_report_blocks(output)])

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
                self.assertIn(text, " ".join(output.split()))
            rows = row_cells(output)
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
        output = log_records.blocks("Receipt fixtures", rows)
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
        self.assertIn("ReceiptMissing", log_records.blocks("Receipt fixtures", rows))

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
            self.assertIn(str(path), log_records.blocks(
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
        summary = summary_lines(self.output(sources), BASELINE_SUMMARY_PREFIXES)
        full = self.output(sources, full=True)
        full_summary = summary_lines(full, BASELINE_SUMMARY_PREFIXES)
        self.assertEqual(summary[-1], "Source problems: 1. Use --full to print the tables.")
        self.assertEqual(full_summary[-1], "Source problems: 1.")
        self.assertEqual(full_summary[:-1], summary[:-1])
        self.assertNotIn("Use --full", full)
        self.assertIn("Capacity on Hana", full)

    def test_default_cli_prints_short_summary_in_user_zone_without_tables(self) -> None:
        output = self.output(self.sources())
        lines = summary_lines(output, BASELINE_SUMMARY_PREFIXES)
        self.assertGreaterEqual(len(lines), 3)
        self.assertLessEqual(len(lines), 5)
        self.assertEqual(phone_overflow_lines(output), [])
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
        self.assertEqual(summary_lines(output, BASELINE_SUMMARY_PREFIXES),
                         [line.replace(" Use --full to print the tables.", "") for line in summary_lines(summary, BASELINE_SUMMARY_PREFIXES)])
        self.assertIn("\n\nCapacity on Hana\n", output)
        rows = {row.measure: row.value for row in read_report_blocks(output)}
        self.assertEqual(phone_overflow_lines(output), [])
        self.assertIn(f"Goal 1: {rows['goal 1 issues closed']} issues closed; "
                      + f"{rows['goal 1 declined closures excluded']} declined closures left out.", " ".join(summary.split()))
        self.assertIn(f"Hana: {rows['Hana merges / origin/init/catalyst']} merges to origin/init/catalyst; "
                      + f"{rows['goal 1 Hana-project issues closed']} Hana-project issues closed.", " ".join(summary.split()))
        self.assertIn(f"User attention: {rows['phone pushes sent']} pushes sent to user; "
                      + f"{rows['phone pushes after failed review']} after a failed text check.", " ".join(summary.split()))

    def test_full_baseline_preserves_every_row_field_in_phone_blocks(self) -> None:
        sources = self.sources()
        sources.problems = [log_records.SourceProblem("fixture history", "history was not recorded for this phase")]
        now = self.window.until
        snapshot = f"snapshot {now.astimezone(user_zone()):%Y-%m-%d %H:%M %Z}"
        expected = [
            *m.capacity(sources.units, sources.workers, snapshot),
            *m.resource_use(sources.builds, sources.tokens, self.window),
            *m.phase_flow(sources.phases, self.window, sources.wait_history),
            *m.issue_flow(sources.issue_records, self.window),
            *m.merge_flow(sources.merges, self.window),
            *m.receipt_rows(sources.receipts, sources.merges, self.window),
            *m.attention(sources.push_report, sources.messages),
            *m.pending_attention(sources.waiting, now, self.window),
            *m.rank_coverage(sources.issue_records, sources.rankings, sources.units, snapshot),
            *log_records.source_problem_rows(sources.problems, self.window),
        ]
        output = m.render(sources, self.window, now, full=True)
        self.assertEqual(read_report_blocks(output), expected)
        self.assertEqual(phone_overflow_lines(output), [])
        self.assertEqual(phone_overflow_lines(m.render(sources, self.window, now)), [])

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
        self.assertEqual(len(summary_lines(summary, BASELINE_SUMMARY_PREFIXES)), 5)
        self.assertEqual(summary_lines(output, BASELINE_SUMMARY_PREFIXES),
                         [line.replace(" Use --full to print the tables.", "") for line in summary_lines(summary, BASELINE_SUMMARY_PREFIXES)])
        self.assertIn("\n\nCapacity on Hana\n", output)
        self.assertNotIn("Hana wait /", summary)
        rows = row_cells(output)
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
        self.assertIn("Hana: not recorded merges to origin/init/catalyst; 2 Hana-project issues closed.",
                      " ".join(summary.split()))
        self.assertEqual(phone_overflow_lines(summary), [])
        self.assertEqual(row_cells(output)["Hana merges / origin/init/catalyst"][1:3], ["not recorded", "0"])

    def test_source_problem_count_and_full_hint_show_with_and_without_problems(self) -> None:
        sources = self.sources()
        self.assertIn("Source problems: 0. Use --full to print the tables.", summary_lines(self.output(sources), BASELINE_SUMMARY_PREFIXES))
        sources.problems = [log_records.SourceProblem("reviews", "missing review file"),
                            log_records.SourceProblem("builds", "cannot read build file")]
        summary = self.output(sources)
        self.assertIn("Source problems: 2. Use --full to print the tables.", summary_lines(summary, BASELINE_SUMMARY_PREFIXES))
        self.assertNotIn("missing review file", summary)
        output = self.output(sources, full=True)
        problems = [row for row in read_report_blocks(output) if row.measure == "source unavailable"]
        self.assertEqual(len(problems), 2)
        self.assertEqual([row.value for row in problems], ["missing review file", "cannot read build file"])


class ComparisonTest(unittest.TestCase):
    before_window: log_records.Window = log_records.Window(
        log_records.required_time("2026-10-13T17:24:00-04:00"),
        log_records.required_time("2026-10-20T17:24:00-04:00"),
    )
    after_window: log_records.Window = log_records.Window(
        before_window.until, before_window.until + timedelta(days=28),
    )
    covered_before_window: log_records.Window = log_records.Window(
        before_window.since - timedelta(days=35), before_window.until - timedelta(days=35),
    )
    covered_after_window: log_records.Window = log_records.Window(
        covered_before_window.until, covered_before_window.until + timedelta(days=28),
    )

    def part(self, window: log_records.Window, count: int, failed: int = 0) -> m.ComparisonWindow:
        at = window.since + timedelta(hours=1)
        pushes = [push_fixture(at, f"push-{index}") for index in range(count)]
        reviews: list[log_records.JsonObject] = [
            {"time": at.isoformat(), "source": "fixture-unit", "verdict": "failed",
             "sent": {"title": f"push-{index}", "body": f"push-{index}"}}
            for index in range(failed)
        ]
        found = [Issue(Path(f"closed-{index}.md"), {
            "status": ["closed"], "backlog_goal": ["1 - Ship Hana"], "project": ["hana"],
            "date_active": [window.since.isoformat()], "date_closed": [at.isoformat()],
        }) for index in range(count)]
        waits = [wait_fixture(f"wait-{index}", "WaitingForQuotaRefusal", at + timedelta(minutes=index), 1)
                 for index in range(count)]
        history = m.parse_wait_report(wait_report_fixture(window, waits, [wait_phase_fixture(wait_n=count)]))
        phases = [m.PhaseCompletion("finish", at + timedelta(hours=1), True, 7200,
                                    m.WaitPhaseInstance("hana-run", "hana-phase"), window.since)]
        sources = m.BaselineSources(
            [m.Unit("hana", "current-unit", "hana", "/current", True, "")], [], [], [], phases,
            notification_report(pushes, reviews, window), [], found, [],
            [m.WaitingItem("changes", at)], [], [m.HanaMerge(f"merge-{index}", at) for index in range(count)],
            history,
        )
        return m.ComparisonWindow(window, sources, {source: m.RecordedCoverage((window,)) for source in m.EventSource},
                                  (), (), (), m.MemoryNotRecorded("fixture has no memory samples"))

    def rows(self, before: m.ComparisonWindow, after: m.ComparisonWindow) -> dict[str, log_records.MeasureRow]:
        return {row.measure: row for row in m.comparison_rows(before, after, self.after_window.until)}

    def load_fixture_home(
        self, home: Path, windows: tuple[log_records.Window, log_records.Window],
    ) -> tuple[m.ComparisonWindow, m.ComparisonWindow]:
        with patch.object(Path, "home", return_value=home), \
                patch.object(audit, "SESSION_CONFIG", home / "config/hierarchies/sessions.toml"), \
                patch.object(m, "COVERAGE_CONFIG", home / "config/hierarchies/coverage.toml"), \
                patch.object(audit, "read_owners", return_value=audit.ProductionOwnership({}, {})), \
                patch.object(m, "read_units", return_value=[]), \
                patch.object(m, "read_workers", return_value=([], [])), \
                patch.object(collect, "store_path", return_value=home / "usage.sqlite"), \
                patch.object(unit_runs, "history_root", return_value=home / "history"):
            return m.load_comparison(*windows)

    def test_coverage_file_drives_live_loader_rates_and_leaves_unclaimed_sources_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            before_window, after_window = self.covered_before_window, self.covered_after_window
            notify = home / ".local/state/notify"
            notify.mkdir(parents=True)
            config = home / "config/hierarchies/coverage.toml"
            config.parent.mkdir(parents=True)
            _ = config.with_name("sessions.toml").write_text('[fixture-unit]\ndisplay_name="Fixture"\n')
            pushes = [push_fixture(window.since + timedelta(hours=1), f"{label}-{index}")
                      for label, window, n in (("before", before_window, 2), ("after", after_window, 8))
                      for index in range(n)]
            _ = (notify / "pushover.jsonl").write_text("".join(json.dumps(row) + "\n" for row in pushes))
            _ = (notify / "review.jsonl").write_text("")
            messages = home / ".local/state/message/log.jsonl"
            messages.parent.mkdir(parents=True)
            _ = messages.write_text("")
            _ = config.write_text('["notification history"]\nsince="' + before_window.since.isoformat()
                                  + '"\nevidence="fixture commit starts continuous delivery logging"\n'
                                  + '[["notification history".gaps]]\nfrom="' + after_window.since.isoformat()
                                  + '"\nto="' + (after_window.since + timedelta(days=14)).isoformat()
                                  + '"\nreason="logging paused"\n')
            original = {path: path.read_bytes() for path in (config, notify / "pushover.jsonl", messages)}
            before, after = self.load_fixture_home(home, (before_window, after_window))
            rows = self.rows(before, after)
            pushes_row = rows["phone pushes sent"]
            self.assertEqual(pushes_row.value,
                             "before: 2.0/week; 7 covered days; after: 0.0/week; 14 covered days (partial); "
                             + "change: -2.0/week; n before=2; after=0")
            self.assertEqual(pushes_row.n, 0)
            self.assertIsInstance(after.coverage[m.EventSource.MESSAGES], m.CoverageUnknown)
            self.assertTrue(all(isinstance(part.coverage[m.EventSource.PUSHES], m.RecordedCoverage)
                                for part in (before, after)))
            self.assertEqual({path: path.read_bytes() for path in original}, original)
            _ = config.write_bytes(original[config] + b'\n["merge history"]\nsince="no time"\nevidence="invalid entry"\n')
            before, after = self.load_fixture_home(home, (before_window, after_window))
            self.assertEqual(self.rows(before, after)["phone pushes sent"], pushes_row)
            for part in (before, after):
                errors = [problem for problem in part.sources.problems if problem.source == str(config)]
                self.assertEqual(len(errors), 1)
                self.assertIn("merge history", errors[0].reason)
            _ = config.write_text("")
            before, after = self.load_fixture_home(home, (before_window, after_window))
            self.assertIsInstance(after.coverage[m.EventSource.PUSHES], m.CoverageUnknown)
            unclaimed = self.rows(before, after)["phone pushes sent"]
            self.assertIn("coverage unknown", unclaimed.value)
            self.assertIn("n before=2; after=8", unclaimed.value)
            self.assertEqual(unclaimed.n, 8)
            _ = config.write_text("[")
            before, after = self.load_fixture_home(home, (before_window, after_window))
            malformed = self.rows(before, after)["phone pushes sent"]
            self.assertEqual(malformed, unclaimed)
            output = m.render_compare(before, after, self.after_window.until, full=True)
            self.assertIn(str(config), output)
            self.assertIn("coverage config", " ".join(output.split()))
            self.assertEqual(phone_overflow_lines(output), [])

    def test_wait_rates_exclude_gap_and_pre_window_starts_without_losing_cause_minutes(self) -> None:
        part = self.part(self.after_window, 0)
        at = part.window.since
        records: list[log_records.JsonObject] = []
        starts = [(f"gap-{index}", at + timedelta(days=2, minutes=index * 3)) for index in range(8)]
        starts.extend((("crossing", at - timedelta(minutes=1)), ("covered", at + timedelta(days=16))))
        for identity, started in starts:
            record = wait_fixture(identity, "WaitingForQuotaRefusal", started, 2)
            record["holders"] = [
                holder_observation(started, named_holder_fixture("account", "worker/account-a")),
                holder_observation(started + timedelta(minutes=1), {
                    "state": "HolderNotNamed", "reason": "account holder not recorded",
                }),
            ]
            records.append(record)
        part = self.with_waits(part, records, (
            m.CauseProject("session:other-project-director", m.NonHanaProject("build-tools")),
        ))
        part.coverage[m.EventSource.WAITS] = m.RecordedCoverage((
            log_records.Window(at, at + timedelta(days=1)),
            log_records.Window(at + timedelta(days=15), part.window.until),
        ))
        total = self.rows(part, part)["Hana wait / WaitingForQuotaRefusal"]
        self.assertEqual(total.n, 1)
        self.assertIn("before: 1.0 min/week; 14 covered days (partial); waits 0.5/week", total.value)
        self.assertIn("after: 1.0 min/week; 14 covered days (partial); waits 0.5/week", total.value)
        detailed = m.wait_cause_rows(part)
        for identity, _ in starts:
            with self.subTest(wait=identity):
                causes = [row for row in detailed if row.measure == "Hana wait cause / " + identity]
                self.assertEqual(len(causes), 1 if identity == "crossing" else 2)
                self.assertTrue(all(row.n == 1 for row in causes))
                expected_rate = "0.5/week" if identity == "covered" else "0.0/week"
                self.assertTrue(all("1.0 min; " + expected_rate + "; 14 covered days (partial)" in row.value
                                    for row in causes))
        output = m.render_compare(part, part, self.after_window.until, full=True)
        displayed = read_report_blocks(output)
        for identity, _ in starts:
            named = [row for row in displayed if row.measure == "Hana wait cause / " + identity]
            if identity == "crossing":
                self.assertEqual(named, [])
                continue
            self.assertEqual(len(named), 2)
            self.assertEqual([row.n for row in named], [1, 1])
            expected_rate = "0.5/week" if identity == "covered" else "0.0/week"
            self.assertTrue(all(row.value == "non-Hana cause session:other-project-director; project build-tools; "
                                + "1.0 min; " + expected_rate + "; 14 covered days (partial)" for row in named))
        for label, minutes, wait_n in (("holder named", 9, 9), ("holder not named", 10, 10), ("cause not recorded", 10, 10)):
            grouped = [row for row in displayed if row.measure == "Hana wait causes / WaitingForQuotaRefusal / " + label]
            self.assertEqual(len(grouped), 2)
            self.assertEqual([row.n for row in grouped], [wait_n, wait_n])
            self.assertTrue(all(row.value == f"{label}; {minutes:.1f} min; waits 0.5/week; 14 covered days (partial)"
                                for row in grouped))
        self.assertEqual(phone_overflow_lines(output), [])

    def test_full_comparison_keeps_one_block_per_measure_as_event_counts_grow(self) -> None:
        reports: list[list[log_records.MeasureRow]] = []
        for before_n, after_n in ((2, 8), (60, 240)):
            before, after = self.part(self.before_window, before_n), self.part(self.after_window, after_n)
            for part in (before, after):
                history = part.sources.wait_history
                if not isinstance(history, m.HanaWaitHistory):
                    self.fail("comparison fixture lost its waits")
                first = replace(history.waits[0], kind=m.WaitKind.SUPPLIER,
                                expected_supplier=m.NamedSupplier("unit:build-tools"))
                memory = replace(history.waits[0], identity="two-holders", kind=m.WaitKind.CAPACITY,
                                 holders=m.HolderHistory((
                                     m.HolderObservation(first.started, m.NamedHolder(
                                         m.HolderResourceKind.MEMORY, "natedev", m.SessionHolder("first-builder"))),
                                     m.HolderObservation(first.started + timedelta(seconds=30), m.NamedHolder(
                                         m.HolderResourceKind.MEMORY, "natedev", m.SessionHolder("second-builder"))),
                                 )))
                part.sources.wait_history = replace(history, waits=(first, *history.waits[1:], memory))
            projects = (
                m.CauseProject("unit:build-tools", m.NonHanaProject("build-tools")),
                m.CauseProject("session:first-builder", m.NonHanaProject("first-tools")),
                m.CauseProject("session:second-builder", m.NonHanaProject("second-tools")),
            )
            before, after = replace(before, projects=projects), replace(after, projects=projects)
            with patch.object(m, "load_comparison", return_value=(before, after)), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(m.main([
                    "compare", "--before", before.window.since.isoformat() + ".." + before.window.until.isoformat(),
                    "--after", after.window.since.isoformat() + ".." + after.window.until.isoformat(), "--full",
                ]), 0)
            text = output.getvalue()
            rows = read_report_blocks(text)
            reports.append(rows)
            self.assertEqual(phone_overflow_lines(text), [])
            self.assertEqual(len({(row.measure, row.window) for row in rows}), len(rows))
            total = next(row for row in rows if row.measure == "Hana wait / WaitingForQuotaRefusal")
            self.assertIn(f"n before={before_n - 1}; after={after_n - 1}", total.value)
            self.assertEqual(total.n, after_n - 1)
            causes = [row for row in rows if "non-Hana cause unit:build-tools" in row.value]
            self.assertEqual([(row.n, "1.0 min" in row.value) for row in causes], [(1, True), (1, True)])
            self.assertTrue(all("wait-0" in row.measure for row in causes))
            for part, wait_n, covered_days in ((before, before_n - 1, 7), (after, after_n - 1, 28)):
                memory_causes = [row for row in rows if row.measure == "Hana wait cause / two-holders"
                                 and row.window == part.window.label]
                self.assertEqual(len(memory_causes), 1)
                memory_cause = memory_causes[0]
                self.assertEqual((memory_cause.value, memory_cause.n), (
                    "non-Hana cause session:first-builder; project first-tools; 0.5 min; "
                    + f"{7 / covered_days:.1f}/week; {covered_days} covered days; "
                    + "non-Hana cause session:second-builder; project second-tools; 0.5 min; "
                    + f"{7 / covered_days:.1f}/week; {covered_days} covered days", 1,
                ))
                grouped = next(row for row in rows if row.measure ==
                               "Hana wait causes / WaitingForQuotaRefusal / cause not recorded"
                               and row.window == part.window.label)
                self.assertEqual((grouped.value, grouped.n), (
                    f"cause not recorded; {wait_n:.1f} min; waits {wait_n * 7 / covered_days:.1f}/week; "
                    + f"{covered_days} covered days", wait_n,
                ))
                holder = next(row for row in rows if row.measure ==
                              "Hana wait causes / WaitingForQuotaRefusal / holder not named"
                              and row.window == part.window.label)
                self.assertEqual((holder.value, holder.n), (
                    f"holder not named; {wait_n:.1f} min; waits {wait_n * 7 / covered_days:.1f}/week; "
                    + f"{covered_days} covered days", wait_n,
                ))
            self.assertNotIn("wait-1", text)
        self.assertEqual([(row.measure, row.window) for row in reports[0]],
                         [(row.measure, row.window) for row in reports[1]])

    def test_bad_build_rows_keep_good_minutes_and_one_diagnostic_in_baseline_and_compare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            before_window, after_window = self.covered_before_window, self.covered_after_window
            path = home / ".local/state/buildlog/index.sqlite"
            path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(path)) as db:
                _ = db.execute("CREATE TABLE steps(repo,step,started_at,duration_s,host)")
                _ = db.executemany("INSERT INTO steps VALUES ('hana','check',?,?,?)", [
                    (before_window.since.isoformat(), 120, "natedev"),
                    (before_window.since.isoformat(), float("inf"), "natedev"),
                    (after_window.since.isoformat(), 240, "natedev"),
                    (after_window.since.isoformat(), "bad seconds", "natedev"),
                    (after_window.since.isoformat(), "ignored machine", "mac"),
                ])
                db.commit()
            original = path.read_bytes()
            config = home / "config/hierarchies/coverage.toml"
            config.parent.mkdir(parents=True)
            _ = config.write_text('[buildlog]\nsince="' + before_window.since.isoformat()
                                  + '"\nevidence="fixture writer enabled"\n')
            before, after = self.load_fixture_home(home, (before_window, after_window))
            for part, minutes in ((before, "2.0"), (after, "4.0")):
                problems = [problem for problem in part.sources.problems if problem.source == str(path)]
                self.assertEqual(len(problems), 1)
                self.assertIn("invalid build rows skipped: 2", problems[0].reason)
                self.assertIn("seconds: expected a finite number", problems[0].reason)
                self.assertIs(problems[0].event_source, m.EventSource.BUILDS)
                self.assertEqual(problems[0].kind, log_records.RowsSkipped(2))
                self.assertIsInstance(part.coverage[m.EventSource.BUILDS], m.RecordedCoverage)
                self.assertEqual([step.seconds for step in part.sources.builds], [120, 240])
                baseline = read_report_blocks(m.render(part.sources, part.window, self.after_window.until, full=True))
                total = next(row for row in baseline if row.measure == "build/check minutes / hana")
                self.assertEqual((total.value, total.n), (minutes, 1))
                diagnostic = [row for row in baseline if row.measure == "source unavailable" and row.source == str(path)]
                self.assertEqual(len(diagnostic), 1)
                self.assertEqual(diagnostic[0].value, problems[0].reason)
            compared = self.rows(before, after)["build/check minutes / hana"]
            self.assertEqual(compared.value,
                             "before: 2.0 min/week; 7 covered days; incomplete: 2 rows skipped; "
                             + "after: 1.0 min/week; 28 covered days; incomplete: 2 rows skipped; "
                             + "change: -1.0/week; n before=1; after=1")
            self.assertEqual(compared.n, 1)
            output = m.render_compare(before, after, self.after_window.until, full=True)
            diagnostic = [row for row in read_report_blocks(output)
                          if row.measure == "source unavailable" and row.source == str(path)]
            self.assertEqual(len(diagnostic), 1)
            self.assertIn("invalid build rows skipped: 2", diagnostic[0].value)
            self.assertEqual(phone_overflow_lines(output), [])
            self.assertEqual(path.read_bytes(), original)

    def test_comparison_normalizes_event_counts_and_changes_over_unequal_windows(self) -> None:
        before, after = self.part(self.before_window, 2, 1), self.part(self.after_window, 12, 2)
        rows = self.rows(before, after)
        for name in ("Hana merges / origin/init/catalyst", "goal 1 issues closed", "phone pushes sent"):
            with self.subTest(measure=name):
                self.assertEqual(rows[name].value,
                                 "before: 2.0/week; 7 covered days; after: 3.0/week; 28 covered days; "
                                 + "change: +1.0/week; n before=2; after=12")
                self.assertEqual(rows[name].n, 12)
                self.assertIn(before.window.label, rows[name].window)
                self.assertIn(after.window.label, rows[name].window)
        failed = rows["phone pushes after failed review"]
        self.assertEqual(failed.value, "before: 1.0/week; 7 covered days; after: 0.5/week; 28 covered days; "
                                      + "change: -0.5/week; n before=2; after=12")
        self.assertEqual(failed.n, 12)
        wait = rows["Hana wait / WaitingForQuotaRefusal"]
        self.assertIn("before: 2.0 min/week; 7 covered days; waits 2.0/week", wait.value)
        self.assertIn("after: 3.0 min/week; 28 covered days; waits 3.0/week", wait.value)
        self.assertIn("change: +1.0/week", wait.value)
        self.assertIn("progress_history.py waits", wait.source)
        missing = rows["question answer time"]
        self.assertIn("before: not recorded; after: not recorded; change: not recorded", missing.value)
        self.assertIn("48-hour", missing.source)

    def test_comparison_keeps_every_baseline_row_including_one_sided_rows(self) -> None:
        before, after = self.part(self.before_window, 2), self.part(self.after_window, 4)
        before.sources.builds = [m.BuildStep("before-only-repo", "check", self.before_window.since, 60)]
        after.sources.builds = [m.BuildStep("after-only-repo", "check", self.after_window.since, 120)]
        rows = m.comparison_rows(before, after, self.after_window.until)
        expected = {row.measure for part in (before, after)
                    for row in read_report_blocks(m.render(part.sources, part.window, self.after_window.until, full=True))}
        daily_merges = {"Hana merges / " + (part.window.since + timedelta(days=day)).date().isoformat()
                        for part in (before, after) for day in range((part.window.until - part.window.since).days + 1)}
        event_details = daily_merges | {name for name in expected if name.startswith((
            "Hana wait holder /", "Hana closure merge commit /", "phone pushes /", "messages /",
        ))}
        self.assertTrue((expected - event_details).issubset({row.measure for row in rows}),
                        sorted(expected - event_details - {row.measure for row in rows}))
        self.assertTrue(event_details.isdisjoint(row.measure for row in rows))
        values = {row.measure: row.value for row in rows}
        self.assertIn("comparison unavailable (no recorded after row)", values["build/check minutes / before-only-repo"])
        self.assertIn("comparison unavailable (no recorded before row)", values["build/check minutes / after-only-repo"])
        for name in ("Hana merges / origin/init/catalyst", "goal 1 issues closed", "phone pushes sent"):
            with self.subTest(aggregate=name):
                row = next(row for row in rows if row.measure == name)
                self.assertEqual((row.value, row.n), (
                    "before: 2.0/week; 7 covered days; after: 1.0/week; 28 covered days; "
                    + "change: -1.0/week; n before=2; after=4", 4,
                ))
        for kind in m.SenderKind:
            with self.subTest(message_kind=kind.value):
                row = next(row for row in rows if row.measure == "messages / " + kind.value)
                self.assertEqual((row.value, row.n), (
                    "before: 0.0/week; 7 covered days; after: 0.0/week; 28 covered days; "
                    + "change: +0.0/week; n before=0; after=0", 0,
                ))

    def test_receipt_rates_use_receipt_coverage_and_keep_digit_leading_missing_hashes(self) -> None:
        before, after = self.part(self.before_window, 2), self.part(self.after_window, 2)
        missing_hash = "1234567890" + "a" * 30
        for part, commits in ((before, ("b" * 40, missing_hash)), (after, ("c" * 40, "d" * 40))):
            at = part.window.since + timedelta(hours=1)
            part.sources.merges = [m.HanaMerge(commit, at) for commit in commits]
            part.sources.receipts = tuple(m.parse_receipt(receipt_fixture(commit, at))
                                          for commit in commits if commit != missing_hash)
        before.coverage[m.EventSource.PHASES] = m.SourceUnavailable("phase history unavailable")
        before.coverage[m.EventSource.RECEIPTS] = m.RecordedCoverage((log_records.Window(
            before.window.since, before.window.since + timedelta(days=3.5)),))
        after.coverage[m.EventSource.PHASES] = m.RecordedCoverage((after.window,))
        after.coverage[m.EventSource.RECEIPTS] = m.RecordedCoverage((log_records.Window(
            after.window.since, after.window.since + timedelta(days=14)),))
        rows = self.rows(before, after)
        total = rows["merge receipts / all projects"]
        self.assertEqual(total.value,
                         "before: 2.0/week; 3.5 covered days (partial); after: 1.0/week; 14 covered days (partial); "
                         + "change: -1.0/week; n before=1; after=2")
        self.assertEqual(total.n, 2)
        self.assertIn("merge_receipts.jsonl", total.source)
        baseline = read_report_blocks(m.render(before.sources, before.window, self.after_window.until, full=True))
        missing_baseline = [row for row in baseline if row.measure == "Hana merge / ReceiptMissing"]
        self.assertEqual([(row.value, row.n) for row in missing_baseline], [(missing_hash, 1)])
        missing = rows["Hana merge / ReceiptMissing"]
        self.assertEqual(missing.value, "before: 2.0/week; 3.5 covered days (partial); "
                         + "after: comparison unavailable (no recorded after row); change: not recorded; n before=1; after=0")
        self.assertEqual(missing.n, 0)
        output = m.render_compare(before, after, self.after_window.until, full=True)
        displayed = {row.measure: row for row in read_report_blocks(output)}
        self.assertEqual(displayed[missing.measure], missing)
        self.assertEqual(displayed[total.measure], total)
        self.assertEqual(phone_overflow_lines(output), [])

    def test_empty_partial_missing_and_unknown_coverage_have_distinct_rate_results(self) -> None:
        before, after = self.part(self.before_window, 0), self.part(self.after_window, 4)
        before.coverage[m.EventSource.MERGES] = m.RecordedCoverage((self.before_window,))
        partial = log_records.Window(after.window.since, after.window.since + timedelta(days=14))
        after.coverage[m.EventSource.MERGES] = m.RecordedCoverage((partial, partial))
        value = self.rows(before, after)["Hana merges / origin/init/catalyst"].value
        self.assertEqual(value, "before: 0.0/week; 7 covered days; after: 2.0/week; 14 covered days (partial); "
                                + "change: +2.0/week; n before=0; after=4")
        for coverage, reason in ((m.SourceUnavailable("file missing"), "source unavailable: file missing"),
                                 (m.CoverageUnknown("coverage stamp absent"), "coverage unknown: coverage stamp absent")):
            with self.subTest(coverage=coverage):
                after.coverage[m.EventSource.MERGES] = coverage
                row = self.rows(before, after)["Hana merges / origin/init/catalyst"]
                self.assertIn("after: not recorded (" + reason + ")", row.value)
                self.assertIn("change: not recorded", row.value)
                self.assertEqual(row.n, 4)

    def test_before_snapshots_use_the_recorded_rows_and_explain_list_visibility(self) -> None:
        before, after = self.part(self.before_window, 1), self.part(self.after_window, 1)
        current = m.capacity(before.sources.units, [], "snapshot now")[0]
        saved = log_records.MeasureRow(current.measure, "11", 11, "snapshot 2026-10-20 17:24 EDT", "saved Baseline",
                                       quantity=log_records.Snapshot(11))
        waiting = log_records.MeasureRow("waiting / changes", "315; oldest not recorded", 315, saved.window, "saved Baseline",
                                         quantity=log_records.Snapshot(315, visibility_changed=True))
        before = replace(before, snapshot_rows=(saved, waiting))
        rows = self.rows(before, after)
        self.assertIn("before: 11; after: 1; change: -10.0", rows[current.measure].value)
        self.assertIn(saved.window, rows[current.measure].window)
        self.assertIn("before: 315; oldest not recorded", rows["waiting / changes"].value)
        self.assertIn("visibility changed", rows["waiting / changes"].value)
        self.assertIn("not backlog growth", rows["waiting / changes"].value)
        self.assertNotIn("/week", rows["waiting / changes"].value)
        without_snapshot = self.rows(self.part(self.before_window, 1), after)[current.measure]
        self.assertIn("before: comparison unavailable", without_snapshot.value)
        self.assertIn("change: not recorded", without_snapshot.value)
        output = log_records.blocks("Comparison", list(rows.values()))
        self.assertEqual(phone_overflow_lines(output), [])
        self.assertEqual({row.measure: row for row in read_report_blocks(output)}, rows)

    def test_compare_cli_uses_explicit_windows_and_keeps_full_rows_below_the_summary(self) -> None:
        before, after = self.part(self.before_window, 2, 1), self.part(self.after_window, 12, 2)
        arguments = ["compare", "--before", before.window.since.isoformat() + ".." + before.window.until.isoformat(),
                     "--after", after.window.since.isoformat() + ".." + after.window.until.isoformat()]
        outputs: list[str] = []
        render_times: list[datetime] = []
        for full in (False, True):
            with patch.object(m, "load_comparison", return_value=(before, after)) as load, \
                    patch.object(m, "render_compare", wraps=m.render_compare) as render, \
                    redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(m.main([*arguments, *(["--full"] if full else [])]), 0)
            load.assert_called_once_with(before.window, after.window)
            render_times.append(cast(datetime, render.call_args.args[2]))
            outputs.append(captured.getvalue())
        summary, full_output = outputs
        self.assertEqual(read_report_blocks(summary), [])
        self.assertIn("Use --full to print the tables.", " ".join(summary.split()))
        self.assertNotIn("Use --full", full_output)
        self.assertEqual(phone_overflow_lines(summary), [])
        self.assertEqual(phone_overflow_lines(full_output), [])
        expected = m.comparison_rows(before, after, render_times[1])
        rows = read_report_blocks(full_output)
        displayed = [row for row in rows if "; n before=" in row.value]
        self.assertEqual(displayed, expected)
        self.assertTrue(all(before.window.label in row.window and after.window.label in row.window for row in displayed))

    def test_compare_date_only_end_includes_the_same_final_day_as_baseline(self) -> None:
        before, after = self.part(self.before_window, 0), self.part(self.after_window, 0)
        with patch.object(m, "load_comparison", return_value=(before, after)) as load, redirect_stdout(io.StringIO()):
            self.assertEqual(m.main([
                "compare", "--before", "2026-10-13..2026-10-19", "--after", "2026-10-23..2026-10-30",
            ]), 0)
        load.assert_called_once_with(
            log_records.Window(log_records.required_time("2026-10-13"), log_records.required_time("2026-10-20")),
            log_records.Window(log_records.required_time("2026-10-23"), log_records.required_time("2026-10-31")),
        )

    def test_comparison_loads_notifications_once_without_turning_attribution_failures_into_zero_deliveries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            notify = home / ".local/state/notify"
            notify.mkdir(parents=True)
            config = home / "config/hierarchies/sessions.toml"
            config.parent.mkdir(parents=True)
            pushes = [push_fixture(self.before_window.since, "Before"),
                      push_fixture(self.after_window.since, "After")]
            reviews: list[log_records.JsonObject] = [
                {"time": (self.after_window.since - timedelta(seconds=1)).isoformat(), "source": "fixture-unit",
                 "verdict": "failed", "sent": {"title": "After", "body": "After"}},
            ]
            push_path, review_path = notify / "pushover.jsonl", notify / "review.jsonl"
            _ = push_path.write_text("".join(json.dumps(record) + "\n" for record in pushes))
            _ = review_path.write_text("".join(json.dumps(record) + "\n" for record in reviews))
            for state in ("missing", "malformed"):
                with self.subTest(session_config=state):
                    if state == "malformed":
                        _ = config.write_text("[")
                    original = {path: path.read_bytes() for path in (push_path, review_path)}
                    with patch.object(audit, "load_sources", wraps=audit.load_sources) as load, \
                            patch.object(audit, "audit", wraps=audit.audit) as run_audit:
                        before, after = self.load_fixture_home(home, (self.before_window, self.after_window))
                    self.assertEqual(load.call_count, 1)
                    self.assertEqual([call.args[1] for call in run_audit.call_args_list], [self.before_window, self.after_window])
                    self.assertEqual([len(part.sources.push_report.delivered_pushes) for part in (before, after)], [1, 1])
                    self.assertEqual([len(part.sources.push_report.entries) for part in (before, after)], [0, 1])
                    self.assertTrue(all(isinstance(part.coverage[m.EventSource.PUSHES], m.CoverageUnknown)
                                        for part in (before, after)))
                    row = self.rows(before, after)["phone pushes after failed review"]
                    self.assertEqual(row.n, 1)
                    self.assertIn("n before=1; after=1", row.value)
                    output = m.render_compare(before, after, self.after_window.until, full=True)
                    self.assertIn(str(config), output)
                    self.assertIn("unreadable session config", " ".join(output.split()))
                    self.assertNotIn("missing delivery history", output)
                    self.assertEqual(phone_overflow_lines(output), [])
                    self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_event_timestamps_do_not_claim_coverage_and_corrupt_sources_keep_their_reason(self) -> None:
        before, after = self.part(self.before_window, 2), self.part(self.after_window, 4)
        after = replace(after, coverage=m.comparison_coverage(after.sources))
        row = self.rows(before, after)["Hana merges / origin/init/catalyst"]
        self.assertIn("after: not recorded (coverage unknown:", row.value)
        self.assertEqual(row.n, 4)

        after.sources.problems.append(log_records.SourceProblem("Hana origin/init/catalyst merge log", "unreadable: invalid commit date",
                                                                 m.EventSource.MERGES, log_records.SourceUnreadable()))
        after = replace(after, coverage=m.comparison_coverage(after.sources))
        row = self.rows(before, after)["Hana merges / origin/init/catalyst"]
        self.assertIn("source unavailable: unreadable: invalid commit date", row.value)
        self.assertNotIn("after: 0.0/week", row.value)
        self.assertEqual(row.n, 4)

    def test_a_corrupt_only_run_keeps_coverage_and_marks_the_phase_rate_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "unrecognized-run-label.jsonl"
            _ = path.write_text("not json\n")
            original = path.read_bytes()
            before, after = self.part(self.before_window, 0), self.part(self.after_window, 0)
            after.sources.phases = m.read_phases(root, after.sources.problems)
            self.assertEqual(after.sources.phases, [])
            self.assertEqual(len(after.sources.problems), 1)
            self.assertEqual(after.sources.problems[0].source, str(path))
            self.assertIs(after.sources.problems[0].event_source, m.EventSource.PHASES)
            self.assertEqual(after.sources.problems[0].kind, log_records.RowsSkipped(1))
            row = self.rows(before, after)["Hana phases done"]
            self.assertIn("after: 0.0/week; 28 covered days", row.value)
            self.assertIn("incomplete: 1 rows skipped", row.value)
            self.assertIsInstance(after.coverage[m.EventSource.PHASES], m.RecordedCoverage)
            output = m.render_compare(before, after, self.after_window.until, full=True)
            self.assertIn(str(path), output)
            self.assertEqual(phone_overflow_lines(output), [])
            self.assertEqual(path.read_bytes(), original)

    def test_phase_read_failure_keeps_its_identity_when_a_state_read_has_the_same_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "runs"
            root.mkdir()
            path = root / "run.jsonl"
            _ = path.write_text("{}\n")
            problems: list[log_records.SourceProblem] = []
            with patch.object(Path, "open", side_effect=PermissionError("access denied")):
                self.assertEqual(m.read_source(str(path), path.read_text, "", problems,
                                              log_records.SourceNotApplicable.STATE), "")
                phases = m.read_phases(root, problems)
            self.assertEqual(phases, [])
            self.assertEqual([(problem.source, problem.reason) for problem in problems],
                             [(str(path), "unreadable: access denied")] * 2)
            source = self.part(self.covered_before_window, 0).sources
            source.phases = phases
            source.problems = problems
            config = home / "config/hierarchies/coverage.toml"
            config.parent.mkdir(parents=True)
            _ = config.write_text('["phase history"]\nsince="' + self.covered_before_window.since.isoformat()
                                  + '"\nevidence="fixture phase writer"\n')
            with patch.object(m, "load_window_sources", return_value=source):
                before, after = self.load_fixture_home(home, (self.covered_before_window, self.covered_after_window))
            for part in (before, after):
                self.assertIsInstance(part.coverage[m.EventSource.PHASES], m.SourceUnavailable)
            row = self.rows(before, after)["Hana phases done"]
            self.assertIn("source unavailable: unreadable: access denied", row.value)
            self.assertNotIn("0.0/week", row.value)
            output = m.render_compare(before, after, self.after_window.until, full=True)
            errors = [row for row in read_report_blocks(output) if row.measure == "source unavailable"
                      and row.source == str(path)]
            self.assertEqual(len(errors), 2)
            self.assertEqual([row.value for row in errors], ["unreadable: access denied"] * 2)
            self.assertEqual(phone_overflow_lines(output), [])

    def test_comparison_uses_the_row_data_when_all_display_labels_are_unrecognized(self) -> None:
        before, after = self.part(self.before_window, 0), self.part(self.after_window, 0)
        before.coverage[m.EventSource.PUSHES] = m.SourceUnavailable("delivery history unavailable")
        after.coverage[m.EventSource.PUSHES] = m.SourceUnavailable("delivery history unavailable")
        groups: list[list[tuple[str, list[log_records.MeasureRow]]]] = []
        for part in (before, after):
            row = next(row for row in m.phase_flow(part.sources.phases, part.window) if row.measure == "Hana phases done")
            groups.append([("opaque group", [replace(row, measure="opaque measure", source="opaque source", value="opaque value")])])
        with patch.object(m, "baseline_groups", side_effect=groups):
            row = self.rows(before, after)["opaque measure"]
        self.assertEqual((row.value, row.n, row.source), (
            "before: 1.0/week; 7 covered days; after: 0.2/week; 28 covered days; "
            + "change: -0.8/week; n before=1; after=1", 1, "opaque source",
        ))

    def test_recorded_baseline_snapshot_reads_saved_values_instead_of_current_state(self) -> None:
        rows = m.recorded_baseline_rows()
        units = [row for row in rows if row.measure.startswith("units / tool-based-ui /")]
        self.assertEqual([(row.value, row.n, row.window) for row in units],
                         [("11", 11, "snapshot 2026-10-20 17:24 EDT")])
        waiting = next(row for row in rows if row.measure == "waiting / changes")
        self.assertTrue(waiting.value.startswith("315;"))
        self.assertEqual(waiting.n, 315)
        self.assertTrue(all("snapshot" in row.window for row in units))

    def test_saved_baseline_capacity_count_drives_the_change_to_current_capacity(self) -> None:
        before, after = self.part(self.before_window, 0), self.part(self.after_window, 0)
        saved = m.recorded_baseline_rows()
        before = replace(before, snapshot_rows=saved)
        after.sources.units = [m.Unit("tool-based-ui", "current-hana", "/home/natepiano/rust/hana",
                                     "/current/hana", True, "")]
        measure = "units / tool-based-ui / /home/natepiano/rust/hana / Hana"
        row = self.rows(before, after)[measure]
        self.assertEqual(row.value, "before: 11; after: 1; change: -10.0; n before=11; after=1")
        self.assertIn("before snapshot 2026-10-20 17:24 EDT", row.window)
        self.assertNotIn("/week", row.value)

    def test_saved_baseline_fractions_suffixes_and_unknown_values_keep_their_meaning(self) -> None:
        rows = {row.measure: row for row in m.recorded_baseline_rows()}
        expected = {
            "waiting / changes": ("315; oldest 11.7 days", 315),
            "top 20 goal 1 open issues with a unit": ("2/20", log_records.Fraction(2, 20)),
            "top 20 goal 1 open issues covered score": ("140/604 (23.2%)", log_records.Fraction(140, 604, percent=True)),
        }
        for measure, (text, amount) in expected.items():
            with self.subTest(measure=measure):
                row = rows[measure]
                self.assertEqual(row.value, text)
                if not isinstance(row.quantity, log_records.Snapshot):
                    self.fail("saved state lost its snapshot type")
                self.assertEqual(row.quantity.amount, amount)
                self.assertTrue(row.window.startswith("snapshot 2026-10-20"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.md"
            _ = path.write_text("## Baseline\n\nunknown state  value not understood  17  "
                                + "snapshot 2026-10-20 17:24 EDT  saved fixture\n### Prior-week Hana merges\n")
            unknown, = m.recorded_baseline_rows(path)
            self.assertEqual((unknown.value, unknown.n, unknown.window),
                             ("value not understood", 17, "snapshot 2026-10-20 17:24 EDT"))
            if not isinstance(unknown.quantity, log_records.Snapshot):
                self.fail("saved state lost its snapshot type")
            self.assertEqual(unknown.quantity.amount, log_records.NotRecorded("value not understood"))

    def with_waits(
        self, part: m.ComparisonWindow, records: Sequence[log_records.JsonObject],
        projects: tuple[m.CauseProject, ...] = (),
    ) -> m.ComparisonWindow:
        history = m.parse_wait_report(wait_report_fixture(part.window, records, [wait_phase_fixture(wait_n=len(records))]))
        return replace(part, sources=replace(part.sources, wait_history=history), projects=projects)

    def projects_from_history(
        self, records: Sequence[log_records.JsonObject], units: Sequence[m.Unit] = (),
    ) -> tuple[m.CauseProject, ...]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "retired.jsonl"
            _ = path.write_text("".join(json.dumps(record) + "\n" for record in records))
            original = path.read_bytes()
            problems: list[log_records.SourceProblem] = []
            projects = m.read_cause_projects(root, units, problems)
            self.assertEqual(problems, [])
            self.assertEqual(path.read_bytes(), original)
            return projects

    def test_two_unavailable_wait_histories_keep_each_reason_and_no_numeric_change(self) -> None:
        before, after = self.part(self.before_window, 0), self.part(self.after_window, 0)
        before.sources.wait_history = m.HistoryNotRecorded("before wait ledger absent")
        after.sources.wait_history = m.HistoryNotRecorded("after wait ledger absent")
        row = self.rows(before, after)["Hana wait coverage / HistoryNotRecorded"]
        self.assertIn("before: not recorded", row.value)
        self.assertIn("before wait ledger absent", row.value)
        self.assertIn("after: not recorded", row.value)
        self.assertIn("after wait ledger absent", row.value)
        self.assertIn("change: not recorded", row.value)
        self.assertEqual(row.n, 0)

    def test_retired_hana_production_keeps_its_classification_with_a_tools_repository(self) -> None:
        projects = self.projects_from_history([{
            "event_type": "phase_started", "unit": {"state": "Recorded", "value": "retired-ui"},
            "main_agent": {"session_id": "retired-ui-director"},
            "production": {"state": "Recorded", "value": "tool-based-ui"},
            "repository": {"state": "Recorded", "value": "claude"},
        }])
        part = self.part(self.before_window, 0)
        record = wait_fixture("ui-holder", "WaitingForMemoryOrSlot", part.window.since, 3)
        holder = named_holder_fixture("memory", "natedev")
        holder["holder"] = {"state": "SessionHolder", "session_id": "retired-ui-director"}
        record["holders"] = [holder_observation(part.window.since, holder)]
        part = self.with_waits(part, [record], projects)
        history = part.sources.wait_history
        if not isinstance(history, m.HanaWaitHistory):
            self.fail("recorded wait was lost")
        intervals = m.wait_causes(history.waits[0], projects, part.window)
        self.assertEqual(len(intervals), 1)
        self.assertIsInstance(intervals[0].cause, m.HanaCause)
        output = " ".join(m.render_compare(part, part, self.after_window.until, full=True).split())
        self.assertIn("0 named wait causes", output)
        self.assertNotIn("HI10 proposal evidence recorded", output)

    def test_active_unit_and_its_repository_history_have_one_project_identity(self) -> None:
        unit = m.Unit("builds", "active-builds", "build-tools", "/active/build-tools", False, "")
        projects = self.projects_from_history([{
            "event_type": "phase_started", "unit": {"state": "Recorded", "value": unit.name},
            "main_agent": {"session_id": "builds-director"},
            "production": {"state": "Recorded", "value": "builds"},
            "repository": {"state": "Recorded", "value": "build-tools"},
        }], [unit])
        part = self.part(self.before_window, 0)
        record = wait_fixture("active-holder", "WaitingForMemoryOrSlot", part.window.since, 3)
        record["holders"] = [holder_observation(part.window.since, {
            "state": "NamedHolder", "resource_kind": "memory", "resource": "natedev",
            "holder": {"state": "UnitHolder", "unit": unit.name},
        })]
        part = self.with_waits(part, [record], projects)
        history = part.sources.wait_history
        if not isinstance(history, m.HanaWaitHistory):
            self.fail("recorded wait was lost")
        causes = m.wait_causes(history.waits[0], projects, part.window)
        self.assertEqual(len(causes), 1)
        self.assertIsInstance(causes[0].cause, m.NonHanaCause)
        self.assertEqual((causes[0].until - causes[0].started).total_seconds(), 180)
        self.assertEqual(len({project.project for project in projects if project.subject == "unit:" + unit.name}), 1)
        self.assertIn("HI10 proposal evidence recorded", " ".join(m.render_compare(part, part, self.after_window.until).split()))

    def test_recorded_hana_project_precedes_reused_unit_and_session_names_and_conflicts_stay_unknown(self) -> None:
        unit = m.Unit("builds", "reused-unit", "build-tools", "/current/build-tools", False, "")
        recorded: log_records.JsonObject = {
            "event_type": "phase_started", "unit": {"state": "Recorded", "value": unit.name},
            "main_agent": {"session_id": unit.name},
            "production": {"state": "Recorded", "value": "tool-based-ui"},
            "repository": {"state": "Recorded", "value": "claude"},
        }
        conflicting = {**recorded, "production": {"state": "Recorded", "value": "builds"},
                       "repository": {"state": "Recorded", "value": "build-tools"}}
        for history, cause_type in (([recorded], m.HanaCause), ([recorded, conflicting], m.CauseNotRecorded)):
            with self.subTest(records=len(history)):
                projects = self.projects_from_history(history, [unit])
                for subject in ("unit:" + unit.name, "session:" + unit.name, unit.name):
                    self.assertIsInstance(m.project_cause(subject, projects), cause_type)
                part = self.part(self.before_window, 0)
                records: list[log_records.JsonObject] = []
                for holder in ({"state": "UnitHolder", "unit": unit.name},
                               {"state": "SessionHolder", "session_id": unit.name}):
                    record = wait_fixture(str(holder["state"]), "WaitingForMemoryOrSlot", part.window.since, 3)
                    record["holders"] = [holder_observation(part.window.since, {
                        "state": "NamedHolder", "resource_kind": "memory", "resource": "natedev", "holder": holder,
                    })]
                    records.append(record)
                part = self.with_waits(part, records, projects)
                evidence = m.wait_evidence(part)
                self.assertEqual(len(evidence), 2)
                for wait in evidence:
                    self.assertEqual(len(wait.causes), 1)
                    self.assertIsInstance(wait.causes[0].cause, cause_type)
                    self.assertEqual(wait.causes[0].minutes, 3)
                output = " ".join(m.render_compare(part, part, self.after_window.until, full=True).split())
                self.assertIn("0 named wait causes", output)
                self.assertNotIn("HI10 proposal evidence recorded", output)

    def test_a_malformed_holder_reason_cannot_create_named_cause_evidence(self) -> None:
        part = self.part(self.before_window, 0)
        record = wait_fixture("misleading-reason", "WaitingForMemoryOrSlot", part.window.since, 10)
        record["holders"] = [holder_observation(part.window.since, {
            "state": "MalformedHolder", "reason": "invalid holder mentions non-Hana cause session:tools",
        })]
        part = self.with_waits(part, [record])
        history = part.sources.wait_history
        if not isinstance(history, m.HanaWaitHistory):
            self.fail("malformed holder removed its wait")
        self.assertTrue(all(isinstance(interval.cause, m.CauseNotRecorded)
                            for interval in m.wait_causes(history.waits[0], (), part.window)))
        output = m.render_compare(part, part, self.after_window.until, full=True)
        summary = " ".join(output.split("\n\n", 1)[0].split())
        self.assertIn("Non-Hana causes: 0 named wait causes", summary)
        self.assertNotIn("HI10 proposal evidence recorded", summary)
        evidence = [row for row in read_report_blocks(output) if row.measure == "Hana wait cause / misleading-reason"]
        self.assertEqual(evidence, [])
        self.assertEqual(self.rows(part, part)["Hana wait / WaitingForMemoryOrSlot"].n, 1)

    def test_holder_minutes_and_wait_counts_use_equal_weekly_rates_for_unequal_windows(self) -> None:
        parts: list[m.ComparisonWindow] = []
        for window, minutes in ((self.before_window, 2), (self.after_window, 8)):
            part = self.part(window, 0)
            records = [wait_fixture("unnamed-" + str(index), "WaitingForQuotaRefusal", window.since, 1)
                       for index in range(minutes)]
            parts.append(self.with_waits(part, records))
        row = self.rows(parts[0], parts[1])["Hana wait holders / WaitingForQuotaRefusal / HolderNotNamed"]
        self.assertIn("before: 2.0 min/week; 7 covered days", row.value)
        self.assertIn("after: 2.0 min/week; 28 covered days", row.value)
        self.assertIn("waits 2.0/week; 7 covered days", row.value)
        self.assertIn("waits 2.0/week; 28 covered days", row.value)
        self.assertIn("change: +0.0/week", row.value)
        self.assertEqual(row.n, 8)

    def test_receipt_coverage_compares_fractions_without_turning_them_into_rates(self) -> None:
        parts: list[m.ComparisonWindow] = []
        for window in (self.before_window, self.after_window):
            part = self.part(window, 0)
            at = window.since + timedelta(hours=1)
            part.sources.merges = [m.HanaMerge(commit, at) for commit in ("b" * 40, "c" * 40)]
            part.sources.receipts = (m.parse_receipt(receipt_fixture("b" * 40, at)),)
            parts.append(part)
        row = self.rows(parts[0], parts[1])["Hana merge receipts / origin/init/catalyst"]
        self.assertIn("before: 1/2", row.value)
        self.assertIn("after: 1/2", row.value)
        self.assertIn("change: unchanged", row.value)
        self.assertNotIn("/week", row.value)
        self.assertEqual(row.n, 2)

    def test_malformed_holder_minutes_keep_waits_without_claiming_a_non_hana_cause(self) -> None:
        part = self.part(self.before_window, 0)
        start = part.window.since
        whole = wait_fixture("all-malformed", "WaitingForMemoryOrSlot", start, 10)
        partial = wait_fixture("part-malformed", "WaitingForMemoryOrSlot", start, 10)
        malformed: log_records.JsonObject = {"state": "MalformedHolder", "reason": "holder invalid",
                                             "raw_value": "private-holder-value"}
        whole["holders"] = [holder_observation(start, malformed)]
        partial["holders"] = [holder_observation(start, named_holder_fixture()),
                              holder_observation(start + timedelta(minutes=4), malformed)]
        part = self.with_waits(part, [whole, partial], (
            m.CauseProject("session:other-project-director", m.NonHanaProject("build-tools")),
        ))
        rows = m.wait_cause_rows(part)
        named = {row.measure: row for row in rows if row.measure.startswith("Hana wait holder minutes /")}
        self.assertEqual([(named["Hana wait holder minutes / " + name].value, named["Hana wait holder minutes / " + name].n)
                          for name in ("all-malformed", "part-malformed")],
                         [("named 0.0 min; not named 10.0 min", 1), ("named 4.0 min; not named 6.0 min", 1)])
        causes = [row for row in rows if row.measure.startswith("Hana wait cause /")]
        proven = [row for row in causes if "non-Hana cause" in row.value]
        self.assertEqual(len(proven), 1)
        self.assertEqual((proven[0].measure, proven[0].n), ("Hana wait cause / part-malformed", 1))
        self.assertIn("project build-tools; 4.0 min", proven[0].value)
        unknown = [row for row in causes if "cause not recorded: MalformedHolder" in row.value]
        self.assertEqual(len(unknown), 2)
        self.assertTrue(any("10.0 min" in row.value for row in unknown))
        self.assertTrue(any("6.0 min" in row.value for row in unknown))
        total = self.rows(part, part)["Hana wait / WaitingForMemoryOrSlot"]
        self.assertEqual(total.n, 2)
        self.assertIn("20.0 min/week", total.value)
        output = m.render_compare(part, part, self.after_window.until, full=True)
        displayed = read_report_blocks(output)
        for identity, expected in (("all-malformed", "named 0.0 min; not named 10.0 min"),
                                   ("part-malformed", "named 4.0 min; not named 6.0 min")):
            with self.subTest(wait=identity):
                breakdowns = [row for row in displayed if row.measure == "Hana wait holder minutes / " + identity]
                self.assertEqual([(row.value, row.n) for row in breakdowns], [(expected, 1)] * 2)
        self.assertNotIn("private-holder-value", output)
        self.assertEqual(phone_overflow_lines(output), [])
        self.assertIn("HI10 proposal evidence recorded.", " ".join(output.split()))

    def test_a_known_quota_kind_and_named_unattributed_supplier_are_not_proven_causes(self) -> None:
        part = self.part(self.before_window, 0)
        quota = wait_fixture("known-quota", "WaitingForQuotaRefusal", part.window.since, 5)
        supplier = wait_fixture("unknown-supplier-project", "WaitingForSupplier", part.window.since, 7)
        supplier["expected_supplier"] = {"state": "NamedSupplier", "address": "worker:old-run/peer"}
        part = self.with_waits(part, [quota, supplier])
        history = part.sources.wait_history
        self.assertIsInstance(history, m.HanaWaitHistory)
        if not isinstance(history, m.HanaWaitHistory):
            self.fail("recorded wait fixture was lost")
        self.assertIs(history.waits[0].kind, m.WaitKind.QUOTA)
        self.assertEqual(history.waits[1].expected_supplier, m.NamedSupplier("worker:old-run/peer"))
        self.assertTrue(all(isinstance(interval.cause, m.CauseNotRecorded)
                            for wait in history.waits for interval in m.wait_causes(wait, part.projects, part.window)))
        causes = [row for row in m.wait_cause_rows(part) if row.measure.startswith("Hana wait cause /")]
        self.assertEqual([row.n for row in causes], [1, 1])
        self.assertIn("5.0 min", causes[0].value)
        self.assertIn("7.0 min", causes[1].value)
        self.assertIn("worker:old-run/peer", causes[1].value)
        self.assertTrue(all("cause not recorded" in row.value for row in causes))
        self.assertNotIn("HI10 proposal evidence recorded", " ".join(m.render_compare(part, part, self.after_window.until).split()))

    def test_named_supplier_evidence_and_holder_resource_kind_control_non_hana_proof(self) -> None:
        part = self.part(self.before_window, 0)
        at = part.window.since
        supplier = wait_fixture("supplier", "WaitingForSupplier", at, 0.5)
        supplier["expected_supplier"] = {"state": "NamedSupplier", "address": "unit:builds"}
        wrong = wait_fixture("wrong-resource", "WaitingForQuotaRefusal", at, 5)
        wrong["holders"] = [holder_observation(at, named_holder_fixture("slot"))]
        part = self.with_waits(part, [supplier, wrong], (
            m.CauseProject("unit:builds", m.NonHanaProject("build-tools")),
            m.CauseProject("session:other-project-director", m.NonHanaProject("build-tools")),
        ))
        proven = [row for row in m.wait_cause_rows(part) if "non-Hana cause" in row.value]
        self.assertEqual([(row.measure, row.n) for row in proven], [("Hana wait cause / supplier", 1)])
        self.assertIn("0.5 min", proven[0].value)
        self.assertIn("HI10 proposal evidence recorded", " ".join(m.render_compare(part, part, self.after_window.until).split()))
        self.assertTrue(any("holder resource does not match wait kind" in row.value
                            for row in m.wait_cause_rows(part)))

    def test_retired_holders_use_recorded_phase_origins_and_unknown_projects_stay_unknown(self) -> None:
        records: list[log_records.JsonObject] = [
            {"event_type": "phase_started", "unit": {"state": "Recorded", "value": "retired-builds"},
             "owner_session": "retired-director", "main_agent": {"session_id": "retired-main"},
             "repository": {"state": "Recorded", "value": "build-tools"},
             "production": {"state": "Recorded", "value": "builds"}},
            {"event_type": "phase_started", "unit": {"state": "Recorded", "value": "retired-hana"},
             "main_agent": {"session_id": "retired-hana-director"},
             "repository": {"state": "Recorded", "value": "/retired/hana"},
             "production": {"state": "Recorded", "value": "tool-based-ui"}},
            {"event_type": "phase_started", "unit": "unknown-project",
             "repository": {"state": "NotRecorded", "reason": "worktree retired"},
             "production": {"state": "NotRecorded", "reason": "production not recorded"}},
        ]
        projects = self.projects_from_history(records)
        part = self.part(self.before_window, 0)
        record = wait_fixture("retired-holder", "WaitingForMemoryOrSlot", part.window.since, 3)
        record["holders"] = [holder_observation(part.window.since, {
            "state": "NamedHolder", "resource_kind": "memory", "resource": "natedev",
            "holder": {"state": "UnitHolder", "unit": "retired-builds"},
        })]
        records = [record]
        for identity, session, minutes in (("retired-main-holder", "retired-main", 2),
                                           ("retired-hana-holder", "retired-hana-director", 4)):
            record = wait_fixture(identity, "WaitingForQuotaRefusal", part.window.since, minutes)
            holder = named_holder_fixture("account", "worker/account-a")
            holder["holder"] = {"state": "SessionHolder", "session_id": session}
            record["holders"] = [holder_observation(part.window.since, holder)]
            records.append(record)
        part = self.with_waits(part, records, projects)
        rows = m.wait_cause_rows(part)
        causes = [row for row in rows if row.measure == "Hana wait cause / retired-holder"]
        self.assertEqual(len(causes), 1)
        self.assertIn("non-Hana cause unit:retired-builds; project build-tools; 3.0 min", causes[0].value)
        main = [row for row in rows if row.measure == "Hana wait cause / retired-main-holder"]
        self.assertEqual(len(main), 1)
        self.assertIn("non-Hana cause session:retired-main; project build-tools; 2.0 min", main[0].value)
        hana = [row for row in rows if row.measure == "Hana wait cause / retired-hana-holder"]
        self.assertEqual(len(hana), 1)
        self.assertIn("Hana cause session:retired-hana-director; 4.0 min", hana[0].value)
        self.assertNotIn("non-Hana cause", hana[0].value)
        self.assertIsInstance(m.project_cause("unit:unknown-project", projects), m.CauseNotRecorded)

    def test_hourly_tokens_keep_account_changes_inside_one_hour_and_unknown_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            other, hana = home / "rust/other-project", home / "rust/hana"
            for repo in (other, hana):
                (repo / ".git").mkdir(parents=True)
            path = home / "usage.sqlite"
            hour = self.before_window.since.replace(minute=0, second=0, microsecond=0)
            write_token_ledger(path, [("worker", str(other)), ("hana", str(hana))], [
                (hour.isoformat(), "workers", "account-a"),
                ((hour + timedelta(minutes=40)).isoformat(), "workers", "account-b"),
            ], [
                ("natedev", "worker", (hour + timedelta(minutes=30)).isoformat(), "worker", 15),
                ("natedev", "worker", (hour + timedelta(minutes=45)).isoformat(), "worker", 20),
                ("natedev", "hana", (hour + timedelta(minutes=46)).isoformat(), "worker", 30),
                ("natedev", "missing", (hour + timedelta(minutes=47)).isoformat(), None, 7),
                ("mac", "worker", (hour + timedelta(minutes=48)).isoformat(), "worker", 900),
                ("natedev", "worker", self.before_window.until.isoformat(), "worker", 1000),
            ])
            original = path.read_bytes()
            with patch.object(Path, "home", return_value=home):
                hourly = m.read_hourly_tokens(path, self.before_window)
                known = {(turn.account.account, turn.repository): (turn.hour, turn.tokens, turn.turns)
                         for turn in hourly if isinstance(turn.account, m.RoleAccount)}
                self.assertEqual(known, {("account-a", str(other)): (hour, 15, 1),
                                        ("account-b", str(other)): (hour, 20, 1),
                                        ("account-b", str(hana)): (hour, 30, 1)})
                unknown = [turn for turn in hourly if isinstance(turn.account, m.AccountNotRecorded)]
                self.assertEqual([(turn.tokens, turn.turns) for turn in unknown], [(7, 1)])
                part = self.part(self.before_window, 0)
                record = wait_fixture("quota-load", "WaitingForQuotaRefusal", hour + timedelta(minutes=30), 20)
                record["holders"] = [holder_observation(hour + timedelta(minutes=30), {
                    "state": "NamedHolder", "resource_kind": "account", "resource": "worker/account-b",
                    "holder": {"state": "SessionHolder", "session_id": "unknown-project-director"},
                })]
                part = replace(self.with_waits(part, [record]), hourly_tokens=hourly)
                rows = m.wait_cause_rows(part)
                contexts = [row for row in rows if row.measure.startswith("Same-hour non-Hana tokens /")]
                self.assertEqual(len(contexts), 1)
                self.assertIn("worker/account-b: 20 tokens", contexts[0].value)
                self.assertNotIn("15 tokens", contexts[0].value)
                self.assertNotIn("30 tokens", contexts[0].value)
                self.assertIn("1 turns with attribution not recorded", contexts[0].value)
                self.assertEqual(contexts[0].n, 1)
                self.assertTrue(all("non-Hana cause" not in row.value for row in rows))
            self.assertEqual(path.read_bytes(), original)

    def test_real_worker_and_showrunner_roles_keep_their_same_account_token_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "rust/build-tools"
            (repo / ".git").mkdir(parents=True)
            path = home / "usage.sqlite"
            at = self.before_window.since
            roles = ("impl", "test", "fix", "review", "showrunner")
            write_token_ledger(path, [(role, str(repo)) for role in roles], [
                (at.isoformat(), "workers", "worker-account"),
                (at.isoformat(), "showrunners", "production-account"),
            ], [("natedev", role, (at + timedelta(minutes=1)).isoformat(), role, 10 + index)
                for index, role in enumerate(roles)])
            original = path.read_bytes()
            with patch.object(Path, "home", return_value=home):
                turns = m.read_hourly_tokens(path, self.before_window)
            known = {turn.account.role: (turn.account.account, turn.tokens, turn.turns)
                     for turn in turns if isinstance(turn.account, m.RoleAccount)}
            self.assertEqual(known, {
                "impl": ("worker-account", 10, 1), "test": ("worker-account", 11, 1),
                "fix": ("worker-account", 12, 1), "review": ("worker-account", 13, 1),
                "showrunner": ("production-account", 14, 1),
            })
            self.assertEqual(len(turns), 5)
            part = self.part(self.before_window, 0)
            records: list[log_records.JsonObject] = []
            for role in roles:
                record = wait_fixture(role, "WaitingForQuotaRefusal", at, 5)
                account = "production-account" if role == "showrunner" else "worker-account"
                record["holders"] = [holder_observation(at, named_holder_fixture("account", role + "/" + account))]
                records.append(record)
            part = replace(self.with_waits(part, records), hourly_tokens=turns)
            contexts = [row for row in m.wait_cause_rows(part) if row.measure.startswith("Same-hour non-Hana tokens /")]
            self.assertEqual(len(contexts), 5)
            for index, role in enumerate(roles):
                row = next(row for row in contexts if row.measure.startswith("Same-hour non-Hana tokens / " + role + " /"))
                account = "production-account" if role == "showrunner" else "worker-account"
                self.assertIn(f"{role}/{account}: {10 + index} tokens", row.value)
                self.assertEqual(row.n, 1)
            self.assertEqual(path.read_bytes(), original)

    def test_a_build_across_two_hours_is_memory_context_without_becoming_cause_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "builds.sqlite"
            hour = self.before_window.since.replace(minute=0, second=0, microsecond=0)
            start = hour + timedelta(minutes=30)
            with closing(sqlite3.connect(path)) as db:
                _ = db.execute("CREATE TABLE steps(repo,started_at,duration_s,peak_mem_bytes,host)")
                _ = db.executemany("INSERT INTO steps VALUES ('build-tools',?,5400,?,?)", [
                    (start.isoformat(), 4096, "natedev"), (start.isoformat(), 9000, "mac"),
                ])
                db.commit()
            original = path.read_bytes()
            memory = m.read_build_memory(path)
            self.assertIsInstance(memory, m.BuildMemoryHistory)
            part = self.part(self.before_window, 0)
            record = wait_fixture("memory-load", "WaitingForMemoryOrSlot", start, 60)
            part = replace(self.with_waits(part, [record]), build_memory=memory)
            rows = m.wait_cause_rows(part)
            contexts = [row for row in rows if row.measure.startswith("Same-hour non-Hana build memory /")]
            self.assertEqual([row.measure for row in contexts], [
                "Same-hour non-Hana build memory / memory-load / " + (hour + timedelta(hours=i)).astimezone(timezone.utc).isoformat()
                for i in (0, 1)
            ])
            self.assertEqual([(row.value, row.n) for row in contexts], [("build-tools: peak 4096 bytes", 1)] * 2)
            self.assertTrue(all("steps.peak_mem_bytes" in row.source and "context only" in row.source for row in contexts))
            self.assertTrue(all("non-Hana cause" not in row.value for row in rows))
            absent = replace(part, build_memory=m.read_build_memory(Path(directory) / "missing.sqlite"))
            missing = [row for row in m.wait_cause_rows(absent) if row.measure.startswith("Same-hour non-Hana build memory /")]
            self.assertEqual([(row.measure, row.n) for row in missing], [(row.measure, 0) for row in contexts])
            self.assertTrue(all(row.value.startswith("not recorded:") and "missing.sqlite" in row.value for row in missing))
            self.assertEqual(path.read_bytes(), original)


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
            answered = state / "showrunner/outstanding/hana.answered.jsonl"
            _ = answered.write_text(json.dumps({"time": "2026-10-07T11:00:00-04:00", "slug": "hana",
                                                "text": "Answered recently", "asked_by": "fixture-unit"}) + "\n")
            answered_bytes = answered.read_bytes()
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
            for days in (7, 28):
                with self.subTest(answer_window_days=days):
                    answer_rows = m.pending_attention(waiting, snapshot, log_records.Window(snapshot - timedelta(days=days), snapshot))[-2:]
                    self.assertEqual([(row.value, row.n) for row in answer_rows], [("not recorded", 0)] * 2)
                    self.assertTrue(all("48-hour" in row.source and "replay" in row.source for row in answer_rows))
                    self.assertTrue(all("no answer history" not in row.source for row in answer_rows))
            self.assertEqual(answered.read_bytes(), answered_bytes)

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
            self.assertEqual(m.read_builds(path, [])[0].seconds, 120)
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
            rows = m.resource_use(m.read_builds(path, []), [], log_records.Window(start, start + timedelta(days=7)))
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
