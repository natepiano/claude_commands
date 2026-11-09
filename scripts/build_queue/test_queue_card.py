"""A card answers whether the selected step is next and explains its place."""
from __future__ import annotations

from scripts.build_queue import admission_records

from scripts.lint.memory_test_support import admission_policy_fixture

from dataclasses import replace
import unittest
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts, queue_card, queue_evidence, queue_view
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.test_queue_view import QueueViewFixtureTests, cloud_reading
from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted
from scripts.build_queue.queue_fixture import GOLDEN, NOW, QueueFixture, queue_read_fixture, with_waiting, write_memory_config
from scripts.build_queue.queue_lines import plain
from scripts.build_queue.scope_activity import LiveBuildRun, ScopeProcesses
from scripts.lib.build_steps import read_step_names
from scripts.lib.terminal_cells import terminal_width
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels


class CloudQueueCardTests(unittest.TestCase):
    def test_cloud_card_reads_progress_from_a_private_service(self) -> None:
        with AdmissionFixture() as fixture:
            sidecar = fixture.root / "cloud.cgroup"
            _ = sidecar.write_text("")
            request = fixture.request(step="merge-gate")
            request = replace(request, place=wire.CloudPlace("aws"), sidecar=str(sidecar),
                identity=replace(request.identity, session_id="private-cloud", unit="private-unit"))
            grant = only_admitted(fixture.connect(request).replies())
            progress_profile.publish_counter(sidecar, progress_profile.ProgressCounter("modules", 7, 12))
            cloud_progress.publish(sidecar, cloud_progress.CloudProgress("running", 32, 1, fixture.clock.now()))
            _ = fixture.run_pass()
            reply, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(reply, wire.ViewReply)
            model = queue_view.QueueModel(reply.reading, {}, admission_records.NoWaiter(), head_facts.PaceUnavailable(),
                ZoneInfo("UTC"), queue_view.HostLoadUnavailable(), admission_records.NoWaiter())
            text = plain(queue_card.render_selection(model, "private-cloud", 140))
            for value in ("Cloud", "1 of 9 slots", "private-unit", read_step_names().shown("merge-gate"),
                          "running", "7 of 12 modules", "32 vCPUs"):
                self.assertIn(value, text)
            self.assertEqual(grant.need, 0)
            fixture.release(grant)

    def cloud_model(self) -> tuple[queue_view.QueueModel, QueueFixture]:
        fixture = self.enterContext(QueueFixture(captured=False))
        write_memory_config(fixture.root / "build_admission.conf",
            cloud_gate_slots="1", cloud_gate_stale_pushes="3")
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        model = fixture.model()
        return replace(model, reading=cloud_reading(model.reading)), fixture

    def test_cloud_card_shows_slots_progress_size_elapsed_and_age(self) -> None:
        model, _fixture = self.cloud_model()
        text = plain(queue_card.render_selection(model, "cloud-session", 140))
        for value in ("Cloud", "1 of 1 slots", "cloud-unit", read_step_names().shown("merge-gate"),
                      "running", "32", "vCPUs", "4 min"):
            self.assertIn(value, text)
        self.assertRegex(text, r"7(?: of |/)12")
        self.assertIn("no update for 2 min", text)
        self.assertEqual(text + "\n", (GOLDEN / "cloud_card.txt").read_text())

    def test_waiting_cloud_card_names_the_full_slots(self) -> None:
        model, _fixture = self.cloud_model()
        text = plain(queue_card.render_selection(model, "cloud-waiter", 140))
        self.assertIn("Cloud", text)
        self.assertIn("waiting-unit", text)
        self.assertIn(read_step_names().shown("targeted-test"), text)
        self.assertIn("waits for a cloud slot: 1 of 1 in use", text)
        self.assertEqual(text + "\n", (GOLDEN / "cloud_wait_card.txt").read_text())

    def test_missing_cloud_progress_has_a_named_card_state(self) -> None:
        model, _fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        for reason in ("missing", "unreadable"):
            with self.subTest(reason=reason):
                row = replace(reading.cloud[0], progress=cloud_progress.NoCloudProgress(reason))
                model = replace(model, reading=replace(reading, cloud=(row,)))
                text = plain(queue_card.render_selection(model, "cloud-session", 140))
                self.assertIn(reason, text)
                self.assertNotIn("no update for", text)

    def test_missing_push_file_or_key_omits_the_card_stale_age(self) -> None:
        model, fixture = self.cloud_model()
        config = fixture.root / "cloud_gate.conf"
        for missing in ("key", "file"):
            with self.subTest(missing=missing):
                if missing == "key":
                    _ = config.write_text("[cloud_gate]\n")
                else:
                    config.unlink()
                text = plain(queue_card.render_selection(model, "cloud-session", 140))
                self.assertIn("running", text)
                self.assertRegex(text, r"7(?: of |/)12")
                self.assertNotIn("no update for", text)


