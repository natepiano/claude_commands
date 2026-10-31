"""The fixed queue stays one row per step and explains waits once."""
from __future__ import annotations

from dataclasses import replace
from statistics import median
import time
import unittest
from typing import cast
from pathlib import Path
from unittest import mock

from scripts.build_queue import head_facts, queue_view
from scripts.build_queue.queue_fixture import (
    GOLDEN, NOW, QueueFixtureTests, queue_read_fixture, waiting_rows, with_waiting,
)
from scripts.build_queue.queue_lines import plain, terminal_width
from scripts.lint import memory_admit as gate


class QueueViewTests(QueueFixtureTests):
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

    def test_captured_queue_matches_golden_at_three_widths(self) -> None:
        model = self.fixture.model()
        for columns in (80, 100, 140):
            with self.subTest(columns=columns):
                self.assertEqual(plain(queue_view.render_view(model, columns)) + "\n",
                                 (GOLDEN / f"view_{columns}.txt").read_text())

    def test_every_width_keeps_exactly_one_row_per_step_and_fits_the_edge(self) -> None:
        model = self.fixture.model()
        for columns in (30, 40, 60, 80, 100, 140, 400):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(model, columns)
                rows = waiting_rows(lines)
                self.assertEqual(len(rows), 32)
                self.assertEqual([int(row[2:4]) for row in rows], list(range(1, 33)))
                self.assertEqual(sum(plain([line]).startswith("●") for line in lines), 1)
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                self.assertFalse(any(row.startswith("Session:") for row in rows))
                if columns < 80:
                    self.assertFalse(any("~" in row for row in rows))
                    self.assertTrue(all("starts" not in plain([line]) for line in lines
                                        if "waited" in plain([line])))
                else:
                    self.assertIn("~1 min", rows[0])
                if 80 <= columns < 100:
                    self.assertNotIn("tool-based-ui", rows[0])
                    self.assertIn("cables · work-lead", rows[0])
                    self.assertIn("Waiting by production: tool-based-ui 19", plain(lines))
                if columns == 40:
                    self.assertTrue(any(row.endswith("…") for row in rows))

    def test_head_checks_survive_wide_and_narrow_wrapping_with_failure_tones(self) -> None:
        for columns in (400, 60, 30):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(self.fixture.model(), columns)
                # Narrow rows can split a word; every nonspace character survives.
                text = "".join(plain(lines).split())
                for fact in ("✗ memory pressure: full avg10 7.7 now; starts below 5.0",
                             "✗ build budget: 11.1 of 21.0 GiB held; needs 11.5; starts when held falls to 9.5",
                             "✓ free memory 24.5 GiB; needs 20.5: this step 11.5 + earlyoom 3.0 + promised not yet used 6.0"):
                    self.assertIn("".join(fact.split()), text)
                self.assertEqual(sum(span.text.count("✗") for line in lines for span in line
                                     if span.tone == "error"), 2)
                self.assertEqual(sum(span.text.count("✓") for line in lines for span in line
                                     if span.tone == "success"), 1)

    def test_step_columns_align_wide_and_combining_step_names_by_cells(self) -> None:
        model = self.fixture.model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, step="界e\u0301"), replace(second, step="doc")])
        rows = waiting_rows(queue_view.render_view(model, 140))
        need_edges = [terminal_width(row[:row.index("11.5") + 4]) for row in rows]
        name_edges = [terminal_width(row[:row.index("tool-based-ui")]) for row in rows]
        self.assertEqual(need_edges[0], need_edges[1])
        self.assertEqual(name_edges[0], name_edges[1])
        self.assertIn("界e\u0301", rows[0])
        # The same guarantee applies to the running table.
        run = model.reading.explanation.running[0]
        other = replace(run, step=replace(run.step, step="界e\u0301"))
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, running=(run, other))))
        runs = [plain([line]) for line in queue_view.render_view(model, 140)
                if plain([line]).startswith("●")]
        self.assertEqual(terminal_width(runs[0][:runs[0].index("11.1") + 4]),
                         terminal_width(runs[1][:runs[1].index("11.1") + 4]))

    def test_old_waits_warn_and_session_names_are_inactive(self) -> None:
        lines = queue_view.render_view(self.fixture.model(), 140)
        for waited in ("39 min", "37 min", "30 min"):
            self.assertTrue(any(waited in span.text and span.tone == "warning"
                                for line in lines for span in line))
        self.assertTrue(any(span.text == "work-lead" and span.tone == "inactive"
                            for line in lines for span in line))
        row = next(line for line in lines if "39 min" in plain([line]))
        self.assertEqual(plain([row]).count("hana-migration"), 1)

    def test_strict_rest_is_said_once_after_the_head_checks(self) -> None:
        text = plain(queue_view.render_view(self.fixture.model(), 400))
        rest = "#2-#32: younger steps may use 9.5 GiB of the budget until 10:56 (9 min 48 s left); 4 of 31 fit now"
        self.assertEqual(text.count(rest), 1)
        self.assertLess(text.index("✓ free memory"), text.index(rest))
        self.assertLess(text.index(rest), text.index("  2 ↑27"))

    def test_mixed_rest_reasons_group_nonadjacent_places_in_first_place_order(self) -> None:
        model = self.fixture.model()
        reasons: tuple[gate.WaitReason, ...] = (
            gate.NeedsBuildBudget(1, 2, 3), gate.NeedsMemory(),
            gate.NeedsBuildBudget(1, 2, 3), gate.NeedsMemory(), gate.NeedsMemory(),
            gate.MemoryPressure(7.7), gate.MemoryPressure(0, next_start_at=NOW + 5),
            gate.ReadyForAdmission(), gate.ReadingUnavailable(),
        )
        steps = [model.reading.view.waiting[0], *[
            replace(step, reason=reason) for step, reason in zip(model.reading.view.waiting[1:], reasons)]]
        facts = model.facts
        self.assertIsInstance(facts, head_facts.HeadFacts)
        assert isinstance(facts, head_facts.HeadFacts)
        model = replace(with_waiting(model, steps), facts=replace(facts, has_priority=False))
        self.assertEqual([plain([line]) for line in queue_view.rest_lines(model)], [
            "#2 #4: build budget", "#3 #5-#6: free memory", "#7: memory pressure",
            "#8: recovery spacing", "#9: starts at the next check", "#10: memory reading unavailable",
        ])

    def test_shared_moves_repeat_history_earliest_mark_and_user_actor(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        records = tuple(replace(record, actor="user:nate") if record.session == "id-work-lead" else record
                        for record in facts.bump_records if record.old_position != 1)
        model = replace(model, facts=replace(facts, bump_records=records))
        text = plain(queue_view.render_view(model, 400))
        self.assertIn("↑ #1 #2 moved up: from #27 by the user at 10:40", text)
        self.assertIn("■  1 ↑27", text)
        original = plain(queue_view.render_view(self.fixture.model(), 400))
        self.assertIn("↑ #1 moved up: from #27 by work-lead at 10:40; from #1 by work-lead at 10:46", original)
        self.assertLess(original.index("↑ #1 moved up"), original.index("↑ running nextest moved up"))
        self.assertLess(original.index("↑ running nextest moved up"), original.index("↑ #2 moved up"))
        self.assertIn("●", original)
        self.assertIn("↑29", original)

    def test_move_lines_cap_at_five_and_count_remaining_steps(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        rows = [(path, dict(row)) for path, row in model.reading.ordered]
        records: list[head_facts.BumpRecord] = []
        for index, step in enumerate(model.reading.view.waiting[:7]):
            rows[index][1]["bump_order"] = -10 + index
            records.append(head_facts.BumpRecord(NOW - 8 + index, "id-work-lead", step.session_id,
                                                step.step, step.folder, 30 + index))
        reading = replace(model.reading, ordered=cast(list[tuple[Path, gate.Waiter]], rows))
        model = replace(model, reading=reading, facts=replace(facts, bump_records=tuple(records)))
        text = plain(queue_view.move_lines(model))
        self.assertEqual(text.count("moved up:"), 5)
        self.assertIn("and 2 more moved-up steps", text)
        self.assertLess(text.index("#7 moved"), text.index("#6 moved"))

    def test_unrecorded_or_old_moves_keep_only_the_bare_mark(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        records = tuple(replace(record, at=NOW - 3600) for record in facts.bump_records)
        model = replace(model, facts=replace(facts, bump_records=records))
        rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertIn("■  1 ↑ ", rows[0])
        self.assertNotIn("↑27", rows[0])
        self.assertEqual(queue_view.waiting_moves(model, 1), ())

    def test_failed_bump_read_has_no_marks_or_move_lines(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        model = replace(model, facts=replace(facts, bumps=head_facts.BumpsUnavailable("unreadable")))
        rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertTrue(all("↑" not in row for row in rows))
        self.assertEqual(queue_view.move_lines(model), [])

    def test_unknown_facts_are_silent_and_known_facts_each_add_one_line(self) -> None:
        model = self.fixture.model()
        text = plain(queue_view.render_view(model, 400))
        for unknown in ("unknown", "no bumps since", "no cargo token wait", "Head for"):
            self.assertNotIn(unknown, text)
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        wait = head_facts.CargoTokenWait("id-work-lead", "nextest", 1001, "1001", NOW - 200,
                                       head_facts.TokenReturned(NOW - 46))
        model = replace(model, facts=replace(facts, head_time=head_facts.RecordedHeadTime(NOW - 125),
            token_waits=head_facts.RecordedTokenWaits((wait,), False, (wait,))))
        text = plain(queue_view.render_view(model, 400))
        self.assertEqual(text.count("Head for"), 1)
        self.assertIn("2 min (since 10:44:07)", text)
        self.assertEqual(text.count("cargo token: waited 154 s (10:42-10:45)"), 1)

    def test_no_pace_displays_dashes_and_the_explicit_footer(self) -> None:
        for pace in (head_facts.RecordedPace(0), head_facts.PaceUnavailable()):
            with self.subTest(pace=pace):
                model = replace(self.fixture.model(), pace=pace)
                text = plain(queue_view.render_view(model, 140))
                self.assertIn("starts: no step ended in the last hour", text)
                self.assertFalse(any("~" in row for row in waiting_rows(queue_view.render_view(model, 140))))

    def test_empty_queue_has_no_head_or_invented_steps(self) -> None:
        model = self.fixture.model()
        reading = gate.QueueRead(gate.QueueView((), (), 24 * gate.GIB, 0, 3 * gate.GIB, NOW),
            replace(model.reading.explanation, running=(), head=gate.NoWaiter()), [])
        lines = queue_view.render_view(replace(model, reading=reading, facts=gate.NoWaiter()), 100)
        text = plain(lines)
        self.assertIn("0 running · 0 waiting · nothing waits", text)
        self.assertIn("Waiting by production: none", text)
        self.assertEqual(waiting_rows(lines), [])
        self.assertNotIn("#1 waits", text)
        self.assertNotIn("●", text)

    def test_checks_use_admissions_numbers_without_redeciding(self) -> None:
        model = self.fixture.model()
        with mock.patch.object(gate, "decide", side_effect=AssertionError("admission decides once")), \
             mock.patch.object(gate, "build_charge", side_effect=AssertionError("recorded charge")):
            text = plain(queue_view.render_view(model, 400))
        self.assertIn("11.1 of 21.0 GiB held; needs 11.5", text)
        self.assertIn("needs 20.5", text)


if __name__ == "__main__":
    _ = unittest.main()
