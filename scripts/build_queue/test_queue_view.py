"""The fixed queue stays one row per step and explains waits once."""
from __future__ import annotations

from scripts.lint.memory_test_support import admission_policy_fixture

from collections.abc import Iterator
from dataclasses import replace
import os
from pathlib import Path
import unittest
from typing import Self, cast, override
from unittest import mock

from scripts.build_queue import head_facts, queue_card, queue_view
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted
from scripts.build_queue.queue_fixture import (
    ASKING_SESSION, GOLDEN, NOW, QueueFixture, QueueFixtureTests, queue_read_fixture, waiting_rows, with_waiting,
)
from scripts.build_queue.queue_lines import json_form, plain, wrap
from scripts.lib.terminal_cells import terminal_width
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import fake_memory_readings
from scripts.message.unit_labels import UnitLabels
from scripts.build_queue.queue_fixture import write_memory_config


class BumpRecordsWithVisitCount(tuple[head_facts.BumpRecord, ...]):
    visits: int

    def __new__(cls, records: tuple[head_facts.BumpRecord, ...]) -> Self:
        result = super().__new__(cls, records)
        result.visits = 0
        return result

    @override
    def __iter__(self) -> Iterator[head_facts.BumpRecord]:
        for record in super().__iter__():
            self.visits += 1
            yield record