class QueueCardTests(QueueViewFixtureTests):

    def test_targeted_test_step_uses_its_shown_name(self) -> None:
        model = self.fixture.model()
        waiting = model.reading.view.waiting
        step = replace(waiting[0], step="targeted-test")
        model = replace(model, reading=replace(model.reading,
            view=replace(model.reading.view, waiting=(step, *waiting[1:]))))
        heading = plain(queue_card.render_card(model, [1], 140)).splitlines()[0]
        self.assertIn(" · targeted test · ", heading)
        self.assertNotIn("targeted-test", heading)

    def test_running_and_release_sentences_use_step_prose(self) -> None:
        _ = (self.fixture.root / "pressure").write_text("full avg10=0\nsome avg10=0\n")
        for position in range(1, len(self.fixture.paths) + 1):
            self.fixture.change(position, need=21 * admission_records.GIB)
        for step, prose in (("build", "release build"),):
            with self.subTest(step=step):
                self.fixture.write_running(used=int(5.1 * admission_records.GIB), need=int(11.1 * admission_records.GIB), step=step)
                model = self.fixture.model()
                text = plain(queue_card.render_card(model, [1], 400))
                self.assertIn(f", hana {prose}: admitted ", text)
                self.assertIn(f"Frees when: widget-enhancements-title hana {prose} end ", text)

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
        self.assertIsInstance(head, admission_records.HeadExplanation)
        if isinstance(head, admission_records.HeadExplanation):
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
            host=admission_records.HostMemory(14 * admission_records.GIB, 60 * admission_records.GIB), pressure=admission_records.NoPressureHold(),
            running=(replace(inputs.running[0], promise=admission_records.GIB, measured=admission_records.MeasuredMemory(admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB))),),
            reservations=({**inputs.reservations[0], "need": admission_records.GIB},), ordered=[
                (token_path, {**token, "token_left_at": NOW - 10}),
                (head_path, {**head, "need": 21 * admission_records.GIB, "priority_since": NOW - 60}),
                (follower_path, {**follower, "need": 10 * admission_records.GIB}),
            ])
        reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        self.assertEqual([step.pid for step in reading.view.waiting], [token["pid"], head["pid"], follower["pid"]])
        self.assertIsInstance(reading.view.waiting[2].reason, admission_records.BehindHead)
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
        follower = replace(model.reading.view.waiting[1], reason=admission_records.BehindHead("head", NOW - 600, "head-id"))
        head = model.reading.view.waiting[0]
        available = 16 * admission_records.GIB
        waiting = (replace(head, reason=admission_records.NeedsMemory(head.need, available)),
                   follower, *model.reading.view.waiting[2:])
        reading = queue_read_fixture(replace(model.reading.view, waiting=waiting, available=available),
                                     policy=model.reading.explanation.policy)
        model = replace(model, facts=admission_records.NoWaiter(), reading=reading,
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
        self.assertEqual(checks, admission_records.ReadingUnavailable("step admission checks unavailable"))
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
        memory = admission_records.UnownedSliceMemory(5 * admission_records.GIB, cgroup="verify.scope", owner="run-owner", activity=activity, pid=run.pid)
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
            host=admission_records.HostMemory(10 * admission_records.GIB, 60 * admission_records.GIB), pressure=admission_records.NoPressureHold(),
            running=(replace(inputs.running[0], promise=admission_records.GIB, measured=admission_records.MeasuredMemory(admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB))),),
            reservations=({**inputs.reservations[0], "need": admission_records.GIB},), ordered=[
                (path, {**head, "need": 15 * admission_records.GIB}),
                (follower_path, {**follower, "need": admission_records.GIB})]), policy=admission_policy_fixture())
        self.assertIsInstance(reading.view.waiting[1].reason, admission_records.BehindHead)
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
        assert isinstance(head, admission_records.HeadExplanation)
        head = replace(head, decision=replace(head.decision, state="admit", pressure=admission_records.NoPressureHold(),
            free_passes=True), room=replace(head.room, passes=True, basis=admission_records.Fits()),
            cause=admission_records.ReadyForAdmission())
        host = model.reading.explanation.host
        assert isinstance(host, admission_records.HostAvailable)
        model = replace(model, reading=replace(model.reading,
                        explanation=replace(model.reading.explanation, head=head,
                            host=replace(host, pressure=admission_records.NoPressureHold()),
                            decided_from=replace(model.reading.explanation.decided_from,
                                pressure=admission_records.NoPressureHold()))))
        text = plain(queue_card.render_card(model, [1], 100))
        self.assertIn("Next? Yes. It starts at the next check.", text)
        self.assertIn("✓ memory pressure: no hold", text)

    def test_a_follower_card_names_the_protected_head(self) -> None:
        head = admission_records.WaitingStep(101, "large-id", "large", "nextest", "/tmp/large", NOW - 120,
            15 * admission_records.GIB, admission_records.NeedsMemory(15 * admission_records.GIB, 10 * admission_records.GIB,
                threshold=18 * admission_records.GIB, reserve=3 * admission_records.GIB))
        follower = admission_records.WaitingStep(102, "small-id", "small", "clippy", "/tmp/small", NOW - 60,
            admission_records.GIB, admission_records.BehindHead("large", head.since, head.session_id))
        reading = queue_read_fixture(admission_records.QueueView((head, follower), (), 10 * admission_records.GIB, 0,
                                                    3 * admission_records.GIB, NOW), policy=admission_policy_fixture())
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
        self.assertEqual(follower.room.need, int(2.9 * admission_records.GIB))
        self.assertEqual(follower.free.need, int(2.9 * admission_records.GIB))
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
        for host, reason in ((admission_records.ReadingUnavailable(), fourth.reason),
                             (model.reading.explanation.host, admission_records.ReadingUnavailable())):
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
                if isinstance(host, admission_records.ReadingUnavailable):
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
