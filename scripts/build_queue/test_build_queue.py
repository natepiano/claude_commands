"""The queue CLI uses one admission reading, file-only labels and shared span renders."""
from __future__ import annotations

from scripts.lint.memory_test_support import admission_policy_fixture

from scripts.build_queue.admission_test_support import queue_decision, AdmissionFixture, Exchange, ScriptedTransport, TimedChunk
from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records

from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Callable, Mapping
from dataclasses import replace
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path
import unittest
from unittest import mock
from typing import Protocol, cast, override
from zoneinfo import ZoneInfo

from scripts.build_queue import build_queue, head_facts, queue_card, queue_evidence, queue_view
from scripts.build_queue.queue_fixture import (
    NOW, ZONE, GOLDEN, QueueFixture, bump_line, head_reading, queue_read_fixture,
    queue_time_line, token_gap_line, waiting_rows, write_bumps, write_process, write_token_gaps,
)
from scripts.build_queue.queue_lines import QueueLines, Span, json_form, plain
from scripts.lib.terminal_cells import terminal_width
from scripts.lib.build_steps import read_step_names
from scripts.buildlog import store
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase, write_idle_alert
from scripts.lint import memory_admit
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.lint.memory_test_support import commit_config_fixture
from scripts.message import roster, unit_labels
from scripts.message.unit_labels import UnitLabels, unit_labels_for
from scripts.production import staff

GIB = records.GIB
CHECK_POLICY = admission_policy_fixture()




class LabelReadMock(Protocol):
    return_value: dict[str, UnitLabels]
    side_effect: Callable[[list[str], Mapping[str, str]], dict[str, UnitLabels]]

    def assert_called_once(self) -> None: ...
    def assert_called_once_with(self, ids: list[str], environment: Mapping[str, str]) -> None: ...
    def reset_mock(self) -> None: ...


