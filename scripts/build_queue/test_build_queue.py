"""Queue output uses one shared reading and explains each waiting step."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import unittest.mock
from unittest import mock
from typing import Protocol, cast, override
from zoneinfo import ZoneInfo

from scripts.build_queue import build_queue, head_facts
from scripts.build_queue.test_head_facts import bump_line, head_reading, queue_read_fixture, queue_time_line, token_gap_line, write_bumps, write_token_gaps
from scripts.buildlog import store
from scripts.lint import memory_admit
from scripts.message import roster
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

GIB = memory_admit.GIB
NOW = 1793214000.0
NO_SHOWRUNNER = roster.NoShowrunner()
NO_DIRECTOR = roster.NoDirector()


class RosterReadMock(Protocol):
    return_value: roster.Roster
    side_effect: Exception | Callable[[object, object], roster.Roster]

    def assert_called_once_with(self, environment: Mapping[str, str], scope: roster.ThisMachine) -> None: ...


class QueueTests(unittest.TestCase):
    root: Path = Path()
    roster_read: RosterReadMock = cast(RosterReadMock, mock.Mock())

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_PROC": str(self.root / "proc"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "builds.slice"),
            "BUILDLOG_MEMINFO": str(self.root / "meminfo"),
            "BUILDLOG_MEMORY_PRESSURE": str(self.root / "pressure"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "USER_ZONE": "America/New_York",
        })))
        self.roster_read = cast(RosterReadMock, self.enterContext(mock.patch(
            "scripts.build_queue.build_queue.roster.roster", return_value=roster.Roster([], []))))
        (self.root / "proc").mkdir()
        _ = (self.root / "meminfo").write_text(
            f"MemAvailable: {int(23.1 * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def entry(self, session: str, name: str, *, codex: bool = False,
              showrunner: roster.ShowrunnerRelationship = NO_SHOWRUNNER,
              director: roster.DirectorRelationship = NO_DIRECTOR) -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="test-machine", kind="codex" if codex else "claude", role="freestanding",
            name=name, status="running", reach=roster.CannotReceiveMessages("test"), cwd="/tmp/tree",
            showrunner=showrunner, director=director, assignment=roster.NotAWorker(),
            thread=roster.CodexThread(session) if codex else roster.NoCodexThread(),
            session=roster.NoClaudeSession() if codex else roster.ClaudeSession(session),
        )

    def cli_output(self, view: memory_admit.QueueView) -> str:
        output = io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", return_value=queue_read_fixture(view)), \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        return output.getvalue()

    def json_output(self, view: memory_admit.QueueView) -> build_queue.QueueSnapshot:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", return_value=queue_read_fixture(view)) as read, \
             mock.patch("sys.argv", ["build_queue.py", "--json"]), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        self.assertEqual(errors.getvalue(), "")
        return cast(build_queue.QueueSnapshot, json.loads(output.getvalue()))

    def test_head_and_why_share_the_same_read_and_never_recalculate_checks(self) -> None:
        from scripts.build_queue import build_why
        from scripts.build_queue.test_build_why import BuildWhyTests
        fixture = BuildWhyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        reading = memory_admit.locked_queue_read()
        labels = roster.unit_labels_for([row.step.session_id for row in reading.explanation.running]
                                       + [reading.view.waiting[0].session_id], os.environ)
        facts = head_facts.read_head_facts(reading)
        why = "\n".join(build_why.render_explanation(reading.explanation, ZoneInfo("America/New_York"), labels, facts))
        # Disagreeing legacy fields cannot override the explanation or trigger another decision.
        legacy_head = replace(reading.view.waiting[0], reason=memory_admit.ReadyForAdmission(), need=GIB)
        reading = replace(reading, view=replace(reading.view, available=0, waiting=(legacy_head,)))
        with mock.patch.object(memory_admit, "decide", side_effect=AssertionError("no second decision")), \
             mock.patch.object(memory_admit, "build_charge", side_effect=AssertionError("use recorded charges")):
            head = build_queue.head_line(reading.explanation, labels, ZoneInfo("America/New_York"), facts)
        for value in ("18.1", "11.8", "16.3", "9.2"):
            self.assertIn(value, head["text"])
            self.assertIn(value, why)
        self.assertIn("budget", head["text"])
        self.assertIn("needs 20.3 GiB free", head["text"])
        self.assertIn("Waiting because: build budget, free memory", why)
        output = io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", return_value=reading) as read, \
             mock.patch.object(memory_admit, "queue_view", side_effect=AssertionError("no separate queue read")), \
             mock.patch("sys.argv", ["build_queue.py", "--json"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        snapshot = cast(build_queue.QueueSnapshot, json.loads(output.getvalue()))
        self.assertEqual(snapshot["head"], head)

    def test_every_text_table_marks_numbers_right_and_text_left(self) -> None:
        head = memory_admit.WaitingStep(10, "head", "worker", "test", "/tmp/tree", NOW - 60,
                                       GIB, memory_admit.ReadyForAdmission())
        step = memory_admit.RunningStep("running", "runner", "test", "/tmp/run", 2 * GIB,
                                       memory_admit.MeasuredMemory(GIB))
        reading = queue_read_fixture(memory_admit.QueueView((head,), (step,), 30 * GIB, 0, 3 * GIB, NOW))
        lines = build_queue.render_queue(reading, ZoneInfo("UTC"))
        tables = 0
        for index, line in enumerate(lines):
            if not line.startswith("| # |"):
                continue
            tables += 1
            headings = [cell.strip() for cell in line.split("|")[1:-1]]
            markers = [cell.strip() for cell in lines[index + 1].split("|")[1:-1]]
            numeric = {"#", "Promised", "Using", "Delta", "Waiting since", "Needs", "Free"}
            self.assertEqual(markers, ["---:" if heading in numeric else "---" for heading in headings])
        self.assertEqual(tables, 2)

    def test_head_line_short_forms_share_the_recorded_head_and_bump_facts(self) -> None:
        reading = head_reading(since=NOW - 158 * 60, now=NOW,
                               fields={"head_since": NOW - 125, "priority_since": NOW - 3600})
        write_bumps(NOW, [bump_line(NOW - 5, folder="/tmp/alpha"),
                          bump_line(NOW - 4, folder="/tmp/beta", step="clippy"),
                          bump_line(NOW - 3, folder="/tmp/alpha")])
        snapshot = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))
        self.assertIn("head for 2 min", snapshot["head"]["text"])
        self.assertIn("3 bumps since it joined, most: alpha check (2)", snapshot["head"]["text"])
        self.assertIn("no head priority: younger steps that fit may start", snapshot["head"]["text"])
        self.assertNotIn("head for 158 min", snapshot["head"]["text"])

    def test_missing_head_time_and_empty_bump_history_have_distinct_short_forms(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW, fields={"priority_since": NOW - 60})
        snapshot = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))
        self.assertIn("head for: unknown", snapshot["head"]["text"])
        self.assertIn("no bumps since it joined", snapshot["head"]["text"])
        self.assertNotIn("head for 10 min", snapshot["head"]["text"])

    def test_no_strict_window_without_admissions_head_priority(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        head = reading.explanation.head
        if not isinstance(head, memory_admit.HeadExplanation):
            self.fail("fixture needs a head")
        reading = replace(reading, explanation=replace(reading.explanation,
            head=replace(head, order=memory_admit.StrictUntil(NOW + 123), has_priority=False)))
        text = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))["head"]["text"]
        self.assertIn("no head priority: younger steps that fit may start", text)
        self.assertNotIn("strict", text)
        self.assertNotIn("until", text)
        self.assertNotIn("younger steps wait", text)

    def test_bump_read_failure_is_unknown_and_snapshot_keys_stay_unchanged(self) -> None:
        write_bumps(NOW, [])
        path = store.host_file(store.host_name(), NOW)
        path.unlink()
        path.mkdir()
        snapshot = build_queue.queue_snapshot(head_reading(since=NOW - 600, now=NOW), ZoneInfo("UTC"))
        self.assertIn("bumps: unknown", snapshot["head"]["text"])
        self.assertNotIn("no bumps since it joined", snapshot["head"]["text"])
        self.assertEqual(set(snapshot), {"running_header", "running", "waiting_header", "waiting",
                                         "head", "totals", "problems"})
        self.assertEqual(set(snapshot["head"]), {"text", "tone"})
        self.assertEqual(set(snapshot["totals"]), {"promised", "measured", "free", "reserve"})

    def test_missing_token_return_never_adds_a_current_token_wait_row(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW, fields={"token_left_at": NOW - 60})
        follower = replace(reading.view.waiting[0], pid=124, session_id="follower", session_name="follower")
        reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, follower)))
        write_token_gaps([queue_time_line(NOW - 60, kind="token_gap", session_id="head-session",
                                         step="nextest", pid=123, start_time="123", token_left_at=NOW - 60)])
        snapshot = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))
        self.assertEqual([row[0] for row in snapshot["waiting"]], ["1", "2"])
        self.assertFalse(any("waiting for the cargo token" in row[-1] for row in snapshot["waiting"]))

    def test_a_returned_token_record_does_not_add_a_current_token_wait_row(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)])
        snapshot = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))
        self.assertEqual([row[0] for row in snapshot["waiting"]], ["1"])

    def test_head_line_shows_a_recorded_cargo_token_wait(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)])
        snapshot = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))
        self.assertTrue(snapshot["head"]["text"].endswith("; cargo token waited 154 s"))
        self.assertEqual(set(snapshot["head"]), {"text", "tone"})
        self.assertEqual(set(snapshot), {"running_header", "running", "waiting_header", "waiting",
                                         "head", "totals", "problems"})

    def test_head_line_cargo_token_sum_excludes_other_waiting_steps(self) -> None:
        reading = head_reading(since=NOW - 1000, now=NOW)
        follower = replace(reading.view.waiting[0], pid=456, session_id="follower", step="clippy",
                           since=NOW - 800)
        reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, follower)))
        write_token_gaps([
            token_gap_line(NOW - 900, NOW - 800),
            token_gap_line(NOW - 700, NOW - 400, session="follower", step="clippy", pid=456),
            token_gap_line(NOW - 200, NOW - 46),
        ])
        text = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))["head"]["text"]
        self.assertTrue(text.endswith("; cargo token waited 254 s"))
        self.assertNotIn("cargo token waited 554 s", text)

    def test_head_line_distinguishes_missing_history_from_no_head_token_wait(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        missing = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))["head"]["text"]
        self.assertTrue(missing.endswith("; cargo token: unknown"))
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="unrelated", pid=999)])
        recorded = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))["head"]["text"]
        self.assertTrue(recorded.endswith("; no cargo token wait"))

    def test_head_line_partial_token_history_does_not_claim_a_complete_sum(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)],
                         tail=token_gap_line(NOW - 40, NOW - 20).rstrip(b"\n"))
        text = build_queue.queue_snapshot(reading, ZoneInfo("UTC"))["head"]["text"]
        self.assertTrue(text.endswith("; cargo token: unknown"))
        self.assertNotIn("cargo token waited", text)

    def test_json_cli_matches_text_cells_and_preserves_all_roster_problems(self) -> None:
        self.roster_read.return_value = roster.Roster([
            self.entry("worker-id", "shown-worker", codex=True,
                       showrunner=roster.UnderShowrunner("builds", "director", "session:director"),
                       director=roster.UnderDirector("report-unit", "report", "session:report")),
        ], ["first roster problem", "second roster problem"])
        running = (
            memory_admit.RunningStep("worker-id", "old-name", "lint", "/tmp/tree", 8 * GIB,
                                    memory_admit.MeasuredMemory(GIB)),
            memory_admit.RunningStep("starting", "starting", "doc", "/tmp/docs", 2 * GIB,
                                    memory_admit.PromisedMemory(2 * GIB)),
            memory_admit.RunningStep("ended", "ended", "check", "/tmp/gone", GIB,
                                    memory_admit.GoneMemory()),
        )
        waiting = memory_admit.WaitingStep(10, "worker-id", "old-name", "nextest", "/tmp/tree",
                                          NOW - 300, 12 * GIB, memory_admit.ReadyForAdmission())
        view = memory_admit.QueueView((waiting,), running, 20 * GIB, 2 * GIB, 3 * GIB, NOW)
        snapshot = self.json_output(view)
        text = self.cli_output(view)
        cells = [[cell.strip() for cell in line.split("|")[1:-1]]
                 for line in text.splitlines() if line.startswith("| ") and "| --- |" not in line]
        self.assertEqual(cells, [snapshot["running_header"], *snapshot["running"],
                                 snapshot["waiting_header"], *snapshot["waiting"]])
        self.assertEqual(snapshot["running"][0], [
            "1", "builds", "report", "shown-worker", "lint", "tree", "8.0 GiB", "1.0 GiB", "7.0 GiB", "measured"])
        self.assertEqual(snapshot["running"][1][-4:], ["2.0 GiB", "—", "—", "starting"])
        self.assertEqual(snapshot["waiting"][0][6], "14:55 (5 min)")
        self.assertEqual(snapshot["totals"], {
            "promised": "10.0 GiB", "measured": "1.0 GiB", "free": "20.0 GiB", "reserve": "3.0 GiB"})
        self.assertIn(snapshot["head"]["text"], text.splitlines())
        self.assertLess(text.index(snapshot["head"]["text"]), text.index("**Waiting**"))
        self.assertEqual(snapshot["problems"], ["first roster problem", "second roster problem"])

    def test_json_empty_tables_are_arrays_and_do_not_invent_a_head(self) -> None:
        snapshot = self.json_output(memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW))
        self.assertEqual(snapshot["running"], [])
        self.assertEqual(snapshot["waiting"], [])
        self.assertEqual(snapshot["head"], {"text": "", "tone": "default"})
        self.assertEqual(snapshot["problems"], [])
        self.assertEqual(snapshot["totals"]["promised"], "0.0 GiB")

    def test_budget_head_names_every_charged_step_with_its_actual_measured_use(self) -> None:
        running = (
            memory_admit.RunningStep("holder", "stale-name", "nextest", "/tmp/title", int(10.8 * GIB),
                                    memory_admit.MeasuredMemory(int(0.5 * GIB), observed_peak=int(10.8 * GIB))),
            memory_admit.RunningStep("starting", "starting-worker", "doc", "/tmp/docs", 4 * GIB,
                                    memory_admit.PromisedMemory(4 * GIB)),
            memory_admit.RunningStep("gone", "ended-worker", "check", "/tmp/gone", GIB,
                                    memory_admit.GoneMemory()),
        )
        need, available = 12 * GIB, 30 * GIB
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "20"}):
            decision = memory_admit.decide(available, 60 * GIB, need, [step.measured for step in running])
        row: memory_admit.Waiter = {"pid": 10, "start_time": "10", "need": need, "since": NOW - 300}
        head = memory_admit.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], need,
                                       memory_admit.head_wait_reason(decision, need, available, row, NOW))
        entries = [self.entry("holder", "widget-enhancements-title", codex=True)]
        view = memory_admit.QueueView((head,), running, available, 4 * GIB, 3 * GIB, NOW)
        snapshot = build_queue.queue_snapshot(queue_read_fixture(view), ZoneInfo("UTC"), entries)
        line = snapshot["head"]["text"]
        self.assertIn("widget-enhancements-title nextest: charged 10.8 GiB, using 0.5 GiB", line)
        self.assertIn("starting-worker doc: charged 4.0 GiB, using — (starting)", line)
        self.assertIn("builds use 14.8 of the 20.0 GiB budget", line)
        self.assertNotIn("stale-name", line)
        self.assertNotIn("ended-worker", line)
        self.assertEqual(snapshot["head"]["tone"], "default")

    def test_free_memory_head_uses_the_existing_decision_threshold_and_available_reading(self) -> None:
        need, available = 8 * GIB, 7 * GIB
        decision = memory_admit.decide(available, 60 * GIB, need, [])
        row: memory_admit.Waiter = {"pid": 10, "start_time": "10", "need": need, "since": NOW - 300}
        head = memory_admit.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], need,
                                       memory_admit.head_wait_reason(decision, need, available, row, NOW))
        view = memory_admit.QueueView((head,), (), available, 0, 3 * GIB, NOW)
        reading = queue_read_fixture(view)
        with mock.patch.object(memory_admit, "decide", side_effect=AssertionError("Do not decide again")):
            line = build_queue.head_line(reading.explanation, {}, ZoneInfo("UTC"), head_facts.read_head_facts(reading))
        self.assertIn("needs 11.0 GiB free: this step 8.0, earlyoom 3.0", line["text"])
        self.assertIn("free 7.0 GiB, step needs 8.0 GiB", line["text"])
        self.assertEqual(line["tone"], "default")

    def test_pressure_head_uses_the_pressure_decision_instead_of_a_budget_explanation(self) -> None:
        need, available = 8 * GIB, 30 * GIB
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "20"}):
            decision = memory_admit.decide(available, 60 * GIB, need,
                                           [memory_admit.MeasuredMemory(19 * GIB)],
                                           pressure=memory_admit.MemoryPressure(23.4))
        row: memory_admit.Waiter = {"pid": 10, "start_time": "10", "need": need, "since": NOW - 300}
        head = memory_admit.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], need,
                                       memory_admit.head_wait_reason(decision, need, available, row, NOW))
        running = memory_admit.RunningStep("other", "other", "doc", "/tmp/other", 19 * GIB,
                                          memory_admit.MeasuredMemory(19 * GIB))
        reading = queue_read_fixture(memory_admit.QueueView((head,), (running,), available, 0, 3 * GIB, NOW))
        line = build_queue.head_line(reading.explanation, {}, ZoneInfo("UTC"), head_facts.read_head_facts(reading))
        self.assertIn("memory pressure: full avg10 23.4", line["text"])
        self.assertNotIn("charged", line["text"])
        self.assertNotIn("budget", line["text"])

    def test_stalled_head_starts_at_fifteen_minutes_only_when_free_memory_covers_need(self) -> None:
        for elapsed, available, stalled in ((899, 8 * GIB, False), (900, 8 * GIB, True),
                                             (901, 8 * GIB - 1, False), (1200, 30 * GIB, True)):
            with self.subTest(elapsed=elapsed, available=available):
                head = memory_admit.WaitingStep(10, "head", "worker", "nextest", "/tmp/head", NOW - elapsed,
                                               8 * GIB, memory_admit.NeedsBuildBudget(8 * GIB, 19 * GIB, 20 * GIB))
                line = build_queue.queue_snapshot(
                    queue_read_fixture(memory_admit.QueueView((head,), (), available, 0, 3 * GIB, NOW)), ZoneInfo("UTC"))["head"]
                self.assertEqual(line["text"].startswith("Stalled:"), stalled)
                self.assertEqual(line["tone"], "error" if stalled else "default")

    def test_unavailable_head_never_claims_a_stall_even_after_fifteen_minutes(self) -> None:
        head = memory_admit.WaitingStep(10, "head", "worker", "check", "/tmp/head", NOW - 1800,
                                       0, memory_admit.ReadingUnavailable())
        reading = queue_read_fixture(memory_admit.QueueView((head,), (), 0, 0, 0, NOW))
        line = build_queue.head_line(reading.explanation, {}, ZoneInfo("UTC"), head_facts.read_head_facts(reading))
        self.assertIn("memory reading unavailable", line["text"])
        self.assertNotIn("Stalled:", line["text"])
        self.assertNotIn("free 0.0", line["text"])
        self.assertEqual(line["tone"], "default")

    def write_waiter(self, pid: int, session: str, name: str, step: str = "nextest") -> Path:
        process = self.root / "proc" / str(pid)
        process.mkdir()
        fields = ["S", "1", *(["0"] * 17), str(pid)]
        _ = (process / "stat").write_text(f"{pid} (verify) {' '.join(fields)}\n")
        directory = self.root / "buildlog/admission"
        directory.mkdir(parents=True, exist_ok=True)
        row: memory_admit.Waiter = {
            "pid": pid, "start_time": str(pid), "need": 12 * GIB, "since": NOW + pid,
            "session_id": session, "session_name": name, "step": step, "folder": f"/tmp/tree-{pid}",
        }
        path = directory / f"{pid}-{pid}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def test_both_tables_resolve_workers_directors_showrunners_and_unmatched_sessions(self) -> None:
        self.roster_read.return_value = roster.Roster([
            self.entry("worker-id", "organon-ci88", codex=True,
                       showrunner=roster.UnderShowrunner("hana", "hana-showrunner", "session:hana"),
                       director=roster.UnderDirector("organon-unit", "organon", "session:organon")),
            self.entry("director-id", "merge-gate",
                       showrunner=roster.UnderShowrunner("hana", "hana-showrunner", "session:hana"),
                       director=roster.DirectsUnit("merge-unit")),
            self.entry("showrunner-id", "hana-showrunner", showrunner=roster.RunsProduction("hana")),
            self.entry("standalone-id", "standalone"),
            self.entry("orphan-id", "orphan",
                       showrunner=roster.ProductionWithoutRunningShowrunner("offline")),
        ], [])
        identities = (("worker-id", "clone88ci"), ("director-id", "old-director"),
                      ("showrunner-id", "old-showrunner"), ("standalone-id", "old-standalone"),
                      ("orphan-id", "old-orphan"), ("missing-id", "recorded"), ("nameless-id", ""))
        waiting = tuple(memory_admit.WaitingStep(
            index, session, name, "nextest", f"/tmp/tree-{index}", NOW, GIB,
            memory_admit.ReadyForAdmission(),
        ) for index, (session, name) in enumerate(identities, 1))
        running = tuple(memory_admit.RunningStep(
            session, name, "clippy", f"/tmp/tree-{index}", GIB, memory_admit.MeasuredMemory(GIB),
        ) for index, (session, name) in enumerate(identities, 1))
        output = self.cli_output(memory_admit.QueueView(waiting, running, 20 * GIB, 0, GIB, NOW))
        labels = ("hana | organon | organon-ci88", "hana | merge | merge-gate", "hana | — | hana-showrunner",
                  "— | — | standalone", "offline | — | orphan", "— | — | recorded", "— | — | tree-7")
        for index, label in enumerate(labels, 1):
            for step in ("nextest", "clippy"):
                with self.subTest(index=index, step=step):
                    self.assertIn(f"| {index} | {label} | {step} | tree-{index} |", output)
        self.assertNotIn("Roster:", output)
        self.assertNotIn("clone88ci", output)
        self.roster_read.assert_called_once_with(os.environ, roster.ThisMachine())

    def test_roster_problems_preserve_matches_and_report_only_the_first_problem_after_totals(self) -> None:
        self.roster_read.return_value = roster.Roster(
            [self.entry("known", "live-name", showrunner=roster.RunsProduction("builds"))],
            ["one session unreadable", "second problem"],
        )
        running = (
            memory_admit.RunningStep("known", "old", "mend", "/tmp/tree", GIB, memory_admit.MeasuredMemory(GIB)),
            memory_admit.RunningStep("unknown", "recorded", "doc", "/tmp/other", GIB, memory_admit.MeasuredMemory(GIB)),
        )
        output = self.cli_output(memory_admit.QueueView((), running, 20 * GIB, 0, GIB, NOW))
        self.assertIn("| 1 | builds | — | live-name | mend |", output)
        self.assertIn("| 2 | — | — | recorded | doc |", output)
        lines = output.splitlines()
        totals = next(index for index, line in enumerate(lines) if line.startswith("Totals:"))
        self.assertEqual(lines[totals + 1:], [
            "Roster: one session unreadable; Showrunner and Unit show — for unmatched steps"])

    def test_both_tables_use_the_shared_unit_labels_without_rewriting_names(self) -> None:
        entries = [self.entry("style-id", "old-name")]
        running = memory_admit.RunningStep(
            "style-id", "recorded-name", "clippy", "/tmp/style", GIB, memory_admit.MeasuredMemory(GIB),
        )
        waiting = memory_admit.WaitingStep(
            10, "style-id", "recorded-name", "nextest", "/tmp/style", NOW, GIB,
            memory_admit.ReadyForAdmission(),
        )
        labels = {"style-id": roster.UnitLabels("style-impl", "cargo-liner", "cargo-liner-style-types")}
        with mock.patch.object(roster, "unit_labels", return_value=labels) as lookup:
            lines = build_queue.render_queue(
                queue_read_fixture(memory_admit.QueueView((waiting,), (running,), 20 * GIB, 0, GIB, NOW)),
                ZoneInfo("UTC"), entries,
            )
        lookup.assert_called_once_with(entries)
        for step in ("clippy", "nextest"):
            with self.subTest(step=step):
                self.assertTrue(any(
                    line.startswith(f"| 1 | cargo-liner | cargo-liner-style-types | style-impl | {step} | style |")
                    for line in lines
                ))

    def test_roster_exception_still_prints_both_tables_and_one_problem(self) -> None:
        self.roster_read.side_effect = RuntimeError("roster lookup failed")
        waiting = memory_admit.WaitingStep(10, "missing", "recorded", "nextest", "/tmp/tree",
                                          NOW, GIB, memory_admit.ReadyForAdmission())
        running = memory_admit.RunningStep("also-missing", "", "doc", "/tmp/folder", GIB,
                                          memory_admit.MeasuredMemory(GIB))
        output = self.cli_output(memory_admit.QueueView((waiting,), (running,), 20 * GIB, 0, GIB, NOW))
        self.assertIn("| 1 | — | — | recorded | nextest | tree |", output)
        self.assertIn("| 1 | — | — | folder | doc |", output)
        lines = output.splitlines()
        totals = next(index for index, line in enumerate(lines) if line.startswith("Totals:"))
        self.assertEqual(lines[totals + 1:], [
            "Roster: roster lookup failed; Showrunner and Unit show — for unmatched steps"])

    def test_queue_and_local_roster_reads_overlap(self) -> None:
        rendezvous = threading.Barrier(2, timeout=TEST_HANG_GUARD_SECS)
        view = memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW)

        def queue_read() -> memory_admit.QueueRead:
            _ = rendezvous.wait()
            return queue_read_fixture(view)

        def roster_read(_environment: object, _scope: object) -> roster.Roster:
            _ = rendezvous.wait()
            return roster.Roster([], [])

        self.roster_read.side_effect = roster_read
        output = io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", side_effect=queue_read) as read, \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        self.roster_read.assert_called_once_with(os.environ, roster.ThisMachine())
        self.assertNotIn("Roster:", output.getvalue())

    def test_behind_head_uses_the_roster_session_label(self) -> None:
        self.roster_read.return_value = roster.Roster([self.entry("head-id", "organon-ci88", codex=True)], [])
        head = memory_admit.WaitingStep(10, "head-id", "clone88ci", "nextest", "/tmp/head", NOW - 2400,
                                       GIB, memory_admit.ReadyForAdmission())
        follower = memory_admit.WaitingStep(
            11, "follower-id", "follower", "doc", "/tmp/follower", NOW - 300, GIB,
            memory_admit.BehindHead(head_name="clone88ci", head_since=NOW - 2400, head_session_id="head-id"))
        output = self.cli_output(memory_admit.QueueView((head, follower), (), 20 * GIB, 0, GIB, NOW))
        self.assertIn("behind organon-ci88 (waiting 40 min) |", output)
        self.assertNotIn("clone88ci", output)

    def test_head_reason_keeps_required_free_threshold_when_both_limits_hold(self) -> None:
        need, available = int(10.9 * GIB), 12 * GIB
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "21"}):
            decision = memory_admit.decide(available, 60 * GIB, need,
                                           [memory_admit.MeasuredMemory(int(16.1 * GIB))])
        waiting = memory_admit.WaitingStep(
            10, "head-id", "unit", "nextest", "/tmp/tree", NOW - 2400, need,
            memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW))
        view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if "head:" in line)
        self.assertTrue(line.endswith(
            "10.9 GiB | 12.0 GiB | head: builds use 16.1 of the 21.0 GiB budget "
            + "(starts when build use falls to 10.1 GiB); needs 13.9 GiB free: this step 10.9, earlyoom 3.0; no head priority: younger steps that fit may start |"))

    def test_older_fitting_step_reason_uses_the_shown_session_label(self) -> None:
        waiting = memory_admit.WaitingStep(200, "younger", "young", "check", "/tmp/young", NOW - 60,
            3 * GIB, memory_admit.BehindOlderFit("recorded-older", NOW - 120, "older-id"))
        view = memory_admit.QueueView((waiting,), (), 20 * GIB, 0, 3 * GIB, NOW)
        reading = queue_read_fixture(view)
        self.assertEqual(build_queue.reason_text(waiting, reading, ZoneInfo("UTC"),
            {"older-id": roster.UnitLabels("shown-older")}, memory_admit.NoWaiter()), "behind older step shown-older")
        self.assertEqual(build_queue.reason_text(waiting, reading, ZoneInfo("UTC"), {}, memory_admit.NoWaiter()),
                         "behind older step recorded-older")

    def test_bump_refuses_raw_selectors_without_moving_or_logging(self) -> None:
        first = self.write_waiter(100, "head-id", "head")
        second = self.write_waiter(200, "worker-id", "worker")
        self.roster_read.return_value = roster.Roster(
            [self.entry("head-id", "shared"), self.entry("worker-id", "shared", codex=True)], [])
        before = (first.read_bytes(), second.read_bytes())
        for selector in ("2", "worker-id", "worker", "/tmp/tree-200", "tree-200", "shared", "unknown"):
            with self.subTest(selector=selector), \
                 mock.patch.object(memory_admit, "bump_waiter", wraps=memory_admit.bump_waiter) as bump, \
                 mock.patch.object(build_queue, "read_queue_and_roster", side_effect=AssertionError("bump read the queue")):
                status, output, errors = self.bump_output(selector)
                self.assertEqual(status, 3)
                self.assertEqual(output, "build queue: only the user can bump; bump is off until /showrunner:bump sets queue priority\n")
                self.assertEqual(errors, "")
                bump.assert_called_once_with(selector, "actor-id")
                self.assertEqual((first.read_bytes(), second.read_bytes()), before)
                self.assertEqual([step.pid for step in memory_admit.queue_view().waiting], [100, 200])
                self.assertEqual(list((self.root / "buildlog").glob("*/*.jsonl")), [])

    def bump_output(self, selector: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "actor-id"}), \
             mock.patch("sys.argv", ["build_queue.py", "bump", selector]), \
             redirect_stdout(output), redirect_stderr(errors):
            status = build_queue.main()
        return status, output.getvalue(), errors.getvalue()


    def test_waiting_row_names_head_and_age_with_local_time(self) -> None:
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "21"}):
            decision = memory_admit.decide(int(23.1 * GIB), 60 * GIB, int(10.9 * GIB),
                                           [memory_admit.MeasuredMemory(15 * GIB)])
        head = memory_admit.WaitingStep(
            10, "head-id", "clone88ci", "check", "/tmp/clone88", NOW - 2400,
            int(10.9 * GIB), memory_admit.head_wait_reason(decision, int(10.9 * GIB), int(23.1 * GIB),
                {"pid": 10, "start_time": "10", "need": int(10.9 * GIB), "since": NOW - 2400}, NOW),
        )
        follower = memory_admit.WaitingStep(
            11, "follower-id", "", "doc", "/tmp/tree", NOW - 300,
            int(11.6 * GIB), memory_admit.BehindHead(head_name="clone88ci", head_since=NOW - 2400, head_session_id="head-id"),
        )
        view = memory_admit.QueueView(
            (head, follower), (), int(23.1 * GIB), 0, 3 * GIB, NOW,
        )
        lines = build_queue.render_queue(queue_read_fixture(view), ZoneInfo("America/New_York"))
        output = "\n".join(lines)
        self.assertIn("| 1 | — | — | clone88ci | check | clone88 | 14:20 (40 min) | 10.9 GiB | 23.1 GiB | "
                      + "head: builds use 15.0 of the 21.0 GiB budget "
                      + "(starts when build use falls to 10.1 GiB); no head priority: younger steps that fit may start |", output)
        self.assertIn("| 2 | — | — | tree | doc | tree | 14:55 (5 min) | 11.6 GiB | 23.1 GiB | "
                      + "behind clone88ci (waiting 40 min) |", output)
        self.assertNotIn("head-id", output)
        self.assertNotIn("follower-id", output)
        self.assertLess(lines.index("**Running**"), lines.index("**Waiting**"))

    def test_memory_reason_names_free_memory_and_earlyoom_reserve(self) -> None:
        pending = int(23.5 * GIB)
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "check", "/tmp/tree", NOW - 2400,
            int(11.6 * GIB), memory_admit.NeedsMemory(
                int(11.6 * GIB), int(23.1 * GIB), threshold=int(11.6 * GIB) + pending + 3 * GIB,
                growth=pending, reserve=3 * GIB),
        )
        running = (
            memory_admit.RunningStep("one", "", "test", "/one", 12 * GIB, memory_admit.PromisedMemory(12 * GIB)),
            memory_admit.RunningStep("two", "", "test", "/two", int(11.5 * GIB), memory_admit.PromisedMemory(int(11.5 * GIB))),
            memory_admit.RunningStep("three", "", "test", "/three", 8 * GIB, memory_admit.MeasuredMemory(GIB)),
        )
        view = memory_admit.QueueView((waiting,), running, int(23.1 * GIB), pending, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if line.startswith("| ") and "earlyoom" in line)
        self.assertTrue(line.endswith(
            "11.6 GiB | 23.1 GiB | needs 38.1 GiB free: this step 11.6, "
            + "running steps may grow 23.5, earlyoom 3.0 |"))

    def test_memory_hold_above_step_need_names_every_threshold_part(self) -> None:
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "clippy", "/tmp/tree", NOW, 3 * GIB,
            memory_admit.NeedsMemory(
                3 * GIB, int(11.5 * GIB), threshold=6 * GIB + int(13.4 * GIB),
                growth=int(13.4 * GIB), reserve=3 * GIB),
        )
        view = memory_admit.QueueView((waiting,), (), int(11.5 * GIB), int(13.4 * GIB), 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if "| clippy |" in line)
        self.assertTrue(line.endswith(
            "3.0 GiB | 11.5 GiB | needs 19.4 GiB free: this step 3.0, "
            + "running steps may grow 13.4, earlyoom 3.0 |"), line)

    def test_memory_reason_omits_parts_that_round_to_zero(self) -> None:
        for growth, reserve, expected in (
            (0, 3 * GIB, "needs 6.0 GiB free: this step 3.0, earlyoom 3.0"),
            (1, 1, "needs 3.0 GiB free: this step 3.0"),
            (0, 0, "needs 0.0 GiB free: this step 0.0"),
        ):
            with self.subTest(growth=growth, reserve=reserve):
                need = 0 if (growth, reserve) == (0, 0) else 3 * GIB
                waiting = memory_admit.WaitingStep(
                    10, "waiting-id", "unit", "clippy", "/tmp/tree", NOW, need,
                    memory_admit.NeedsMemory(need, 0, threshold=need + growth + reserve,
                                            growth=growth, reserve=reserve),
                )
                view = memory_admit.QueueView((waiting,), (), 0, growth, reserve, NOW)
                line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if "| clippy |" in line)
                self.assertEqual(line.split("|")[-2].strip(), expected)

    def test_head_free_threshold_names_step_growth_and_reserve(self) -> None:
        decision = memory_admit.decide(5 * GIB, 60 * GIB, 3 * GIB,
                                       [memory_admit.PromisedMemory(4 * GIB)])
        waiting = memory_admit.WaitingStep(
            10, "head-id", "unit", "clippy", "/tmp/tree", NOW - 2400, 3 * GIB,
            memory_admit.head_wait_reason(decision, 3 * GIB, 5 * GIB,
                {"pid": 10, "start_time": "10", "need": 3 * GIB, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting,), (), 5 * GIB, 4 * GIB, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if "head:" in line)
        self.assertTrue(line.endswith(
            "head: needs 10.0 GiB free: this step 3.0, running steps may grow 4.0, earlyoom 3.0; "
            + "no head priority: younger steps that fit may start |"), line)

    def test_budget_reason_names_charged_builds_and_the_limit(self) -> None:
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "check", "/tmp/tree", NOW - 300,
            int(10.8 * GIB), memory_admit.NeedsBuildBudget(int(10.8 * GIB), int(17.6 * GIB), 20 * GIB),
        )
        view = memory_admit.QueueView((waiting,), (), 30 * GIB, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if line.startswith("| ") and "budget" in line)
        self.assertTrue(line.endswith("10.8 GiB | 30.0 GiB | builds use 17.6 of the 20.0 GiB budget |"))

    def test_head_reason_names_measured_budget_limit_and_start_level(self) -> None:
        need, available = int(15.1 * GIB), int(19.5 * GIB)
        memory: list[memory_admit.StepMemory] = [memory_admit.MeasuredMemory(int(1.32 * GIB)) for _ in range(10)]
        with unittest.mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "20"}):
            decision = memory_admit.decide(available, 60 * GIB, need, memory)
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "nextest", "/tmp/tree", NOW - 2400,
            need, memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if "head:" in line)
        self.assertTrue(line.endswith("head: builds use 13.2 of the 20.0 GiB budget "
                                      + "(starts when build use falls to 4.9 GiB); no head priority: younger steps that fit may start |"))

    def test_head_reason_names_only_free_memory_when_budget_passes(self) -> None:
        need, available = int(15.1 * GIB), 18 * GIB
        decision = memory_admit.decide(available, 60 * GIB, need, [memory_admit.MeasuredMemory(GIB)])
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "nextest", "/tmp/tree", NOW - 2400,
            need, memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")) if "head:" in line)
        self.assertTrue(line.endswith("head: needs 18.1 GiB free: this step 15.1, earlyoom 3.0; no head priority: younger steps that fit may start |"))

    def test_head_backfill_label_uses_the_since_zone_and_becomes_strict_at_ten_minutes(self) -> None:
        available = 18 * GIB
        for need_gib, waited, bump, suffix in (
            (15.1, 540, 0, "younger steps may start until 15:01 (1 min 0 s left)"),
            (15.1, 600, 0, "younger steps may use 5.9 GiB of the budget until 15:10 (10 min 0 s left)"),
            (15.1, 60, -1, "younger steps may use 5.9 GiB of the budget until 15:10 (10 min 0 s left)"),
            (21, 600, 0, "younger steps wait until 15:10 (10 min 0 s left)"),
            (22, 600, 0, "younger steps wait until 15:10 (10 min 0 s left)"),
        ):
            with self.subTest(need=need_gib, waited=waited, bump=bump):
                need = int(need_gib * GIB)
                decision = memory_admit.decide(available, 60 * GIB, need, [memory_admit.MeasuredMemory(GIB)])
                row: memory_admit.Waiter = {"pid": 10, "start_time": "10", "need": need,
                                           "since": NOW - waited, "bump_order": bump}
                waiting = memory_admit.WaitingStep(
                    10, "waiting-id", "unit", "nextest", "/tmp/tree", row["since"], need,
                    memory_admit.head_wait_reason(decision, need, available, row, NOW))
                view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
                reading = queue_read_fixture(view)
                head = reading.explanation.head
                if not isinstance(head, memory_admit.HeadExplanation):
                    self.fail("fixture needs a head")
                reading = replace(reading, explanation=replace(reading.explanation,
                    head=replace(head, has_priority=True)))
                line = next(line for line in build_queue.render_queue(reading, ZoneInfo("America/New_York")) if "head:" in line)
                budget = (f"builds use 1.0 of the 21.0 GiB budget "
                          + f"(starts when build use falls to {21 - need_gib:.1f} GiB); "
                          if need_gib >= 21 else "")
                self.assertTrue(line.endswith(
                    "head: " + budget
                    + f"needs {need_gib + 3:.1f} GiB free: this step {need_gib:.1f}, earlyoom 3.0; " + suffix + " |"), line)

    def test_live_queue_pressure_reason_reaches_the_cli(self) -> None:
        process = self.root / "proc/100"
        process.mkdir()
        fields = ["S", "1", *(["0"] * 17), "100"]
        _ = (process / "stat").write_text(f"100 (verify) {' '.join(fields)}\n")
        directory = self.root / "buildlog/admission"
        directory.mkdir(parents=True)
        _ = (directory / "100.waiting").write_text(json.dumps({
            "pid": 100, "start_time": "100", "need": 8 * GIB, "since": NOW,
            "session_name": "unit", "step": "check", "folder": "/tree",
        }))
        _ = (self.root / "pressure").write_text("full avg10=23.4 avg60=1.0 total=1\n")
        output = io.StringIO()
        with mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        self.assertIn("| 1 | — | — | unit | check | tree |", output.getvalue())
        self.assertIn("memory pressure: full avg10 23.4", output.getvalue())
        self.assertNotIn("swap", output.getvalue())
        self.assertNotIn("ready", output.getvalue())

    def test_unavailable_reading_does_not_claim_zero_free_memory(self) -> None:
        step = memory_admit.WaitingStep(
            10, "session", "unit", "check", "/tmp/tree", NOW - 300,
            12 * GIB, memory_admit.ReadingUnavailable(),
        )
        follower = memory_admit.WaitingStep(
            11, "follower", "other", "doc", "/tmp/other", NOW - 60,
            GIB, memory_admit.BehindHead("unit", NOW - 300, "session"),
        )
        view = memory_admit.QueueView((step, follower), (), 0, 0, 0, NOW)
        lines = build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC"))
        line = next(line for line in lines if line.startswith("| ") and "unavailable" in line)
        self.assertTrue(line.endswith("12.0 GiB | — | memory reading unavailable |"))
        for position in (1, 2):
            row = next(line for line in lines if line.startswith(f"| {position} |"))
            cells = [cell.strip() for cell in row.split("|")[1:-1]]
            with self.subTest(position=position):
                self.assertEqual(cells[8], "—")
                self.assertNotIn("0.0 GiB", cells[8])

    def test_running_rows_show_signed_delta_and_totals(self) -> None:
        steps = (
            memory_admit.RunningStep("session-id", "unit", "lint", "/tmp/tree", 8 * GIB,
                                    memory_admit.MeasuredMemory(int(1.2 * GIB))),
            memory_admit.RunningStep("other-id", "link", "build", "/tmp/link", 4 * GIB,
                                    memory_admit.MeasuredMemory(5 * GIB)),
        )
        view = memory_admit.QueueView((), steps, 20 * GIB, 8 * GIB, GIB, NOW)
        output = "\n".join(build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")))
        self.assertIn("| 1 | — | — | unit | lint | tree | 8.0 GiB | 1.2 GiB | 6.8 GiB | measured |", output)
        self.assertIn("| 2 | — | — | link | build | link | 4.0 GiB | 5.0 GiB | -1.0 GiB | measured |", output)
        self.assertIn("Totals: promised 12.0 GiB; measured 6.2 GiB; free 20.0 GiB; reserve 1.0 GiB", output)

    def test_pending_and_ended_steps_do_not_report_zero_measured_use(self) -> None:
        pending = memory_admit.RunningStep("first", "", "check", "/one", 8 * GIB, memory_admit.PromisedMemory(8 * GIB))
        ended = memory_admit.RunningStep("second", "", "test", "/two", 12 * GIB, memory_admit.GoneMemory())
        view = memory_admit.QueueView((), (pending, ended), 20 * GIB, 8 * GIB, GIB, NOW)
        output = "\n".join(build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC")))
        self.assertIn("| 1 | — | — | one | check | one | 8.0 GiB | — | — | starting |", output)
        self.assertNotIn("second", output)
        self.assertNotIn("two", output)
        self.assertIn("promised 8.0 GiB; measured 0.0 GiB", output)

    def test_empty_queue_reads_once_without_subprocess_or_wait(self) -> None:
        view = memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW)
        output = io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", return_value=queue_read_fixture(view)) as read, \
             mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             mock.patch("time.sleep", side_effect=AssertionError("no wait")), \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        self.roster_read.assert_called_once_with(os.environ, roster.ThisMachine())
        self.assertEqual(output.getvalue(), (
            "**Running**\n\n"
            + "| # | Showrunner | Unit | Session | Step | Folder | Promised | Using | Delta | State |\n"
            + "| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |\n"
            + "| empty |  |  |  |  |  |  |  |  |  |\n\n"
            + "**Waiting**\n\n"
            + "| # | Showrunner | Unit | Session | Step | Folder | Waiting since | Needs | Free | Reason |\n"
            + "| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |\n"
            + "| empty |  |  |  |  |  |  |  |  |  |\n\n"
            + "Totals: promised 0.0 GiB; measured 0.0 GiB; free 20.0 GiB; reserve 1.0 GiB\n"))

    def test_one_row_per_table_uses_names_and_escapes_cell_text(self) -> None:
        waiting = memory_admit.WaitingStep(10, "hidden-id", "", "doc", "/tmp/hana/",
                                          NOW - 60, GIB, memory_admit.ReadyForAdmission())
        running = memory_admit.RunningStep("other-hidden-id", "lfo|trunk\nunit", "check", "/tmp/lfo",
                                          2 * GIB, memory_admit.MeasuredMemory(GIB))
        view = memory_admit.QueueView((waiting,), (running,), 20 * GIB, GIB, GIB, NOW)
        lines = build_queue.render_queue(queue_read_fixture(view), ZoneInfo("UTC"))
        output = "\n".join(lines)
        self.assertIn("| 1 | — | — | lfo\\|trunk unit | check | lfo | 2.0 GiB | 1.0 GiB | 1.0 GiB | measured |", output)
        self.assertIn("| 1 | — | — | hana | doc | hana | 18:59 (1 min) | 1.0 GiB | 20.0 GiB | "
                      + "ready for the next admission check |", output)
        self.assertEqual(sum(line.startswith("| 1 |") for line in lines), 2)
        self.assertNotIn("empty", output)
        self.assertNotIn("hidden-id", output)

    def test_cli_reports_admission_lock_timeout_with_status_two(self) -> None:
        directory = self.root / "buildlog/admission"
        directory.mkdir(parents=True)
        errors = io.StringIO()
        output = io.StringIO()
        with (directory / "lock").open("a+") as lock, \
             mock.patch.object(memory_admit, "QUEUE_LOCK_TIMEOUT_SECONDS", 0.03), \
             mock.patch("sys.argv", ["build_queue.py"]), \
             redirect_stderr(errors), redirect_stdout(output):
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(build_queue.main(), 2)
        self.assertEqual(errors.getvalue(), "build queue: admission lock busy for 0.03 s; try again\n")
        self.assertEqual(output.getvalue(), "")

    def test_ten_thousand_waiting_and_running_rows_render_in_under_one_second(self) -> None:
        waiting = tuple(memory_admit.WaitingStep(
            pid, f"session-{pid}", "", "check", f"/tmp/tree-{pid}", NOW - pid,
            12 * GIB, memory_admit.NeedsMemory(12 * GIB, 20 * GIB),
        ) for pid in range(10000))
        running = tuple(memory_admit.RunningStep(
            f"session-{pid}", "", "test", f"/tmp/tree-{pid}", 8 * GIB, memory_admit.MeasuredMemory(GIB),
        ) for pid in range(10000))
        view = memory_admit.QueueView(waiting, running, 20 * GIB, 10 * GIB, GIB, NOW)
        started = time.perf_counter()
        lines = build_queue.render_queue(queue_read_fixture(view), ZoneInfo("America/New_York"))
        elapsed = time.perf_counter() - started
        self.assertEqual(sum(line.startswith("| ") for line in lines), 20004)
        self.assertEqual(sum(line.startswith("| 10000 | — | — | tree-9999 |") for line in lines), 2)
        self.assertLess(elapsed, 1.0, f"render took {elapsed:.6f} s")
        print(f"queue render: 20000 rows in {elapsed:.6f} s")


    def test_queue_read_failures_exit_two_with_diagnostics_only_on_stderr(self) -> None:
        for error in (PermissionError(13, "Permission denied", "/private/admission/lock"),
                      ValueError("queue reading invalid")):
            with self.subTest(error=error):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(memory_admit, "locked_queue_read", side_effect=error), \
                     mock.patch("sys.argv", ["build_queue.py"]), \
                     redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(build_queue.main(), 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), f"build queue: {error}\n")


if __name__ == "__main__":
    _ = unittest.main()
