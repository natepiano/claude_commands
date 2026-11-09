"""Check native worker reuse and model settings across assignment retries."""
from __future__ import annotations


from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign_launcher_test_support as launcher_fixture
from scripts.direct.assign_launcher_test_support import read_object
from scripts.lib.test_models_registry import write_registry
from scripts.herdr import herdr, worker_panes
from scripts.production import staff

setUpModule = codex_fixture.setUpModule


class AssignReuseTests(launcher_fixture.FirstDispatchFixture):
    def test_a_stopped_native_worker_recovers_the_same_thread_for_named_dispatch(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        row = staff.by_id(staff.StaffId(str(original["staff_id"])))
        assert row is not None
        world = self.native.fixture.world
        source_pane = self.native.agent.submitted[-1][0]
        _ = world.stop(row, world)
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(len(world.locations), 2)
        destination = world.launches[-1]
        self.assertIsInstance(destination, herdr.SplitPane)
        self.assertNotIn(source_pane, world.locations)
        self.assertNotEqual(self.native.agent.submitted[-1][0], source_pane)

    def test_a_closed_worker_dispatches_again_on_its_recorded_thread(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        self.assertIsInstance(worker_panes.close(self.native.fixture.parent.id, staff.WorkerName.parse(name)),
                              worker_panes.Closed)
        self.assertEqual(self.assignment_records()[name], original)
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(len(self.native.fixture.world.locations), 2)

    def test_an_unproved_native_worker_receives_no_replacement_or_assignment(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        world = self.native.fixture.world
        world.unknown["w1:p1"] = "native process evidence unavailable"
        launches = list(world.launches)
        result = self.launch("implement", "--to", name)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("native process evidence unavailable", (self.session_dir / f"phases/7/{name}_agent.log").read_text())
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(world.launches, launches)
        self.assertEqual(self.turn_starts, 1)

    def test_named_dispatch_reuses_a_thread_and_advances_the_manifest_number(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        _ = self.successful("test", "--to", name)
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(self.session_dir / "phases/7/dispatch_2_paths.json")["worker"], name)

    def test_explicit_turn_settings_reuse_the_named_worker_without_rewriting_its_identity(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        for options, expected in ((["--model", "gpt-other-sol:high"], ("gpt-other-sol", "high")),
                                  (["--effort", "max"], ("gpt-kept-sol", "max"))):
            with self.subTest(options=options):
                _ = self.successful("implement", "--to", name, *options)
                command = self.native.fixture.world.requests[-1][1]
                self.assertEqual(command[command.index("--model") + 1], expected[0])
                self.assertIn(f'model_reasoning_effort="{expected[1]}"', command)
                self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(self.turn_starts, 3)

    def test_a_model_override_reuses_an_idle_worker_with_the_requested_settings(self) -> None:
        self.first_assignment()
        name, before = next(iter(self.assignment_records().items()))
        _ = self.successful("implement", "--model", "gpt-other-sol:high")
        self.assertEqual(self.assignment_records(), {name: before})
        command = self.native.fixture.world.requests[-1][1]
        self.assertEqual(command[command.index("--model") + 1], "gpt-other-sol")
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(self.turn_starts, 2)

    def test_a_saved_identity_model_is_history_while_the_turn_uses_the_registry(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        identity_path = self.session_dir / f"impl_worker_{name}.json"
        _ = identity_path.write_text('{"staff_id": "' + str(original["staff_id"]) + '", "model": "gpt-other-sol"}')
        saved = identity_path.read_bytes()
        _ = self.successful("implement", "--to", name)
        command = self.native.fixture.world.requests[-1][1]
        self.assertEqual(command[command.index("--model") + 1], "gpt-kept-sol")
        self.assertEqual(identity_path.read_bytes(), saved)
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(self.turn_starts, 2)

    def test_changed_model_config_takes_effect_on_the_next_launch(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-other-sol:max"}}, update=True)
        row = staff.by_id(staff.StaffId(str(original["staff_id"])))
        assert row is not None
        _ = self.native.fixture.world.stop(row, self.native.fixture.world)
        _ = self.successful("implement", "--to", name)
        command = self.native.fixture.world.requests[-1][1]
        self.assertEqual(command[command.index("--model") + 1], "gpt-other-sol")
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(self.turn_starts, 2)
