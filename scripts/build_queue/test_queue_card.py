"""A card answers whether the selected step is next and explains its place."""
from __future__ import annotations

from scripts.lint.memory_test_support import admission_policy_fixture

from dataclasses import replace
import unittest

from scripts.build_queue import head_facts, queue_card, queue_evidence, queue_view
from scripts.build_queue.queue_fixture import GOLDEN, NOW, QueueFixtureTests, queue_read_fixture, with_waiting
from scripts.build_queue.queue_lines import plain
from scripts.build_queue.scope_activity import LiveBuildRun, ScopeProcesses
from scripts.lib.terminal_cells import terminal_width
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels


class QueueCardTests(QueueFixtureTests):

    def test_running_card_names_the_private_machine_after_pid(self) -> None:
        model = self.fixture.model()
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_card.render_selection(model, "1", columns)
                text = plain(lines)
                record = " ".join(text.split())
                self.assertIn("pid 900 · fixture-host · tool-based-ui / widget / widget-enhancements-title, hana nextest:", record)
                self.assertEqual(record.count("pid 900"), 1)
                self.assertEqual(record.count("0 min ago"), 1)
                self.assertNotIn("min machine", text)
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))

    def test_project_names_appear_in_the_heading_steps_ahead_and_first_in_line(self) -> None:
        model = self.fixture.model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, project="nateroids", step="nextest"), replace(second, project="hana")])
        head = model.reading.explanation.head
        self.assertIsInstance(head, gate.HeadExplanation)
        if isinstance(head, gate.HeadExplanation):
            model = replace(model, reading=replace(model.reading,
                explanation=replace(model.reading.explanation,
                    head=replace(head, waiter=model.reading.view.waiting[0]))))
        text = plain(queue_card.render_card(model, [2], 400))
        self.assertIn("hana nextest", text.splitlines()[0])
        ahead, = [line for line in text.splitlines() if "#1 pid" in line]
        self.assertIn("nateroids nextest", ahead)
        first_line, = [line for line in text.splitlines() if "First in line:" in line]
        self.assertIn("nateroids nextest", first_line)

    def test_unit_priority_cards_use_the_unit_and_saved_why_without_legacy_move_reasons(self) -> None:
        model = self.fixture.model()
        for position in (1, 2, 4):
            with self.subTest(position=position):
                text = plain(queue_card.render_card(model, [position], 400))
                self.assertEqual(text.count("queue priority: cables (captured priority)"), 1)
                self.assertNotIn("moved up", text)
                self.assertNotIn("from #27", text)
                self.assertNotIn("from #1 by", text)
                self.assertNotIn("the user's bump", text)
                if position > 1:
                    self.assertIn(f"{min(position - 1, 3)} of cables (queue priority)", text)
                need = "11.5" if position == 2 else "7.6"
                self.assertIn(f"Room: need {need} GiB", text)
                self.assertNotIn("Build budget:", text)

    def test_follower_names_the_eligible_heads_place_after_a_token_waiter(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        (token_path, token), (head_path, head), (follower_path, follower) = inputs.ordered[:3]
        inputs = replace(inputs,
            host=gate.HostMemory(14 * gate.GIB, 60 * gate.GIB), pressure=gate.NoPressureHold(),
            running=(replace(inputs.running[0], promise=gate.GIB, measured=gate.MeasuredMemory(gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB))),),
            reservations=({**inputs.reservations[0], "need": gate.GIB},), ordered=[
                (token_path, {**token, "token_left_at": NOW - 10}),
                (head_path, {**head, "need": 21 * gate.GIB, "priority_since": NOW - 60}),
                (follower_path, {**follower, "need": 10 * gate.GIB}),
            ])
        reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        self.assertEqual([step.pid for step in reading.view.waiting], [token["pid"], head["pid"], follower["pid"]])
        self.assertIsInstance(reading.view.waiting[2].reason, gate.BehindHead)
        self.assertEqual(queue_view.reason_phrase(reading.view.waiting[2].reason, reading.view.waiting), "behind #2")
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_card.render_card(model, [3], 400))
        self.assertIn("#2's turn", text)
        self.assertNotIn("#1's turn", text)
        self.assertIn("younger runs start only if room still covers the first run's need of 21.0 GiB", text)

    def test_unit_override_card_heading_matches_golden(self) -> None:
        model = self.fixture.model()
        waiting = model.reading.view.waiting
        step = replace(waiting[0], unit="queue-unit", step="merge-gate")
        model = replace(model, labels={**model.labels, step.session_id: UnitLabels("gate", "builds")},
            reading=replace(model.reading, view=replace(model.reading.view, waiting=(step, *waiting[1:]))))
        heading = plain(queue_card.render_selection(model, "1", 140)).splitlines()[0]
        self.assertEqual(heading + "\n", (GOLDEN / "unit_override_card.txt").read_text())
    def test_behind_head_check_survives_missing_optional_head_facts(self) -> None:
        model = self.fixture.model()
        follower = replace(model.reading.view.waiting[1], reason=gate.BehindHead("head", NOW - 600, "head-id"))
        head = model.reading.view.waiting[0]
        available = 16 * gate.GIB
        waiting = (replace(head, reason=gate.NeedsMemory(head.need, available)),
                   follower, *model.reading.view.waiting[2:])
        reading = queue_read_fixture(replace(model.reading.view, waiting=waiting, available=available),
                                     policy=model.reading.explanation.policy)
        model = replace(model, facts=gate.NoWaiter(), reading=reading,
                        release=gate.smallest_head_release(reading.explanation))
        text = plain(queue_card.render_selection(model, "2", 400))
        self.assertIn("✗ #1's turn", text)

    def test_missing_recorded_checks_return_unavailable_without_hiding_other_checks(self) -> None:
        model = self.fixture.model()
        explanation = model.reading.explanation
        follower = model.reading.view.waiting[1]
        recorded = next(item for item in explanation.line.steps if item.waiter == follower)
        other = next(item for item in explanation.line.steps
                     if item.waiter == model.reading.view.waiting[2])
        remaining = tuple(item for item in explanation.line.steps if item != recorded)
        model = replace(model, reading=replace(model.reading,
            explanation=replace(explanation, line=replace(explanation.line, steps=remaining))))
        checks = queue_view.step_checks(model, 2)
        self.assertEqual(checks, gate.ReadingUnavailable("step admission checks unavailable"))
        self.assertEqual(plain(queue_view.check_lines(checks, model)), "step admission checks unavailable")
        self.assertEqual(queue_view.step_checks(model, 3), other)
        text = plain(queue_card.render_selection(model, "2", 400))
        self.assertIn("#2 of 32", text)
        self.assertIn("step admission checks unavailable", text)

    def test_unowned_slice_owner_selector_finds_its_record_without_a_view_row(self) -> None:
        model = self.fixture.model()
        running = model.reading.explanation.running[0]
        run = LiveBuildRun(123, "123", NOW - 600)
        activity = ScopeProcesses(1, "run-owner", "run-owner", "/tmp/run", "bash", NOW - 600, False, run)
        memory = gate.UnownedSliceMemory(5 * gate.GIB, cgroup="verify.scope", owner="run-owner", activity=activity, pid=run.pid)
        step = replace(running.step, session_id="run-owner", session_name="run-owner", measured=memory,
                       step="between steps", promise=0, run=run)
        reading = queue_read_fixture(replace(model.reading.view, running=(step,), waiting=(), promised=0),
                                     policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        model = replace(model, labels={**model.labels, "run-owner": UnitLabels("worker", "builds", "run-unit")})
        view = queue_view.render_view(model, 400, selector="run-owner")
        card = queue_card.render_selection(model, "run-owner", 400)
        text = plain([*view, (), *card])
        self.assertIn("Selected running step: ", plain(card))
        self.assertIn("run-unit", plain(card))
        self.assertNotIn("Selected running step: ", plain(view))
        self.assertNotIn("▸", plain(view))
        self.assertNotIn("(unowned)", text)
        self.assertNotIn("run-unit", plain(view))
        self.assertNotIn("run-owner", text)
        self.assertNotIn("No waiting or running step matches", text)
        selected, = [entry for entry in queue_card.page_running_steps(model)
                     if queue_view.running_matches_selector(entry.step, "run-owner", model.labels)]
        self.assertEqual(selected.step, step)
        self.assertNotIn("between steps", plain(view))
        self.assertIn("5.0", plain(card))
        self.assertNotIn("run-owner", plain(card))
        other = replace(model, reading=queue_read_fixture(
            replace(model.reading.view, waiting=(self.fixture.model().reading.view.waiting[0],)),
            policy=admission_policy_fixture()))
        unselected = plain(queue_card.render_selection(other, "1", 400))
        self.assertNotIn("Selected running step: ", unselected)
        self.assertNotIn("run-unit", unselected)
        self.assertNotIn("between steps", unselected)

    def test_step_ahead_keeps_its_run_place_instead_of_joining_earlier(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        first_path, first = inputs.ordered[0]
        second_path, second = inputs.ordered[1]
        first = {**first, "priority_since": 0, "priority_rank": 1,
                 "since": NOW - 60, "order_since": NOW - 600}
        second = {**second, "priority_since": 0, "priority_rank": 1,
                  "since": NOW - 300}
        reading = gate.queue_read_from(replace(inputs, ordered=[(first_path, first), (second_path, second)]), policy=admission_policy_fixture())
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_card.render_selection(model, "2", 400))
        selected = text.split("\nHost:", 1)[0]
        self.assertIn("keeps its run's place", selected)
        self.assertNotIn("1 joined earlier", selected)
        self.assertIn("#1", selected)

    def test_expired_follower_prints_estimate_before_its_own_resource_checks(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        path, row = inputs.ordered[1]
        reading = gate.queue_read_from(replace(inputs, ordered=[inputs.ordered[0],
            (path, {**row, "estimated_at": NOW - 600})]), policy=admission_policy_fixture())
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_card.render_card(model, [2], 400))
        self.assertIn("✗ estimate       the estimate expired at 10:36; the client refreshes it through the admission service", text)
        self.assertLess(text.index("✗ estimate"), text.index("Room:"))
        self.assertLess(text.index("✗ estimate"), text.index("Room:"))

    def test_a_smaller_follower_keeps_a_failing_order_check(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        path, head = inputs.ordered[0]
        follower_path, follower = inputs.ordered[1]
        reading = gate.queue_read_from(replace(inputs,
            host=gate.HostMemory(10 * gate.GIB, 60 * gate.GIB), pressure=gate.NoPressureHold(),
            running=(replace(inputs.running[0], promise=gate.GIB, measured=gate.MeasuredMemory(gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB))),),
            reservations=({**inputs.reservations[0], "need": gate.GIB},), ordered=[
                (path, {**head, "need": 15 * gate.GIB}),
                (follower_path, {**follower, "need": gate.GIB})]), policy=admission_policy_fixture())
        self.assertIsInstance(reading.view.waiting[1].reason, gate.BehindHead)
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_card.render_card(model, [2], 400))
        self.assertIn("✗ #1's turn", text)
        self.assertNotIn("younger steps wait until", text)

    def test_multiple_matches_share_one_evidence_and_release_block(self) -> None:
        model = self.fixture.model()
        text = plain(queue_card.render_selection(model, "work-lead", 400))
        self.assertIn("#2 of 32", text)
        self.assertIn("#3 of 32", text)
        for fact in ("Host: available RAM", "Host load:", "Running:",
                     "Total current use:", "Need source:", "Cargo token waits:", "Frees when:"):
            self.assertEqual(text.count(fact), 1, fact)

        self.assertEqual(text.count(" of 32 ·"), 2)
        self.assertIn("#2 of 32", text)
        self.assertIn("\n\n#3 of 32", text)
        self.assertIn("Folder /tmp/work.", text)
        self.assertIn("Folder /tmp/measure-repair2.", text)

    def test_head_and_fourth_card_match_the_captured_goldens(self) -> None:
        model = self.fixture.model()
        for position in (1, 4):
            with self.subTest(position=position):
                page = [*queue_view.render_view(model, 100), (), *queue_card.render_selection(model, str(position), 100)]
                self.assertEqual(plain(page) + "\n",
                                 (GOLDEN / f"card_{position}.txt").read_text())

    def test_first_card_answers_next_and_shares_all_head_checks(self) -> None:
        text = plain(queue_card.render_card(self.fixture.model(), [1], 400))
        self.assertIn("Next? Yes, it is first. It starts when the checks below pass.", text)
        self.assertIn("Room: need 7.6 GiB", text)
        self.assertNotIn("Build budget:", text)
        self.assertIn("✗ memory pressure: full avg10 7.7", text)
        self.assertNotIn("/build:queue bump", text)

    def test_admitted_head_answers_that_it_starts_at_the_next_check(self) -> None:
        model = self.fixture.model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        head = replace(head, decision=replace(head.decision, state="admit", pressure=gate.NoPressureHold(),
            free_passes=True), room=replace(head.room, passes=True, basis=gate.Fits()),
            cause=gate.ReadyForAdmission())
        host = model.reading.explanation.host
        assert isinstance(host, gate.HostAvailable)
        model = replace(model, reading=replace(model.reading,
                        explanation=replace(model.reading.explanation, head=head,
                            host=replace(host, pressure=gate.NoPressureHold()),
                            decided_from=replace(model.reading.explanation.decided_from,
                                pressure=gate.NoPressureHold()))))
        text = plain(queue_card.render_card(model, [1], 100))
        self.assertIn("Next? Yes. It starts at the next check.", text)
        self.assertIn("✓ memory pressure: no hold", text)

    def test_a_follower_card_names_the_protected_head(self) -> None:
        head = gate.WaitingStep(101, "large-id", "large", "nextest", "/tmp/large", NOW - 120,
            15 * gate.GIB, gate.NeedsMemory(15 * gate.GIB, 10 * gate.GIB,
                threshold=18 * gate.GIB, reserve=3 * gate.GIB))
        follower = gate.WaitingStep(102, "small-id", "small", "clippy", "/tmp/small", NOW - 60,
            gate.GIB, gate.BehindHead("large", head.since, head.session_id))
        reading = queue_read_fixture(gate.QueueView((head, follower), (), 10 * gate.GIB, 0,
                                                    3 * gate.GIB, NOW), policy=admission_policy_fixture())
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_card.render_card(model, [2], 400))
        self.assertIn("Next? No.", text)
        self.assertNotIn("Next? Yes.", text)
        self.assertIn("#1 pid 101 ·    large nextest", text)

    def test_fourth_card_counts_moves_and_join_order_with_recorded_checks(self) -> None:
        text = plain(queue_card.render_card(self.fixture.model(), [4], 400))
        self.assertIn("Next? No. 3 steps are ahead of it: 3 of cables (queue priority), 0 joined earlier.", text)
        self.assertIn("joined 10:10, 3 min after it", text)
        self.assertIn("It joined 1st of the steps waiting now.", text)
        self.assertIn("✓ queue order", text)
        self.assertIn("Room: need 7.6 GiB", text)
        self.assertNotIn("Build budget:", text)
        self.assertIn("✗ memory pressure: full avg10 7.7%, some avg10 0.0%, swap used 0.0%; starts below full avg10 5.0%", text)
        self.assertIn("Likely start ~10:51: 4th in line at 44 steps an hour, an estimate.", text)

    def test_unrecorded_moved_step_that_joined_later_is_said_to_join_after_it(self) -> None:
        model = self.fixture.model()
        facts = model.facts
        assert isinstance(facts, head_facts.HeadFacts)
        model = replace(model, facts=replace(facts, bump_records=()))
        text = plain(queue_card.render_card(model, [4], 400))
        self.assertIn("#2 pid 1001 · ↑  tool-based-ui · cables · work-lead nextest  joined 10:40, 32 min after it", text)
        self.assertNotIn("0 min before it", text)

    def test_position_id_recorded_name_and_folder_select_the_same_step(self) -> None:
        model = self.fixture.model()
        expected = plain(queue_card.render_card(model, [5], 100))
        for selector in ("5", "id-cargo-liner-berth-parallel-impl", "cargo-liner-berth-parallel-impl",
                         "/home/natepiano/rust/cargo-liner/repo", "repo"):
            with self.subTest(selector=selector):
                self.assertEqual(plain(queue_card.render_selection(model, selector, 100)), expected)
        running = model.reading.explanation.running[0]
        step = replace(running.step, session_name="recorded-widget")
        model = replace(model, reading=replace(model.reading,
            view=replace(model.reading.view, running=(step,)),
            explanation=replace(model.reading.explanation, running=(replace(running, step=step),))))
        expected = plain(queue_card.render_selection(model, step.session_id, 400))
        self.assertIn("Selected running step:", expected)
        for selector in ("recorded-widget", step.folder, "widget"):
            with self.subTest(running_selector=selector):
                self.assertEqual(plain(queue_card.render_selection(model, selector, 400)), expected)

    def test_follower_card_prints_its_recorded_checks_without_recomputing_admission(self) -> None:
        model = self.fixture.model()
        follower = model.reading.explanation.line.steps[3]
        self.assertEqual(follower.waiter, model.reading.view.waiting[4])
        self.assertEqual(follower.room.need, int(2.9 * gate.GIB))
        self.assertEqual(follower.free.need, int(2.9 * gate.GIB))
        from unittest import mock
        release = gate.smallest_head_release(model.reading.explanation)
        with mock.patch.object(gate, "smallest_head_release", return_value=release), \
             mock.patch.object(gate, "admission_pass", side_effect=AssertionError("card recalculated admission")):
            text = plain(queue_card.render_card(model, [5], 400))
        self.assertIn(queue_evidence.room_check_text(follower.room), text)
        self.assertNotIn("Build budget:", text)
        self.assertIn("memory pressure: full avg10 7.7", text)

    def test_unavailable_selected_checks_keep_valid_head_evidence_separate(self) -> None:
        model = self.fixture.model()
        fourth = model.reading.view.waiting[3]
        for host, reason in ((gate.ReadingUnavailable(), fourth.reason),
                             (model.reading.explanation.host, gate.ReadingUnavailable())):
            with self.subTest(host=host, reason=reason):
                waiting = (*model.reading.view.waiting[:3], replace(fourth, reason=reason),
                           *model.reading.view.waiting[4:])
                reading = replace(model.reading, view=replace(model.reading.view, waiting=waiting),
                                  explanation=replace(model.reading.explanation, host=host))
                text = plain(queue_card.render_card(replace(model, reading=reading), [4], 400))
                self.assertIn("memory reading unavailable", text)
                selected = text.split("\nHost:", 1)[0]
                self.assertNotIn("Room:", selected)
                self.assertNotIn("Room:", selected)
                if isinstance(host, gate.ReadingUnavailable):
                    self.assertNotIn("Build budget:", text)
                    self.assertNotIn("Free memory:", text)
                else:
                    self.assertEqual(text.count("Room:"), 1)
                    self.assertEqual(text.count("Room:"), 1)
                    self.assertIn("First in line:", text)

    def test_shown_name_match_wins_over_recorded_identity_and_numeric_place(self) -> None:
        model = self.fixture.model()
        first, fourth = model.reading.view.waiting[0], model.reading.view.waiting[3]
        from scripts.message.unit_labels import UnitLabels
        labels = {**model.labels, fourth.session_id: UnitLabels(first.session_name)}
        self.assertEqual(queue_card.select_waiters(replace(model, labels=labels), first.session_name), [1, 4, 23])
        labels = {**model.labels, fourth.session_id: UnitLabels("1")}
        self.assertEqual(queue_card.select_waiters(replace(model, labels=labels), "1"), [4, 23])


    def test_running_selector_prints_one_complete_running_evidence_record(self) -> None:
        text = plain(queue_card.render_selection(self.fixture.model(), "id-widget", 400))
        expected = ("Selected running step: pid 900 · fixture-host · tool-based-ui / widget / widget-enhancements-title, hana nextest: "
                    "admitted 10:45 (0 min ago); need 11.1 GiB; anonymous use 5.1 GiB; "
                    "held slot 0.0 GiB; kept aside 6.0 GiB")
        self.assertEqual(text.count(expected), 1)
        self.assertNotIn("Next?", text)

    def test_no_match_is_a_plain_explanation(self) -> None:
        self.assertEqual(plain(queue_card.render_selection(self.fixture.model(), "not-a-step", 100)),
                         "No waiting or running step matches not-a-step.")

    def test_rows_ahead_cap_at_five_and_report_the_remainder(self) -> None:
        text = plain(queue_card.render_card(self.fixture.model(), [9], 400))
        self.assertIn("8 steps are ahead of it: 3 of cables (queue priority), 5 joined earlier.", text)
        for place in range(1, 6):
            self.assertIn(f"\n  #{place} ", text)
        self.assertNotIn("\n  #6 ", text)
        self.assertIn("and 3 more", text)

    def test_card_wraps_every_fact_instead_of_cutting_a_step_to_a_record(self) -> None:
        for columns in (30, 60, 100):
            with self.subTest(columns=columns):
                lines = queue_card.render_card(self.fixture.model(), [5], columns)
                text = " ".join(plain(lines).split())
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                self.assertIn("Next? No. 4 steps are ahead of it: 3 of cables (queue priority), 1 joined earlier.", text)
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
