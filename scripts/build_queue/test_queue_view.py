"""The fixed queue stays one row per step and explains waits once."""
from __future__ import annotations

from dataclasses import replace
from statistics import median
import time
import unittest
from unittest import mock

from scripts.build_queue import head_facts, queue_card, queue_view
from scripts.build_queue.queue_fixture import (
    ASKING_SESSION, GOLDEN, NOW, QueueFixture, QueueFixtureTests, queue_read_fixture, waiting_rows, with_waiting,
)
from scripts.build_queue.queue_lines import json_form, plain, terminal_width, wrap
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.lint.memory_test_support import fake_memory_readings


class QueueViewTests(QueueFixtureTests):
    def test_expired_head_with_passing_checks_has_an_error_hold_at_sixty_cells(self) -> None:
        inputs = self.fixture.inputs()
        path, row = inputs.ordered[0]
        inputs = replace(inputs, running=(), reservations=(), pressure=gate.NoPressureHold(),
            host=gate.HostMemory(60 * gate.GIB, 60 * gate.GIB),
            limits=gate.AdmissionLimits(gate.GIB, 3 * gate.GIB),
            ordered=[(path, {**row, "need": gate.GIB, "estimated_at": NOW - 600,
                            "priority_since": NOW - 42}), inputs.ordered[1]])
        reading = gate.queue_read_from(inputs)
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
        self.assertIn("queue priority: cables (the user's bump)", card)
        self.assertIn("Waited 36 min", card)
        for text in (view, card):
            self.assertNotIn("head_since", text)
            self.assertNotIn("became first", text)
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertEqual(head.waiter.session_id, "id-work-pan")
        self.assertIsInstance(frozen, gate.HeadStillHeld)
        assert isinstance(frozen, gate.HeadStillHeld)
        self.assertEqual(frozen.decision, head.decision)

    def test_stale_snapshot_marks_only_the_original_header_time_as_warning(self) -> None:
        model = self.fixture.budget_model()
        current = queue_view.render_view(model, 100)
        stale = replace(model, reading=replace(model.reading, freshness=gate.StaleReading(NOW - 11)))
        lines = queue_view.render_view(stale, 100)
        self.assertEqual(plain(lines), plain(current))
        self.assertEqual(plain([lines[0]]), "Build queue · 10:46")
        self.assertTrue(all(span.tone == "warning" for span in lines[0]))
        self.assertTrue(all(span.tone == "inactive" for span in current[0]))
        self.assertEqual(lines[1:], current[1:])

    def test_older_fit_phrase_requires_both_recorded_identity_and_join_time(self) -> None:
        waiting = self.fixture.model().reading.view.waiting
        older = replace(waiting[2], since=waiting[2].since + 1)
        waiting = (*waiting[:2], older, *waiting[3:])
        reason = gate.BehindOlderFit(older.session_name, older.since, older.session_id)
        self.assertEqual(queue_view.reason_phrase(reason, waiting), "behind #3, an older step that fits")
        for changed in (replace(reason, older_since=older.since + 1),
                        replace(reason, older_session_id="not-waiting")):
            self.assertEqual(queue_view.reason_phrase(changed, waiting), "behind an older step that fits")

    def test_unrelated_bump_history_adds_at_most_ten_percent_to_render_time(self) -> None:
        model = self.fixture.model()
        steps = tuple(replace(model.reading.view.waiting[0], pid=pid,
                              session_id=f"row-{pid}", folder=f"/tmp/row-{pid}")
                      for pid in range(4000))
        reading = queue_read_fixture(replace(model.reading.view, waiting=steps))
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        empty = replace(model, reading=reading, facts=replace(facts, bump_records=()))
        records = tuple(head_facts.BumpRecord(NOW - 1, "actor", f"unrelated-{index}",
                                             "check", f"/tmp/unrelated-{index}", 20)
                        for index in range(5000))
        history = replace(empty, facts=replace(facts, bump_records=records))
        expected = plain(queue_view.render_view(empty, 100))
        self.assertEqual(plain(queue_view.render_view(history, 100)), expected)
        samples: dict[bool, list[float]] = {False: [], True: []}
        # Alternate order and use CPU time to avoid attributing another worker's
        # scheduling delay to bump-history lookup.
        for repetition in range(6):
            for has_history in ((False, True) if repetition % 2 else (True, False)):
                started = time.process_time()
                _ = queue_view.render_view(history if has_history else empty, 100)
                samples[has_history].append(time.process_time() - started)
        baseline, indexed = median(samples[False]), median(samples[True])
        self.assertLessEqual(indexed, baseline * 1.1,
                             f"5000 unrelated moves: {indexed:.6f}s; none: {baseline:.6f}s")
        print(f"queue history render: none {baseline:.6f}s; 5000 unrelated {indexed:.6f}s")


    def test_name_column_stays_aligned_when_places_grow_to_three_digits(self) -> None:
        model = self.fixture.model()
        step = model.reading.view.waiting[0]
        steps = tuple(replace(step, pid=pid) for pid in range(1, 101))
        model = replace(model, reading=queue_read_fixture(replace(model.reading.view, waiting=steps)))
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
                for removed in ("●", "Running", "starts", "moved up:", "#1 waits on"):
                    self.assertNotIn(removed, plain(rendered))

    def test_card_checks_keep_all_numbers_and_failure_tones_when_wrapped(self) -> None:
        model = self.fixture.model()
        for columns in (400, 60, 30):
            with self.subTest(columns=columns):
                lines = [row for check in queue_view.check_lines(queue_view.step_checks(model, 1), model)
                         for row in wrap(check, columns)]
                text = "".join(plain(lines).split())
                for fact in ("✗ memory pressure: full avg10 7.7 now; starts below 5.0",
                             "✓ build budget: 11.1 of 21.0 GiB held; needs 7.6",
                             "✓ free memory 24.5 GiB; needs 16.6: this step 7.6 + earlyoom 3.0 + promised not yet used 6.0"):
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
        reading = queue_read_fixture(gate.QueueView((), (), 24 * gate.GIB, 0, 3 * gate.GIB, NOW))
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
        self.assertIn("11.1 of 21.0 GiB held; needs 7.6", text)
        self.assertIn("needs 16.6", text)




