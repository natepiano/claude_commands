"""Fixture checks for the baseline's counts and source boundaries."""
from __future__ import annotations

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

from scripts.hierarchies import measure as m
from scripts.production.issues import Issue


class MeasuresTest(unittest.TestCase):
    start: datetime = datetime(2026, 10, 13, tzinfo=timezone.utc)
    window: m.Window = m.Window(start, start + timedelta(days=7))
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
        other_window = m.Window(self.window.until, self.window.until + timedelta(days=1))
        tokens = [m.WindowTokenUsage(self.window, "hana", 1234, 3), m.WindowTokenUsage(other_window, "hana", 9000, 1)]
        rows = m.resource_use(builds, tokens, self.window)
        self.assertEqual([(r.value, r.n) for r in rows], [("1.0", 1), ("2.0", 1), ("1,234", 3)])

    def test_phase_flow_filters_hana_status_window_and_deduplicates_finishes(self) -> None:
        base: m.JsonObject = {
            "working_dir": "/deleted/tool-based-ui-tools", "event_type": "phase_finished",
            "status": "completed", "timestamp": self.at,
        }
        records: list[m.JsonObject] = [{**base, "event_id": str(i), "phase_elapsed_seconds": i * 60} for i in range(1, 11)]
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
        self.assertTrue(all(row.value == "not recorded" and "progress_history.py" in row.source for row in rows[2:]))

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
        at = m.required_time("2026-10-14T01:00:00Z")
        pushes = [
            m.PhonePush(at, "unit", "one", "sent"),
            m.PhonePush(at + timedelta(hours=1), "unit", "two", "sent"),
            m.PhonePush(self.start, "unit", "quiet", "silenced"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.jsonl"
            records = [
                {"time": "2026-10-14T01:00:01Z", "source": "unit", "verdict": "failed", "sent": {"title": "one"}},
                {"time": "2026-10-14T02:00:01Z", "source": "unit", "verdict": "failed",
                 "sent": {"title": "two"}, "replay": True},
            ]
            _ = path.write_text("\n".join(json.dumps(record) for record in records))
            reviews = m.read_failed_reviews(path)
        messages = [m.parse_message({"time": at.isoformat(), "from": sender, "to": target})
                    for sender, target in [("unit-a", "unit-b"), ("stall-watch", "unit-a"),
                                           ("natedev", "unit-a"), ("unit-a", "user")]]
        rows = m.attention(pushes, reviews, messages, self.window)
        selected = {row.measure: row.value for row in rows if "2026-10-13" in row.measure}
        self.assertEqual(selected["phone pushes / 2026-10-13"], "2; after failed review 1")
        self.assertEqual(selected["messages / 2026-10-13 / agent"], "2")
        self.assertEqual(selected["messages / 2026-10-13 / automation"], "1")
        self.assertEqual(selected["messages / 2026-10-13 / user-bound"], "1")

    def test_pending_reports_unknown_ages_and_unrecorded_answer_history(self) -> None:
        items = [m.WaitingItem("changes", self.start), m.WaitingItem("changes", m.TimeNotRecorded.MISSING)]
        rows = m.pending_attention(items, self.window.until, self.window)
        self.assertEqual(rows[0].value, "2; oldest 7.0 days (1 ages not recorded)")
        self.assertEqual([(row.value, row.n) for row in rows[-2:]], [("not recorded", 0), ("not recorded", 0)])
        self.assertTrue(all("showrunner/outstanding" in row.source for row in rows[-2:]))

    def test_missing_values_keep_separate_domain_types_and_output(self) -> None:
        missing_time = m.moment(None)
        rank = m.parse_rank({"name": "one", "rank": 1})
        phase = m.parse_phase({
            "event_type": "phase_finished", "status": "completed", "timestamp": self.at,
            "working_dir": "/deleted/tool-based-ui-tools",
        }, ())[0]
        sources = m.BaselineSources([], [], [], [], [phase], [], [], [], [], [rank], [], [])
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
        records: list[m.JsonObject] = [
            {"time": self.at, "from": "cron", "to": "unit", "kind": "agent"},
            {"time": self.at, "from": "natedev", "to": "unit", "kind": "automation"},
            {"time": self.at, "from": "timer", "to": "unit", "route": {"kind": "session"}},
            {"time": self.at, "from": "unit", "to": "unit", "route": "automation"},
            {"time": self.at, "from": "unit", "to": "user", "kind": "agent"},
        ]
        messages = [m.parse_message(record) for record in records]
        rows = m.attention([], [], messages, self.window)
        selected = {row.measure.rsplit(" / ", 1)[-1]: row.n for row in rows if "2026-10-12" in row.measure}
        self.assertEqual(selected, {"2026-10-12": 0, "agent": 2, "automation": 2, "user-bound": 1})

    def test_failed_review_match_requires_source_title_and_time(self) -> None:
        push = m.PhonePush(self.start, "hana", "one", "sent")
        mismatches = [
            m.FailedPushReview(self.start, "other", "one"),
            m.FailedPushReview(self.start, "hana", "other"),
            m.FailedPushReview(self.start + timedelta(seconds=6), "hana", "one"),
        ]
        self.assertFalse(m.sent_after_failed_review(push, mismatches))
        self.assertTrue(m.sent_after_failed_review(push, [m.FailedPushReview(self.start, "hana", "one")]))

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
        window = m.window_bounds("2026-10-25", "2026-10-25", self.window.until)
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
        now = m.required_time("2026-10-21T00:00Z")
        dates = m.window_bounds("2026-10-13", "2026-10-20", now)
        self.assertEqual(dates.since, m.required_time("2026-10-13T04:00Z"))
        self.assertEqual(dates.until, m.required_time("2026-10-21T04:00Z"))
        times = m.window_bounds("2026-10-13T17:24", "2026-10-20T17:24", now)
        self.assertEqual(times.since, m.required_time("2026-10-13T21:24Z"))
        self.assertEqual(times.until, m.required_time("2026-10-20T21:24Z"))
        self.assertEqual(times, m.window_bounds("2026-10-13T17:24-04:00", "2026-10-20T21:24Z", now))
        self.assertEqual(m.window_bounds("", "", now), m.Window(now - timedelta(days=7), now))
        self.assertRaises(ValueError, m.window_bounds, "2026-10-20T17:24", "2026-10-20T17:24", now)
        self.assertRaises(ValueError, m.window_bounds, "bad", "", now)
        with patch.object(m, "load_sources") as load, patch.object(m, "render", return_value="fixture"), patch("builtins.print"):
            self.assertEqual(m.main(["baseline", "--since", "2026-10-13T17:24", "--until", "2026-10-20T17:24"]), 0)
            load.assert_called_once_with(times)
        with patch.object(m, "load_sources") as load, patch.object(m, "render", return_value="fixture"), patch("builtins.print"):
            self.assertEqual(m.main(["baseline", "--since", "2026-10-13", "--until", "2026-10-20"]), 0)
            load.assert_called_once_with(dates)


class BaselineOutputTest(unittest.TestCase):
    window: m.Window = m.window_bounds("2026-10-21", "2026-10-27", datetime.now(timezone.utc))

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
        pushes = [m.PhonePush(start, "unit", "one", "sent"),
                  m.PhonePush(start + timedelta(days=1), "unit", "two", "sent"),
                  m.PhonePush(start, "unit", "quiet", "silenced"),
                  m.PhonePush(end, "unit", "later", "sent")]
        reviews = [m.FailedPushReview(start + timedelta(seconds=1), "unit", "one")]
        merges = [m.HanaMerge("one", start), m.HanaMerge("two", start + timedelta(days=1)),
                  m.HanaMerge("later", end)]
        return m.BaselineSources([], [], [], [], [], pushes, reviews, [], found, [], [], [], merges)

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
        sources.problems = [m.SourceProblem("reviews", "missing review file"),
                            m.SourceProblem("builds", "cannot read build file")]
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
            window = m.window_bounds("2026-10-13T17:24", "2026-10-20T17:24", m.required_time("2026-10-21"))
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
            def save(name: str, tree: str, at: int, events: list[m.JsonObject]) -> None:
                records: list[m.JsonObject] = [{"event_type": "run_started", "working_dir": tree, "run_started_at": at}, *events]
                _ = (root / name).write_text("\n".join(json.dumps(record) for record in records))

            phase_a: m.JsonObject = {"phase_instance_id": "A", "phase_id": "1", "phase_title": "First",
                                     "phase_started_at": 1}
            phase_b: m.JsonObject = {"phase_instance_id": "B", "phase_id": "2", "phase_title": "Second",
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
            problems: list[m.SourceProblem] = []
            self.assertEqual(m.running_phase_worktrees(root, problems), {"/live", "/repeated"})
            self.assertEqual(problems, [])

    def test_running_phase_uses_phase_id_fallback_and_ignores_missing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records: dict[str, list[m.JsonObject]] = {
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
                run: m.JsonObject = {"event_type": "run_started", "working_dir": f"/{name}", "run_started_at": 1}
                _ = (root / f"{name}.jsonl").write_text("\n".join(json.dumps(record) for record in [run, *events]))
            problems: list[m.SourceProblem] = []
            self.assertEqual(m.running_phase_worktrees(root, problems), {"/fallback", "/missing-finish"})
            self.assertCountEqual([problem.source for problem in problems],
                                  [str(root / "missing-start.jsonl")] * 2 + [str(root / "missing-finish.jsonl")])
            self.assertTrue(all("phase" in problem.reason and "not recorded" in problem.reason for problem in problems))

    def test_json_source_missing_and_bad_rows_report_source_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            problems: list[m.SourceProblem] = []
            self.assertEqual(m.read_source(str(path), lambda: m.json_lines(path, m.parse_message), [], problems), [])
            self.assertIn("missing", problems[0].reason)
            _ = path.write_text('{"time":"2026-10-13T00:00:00Z"}\nnot json\n')
            self.assertEqual(m.read_source(str(path), lambda: m.json_lines(path, m.parse_message), [], problems), [])
            self.assertIn("1 invalid JSON or field rows", problems[-1].reason)
            now = datetime(2026, 10, 20, tzinfo=timezone.utc)
            rows = m.source_problem_rows(problems, m.Window(now - timedelta(days=7), now))
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
            problems: list[m.SourceProblem] = []
            waiting = m.read_waiting(state, vault, found, problems)
            self.assertEqual(problems, [])
            self.assertEqual([item.kind for item in waiting], ["changes", "proposals", "proposals", "efficiency items", "questions"])
            self.assertEqual(waiting[0].opened, m.moment("2026-10-02"))
            self.assertEqual(waiting[-1].opened, m.moment("2026-10-05T12:00"))
            snapshot = m.required_time("2026-10-07T12:00-04:00")
            window = m.Window(snapshot - timedelta(days=7), snapshot)
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
            problems: list[m.SourceProblem] = []
            waiting = m.read_waiting(root / "state", root / "vault", [], problems)
            self.assertEqual(waiting, [])
            issue = next(problem for problem in problems if problem.source == str(path))
            self.assertIn("no `## To review` section", issue.reason)
            now = m.required_time("2026-10-20")
            rows = m.source_problem_rows([issue], m.Window(now - timedelta(days=7), now))
            self.assertEqual(rows[0].source, str(path))
            self.assertIn("unreadable", rows[0].value)

    def test_phase_loader_reports_each_corrupt_file_and_keeps_valid_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid: m.JsonObject = {
                "event_type": "phase_finished", "status": "completed", "event_id": "one",
                "timestamp": "2026-10-14T00:00:00Z", "working_dir": "/deleted/tool-based-ui-demo",
                "phase_elapsed_seconds": 60,
            }
            first, second = root / "one.jsonl", root / "two.jsonl"
            _ = first.write_text(json.dumps(valid) + "\n{bad json\n[]\n")
            _ = second.write_text("broken\n")
            problems: list[m.SourceProblem] = []
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
            base: m.JsonObject = {
                "event_type": "phase_finished", "status": "completed", "timestamp": "2026-10-14T00:00:00Z",
                "phase_elapsed_seconds": 60,
            }
            origins: list[m.JsonObject] = [
                {"working_dir": str(deleted_tree)},
                {"working_dir": str(root / "unknown"), "plan_doc": "docs/plans/tool-based-ui-demo.md"},
                {"working_dir": str(root / "unknown"), "production": "hana"},
                {"working_dir": str(root / "unknown"), "repository": "/archive/hana_catalyst"},
                {"working_dir": str(root / "widget-enhancements-retired")},
                {"working_dir": str(root / "other")},
            ]
            records = [{**base, **origin, "event_id": str(index)} for index, origin in enumerate(origins)]
            _ = (root / "events.jsonl").write_text("\n".join(json.dumps(record) for record in records))
            problems: list[m.SourceProblem] = []
            phases = m.read_phases(root, problems, ["widget-enhancements"])
            start = m.required_time("2026-10-13T00:00:00Z")
            rows = m.phase_flow(phases, m.Window(start, start + timedelta(days=7)))
            self.assertEqual(rows[0].n, 5)
            self.assertEqual(rows[1].value, "median 1.0 min; p90 1.0 min")
            self.assertEqual(problems, [])

    def test_invalid_source_fields_report_problem_instead_of_zero_or_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rankings.jsonl"
            _ = path.write_text('{"name":"one","rank":"first"}\n')
            problems: list[m.SourceProblem] = []
            ranks = m.read_source(str(path), lambda: m.json_lines(path, m.parse_rank), [], problems)
            self.assertEqual(ranks, [])
            self.assertIn("1 invalid JSON or field rows", problems[0].reason)
            self.assertRaisesRegex(ValueError, "from: expected text", m.parse_message,
                                   {"from": 123, "to": "unit", "time": "2026-10-14"})
            self.assertRaisesRegex(ValueError, "timestamp", m.parse_push, {"time": "bad stamp", "outcome": "sent"})
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
            start = m.required_time("2026-10-13T00:00:00Z")
            window = m.Window(start, start + timedelta(days=7))
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
            start = m.required_time("2026-10-13")
            rows = m.resource_use(m.read_builds(path), [], m.Window(start, start + timedelta(days=7)))
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
