"""Check fixed staff identity and native review permissions across assignments."""
from __future__ import annotations

from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign_launcher_test_support as launcher_fixture
from scripts.direct.assign_launcher_test_support import read_object
from scripts.herdr import worker_panes
from scripts.production import staff

setUpModule = codex_fixture.setUpModule


class AssignIdentityTests(launcher_fixture.FirstDispatchFixture):
    def test_review_assignments_resume_the_same_read_only_reviewer_across_phases(self) -> None:
        self.first_assignment("review", "--lens", "contract", "--pass", "1")
        review_prompt = (self.session_dir / "phases/7/review_prompt_contract_1.txt").read_text()
        self.assertIn("Return the complete review result in your final reply", review_prompt)
        self.assertNotIn("write your summary to", review_prompt)
        name, first = next(iter(self.assignment_records().items()))
        self.record("finish-phase", "--phase-id", "7", "--status", "completed")
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Next phase")
        self.write_phase_prompt("8", "Next phase", "Review the next change.")
        _ = self.successful("review", "--to", name, "--phase", "8", "--lens", "contract", "--pass", "2")
        self.assertEqual(self.assignment_records(), {name: first})
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(len(self.native.fixture.world.launches), 2)
        self.assertEqual([command[command.index("--sandbox") + 1]
                          for _, command, _ in self.native.fixture.world.requests], ["read-only", "read-only"])
        row = staff.by_id(staff.StaffId(str(first["staff_id"])))
        assert row is not None
        self.assertIsInstance(staff.backend(row), staff.CodexBackend)

    def test_unit_reviews_reuse_their_reviewer_across_passes(self) -> None:
        self.commit_project()
        for pass_number in ("1", "2"):
            option = "--new-worker" if pass_number == "1" else "--to"
            _ = self.successful("review", option, "project-review", "--lens", "contract", "--pass", pass_number)
            self.assertEqual(self.turn_starts, int(pass_number))
        self.assertEqual(set(self.assignment_records()), {"project-review"})
        self.assertEqual(len(self.native.fixture.world.launches), 2)
        self.assertEqual([command[command.index("--sandbox") + 1]
                          for _, command, _ in self.native.fixture.world.requests], ["read-only", "read-only"])
        turns = [read_object(path) for path in (self.history / "review_turns" / self.session_dir.name).glob("*.json")]
        self.assertEqual(len(turns), 2)
        self.assertTrue(all(turn["arm"] == "own" for turn in turns))

    def test_a_review_worker_dispatched_for_implementation_keeps_one_durable_identity(self) -> None:
        self.first_assignment("review", "--lens", "contract", "--pass", "1")
        name, original = next(iter(self.assignment_records().items()))
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records(), {name: original})
        row = staff.by_id(staff.StaffId(str(original["staff_id"])))
        assert row is not None
        self.assertEqual(worker_panes.dispatch_role(row), "impl")
        command = self.native.fixture.world.requests[-1][1]
        self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
        self.assertEqual(self.turn_starts, 2)

    def test_a_saved_slot_keys_the_claim_and_board_for_a_full_worker_address(self) -> None:
        name = "project-impl"
        _ = self.successful("implement", "--new-worker", name)
        original = self.assignment_records()[name]
        _ = self.successful("fix", "--to", name, "--fix-cause", "Repair the fixture defect")
        self.assertEqual(self.assignment_records()[name], original)
        board = (self.session_dir / "board.log").read_text()
        self.assertIn(f"[{name}] register:", board)
        passes = [event for event in self.events() if event.get("event_type") == "pass_started"]
        self.assertEqual([event["worker_id"] for event in passes], [original["staff_id"], original["staff_id"]])
        self.assertEqual(self.turn_starts, 2)

    def test_two_implementation_workers_have_distinct_board_slot_keys(self) -> None:
        for name in ("impl", "impl2"):
            _ = self.successful("implement", "--new-worker", name)
        self.assertEqual(set(self.assignment_records()), {"impl", "impl2"})
        registrations = [line for line in (self.session_dir / "board.log").read_text().splitlines()
                         if "] register:" in line]
        for name in ("impl", "impl2"):
            self.assertEqual(sum(f"[{name}] register:" in line for line in registrations), 1)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(len(self.native.fixture.world.launches), 2)

    def test_reactivating_a_named_finished_run_keeps_its_original_thread(self) -> None:
        self.first_assignment()
        name, original = next(iter(self.assignment_records().items()))
        self.record("finish-run", "--status", "completed")
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records()[name], original)
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "active")
        self.assertTrue(any(event.get("event_type") == "reopened" for event in self.events()))
        self.assertEqual(self.turn_starts, 2)

    def test_new_worker_uses_its_requested_address_and_full_access_sandbox(self) -> None:
        _ = self.successful("test", "--new-worker", "address-without-role")
        self.assertEqual(set(self.assignment_records()), {"address-without-role"})
        row = staff.by_id(staff.StaffId(str(self.assignment_records()["address-without-role"]["staff_id"])))
        assert row is not None
        self.assertEqual(worker_panes.dispatch_role(row), "test")
        command = self.native.fixture.world.requests[0][1]
        self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
