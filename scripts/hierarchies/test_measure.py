"""Fixture checks for the baseline's counts and source boundaries."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
import json

import measure as m
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
        items = [m.WaitingItem("changes", self.start), m.WaitingItem("changes", m.Unrecorded.TIME)]
        rows = m.pending_attention(items, self.window.until, self.window)
        self.assertEqual(rows[0].value, "2; oldest 7.0 days (1 ages not recorded)")
        self.assertEqual([(row.value, row.n) for row in rows[-2:]], [("not recorded", 0), ("not recorded", 0)])
        self.assertTrue(all("showrunner/outstanding" in row.source for row in rows[-2:]))

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
        rankings = [m.IssueRank(str(i), 25 - i) for i in range(25)] + [m.IssueRank("wrong", 1)]
        units = [m.Unit("tool-based-ui", "unit", "hana", "tree", True, "", "hana"), m.Unit("hana", "unlinked", "hana", "tree2", True, "")]
        rows = m.rank_coverage(found, rankings, units, "snapshot")
        self.assertEqual(rows[0].value, "10/20")
        self.assertEqual(rows[1].value, "1/2")


class SourcesTest(unittest.TestCase):
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
