"""Check named assignment identity with the shared loopback fixture."""
from __future__ import annotations

import json
import os
from typing import cast, final
from scripts.agents import test_codex_mesh as mesh_fixture
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.direct import workers
from scripts.direct.assign_launcher_test_support import read_object
from scripts.direct import assign_launcher_test_support as launcher_fixture


setUpModule = mesh_fixture.setUpModule


@final
class AssignIdentityTests(launcher_fixture.AssignLauncherFixture):
    def test_review_assignments_resume_the_same_read_only_reviewer_across_phases(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        review_prompt = (self.session_dir / "phases/7/dispatch_1_prompt.txt").read_text()
        self.assertIn("Return the complete review result in your final reply", review_prompt)
        self.assertNotIn("write your summary to", review_prompt)
        self.assertNotIn("review_summary_contract_1.txt", review_prompt)
        name, first = next(iter(self.assignment_records().items()))
        thread = first["thread_id"]
        self.record("finish-phase", "--phase-id", "7", "--status", "completed")
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Next phase")
        self.write_phase_prompt("8", "Next phase", "Review the next change.")
        _ = self.successful("review", "--phase", "8", "--lens", "contract", "--pass", "2")
        records = self.assignment_records()
        self.assertEqual(set(records), {name})
        self.assertEqual(records[name]["thread_id"], thread)
        self.assertEqual(records[name]["role"], "review")
        self.assertEqual(records[name]["access_policy"], {"kind": "ReadOnlyReviewer"})
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.methods("thread/fork"), [])
        self.assertEqual(len(self.methods("turn/start")), 2)
        ledger = workers.ledger_for_run(self.session_dir)
        with ledger.locked() as saved:
            self.assertEqual(saved.workers[name].backend,
                             workers.CodexThread(str(thread), os.environ["CODEX_HOME"]))

    def test_unit_reviews_reuse_their_reviewer_across_passes(self) -> None:
        self.commit_project()
        for pass_number in ("1", "2"):
            option = "--new-worker" if pass_number == "1" else "--to"
            name = "project-review"
            self.review_launcher(option, name, pass_number)
            turns_started = self.methods("turn/start")
            self.assertEqual(len(turns_started), int(pass_number))
            self.assertEqual(cast(dict[str, object], turns_started[-1]["params"])["sandboxPolicy"],
                             {"type": "readOnly", "networkAccess": False})
        records = self.assignment_records()
        self.assertEqual(set(records), {"project-review"})
        self.assertEqual(records["project-review"]["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(records["project-review"]["role"], "review")
        self.assertEqual(records["project-review"]["access_policy"], {"kind": "ReadOnlyReviewer"})
        self.assertEqual(cast(dict[str, object], self.methods("thread/start")[0]["params"])["sandbox"],
                         "read-only")
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.methods("thread/fork"), [])
        self.assertEqual(len(self.methods("turn/start")), 2)
        turns = [read_object(path) for path in (self.history / "review_turns" / self.session_dir.name).glob("*.json")]
        self.assertEqual(len(turns), 2)
        self.assertTrue(all(turn["arm"] == "own" for turn in turns))

    def test_a_review_worker_dispatched_for_implementation_keeps_one_durable_identity(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name, original = next(iter(self.assignment_records().items()))
        _ = self.successful("implement", "--to", name)
        identities = [read_object(path) for path in self.session_dir.rglob("impl_worker_*.json")]
        matched = [identity for identity in identities if identity.get("staff_id") == original["staff_id"]]
        self.assertEqual(len(matched), 1, identities)
        self.assertEqual(matched[0]["staff_id"], original["staff_id"])
        self.assertNotIn("role", matched[0])
        self.assertEqual(self.assignment_records()[name]["role"], "impl")
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_replacement_after_review_to_implementation_keeps_writable_access(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name, before = next(iter(self.assignment_records().items()))
        self.failing_method = "thread/resume"
        self.next_thread_id = fixture_runtime_id("replacement-implementation-thread")
        _ = self.successful("implement", "--to", name)
        after = self.assignment_records()[name]
        self.assertNotEqual(after["thread_id"], before["thread_id"])
        replacement = cast(dict[str, object], self.methods("thread/start")[-1]["params"])
        self.assertEqual(replacement["sandbox"], "danger-full-access")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(cast(dict[str, object], turn["sandboxPolicy"])["type"], "dangerFullAccess")
        self.assertEqual(after["role"], "impl")
        self.assertEqual(after["access_policy"], {"kind": "WorkerLaunchPolicy"})

    def test_a_saved_slot_keys_the_claim_and_board_for_a_full_worker_address(self) -> None:
        name = "project-impl"
        _ = self.successful("implement", "--new-worker", name)
        identity_path = self.identity_path(name)
        identity = read_object(identity_path)
        identity["slot"] = "impl"
        _ = identity_path.write_text(json.dumps(identity))
        saved_path = self.session_dir / "impl_worker_impl.json"
        _ = identity_path.rename(saved_path)
        observed_claims: list[dict[str, object]] = []
        original = self.server.respond

        @self.hang_guard.callback
        def observe_saved_slot(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                observed_claims.append(read_object(self.session_dir / "impl_claim_impl.json"))
                self.assertFalse((self.session_dir / f"impl_claim_{name}.json").exists())
            return original(request)

        self.server.respond = observe_saved_slot
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(len(observed_claims), 1)
        self.assertEqual(observed_claims[0]["worker"], name)
        self.assertEqual(observed_claims[0]["pid"], self.claims[-1]["launcher_pid"])
        self.assertEqual(read_object(saved_path), identity)
        registrations = [line for line in (self.session_dir / "board.log").read_text().splitlines()
                         if "] register:" in line]
        self.assertIn("[impl] register:", registrations[-1])
        self.assertIn(f"mesh={name};", registrations[-1])
        self.assertFalse((self.session_dir / "impl_claim_impl.json").exists())

    def test_two_implementation_workers_have_distinct_board_slot_keys(self) -> None:
        _ = self.successful("implement", "--new-worker", "impl")
        self.next_thread_id = fixture_runtime_id("second-implementation-thread")
        _ = self.successful("implement", "--new-worker", "impl2")
        registrations = [line for line in (self.session_dir / "board.log").read_text().splitlines()
                         if "] register:" in line]
        for address in ("impl", "impl2"):
            matched = [line for line in registrations if f"mesh={address};" in line]
            self.assertEqual(len(matched), 1, registrations)
            self.assertIn(f"[{address}] register:", matched[0])
            self.assertIn("fixture-production · fixture ·", matched[0])

    def test_a_busy_named_worker_refuses_without_creating_another_dispatch_or_pass(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record.update({"status": "running", "launcher_pid": os.getpid(), "turn_id": "busy-turn"})
        self.write_pool_roster({name: record})
        started = [event for event in self.events() if event.get("event_type") == "pass_started"]
        result = self.launch("implement", "--to", name,
                             execution=launcher_fixture.LauncherScript(launcher_fixture.ASSIGN))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(len([event for event in self.events() if event.get("event_type") == "pass_started"]),
                         len(started))
        self.assertFalse((self.session_dir / "phases/7/dispatch_2_paths.json").exists())

    def test_reactivating_a_named_finished_run_keeps_its_original_thread(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        self.record("finish-run", "--status", "completed")
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records()[name]["thread_id"], original["thread_id"])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(read_object(self.session_dir / "progress_history_state.json")["status"], "active")
        self.assertTrue(any(event.get("event_type") == "reopened" for event in self.events()))

    def test_new_worker_uses_its_requested_address_and_full_access_sandbox(self) -> None:
        _ = self.successful("test", "--new-worker", "address-without-role")
        self.assertEqual(set(self.assignment_records()), {"address-without-role"})
        self.assertEqual(self.assignment_records()["address-without-role"]["role"], "test")
        self.assertEqual(cast(dict[str, object], self.methods("thread/start")[0]["params"])["sandbox"],
                         "danger-full-access")
