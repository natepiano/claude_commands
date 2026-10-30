"""A card answers whether the selected step is next and explains its place."""
from __future__ import annotations

from dataclasses import replace
import unittest

from scripts.build_queue import head_facts, queue_card
from scripts.build_queue.queue_fixture import GOLDEN, NOW, QueueFixtureTests, queue_read_fixture
from scripts.build_queue.queue_lines import plain, terminal_width
from scripts.lint import memory_admit as gate


class QueueCardTests(QueueFixtureTests):
    def test_head_and_fourth_card_match_the_captured_goldens(self) -> None:
        model = self.fixture.model()
        for position in (1, 4):
            with self.subTest(position=position):
                self.assertEqual(plain(queue_card.render_card(model, [position], 100)) + "\n",
                                 (GOLDEN / f"card_{position}.txt").read_text())

    def test_first_card_answers_next_and_shares_all_head_checks(self) -> None:
        text = plain(queue_card.render_card(self.fixture.model(), [1], 400))
        self.assertIn("Next? Yes, it is first. It starts when the checks below pass.", text)
        self.assertIn("✗ build budget: 11.1 of 21.0 GiB held; needs 11.5", text)
        self.assertIn("✓ free memory 24.5 GiB; needs 20.5", text)
        self.assertIn("✗ memory pressure: full avg10 7.7", text)
        self.assertNotIn("/build:queue bump", text)

    def test_admitted_head_answers_that_it_starts_at_the_next_check(self) -> None:
        model = self.fixture.model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        head = replace(head, decision=replace(head.decision, state="admit", pressure=gate.NoPressureHold(),
            budget_passes=True), budget=replace(head.budget, passes=True))
        model = replace(model, reading=replace(model.reading,
                        explanation=replace(model.reading.explanation, head=head)))
        text = plain(queue_card.render_card(model, [1], 100))
        self.assertIn("Next? Yes. It starts at the next check.", text)
        self.assertIn("✓ memory pressure: no hold", text)

    def test_ready_follower_answers_yes_while_a_larger_head_waits_for_memory(self) -> None:
        head = gate.WaitingStep(101, "large-id", "large", "nextest", "/tmp/large", NOW - 120,
            15 * gate.GIB, gate.NeedsMemory(15 * gate.GIB, 10 * gate.GIB,
                threshold=18 * gate.GIB, reserve=3 * gate.GIB))
        follower = gate.WaitingStep(102, "small-id", "small", "clippy", "/tmp/small", NOW - 60,
            gate.GIB, gate.ReadyForAdmission())
        reading = queue_read_fixture(gate.QueueView((head, follower), (), 10 * gate.GIB, 0,
                                                    3 * gate.GIB, NOW))
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_card.render_card(model, [2], 400))
        self.assertIn("Next? Yes. It starts at the next check.", text)
        self.assertNotIn("Next? No.", text)
        self.assertIn("#1    large nextest", text)

    def test_fourth_card_counts_moves_and_join_order_without_inventing_checks(self) -> None:
        text = plain(queue_card.render_card(self.fixture.model(), [4], 400))
        self.assertIn("Next? No. 3 steps are ahead of it: 2 moved up, 1 joined earlier.", text)
        self.assertIn("joined 10:07, 2 min before it", text)
        self.assertIn("It joined 2nd of the steps waiting now.", text)
        self.assertIn("✗ #1's turn", text)
        self.assertIn("strict until 10:56:00 (9 min 48 s left): younger steps wait", text)
        self.assertNotIn("build budget", text)  # These per-step checks arrive in Part B.
        self.assertNotIn("free memory", text)
        self.assertIn("Likely start ~10:51: 4th in line at 44 steps an hour, an estimate.", text)

    def test_unrecorded_moved_step_that_joined_later_is_said_to_join_after_it(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        model = replace(model, facts=replace(facts, bump_records=()))
        text = plain(queue_card.render_card(model, [4], 400))
        self.assertIn("#1 ↑  tool-based-ui · cables · work-lead nextest  joined 10:40, 30 min after it", text)
        self.assertNotIn("0 min before it", text)

    def test_session_selector_prints_each_step_as_a_card_with_a_blank_separator(self) -> None:
        text = plain(queue_card.render_selection(self.fixture.model(), "work-lead", 400))
        self.assertEqual(text.count(" of 32 ·"), 2)
        self.assertIn("#1 of 32", text)
        self.assertIn("\n\n#2 of 32", text)
        self.assertIn("Folder /tmp/work.", text)
        self.assertIn("Folder /tmp/measure-repair2.", text)

    def test_position_id_recorded_name_and_folder_select_the_same_step(self) -> None:
        model = self.fixture.model()
        expected = plain(queue_card.render_card(model, [4], 100))
        for selector in ("4", "id-cargo-liner-berth-parallel-impl", "cargo-liner-berth-parallel-impl",
                         "/home/natepiano/rust/cargo-liner/repo", "repo"):
            with self.subTest(selector=selector):
                self.assertEqual(plain(queue_card.render_selection(model, selector, 100)), expected)
        running = model.reading.explanation.running[0]
        step = replace(running.step, session_name="recorded-widget")
        model = replace(model, reading=replace(model.reading,
            view=replace(model.reading.view, running=(step,)),
            explanation=replace(model.reading.explanation, running=(replace(running, step=step),))))
        expected = plain(queue_card.render_selection(model, step.session_id, 400))
        self.assertIn("runs now:", expected)
        for selector in ("recorded-widget", step.folder, "widget"):
            with self.subTest(running_selector=selector):
                self.assertEqual(plain(queue_card.render_selection(model, selector, 400)), expected)

    def test_running_selector_prints_a_single_running_sentence(self) -> None:
        text = plain(queue_card.render_selection(self.fixture.model(), "id-widget", 400))
        self.assertEqual(text, "tool-based-ui · widget · widget-enhancements-title nextest runs now: "
                         + "started 10:45 (0 min ago), using 5.1 GiB, held 11.1 GiB.")
        self.assertNotIn("Next?", text)

    def test_no_match_is_a_plain_explanation(self) -> None:
        self.assertEqual(plain(queue_card.render_selection(self.fixture.model(), "not-a-step", 100)),
                         "No waiting or running step matches not-a-step.")

    def test_rows_ahead_cap_at_five_and_report_the_remainder(self) -> None:
        text = plain(queue_card.render_card(self.fixture.model(), [9], 400))
        self.assertIn("8 steps are ahead of it: 2 moved up, 6 joined earlier.", text)
        for place in range(1, 6):
            self.assertIn(f"\n  #{place} ", text)
        self.assertNotIn("\n  #6 ", text)
        self.assertIn("and 3 more", text)

    def test_card_wraps_every_fact_instead_of_cutting_a_step_to_a_record(self) -> None:
        for columns in (30, 60, 100):
            with self.subTest(columns=columns):
                lines = queue_card.render_card(self.fixture.model(), [4], columns)
                text = " ".join(plain(lines).split())
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                self.assertIn("Next? No. 3 steps are ahead of it: 2 moved up, 1 joined earlier.", text)
                self.assertIn("Folder/home/natepiano/rust/cargo-liner/repo.", text.replace(" ", ""))

    def test_missing_pace_never_invents_a_likely_start(self) -> None:
        for pace in (head_facts.RecordedPace(0), head_facts.PaceUnavailable()):
            with self.subTest(pace=pace):
                model = replace(self.fixture.model(), pace=pace)
                text = plain(queue_card.render_card(model, [4], 400))
                self.assertIn("Likely start —.", text)
                self.assertNotIn("Likely start ~", text)
                self.assertNotIn("adds about", text)


if __name__ == "__main__":
    _ = unittest.main()