class QueueViewTests(QueueFixtureTests):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.fixture.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(mock.patch.dict("os.environ", {"CONF_DIR": str(config.parent)})))

    def service_model(self, fixture: AdmissionFixture) -> queue_view.QueueModel:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return replace(self.fixture.model(), reading=reply.reading,
                       facts=head_facts.read_head_facts(reply.reading))

    def test_merge_gate_grant_has_a_running_row_with_use_and_grant(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate")
            request = replace(request, identity=wire.Identity("gate-owner", "gate-worker", "", str(fixture.root)),
                profile=gate.ProgramProfile("merge-gate", "repo", "tree", gate.FixedCapSizing(6 * gate.GIB)),
                sidecar=str(fixture.scope(request.owner)))
            _ = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(7 * 60)
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request(need=8 * gate.GIB))
            model = replace(self.service_model(fixture), labels={"gate-owner": UnitLabels("gate-worker", "devtools", "devtools")})
            for colour in (False, True):
                lines = queue_view.render_view(model, 140, colour=colour)
                text = plain(lines)
                running, queued = text.split("Running\n", 1)[1].split("Queued\n", 1)
                row, = [line for line in running.splitlines() if "merge-gate" in line]
                self.assertIn("devtools", row)
                self.assertIn("1.0 / 6.0", row)
                self.assertIn("7", row.split())
                self.assertEqual(row.count("merge-gate"), 1)
                queued_row, = waiting_rows(lines)
                bar_column = terminal_width(row[:row.index("█")])
                self.assertEqual(bar_column, terminal_width(queued_row[:queued_row.index("█")]))
                top = next(line for line in text.splitlines() if line.startswith("budget "))
                self.assertEqual(bar_column, terminal_width(top[:next(index for index, char in enumerate(top) if char in "░█")]))
                self.assertIn("8.0", queued)
                self.assertIn("░", row)

    def test_live_verify_run_between_steps_has_use_without_a_grant(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (fixture.proc.path / "stat").write_text(
                f"btime {fixture.clock.now() - 12 * 60 - 100 / os.sysconf('SC_CLK_TCK')}\n")
            _ = (process / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0CLAUDE_CODE_SESSION_ID=between-owner\0".encode())
            _ = (process / "cmdline").write_bytes(b"bash\0verify.sh\0")
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "between.scope")
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"between-owner": UnitLabels("worker", "hana", "hana")})
            text = plain(queue_view.render_view(model, 140))
            running = text.split("Running\n", 1)[1].split("Queued\n", 1)[0]
            row, = [line for line in running.splitlines() if "between steps" in line]
            self.assertIn("hana", row)
            self.assertIn("1.0", row)
            self.assertIn("12", row.split())
            self.assertNotIn("/", row)
            self.assertNotIn("Still running after their build ended", text)

    def test_ended_run_scope_names_its_process_owner_program_and_age(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700002, "100")
            second = fixture.fixture_owner(700003, "101")
            process = fixture.proc.path / str(owner.pid)
            _ = (fixture.proc.path / "stat").write_text(
                f"btime {fixture.clock.now() - 2 * 86400 - 100 / os.sysconf('SC_CLK_TCK')}\n")
            _ = (process / "environ").write_bytes(b"BUILDLOG_RUN=999999:1\0CLAUDE_CODE_SESSION_ID=ended-owner\0")
            _ = (process / "cmdline").write_bytes(b"python3\0-m\0unittest\0")
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "cgroup.procs").write_text(f"{owner.pid}\n{second.pid}\n")
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "unreadable-run-name.scope")
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"ended-owner": UnitLabels("worker", "builds", "queue-unit")})
            text = plain(queue_view.render_view(model, 140))
            section = text.split("Still running after their build ended", 1)[1]
            self.assertIn("2 processes", section)
            self.assertIn("1.0 GiB", section)
            self.assertIn("queue-unit", section)
            self.assertIn("test fixture", section)
            self.assertIn("python3", section)
            self.assertIn("started 2 days ago", section)
            self.assertNotIn("unreadable-run-name.scope", text)
            self.assertNotIn("(unowned)", text)

    def test_empty_scope_cache_remains_charged_but_has_no_visible_row(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds.slice/cache-only.scope"
            scope.mkdir(parents=True)
            _ = (scope / "memory.current").write_text(str(5 * gate.GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = (scope / "cgroup.procs").write_text("")
            _ = fixture.server.run_pass()
            model = self.service_model(fixture)
            self.assertEqual(model.reading.explanation.totals.charged, 5 * gate.GIB)
            text = plain(queue_view.render_view(model, 140))
            self.assertIn("taken 5.0", text)
            self.assertNotIn("cache-only", text)
            self.assertNotIn("Still running after their build ended", text)
            self.assertNotIn("(unowned)", text)

    def test_service_counts_unowned_scope_once_and_renders_the_held_heads_numbers(self) -> None:
        import os
        from pathlib import Path
        from scripts.build_queue import admission_client as client
        from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted
        with AdmissionFixture() as server:
            request = server.request(need=gate.GIB)
            sidecar = server.scope(request.owner)
            owned = Path(sidecar.read_text())
            builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
            builds.mkdir(parents=True, exist_ok=True)
            _ = owned.rename(builds / "owned.scope")
            _ = sidecar.write_text(str(builds / "owned.scope"))
            _ = only_admitted(server.server.handle_request(replace(request, sidecar=str(sidecar))))
            unowned = builds / "replay.scope"
            unowned.mkdir()
            amount = int(17.4 * gate.GIB)
            _ = (unowned / "memory.current").write_text(str(amount))
            _ = (unowned / "memory.stat").write_text("inactive_file 0\n")
            _ = (unowned / "cgroup.procs").write_text("")
            server.free(12 * gate.GIB)
            _ = server.server.handle_request(server.request(need=8 * gate.GIB))
            with server.serving():
                reading = client.view()
            measured = [step.measured for step in reading.view.running
                        if isinstance(step.measured, gate.UnownedSliceMemory)]
            self.assertEqual(len(measured), 1)
            self.assertEqual(measured[0].bytes, amount)
            head = reading.explanation.head
            assert isinstance(head, gate.HeadExplanation)
            self.assertEqual(head.budget.charged, amount + gate.GIB)
            self.assertEqual(head.free.available, 12 * gate.GIB)
            model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
            view = plain(queue_view.render_view(model, 400))
            card = plain(queue_card.render_card(model, [1], 400))
            page = view + "\n\n" + card
            self.assertNotIn("replay.scope", page)
            self.assertNotIn("Still running after their build ended", page)
            self.assertIn(f"need 8.0 + earlyoom {head.free.reserve / gate.GIB:.1f}", page)
            self.assertNotIn("owned.scope:", page)
            self.assertEqual(page.count("Free memory: free 12.0 GiB"), 1)

    def test_expired_head_with_passing_checks_has_an_error_hold_at_sixty_cells(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        path, row = inputs.ordered[0]
        inputs = replace(inputs, running=(), reservations=(), pressure=gate.NoPressureHold(),
            host=gate.HostMemory(60 * gate.GIB, 60 * gate.GIB),
            ordered=[(path, {**row, "need": gate.GIB, "estimated_at": NOW - 600,
                            "priority_since": NOW - 42}), inputs.ordered[1]])
        reading = gate.queue_read_from(inputs, gate.AdmissionLimits(gate.GIB, 3 * gate.GIB), policy=admission_policy_fixture())
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertTrue(head.budget.passes)
        self.assertTrue(head.free.passes)
        self.assertIsInstance(head.decision.pressure, gate.NoPressureHold)
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        lines = queue_view.render_view(model, 60)
        rows = waiting_rows(lines)
        self.assertTrue(rows[0].endswith("⊘ 9:18 estimate expired"), rows[0])
        self.assertEqual(queue_view.reason_phrase(reading.view.waiting[0].reason, reading.view.waiting),
                         "estimate expired")
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))
        self.assertEqual([row.index("█") for row in rows], [rows[0].index("█")] * len(rows))
        self.assertTrue(any(span.text.endswith("estimate expired") and span.tone == "error"
                            for line in lines for span in line))

    def test_pressure_threshold_text_uses_the_policy_in_the_reading(self) -> None:
        config = self.fixture.root / "build_admission.conf"
        write_memory_config(config, full_pressure_limit="9", swap_used_limit="70", some_pressure_limit="12", recovery_start_seconds="45")
        with mock.patch.dict("os.environ", {"CONF_DIR": str(config.parent)}):
            policy = gate.read_memory_config().policy
            write_memory_config(config, full_pressure_limit="3", swap_used_limit="90", some_pressure_limit="30")
            model = self.fixture.model()
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, policy=policy)))
        with mock.patch.object(gate, "read_memory_config", side_effect=AssertionError("render read config")):
            _, full = queue_view.pressure_check(gate.MemoryPressure(10, condition="full"), model)
            _, swap = queue_view.pressure_check(gate.MemoryPressure(0, 15, 80, condition="swap"), model)
            _, recovery = queue_view.pressure_check(gate.MemoryPressure(0, next_start_at=NOW + 45, condition="full"), model)
        self.assertIn("starts below full avg10 9.0%", full)
        self.assertIn("swap is at most 70% or some avg10 is at most 12.0%", swap)
        self.assertIn("one start per 45 s", recovery)

    def test_live_priority_marks_and_cards_use_the_snapshot_without_bump_history(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        model = replace(model, facts=replace(facts, bump_records=(), bumps=head_facts.BumpHistory((), 0, ())))
        self.assertEqual([step.session_id for step in model.reading.view.waiting[:3]],
                         ["id-work-pan", "id-work-lead", "id-work-lead"])
        with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("captured roster")), \
             mock.patch.object(gate, "read_queue_priority", side_effect=AssertionError("captured priority")):
            self.assertEqual([queue_view.is_moved(model, position) for position in range(1, 5)],
                             [True, True, True, False])
            self.assertEqual(queue_view.waiting_moves(model, 1), ())
            view = plain(queue_view.render_view(model, 100))
            card = plain(queue_card.render_card(model, [1], 100))
            frozen = gate.head_after_steps_end(model.reading.explanation, ())
        self.assertIn("queue priority: cables (the user's bump)", view)
        self.assertEqual((view + "\n" + card).count("queue priority: cables (the user's bump)"), 1)
        self.assertIn("waiting 36 min", card)
        for text in (view, card):
            self.assertNotIn("head_since", text)
            self.assertNotIn("became first", text)
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertEqual(head.waiter.session_id, "id-work-pan")
        self.assertIsInstance(frozen, gate.HeadStillHeld)
        assert isinstance(frozen, gate.HeadStillHeld)
        self.assertEqual(frozen.decision, head.decision)

    def test_live_bump_marks_every_unit_row_without_moving_columns(self) -> None:
        model = self.fixture.model()
        for unit in ("cables", "widget"):
            priority: gate.QueuePriority = {"unit": unit, "why": "test", "set_at": NOW, "set_by": "actor"}
            bumped = replace(model, reading=replace(model.reading,
                explanation=replace(model.reading.explanation, priority=priority)))
            unbumped = replace(model, reading=replace(model.reading,
                explanation=replace(model.reading.explanation, priority=gate.NoQueuePriority())))
            for columns in (40, 60, 80, 100, 140):
                with self.subTest(unit=unit, columns=columns):
                    baseline = queue_view.render_view(unbumped, columns, you=ASKING_SESSION, selector="widget")
                    with mock.patch.object(head_facts, "queue_moves", side_effect=AssertionError("bump history read")), \
                         mock.patch.object(gate, "read_queue_priority", side_effect=AssertionError("live priority read")):
                        lines = queue_view.render_view(bumped, columns, you=ASKING_SESSION, selector="widget")
                    rows = [line for line in lines if len(line) >= 6]
                    previous = [line for line in baseline if len(line) >= 6]
                    steps = [row.step for row in queue_view.running_builds(model)] + list(model.reading.view.waiting)
                    self.assertEqual(len(rows), len(steps))
                    for row, before, step in zip(rows, previous, steps):
                        expected = model.labels[step.session_id].unit == unit
                        self.assertEqual(sum(span.text == "●" for span in row), int(expected))
                        # The step, minutes, bars and selection mark retain their positions.
                        unit_end = 5 if expected else 4
                        self.assertEqual(row[unit_end:], before[4:])
                        self.assertEqual(terminal_width(plain([row[:unit_end]])), terminal_width(plain([before[:4]])))
                        self.assertEqual(row[:3], before[:3])
                    data = json_form(lines, columns, NOW)
                    self.assertEqual(sum(part["text"] == "●" and part.get("tone") == "warning"
                                         for row in data["lines"] for part in row),
                                     sum(model.labels[step.session_id].unit == unit for step in steps))

    def test_no_live_bump_keeps_the_recorded_board_bytes(self) -> None:
        model = self.fixture.budget_model()
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, priority=gate.NoQueuePriority())))
        self.assertEqual(plain(queue_view.render_view(model, 100, you=ASKING_SESSION)) + "\n",
                         (GOLDEN / "no_bump_100.txt").read_text())



    def test_older_fit_phrase_requires_both_recorded_identity_and_join_time(self) -> None:
        waiting = self.fixture.model().reading.view.waiting
        older = replace(waiting[2], since=waiting[2].since + 1)
        waiting = (*waiting[:2], older, *waiting[3:])
        reason = gate.BehindOlderFit(older.session_name, older.since, older.session_id)
        self.assertEqual(queue_view.reason_phrase(reason, waiting), "behind #3, an older step that fits")
        for changed in (replace(reason, older_since=older.since + 1),
                        replace(reason, older_session_id="not-waiting")):
            self.assertEqual(queue_view.reason_phrase(changed, waiting), "behind an older step that fits")

    def test_unrelated_bump_history_adds_no_record_visits_to_render(self) -> None:
        model = self.fixture.model()
        steps = tuple(replace(model.reading.view.waiting[0], pid=pid,
                              session_id=f"row-{pid}", folder=f"/tmp/row-{pid}")
                      for pid in range(4000))
        reading = queue_read_fixture(replace(model.reading.view, waiting=steps), policy=admission_policy_fixture())
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        empty = replace(model, reading=reading, facts=replace(facts, bump_records=()))
        records = BumpRecordsWithVisitCount(tuple(
            head_facts.BumpRecord(NOW - 1, "actor", f"unrelated-{index}",
                                  "check", f"/tmp/unrelated-{index}", 20)
            for index in range(5000)))
        history = replace(empty, facts=replace(facts, bump_records=records))
        expected = plain(queue_view.render_view(empty, 100))
        self.assertEqual(plain(queue_view.render_view(history, 100)), expected)
        self.assertEqual(records.visits, 0, "the view must not scan unrelated bump records")


    def test_name_column_stays_aligned_when_places_grow_to_three_digits(self) -> None:
        model = self.fixture.model()
        step = model.reading.view.waiting[0]
        steps = tuple(replace(step, pid=pid) for pid in range(1, 101))
        model = replace(model, reading=queue_read_fixture(replace(model.reading.view, waiting=steps), policy=admission_policy_fixture()))
        rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertEqual(len(rows), 100)
        for row, position in zip(rows[98:], (99, 100)):
            self.assertEqual(int(row.split()[0]), position)
        self.assertEqual(terminal_width(rows[98][:rows[98].index("tool-based-ui")]),
                         terminal_width(rows[99][:rows[99].index("tool-based-ui")]))


    def test_every_width_keeps_one_waiting_row_per_step_without_running_records(self) -> None:
        for columns in (30, 40, 60, 80, 100, 140, 400):
            with self.subTest(columns=columns):
                rendered = queue_view.render_view(self.fixture.budget_model(), columns)
                rows = waiting_rows(rendered)
                self.assertEqual(len(rows), 32)
                self.assertEqual([int(row.split()[0]) for row in rows], list(range(1, 33)))
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in rendered))
                self.assertFalse(any(row.startswith("Session:") for row in rows))
                self.assertIn("\nRunning\n", plain(rendered))
                self.assertIn("\nQueued\n", plain(rendered))
                self.assertEqual(plain(rendered).count("●"), 3)
                for removed in ("starts", "moved up:", "#1 waits on"):
                    self.assertNotIn(removed, plain(rendered))

    def test_card_checks_keep_all_numbers_and_failure_tones_when_wrapped(self) -> None:
        model = self.fixture.model()
        for columns in (400, 60, 30):
            with self.subTest(columns=columns):
                checks = queue_view.step_checks(model, 1)
                assert isinstance(checks, gate.HeadExplanation)
                shared_pressure = queue_view.check_line(*queue_view.pressure_check(checks.decision.pressure, model))
                lines = [row for check in [shared_pressure, *queue_view.check_lines(checks, model)]
                         for row in wrap(check, columns)]
                text = "".join(plain(lines).split())
                for fact in ("✗ memory pressure: full avg10 7.7%, some avg10 0.0%, swap used 0.0%; starts below full avg10 5.0%",
                             "✓ Build budget: charged 11.1 + need 7.6 = 18.7 GiB <= 21.0 GiB: pass",
                             "✓ Free memory: free 24.5 GiB >= need 7.6 + earlyoom 3.0 + promise not yet used 6.0 = 16.6 GiB: pass"):
                    self.assertIn("".join(fact.split()), text)
                self.assertEqual(sum(span.text.count("✗") for row in lines for span in row if span.tone == "error"), 1)
                self.assertEqual(sum(span.text.count("✓") for row in lines for span in row if span.tone == "success"), 2)

    def test_step_columns_align_wide_and_combining_names_by_cells(self) -> None:
        model = self.fixture.budget_model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, step="界e\u0301"), replace(second, step="doc")])
        rows = waiting_rows(queue_view.render_view(model, 140))
        for marker in ("tool-based-ui", "█"):
            self.assertEqual(terminal_width(rows[0][:rows[0].index(marker)]),
                             terminal_width(rows[1][:rows[1].index(marker)]))
        self.assertIn("界e\u0301", rows[0])

    def test_view_ignores_historical_moves_token_history_and_pace(self) -> None:
        model = self.fixture.budget_model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        expected = plain(queue_view.render_view(model, 100))
        wait = head_facts.CargoTokenWait("id-work-lead", "nextest", 1001, "1001", NOW - 200,
                                       head_facts.TokenReturned(NOW - 46))
        for changed in (replace(facts, bumps=head_facts.BumpsUnavailable("unreadable")),
                        replace(facts, bump_records=()),
                        replace(facts, token_waits=head_facts.RecordedTokenWaits((wait,), False, (wait,)))):
            for pace in (head_facts.RecordedPace(0), head_facts.PaceUnavailable()):
                self.assertEqual(plain(queue_view.render_view(replace(model, facts=changed, pace=pace), 100)), expected)
        for removed in ("unknown", "cargo token:", "Head for", "moved up:", "starts:"):
            self.assertNotIn(removed, expected)

    def test_empty_queue_keeps_budget_and_header_without_invented_steps(self) -> None:
        model = self.fixture.budget_model()
        reading = queue_read_fixture(gate.QueueView((), (), 24 * gate.GIB, 0, 3 * gate.GIB, NOW), policy=admission_policy_fixture())
        rendered = queue_view.render_view(replace(model, reading=reading, facts=gate.NoWaiter()), 100)
        text = plain(rendered)
        self.assertTrue(text.startswith("Build queue · 10:46\n\n"))
        self.assertIn("free 21.0 │ taken 0.0", text)
        self.assertIn("budget 21.0", text)
        self.assertEqual(waiting_rows(rendered), [])
        self.assertNotIn("⊘", text)
        self.assertNotIn("↑", text)

    def test_card_checks_use_admissions_numbers_without_redeciding(self) -> None:
        model = self.fixture.model()
        with mock.patch.object(gate, "decide", side_effect=AssertionError("admission decides once")), \
             mock.patch.object(gate, "build_charge", side_effect=AssertionError("recorded charge")):
            text = plain(queue_view.check_lines(queue_view.step_checks(model, 1), model))
        self.assertIn("Build budget: charged 11.1 + need 7.6 = 18.7 GiB <= 21.0 GiB", text)
        self.assertIn("= 16.6 GiB: pass", text)




