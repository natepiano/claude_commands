"""The fixed queue stays one row per step and explains waits once."""
from __future__ import annotations

from scripts.lint.memory_test_support import write_pressure_reading, admission_policy_fixture

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
import json
import os
import unittest
from typing import Self, cast, override
from unittest import mock

from scripts.build_queue.scope_activity import ScopeProcesses, NoLiveBuildRun, LiveBuildRun, IdleService, RunningServiceJob, NotServiceSlice
from scripts.buildlog import store
from scripts.build_queue import head_facts, queue_card, queue_view, queue_evidence
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.admission_test_support import only_admitted
from scripts.build_queue.queue_fixture import (
    ci_step,
    ASKING_SESSION, GOLDEN, NOW, QueueFixture, QueueFixtureTests, queue_read_fixture, waiting_rows, with_waiting,
)
from scripts.build_queue.queue_lines import json_form, plain, wrap
from scripts.lib.build_steps import read_step_names
from scripts.lib.terminal_cells import terminal_width
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import fake_memory_readings
from scripts.message.unit_labels import UnitLabels
from scripts.message import unit_labels
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


class CloudQueueViewTests(unittest.TestCase):
    def cloud_model(self) -> tuple[queue_view.QueueModel, QueueFixture]:
        fixture = self.enterContext(QueueFixture(captured=False))
        write_memory_config(fixture.root / "build_admission.conf",
            cloud_gate_slots="1", cloud_gate_stale_pushes="3")
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        model = fixture.model()
        rows = (
            wire.CloudRow(wire.Owner(700001, "100"),
                wire.Identity("cloud-session", "route-one", "merge-gate", "/private/cloud", "cloud-unit"),
                wire.CloudPlace("aws"), NOW - 245, NOW - 240,
                cloud_progress.CloudProgress("running", 32, 1, NOW - 120),
                progress_profile.ProgressCounter("modules", 7, 12), gate.ReadyForAdmission()),
            wire.CloudRow(wire.Owner(700002, "200"),
                wire.Identity("cloud-waiter", "route-two", "targeted-test", "/private/waiting", "waiting-unit"),
                wire.CloudPlace("aws"), NOW - 60, 0,
                cloud_progress.NoCloudProgress(), progress_profile.NoCounter(), wire.CloudSlotWait(1, 1)),
        )
        reading = model.reading
        return replace(model, reading=wire.CloudQueueRead(reading.view, reading.explanation, reading.ordered,
            reading.diagnostics, rows, 1, cloud_gate_stale_pushes=3)), fixture

    def test_cloud_group_shows_slots_progress_size_elapsed_and_wait(self) -> None:
        model, _fixture = self.cloud_model()
        names = read_step_names()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        assert isinstance(row.progress, cloud_progress.CloudProgress)
        expected = (GOLDEN / "cloud_view.txt").read_text()
        for failures, failure_text in ((1, "1 failed module"), (2, "2 failed modules")):
            with self.subTest(failures=failures):
                current = replace(row, progress=replace(row.progress, failed_modules=failures))
                model = replace(model, reading=replace(reading, cloud=(current, *reading.cloud[1:])))
                text = plain(queue_view.render_view(model, 140))
                self.assertIn("Cloud", text)
                self.assertIn("1 of 1 slots", text)
                for value in ("cloud-unit", names.shown("merge-gate"), "running", "waiting-unit",
                              names.shown("targeted-test"), "32", "vCPUs"):
                    self.assertIn(value, text)
                self.assertRegex(text, r"7(?: of |/)12")
                self.assertIn("4 min", text)
                self.assertIn("no update for 2 min", text)
                self.assertIn("waits for a cloud slot: 1 of 1 in use", text)
                self.assertEqual(text + "\n", expected.replace("1 failed module", failure_text))

    def test_missing_cloud_progress_has_a_named_display_state(self) -> None:
        model, _fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        for reason in ("missing", "unreadable"):
            with self.subTest(reason=reason):
                row = replace(reading.cloud[0], progress=cloud_progress.NoCloudProgress(reason))
                model = replace(model, reading=replace(reading, cloud=(row,)))
                text = plain(queue_view.render_view(model, 140))
                self.assertIn(reason, text)
                self.assertNotIn("no update for", text)

    def test_stale_age_uses_the_pass_threshold_and_the_current_push_interval(self) -> None:
        model, fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        assert isinstance(row.progress, cloud_progress.CloudProgress)
        for seconds, expected in ((14, False), (15, True), (16, True)):
            with self.subTest(seconds=seconds):
                current = replace(row, progress=replace(row.progress, updated_at=NOW - seconds))
                model = replace(model, reading=replace(reading, cloud=(current,)))
                text = plain(queue_view.render_view(model, 140))
                self.assertEqual("no update for" in text, expected)
        write_memory_config(fixture.root / "build_admission.conf", cloud_gate_stale_pushes="4")
        configuration = gate.read_admission_configuration()
        assert isinstance(configuration, gate.UsableAdmissionConfiguration)
        model = replace(model, reading=replace(model.reading,
            cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes))
        self.assertNotIn("no update for", plain(queue_view.render_view(model, 140)))
        write_memory_config(fixture.root / "build_admission.conf", cloud_gate_stale_pushes="3")
        configuration = gate.read_admission_configuration()
        assert isinstance(configuration, gate.UsableAdmissionConfiguration)
        model = replace(model, reading=replace(model.reading,
            cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes))
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=10\n")
        self.assertNotIn("no update for", plain(queue_view.render_view(model, 140)))

    def test_cloud_unit_uses_the_local_label_lookup_when_the_request_has_no_unit(self) -> None:
        model, _fixture = self.cloud_model()
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        row = replace(row, identity=replace(row.identity, unit=""))
        model = replace(model, reading=replace(reading, cloud=(row,)))
        with mock.patch.object(unit_labels, "unit_labels_for",
                return_value={row.identity.session_id: UnitLabels("route-label", unit="lookup-unit")}) as lookup:
            text = plain(queue_view.render_view(model, 140))
            card = plain(queue_card.render_selection(model, "cloud-session", 140))
        lookup.assert_called_once_with({row.identity.session_id}, os.environ)
        for rendered in (text, card):
            self.assertIn("lookup-unit", rendered)
            self.assertNotIn("route-one", rendered)

    def test_cloud_selection_accepts_the_folder_name_like_local_selection(self) -> None:
        model, _fixture = self.cloud_model()
        expected = plain(queue_card.render_selection(model, "cloud-session", 140))
        self.assertEqual(plain(queue_card.render_selection(model, "cloud", 140)), expected)
        self.assertIn("cloud-unit", expected)
        self.assertNotIn("waiting-unit", expected)

    def test_missing_push_file_or_key_omits_the_stale_age(self) -> None:
        model, fixture = self.cloud_model()
        config = fixture.root / "cloud_gate.conf"
        for missing in ("key", "file"):
            with self.subTest(missing=missing):
                if missing == "key":
                    _ = config.write_text("[cloud_gate]\n")
                else:
                    config.unlink()
                text = plain(queue_view.render_view(model, 140))
                self.assertIn("running", text)
                self.assertRegex(text, r"7(?: of |/)12")
                self.assertNotIn("no update for", text)


