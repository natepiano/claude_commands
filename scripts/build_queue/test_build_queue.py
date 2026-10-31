"""The queue CLI uses one admission reading, file-only labels and shared span renders."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Callable, Mapping
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock
from typing import Protocol, cast, override
from zoneinfo import ZoneInfo

from scripts.build_queue import build_queue, head_facts, queue_card, queue_view
from scripts.build_queue.queue_fixture import (
    NOW, ZONE, QueueFixture, bump_line, head_reading, queue_read_fixture,
    queue_time_line, token_gap_line, waiting_rows, write_bumps, write_process, write_token_gaps,
)
from scripts.build_queue.queue_lines import QueueLines, plain, terminal_width
from scripts.buildlog import store
from scripts.lint import memory_admit
from scripts.message import roster, unit_labels
from scripts.message.unit_labels import UnitLabels, unit_labels_for

GIB = memory_admit.GIB


class LabelReadMock(Protocol):
    return_value: dict[str, UnitLabels]
    side_effect: Callable[[list[str], Mapping[str, str]], dict[str, UnitLabels]]

    def assert_called_once(self) -> None: ...
    def assert_called_once_with(self, ids: list[str], environment: Mapping[str, str]) -> None: ...
    def reset_mock(self) -> None: ...


class QueueTests(unittest.TestCase):
    fixture: QueueFixture = cast(QueueFixture, cast(object, None))
    root: Path = Path()
    labels_read: LabelReadMock = cast(LabelReadMock, cast(object, None))

    @override
    def setUp(self) -> None:
        self.fixture = self.enterContext(QueueFixture(captured=False))
        self.root = self.fixture.root
        self.labels_read = cast(LabelReadMock, self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={})))
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "100"}))

    @staticmethod
    def entry(session: str, name: str) -> tuple[str, UnitLabels]:
        return session, UnitLabels(name)

    def model(self, reading: memory_admit.QueueRead, zone: ZoneInfo = ZONE) -> queue_view.QueueModel:
        return queue_view.QueueModel(reading, self.labels_read.return_value,
            head_facts.read_head_facts(reading), head_facts.read_pace(reading.explanation.now), zone)

    def render(self, view: memory_admit.QueueView, columns: int = 400) -> str:
        return plain(queue_view.render_view(self.model(queue_read_fixture(view)), columns))

    def invoke(self, reading: memory_admit.QueueRead, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", return_value=reading) as read, \
             mock.patch("sys.argv", ["build_queue.py", *arguments]), \
             redirect_stdout(output), redirect_stderr(errors):
            status = build_queue.main()
        read.assert_called_once_with()
        return status, output.getvalue(), errors.getvalue()

    def snapshot(self, reading: memory_admit.QueueRead, *arguments: str) -> QueueLines:
        status, output, errors = self.invoke(reading, "--json", "--columns", "400", *arguments)
        self.assertEqual(status, 0, errors)
        self.assertEqual(errors, "")
        return cast(QueueLines, json.loads(output))

    @staticmethod
    def snapshot_text(data: QueueLines) -> str:
        return "\n".join("".join(span["text"] for span in row) for row in data["lines"])


    def test_older_fitting_step_reason_uses_the_shown_session_label(self) -> None:
        waiting = memory_admit.WaitingStep(200, "younger", "young", "check", "/tmp/young", NOW - 60,
            3 * GIB, memory_admit.BehindOlderFit("recorded-older", NOW - 120, "older-id"))
        head = replace(waiting, pid=100, session_id="head", reason=memory_admit.ReadyForAdmission())
        view = memory_admit.QueueView((head, waiting), (), 20 * GIB, 0, 3 * GIB, NOW)
        model = self.model(queue_read_fixture(view), ZoneInfo("UTC"))
        for labels, expected in (({"older-id": roster.UnitLabels("shown-older")}, "shown-older"),
                                 ({}, "recorded-older")):
            with self.subTest(labels=labels):
                line = plain(queue_view.rest_lines(replace(model, labels=labels)))
                self.assertEqual(line.removeprefix("#2: "), "behind older step " + expected)

    def test_bump_refuses_raw_selectors_without_moving_or_logging(self) -> None:
        first = self.fixture.waiter(100, "head-id", "head")
        second = self.fixture.waiter(200, "worker-id", "worker")
        self.labels_read.return_value = dict([self.entry("head-id", "shared"), self.entry("worker-id", "shared")])
        before = (first.read_bytes(), second.read_bytes())
        for selector in ("2", "worker-id", "worker", "/tmp/tree-200", "tree-200", "shared", "unknown"):
            with self.subTest(selector=selector), \
                 mock.patch.object(memory_admit, "bump_waiter", wraps=memory_admit.bump_waiter) as bump, \
                 mock.patch.object(memory_admit, "locked_queue_read", side_effect=AssertionError("bump read the queue")), \
                 mock.patch.object(unit_labels, "unit_labels_for", side_effect=AssertionError("bump read labels")):
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


    def test_cli_reports_admission_lock_timeout_with_status_two(self) -> None:
        directory = self.root / "buildlog/admission"
        directory.mkdir(parents=True, exist_ok=True)
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


    def test_head_and_why_share_the_same_read_and_never_recalculate_checks(self) -> None:
        from scripts.build_queue import build_why
        from scripts.build_queue.test_build_why import BuildWhyTests
        fixture = BuildWhyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", self.labels_read))
        reading = memory_admit.locked_queue_read()
        facts = head_facts.read_head_facts(reading)
        why = "\n".join(build_why.render_explanation(reading.explanation, ZONE, {}, facts))
        legacy = replace(reading.view.waiting[0], reason=memory_admit.ReadyForAdmission(), need=GIB)
        reading = replace(reading, view=replace(reading.view, available=0, waiting=(legacy, )))
        with mock.patch.object(memory_admit, "decide", side_effect=AssertionError("one decision")), \
             mock.patch.object(memory_admit, "build_charge", side_effect=AssertionError("recorded charges")):
            text = plain(queue_view.render_view(self.model(reading), 400))
        for value in ("18.1", "11.8", "16.3", "9.2"):
            self.assertIn(value, text)
            self.assertIn(value, why)
        self.assertIn("free memory 16.3 GiB; needs 20.3", text)
        self.assertIn("Waiting because: build budget, free memory", why)
        with mock.patch.object(memory_admit, "queue_view", side_effect=AssertionError("no separate queue read")):
            data = self.snapshot(reading)
        self.assertEqual(self.snapshot_text(data), text)

    def test_every_text_table_marks_numbers_right_and_text_left(self) -> None:
        with QueueFixture() as fixture:
            rows = waiting_rows(queue_view.render_view(fixture.model(), 140))
        self.assertEqual(len(rows), 32)
        self.assertEqual([row[2:4] for row in rows[:10]], [f"{n:>2}" for n in range(1, 11)])
        self.assertEqual([row.index("nextest") for row in rows[:2]], [27, 27])
        self.assertTrue(all("| ---" not in row for row in rows))

    def test_head_line_short_forms_share_the_recorded_head_and_bump_facts(self) -> None:
        reading = head_reading(since=NOW - 158 * 60, now=NOW,
                              fields={"head_since": NOW - 125, "priority_since": NOW - 3600})
        write_bumps(NOW, [bump_line(NOW - 5, folder="/tmp/alpha"),
                          bump_line(NOW - 4, folder="/tmp/beta", step="clippy"),
                          bump_line(NOW - 3, folder="/tmp/alpha")])
        text = self.snapshot_text(self.snapshot(reading))
        self.assertIn("Head for: 2 min (since 10:44:07)", text)
        self.assertNotIn("158 min (since", text)
        facts = head_facts.read_head_facts(reading)
        assert isinstance(facts, head_facts.HeadFacts)
        assert isinstance(facts.bumps, head_facts.BumpHistory)
        self.assertEqual(facts.bumps.total, 3)
        self.assertEqual(facts.bumps.groups[0].count, 2)
        self.assertEqual(facts.bumps.groups[0].folder, "/tmp/alpha")

    def test_missing_head_time_and_empty_bump_history_have_distinct_short_forms(self) -> None:
        text = self.snapshot_text(self.snapshot(head_reading(since=NOW - 600, now=NOW)))
        self.assertNotIn("head for: unknown", text)
        self.assertNotIn("no bumps since it joined", text)
        self.assertNotIn("head for 10 min", text)

    def test_no_strict_window_without_admissions_head_priority(self) -> None:
        reading = head_reading(now=NOW, since=NOW - 600)
        head = reading.explanation.head
        assert isinstance(head, memory_admit.HeadExplanation)
        follower = replace(reading.view.waiting[0], pid=124, reason=memory_admit.ReadyForAdmission())
        reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, follower)),
            ordered=[*reading.ordered, (Path("124.waiting"), {"pid":124, "start_time":"124", "need":GIB, "since":NOW})],
            explanation=replace(reading.explanation, head=replace(head, order=memory_admit.StrictUntil(NOW + 123), has_priority=False)))
        text = self.snapshot_text(self.snapshot(reading))
        self.assertNotIn("strict", text)
        self.assertNotIn("younger steps wait", text)
        self.assertIn("#2: starts at the next check", text)

    def test_bump_read_failure_is_unknown_and_snapshot_keys_stay_unchanged(self) -> None:
        store.host_file(store.host_name(), NOW).parent.mkdir(parents=True, exist_ok=True)
        store.host_file(store.host_name(), NOW).mkdir()
        data = self.snapshot(head_reading(since=NOW - 600, now=NOW))
        self.assertEqual(set(data), {"columns", "as_of", "lines"})
        self.assertNotIn("bumps: unknown", self.snapshot_text(data))
        self.assertNotIn("no bumps since it joined", self.snapshot_text(data))
        self.assertTrue(all(set(span) == {"text", "tone", "bold"} for row in data["lines"] for span in row))

    def test_missing_token_return_never_adds_a_current_token_wait_row(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW, fields={"token_left_at":NOW - 60})
        first = reading.view.waiting[0]
        follower = replace(first, pid=124, session_id="follower", session_name="follower")
        reading = replace(reading, view=replace(reading.view, waiting=(first, follower)),
            ordered=[*reading.ordered, (Path("124.waiting"), {"pid":124, "start_time":"124", "need":GIB, "since":NOW - 600})])
        write_token_gaps([queue_time_line(NOW - 60, kind="token_gap", session_id="head-session",
            step="nextest", pid=123, start_time="123", token_left_at=NOW - 60)])
        text = self.snapshot_text(self.snapshot(reading))
        self.assertEqual(len(waiting_rows(queue_view.render_view(self.model(reading), 400))), 2)
        self.assertNotIn("waiting for the cargo token", text)
        self.assertNotIn("cargo token:", text)

    def test_a_returned_token_record_does_not_add_a_current_token_wait_row(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)])
        self.assertEqual(len(waiting_rows(queue_view.render_view(self.model(reading), 400))), 1)

    def test_head_line_shows_a_recorded_cargo_token_wait(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)])
        data = self.snapshot(reading)
        self.assertIn("cargo token: waited 154 s (10:42-10:45)", self.snapshot_text(data))
        self.assertEqual(set(data), {"columns", "as_of", "lines"})

    def test_head_line_cargo_token_sum_excludes_other_waiting_steps(self) -> None:
        reading = head_reading(since=NOW - 1000, now=NOW)
        follower = replace(reading.view.waiting[0], pid=456, session_id="follower", step="clippy", since=NOW - 800)
        path, row = reading.ordered[0]
        follower_row: memory_admit.Waiter = {**row, "pid": follower.pid,
            "start_time": str(follower.pid), "session_id": follower.session_id,
            "step": follower.step, "since": follower.since}
        reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, follower)),
                          ordered=[*reading.ordered, (path.with_name("456-456.waiting"), follower_row)])
        write_token_gaps([token_gap_line(NOW - 900, NOW - 800), token_gap_line(NOW - 700, NOW - 400,
            session="follower", step="clippy", pid=456), token_gap_line(NOW - 200, NOW - 46)])
        text = self.snapshot_text(self.snapshot(reading))
        self.assertIn("cargo token: waited 254 s", text)
        self.assertNotIn("cargo token: waited 554 s", text)

    def test_head_line_distinguishes_missing_history_from_no_head_token_wait(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        missing = self.snapshot_text(self.snapshot(reading))
        self.assertNotIn("cargo token:", missing)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="unrelated", pid=999)])
        self.assertNotIn("cargo token:", self.snapshot_text(self.snapshot(reading)))
        self.assertNotIn("no cargo token wait", self.snapshot_text(self.snapshot(reading)))

    def test_head_line_partial_token_history_does_not_claim_a_complete_sum(self) -> None:
        reading = head_reading(since=NOW - 600, now=NOW)
        write_token_gaps([token_gap_line(NOW - 200, NOW - 46)], tail=token_gap_line(NOW - 40, NOW - 20).rstrip(b"\n"))
        text = self.snapshot_text(self.snapshot(reading))
        self.assertNotIn("cargo token: unknown", text)
        self.assertNotIn("cargo token: waited", text)

    def test_json_cli_matches_text_cells_and_preserves_all_roster_problems(self) -> None:
        self.labels_read.return_value = {"worker-id": UnitLabels("shown-worker", "builds", "report")}
        running = (
            memory_admit.RunningStep("worker-id", "old-name", "lint", "/tmp/tree", 8 * GIB,
                                    memory_admit.MeasuredMemory(GIB)),
            memory_admit.RunningStep("starting", "starting", "doc", "/tmp/docs", 2 * GIB,
                                    memory_admit.PromisedMemory(2 * GIB)),
            memory_admit.RunningStep("gone-session", "gone-session", "check", "/tmp/gone", GIB,
                                    memory_admit.GoneMemory()),
        )
        waiting = memory_admit.WaitingStep(10, "worker-id", "old-name", "nextest", "/tmp/tree",
                                          NOW - 300, 12 * GIB, memory_admit.ReadyForAdmission())
        view = memory_admit.QueueView((waiting, ), running, 20 * GIB, 2 * GIB, 3 * GIB, NOW)
        reading = queue_read_fixture(view)
        data = self.snapshot(reading)
        status, text, errors = self.invoke(reading, "--columns", "400")
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.snapshot_text(data) + "\n", text)
        self.assertEqual(data["columns"], 400)
        self.assertEqual(data["as_of"], NOW)
        runs = [line for line in text.splitlines() if line.startswith("●")]
        self.assertEqual(len(runs), 2)
        self.assertIn("builds · report · shown-worker", runs[0])
        self.assertIn("1.0", runs[0])
        self.assertIn("2.0", runs[1])
        self.assertIn("—", runs[1])
        self.assertIn("5 min", waiting_rows(queue_view.render_view(self.model(reading), 400))[0])
        self.assertIn("12.0", text)
        self.assertIn("free memory 20.0 GiB; needs 15.0: this step 12.0 + earlyoom 3.0", text)
        self.assertNotIn("gone-session", text)
        self.assertNotIn("Roster:", text)

    def test_json_empty_tables_are_arrays_and_do_not_invent_a_head(self) -> None:
        data = self.snapshot(queue_read_fixture(memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW)))
        self.assertIsInstance(data["lines"], list)
        text = self.snapshot_text(data)
        self.assertIn("0 running · 0 waiting · nothing waits", text)
        self.assertNotIn("#1 waits on", text)
        self.assertNotIn("■", text)
        self.assertNotIn("●", text)

    def test_fixture_json_stays_within_three_times_its_text_size(self) -> None:
        with QueueFixture() as fixture:
            reading = fixture.read()
            self.labels_read.return_value = fixture.labels
            status, text, errors = self.invoke(reading, "--columns", "100")
            json_status, encoded, json_errors = self.invoke(reading, "--json", "--columns", "100")
        self.assertEqual(status, 0, errors)
        self.assertEqual(json_status, 0, json_errors)
        self.assertEqual(errors, "")
        self.assertEqual(json_errors, "")
        data = cast(QueueLines, json.loads(encoded))
        self.assertEqual(self.snapshot_text(data) + "\n", text)
        self.assertLessEqual(len(encoded.encode()), 3 * len(text.encode()))
        print(f"queue fixture size: text {len(text.encode())} bytes; JSON {len(encoded.encode())} bytes")

    def test_budget_head_names_every_charged_step_with_its_actual_measured_use(self) -> None:
        running = (
            memory_admit.RunningStep("holder", "stale-name", "nextest", "/tmp/title", int(10.8 * GIB),
                memory_admit.MeasuredMemory(int(.5 * GIB), observed_peak=int(10.8 * GIB))),
            memory_admit.RunningStep("starting", "starting-worker", "doc", "/tmp/docs", 4 * GIB, memory_admit.PromisedMemory(4 * GIB)),
            memory_admit.RunningStep("gone", "ended-worker", "check", "/tmp/gone", GIB, memory_admit.GoneMemory()))
        self.labels_read.return_value = {"holder":UnitLabels("widget-enhancements-title")}
        decision = memory_admit.decide(30 * GIB, 60 * GIB, 12 * GIB, [step.measured for step in running],
            limits=memory_admit.AdmissionLimits(20 * GIB, 3 * GIB))
        row:memory_admit.Waiter = {"pid":10, "start_time":"10", "need":12 * GIB, "since":NOW - 300}
        head = memory_admit.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], 12 * GIB,
            memory_admit.head_wait_reason(decision, 12 * GIB, 30 * GIB, row, NOW))
        text = self.render(memory_admit.QueueView((head, ), running, 30 * GIB, 4 * GIB, 3 * GIB, NOW))
        self.assertIn("14.8 of 20.0 GiB held; needs 12.0", text)
        measured = next(line for line in text.splitlines() if "widget-enhancements-title" in line)
        self.assertIn("0.5", measured)
        self.assertIn("10.8", measured)
        starting = next(line for line in text.splitlines() if "starting-worker" in line)
        self.assertIn("—", starting)
        self.assertIn("4.0", starting)
        self.assertNotIn("stale-name", text)
        self.assertNotIn("ended-worker", text)

    def test_stalled_head_starts_at_fifteen_minutes_only_when_free_memory_covers_need(self) -> None:
        for elapsed, available in ((899, 8 * GIB), (900, 8 * GIB), (901, 8 * GIB - 1), (1200, 30 * GIB)):
            with self.subTest(elapsed=elapsed, available=available):
                step = memory_admit.WaitingStep(10, "head", "worker", "nextest", "/tmp/head", NOW - elapsed,
                    8 * GIB, memory_admit.NeedsBuildBudget(8 * GIB, 19 * GIB, 20 * GIB))
                text = self.render(memory_admit.QueueView((step, ), (), available, 0, 3 * GIB, NOW))
                self.assertIn("#1 waits on budget", text)
                self.assertIn("✗ build budget: 19.0 of 20.0 GiB held; needs 8.0", text)
                self.assertNotIn("Stalled:", text)

    def test_unavailable_head_never_claims_a_stall_even_after_fifteen_minutes(self) -> None:
        step = memory_admit.WaitingStep(10, "head", "worker", "check", "/tmp/head", NOW - 1800, 0, memory_admit.ReadingUnavailable())
        text = self.render(memory_admit.QueueView((step, ), (), 0, 0, 0, NOW))
        self.assertIn("memory reading unavailable", text)
        self.assertNotIn("Stalled:", text)
        self.assertNotIn("free memory 0.0", text)

    def test_both_tables_resolve_workers_directors_showrunners_and_unmatched_sessions(self) -> None:
        identities = (("worker-id", "clone88ci"), ("director-id", "old-director"), ("showrunner-id", "old-showrunner"),
            ("standalone-id", "old-standalone"), ("orphan-id", "old-orphan"), ("missing-id", "recorded"), ("nameless-id", ""))
        self.labels_read.return_value = {
            "worker-id":UnitLabels("organon-ci88", "hana", "organon"),
            "director-id":UnitLabels("merge-gate", "hana", "merge"),
            "showrunner-id":UnitLabels("hana-showrunner", "hana"),
            "standalone-id":UnitLabels("standalone"), "orphan-id":UnitLabels("orphan", "offline")}
        waiting = tuple(memory_admit.WaitingStep(index, session, name, "nextest", f"/tmp/tree-{index}", NOW, GIB,
            memory_admit.ReadyForAdmission()) for index, (session, name) in enumerate(identities, 1))
        running = tuple(memory_admit.RunningStep(session, name, "clippy", f"/tmp/tree-{index}", GIB, memory_admit.MeasuredMemory(GIB))
            for index, (session, name) in enumerate(identities, 1))
        text = self.render(memory_admit.QueueView(waiting, running, 20 * GIB, 0, GIB, NOW))
        for label in ("hana · organon · organon-ci88", "hana · merge · merge-gate", "hana · hana-showrunner",
                      "standalone", "offline · orphan", "recorded", "tree-7"):
            self.assertEqual(sum(label in row for row in text.splitlines() if "clippy" in row or "nextest" in row), 2)
        self.assertNotIn("clone88ci", text)
        self.assertNotIn("Roster:", text)

    def test_roster_problems_preserve_matches_and_report_only_the_first_problem_after_totals(self) -> None:
        self.labels_read.return_value = {"known":UnitLabels("live-name", "builds")}
        running = (memory_admit.RunningStep("known", "old", "mend", "/tmp/tree", GIB, memory_admit.MeasuredMemory(GIB)),
            memory_admit.RunningStep("unknown", "recorded", "doc", "/tmp/other", GIB, memory_admit.MeasuredMemory(GIB)))
        status, text, errors = self.invoke(queue_read_fixture(memory_admit.QueueView((), running, 20 * GIB, 0, GIB, NOW)))
        self.assertEqual(status, 0, errors)
        self.assertIn("builds · live-name", text)
        self.assertIn("recorded", text)
        self.assertNotIn("Roster:", text)

    def test_both_tables_use_the_shared_unit_labels_without_rewriting_names(self) -> None:
        self.labels_read.return_value = {"style-id":UnitLabels("style-impl", "cargo-liner", "cargo-liner-style-types")}
        waiting = memory_admit.WaitingStep(10, "style-id", "recorded-name", "nextest", "/tmp/style", NOW, GIB, memory_admit.ReadyForAdmission())
        running = memory_admit.RunningStep("style-id", "recorded-name", "clippy", "/tmp/style", GIB, memory_admit.MeasuredMemory(GIB))
        status, text, errors = self.invoke(queue_read_fixture(memory_admit.QueueView((waiting, ), (running, ), 20 * GIB, 0, GIB, NOW)), "--columns", "140")
        self.assertEqual(status, 0, errors)
        self.assertEqual(text.count("cargo-liner · cargo-liner-style-types · style-impl"), 2)
        self.labels_read.assert_called_once_with(["style-id", "style-id"], os.environ)
        self.assertNotIn("recorded-name", text)

    def test_failed_label_files_preserve_both_steps_with_fallback_names(self) -> None:
        waiting = memory_admit.WaitingStep(10, "missing", "recorded", "nextest", "/tmp/tree", NOW, GIB, memory_admit.ReadyForAdmission())
        running = memory_admit.RunningStep("also-missing", "", "doc", "/tmp/folder", GIB, memory_admit.MeasuredMemory(GIB))
        reading = queue_read_fixture(memory_admit.QueueView((waiting, ), (running, ), 20 * GIB, 0, GIB, NOW))
        with mock.patch.object(unit_labels, "unit_labels_for", unit_labels_for), \
             mock.patch.object(unit_labels, "newest_sessions", side_effect=OSError("session files unavailable")) as sessions:
            status, text, errors = self.invoke(reading)
        sessions.assert_called_once_with()
        self.assertEqual(status, 0, errors)
        self.assertEqual(errors, "")
        self.assertIn("recorded", text)
        self.assertIn("folder", text)
        self.assertNotIn("Roster:", text)

    def test_queue_and_local_roster_reads_overlap(self) -> None:
        with QueueFixture() as fixture:
            reading = fixture.read()
            expected_ids = [step.session_id for step in (*reading.view.running, *reading.view.waiting)]
            expected_ids.extend(["id-widget", "id-widget-director", "id-work-lead"])
            events:list[str] = []
            def read() -> memory_admit.QueueRead:
                events.append("queue")
                return reading
            def labels(ids:list[str], environment:Mapping[str, str]) -> dict[str, UnitLabels]:
                events.append("labels")
                self.assertIn("id-widget", ids)
                self.assertIs(environment, os.environ)
                return fixture.labels
            self.labels_read.side_effect = labels
            output = io.StringIO()
            with mock.patch.object(memory_admit, "locked_queue_read", side_effect=read), \
                 mock.patch.object(roster, "roster", side_effect=AssertionError("no full roster")), \
                 mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
                self.assertEqual(build_queue.main(), 0)
        self.assertEqual(events, ["queue", "labels"])
        self.labels_read.assert_called_once_with(expected_ids, os.environ)

    def test_behind_head_uses_the_roster_session_label(self) -> None:
        self.labels_read.return_value = {"head-id":UnitLabels("organon-ci88")}
        first = memory_admit.WaitingStep(10, "head-id", "clone88ci", "nextest", "/tmp/head", NOW - 2400, GIB, memory_admit.ReadyForAdmission())
        follower = memory_admit.WaitingStep(11, "follower-id", "follower", "doc", "/tmp/follower", NOW - 300, GIB,
            memory_admit.BehindHead("clone88ci", NOW - 2400, "head-id"))
        text = self.render(memory_admit.QueueView((first, follower), (), 20 * GIB, 0, GIB, NOW))
        self.assertIn("organon-ci88", text)
        self.assertIn("#2:", text)
        self.assertNotIn("clone88ci", text)

    def test_waiting_row_names_head_and_age_with_local_time(self) -> None:
        now = 1793214000.0
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "21"}):
            decision = memory_admit.decide(int(23.1 * GIB), 60 * GIB, int(10.9 * GIB),
                                           [memory_admit.MeasuredMemory(15 * GIB)])
        head = memory_admit.WaitingStep(
            10, "head-id", "clone88ci", "check", "/tmp/clone88", now - 2400,
            int(10.9 * GIB), memory_admit.head_wait_reason(decision, int(10.9 * GIB), int(23.1 * GIB),
                {"pid": 10, "start_time": "10", "need": int(10.9 * GIB), "since": now - 2400}, now),
        )
        follower = memory_admit.WaitingStep(
            11, "follower-id", "", "doc", "/tmp/tree", now - 300,
            int(11.6 * GIB), memory_admit.BehindHead(head_name="clone88ci", head_since=now - 2400, head_session_id="head-id"),
        )
        view = memory_admit.QueueView(
            (head, follower), (), int(23.1 * GIB), 0, 3 * GIB, now,
        )
        text = self.render(view)
        rows = waiting_rows(queue_view.render_view(self.model(queue_read_fixture(view)), 400))
        self.assertEqual(len(rows), 2)
        self.assertIn("40 min", rows[0])
        self.assertIn("10.9", rows[0])
        self.assertIn("clone88ci", rows[0])
        self.assertIn("5 min", rows[1])
        self.assertIn("11.6", rows[1])
        self.assertIn("tree", rows[1])
        self.assertIn("15.0 of 21.0 GiB held; needs 10.9; starts when held falls to 10.1", text)
        self.assertIn("free memory 23.1 GiB", text)
        self.assertNotIn("head-id", text)
        self.assertNotIn("follower-id", text)
        self.assertLess(text.index("Running"), text.index("Waiting ·"))

    def test_live_queue_pressure_reason_reaches_the_cli(self) -> None:
        write_process(self.fixture.proc, 100)
        self.fixture.write_json(self.fixture.directory / "100.waiting", {"pid":100, "start_time":"100", "need":8 * GIB,
            "since":NOW, "session_name":"unit", "step":"check", "folder":"/tree"})
        _ = (self.root / "pressure").write_text("full avg10=23.4 avg60=1.0 total=1\n")
        output = io.StringIO()
        with mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        self.assertIn("unit", output.getvalue())
        self.assertIn("memory pressure: full avg10 23.4", output.getvalue())
        self.assertNotIn("swap", output.getvalue())
        self.assertNotIn("ready", output.getvalue())

    def test_unavailable_reading_does_not_claim_zero_free_memory(self) -> None:
        first = memory_admit.WaitingStep(10, "session", "unit", "check", "/tmp/tree", NOW - 300, 12 * GIB, memory_admit.ReadingUnavailable())
        follower = replace(first, pid=11, session_id="follower", session_name="other", step="doc")
        text = self.render(memory_admit.QueueView((first, follower), (), 0, 0, 0, NOW))
        self.assertIn("memory reading unavailable", text)
        self.assertNotIn("free memory 0.0", text)
        self.assertNotIn("✓", text)

    def test_running_rows_show_signed_delta_and_totals(self) -> None:
        running = (memory_admit.RunningStep("session-id", "unit", "lint", "/tmp/tree", 8 * GIB, memory_admit.MeasuredMemory(int(1.2 * GIB))),
            memory_admit.RunningStep("other-id", "link", "build", "/tmp/link", 4 * GIB, memory_admit.MeasuredMemory(5 * GIB)))
        text = self.render(memory_admit.QueueView((), running, 20 * GIB, 8 * GIB, GIB, NOW))
        rows = [row for row in text.splitlines() if row.startswith("●")]
        self.assertEqual(len(rows), 2)
        self.assertIn("1.2", rows[0])
        self.assertIn("5.0", rows[1])
        self.assertIn("unit", rows[0])
        self.assertIn("link", rows[1])
        self.assertIn("Memory in GiB", text)

    def test_pending_and_ended_steps_do_not_report_zero_measured_use(self) -> None:
        pending = memory_admit.RunningStep("first", "", "check", "/one", 8 * GIB, memory_admit.PromisedMemory(8 * GIB))
        ended = memory_admit.RunningStep("second", "", "test", "/two", 12 * GIB, memory_admit.GoneMemory())
        text = self.render(memory_admit.QueueView((), (pending, ended), 20 * GIB, 8 * GIB, GIB, NOW))
        row = next(row for row in text.splitlines() if row.startswith("●"))
        self.assertIn("8.0", row)
        self.assertIn("—", row)
        self.assertNotIn("second", text)
        self.assertNotIn("two", text)
        self.assertNotIn("0.0", row)

    def test_empty_queue_reads_once_without_subprocess_or_wait(self) -> None:
        reading = queue_read_fixture(memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW))
        output = io.StringIO()
        with mock.patch.object(memory_admit, "locked_queue_read", return_value=reading) as read, \
             mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             mock.patch("time.sleep", side_effect=AssertionError("no wait")), \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        self.labels_read.assert_called_once_with([], os.environ)
        self.assertIn("0 running · 0 waiting · nothing waits", output.getvalue())
        self.assertNotIn("|", output.getvalue())

    def test_one_row_per_table_uses_names_and_escapes_cell_text(self) -> None:
        waiting = memory_admit.WaitingStep(10, "hidden-id", "", "doc", "/tmp/hana/", NOW - 60, GIB, memory_admit.ReadyForAdmission())
        running = memory_admit.RunningStep("other-hidden-id", "lfo|trunk\nunit", "check", "/tmp/lfo", 2 * GIB, memory_admit.MeasuredMemory(GIB))
        text = self.render(memory_admit.QueueView((waiting, ), (running, ), 20 * GIB, GIB, GIB, NOW))
        self.assertIn("lfo|trunk unit", text)
        self.assertIn("hana", text)
        self.assertEqual(sum(row.startswith("●") for row in text.splitlines()), 1)
        self.assertNotIn("hidden-id", text)

    def test_ten_thousand_waiting_and_running_rows_render_in_under_one_second(self) -> None:
        waiting = tuple(memory_admit.WaitingStep(pid, f"session-{pid}", "", "check", f"/tmp/tree-{pid}", NOW - pid,
            12 * GIB, memory_admit.NeedsMemory(12 * GIB, 20 * GIB)) for pid in range(10000))
        running = tuple(memory_admit.RunningStep(f"session-{pid}", "", "test", f"/tmp/tree-{pid}", 8 * GIB,
            memory_admit.MeasuredMemory(GIB)) for pid in range(10000))
        model = self.model(queue_read_fixture(memory_admit.QueueView(waiting, running, 20 * GIB, 10 * GIB, GIB, NOW)))
        started = time.perf_counter()
        lines = queue_view.render_view(model, 100)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(waiting_rows(lines)), 10000)
        self.assertEqual(sum(plain([line]).startswith("●") for line in lines), 10000)
        self.assertEqual(sum("tree-9999" in plain([line]) for line in lines), 2)
        self.assertLess(elapsed, 1., f"render took {elapsed:.6f} s")
        print(f"queue render: 20000 rows in {elapsed:.6f} s")

    def test_head_reason_keeps_required_free_threshold_when_both_limits_hold(self) -> None:
        need, available = int(10.9 * GIB), 12 * GIB
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "21"}):
            decision = memory_admit.decide(available, 60 * GIB, need,
                                           [memory_admit.MeasuredMemory(int(16.1 * GIB))])
        waiting = memory_admit.WaitingStep(
            10, "head-id", "unit", "nextest", "/tmp/tree", NOW - 2400, need,
            memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW))
        view = memory_admit.QueueView((waiting, ), (), available, 0, 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("10.9", text)
        self.assertIn("16.1 of 21.0 GiB held", text)
        self.assertIn("starts when held falls to 10.1", text)
        self.assertIn("free memory 12.0 GiB; needs 13.9: this step 10.9 + earlyoom 3.0", text)

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
        view = memory_admit.QueueView((waiting, ), running, int(23.1 * GIB), pending, 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("free memory 23.1 GiB; needs 38.1: this step 11.6 + earlyoom 3.0 + promised not yet used 23.5", text)

    def test_memory_hold_above_step_need_names_every_threshold_part(self) -> None:
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "clippy", "/tmp/tree", NOW, 3 * GIB,
            memory_admit.NeedsMemory(
                3 * GIB, int(11.5 * GIB), threshold=6 * GIB + int(13.4 * GIB),
                growth=int(13.4 * GIB), reserve=3 * GIB),
        )
        view = memory_admit.QueueView((waiting, ), (), int(11.5 * GIB), int(13.4 * GIB), 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("free memory 11.5 GiB; needs 19.4: this step 3.0 + earlyoom 3.0 + promised not yet used 13.4", text)

    def test_head_free_threshold_names_step_growth_and_reserve(self) -> None:
        decision = memory_admit.decide(5 * GIB, 60 * GIB, 3 * GIB,
                                       [memory_admit.PromisedMemory(4 * GIB)])
        waiting = memory_admit.WaitingStep(
            10, "head-id", "unit", "clippy", "/tmp/tree", NOW - 2400, 3 * GIB,
            memory_admit.head_wait_reason(decision, 3 * GIB, 5 * GIB,
                {"pid": 10, "start_time": "10", "need": 3 * GIB, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting, ), (), 5 * GIB, 4 * GIB, 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("free memory 5.0 GiB; needs 10.0: this step 3.0 + earlyoom 3.0 + promised not yet used 4.0", text)

    def test_budget_reason_names_charged_builds_and_the_limit(self) -> None:
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "check", "/tmp/tree", NOW - 300,
            int(10.8 * GIB), memory_admit.NeedsBuildBudget(int(10.8 * GIB), int(17.6 * GIB), 20 * GIB),
        )
        view = memory_admit.QueueView((waiting, ), (), 30 * GIB, 0, 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("17.6 of 20.0 GiB held; needs 10.8", text)

    def test_head_reason_names_measured_budget_limit_and_start_level(self) -> None:
        need, available = int(15.1 * GIB), int(19.5 * GIB)
        memory: list[memory_admit.StepMemory] = [memory_admit.MeasuredMemory(int(1.32 * GIB)) for _ in range(10)]
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "20"}):
            decision = memory_admit.decide(available, 60 * GIB, need, memory)
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "nextest", "/tmp/tree", NOW - 2400,
            need, memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting, ), (), available, 0, 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("13.2 of 20.0 GiB held; needs 15.1; starts when held falls to 4.9", text)

    def test_head_reason_names_only_free_memory_when_budget_passes(self) -> None:
        need, available = int(15.1 * GIB), 18 * GIB
        decision = memory_admit.decide(available, 60 * GIB, need, [memory_admit.MeasuredMemory(GIB)])
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "nextest", "/tmp/tree", NOW - 2400,
            need, memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting, ), (), available, 0, 3 * GIB, NOW)
        text = self.render(view)
        self.assertIn("#1 waits on free memory", text)
        self.assertIn("free memory 18.0 GiB; needs 18.1: this step 15.1 + earlyoom 3.0", text)
        self.assertIn("✓ build budget", text)

    def test_free_memory_head_uses_the_existing_decision_threshold_and_available_reading(self) -> None:
        need, available = 8 * GIB, 7 * GIB
        decision = memory_admit.decide(available, 60 * GIB, need, [])
        row: memory_admit.Waiter = {"pid": 10, "start_time": "10", "need": need, "since": NOW - 300}
        head = memory_admit.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], need,
                                       memory_admit.head_wait_reason(decision, need, available, row, NOW))
        view = memory_admit.QueueView((head, ), (), available, 0, 3 * GIB, NOW)
        with mock.patch.object(memory_admit, "decide", side_effect=AssertionError("one decision")):
            text = self.render(view)
        self.assertIn("free memory 7.0 GiB; needs 11.0: this step 8.0 + earlyoom 3.0", text)

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
        text = self.render(memory_admit.QueueView((head, ), (running, ), available, 0, 3 * GIB, NOW))
        self.assertIn("#1 waits on memory pressure, budget", text)
        self.assertIn("memory pressure: full avg10 23.4", text)
        self.assertIn("19.0 of 20.0 GiB held; needs 8.0", text)

    def test_memory_reason_omits_parts_that_round_to_zero(self) -> None:
        for growth, reserve, present in (
            (0, 3 * GIB, {"earlyoom"}),
            (1, 1, set[str]()),
            (0, 0, set[str]()),
            (3 * GIB, 3 * GIB, {"earlyoom", "promised not yet used"}),
        ):
            with self.subTest(growth=growth, reserve=reserve):
                need = 0 if (growth, reserve) == (0, 0) else 3 * GIB
                step = memory_admit.WaitingStep(10, "waiting-id", "unit", "clippy", "/tmp/tree", NOW, need,
                    memory_admit.NeedsMemory(need, 0, threshold=need + growth + reserve,
                        growth=growth, reserve=reserve))
                text = self.render(memory_admit.QueueView((step, ), (), 0, growth, reserve, NOW))
                self.assertIn(f"this step {need / GIB:.1f}", text)
                for label, amount in (("earlyoom", reserve), ("promised not yet used", growth)):
                    part = f"{label} {amount / GIB:.1f}"
                    if label in present:
                        self.assertIn(part, text)
                    else:
                        self.assertNotIn(part, text)

    def test_head_backfill_label_uses_the_since_zone_and_becomes_strict_at_ten_minutes(self) -> None:
        now = 1793214000.0
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
                                           "since": now - waited, "bump_order": bump}
                waiting = memory_admit.WaitingStep(
                    10, "waiting-id", "unit", "nextest", "/tmp/tree", row["since"], need,
                    memory_admit.head_wait_reason(decision, need, available, row, now))
                follower = replace(waiting, pid=11, reason=memory_admit.BehindHead("unit", row["since"], "waiting-id"))
                view = memory_admit.QueueView((waiting, follower), (), available, 0, 3 * GIB, now)
                reading = queue_read_fixture(view)
                head = reading.explanation.head
                if not isinstance(head, memory_admit.HeadExplanation):
                    self.fail("fixture needs a head")
                reading = replace(reading, explanation=replace(reading.explanation,
                    head=replace(head, has_priority=True)))
                model = self.model(reading)
                line = plain(queue_card.render_card(model, [2], 400))
                self.assertIn(suffix, line)
                head_checks = plain(queue_view.check_lines(head, model))
                self.assertIn(
                    f"free memory 18.0 GiB; needs {need_gib + 3:.1f}: this step {need_gib:.1f} + earlyoom 3.0",
                    head_checks)
                if need_gib >= 21:
                    self.assertIn(
                        f"build budget: 1.0 of 21.0 GiB held; needs {need_gib:.1f}; starts when held falls to {21 - need_gib:.1f}",
                        head_checks)

    def test_view_and_card_use_one_label_read_and_never_wait_for_a_full_roster(self) -> None:
        with QueueFixture() as fixture:
            reading = fixture.read()
            write_bumps(NOW, [*fixture.moves, bump_line(NOW - 5, actor="actor-only", session="bump-only")])
            write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="id-work-lead")])
            def slow_roster(*args:object, **kwargs:object) -> roster.Roster:
                self.assertTrue(args or kwargs)
                time.sleep(5)
                return roster.Roster([], [])
            seen: list[tuple[list[str], Mapping[str, str]]] = []
            def lookup(ids: list[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
                seen.append((ids, environment))
                return fixture.labels
            self.labels_read.side_effect = lookup
            expected = {step.session_id for step in (*reading.view.waiting, *reading.view.running)} | {"actor-only", "bump-only", "id-widget-director"}
            for args in ((), ("4", )):
                self.labels_read.reset_mock()
                seen.clear()
                with self.subTest(args=args), mock.patch.object(roster, "roster", side_effect=slow_roster) as full:
                    started = time.perf_counter()
                    status, text, errors = self.invoke(reading, *args)
                    elapsed = time.perf_counter() - started
                    self.assertEqual(status, 0, errors)
                    self.assertLess(elapsed, 1.0, f"queue {args} took {elapsed:.6f}s")
                    self.labels_read.assert_called_once()
                    ids, environment = seen[0]
                    self.assertEqual(set(ids), expected)
                    self.assertIs(environment, os.environ)
                    full.assert_not_called()
                    self.assertIn("#4 of 32" if args else "32 waiting", text)

    def test_explicit_columns_override_environment_for_text_and_json(self) -> None:
        reading = queue_read_fixture(memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW))
        with mock.patch.dict(os.environ, {"COLUMNS":"80"}):
            for args, expected in ((["--json"], 80), (["--json", "--columns", "60"], 60)):
                status, text, errors = self.invoke(reading, *args)
                self.assertEqual(status, 0, errors)
                data = cast(QueueLines, json.loads(text))
                self.assertEqual(data["columns"], expected)
                self.assertTrue(all(terminal_width("".join(span["text"] for span in row)) <= expected for row in data["lines"]))

    def test_default_columns_use_positive_environment_then_tty_then_one_hundred(self) -> None:
        output = io.StringIO()
        with mock.patch("sys.stdout", output), mock.patch.object(output, "fileno", return_value=7), \
             mock.patch.object(os, "get_terminal_size", return_value=os.terminal_size((89, 24))):
            for value in ("bad", "0", "-4", ""):
                with self.subTest(value=value), mock.patch.dict(os.environ, {"COLUMNS":value}):
                    with mock.patch.object(output, "isatty", return_value=True):
                        self.assertEqual(build_queue.default_columns(), 89)
                    with mock.patch.object(output, "isatty", return_value=False):
                        self.assertEqual(build_queue.default_columns(), 100)
            with mock.patch.dict(os.environ, {"COLUMNS":"140"}):
                self.assertEqual(build_queue.default_columns(), 140)

    def test_cli_card_no_match_succeeds_without_a_bump(self) -> None:
        reading = queue_read_fixture(memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW))
        with mock.patch.object(memory_admit, "bump_waiter") as bump:
            status, text, errors = self.invoke(reading, "nobody")
        self.assertEqual((status, text, errors), (0, "No waiting or running step matches nobody.\n", ""))
        bump.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