class BudgetViewTests(QueueFixtureTests):
    def test_compact_page_matches_reviewed_golden_files(self) -> None:
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(self.fixture.budget_model(), columns, you=ASKING_SESSION)
                self.assertEqual(plain(lines) + "\n", (GOLDEN / f"view_{columns}.txt").read_text())
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                page = [*lines, (), *queue_card.render_selection(self.fixture.budget_model(), "1", columns)]
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in page))

    def test_budget_scale_and_row_columns_match_the_sixty_column_page(self) -> None:
        model = self.fixture.budget_model()
        lines = queue_view.render_view(model, 60)
        text = plain(lines).splitlines()
        self.assertEqual(text[:2], ["Build queue · 10:46", ""])
        self.assertEqual(text[2], "queue priority: cables (the user's bump)")
        self.assertEqual(text[3], " " * 36 + "free 4.4 │ taken 16.6")
        self.assertEqual(text[4], "budget 21.0" + " " * 25 + "░" * 4 + "█" * 17)
        self.assertEqual(text[5], "")
        rows = waiting_rows(lines)
        self.assertEqual(len(rows), 32)
        self.assertEqual(rows[0][36:], "██████▊ 6.8 ↑ ≤14.2 9:18")
        self.assertEqual(rows[0][3:13], "tool-base…")
        self.assertEqual(rows[0][14:23], "●cables  ")
        self.assertEqual(rows[0][24:31], "mend   ")
        self.assertEqual(rows[0][32:35], " 36")
        self.assertIn("\nRunning\n", plain(lines))
        self.assertIn("\nQueued\n", plain(lines))
        self.assertNotIn("moved up", plain(lines))
        self.assertNotIn("starts", plain(lines))

    def test_colour_budget_has_a_free_fraction_on_taken_background(self) -> None:
        lines = queue_view.render_view(self.fixture.budget_model(), 60, colour=True)
        budget = lines[4]
        free = [span for span in budget if span.tone == "success"]
        taken = [span for span in budget if span.tone == "suggestion"]
        self.assertEqual("".join(span.text for span in free), "████▍")
        self.assertEqual("".join(span.text for span in taken), "█" * 16)
        boundary = next(span for span in free if "▍" in span.text)
        self.assertEqual(boundary.background, "suggestion")
        self.assertEqual(terminal_width(plain([budget])) - 36, 21)
        wire = json_form(lines, 60, NOW)
        countdowns = [span for row in wire["lines"] for span in row if "until" in span]
        self.assertEqual([(span["text"], span["until"]) for span in countdowns], [("9:18", NOW + 558)])
        self.assertEqual([(span["text"], span["background"]) for row in wire["lines"]
                          for span in row if "background" in span], [("▍", "suggestion")])

    def test_unreadable_meminfo_reports_no_reading_instead_of_low_memory(self) -> None:
        _ = fake_memory_readings(self.fixture.root, full_avg10=0.0)
        model = self.fixture.budget_model()
        (self.fixture.root / "meminfo").unlink()
        reading = self.fixture.read()
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertIsInstance(head.waiter.reason, gate.ReadingUnavailable)
        self.assertFalse(head.free.passes)
        model = replace(model, reading=reading)
        lines = queue_view.render_view(model, 100, colour=True)
        first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
        self.assertTrue(plain([first]).endswith("no reading"))
        self.assertNotIn("low memory", plain([first]))
        self.assertNotIn("pressure", plain([first]))
        self.assertTrue(any("no reading" in span.text and span.tone == "error" for span in first))

    def test_real_head_decision_distinguishes_low_memory_from_a_budget_only_wait(self) -> None:
        model = self.fixture.budget_model()
        for available_gib, budget_gib, free_passes, budget_passes in (
                (2, 21, False, True), (32, 5, True, False)):
            with self.subTest(available_gib=available_gib, budget_gib=budget_gib), \
                    QueueFixture(captured=False) as fixture, \
                    mock.patch.dict("os.environ", {"BUILDLOG_BUILD_BUDGET_GIB": str(budget_gib)}):
                _ = fake_memory_readings(fixture.root, available_kib=available_gib * gate.GIB // 1024,
                                         total_kib=60 * gate.GIB // 1024, full_avg10=0.0)
                if not budget_passes:
                    fixture.write_running(used=gate.GIB, need=gate.GIB)
                _ = fixture.write_waiter({"pid": 123, "start_time": "123", "need": 10 * gate.GIB,
                    "since": NOW - 900, "session_id": "head-session", "session_name": "head",
                    "step": "nextest", "folder": "/tmp/head"})
                reading = fixture.read()
                head = reading.explanation.head
                assert isinstance(head, gate.HeadExplanation)
                reason = head.waiter.reason
                assert isinstance(reason, gate.HeadAdmissionStatus)
                self.assertEqual(reason.decision.state, "hold")
                self.assertIsInstance(reason.decision.pressure, gate.NoPressureHold)
                self.assertEqual(head.free.available, available_gib * gate.GIB)
                self.assertEqual(head.free.passes, free_passes)
                self.assertEqual(head.budget.passes, budget_passes)
                lines = queue_view.render_view(replace(model, reading=reading), 100, colour=True)
                first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
                if free_passes:
                    for words in ("low memory", "pressure", "no reading"):
                        self.assertNotIn(words, plain([first]))
                else:
                    self.assertTrue(plain([first]).endswith("low memory"))
                    self.assertTrue(any("low memory" in span.text and span.tone == "error" for span in first))
                    self.assertNotIn("pressure", plain([first]))
                    self.assertNotIn("no reading", plain([first]))

    def test_fractional_budget_with_nothing_taken_matches_its_need_bar_and_is_all_free(self) -> None:
        model = self.fixture.budget_model()
        host = model.reading.explanation.host
        head = model.reading.explanation.head
        assert isinstance(host, gate.HostAvailable)
        assert isinstance(head, gate.HeadExplanation)
        budget = int(21.01 * gate.GIB)
        step = replace(head.waiter, need=budget)
        model = with_waiting(model, [step])
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, limits=replace(model.reading.explanation.limits, budget=budget),
                totals=replace(model.reading.explanation.totals, charged=0),
                head=replace(head, waiter=step, budget=replace(head.budget, need=budget, budget=budget)))))
        lines = queue_view.render_view(model, 100, colour=True)
        budget_spans = [span for span in lines[4] if span.tone in ("success", "suggestion") and span.text]
        head_row = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
        need_bar = "".join(span.text for span in head_row if span.tone == "warning" and "█" in span.text)
        self.assertEqual(need_bar, "█" * 21)
        self.assertEqual("".join(span.text for span in budget_spans), need_bar)
        self.assertTrue(all(span.tone == "success" and span.background == "default" for span in budget_spans))
        plain_budget = queue_view.render_view(model, 100)[4]
        self.assertTrue(plain([plain_budget]).endswith("░" * 21))
        self.assertNotIn("█", plain([plain_budget]))

    def test_countdown_cut_at_fifty_three_columns_cannot_be_repainted_past_the_edge(self) -> None:
        model = self.fixture.budget_model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        step = replace(head.waiter, need=21 * gate.GIB)
        model = with_waiting(model, [step, *model.reading.view.waiting[1:]])
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation,
                head=replace(head, waiter=step, budget=replace(head.budget, need=step.need)))))
        wide = queue_view.render_view(model, 60, colour=True)
        wide_head = next(line for line in wide if plain([line]).lstrip().startswith("1 "))
        self.assertEqual([span.until for span in wide_head if span.until is not None], [NOW + 558])
        narrow = queue_view.render_view(model, 53, colour=True)
        first = next(line for line in narrow if plain([line]).lstrip().startswith("1 "))
        self.assertTrue(plain([first]).endswith("…"))
        self.assertTrue(plain([first]).endswith(" ⊘ …"))
        self.assertLessEqual(terminal_width(plain([first])), 53)
        self.assertTrue(all(span.until is None for span in first))
        head_index = narrow.index(first)
        self.assertTrue(all("until" not in span for span in json_form(narrow, 53, NOW)["lines"][head_index]))

    def test_row_styles_and_you_marker_match_the_drawn_page(self) -> None:
        lines = queue_view.render_view(self.fixture.budget_model(), 60, colour=True, you=ASKING_SESSION)
        rows = [line for line in lines if plain([line]) in waiting_rows(lines)]
        for position, row in enumerate(rows, 1):
            with self.subTest(position=position):
                self.assertTrue(any(str(position) in span.text and span.tone == ("warning" if position == 1 else "inactive")
                                    for span in row))
                self.assertTrue(any(span.tone == "suggestion" and ("cables" in span.text or position > 3)
                                    for span in row))
                self.assertEqual(sum(span.text.count("▸") for span in row if span.tone == "suggestion"),
                                 1 if position in (2, 3) else 0)
                self.assertTrue(any(span.tone == "warning" and "█" in span.text for span in row))
        self.assertEqual(lines[0][0].tone, "inactive")

    def test_taken_over_budget_clamps_free_to_zero_without_growing_the_bar(self) -> None:
        model = self.fixture.budget_model()
        reading = replace(model.reading, explanation=replace(model.reading.explanation,
            totals=replace(model.reading.explanation.totals, charged=25 * gate.GIB)))
        lines = queue_view.render_view(replace(model, reading=reading), 60, colour=True)
        self.assertIn("free 0.0 │ taken 25.0", plain([lines[3]]))
        self.assertEqual(plain([lines[4]])[36:], "█" * 21)
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))

    def test_size_bars_use_nearest_eighth_zero_and_budget_cap(self) -> None:
        model = self.fixture.budget_model()
        for need, expected in ((6.8, "██████▊"), (0, ""), (25, "█" * 21)):
            with self.subTest(need=need):
                step = replace(model.reading.view.waiting[1], need=int(need * gate.GIB))
                changed = with_waiting(model, [model.reading.view.waiting[0], step])
                lines = queue_view.render_view(changed, 100, colour=True)
                row = next(line for line in lines if plain([line]).lstrip().startswith("2 "))
                self.assertEqual("".join(span.text for span in row if span.tone == "warning"), "●" + expected)
                self.assertTrue(any(f"{need:.1f}" in span.text and span.tone == "inactive" for span in row))

    def test_long_name_growth_and_missing_unit_session_fallback(self) -> None:
        model = self.fixture.budget_model()
        first, second = model.reading.view.waiting[:2]
        first = replace(first, step="nextest")
        labels = dict(model.labels)
        labels[first.session_id] = UnitLabels(
            "session", "performance", "performance-metrics-viewer")
        second = replace(second, session_id="no-unit")
        labels[second.session_id] = UnitLabels("other-session", "tool-based-ui", "—")
        model = replace(with_waiting(model, [first, second]), labels=labels)
        for columns in (80, 100, 140):
            with self.subTest(columns=columns):
                rows = waiting_rows(queue_view.render_view(model, columns))
                self.assertIn("metrics-viewer", rows[0])
                self.assertNotIn("performance-metrics-viewer", rows[0])
                self.assertIn("tool-based-ui", rows[1])
                self.assertIn("other-session", rows[1])
                self.assertEqual(rows[0].index("nextest"), rows[1].index("nextest"))

    def test_showrunner_prefix_is_removed_from_running_and_queued_units(self) -> None:
        model = self.fixture.budget_model()
        queued = model.reading.view.waiting[0]
        running = model.reading.explanation.running[0].step
        labels = dict(model.labels)
        for step in (queued, running):
            labels[step.session_id] = UnitLabels("merge-gate", "builds", "builds-queue")
        text = plain(queue_view.render_view(replace(model, labels=labels), 140))
        running_text, queued_text = text.split("Running\n", 1)[1].split("Queued\n", 1)
        for section, offset in ((running_text, 0), (queued_text, 1)):
            row, = [line for line in section.splitlines() if "builds" in line]
            self.assertEqual(row.split()[offset:offset + 2], ["builds", "queue"])
            self.assertNotIn("builds-queue", row)

    def test_queued_showrunner_without_a_unit_uses_its_session_name(self) -> None:
        model = self.fixture.budget_model()
        step = model.reading.view.waiting[0]
        for missing_unit in ("", "—"):
            with self.subTest(unit=missing_unit):
                labels = {**model.labels, step.session_id: UnitLabels("builds", "builds", missing_unit)}
                row = waiting_rows(queue_view.render_view(replace(model, labels=labels), 140))[0]
                self.assertEqual(row.split()[1:3], ["builds", "builds"])

    def test_queued_staff_unit_overrides_merge_gate_session_and_folder_names(self) -> None:
        model = self.fixture.budget_model()
        step = replace(model.reading.view.waiting[0], session_name="merge-gate", folder="/tmp/merge-gate", step="merge-gate")
        labels = {**model.labels, step.session_id: UnitLabels("merge-gate", "sdlc", "devtools")}
        model = replace(with_waiting(model, [step]), labels=labels)
        row, = waiting_rows(queue_view.render_view(model, 140))
        self.assertEqual(row.split()[1:4], ["sdlc", "devtools", "merge-gate"])

    def test_head_states_have_recorded_deadlines_or_no_deadline(self) -> None:
        model = self.fixture.budget_model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        cases: tuple[tuple[float, bool, gate.StrictUntil | gate.BackfillUntil | gate.WindowNotStarted,
                           dict[str, float], str, float | None], ...] = (
            (7, True, gate.StrictUntil(NOW + 558), {"priority_since": NOW - 42}, "↑ ≤14.0 9:18", NOW + 558),
            (21, True, gate.StrictUntil(NOW + 558), {"priority_since": NOW - 42}, "⊘ 9:18", NOW + 558),
            (22, True, gate.StrictUntil(NOW + 558), {"priority_since": NOW - 42}, "⊘ 9:18", NOW + 558),
            (7, True, gate.StrictUntil(NOW + 720), {"priority_since": NOW}, "↑ ≤14.0 12:00", NOW + 720),
            (7, True, gate.StrictUntil(NOW - 1), {"priority_since": NOW - 601}, "↑ ≤14.0 0:00", NOW - 1),
            (7, True, gate.BackfillUntil(NOW + 78), {"priority_since": NOW - 642}, "↑ 1:18", NOW + 78),
            (7, False, gate.WindowNotStarted(), {}, "↑", None),
            (7, True, gate.WindowNotStarted(), {}, "⊘", None),
        )
        for need, priority, order, fields, expected, until in cases:
            for elapsed in (0, 60) if until is None else (0,):
                with self.subTest(priority=priority, order=order, elapsed=elapsed):
                    row = dict(model.reading.ordered[0][1])
                    if "priority_since" in row:
                        del row["priority_since"]
                    row.update(fields)
                    step = replace(head.waiter, need=int(need * gate.GIB))
                    reading = replace(model.reading,
                        view=replace(model.reading.view, waiting=(step, *model.reading.view.waiting[1:])),
                        ordered=[(model.reading.ordered[0][0], row), *model.reading.ordered[1:]],
                        explanation=replace(model.reading.explanation, now=NOW + elapsed,
                                            head=replace(head, waiter=step, has_priority=priority, order=order)))
                    lines = queue_view.render_view(replace(model, reading=reading), 100)
                    first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
                    self.assertTrue(plain([first]).endswith(expected))
                    spans = [span for span in first if span.until is not None]
                    self.assertEqual([span.until for span in spans], [] if until is None else [until])
                    symbol = "⊘" if expected.startswith("⊘") else "↑"
                    self.assertTrue(any(symbol in span.text and span.tone == ("error" if symbol == "⊘" else "success")
                                        for span in first))

    def test_strict_room_and_low_memory_fit_sixty_columns_with_shared_alignment(self) -> None:
        model = self.fixture.budget_model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        step = replace(head.waiter, step="nextest", need=7 * gate.GIB, reason=gate.NeedsMemory())
        model = with_waiting(model, [step, model.reading.view.waiting[1]])
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, head=replace(head, waiter=step))))
        lines = queue_view.render_view(model, 60, colour=True)
        rows = waiting_rows(lines)
        self.assertTrue(rows[0].endswith("↑ ≤14.0 9:18 low memory"))
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))
        self.assertEqual(rows[0].index("nextest"), rows[1].index("nextest"))
        self.assertEqual(rows[0].index("█"), rows[1].index("█"))
        head_row = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
        self.assertTrue(any("↑ ≤14.0" in span.text and span.tone == "success" for span in head_row))
        self.assertTrue(any("low memory" in span.text and span.tone == "error" for span in head_row))

    def test_host_holds_add_short_words_and_a_large_head_keeps_columns_aligned(self) -> None:
        model = self.fixture.budget_model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        for reason, word in ((gate.NeedsMemory(), "low memory"), (gate.MemoryPressure(7.7, condition="full"), "pressure"),
                             (gate.NeedsBuildBudget(21 * gate.GIB, 16 * gate.GIB, 21 * gate.GIB), "")):
            with self.subTest(reason=reason):
                step = replace(head.waiter, step="nextest", reason=reason, need=21 * gate.GIB)
                changed = with_waiting(model, [step, model.reading.view.waiting[1]])
                changed = replace(changed, reading=replace(changed.reading,
                    explanation=replace(changed.reading.explanation, head=replace(head, waiter=step))))
                lines = queue_view.render_view(changed, 140)
                first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
                if word:
                    self.assertTrue(plain([first]).endswith(word))
                    self.assertTrue(any(word in span.text and span.tone == "error" for span in first))
                else:
                    self.assertNotIn("low memory", plain([first]))
                    self.assertNotIn("pressure", plain([first]))
                narrow = queue_view.render_view(changed, 60)
                rows = waiting_rows(narrow)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in narrow))
                self.assertEqual(rows[0].index("nextest"), rows[1].index("nextest"))


if __name__ == "__main__":
    _ = unittest.main()