class QueueTests(NotificationGuardedTestCase):
    fixture: QueueFixture = cast(QueueFixture, cast(object, None))
    root: Path = Path()
    labels_read: LabelReadMock = cast(LabelReadMock, cast(object, None))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fixture = self.enterContext(QueueFixture(captured=False))
        self.root = self.fixture.root
        _ = cast(object, self.enterContext(mock.patch.object(memory_admit, "queue_read", side_effect=self.fixture.read)))
        self.labels_read = cast(LabelReadMock, self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={})))
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "100", "CONF_DIR": str(config.parent)}))
        self.enterContext(mock.patch.dict(os.environ, {"MAC_IDLE_STATE_DIR": str(self.root / "mac-idle")}))

    @staticmethod
    def entry(session: str, name: str) -> tuple[str, UnitLabels]:
        return session, UnitLabels(name)

    def model(self, reading: records.QueueRead, zone: ZoneInfo = ZONE,
              *, selected: bool = False) -> queue_view.QueueModel:
        return queue_view.QueueModel(reading, self.labels_read.return_value,
            head_facts.read_head_facts(reading) if selected else records.NoWaiter(),
            head_facts.read_pace(reading.explanation.now) if selected else head_facts.PaceUnavailable(), zone,
            build_queue.read_host_load() if selected else queue_view.HostLoadUnavailable(),
            memory_admit.smallest_head_release(reading.explanation) if selected else records.NoWaiter())

    def render(self, view: records.QueueView, columns: int = 400) -> str:
        return plain(queue_view.render_view(self.model(queue_read_fixture(view, policy=admission_policy_fixture())), columns))

    def checks_model(self, view: records.QueueView, policy: records.AdmissionPolicy) -> queue_view.QueueModel:
        return self.model(queue_read_fixture(view, policy))

    def render_checks(self, model: queue_view.QueueModel) -> str:
        checks = queue_view.step_checks(model, 1)
        lines = queue_view.check_lines(checks, model)
        if not isinstance(checks, records.ReadingUnavailable):
            lines.insert(0, queue_view.check_line(*queue_view.pressure_check(checks.decision.pressure, model)))
        return plain(lines)

    def checks(self, view: records.QueueView, policy: records.AdmissionPolicy = CHECK_POLICY) -> str:
        return self.render_checks(self.checks_model(view, policy))

    def invoke(self, reading: records.QueueRead,
               *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(memory_admit, "queue_read", return_value=reading) as read, \
             mock.patch.object(admission_client, "mine", return_value=reading) as mine, \
             mock.patch("sys.argv", ["build_queue.py", *arguments]), \
             redirect_stdout(output), redirect_stderr(errors):
            status = build_queue.main()
        if "mine" in arguments:
            mine.assert_called_once_with(build_queue.caller())
            read.assert_not_called()
        else:
            read.assert_called_once_with()
            mine.assert_not_called()
        return status, output.getvalue(), errors.getvalue()

    def test_cli_keeps_running_grants_above_numbered_waiters(self) -> None:
        with QueueFixture() as fixture:
            reading = fixture.read()
            measured = reading.view.running[0].measured
            assert isinstance(measured, records.MeasuredMemory)
            grants = tuple(replace(reading.view.running[0], session_id=f"gate-{index}",
                step="merge-gate", promise=6 * GIB, unit="queue-unit", pid=900 + index,
                measured=replace(measured, anonymous_source=records.ScopeSource(
                    fixture.root / f"gate-{index}.scope", frozenset({900 + index})))) for index in range(3))
            reading = queue_read_fixture(replace(reading.view, running=grants), policy=admission_policy_fixture())
            self.labels_read.return_value = {**fixture.labels,
                **{step.session_id: UnitLabels("gate", "builds", "wrong-unit") for step in grants}}
            text = self.snapshot_text(self.snapshot(reading))
            rows = text.splitlines()
            running = [line for line in rows if line.lstrip().startswith("run")]
            self.assertEqual(len(running), 3)
            self.assertTrue(all("builds" in row and "queue-unit" in row and read_step_names().shown("merge-gate") in row for row in running))
            self.assertTrue(all("0.9 / 5.1" in row for row in running))
            positions = re.findall(r"^\s*(\d+)[ ▸]", text, re.MULTILINE)
            self.assertEqual(positions, [str(index) for index in range(1, 33)])
            self.assertLess(text.index(running[-1]), text.index(next(line for line in rows if line.lstrip().startswith("1 "))))

    def test_queue_command_warns_about_a_missing_key_once_per_process(self) -> None:
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        commit_config_fixture(config)
        write_memory_config(config, estimate_refresh_seconds=None)
        with AdmissionFixture() as admission:
            with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}):
                _ = admission.server.run_pass()
            with admission.serving(), \
                 mock.patch.object(memory_admit, "queue_read", side_effect=admission_client.view), \
                 mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                 mock.patch("sys.argv", ["build_queue.py"]), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_queue.main(), 0)
                self.assertEqual(build_queue.main(), 0)
            self.assertEqual(errors.getvalue().count(
                "estimate_refresh_seconds='<missing>'"), 1)

    def snapshot(self, reading: records.QueueRead, *arguments: str) -> QueueLines:
        status, output, errors = self.invoke(reading, "--json", "--columns", "400", *arguments)
        self.assertEqual(status, 0, errors)
        self.assertEqual(errors, "")
        return cast(QueueLines, json.loads(output))

    @staticmethod
    def snapshot_text(data: QueueLines) -> str:
        return "\n".join("".join(span["text"] for span in row) for row in data["lines"])

    def test_terminal_and_pane_share_the_recorded_complete_page(self) -> None:
        fixture = self.enterContext(QueueFixture())
        reading = fixture.read()
        self.labels_read.return_value = fixture.labels
        status, output, errors = self.invoke(reading, "--json", "--columns", "100", "1")
        self.assertEqual((status, errors), (0, ""))
        recorded = cast(QueueLines, json.loads(output))
        status, terminal, errors = self.invoke(reading, "--columns", "100", "1")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(terminal, (GOLDEN / "card_1.txt").read_text())
        def cells(text: str) -> str:
            return text.translate(str.maketrans({glyph: "█" for glyph in "░·▒▏▎▍▌▋▊▉"}))
        self.assertEqual(cells(self.snapshot_text(recorded)), cells(terminal.rstrip("\n")))
        self.assertIn("Frees when:", self.snapshot_text(recorded))

    def test_head_line_uses_singular_for_one_recorded_bump(self) -> None:
        write_bumps(NOW, [bump_line(NOW - 1)])
        facts = head_facts.read_head_facts(head_reading(since=NOW - 600, now=NOW))
        assert isinstance(facts, head_facts.HeadFacts)
        text = head_facts.bump_text(facts, ZoneInfo("UTC"), short=True, names=read_step_names())
        self.assertIn("1 bump since it joined, most: bumped check (1)", text)
        self.assertNotIn("1 bumps", text)


    def test_queue_help_starts_without_pythonpath(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment["MAC_IDLE_STATE_DIR"] = str(self.root / "mac-idle")
        for name in ("build_queue.py",):
            with self.subTest(script=name):
                result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).with_name(name)), "--help"],
                    env=environment, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mac_idle_alert_precedes_queue_and_selection_in_text_and_json(self) -> None:
        _ = write_idle_alert(self.root / "mac-idle", NOW)
        reading = head_reading(now=NOW)
        expected = ("MAC IDLE 10 min: 7 steps wait for the Mac. Blocked by research since "
                    + time.strftime("%H:%M", time.gmtime(NOW - 600))
                    + " (P15 F11 L0 run); /build:mac unblock clears it.")
        with mock.patch.object(time, "time", return_value=NOW), \
             mock.patch.object(build_queue, "user_zone", return_value=ZoneInfo("UTC")):
            for arguments in ((), ("1",)):
                with self.subTest(arguments=arguments):
                    code, output, errors = self.invoke(reading, *arguments)
                    self.assertEqual((code, errors), (0, ""))
                    self.assertEqual(output.splitlines()[0], expected)
                    data = self.snapshot(reading, *arguments)
                    self.assertEqual(data["lines"][0], [{"text": expected, "tone": "error", "bold": True}])
                    self.assertGreater(len(data["lines"]), 1)

    def test_mac_idle_alert_read_does_not_mask_admission_failure(self) -> None:
        _ = write_idle_alert(self.root / "mac-idle", NOW)
        with mock.patch.object(memory_admit, "queue_read", side_effect=wire.AdmissionUnavailable("fixture unavailable")), \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(build_queue.main(), 2)
        self.assertIn("admission service unavailable: fixture unavailable", errors.getvalue())

    def test_stale_mac_reading_precedes_queue(self) -> None:
        _ = write_idle_alert(self.root / "mac-idle", NOW, written_age=301)
        with mock.patch.object(time, "time", return_value=NOW):
            data = self.snapshot(head_reading(now=NOW))
        first = data["lines"][0][0]
        self.assertTrue(first["text"].startswith("MAC IDLE WATCH: no reading since "))
        self.assertTrue(first["text"].endswith("; check buildlog-sample.timer."))
        self.assertEqual(first.get("tone"), "error")

    def test_alert_fixture_read_stays_local_and_finishes_under_50_ms(self) -> None:
        from scripts.buildlog import mac_idle_alert

        path = write_idle_alert(self.root / "mac-idle", NOW)
        with mock.patch("subprocess.run", side_effect=AssertionError("alert read starts a process")):
            started = time.perf_counter()
            line = mac_idle_alert.alert_line(path, NOW, ZoneInfo("UTC"))
            elapsed = time.perf_counter() - started
        self.assertIsNotNone(line)
        self.assertIn("Blocked by research", line or "")
        self.assertLess(elapsed, 0.05)


    def test_expired_head_json_paints_the_same_sixty_column_error_as_inline(self) -> None:
        _ = self.fixture.waiter(500, "id-expired", "expired")
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        path, row = inputs.ordered[0]
        reading = memory_admit.queue_read_from(replace(inputs, pressure=records.NoPressureHold(),
            ordered=[(path, {**row, "estimated_at": NOW - 600})]), policy=admission_policy_fixture())
        status, output, errors = self.invoke(reading, "--json", "--columns", "60")
        self.assertEqual((status, errors), (0, ""))
        data = cast(QueueLines, json.loads(output))
        pane = self.snapshot_text(data)
        status, inline, errors = self.invoke(reading, "--columns", "60")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("│", inline)
        bar_cells = str.maketrans({glyph: "█" for glyph in "░·▒"})
        expected: list[str] = []
        for line in inline.rstrip("\n").splitlines():
            if line.startswith("Build queue"):
                expected.append(line)
            elif line.startswith("· "):
                expected.append("· " + line[2:].translate(bar_cells))
            else:
                expected.append(line.translate(bar_cells))
        self.assertEqual(pane, "\n".join(expected))
        self.assertIn("estimate expired", pane)
        self.assertTrue(all(terminal_width(line) <= 60 for line in pane.splitlines()))
        self.assertTrue(any(span.get("tone") == "error" and "estimate expired" in span["text"]
                            for line in data["lines"] for span in line))



    def test_bump_from_any_session_records_the_actor(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        admission = self.private_mutation_service()
        labels = {"worker-id": UnitLabels("worker", unit="berth")}
        self.labels_read.return_value = labels
        for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                      ("", "codex-session", "codex-session"), ("", "", "user")):
            with self.subTest(actor=actor), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}), \
                 mock.patch.object(memory_admit, "priority_labels", return_value=labels):
                self.assertEqual(self.bump_output("2" if actor == "ordinary-session" else "1")[0], 0)
                priority = admission.server.priority
                self.assertNotIsInstance(priority, records.NoQueuePriority)
                if not isinstance(priority, records.NoQueuePriority):
                    self.assertEqual(priority["set_by"], actor)
                events = [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                          for line in path.read_text().splitlines() if '"queue_priority"' in line]
                self.assertEqual(events[-1]["set_by"], actor)

    def test_bump_passes_a_dash_unit_as_one_argparse_value(self) -> None:
        with mock.patch.object(records, "queue_command_actor", return_value="showrunner"), \
             mock.patch.object(build_queue, "bump", return_value=0) as run:
            self.assertEqual(self.bump_output("-x"), (0, "", ""))
        run.assert_called_once_with("-x", "showrunner")

    def test_bump_sets_the_units_priority_and_reports_its_resolved_place(self) -> None:
        first = self.fixture.waiter(100, "head-id", "head")
        second = self.fixture.waiter(200, "worker-id", "worker")
        before = (first.read_bytes(), second.read_bytes())
        admission = self.private_mutation_service()
        with mock.patch.object(memory_admit, "priority_labels", return_value={"worker-id": UnitLabels("worker", unit="berth")}):
            self.labels_read.return_value = {"worker-id": UnitLabels("worker", unit="berth")}
            status, output, errors = self.bump_output("2")
            self.assertEqual([step.pid for step in memory_admit.queue_view().waiting], [200, 100])
        self.assertEqual((status, output, errors), (0, "queue priority set for berth (step #2): first step now #1\n", ""))
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)
        priority = admission.server.priority
        self.assertNotIsInstance(priority, records.NoQueuePriority)
        if not isinstance(priority, records.NoQueuePriority):
            self.assertEqual((priority["unit"], priority["why"]), ("berth", "the user's bump"))

    def test_unmatched_bump_selector_refuses_without_writing_priority(self) -> None:
        admission = self.private_mutation_service()
        with mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("unmatched priority write")):
            status, output, errors = self.bump_output("berth")
        self.assertEqual((status, output, errors), (3, "build queue: no waiting step matches berth\n", ""))
        self.assertIsInstance(admission.server.priority, records.NoQueuePriority)

    def test_bump_requires_exactly_one_selector_without_running_priority(self) -> None:
        for selectors in ((), ("berth", "other"), ("two words",)):
            with self.subTest(selectors=selectors), \
                 mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("usage ran priority")):
                self.assertEqual(self.bump_output(*selectors),
                    (3, "build queue: use /build:queue bump <position|session|folder>\n", ""))

    def test_bump_selectors_send_the_selected_rows_real_unit_and_actor(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        self.labels_read.return_value = {"worker-id": UnitLabels("worker", unit="actual-unit")}
        for selector in ("2", "worker-id", "worker", "/tmp/tree-200", "tree-200"):
            with self.subTest(selector=selector), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "actor-id"}), \
                 mock.patch.object(admission_client, "priority_set", return_value=wire.Ok()) as mutation:
                self.assertEqual(self.bump_output(selector),
                    (0, "queue priority set for actual-unit (step #2): first step now #2\n", ""))
                mutation.assert_called_once_with("actual-unit", "the user's bump", "actor-id")

    def test_bump_row_without_a_unit_refuses_without_writing_priority(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        self.labels_read.return_value = {}
        with mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("unitless priority write")):
            self.assertEqual(self.bump_output("1"), (3, "build queue: waiting step #1 has no unit\n", ""))

    def test_admit_head_and_clear_from_any_session_send_the_actor(self) -> None:
        for arguments in (("admit-head", "urgent"), ("clear",)):
            for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                          ("", "codex-session", "codex-session"), ("", "", "user")):
                with self.subTest(arguments=arguments, actor=actor), \
                     mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}), \
                     mock.patch.object(admission_client, "admit_head", return_value=wire.Ok()) as head, \
                     mock.patch.object(admission_client, "priority_clear", return_value=wire.Ok()) as clear, \
                     mock.patch("sys.argv", ["build_queue.py", *arguments]), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(build_queue.main(), 0)
                    if arguments[0] == "clear":
                        clear.assert_called_once_with(actor)
                        head.assert_not_called()
                    else:
                        head.assert_called_once_with("urgent", actor)
                        clear.assert_not_called()

    def private_mutation_service(self) -> AdmissionFixture:
        files = {path: path.read_bytes() for path in self.fixture.directory.glob("*.waiting")}
        admission = self.enterContext(AdmissionFixture(root=self.root, log_dir=self.root / "buildlog"))
        for path, contents in files.items():
            _ = path.write_bytes(contents)
        admission.clock.epoch = NOW
        admission.free(0)
        admission.hold_starts()
        for path in self.fixture.directory.glob("*.waiting"):
            row = memory_admit.read_waiter(path)
            owner = admission.fixture_owner(row["pid"], row["start_time"])
            request = admission.request(owner=owner, need=row["need"], since=row["since"])
            request = replace(request, identity=wire.Identity(row.get("session_id", ""),
                row.get("session_name", ""), row.get("step", ""), row.get("folder", "")))
            _ = admission.server.handle_request(request)
        _ = self.enterContext(admission.serving())
        _ = self.enterContext(mock.patch.object(memory_admit, "queue_read", side_effect=admission_client.view))
        return admission

    def bump_output(self, *units: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["build_queue.py", "bump", *units]), \
             redirect_stdout(output), redirect_stderr(errors):
            status = build_queue.main()
        return status, output.getvalue(), errors.getvalue()



    def test_queue_read_failures_exit_two_with_diagnostics_only_on_stderr(self) -> None:
        for error in (PermissionError(13, "Permission denied", "/private/admission/lock"),
                      ValueError("queue reading invalid")):
            with self.subTest(error=error):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(memory_admit, "queue_read", side_effect=error), \
                     mock.patch("sys.argv", ["build_queue.py"]), \
                     redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(build_queue.main(), 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), f"build queue: {error}\n")


    def test_head_and_card_share_the_same_read_and_never_recalculate_checks(self) -> None:
        from scripts.build_queue.test_queue_evidence import QueueEvidenceTests
        fixture = QueueEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", self.labels_read))
        reading = memory_admit.queue_read()
        evidence = plain(queue_card.render_selection(self.model(reading, selected=True), "1", 400))
        legacy = replace(reading.view.waiting[0], reason=records.ReadyForAdmission(), need=GIB)
        reading = replace(reading, view=replace(reading.view, available=0, waiting=(legacy, )))
        model = self.model(reading)
        with mock.patch.object(memory_admit, "admission_pass", side_effect=AssertionError("one decision")):
            text = plain(queue_view.check_lines(queue_view.step_checks(model, 1), model))
        for value in ("11.8", "0.0"):
            self.assertIn(value, text)
            self.assertIn(value, evidence)
        self.assertIn("Room: need 11.8 GiB > room 0.0 GiB: held", text)
        self.assertIn("Frees when:", evidence)
        with mock.patch.object(memory_admit, "queue_view", side_effect=AssertionError("no separate queue read")):
            data = self.snapshot(reading)
        self.assertEqual(self.snapshot_text(data), plain(queue_view.render_view(model, 400, colour=True)))

    def test_every_text_table_marks_numbers_right_and_text_left(self) -> None:
        with QueueFixture() as fixture:
            model = fixture.model()
            first = replace(model.reading.view.waiting[0], step="nextest")
            model = replace(model, reading=replace(model.reading, view=replace(model.reading.view,
                waiting=(first, *model.reading.view.waiting[1:]))))
            rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertEqual(len(rows), 32)
        self.assertEqual([row[:3] for row in rows[:10]], [f"{n:>3}" for n in range(1, 11)])
        self.assertEqual([row.index("nextest") for row in rows[:2]], [40, 40])
        self.assertTrue(all("| ---" not in row for row in rows))

    def test_head_line_short_forms_share_the_recorded_head_and_bump_facts(self) -> None:
        reading = head_reading(since=NOW - 158 * 60, now=NOW,
                              fields={"head_since": NOW - 125, "priority_since": NOW - 3600})
        write_bumps(NOW, [bump_line(NOW - 5, folder="/tmp/alpha"),
                          bump_line(NOW - 4, folder="/tmp/beta", step="clippy"),
                          bump_line(NOW - 3, folder="/tmp/alpha")])
        text = self.snapshot_text(self.snapshot(reading))
        self.assertNotIn("Head for:", text)
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

    def test_legacy_head_clock_is_ignored_in_view_card_and_json(self) -> None:
        reading = head_reading(since=NOW - 2400, now=NOW,
            fields={"head_state": "head", "head_since": NOW - 125})
        model = self.model(reading, selected=True)
        for lines in (queue_view.render_view(model, 400), queue_card.render_card(model, [1], 400)):
            text = plain(lines)
            self.assertIn("40", text)
            self.assertNotIn("became first", text)
            self.assertNotIn("head for", text.lower())
            self.assertNotIn("head_since", text)
        data = self.snapshot(reading)
        self.assertNotIn("head_since", json.dumps(data))
        self.assertIn("40", self.snapshot_text(data))


    def test_bump_read_failure_is_unknown_and_snapshot_keys_stay_unchanged(self) -> None:
        store.host_file(store.host_name(), NOW).parent.mkdir(parents=True, exist_ok=True)
        store.host_file(store.host_name(), NOW).mkdir()
        data = self.snapshot(head_reading(since=NOW - 600, now=NOW))
        self.assertEqual(set(data), {"columns", "as_of", "lines"})
        self.assertNotIn("bumps: unknown", self.snapshot_text(data))
        self.assertNotIn("no bumps since it joined", self.snapshot_text(data))
        self.assertTrue(all({"text"} <= set(span)
                            and set(span) <= {"text", "tone", "bold", "background", "until"}
                            for row in data["lines"] for span in row))

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
        self.assertNotIn("cargo token:", self.snapshot_text(data))
        facts = head_facts.read_head_facts(reading)
        assert isinstance(facts, head_facts.HeadFacts)
        self.assertEqual(head_facts.head_token_text(facts, ZONE), "cargo token: waited 154 s (10:42-10:45)")
        self.assertEqual(set(data), {"columns", "as_of", "lines"})

    def test_head_line_cargo_token_sum_excludes_other_waiting_steps(self) -> None:
        reading = head_reading(since=NOW - 1000, now=NOW)
        follower = replace(reading.view.waiting[0], pid=456, session_id="follower", step="clippy", since=NOW - 800)
        path, row = reading.ordered[0]
        follower_row: records.RankedWaiter = {**row, "pid": follower.pid,
            "start_time": str(follower.pid), "session_id": follower.session_id,
            "step": follower.step, "since": follower.since}
        reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, follower)),
                          ordered=[*reading.ordered, (path.with_name("456-456.waiting"), follower_row)])
        write_token_gaps([token_gap_line(NOW - 900, NOW - 800), token_gap_line(NOW - 700, NOW - 400,
            session="follower", step="clippy", pid=456), token_gap_line(NOW - 200, NOW - 46)])
        facts = head_facts.read_head_facts(reading)
        assert isinstance(facts, head_facts.HeadFacts)
        text = head_facts.head_token_text(facts, ZONE)
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
            records.RunningStep("worker-id", "old-name", "lint", "/tmp/tree", 8 * GIB,
                                    records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123),
            records.RunningStep("starting", "starting", "doc", "/tmp/docs", 2 * GIB,
                                    records.PromisedMemory(2 * GIB), pid=123),
            records.RunningStep("gone-session", "gone-session", "check", "/tmp/gone", GIB,
                                    records.GoneMemory(), pid=123),
        )
        waiting = records.WaitingStep(10, "worker-id", "old-name", "nextest", "/tmp/tree",
                                          NOW - 300, 12 * GIB, records.ReadyForAdmission())
        view = records.QueueView((waiting, ), running, 20 * GIB, 2 * GIB, 3 * GIB, NOW)
        reading = queue_read_fixture(view, policy=admission_policy_fixture())
        data = self.snapshot(reading)
        status, text, errors = self.invoke(reading, "--columns", "400")
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.snapshot_text(data), plain(queue_view.render_view(self.model(reading), 400, colour=True)))
        self.assertEqual(text.rstrip("\n"), plain(queue_view.render_view(self.model(reading), 400)))
        self.assertEqual(data["columns"], 400)
        self.assertEqual(data["as_of"], NOW)
        self.assertFalse(any(line.startswith("●") for line in text.splitlines()))
        self.assertIn("builds", text)
        self.assertIn("report", text)
        self.assertIn("cap", text)
        self.assertIn("12.0", text)
        self.assertIn("Room: need 12.0 GiB > room 7.0 GiB: held", self.checks(view))
        self.assertNotIn("gone-session", text)
        self.assertNotIn("Roster:", text)

    def test_json_empty_tables_are_arrays_and_do_not_invent_a_head(self) -> None:
        data = self.snapshot(queue_read_fixture(records.QueueView((), (), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture()))
        self.assertIsInstance(data["lines"], list)
        text = self.snapshot_text(data)
        self.assertIn("Build queue · 10:46", text)
        self.assertIn("cap 17.0", text)
        self.assertNotIn("#1 waits on", text)
        self.assertNotIn("■", text)
        self.assertNotIn("●", text)

    def test_fixture_json_stays_within_four_times_its_text_size(self) -> None:
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
        self.assertEqual(self.snapshot_text(data) + "\n",
                         plain(queue_view.render_view(self.model(reading), 100, colour=True)) + "\n")
        self.assertLessEqual(len(encoded.encode()), 4 * len(text.encode()))
        defaults = Span("")
        for row in data["lines"]:
            for span in row:
                if "tone" in span:
                    self.assertNotEqual(span["tone"], defaults.tone, span)
                if "background" in span:
                    self.assertNotEqual(span["background"], defaults.background, span)
                if "bold" in span:
                    self.assertNotEqual(span["bold"], defaults.bold, span)
            styles = [(span.get("tone", defaults.tone), span.get("background", defaults.background),
                       span.get("bold", defaults.bold)) for span in row]
            for left, right in zip(styles, styles[1:]):
                self.assertNotEqual(left, right, row)
        print(f"queue fixture size: text {len(text.encode())} bytes; JSON {len(encoded.encode())} bytes")




    def test_both_tables_resolve_workers_directors_showrunners_and_unmatched_sessions(self) -> None:
        identities = (("worker-id", "clone88ci"), ("director-id", "old-director"), ("showrunner-id", "old-showrunner"),
            ("standalone-id", "old-standalone"), ("orphan-id", "old-orphan"), ("missing-id", "recorded"), ("nameless-id", ""))
        self.labels_read.return_value = {
            "worker-id":UnitLabels("organon-ci88", "hana", "organon"),
            "director-id":UnitLabels("merge-gate", "hana", "merge"),
            "showrunner-id":UnitLabels("hana-showrunner", "hana"),
            "standalone-id":UnitLabels("standalone"), "orphan-id":UnitLabels("orphan", "offline")}
        waiting = tuple(records.WaitingStep(index, session, name, "nextest", f"/tmp/tree-{index}", NOW, GIB,
            records.ReadyForAdmission()) for index, (session, name) in enumerate(identities, 1))
        running = tuple(records.RunningStep(session, name, "clippy", f"/tmp/tree-{index}", GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=index)
            for index, (session, name) in enumerate(identities, 1))
        model = self.model(queue_read_fixture(records.QueueView(waiting, running, 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture()), selected=True)
        text = "\n".join("\n".join(line for line in
            plain(queue_card.render_selection(model, session, 400)).splitlines()
            if line.startswith("#") or line.startswith("Selected running step:"))
            for session, _ in identities)
        for label in ("hana · organon · organon-ci88", "hana · merge · merge-gate", "hana · hana-showrunner",
                      "standalone", "offline · orphan", "recorded", "tree-7"):
            cards = [row for row in text.splitlines() if row.startswith("#")]
            running_rows = [row for row in text.splitlines() if row.startswith("Selected running step:")]
            self.assertEqual(sum(label in row for row in cards), 1)
            session_label = label.split(" · ")[-1]
            self.assertEqual(sum(session_label in row for row in running_rows), 1)
        self.assertNotIn("clone88ci", text)
        self.assertNotIn("Roster:", text)

    def test_roster_problems_preserve_matches_and_report_only_the_first_problem_after_totals(self) -> None:
        self.labels_read.return_value = {"known":UnitLabels("live-name", "builds")}
        running = (records.RunningStep("known", "old", "mend", "/tmp/tree", GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123),
            records.RunningStep("unknown", "recorded", "doc", "/tmp/other", GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123))
        reading = queue_read_fixture(records.QueueView((), running, 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture())
        status, text, errors = self.invoke(reading, "known")
        other_status, other, other_errors = self.invoke(reading, "unknown")
        self.assertEqual((other_status, other_errors), (0, ""))
        text += other
        self.assertEqual(status, 0, errors)
        self.assertIn("builds / — / live-name", text)
        self.assertIn("recorded", text)
        self.assertNotIn("Roster:", text)

    def test_both_tables_use_the_shared_unit_labels_without_rewriting_names(self) -> None:
        self.labels_read.return_value = {"style-id":UnitLabels("style-impl", "cargo-liner", "cargo-liner-style-types")}
        waiting = records.WaitingStep(10, "style-id", "recorded-name", "nextest", "/tmp/style", NOW, GIB, records.ReadyForAdmission())
        running = records.RunningStep("style-id", "recorded-name", "clippy", "/tmp/style", GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123)
        status, text, errors = self.invoke(queue_read_fixture(records.QueueView((waiting, ), (running, ), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture()), "--columns", "140")
        self.assertEqual(status, 0, errors)
        self.assertIn("cargo-liner", text)
        self.assertIn("style-types", text)
        self.assertNotIn("cargo-liner-style-types", text)
        self.assertEqual(text.count("nextest"), 1)
        self.assertEqual(text.count("clippy"), 1)
        self.labels_read.assert_called_once_with(["style-id", "style-id"], os.environ)
        self.assertNotIn("recorded-name", text)

    def test_failed_label_files_preserve_both_steps_with_fallback_names(self) -> None:
        waiting = records.WaitingStep(10, "missing", "recorded", "nextest", "/tmp/tree", NOW, GIB, records.ReadyForAdmission())
        running = records.RunningStep("also-missing", "", "doc", "/tmp/folder", GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123)
        reading = queue_read_fixture(records.QueueView((waiting, ), (running, ), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture())
        with mock.patch.object(unit_labels, "unit_labels_for", unit_labels_for), \
             mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())), \
             mock.patch.object(unit_labels, "newest_sessions", side_effect=OSError("session files unavailable")) as sessions:
            status, text, errors = self.invoke(reading, "recorded")
            other_status, other, other_errors = self.invoke(reading, "folder")
        self.assertEqual(sessions.call_args_list, [mock.call(), mock.call()])
        self.assertEqual((other_status, other_errors), (0, ""))
        text += other
        self.assertEqual(status, 0, errors)
        self.assertEqual(errors, "")
        self.assertIn("recorded", text)
        self.assertIn("folder", text)
        self.assertNotIn("Roster:", text)

    def test_queue_and_local_roster_reads_overlap(self) -> None:
        with QueueFixture() as fixture:
            reading = fixture.read()
            expected_ids = [step.session_id for step in (*reading.view.running, *reading.view.waiting)]
            events:list[str] = []
            def read() -> records.QueueRead:
                events.append("queue")
                return reading
            def labels(ids:list[str], environment:Mapping[str, str]) -> dict[str, UnitLabels]:
                events.append("labels")
                self.assertIn("id-widget", ids)
                self.assertIs(environment, os.environ)
                return fixture.labels
            self.labels_read.side_effect = labels
            output = io.StringIO()
            with mock.patch.object(memory_admit, "queue_read", side_effect=read), \
                 mock.patch.object(roster, "roster", side_effect=AssertionError("no full roster")), \
                 mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
                self.assertEqual(build_queue.main(), 0)
        self.assertEqual(events, ["queue", "labels"])
        self.labels_read.assert_called_once_with(expected_ids, os.environ)

    def test_behind_head_uses_the_roster_session_label(self) -> None:
        self.labels_read.return_value = {"head-id":UnitLabels("organon-ci88")}
        first = records.WaitingStep(10, "head-id", "clone88ci", "nextest", "/tmp/head", NOW - 2400, GIB, records.ReadyForAdmission())
        follower = records.WaitingStep(11, "follower-id", "follower", "doc", "/tmp/follower", NOW - 300, GIB,
            records.BehindHead("clone88ci", NOW - 2400, "head-id"))
        model = self.model(queue_read_fixture(records.QueueView((first, follower), (), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture()), selected=True)
        text = plain(queue_card.render_card(model, [2], 400))
        self.assertIn("organon-ci88", text)
        self.assertIn("  #1 ", text)
        self.assertNotIn("clone88ci", text)

    def test_waiting_row_names_head_and_age_with_local_time(self) -> None:
        now = 1793214000.0
        decision = queue_decision(int(23.1 * GIB), 60 * GIB, int(10.9 * GIB), [records.MeasuredMemory(15 * GIB, anonymous=records.AnonymousRunUse(15 * GIB))])
        head = records.WaitingStep(
            10, "head-id", "clone88ci", "check", "/tmp/clone88", now - 2400,
            int(10.9 * GIB), records.HeadAdmissionStatus(decision, int(10.9 * GIB), int(23.1 * GIB),
                cause=records.ReadyForAdmission()),
        )
        follower = records.WaitingStep(
            11, "follower-id", "", "doc", "/tmp/tree", now - 300,
            int(11.6 * GIB), records.BehindHead(head_name="clone88ci", joined_at=now - 2400, head_session_id="head-id"),
        )
        view = records.QueueView(
            (head, follower), (), int(23.1 * GIB), 0, 3 * GIB, now,
        )
        self.labels_read.return_value = {"head-id": UnitLabels("clone88ci", "builds", "clone88ci"),
                                         "follower-id": UnitLabels("tree", "builds", "tree")}
        text = self.checks(view)
        rows = waiting_rows(queue_view.render_view(self.model(queue_read_fixture(view, policy=admission_policy_fixture())), 400))
        self.assertEqual(len(rows), 2)
        self.assertIn(" 40 ", rows[0])
        self.assertIn("10.9", rows[0])
        self.assertIn("clone88ci", rows[0])
        self.assertIn("  5 ", rows[1])
        self.assertIn("11.6", rows[1])
        self.assertIn("tree", rows[1])
        self.assertIn("Room: need 10.9 GiB <= room 20.1 GiB: pass", text)
        self.assertIn("Room: need 10.9 GiB", text)
        self.assertNotIn("head-id", text)
        self.assertNotIn("follower-id", text)
        self.assertEqual(sum(line.lstrip().startswith("# ") for line in self.render(view).splitlines()), 1)
        self.assertEqual(len(waiting_rows(queue_view.render_view(self.model(queue_read_fixture(view, policy=admission_policy_fixture())), 140))), 2)

    def test_live_queue_pressure_reason_reaches_the_cli(self) -> None:
        write_process(self.fixture.proc, 100)
        self.fixture.write_json(self.fixture.directory / "100.waiting", {"pid":100, "start_time":"100", "need":8 * GIB,
            "since":NOW, "session_name":"unit", "step":"check", "folder":"/tree"})
        _ = (self.root / "pressure").write_text("full avg10=23.4 avg60=1.0 total=1\n")
        output = io.StringIO()
        with mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        self.assertIn("check", output.getvalue())
        self.assertIn("pressure", output.getvalue())
        self.assertNotIn("swap", output.getvalue())
        self.assertNotIn("ready", output.getvalue())

    def test_unavailable_reading_does_not_claim_zero_free_memory(self) -> None:
        first = records.WaitingStep(10, "session", "unit", "check", "/tmp/tree", NOW - 300, 12 * GIB, records.ReadingUnavailable())
        follower = replace(first, pid=11, session_id="follower", session_name="other", step="doc")
        text = self.checks(records.QueueView((first, follower), (), 0, 0, 0, NOW))
        self.assertIn("memory reading unavailable", text)
        self.assertNotIn("Free memory: free 0.0", text)
        self.assertNotIn("✓", text)

    def test_running_records_show_promise_use_and_totals(self) -> None:
        running = (records.RunningStep("session-id", "unit", "lint", "/tmp/tree", 8 * GIB, records.MeasuredMemory(int(1.2 * GIB), anonymous=records.AnonymousRunUse(int(1.2 * GIB))), pid=123),
            records.RunningStep("other-id", "link", "build", "/tmp/link", 4 * GIB, records.MeasuredMemory(5 * GIB, anonymous=records.AnonymousRunUse(5 * GIB)), pid=123))
        view = records.QueueView((), running, 20 * GIB, 8 * GIB, GIB, NOW)
        model = self.model(queue_read_fixture(view, policy=admission_policy_fixture()), selected=True)
        text = "\n".join(plain(queue_card.render_selection(model, step.session_id, 400)) for step in running)
        rows = [row for row in text.splitlines() if "Selected running step:" in row]
        self.assertEqual(len(rows), 2)
        self.assertIn("need 8.0 GiB; anonymous use 1.2 GiB; held slot 0.0 GiB; kept aside 6.8 GiB", rows[0])
        self.assertIn("need 4.0 GiB; anonymous use 5.0 GiB; held slot 0.0 GiB; kept aside 0.0 GiB", rows[1])
        self.assertIn("unit", rows[0])
        self.assertIn("link", rows[1])
        self.assertIn("anonymous use 6.2 GiB", self.render(view))


    def test_pending_and_ended_steps_do_not_report_zero_measured_use(self) -> None:
        pending = records.RunningStep("first", "", "check", "/one", 8 * GIB, records.PromisedMemory(8 * GIB), pid=123)
        ended = records.RunningStep("second", "", "test", "/two", 12 * GIB, records.GoneMemory(), pid=123)
        model = self.model(queue_read_fixture(records.QueueView((), (pending, ended), 20 * GIB, 8 * GIB, GIB, NOW), policy=admission_policy_fixture()), selected=True)
        text = plain(queue_card.render_selection(model, "first", 400))
        row = next(row for row in text.splitlines() if "Selected running step:" in row)
        self.assertIn("8.0", row)
        self.assertIn("swap-backed use unavailable:", row)
        self.assertNotIn("second", text)
        self.assertNotIn("two", text)
        self.assertNotIn("anonymous use 0.0 GiB", row)

    def test_empty_queue_reads_once_without_subprocess_or_wait(self) -> None:
        reading = queue_read_fixture(records.QueueView((), (), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture())
        output = io.StringIO()
        with mock.patch.object(memory_admit, "queue_read", return_value=reading) as read, \
             mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             mock.patch("time.sleep", side_effect=AssertionError("no wait")), \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        self.labels_read.assert_called_once_with([], os.environ)
        self.assertIn("Build queue · 10:46", output.getvalue())
        self.assertIn("cap 17.0", output.getvalue())
        self.assertNotIn("|", output.getvalue())

    def test_one_row_per_table_uses_names_and_escapes_cell_text(self) -> None:
        waiting = records.WaitingStep(10, "hidden-id", "", "doc", "/tmp/hana/", NOW - 60, GIB, records.ReadyForAdmission())
        running = records.RunningStep("other-hidden-id", "lfo|trunk\nunit", "check", "/tmp/lfo", 2 * GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123)
        model = self.model(queue_read_fixture(records.QueueView((waiting,), (running,), 20 * GIB, GIB, GIB, NOW), policy=admission_policy_fixture()), selected=True)
        text = plain(queue_card.render_selection(model, "hana", 400)) + "\n"
        text += plain(queue_card.render_selection(model, "other-hidden-id", 400))
        self.assertIn("lfo|trunk unit", text)
        self.assertIn("hana", text)
        self.assertEqual(sum("Selected running step:" in row for row in text.splitlines()), 1)
        self.assertNotIn("hidden-id", text)

    def test_ten_thousand_waiting_and_running_rows_resolve_labels_once_per_row(self) -> None:
        waiting = tuple(records.WaitingStep(pid, f"session-{pid}", "", "check", f"/tmp/tree-{pid}", NOW - pid,
            12 * GIB, records.NeedsMemory(12 * GIB, 20 * GIB)) for pid in range(10000))
        running = tuple(records.RunningStep(f"session-{pid}", "", "test", f"/tmp/tree-{pid}", 8 * GIB,
            records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=pid) for pid in range(10000))
        reading = queue_read_fixture(records.QueueView(waiting, running, 20 * GIB, 10 * GIB, GIB, NOW),
                                     policy=admission_policy_fixture())
        with mock.patch.object(memory_admit, "smallest_head_release",
                               side_effect=AssertionError("an unselected view must not search releases")), \
             mock.patch.object(queue_view, "step_labels", wraps=queue_evidence.step_labels) as labels:
            model = self.model(reading)
            lines = queue_view.render_view(model, 100)
        self.assertEqual(labels.call_count, len(waiting) + len(running))
        self.assertEqual(len(waiting_rows(lines)), 10000)
        self.assertEqual(sum(plain([line]).startswith("●") for line in lines), 0)
        running_rows = "\n".join(plain([line]) for line in lines if plain([line]).lstrip().startswith("run"))
        queued_rows = "\n".join(waiting_rows(lines))
        for section, step in ((running_rows, "test"), (queued_rows, "check")):
            self.assertEqual(len(section.splitlines()), 10000)
            self.assertTrue(all(step in row.split() for row in section.splitlines()))
        self.assertEqual(sum("test" in row for row in running_rows.splitlines()), 10000)

    def test_selected_card_with_thirty_two_waiters_and_eight_running_steps_bounds_release_checks(self) -> None:
        waiting = tuple(records.WaitingStep(pid, f"waiter-{pid}", "worker", "check",
            f"/tmp/waiter-{pid}", NOW - 600, 16 * GIB,
            records.NeedsMemory(16 * GIB, 18 * GIB)) for pid in range(32))
        running = tuple(records.RunningStep(f"runner-{pid}", "runner", "test",
            f"/tmp/runner-{pid}", 8 * GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=pid) for pid in range(8))
        reading = queue_read_fixture(records.QueueView(waiting, running, 18 * GIB, 0, 3 * GIB, NOW),
                                     policy=admission_policy_fixture())
        with mock.patch.object(memory_admit, "smallest_head_release",
                               wraps=memory_admit.smallest_head_release) as release, \
             mock.patch.object(memory_admit, "head_after_steps_end",
                               wraps=memory_admit.head_after_steps_end) as scenarios:
            model = self.model(reading, selected=True)
            text = plain(queue_card.render_selection(model, "1", 100))
        release.assert_called_once_with(reading.explanation)
        self.assertLessEqual(scenarios.call_count, 2 + len(running) ** 2)
        self.assertIsInstance(model.release, memory_admit.HeadAdmitsAfterEnd)
        self.assertIn("#1 of 32", text)
        self.assertIn("Frees when:", text)


    def test_memory_reason_names_free_memory_and_earlyoom_reserve(self) -> None:
        pending = int(23.5 * GIB)
        waiting = records.WaitingStep(
            10, "waiting-id", "unit", "check", "/tmp/tree", NOW - 2400,
            int(11.6 * GIB), records.NeedsMemory(
                int(11.6 * GIB), int(23.1 * GIB), threshold=int(11.6 * GIB) + pending + 3 * GIB,
                growth=pending, reserve=3 * GIB),
        )
        running = (
            records.RunningStep("one", "", "test", "/one", 12 * GIB, records.PromisedMemory(12 * GIB), pid=123),
            records.RunningStep("two", "", "test", "/two", int(11.5 * GIB), records.PromisedMemory(int(11.5 * GIB)), pid=123),
            records.RunningStep("three", "", "test", "/three", 8 * GIB, records.MeasuredMemory(GIB, anonymous=records.AnonymousRunUse(GIB)), pid=123),
        )
        view = records.QueueView((waiting, ), running, int(23.1 * GIB), pending, 3 * GIB, NOW)
        text = self.checks(view)
        self.assertIn("Room: need 11.6 GiB > room 0.0 GiB: held", text)

    def test_memory_hold_above_step_need_names_every_threshold_part(self) -> None:
        waiting = records.WaitingStep(
            10, "waiting-id", "unit", "clippy", "/tmp/tree", NOW, 3 * GIB,
            records.NeedsMemory(
                3 * GIB, int(11.5 * GIB), threshold=6 * GIB + int(13.4 * GIB),
                growth=int(13.4 * GIB), reserve=3 * GIB),
        )
        view = records.QueueView((waiting, ), (), int(11.5 * GIB), int(13.4 * GIB), 3 * GIB, NOW)
        text = self.checks(view)
        self.assertIn("Room: need 3.0 GiB > room 0.0 GiB: held", text)

    def test_head_free_threshold_names_step_growth_and_reserve(self) -> None:
        decision = queue_decision(5 * GIB, 60 * GIB, 3 * GIB, [records.PromisedMemory(4 * GIB)])
        waiting = records.WaitingStep(
            10, "head-id", "unit", "clippy", "/tmp/tree", NOW - 2400, 3 * GIB,
            records.HeadAdmissionStatus(decision, 3 * GIB, 5 * GIB,
                cause=records.NeedsMemory(3 * GIB, 5 * GIB, decision.threshold, decision.promised, decision.reserve)),
        )
        view = records.QueueView((waiting, ), (), 5 * GIB, 4 * GIB, 3 * GIB, NOW)
        text = self.checks(view)
        self.assertIn("Room: need 3.0 GiB > room 0.0 GiB: held", text)




    def test_free_memory_head_uses_the_existing_decision_threshold_and_available_reading(self) -> None:
        need, available = 8 * GIB, 7 * GIB
        decision = queue_decision(available, 60 * GIB, need, [])
        row: records.Waiter = {"pid": 10, "start_time": "10", "need": need, "since": NOW - 300}
        head = records.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], need,
                                       records.HeadAdmissionStatus(decision, need, available,
                                           cause=records.NeedsMemory(need, available, decision.threshold, decision.promised, decision.reserve)))
        view = records.QueueView((head, ), (), available, 0, 3 * GIB, NOW)
        model = self.checks_model(view, admission_policy_fixture())
        with mock.patch.object(memory_admit, "admission_pass", side_effect=AssertionError("one decision")):
            text = self.render_checks(model)
        self.assertIn("Room: need 8.0 GiB > room 4.0 GiB: held", text)

    def test_pressure_head_uses_the_pressure_decision_instead_of_a_budget_explanation(self) -> None:
        need, available = 8 * GIB, 30 * GIB
        decision = queue_decision(available, 60 * GIB, need, [records.MeasuredMemory(19 * GIB, anonymous=records.AnonymousRunUse(19 * GIB))], pressure=records.MemoryPressure(23.4, condition="full"))
        row: records.Waiter = {"pid": 10, "start_time": "10", "need": need, "since": NOW - 300}
        head = records.WaitingStep(10, "head", "head-worker", "check", "/tmp/head", row["since"], need,
                                       records.HeadAdmissionStatus(decision, need, available,
                                           cause=records.MemoryPressure(23.4, condition="full")))
        running = records.RunningStep("other", "other", "doc", "/tmp/other", 19 * GIB,
                                          records.MeasuredMemory(19 * GIB, anonymous=records.AnonymousRunUse(19 * GIB)), pid=123)
        text = self.checks(records.QueueView((head, ), (running, ), available, 0, 3 * GIB, NOW))
        self.assertIn("memory pressure: full avg10 23.4", text)
        self.assertIn("Room: need 8.0 GiB <= room 27.0 GiB: pass", text)

    def test_room_check_keeps_the_recorded_need_and_room_including_rounded_zero(self) -> None:
        for need, room, passes in ((3 * GIB, 0, False), (1, 1, True), (0, 0, True)):
            with self.subTest(need=need, room=room):
                decision = records.AdmissionDecision("admit" if passes else "hold", need, 0, 0, 0,
                    free_passes=passes, room=records.RoomCheck(need, room, passes,
                        basis=records.Fits() if passes else records.DoesNotFit()))
                step = records.WaitingStep(10, "head", "unit", "clippy", "/tree", NOW, need,
                    records.HeadAdmissionStatus(decision, need, room,
                        cause=records.ReadyForAdmission() if passes else records.NeedsMemory(need, room)))
                text = self.checks(records.QueueView((step,), (), room, 0, 0, NOW),
                    policy=replace(admission_policy_fixture(), safety_bytes=0))
                self.assertIn(queue_evidence.room_check_text(decision.room), text)


    def test_view_and_card_use_one_label_read_and_never_wait_for_a_full_roster(self) -> None:
        with QueueFixture() as fixture:
            reading = fixture.read()
            write_bumps(NOW, [*fixture.moves, bump_line(NOW - 5, actor="actor-only", session="bump-only")])
            write_token_gaps([token_gap_line(NOW - 200, NOW - 46, session="id-work-lead")])
            seen: list[tuple[list[str], Mapping[str, str]]] = []
            def lookup(ids: list[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
                seen.append((ids, environment))
                return fixture.labels
            self.labels_read.side_effect = lookup
            expected = {step.session_id for step in (*reading.view.waiting, *reading.view.running)} | {"actor-only", "bump-only", "id-widget-director"}
            for args in ((), ("4", )):
                self.labels_read.reset_mock()
                seen.clear()
                with self.subTest(args=args), mock.patch.object(
                        roster, "roster", side_effect=AssertionError("the queue must not read the full roster")) as full:
                    status, text, errors = self.invoke(reading, *args)
                    self.assertEqual(status, 0, errors)
                    self.labels_read.assert_called_once()
                    ids, environment = seen[0]
                    self.assertEqual(set(ids), expected if args else
                                     {step.session_id for step in (*reading.view.waiting, *reading.view.running)})
                    self.assertIs(environment, os.environ)
                    full.assert_not_called()
                    self.assertIn("#4 of 32" if args else "Build queue · 10:46", text)
                    if not args:
                        self.assertEqual(len(waiting_rows(queue_view.render_view(self.model(reading), 100))), 32)

    def test_you_marks_every_waiting_step_of_only_the_asking_session(self) -> None:
        fixture = self.enterContext(QueueFixture())
        reading = fixture.cap_model().reading
        self.labels_read.return_value = fixture.labels
        for session, expected in (("id-work-lead", ["2", "3"]), ("id-widget", ["run"]), ("unknown", [])):
            with self.subTest(session=session):
                status, text, errors = self.invoke(reading, "--you", session)
                self.assertEqual(status, 0, errors)
                self.assertEqual(text.count("▸"), len(expected))
                self.assertEqual([row[:row.index("▸")].strip() for row in text.splitlines() if "▸" in row], expected)

    def test_inline_you_defaults_to_the_caller_and_a_shell_has_no_mark(self) -> None:
        fixture = self.enterContext(QueueFixture())
        reading = fixture.cap_model().reading
        for values, count in (({}, 0), ({"CODEX_THREAD_ID": "id-work-lead"}, 2),
                              ({"CLAUDE_CODE_SESSION_ID": "id-work-lead"}, 2),
                              ({"CODEX_THREAD_ID": "unknown", "CLAUDE_CODE_SESSION_ID": "id-work-lead"}, 0)):
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
                    if key not in values:
                        if key in os.environ:
                            del os.environ[key]
                status, text, errors = self.invoke(reading)
                self.assertEqual(status, 0, errors)
                self.assertEqual(text.count("▸"), count)

    def test_compact_view_never_reads_an_unmeasured_start_pace(self) -> None:
        fixture = self.enterContext(QueueFixture())
        reading = fixture.cap_model().reading
        with mock.patch.object(head_facts, "read_pace", side_effect=AssertionError("view has no start estimate")):
            status, text, errors = self.invoke(reading)
        self.assertEqual(status, 0, errors)
        self.assertTrue(text.startswith("Build queue · 10:46\n\n"))
        self.assertNotIn("starts", text)

    def test_explicit_columns_override_environment_for_text_and_json(self) -> None:
        reading = queue_read_fixture(records.QueueView((), (), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture())
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
        reading = queue_read_fixture(records.QueueView((), (), 20 * GIB, 0, GIB, NOW), policy=admission_policy_fixture())
        with mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("card cannot move queue")):
            status, text, errors = self.invoke(reading, "nobody")
        self.assertEqual((status, errors), (0, ""))
        self.assertTrue(text.startswith("Build queue · 10:46\n\n"))
        self.assertTrue(text.endswith("\n\nNo waiting or running step matches nobody.\n"))

    def test_selector_page_starts_with_the_full_view_and_ends_with_the_card(self) -> None:
        fixture = self.enterContext(QueueFixture())
        model = fixture.model()
        self.labels_read.return_value = fixture.labels
        session = "id-work-lead"
        for columns in (60, 80, 100, 140):
            for selector in ("1", session, "/tmp/tree", "mine"):
                for as_json in (False, True):
                    with self.subTest(columns=columns, selector=selector, json=as_json), \
                         mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session}):
                        args = ["--columns", str(columns), "--you", session, selector]
                        if as_json:
                            args.insert(0, "--json")
                        status, output, errors = self.invoke(model.reading, *args)
                        self.assertEqual((status, errors), (0, ""))
                        view = queue_view.render_view(model, columns, colour=as_json, you=session)
                        card = queue_card.render_selection(model, session if selector == "mine" else selector, columns)
                        if as_json:
                            data = cast(QueueLines, json.loads(output))
                            self.assertEqual(data["lines"][:len(view)], json_form(view, columns, NOW)["lines"])
                            self.assertEqual(data["lines"][len(view)], [])
                            self.assertEqual(data["lines"][len(view) + 1:], json_form(card, columns, NOW)["lines"])
                            text = self.snapshot_text(data)
                        else:
                            text = output.removesuffix("\n")
                            self.assertTrue(text.startswith(plain(view) + "\n\n"))
                            self.assertTrue(text.endswith("\n\n" + plain(card)))
                        self.assertEqual(text.splitlines()[0], "Build queue · 10:46")
                        self.assertTrue(all(terminal_width(line) <= columns for line in text.splitlines()))


if __name__ == "__main__":
    _ = unittest.main()


class ServiceQueryFailureTests(unittest.TestCase):
    def test_views_use_one_query_deadline_for_missing_and_silent_services(self) -> None:
        for silent in (False, True):
            with self.subTest(silent=silent), AdmissionFixture() as fixture:
                timeout = records.read_memory_config().query_timeout_seconds
                for arguments in ([], ["4"], ["--json", "1"]):
                    exchange = (Exchange(TimedChunk(timeout / 4, b"{"), TimeoutError()) if silent
                                else Exchange(connect_error=FileNotFoundError()))
                    transport = ScriptedTransport(fixture.clock, exchange)
                    output, errors = io.StringIO(), io.StringIO()
                    started = fixture.clock.monotonic()
                    with mock.patch.object(admission_client, "CLOCK", fixture.clock), \
                         mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket), \
                         mock.patch("time.sleep", side_effect=AssertionError("a query must not retry")), \
                         mock.patch("sys.argv", ["queue", *arguments]), \
                         redirect_stdout(output), redirect_stderr(errors):
                        status = build_queue.main()
                    self.assertEqual(fixture.clock.monotonic() - started, timeout if silent else 0)
                    self.assertEqual(len(transport.attempts), 1)
                    self.assertEqual(len(transport.requests), 1 if silent else 0)
                    self.assertEqual(status, 2)
                    self.assertEqual(output.getvalue(), "")
                    reason = "no answer in 0.5 s" if silent else "no socket"
                    self.assertEqual(errors.getvalue(),
                        f"build queue: admission service unavailable: {reason}\n")

    def test_queue_read_returns_the_private_services_exact_reading(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            with fixture.serving():
                self.assertEqual(memory_admit.queue_read(), reply.reading)