class QueueViewTests(QueueFixtureTests):

    def test_targeted_test_step_uses_its_shown_name(self) -> None:
        model = self.fixture.model()
        waiting = model.reading.view.waiting
        running = model.reading.view.running
        view = replace(model.reading.view,
            waiting=(replace(waiting[0], step="targeted-test"), *waiting[1:]),
            running=(replace(running[0], step="targeted-test"), *running[1:]))
        model = replace(model, reading=queue_read_fixture(view, policy=model.reading.explanation.policy))
        text = plain(queue_view.render_view(model, 140))
        rows = [row for row in text.splitlines() if "targeted test" in row]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row.startswith("run") for row in rows))
        self.assertNotIn("targeted-test", text)

    def test_running_machine_column_uses_the_private_host_and_preserves_names(self) -> None:
        model = self.fixture.cap_model()
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                text = plain(queue_view.render_view(model, columns))
                header, = [row for row in text.splitlines() if row.lstrip().startswith("# ")]
                rows = [row for row in text.splitlines() if row.startswith("run")]
                self.assertTrue(rows)
                if columns >= 100:
                    self.assertLess(header.index("min"), header.index("machine"))
                    self.assertLess(header.index("machine"), header.index("pid"))
                    for row in rows:
                        self.assertEqual(row.index("fixture-host"), header.index("machine"))
                        self.assertEqual(row.index("pid "), header.index("pid"))
                    for row in waiting_rows(queue_view.render_view(model, columns)):
                        self.assertEqual(row[header.index("machine"):header.index("pid")].strip(), "")
                else:
                    self.assertNotIn("machine", header)
                    self.assertNotIn("fixture-host", text)
                self.assertTrue(all(terminal_width(row) <= columns for row in text.splitlines()))

    def test_long_machine_name_drops_before_the_view_cuts_names(self) -> None:
        model = self.fixture.cap_model()
        baseline = plain(queue_view.render_view(model, 100))
        with mock.patch.object(store, "host_name", return_value="private-machine-with-a-long-name"):
            text = plain(queue_view.render_view(model, 100))
        header, = [row for row in text.splitlines() if row.lstrip().startswith("# ")]
        self.assertNotIn("machine", header)
        before_rows = [row for row in baseline.splitlines() if "pid " in row]
        after_rows = [row for row in text.splitlines() if "pid " in row]
        self.assertEqual([row.split()[:5] for row in before_rows],
                         [row.split()[:5] for row in after_rows])
        self.assertIn("metrics-gathering", text)
        self.assertIn("metrics-gathering", baseline)

    def test_project_step_text_sets_the_column_width_and_keeps_rows_aligned(self) -> None:
        model = self.fixture.cap_model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, project="nateroids", step="nextest"),
            replace(second, project="long-project-name", step="nextest")])
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(model, columns)
                rows = waiting_rows(lines)
                self.assertIn("nateroids nextest", rows[0])
                for marker in ("pid ",):
                    self.assertEqual(terminal_width(rows[0][:rows[0].index(marker)]),
                                     terminal_width(rows[1][:rows[1].index(marker)]))
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
        self.assertEqual(model.reading.view.waiting[0].step, "nextest")

    def test_snapshot_without_project_fields_reads_with_empty_projects(self) -> None:
        reservation = cast(dict[str, object], json.loads(self.fixture.reservation.read_text()))
        reservation["repo"] = ""
        self.fixture.write_json(self.fixture.reservation, reservation)
        reading = self.fixture.model().reading
        encoded = wire.encode_reply(wire.ViewReply(reading))
        # Remove only the new optional field from the complete service reply.
        def without_project(value: object) -> object:
            if isinstance(value, dict):
                return {key: without_project(item) for key, item in cast(dict[str, object], value).items()
                        if key != "project"}
            if isinstance(value, list):
                return [without_project(item) for item in cast(list[object], value)]
            return value
        reply = wire.parse_reply(json.dumps(without_project(cast(object, json.loads(encoded)))))
        self.assertIsInstance(reply, wire.ViewReply)
        if isinstance(reply, wire.ViewReply):
            self.assertEqual(reply.reading, reading)
            self.assertTrue(all(not step.project for step in reply.reading.view.waiting))
            self.assertTrue(all(not step.project for step in reply.reading.view.running))

    def test_service_rows_show_the_repository_project_in_the_view_and_cards(self) -> None:
        cases = (
            ("checkout-alias", gate.RepositoryIdentity("repo-key", "nateroids"), "nextest", "nateroids"),
            ("nateroids", gate.UnknownRepository(), "nextest", "nateroids"),
            ("/home/natepiano/.claude", gate.UnknownRepository(), "merge-gate", ""),
        )
        for repo, repository, step, project in cases:
            with self.subTest(repo=repo), AdmissionFixture() as fixture:
                running = fixture.request(need=gate.GIB, step=step)
                running = replace(running, profile=replace(running.profile, repo=repo, repository=repository),
                                  sidecar=str(fixture.scope(running.owner)))
                grant = only_admitted(fixture.server.handle_request(running))
                waiting = fixture.request(need=60 * gate.GIB, step=step)
                waiting = replace(waiting, profile=replace(waiting.profile, repo=repo, repository=repository))
                replies = fixture.server.handle_request(waiting)
                self.assertTrue(any(isinstance(reply, wire.Wait) for reply in replies))
                _ = fixture.run_pass()
                model = self.service_model(fixture)
                active, = [row for row in model.reading.view.running if row.pid == running.owner.pid]
                queued, = model.reading.view.waiting
                self.assertEqual((active.project, queued.project), (project, project))
                self.assertEqual((active.step, queued.step), (step, step))
                shown_step = read_step_names().shown(step)
                shown = f"{project} {shown_step}" if project else shown_step
                lines = queue_view.render_view(model, 140)
                active_line, = [plain([line]) for line in lines if f"pid {running.owner.pid}" in plain([line])]
                waiting_line, = waiting_rows(lines)
                self.assertIn(shown, active_line)
                self.assertIn(shown, waiting_line)
                self.assertIn(shown, plain(queue_card.render_card(model, [1], 140)))
                self.assertIn(shown, plain(queue_card.render_selection(model, active.session_id, 140)))
                if not project:
                    self.assertNotIn(repo, active_line)
                    self.assertNotIn(repo, waiting_line)
                fixture.release(grant)

    def test_ci_reservation_label_comes_from_configuration_and_runner_identity(self) -> None:
        with AdmissionFixture() as admission:
            write_memory_config(admission.root / "build_admission.conf", ci_label="CI {runner} {job}")
            request = admission.service_request(runner="a", job="job")
            grant = only_admitted(admission.server.handle_request(request))
            _ = admission.run_pass()
            reply, = admission.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(reply, wire.ViewReply)
            if isinstance(reply, wire.ViewReply):
                row, = [row for row in reply.reading.view.running if row.pid == request.owner.pid]
                self.assertEqual(row.session_name, "CI a job")
                model = replace(self.fixture.model(), reading=reply.reading,
                    facts=head_facts.read_head_facts(reply.reading))
                text = plain(queue_view.render_view(model, 140))
                self.assertIn("CI a job", text)
            admission.release(grant)

    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.fixture.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(mock.patch.dict("os.environ", {"CONF_DIR": str(config.parent)})))


    def test_token_wait_keeps_place_one_and_head_status_moves_to_the_eligible_row(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        (token_path, token), (head_path, head) = inputs.ordered[:2]
        inputs = replace(inputs, ordered=[
            (token_path, {**token, "token_left_at": NOW - 10}),
            (head_path, {**head, "priority_since": NOW - 42}),
        ])
        reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        self.assertEqual([step.pid for step in reading.view.waiting], [token["pid"], head["pid"]])
        self.assertIsInstance(queue_view.step_checks(model, 1), gate.LineWaiter)
        eligible = queue_view.step_checks(model, 2)
        self.assertIsInstance(eligible, gate.HeadExplanation)
        if isinstance(eligible, gate.HeadExplanation):
            self.assertEqual(eligible.waiter.pid, head["pid"])
        self.assertEqual(queue_view.reason_phrase(reading.view.waiting[0].reason, reading.view.waiting),
                         "waiting for the cargo token")
        self.assertIn("waiting for the cargo token", plain(queue_card.render_card(model, [1], 140)))
        rows = waiting_rows(queue_view.render_view(model, 140))
        self.assertEqual([int(row.split()[0]) for row in rows], [1, 2])
        self.assertNotIn("↑", rows[0])
        self.assertNotIn("⊘", rows[0])
        self.assertIn("↑", rows[1])

    def test_merge_gate_rows_show_the_counted_anonymous_use_beside_the_keep_aside(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        source = gate.ProcessSource((gate.ProcessAnonymousUse(900, gate.AnonymousRunUse(4 * gate.GIB)),))
        original = inputs.running[0]
        parent = replace(original, session_id="id-parent-holder", session_name="parent-holder", unit="parent-holder", pid=900, promise=2 * gate.GIB,
            measured=gate.MeasuredMemory(7 * gate.GIB, anonymous=gate.AnonymousRunUse(4 * gate.GIB),
                                         anonymous_source=source), step="merge-gate")
        child = replace(parent, session_id="id-child-holder", session_name="child-holder", unit="child-holder", promise=6 * gate.GIB)
        base = inputs.reservations[0]
        rows = (dict(base, need=2 * gate.GIB, reservation_id="z-parent"),
                dict(base, need=6 * gate.GIB, reservation_id="a-child"))
        reading = gate.queue_read_from(replace(inputs, running=(parent, child), reservations=rows), admission_policy_fixture())
        room = reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        holds = {hold.memory.owner: hold for hold in room.runs}
        self.assertEqual(holds["a-child"].memory.use, gate.AnonymousRunUse(4 * gate.GIB))
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        text = plain(queue_view.render_view(model, 200))
        for name, owner in (("parent-holder", "z-parent"), ("child-holder", "a-child")):
            row, = [line for line in text.splitlines() if name in line]
            hold = holds[owner]
            assert isinstance(hold.memory.use, gate.AnonymousRunUse)
            self.assertIn(f"{hold.bytes / gate.GIB:.1f} / {hold.memory.use.bytes / gate.GIB:.1f}", row)

    def service_model(self, fixture: AdmissionFixture) -> queue_view.QueueModel:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return replace(self.fixture.model(), reading=reply.reading,
                       facts=head_facts.read_head_facts(reply.reading))

    def test_running_waiting_and_held_rows_show_run_pids_at_every_width(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(7000001, "100")
            child = fixture.fixture_owner(7000002, "200")
            run = wire.RunIdentity(owner.pid, owner.start_time)
            _ = (fixture.proc.path / str(child.pid) / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0".encode())
            request = replace(fixture.request(owner=child, run=run), sidecar=str(fixture.scope(child)))
            grant = only_admitted(fixture.server.handle_request(request))
            waiter = fixture.request(owner=fixture.fixture_owner(7000003, "300"),
                run=wire.RunIdentity(7000004, "400"), need=60 * gate.GIB)
            _ = fixture.fixture_owner(7000004, "400")
            _ = fixture.server.handle_request(waiter)
            for held in (False, True):
                if held:
                    _ = fixture.server.handle_request(wire.ReleaseRequest("held", grant.reservation, 0))
                model = self.service_model(fixture)
                for columns in (60, 80, 100, 140):
                    with self.subTest(held=held, columns=columns):
                        lines = queue_view.render_view(model, columns)
                        running, = [plain([line]) for line in lines
                                    if plain([line]).startswith("run") and "pid 7000001" in plain([line])]
                        queued, = waiting_rows(lines)
                        self.assertIn("pid 7000001", running)
                        self.assertIn("pid 7000004", queued)
                        self.assertNotIn("pid 7000002", running)
                        self.assertNotIn("pid 7000003", queued)
                        self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                        card = queue_card.render_selection(model, "1", columns)
                        self.assertIn("pid 7000004", plain(card))
                        self.assertTrue(all(terminal_width(plain([line])) <= columns for line in card))
                if held:
                    self.assertFalse(fixture.server.reservations)
                    room = model.reading.explanation.room
                    assert isinstance(room, gate.MemoryRoom)
                    self.assertGreater(sum(hold.memory.held for hold in room.runs), 0)

    def test_service_without_a_recorded_process_shows_pid_absence(self) -> None:
        model = self.fixture.model()
        compiler = gate.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
            gate.CompilerServiceMemory(gate.GIB, pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded())
        reading = queue_read_fixture(replace(model.reading.view, running=(compiler,)),
                                     policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        for lines in (queue_view.render_view(model, 140), queue_card.render_card(model, [], 140)):
            self.assertIn("pid —", plain(lines))
            self.assertNotIn("pid 0", plain(lines))


    def test_running_row_marks_only_the_asking_session(self) -> None:
        model = self.fixture.model()
        grants = tuple(gate.RunningStep(session, session, "merge-gate", "/gate", 6 * gate.GIB,
                                        gate.MeasuredMemory(gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB)), pid=900) for session in ("mine", "other"))
        reading = queue_read_fixture(replace(model.reading.view, running=grants), policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        for you, expected in (("mine", [1, 0]), ("other", [0, 1]), ("", [0, 0])):
            with self.subTest(you=you):
                rows = [row for row in queue_view.render_view(model, 100, you=you)
                        if plain([row]).startswith("run")]
                self.assertEqual([sum(span.text.count("▸") for span in row if span.tone == "suggestion")
                                  for row in rows], expected)

    def test_service_records_ended_scope_identity_and_the_view_uses_its_label(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (process / "environ").write_bytes(
                b"CLAUDE_CODE_SESSION_ID=ended-session\0CLAUDE_SESSION_NAME=recorded worker\0")
            _ = (process / "cmdline").write_bytes(b"python3\0worker.py\0")
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "cgroup.procs").write_text(str(owner.pid))
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "ended.scope")
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"ended-session": UnitLabels("named worker")})
            step, = (step for step in model.reading.view.running if step.session_id == "ended-session")
            self.assertIsInstance(step.measured, gate.UnownedSliceMemory)
            assert isinstance(step.measured, gate.UnownedSliceMemory)
            self.assertIsInstance(step.measured.activity, ScopeProcesses)
            text = plain(queue_view.render_view(model, 140))
            self.assertIn("Still running after their build ended (1 process, 1.0 GiB)", text)
            self.assertIn("named worker · python3", text)
            self.assertNotIn("ended-session", text)

    def ci_service_model(self, fixture: AdmissionFixture, *, active: bool) -> queue_view.QueueModel:
        ticks = int(os.sysconf("SC_CLK_TCK"))
        boot = int(fixture.clock.now()) - 172801
        _ = (fixture.proc.path / "stat").write_text(f"btime {boot}\n")
        listener = fixture.fixture_owner(700001, str(ticks))
        process = fixture.proc.path / str(listener.pid)
        _ = (process / "comm").write_text("Runner.Listener\n")
        _ = (process / "cmdline").write_bytes(b"Runner.Listener\0")
        scope = Path(fixture.scope(listener).read_text()).rename(
            fixture.root / gate.read_memory_config().ci_memory_slice)
        _ = (scope / "memory.max").write_text(str(8 * gate.GIB))
        _ = (scope / "memory.current").write_text(str(int(0.09 * gate.GIB)))
        _ = (scope / "memory.swap.current").write_text(str(int(1.98 * gate.GIB)))
        if active:
            worker = fixture.fixture_owner(700002, str((172801 - 120) * ticks))
            process = fixture.proc.path / str(worker.pid)
            _ = (process / "comm").write_text("Runner.Worker\n")
            _ = (process / "cmdline").write_bytes(b"Runner.Worker\0")
            _ = (scope / "cgroup.procs").write_text(f"{listener.pid}\n{worker.pid}\n")
        with mock.patch.object(gate, "ci_slice_cgroup", return_value=scope):
            _ = fixture.server.run_pass()
            return self.service_model(fixture)

    def test_idle_ci_keeps_a_small_ram_row_and_separate_swap_without_runner_age(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=False)
            self.assertEqual(model.reading.explanation.totals.charged, int(0.09 * gate.GIB))
            self.assertEqual(model.reading.explanation.totals.measured, int(0.09 * gate.GIB))
            for source, lines in (("view", queue_view.render_view(model, 240)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    rows = [line for line in plain(lines).splitlines() if "ci.slice:" in line]
                    self.assertEqual(len(rows), 1)
                    row, = rows
                    self.assertIn("idle", row)
                    self.assertIn("92 MiB RAM", row)
                    self.assertIn("swap", row)
                    self.assertIn("2.0", row)
                    self.assertNotIn("min", row)
                    for hidden in ("2880", "2 days", "admitted", "Runner.Listener"):
                        self.assertNotIn(hidden, row)

    def test_active_ci_shows_worker_age_instead_of_listener_uptime(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=True)
            ci, = [step for step in model.reading.view.running
                   if isinstance(step.measured, gate.UnownedSliceMemory)]
            assert isinstance(ci.measured, gate.UnownedSliceMemory)
            self.assertEqual(ci.pid, 700002)
            self.assertEqual(ci.measured.pid, 700002)
            _ = (fixture.proc.path / "700002" / "comm").write_text("Runner.Listener\n")
            _ = (fixture.root / model.ci_memory_slice / "cgroup.procs").write_text("9999999\n")
            for source, lines in (("view", queue_view.render_view(model, 240)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    rows = [line for line in plain(lines).splitlines() if "ci.slice:" in line]
                    self.assertEqual(len(rows), 1)
                    row, = rows
                    self.assertIn("job", row)
                    self.assertIn("2 min", row)
                    self.assertIn("pid 700002", row)
                    self.assertNotIn("9999999", row)
                    self.assertIn("swap", row)
                    for hidden in ("idle", "2880", "2 days", "Runner.Listener"):
                        self.assertNotIn(hidden, row)

    def test_running_ci_names_its_keep_aside_in_the_view_and_card(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=True)
            for source, lines in (("view", queue_view.render_view(model, 400)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    self.assertIn("CI keep-aside 7.0 GiB", plain(lines))

    def test_idle_ci_has_no_keep_aside_in_the_view_or_card(self) -> None:
        with AdmissionFixture() as fixture:
            model = self.ci_service_model(fixture, active=False)
            for source, lines in (("view", queue_view.render_view(model, 400)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(source=source):
                    self.assertNotIn("CI keep-aside", plain(lines))

    def test_build_swap_has_a_separate_label_and_does_not_enter_ram_values(self) -> None:
        model = self.fixture.model()
        step = gate.RunningStep("owner", "worker", "clippy", "/tree", 0,
            gate.MeasuredMemory(gate.GIB // 10, swap_bytes=2 * gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB // 10)), started_at=NOW - 120, pid=123)
        reading = queue_read_fixture(replace(model.reading.view, running=(step,)),
                                     policy=admission_policy_fixture())
        model = replace(model, reading=reading)
        view_row, = [line for line in plain(queue_view.render_view(model, 240)).splitlines()
                     if line.startswith("run")]
        self.assertIn("0.1", view_row)
        self.assertIn("2.0 GiB swapped", view_row)
        self.assertNotIn("2.1", view_row)
        card_row, = [line for line in plain(queue_card.render_card(model, [], 400)).splitlines()
                     if "worker, clippy:" in line]
        self.assertIn("need 0.0 GiB; anonymous use 0.1 GiB; held slot 0.0 GiB; kept aside 0.0 GiB", card_row)
        self.assertIn("2.0 GiB swapped", card_row)

    def test_small_compiler_service_keeps_its_own_ram_row(self) -> None:
        for used in (0, gate.GIB // 10):
            model = self.fixture.model()
            compiler = gate.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
                                       gate.CompilerServiceMemory(used, swap_bytes=2 * gate.GIB, pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded())
            reading = queue_read_fixture(replace(model.reading.view, running=(compiler,)),
                                         policy=admission_policy_fixture())
            model = replace(model, reading=reading)
            for source, lines in (("view", queue_view.render_view(model, 240)),
                                  ("card", queue_card.render_card(model, [], 400))):
                with self.subTest(used=used, source=source):
                    rows = [line for line in plain(lines).splitlines() if "sccache" in line]
                    self.assertEqual(len(rows), 1)
                    self.assertIn(f"{used / gate.GIB:.1f} GiB RAM", rows[0])
                    self.assertIn("idle", rows[0])
                    self.assertIn("2.0 GiB swapped", rows[0])
                    self.assertNotIn("admission time unknown", rows[0])

    def test_ci_slice_processes_never_appear_as_ended_builds(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (process / "environ").write_bytes(b"CLAUDE_SESSION_NAME=ci-worker\0")
            scope = Path(fixture.scope(owner).read_text()).rename(
                fixture.root / gate.read_memory_config().ci_memory_slice)
            _ = (scope / "memory.max").write_text(str(8 * gate.GIB))
            for program in ("bash", "Runner.Listener", "Runner.Worker"):
                with self.subTest(program=program):
                    _ = (process / "cmdline").write_bytes(program.encode() + b"\0")
                    _ = (process / "comm").write_text(program)
                    with mock.patch.object(gate, "ci_slice_cgroup", return_value=scope):
                        _ = fixture.server.run_pass()
                    model = self.service_model(fixture)
                    self.assertTrue(any(isinstance(step.measured, gate.UnownedSliceMemory)
                                        for step in model.reading.view.running))
                    text = plain(queue_view.render_view(model, 140))
                    self.assertNotIn("Still running after their build ended", text)
                    self.assertNotIn("ci-worker ·", text)

    def test_live_run_owner_outside_the_scope_keeps_its_processes_live(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            child = fixture.fixture_owner(700002, "200")
            process = fixture.proc.path / str(child.pid)
            _ = (process / "environ").write_bytes(b"BUILDLOG_RUN=700001:100\0")
            _ = (process / "cmdline").write_bytes(b"nextest\0")
            scope = Path(fixture.scope(child).read_text())
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / f"run-p{owner.pid}-iabc.scope")
            _ = fixture.server.run_pass()
            model = self.service_model(fixture)
            self.assertEqual(len(model.reading.view.running), 2)
            step, = (step for step in model.reading.view.running
                     if isinstance(step.measured, gate.UnownedSliceMemory)
                     and isinstance(step.measured.job_state, NotServiceSlice))
            assert isinstance(step.measured, gate.UnownedSliceMemory)
            activity = step.measured.activity
            self.assertIsInstance(activity, ScopeProcesses)
            assert isinstance(activity, ScopeProcesses)
            self.assertIsInstance(activity.run, LiveBuildRun)
            assert isinstance(activity.run, LiveBuildRun)
            self.assertEqual((activity.run.pid, activity.run.start_time), (owner.pid, owner.start_time))
            self.assertNotIn("Still running after their build ended", plain(queue_view.render_view(model, 140)))

    def test_ended_processes_use_session_labels_and_exclude_the_ci_slice(self) -> None:
        model = self.fixture.model()
        ended = ScopeProcesses(2, "private-session-id", "recorded worker", "/tree", "python3", NOW - 120,
                               True, NoLiveBuildRun())
        live = replace(ended, run=LiveBuildRun(321, "123", NOW - 180))
        listener = replace(ended, session_id="ci-owner", program="Runner.Listener")
        rows = tuple(gate.RunningStep(activity.session_id, activity.session_name, "unowned", activity.folder, 0,
            gate.UnownedSliceMemory(gate.GIB, owner=model.ci_memory_slice if activity is listener else "opaque.scope",
                                    activity=activity,
                                    job_state=IdleService() if activity is listener else NotServiceSlice(), pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded())
            for activity in (ended, live, listener))
        reading = queue_read_fixture(replace(model.reading.view, running=rows), policy=admission_policy_fixture())
        for labels, owner in (({"private-session-id": UnitLabels("named worker", "builds", "queue")}, "named worker"),
                              ({}, "recorded worker")):
            with self.subTest(owner=owner):
                text = plain(queue_view.render_view(replace(model, reading=reading, labels=labels), 140))
                section = text.split("Still running after their build ended", 1)[1]
                self.assertIn("2 processes, 1.0 GiB", section)
                self.assertIn(owner + " · python3 · 2 min · 1.0 GiB", section)
                self.assertIn("ask the named owner to stop them if no longer needed", section)
                for hidden in ("private-session-id", "unknown owner", "Runner.Listener", "opaque.scope"):
                    self.assertNotIn(hidden, section)
                self.assertEqual(section.count("python3"), 1)

    def test_leftover_age_ranges_and_edges_match_the_golden(self) -> None:
        model = self.fixture.model()
        minutes = (0, 45, 119, 120, 19 * 60, 47 * 60, 48 * 60 - 1, 48 * 60, 3567)
        rows = tuple(gate.RunningStep("worker", "worker", "unowned", "/tree", 0,
            gate.UnownedSliceMemory(gate.GIB, owner=f"scope-{value}", activity=ScopeProcesses(
                1, "worker", "worker", "/tree", "python3", NOW - value * 60, True, NoLiveBuildRun()), pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded())
            for value in minutes)
        reading = queue_read_fixture(replace(model.reading.view, running=rows), policy=admission_policy_fixture())
        text = plain(queue_view.render_view(replace(model, reading=reading, labels={}), 140))
        section = "Still running after their build ended" + text.split("Still running after their build ended", 1)[1]
        self.assertEqual(section + "\n", (GOLDEN / "leftover_ages.txt").read_text())

    def test_joined_running_row_counts_process_swap_once(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            process = fixture.proc.path / str(owner.pid)
            _ = (process / "status").write_text(
                f"VmRSS: {gate.GIB // 1024} kB\nVmSwap: {gate.GIB // 1024} kB\n")
            _ = (process / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0".encode())
            sidecar = fixture.scope(owner)
            scope = Path(sidecar.read_text())
            _ = (scope / "memory.swap.current").write_text(str(gate.GIB))
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            _ = scope.rename(builds / "joined.scope")
            _ = sidecar.write_text(str(builds / "joined.scope"))
            request = replace(fixture.request(owner=owner, run=wire.RunIdentity(owner.pid, owner.start_time)), sidecar=str(sidecar))
            _ = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            model = self.service_model(fixture)
            row, = queue_view.running_builds(model)
            self.assertEqual(row.used, gate.GIB)
            self.assertEqual(row.swap_bytes, gate.GIB)
            text = plain(queue_view.render_view(model, 180))
            rendered, = (line for line in text.splitlines() if line.startswith("run"))
            self.assertIn("1.0 GiB swapped", rendered)

    def test_unowned_scope_stays_outside_the_granted_rows(self) -> None:
        model = self.fixture.model()
        grant = gate.RunningStep("gate", "gate", "merge-gate", "/gate", 6 * gate.GIB, gate.MeasuredMemory(gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB)), pid=123)
        scope = gate.RunningStep("gate", "gate", "unowned", "/gate", 0,
                                gate.UnownedSliceMemory(2 * gate.GIB, owner="gate.scope", pid=gate.PidNotRecorded()), pid=gate.PidNotRecorded())
        reading = queue_read_fixture(replace(model.reading.view, running=(grant, scope)), policy=admission_policy_fixture())
        current = replace(model, reading=reading)
        row, = queue_view.running_builds(current)
        self.assertIs(row.step, grant)
        text = plain(queue_view.render_view(current, 140))
        self.assertEqual(sum(line.startswith("run") for line in text.splitlines()), 1)
        self.assertNotIn("gate.scope", text)
        self.assertEqual(current.reading.explanation.totals.measured, 3 * gate.GIB)

    def test_running_and_waiting_share_one_table_without_merging_grants(self) -> None:
        model = self.fixture.model()
        grants = tuple(gate.RunningStep(f"gate-{index}", "gate", "merge-gate", f"/gate-{index}",
            6 * gate.GIB, gate.MeasuredMemory(gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB)), unit="queue-unit", pid=900 + index) for index in range(3))
        reading = queue_read_fixture(replace(model.reading.view, running=grants), policy=admission_policy_fixture())
        explained = tuple(replace(row, admitted_at=NOW - (index + 1) * 60)
                          for index, row in enumerate(reading.explanation.running))
        reading = replace(reading, explanation=replace(reading.explanation, running=explained))
        model = replace(model, reading=reading, labels={**model.labels,
            **{step.session_id: UnitLabels("gate", "builds", "wrong-unit") for step in grants}})
        for columns in (60, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(model, columns)
                text = plain(lines)
                header, = [line for line in text.splitlines() if line.lstrip().startswith("# ")]
                running = [line for line in text.splitlines() if line.startswith("run")]
                self.assertEqual(len(running), 3)
                for index, row in enumerate(running):
                    unit = "queue-…" if columns == 60 else "queue-unit"
                    self.assertEqual(row[:header.index("step")].split(), ["run", "builds", unit])
                    self.assertEqual(row[header.index("step"):header.index("min")].strip(), read_step_names().shown("merge-gate"))
                    self.assertEqual(row[header.index("min"):].split()[0], str(index + 1))
                    self.assertIn("5.0", row)
                    if columns >= 100:
                        self.assertIn("5.0 / 1.0", row)
                waiting = waiting_rows(lines)
                self.assertEqual([int(row.split()[0]) for row in waiting], list(range(1, 33)))
                self.assertLess(text.index(running[-1]), text.index(waiting[0]))
                self.assertEqual(sum(line.lstrip().startswith("# ") for line in text.splitlines()), 1)
                self.assertTrue(all(terminal_width(line) <= columns for line in text.splitlines()))

    def test_unit_override_survives_restart_and_matches_priority(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request()
            later = fixture.request(since=first.since + 1)
            later = replace(later, identity=replace(later.identity, unit="queue-unit"),
                            sidecar=str(fixture.scope(later.owner)))
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(later)
            fixture.restart()
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(later)
            labels = {first.identity.session_id: UnitLabels("first", "builds", "other-unit"),
                      later.identity.session_id: UnitLabels("gate", "builds", "wrong-unit")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                refused, = fixture.completed_queue_change(wire.PrioritySetRequest("wrong-bump", "wrong-unit", "urgent", "user"))
                self.assertIsInstance(refused, wire.Refused)
                replies = fixture.completed_queue_change(wire.PrioritySetRequest("bump-unit", "queue-unit", "urgent", "user"))
                self.assertEqual(replies, [wire.Ok()])
                model = self.service_model(fixture)
                self.assertEqual([step.pid for step in model.reading.view.waiting], [later.owner.pid, first.owner.pid])
                self.assertEqual(model.reading.view.waiting[0].unit, "queue-unit")
                self.assertEqual(model.reading.view.waiting[1].unit, "")
                self.assertEqual(queue_evidence.step_labels(model.reading.view.waiting[1], labels).unit, "other-unit")
                self.assertEqual(queue_evidence.step_labels(model.reading.view.waiting[0], {}).unit, "queue-unit")
                fixture.free(60 * gate.GIB)
                write_pressure_reading(fixture.root / "pressure")
                _ = fixture.server.run_pass()
                _ = fixture.server.run_pass()
                model = self.service_model(fixture)
                running = next(step for step in model.reading.view.running if step.session_id == later.identity.session_id)
                self.assertEqual(queue_evidence.step_labels(running, labels).unit, "queue-unit")

    def test_unit_override_view_row_matches_golden(self) -> None:
        model = self.fixture.model()
        waiting = model.reading.view.waiting
        step = replace(waiting[0], unit="queue-unit", step="merge-gate")
        model = replace(model, labels={**model.labels, step.session_id: UnitLabels("gate", "builds")},
            reading=replace(model.reading, view=replace(model.reading.view, waiting=(step, *waiting[1:]))))
        rows = plain(queue_view.render_view(model, 140)).splitlines()
        row, = [row for row in rows if "queue-unit" in row]
        self.assertEqual(row + "\n", (GOLDEN / "unit_override_view.txt").read_text())

    def test_showrunner_without_a_unit_keeps_the_empty_unit_label(self) -> None:
        model = self.fixture.model()
        model = replace(model, labels={session: UnitLabels("builds", "builds") for session in model.labels})
        text = plain(queue_view.render_view(model, 140))
        rows = [line for line in text.splitlines() if line.startswith("run") or line.lstrip().startswith("1 ")]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.split()[1:3], ["builds", "—"])
    def test_merge_gate_grant_has_a_running_row_with_use_and_grant(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate")
            request = replace(request, identity=wire.Identity("gate-owner", "gate-worker", "", str(fixture.root)),
                profile=gate.ProgramProfile("merge-gate", "repo", "tree", gate.HistoryPeakSizing(6 * gate.GIB, gate.FirstProgramPeak(6 * gate.GIB), 0.0)),
                sidecar=str(fixture.scope(request.owner)))
            _ = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(7 * 60)
            fixture.free(10 * gate.GIB)
            _ = fixture.server.handle_request(fixture.request(need=8 * gate.GIB))
            model = replace(self.service_model(fixture), labels={"gate-owner": UnitLabels("gate-worker", "devtools", "devtools")})
            for colour in (False, True):
                lines = queue_view.render_view(model, 140, colour=colour)
                text = plain(lines)
                running = "\n".join(line for line in text.splitlines() if line.startswith("run"))
                queued = "\n".join(waiting_rows(lines))
                row, = [line for line in running.splitlines() if read_step_names().shown("merge-gate") in line]
                self.assertIn("devtools", row)
                self.assertIn("5.0 / 1.0", row)
                self.assertIn("7", row.split())
                self.assertEqual(row.count(read_step_names().shown("merge-gate")), 1)
                queued_row, = waiting_rows(lines)
                bar_characters = "░█▏▎▍▌▋▊▉"
                bar_column = terminal_width(row[:next(index for index, char in enumerate(row) if char in bar_characters)])
                self.assertEqual(bar_column, terminal_width(queued_row[:next(
                    index for index, char in enumerate(queued_row) if char in bar_characters)]))
                rendered_lines = text.splitlines()
                cap_index = next(index for index, line in enumerate(rendered_lines) if line.lstrip().startswith("cap "))
                top = rendered_lines[cap_index + 1]
                self.assertEqual(bar_column, terminal_width(top[:next(
                    index for index, char in enumerate(top) if char in "░█·▒│")]))
                self.assertIn("8.0", queued)
                self.assertIn("░", row)

    def test_live_scope_without_a_reservation_or_hold_has_no_queue_row(self) -> None:
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
            _ = sidecar.write_text(str(builds / "between.scope"))
            _ = fixture.server.run_pass()
            model = replace(self.service_model(fixture), labels={"between-owner": UnitLabels("worker", "hana", "hana")})
            text = plain(queue_view.render_view(model, 140))
            self.assertFalse(fixture.server.reservations)
            room = model.reading.explanation.room
            assert isinstance(room, gate.MemoryRoom)
            self.assertFalse(room.runs)
            self.assertTrue(any(isinstance(step.measured, gate.UnownedSliceMemory)
                                and isinstance(step.measured.activity, ScopeProcesses)
                                and isinstance(step.measured.activity.run, LiveBuildRun)
                                for step in model.reading.view.running))
            self.assertFalse(queue_view.running_builds(model))
            self.assertNotIn("between steps", text)
            self.assertFalse(any(line.startswith("run") for line in text.splitlines()))
            self.assertNotIn("Still running after their build ended", text)

    def test_live_scope_with_a_hold_shows_one_between_steps_row(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.fixture_owner(700001, "100")
            child = fixture.fixture_owner(700002, "200")
            _ = (fixture.proc.path / str(child.pid) / "environ").write_bytes(
                f"BUILDLOG_RUN={owner.pid}:{owner.start_time}\0".encode())
            sidecar = fixture.scope(child)
            scope = Path(sidecar.read_text())
            builds = fixture.root / "builds.slice"
            builds.mkdir(exist_ok=True)
            scope = scope.rename(builds / "held.scope")
            _ = sidecar.write_text(str(scope))
            request = replace(fixture.request(owner=child, run=wire.RunIdentity(owner.pid, owner.start_time)),
                              sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.release(grant)
            _ = fixture.run_pass()
            model = self.service_model(fixture)
            self.assertFalse(fixture.server.reservations)
            room = model.reading.explanation.room
            assert isinstance(room, gate.MemoryRoom)
            hold, = room.runs
            self.assertGreater(hold.bytes, 0)
            self.assertTrue(any(isinstance(step.measured, gate.UnownedSliceMemory)
                                and isinstance(step.measured.activity, ScopeProcesses)
                                and isinstance(step.measured.activity.run, LiveBuildRun)
                                for step in model.reading.view.running))
            row, = queue_view.running_builds(model)
            self.assertEqual(row.kept, hold.bytes)
            self.assertEqual(row.step.run, hold.memory.run)
            text = plain(queue_view.render_view(model, 140))
            line, = [line for line in text.splitlines() if line.startswith("run")]
            self.assertIn("between steps", line)
            self.assertIn(f"pid {owner.pid}", line)

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
            ended, = [step for step in model.reading.view.running
                      if isinstance(step.measured, gate.UnownedSliceMemory) and step.measured.owner == "unreadable-run-name.scope"]
            assert isinstance(ended.measured, gate.UnownedSliceMemory)
            self.assertEqual(ended.pid, owner.pid)
            self.assertEqual(ended.measured.pid, owner.pid)
            activity = ended.measured.activity
            assert isinstance(activity, ScopeProcesses)
            self.assertEqual([member.pid for member in activity.members], [owner.pid, second.pid])
            _ = (builds / "unreadable-run-name.scope" / "cgroup.procs").write_text("9999999\n")
            text = plain(queue_view.render_view(model, 140))
            section = text.split("Still running after their build ended", 1)[1]
            self.assertIn("2 processes", section)
            self.assertIn("1.0 GiB", section)
            self.assertIn("worker · python3", section)
            self.assertIn(f"pid {owner.pid}", section)
            self.assertIn(f"pid {second.pid}", section)
            self.assertNotIn("9999999", section)
            self.assertNotIn("ended-owner", section)
            self.assertIn("python3", section)
            self.assertIn("2 d", section)
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
            self.assertIn("room", text)
            self.assertNotIn("taken", text)
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
            self.assertEqual(len(measured), 2)
            scope, = (memory for memory in measured if isinstance(memory.job_state, NotServiceSlice))
            idle, = (memory for memory in measured if isinstance(memory.job_state, IdleService))
            self.assertEqual(idle.bytes, 0)
            self.assertEqual(scope.bytes, amount)
            head = reading.explanation.head
            assert isinstance(head, gate.HeadExplanation)
            self.assertEqual(head.decision.charged, gate.GIB)
            self.assertEqual(head.free.available, 12 * gate.GIB)
            model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
            view = plain(queue_view.render_view(model, 400))
            card = plain(queue_card.render_card(model, [1], 400))
            page = view + "\n\n" + card
            self.assertNotIn("replay.scope", page)
            self.assertNotIn("Still running after their build ended", page)
            self.assertIn("Room: need 8.0 GiB <= room 9.0 GiB: pass", page)
            self.assertNotIn("owned.scope:", page)
            self.assertEqual(page.count("Room: need 8.0 GiB"), 1)

    def test_expired_head_with_passing_checks_has_an_error_hold_at_sixty_cells(self) -> None:
        inputs = self.fixture.inputs(policy=admission_policy_fixture())
        path, row = inputs.ordered[0]
        inputs = replace(inputs, running=(), reservations=(), pressure=gate.NoPressureHold(),
            host=gate.HostMemory(60 * gate.GIB, 60 * gate.GIB),
            ordered=[(path, {**row, "need": gate.GIB, "estimated_at": NOW - 600,
                            "priority_since": NOW - 42}), inputs.ordered[1]])
        reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertTrue(head.room.passes)
        self.assertTrue(head.free.passes)
        self.assertIsInstance(head.decision.pressure, gate.NoPressureHold)
        model = replace(self.fixture.model(), reading=reading, facts=head_facts.read_head_facts(reading))
        lines = queue_view.render_view(model, 60)
        rows = waiting_rows(lines)
        text_lines = plain(lines).splitlines()
        diagnostic_lines = [rows[0]]
        for line in text_lines[text_lines.index(rows[0]) + 1:]:
            if not line.startswith("· "):
                break
            diagnostic_lines.append(line.removeprefix("· "))
        self.assertIn("↑ estimate expired", " ".join(diagnostic_lines))
        self.assertEqual(queue_view.reason_phrase(reading.view.waiting[0].reason, reading.view.waiting),
                         "estimate expired")
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))
        self.assertNotIn("█", rows[0])
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
        self.assertIn("starts below full avg10 9.0%", recovery)
        self.assertNotIn("one start per", recovery)

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
        self.assertIn("queue priority: cables (captured priority)", view)
        self.assertIn("queue priority: cables (captured priority)", card)
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
            priority: gate.QueuePriority = {"unit": unit, "why": "test", "set_at": NOW, "set_by": "actor",
                                            "transition": gate.SettledPriority()}
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
                    waiting = set(waiting_rows(lines))
                    previous_waiting = set(waiting_rows(baseline))
                    rows = [line for line in lines if plain([line]).startswith("run") or plain([line]) in waiting]
                    previous = [line for line in baseline if plain([line]).startswith("run") or plain([line]) in previous_waiting]
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
        model = self.fixture.cap_model()
        model = replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, priority=gate.NoQueuePriority())))
        self.assertEqual(plain(queue_view.render_view(model, 100, you=ASKING_SESSION)) + "\n",
                         (GOLDEN / "no_bump_100.txt").read_text())



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
                rendered = queue_view.render_view(self.fixture.cap_model(), columns)
                rows = waiting_rows(rendered)
                self.assertEqual(len(rows), 32)
                self.assertEqual([int(row.split()[0]) for row in rows], list(range(1, 33)))
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in rendered))
                self.assertFalse(any(row.startswith("Session:") for row in rows))
                self.assertEqual(sum(line.startswith("run") for line in plain(rendered).splitlines()), 1)
                self.assertEqual(sum(line.lstrip().startswith("# ") for line in plain(rendered).splitlines()), 1)
                self.assertEqual(plain(rendered).count("●"), 3)
                if columns == 30:
                    self.assertTrue(any(line.startswith("· ") for line in plain(rendered).splitlines()))
                for removed in ("starts", "moved up:", "#1 waits on"):
                    self.assertNotIn(removed, plain(rendered))

    def test_card_checks_keep_all_room_numbers_and_failure_tones_when_wrapped(self) -> None:
        model = self.fixture.model()
        checks = queue_view.step_checks(model, 1)
        assert isinstance(checks, gate.HeadExplanation)
        expected = queue_evidence.room_check_text(checks.room)
        self.assertEqual(checks.room.need, int(7.6 * gate.GIB))
        for columns in (400, 60, 30):
            with self.subTest(columns=columns):
                pressure = queue_view.check_line(*queue_view.pressure_check(checks.decision.pressure, model))
                lines = [row for check in [pressure, *queue_view.check_lines(checks, model)]
                         for row in wrap(check, columns)]
                text = "".join(plain(lines).split())
                for fact in ("✗ memory pressure: full avg10 7.7%, some avg10 0.0%, swap used 0.0%; starts below full avg10 5.0%", expected):
                    self.assertIn("".join(fact.split()), text)
                self.assertEqual(sum(span.text.count("✗") for row in lines for span in row if span.tone == "error"),
                                 1 + int(not checks.room.passes))
                self.assertEqual(sum(span.text.count("✓") for row in lines for span in row if span.tone == "success"),
                                 int(checks.room.passes))

    def test_step_columns_align_wide_and_combining_names_by_cells(self) -> None:
        model = self.fixture.cap_model()
        first, second = model.reading.view.waiting[:2]
        model = with_waiting(model, [replace(first, step="界e\u0301"), replace(second, step="doc")])
        rows = waiting_rows(queue_view.render_view(model, 140))
        for marker in ("tool-based-ui", "█"):
            self.assertEqual(terminal_width(rows[0][:rows[0].index(marker)]),
                             terminal_width(rows[1][:rows[1].index(marker)]))
        self.assertIn("界e\u0301", rows[0])

    def test_view_ignores_historical_moves_token_history_and_pace(self) -> None:
        model = self.fixture.cap_model()
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

    def test_empty_queue_keeps_cap_and_header_without_invented_steps(self) -> None:
        model = self.fixture.cap_model()
        reading = queue_read_fixture(gate.QueueView((), (), 24 * gate.GIB, 0, 3 * gate.GIB, NOW), policy=admission_policy_fixture())
        rendered = queue_view.render_view(replace(model, reading=reading, facts=gate.NoWaiter()), 100)
        text = plain(rendered)
        self.assertTrue(text.startswith("Build queue · 10:46\n\n"))
        self.assertIn("room 21.0", text)
        self.assertIn("cap ", text)
        self.assertEqual(waiting_rows(rendered), [])
        self.assertNotIn("⊘", text)
        self.assertNotIn("↑", text)

    def test_card_checks_use_the_recorded_room_without_redeciding(self) -> None:
        model = self.fixture.model()
        checks = queue_view.step_checks(model, 1)
        assert isinstance(checks, gate.HeadExplanation)
        with mock.patch.object(gate, "admission_pass", side_effect=AssertionError("admission decides once")),              mock.patch.object(gate, "memory_room", side_effect=AssertionError("recorded room")):
            text = plain(queue_view.check_lines(checks, model))
        self.assertEqual(checks.room.need, int(7.6 * gate.GIB))
        self.assertIn(queue_evidence.room_check_text(checks.room), text)
        self.assertNotIn("budget", text.lower())


class CapViewTests(QueueFixtureTests):
    def cap_model(self, *, available: int = 20, need: int = 10, used: int = 5) -> queue_view.QueueModel:
        model = self.fixture.cap_model()
        room = gate.memory_room(gate.HostMemory(available * gate.GIB, 60 * gate.GIB), [gate.RunMemory("run", need * gate.GIB, gate.AnonymousRunUse(used * gate.GIB))], admission_policy_fixture())
        return replace(model, reading=replace(model.reading,
            explanation=replace(model.reading.explanation, room=room)))


    def test_compact_page_matches_reviewed_golden_files(self) -> None:
        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                lines = queue_view.render_view(self.fixture.cap_model(), columns, you=ASKING_SESSION)
                self.assertEqual(plain(lines) + "\n", (GOLDEN / f"view_{columns}.txt").read_text())
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in lines))
                page = [*lines, (), *queue_card.render_selection(self.fixture.cap_model(), "1", columns)]
                self.assertTrue(all(terminal_width(plain([line])) <= columns for line in page))

    def test_cap_and_row_bars_share_the_sixty_column_page(self) -> None:
        lines = queue_view.render_view(self.cap_model(), 60)
        text = plain(lines).splitlines()
        self.assertEqual(text[:2], ["Build queue · 10:46", ""])
        self.assertIn("cap 22.0", plain(lines))
        bar = next(line for line in lines if "│" in plain([line]) and "█" in plain([line]))
        prefix = ""
        for span in bar:
            if "█" in span.text:
                break
            prefix += span.text
        rows = waiting_rows(lines)
        self.assertEqual(len(rows), 32)
        self.assertEqual(rows[0].index("█"), terminal_width(prefix))
        self.assertEqual([int(row.split()[0]) for row in rows], list(range(1, 33)))
        self.assertNotIn("budget", plain(lines).lower())
        self.assertTrue(all(terminal_width(line) <= 60 for line in text))

    def test_cap_orders_use_keep_room_safety_and_other_programs(self) -> None:
        room = self.cap_model().reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        bar = queue_view.cap_bar(room, 60, colour=True)
        self.assertEqual(plain([bar]), "█" * 5 + "░" * 5 + "█" * 12 + "│" + "█" * 37)
        self.assertEqual([span.tone for span in bar],
            ["suggestion"] * 5 + ["warning"] * 5 + ["success"] * 12
            + ["error"] + ["warning"] * 2 + ["inactive"] * 35)
        self.assertEqual(queue_evidence.room_text(room),
            "Cap 22.0 GiB; anonymous use 5.0 GiB + free 20.0 GiB − safety 3.0 GiB; kept aside 5.0 GiB; room 12.0 GiB")
        self.assertEqual(queue_evidence.keep_aside_text(room.runs[0]),
            "need 10.0 GiB; anonymous use 5.0 GiB; held slot 0.0 GiB; kept aside 5.0 GiB")

    def test_view_and_cap_bar_include_ci_use_in_the_shared_cap(self) -> None:
        model = self.fixture.cap_model()
        policy = admission_policy_fixture()
        inputs = self.fixture.inputs(policy=policy)
        run = replace(inputs.running[0], promise=10 * gate.GIB,
                      measured=gate.MeasuredMemory(5 * gate.GIB, anonymous=gate.AnonymousRunUse(5 * gate.GIB)))
        ci = ci_step(4 * gate.GIB, RunningServiceJob(NOW - 60))
        inputs = replace(inputs, running=(run, ci),
            reservations=({**inputs.reservations[0], "need": 10 * gate.GIB},),
            host=gate.HostMemory(20 * gate.GIB, 60 * gate.GIB))
        reading = gate.queue_read_from(inputs, policy)
        room = reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        shown = plain(queue_view.render_view(replace(model, reading=reading), 140))
        self.assertIn("cap 26.0", shown)
        self.assertIn("Cap 26.0 GiB", queue_evidence.room_text(room))
        bar = queue_view.cap_bar(room, 60, colour=True)
        self.assertEqual(plain([bar[:9]]), "█" * 9)
        self.assertEqual([span.tone for span in bar],
            ["suggestion"] * 9 + ["warning"] * 12 + ["success"] * 5 + ["error"] + ["warning"] * 2 + ["inactive"] * 31)

    def test_unreadable_meminfo_reports_no_reading_instead_of_low_memory(self) -> None:
        _ = fake_memory_readings(self.fixture.root, full_avg10=0.0)
        model = self.fixture.cap_model()
        (self.fixture.root / "meminfo").unlink()
        reading = self.fixture.read()
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertIsInstance(head.cause, gate.ReadingUnavailable)
        self.assertFalse(head.free.passes)
        model = replace(model, reading=reading)
        lines = queue_view.render_view(model, 180, colour=True)
        first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
        self.assertTrue(plain([first]).endswith("memory reading unavailable"))
        self.assertNotIn("low memory", plain([first]))
        self.assertNotIn("pressure", plain([first]))
        self.assertTrue(any("memory reading unavailable" in span.text and span.tone == "error" for span in first))

    def test_head_room_check_uses_the_recorded_host_and_keep_aside(self) -> None:
        model = self.fixture.model()
        for available, expected_room, expected_passes in ((10, 1, False), (20, 11, True)):
            with self.subTest(available=available):
                inputs = self.fixture.inputs(policy=admission_policy_fixture())
                inputs = replace(inputs, pressure=gate.NoPressureHold(),
                    host=gate.HostMemory(available * gate.GIB, 60 * gate.GIB),
                    reservations=tuple({**row, "need": 6 * gate.GIB} for row in inputs.reservations),
                    running=(replace(inputs.running[0], promise=6 * gate.GIB,
                        measured=gate.MeasuredMemory(0, anonymous=gate.AnonymousRunUse(0))),))
                reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
                head = reading.explanation.head
                assert isinstance(head, gate.HeadExplanation)
                self.assertEqual(head.room.room, expected_room * gate.GIB)
                self.assertEqual(head.room.passes, expected_passes)
                shown = replace(model, reading=reading)
                self.assertIn(queue_evidence.room_check_text(head.room),
                              plain(queue_card.render_card(shown, [1], 400)))

    def test_empty_run_set_has_room_before_the_cap_and_safety_after_it(self) -> None:
        room = self.cap_model(need=0, used=0).reading.explanation.room
        bar = queue_view.cap_bar(room, 60, colour=False)
        self.assertEqual(plain([bar]), "·" * 17 + "│" + "▒" * 2 + "█" * 40)
        self.assertTrue(all(span.tone == "success" for span in bar[:17]))
        self.assertEqual(bar[17].tone, "error")


    def test_row_styles_and_you_marker_match_the_drawn_page(self) -> None:
        lines = queue_view.render_view(self.fixture.cap_model(), 60, colour=True, you=ASKING_SESSION)
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

    def test_negative_room_shows_zero_and_the_cap_explains_the_shortfall(self) -> None:
        model = self.cap_model(available=2, need=10, used=5)
        room = model.reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        self.assertEqual((room.room, room.shortfall, room.cap), (0, 6 * gate.GIB, 4 * gate.GIB))
        bar = queue_view.cap_bar(room, 60, colour=True)
        self.assertEqual(plain([bar]), "████│" + "█" * 55)
        self.assertEqual([span.tone for span in bar[:5]], ["suggestion"] * 4 + ["error"])
        self.assertFalse(any(span.tone == "success" for span in bar))
        self.assertIn("shortfall 6.0 GiB", queue_evidence.room_text(room))
        lines = queue_view.render_view(model, 60, colour=True)
        self.assertIn("room 0.0", plain(lines))
        self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in lines))

    def test_size_bars_show_uncut_needs_with_nearest_eighth_and_zero(self) -> None:
        model = self.fixture.cap_model()
        for need, expected in ((6.8, "██████▊"), (0, ""), (25, "█" * 25)):
            with self.subTest(need=need):
                step = replace(model.reading.view.waiting[1], need=int(need * gate.GIB))
                changed = with_waiting(model, [model.reading.view.waiting[0], step])
                lines = queue_view.render_view(changed, 100, colour=True)
                row = next(line for line in lines if plain([line]).lstrip().startswith("2 "))
                self.assertEqual("".join(span.text for span in row if span.tone == "warning"), "●" + expected)
                self.assertTrue(any(f"{need:.1f}" in span.text and span.tone == "inactive" for span in row))

    def test_long_name_growth_and_missing_unit_session_fallback(self) -> None:
        model = self.fixture.cap_model()
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
                self.assertEqual(rows[1].split()[2], "—")
                self.assertNotIn("other-session", rows[1])
                self.assertEqual(rows[0].index("nextest"), rows[1].index("nextest"))

    def test_showrunner_prefix_is_removed_from_running_and_queued_units(self) -> None:
        model = self.fixture.cap_model()
        queued = model.reading.view.waiting[0]
        running = model.reading.explanation.running[0].step
        labels = dict(model.labels)
        for step in (queued, running):
            labels[step.session_id] = UnitLabels("merge-gate", "builds", "builds-queue")
        text = plain(queue_view.render_view(replace(model, labels=labels), 140))
        running_text = "\n".join(line for line in text.splitlines() if line.startswith("run"))
        queued_text = "\n".join(waiting_rows(queue_view.render_view(replace(model, labels=labels), 140)))
        for section, offset in ((running_text, 1), (queued_text, 1)):
            row, = [line for line in section.splitlines() if "builds" in line]
            self.assertEqual(row.split()[offset:offset + 2], ["builds", "queue"])
            self.assertNotIn("builds-queue", row)

    def test_queued_showrunner_without_a_unit_uses_its_session_name(self) -> None:
        model = self.fixture.cap_model()
        step = model.reading.view.waiting[0]
        for missing_unit in ("", "—"):
            with self.subTest(unit=missing_unit):
                labels = {**model.labels, step.session_id: UnitLabels("builds", "builds", missing_unit)}
                row = waiting_rows(queue_view.render_view(replace(model, labels=labels), 140))[0]
                self.assertEqual(row.split()[1:3], ["builds", "—"])

    def test_queued_staff_unit_overrides_merge_gate_session_and_folder_names(self) -> None:
        model = self.fixture.cap_model()
        step = replace(model.reading.view.waiting[0], session_name="merge-gate", folder="/tmp/merge-gate", step="merge-gate")
        labels = {**model.labels, step.session_id: UnitLabels("merge-gate", "sdlc", "devtools")}
        model = replace(with_waiting(model, [step]), labels=labels)
        lines = queue_view.render_view(model, 140)
        header, = [line for line in plain(lines).splitlines() if line.lstrip().startswith("# ")]
        row, = waiting_rows(lines)
        self.assertEqual(row[:header.index("step")].split()[1:], ["sdlc", "devtools"])
        self.assertEqual(row[header.index("step"):header.index("min")].strip(), read_step_names().shown("merge-gate"))



    def test_host_holds_keep_short_words_and_large_need_columns_aligned(self) -> None:
        model = self.cap_model()
        head = model.reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        for pressure, passes, word in ((gate.NoPressureHold(), False, "low room"),
                (gate.MemoryPressure(7.7, condition="full"), True, "pressure")):
            with self.subTest(word=word):
                step = replace(head.waiter, step="nextest", need=21 * gate.GIB)
                changed = with_waiting(model, [step, model.reading.view.waiting[1]])
                changed = replace(changed, reading=replace(changed.reading,
                    explanation=replace(changed.reading.explanation,
                        head=replace(head, waiter=step, decision=replace(head.decision,
                            pressure=pressure, free_passes=passes, starts_at=0),
                            cause=pressure if isinstance(pressure, gate.MemoryPressure) else
                                gate.NeedsMemory(step.need, head.free.available)))))
                lines = queue_view.render_view(changed, 140)
                first = next(line for line in lines if plain([line]).lstrip().startswith("1 "))
                self.assertTrue(plain([first]).endswith(word))
                self.assertTrue(any(word in span.text and span.tone == "error" for span in first))
                narrow = queue_view.render_view(changed, 60)
                rows = waiting_rows(narrow)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(terminal_width(plain([line])) <= 60 for line in narrow))
                self.assertEqual(rows[0].index("nextest"), rows[1].index("nextest"))


if __name__ == "__main__":
    _ = unittest.main()