class BudgetViewTests(QueueFixtureTests):
    def test_compact_page_matches_reviewed_golden_files(self) -> None:
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(self.fixture.budget_model(), columns, you=ASKING_SESSION)
                self.assertEqual(plain(lines) + "\n", (GOLDEN / f"view_{columns}.txt").read_text())
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))

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
        self.assertEqual(rows[0][14:23], "cables   ")
        self.assertEqual(rows[0][24:31], "mend   ")
        self.assertEqual(rows[0][32:35], " 36")
        self.assertNotIn("Running", plain(lines))
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
        reading = gate.locked_queue_read()
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
                reading = gate.locked_queue_read()
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
                first = lines[6]
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
            explanation=replace(model.reading.explanation, host=replace(host, budget=budget),
                totals=replace(model.reading.explanation.totals, charged=0),
                head=replace(head, waiter=step, budget=replace(head.budget, need=budget, budget=budget)))))
        lines = queue_view.render_view(model, 100, colour=True)
        budget_spans = [span for span in lines[4] if span.tone in ("success", "suggestion") and span.text]
        need_bar = "".join(span.text for span in lines[7] if span.tone == "warning" and "█" in span.text)
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
        self.assertEqual([span.until for span in wide[7] if span.until is not None], [NOW + 558])
        narrow = queue_view.render_view(model, 53, colour=True)
        first = narrow[7]
        self.assertTrue(plain([first]).endswith("…"))
        self.assertTrue(plain([first]).endswith(" ⊘ …"))
        self.assertLessEqual(terminal_width(plain([first])), 53)
        self.assertTrue(all(span.until is None for span in first))
        self.assertTrue(all("until" not in span for span in json_form(narrow, 53, NOW)["lines"][7]))

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
                self.assertEqual("".join(span.text for span in row if span.tone == "warning"), expected)
                self.assertTrue(any(f"{need:.1f}" in span.text and span.tone == "inactive" for span in row))

    def test_long_name_growth_prefix_removal_and_missing_unit(self) -> None:
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
                self.assertIn("—", rows[1])
                self.assertEqual(rows[0].index("nextest"), rows[1].index("nextest"))

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
        self.assertTrue(any("↑ ≤14.0" in span.text and span.tone == "success" for span in lines[7]))
        self.assertTrue(any("low memory" in span.text and span.tone == "error" for span in lines[7]))

    def test_host_holds_add_short_words_and_a_large_head_keeps_columns_aligned(self) -> None:
        model = self.fixture.budget_model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        for reason, word in ((gate.NeedsMemory(), "low memory"), (gate.MemoryPressure(7.7), "pressure"),
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
