"""Named assignment dispatches through the real recorder and loopback app-server."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import workers
from scripts.direct.recorder_fixture import invoke_recorder, communicate_launcher


ROOT = Path(__file__).resolve().parents[2]
ASSIGN = ROOT / "scripts/direct/assign.sh"


def setUpModule() -> None:
    mesh_fixture.setUpModule()


def read_object(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text()))


@final
class AssignLauncherTests(mesh_fixture.MeshCommandFixture):
    project: Path = Path()
    history: Path = Path()
    paths: Path = Path()
    prompt: Path = Path()
    registry: Path = Path()
    claims: list[dict[str, object]]
    reservations: list[dict[str, object]]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.claims = []
        self.reservations = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.project = self.session_dir.parent / "project"
        self.project.mkdir()
        _ = (self.project / "plan.md").write_text("## Delegation Context\n\n- **Project:** Assignment fixture\n")
        self.history = self.session_dir.parent / "history"
        self.paths = self.session_dir.parent / "paths.txt"
        _ = self.paths.write_text("src/worker.py\ntests/test_worker.py\n")
        self.prompt = self.session_dir.parent / "assignment.txt"
        _ = self.prompt.write_text("Phase 7 — Assignment core\nImplement the assigned change.\n")
        self.registry = self.session_dir.parent / "agents.conf"
        _ = self.registry.write_text(
            "[codex.agents]\ngpt-kept-sol=low,medium,high,xhigh,max\n" +
            "gpt-other-sol=low,medium,high,xhigh,max\n[assignments]\ndirect=codex\n" +
            "[direct.codex]\nimpl=gpt-kept-sol:high\ntest=gpt-kept-sol:high\n" +
            "fix=gpt-kept-sol:high\nreview=gpt-kept-sol:high\n")
        checkpoint = self.session_dir.parent / "catalog-current"
        checkpoint.touch()
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
            "UNIT_HISTORY_DIR": str(self.history),
            "UNIT_ACTIVE_DIR": str(self.session_dir.parent / "active"),
            "UNIT_CONFIG": str(ROOT / "config/direct.conf"),
            "AGENTS_CONFIG_FILE": str(self.registry),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(checkpoint),
            "NOTIFIER_SESSIONS_DIR": str(self.session_dir.parent / "sessions"),
            "SHUTDOWN_STATE_DIR": str(self.session_dir.parent / "shutdown"),
            "UNIT_CODEX_MESH": "1", "UNIT_SYNC_WRITES": "0",
            "PYTHONPATH": str(ROOT), "TMPDIR": "/tmp",
        }))
        self.record("start-run", "--working-dir", str(self.project), "--plan-doc", "plan.md",
                    "--main-family", "codex", "--main-model", "gpt-main", "--main-effort", "high",
                    "--main-session-id", "assign-director")
        self.record("set-unit", "--production", "fixture-production", "--unit", "fixture-unit")
        self.record("start-phase", "--phase-id", "7", "--phase-title", "Assignment core")
        self.claims = []
        self.reservations = []
        original = self.server.respond

        @mesh_fixture.bounded_fixture
        def observe(request: dict[str, object]) -> list[dict[str, object]] | None:
            roster_path = self.session_dir / codex_mesh.ROSTER_FILE
            if roster_path.exists() and roster_path.stat().st_size:
                for value in read_object(roster_path).values():
                    record = cast(dict[str, object], value)
                    if record.get("allocation_pending"):
                        owner = record.get("allocation_owner_pid")
                        if isinstance(owner, int):
                            os.kill(owner, 0)
                        self.reservations.append(record.copy())
            if request.get("method") == "turn/start":
                self.claims.extend(cast(dict[str, object], entry).copy() for entry in
                                   read_object(self.session_dir / codex_mesh.ROSTER_FILE).values())
            frames = original(request)
            params = cast(dict[str, object], request.get("params", {}))
            thread = params.get("threadId")
            if frames is not None and isinstance(thread, str) and thread != mesh_fixture.THREAD_ID:
                return cast(list[dict[str, object]], json.loads(
                    json.dumps(frames).replace(mesh_fixture.THREAD_ID, thread)))
            return frames

        self.server.respond = observe

    def record(self, *arguments: str) -> None:
        result = invoke_recorder([arguments[0], "--session-dir", str(self.session_dir), *arguments[1:]],
                                 os.environ.copy(), self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def launch(self, role: str = "implement", *options: str,
               paths: bool = True, session: bool = True,
               script: Path = ASSIGN) -> subprocess.CompletedProcess[str]:
        arguments = ["bash", str(script), "--role", role,
                     "--cwd", str(self.project), "--phase", "7", "--prompt", str(self.prompt)]
        if session:
            arguments.extend(["--session-dir", str(self.session_dir)])
        if paths and role in {"implement", "test", "fix"}:
            arguments.extend(["--paths", str(self.paths)])
        arguments.extend(options)
        with subprocess.Popen(arguments, env=os.environ.copy(), cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True) as child:
            output, errors = communicate_launcher(child, timeout=45)
            return subprocess.CompletedProcess(arguments, child.returncode, output, errors)

    def successful(self, role: str = "implement", *options: str) -> subprocess.CompletedProcess[str]:
        result = self.launch(role, *options)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assignment_records(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], read_object(self.session_dir / codex_mesh.ROSTER_FILE))

    def events(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in
                (self.history / "runs" / f"{self.session_dir.name}.jsonl").read_text().splitlines()]

    def test_missing_required_options_and_positionals_refuse_before_opening_a_thread(self) -> None:
        for options in ([], ["--prompt", str(self.prompt)], ["--role", "test"], ["test", str(self.prompt)]):
            with self.subTest(options=options):
                result = subprocess.run(["bash", str(ASSIGN), *options], cwd=ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertEqual(self.methods("thread/start"), [])

    def test_future_roles_are_refused_before_any_thread_is_opened(self) -> None:
        for role in ("research", "propose"):
            with self.subTest(role=role):
                result = self.launch(role)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_paths_are_required_and_review_paths_are_refused(self) -> None:
        for role in ("implement", "test", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.launch(role, paths=False).returncode, 2)
        self.assertEqual(self.launch("review", "--paths", str(self.paths)).returncode, 2)
        self.assertEqual(self.methods("thread/start"), [])

    def test_unsafe_empty_and_escaping_paths_refuse_before_claim(self) -> None:
        outside = self.project.parent / "outside"
        outside.mkdir()
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("", "../secret", "/tmp/secret", "src/../secret", "linked/secret"):
            with self.subTest(path=path):
                _ = self.paths.write_text(path + "\n")
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if path:
                    self.assertIn(path, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_dispatch_records_paths_rules_identity_and_a_finished_pass(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["role"], "impl")
        self.assertEqual(self.turn_starts, 1)
        prompt = self.turn_inputs[0]
        self.assertIn(self.prompt.read_text().strip(), prompt)
        for text in ("mesh_runs.py start", "Simple, Fast and Beautiful", "poll"):
            self.assertIn(text, prompt)
        manifest = self.session_dir / "phases/7/dispatch_1_paths.json"
        self.assertEqual(read_object(manifest), {"worker": name, "round": 1,
                                               "paths": ["src/worker.py", "tests/test_worker.py"]})
        self.assertEqual((manifest.parent / "impl_status").read_text().strip(), "done")
        self.assertTrue((manifest.parent / "impl_summary.txt").is_file())
        self.assertTrue((manifest.parent / "impl_agent.log").is_file())
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertTrue(self.claims)
        launcher_pid = self.claims[0].get("launcher_pid")
        self.assertIsInstance(launcher_pid, int)
        self.assertTrue(self.reservations, "reservation must survive until can-follow probes the server")
        self.assertEqual(self.reservations[-1].get("allocation_owner_pid"), launcher_pid)

    def test_review_is_read_only_and_thread_board_and_summary_use_the_shown_context(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["role"], "review")
        starts = self.methods("thread/start")
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["sandbox"], "read-only")
        names = [str(cast(dict[str, object], request["params"])["name"])
                 for request in self.methods("thread/name/set")]
        self.assertTrue(names)
        for shown in names:
            self.assertIn("fixture-production · fixture ·", shown)
            self.assertNotIn(f"({name})", shown)
        board = (self.session_dir / "board.log").read_text()
        self.assertIn(names[-1], board)
        self.assertTrue((self.session_dir / "phases/7/review_status_contract_1").exists())

    def test_named_dispatch_reuses_a_thread_and_advances_the_manifest_number(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        _ = self.successful("test", "--to", name)
        self.assertEqual(self.assignment_records()[name]["thread_id"], original["thread_id"])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(self.session_dir / "phases/7/dispatch_2_paths.json")["worker"], name)

    def test_an_explicit_model_mismatch_refuses_without_changing_the_named_worker(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        for options, requested in ((["--model", "gpt-other-sol:high"], "gpt-other-sol"),
                                   (["--effort", "max"], "max")):
            with self.subTest(options=options):
                result = self.launch("implement", "--to", name, *options)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("gpt-kept-sol", result.stdout + result.stderr)
                self.assertIn(requested, result.stdout + result.stderr)
                self.assertEqual((self.assignment_records()[name]["model"],
                                  self.assignment_records()[name]["effort"]),
                                 (original["model"], original["effort"]))
        self.assertEqual(self.turn_starts, 1)

    def test_new_worker_uses_its_requested_address_and_full_access_sandbox(self) -> None:
        _ = self.successful("test", "--new-worker", "address-without-role")
        self.assertEqual(set(self.assignment_records()), {"address-without-role"})
        self.assertEqual(self.assignment_records()["address-without-role"]["role"], "test")
        self.assertEqual(cast(dict[str, object], self.methods("thread/start")[0]["params"])["sandbox"],
                         "danger-full-access")

    def test_model_override_selects_another_worker_and_preserves_the_first_workers_identity(self) -> None:
        _ = self.successful()
        name, before = next(iter(self.assignment_records().items()))
        self.next_thread_id = "override-thread"
        _ = self.successful("implement", "--model", "gpt-other-sol:high")
        roster = self.assignment_records()
        self.assertEqual((roster[name]["thread_id"], roster[name]["model"], roster[name]["effort"]),
                         (before["thread_id"], before["model"], before["effort"]))
        others = [record for address, record in roster.items() if address != name]
        self.assertEqual(len(others), 1, roster)
        self.assertEqual((others[0]["model"], others[0]["effort"]), ("gpt-other-sol", "high"))

    def test_a_review_worker_dispatched_for_implementation_keeps_one_durable_identity(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name, original = next(iter(self.assignment_records().items()))
        _ = self.successful("implement", "--to", name)
        identities = [read_object(path) for path in self.session_dir.rglob("impl_worker_*.json")]
        matched = [identity for identity in identities if identity.get("name") == name]
        self.assertEqual(len(matched), 1, identities)
        self.assertEqual(matched[0]["worker_id"], original["thread_id"])
        self.assertNotIn("role", matched[0])
        self.assertEqual(self.assignment_records()[name]["role"], "impl")
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_replacement_after_review_to_implementation_keeps_writable_access(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name, before = next(iter(self.assignment_records().items()))
        self.failing_method = "thread/resume"
        self.next_thread_id = "replacement-implementation-thread"
        _ = self.successful("implement", "--to", name)
        after = self.assignment_records()[name]
        self.assertNotEqual(after["thread_id"], before["thread_id"])
        replacement = cast(dict[str, object], self.methods("thread/start")[-1]["params"])
        self.assertEqual(replacement["sandbox"], "danger-full-access")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(cast(dict[str, object], turn["sandboxPolicy"])["type"], "dangerFullAccess")
        self.assertEqual(after["role"], "impl")
        self.assertEqual(after["access_policy"], {"kind": "WorkerLaunchPolicy"})

    def test_a_corrupt_durable_family_or_worker_id_refuses_before_the_next_turn(self) -> None:
        _ = self.successful()
        name = next(iter(self.assignment_records()))
        identity_path = next(path for path in self.session_dir.glob("impl_worker_*.json")
                             if read_object(path).get("name") == name)
        identity = read_object(identity_path)
        for field, value in (("family", "claude"), ("worker_id", "another-worker-thread")):
            with self.subTest(field=field):
                damaged = {**identity, field: value}
                _ = identity_path.write_text(json.dumps(damaged))
                result = self.launch("implement", "--to", name)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"{name} has no matching durable worker identity for slot {name}",
                              result.stdout + result.stderr)
                self.assertEqual(read_object(identity_path), damaged)
                self.assertEqual(self.turn_starts, 1)
        _ = identity_path.write_text(json.dumps(identity))

    def test_a_saved_slot_keys_the_claim_and_board_for_a_full_worker_address(self) -> None:
        name = "project-impl"
        _ = self.successful("implement", "--new-worker", name)
        identity_path = next(path for path in self.session_dir.glob("impl_worker_*.json")
                             if read_object(path).get("name") == name)
        identity = read_object(identity_path)
        identity["slot"] = "impl"
        _ = identity_path.write_text(json.dumps(identity))
        saved_path = self.session_dir / "impl_worker_impl.json"
        _ = identity_path.rename(saved_path)
        observed_claims: list[dict[str, object]] = []
        original = self.server.respond

        @mesh_fixture.bounded_fixture
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

    def test_a_saved_identity_model_is_accepted_while_the_turn_keeps_the_roster_model(self) -> None:
        _ = self.successful()
        name = next(iter(self.assignment_records()))
        identity_path = next(path for path in self.session_dir.glob("impl_worker_*.json")
                             if read_object(path).get("name") == name)
        identity = read_object(identity_path)
        identity["model"] = "gpt-other-sol"
        _ = identity_path.write_text(json.dumps(identity))
        _ = self.successful("implement", "--to", name)
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-kept-sol")
        self.assertEqual(read_object(identity_path)["model"], "gpt-other-sol")
        self.assertEqual(self.turn_starts, 2)

    def test_a_legacy_roster_without_a_model_uses_the_saved_model_after_registry_changes(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        original_thread = record["thread_id"]
        del record["model"]
        self.write_pool_roster({name: record})
        _ = self.registry.write_text(self.registry.read_text().replace(
            "impl=gpt-kept-sol:high", "impl=gpt-other-sol:high"))
        _ = self.successful("implement", "--to", name)
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-kept-sol")
        self.assertEqual(self.assignment_records()[name]["thread_id"], original_thread)
        self.assertEqual(len(self.methods("thread/start")), 1)
        refused = self.launch("implement", "--to", name, "--model", "gpt-other-sol")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("gpt-kept-sol", refused.stdout + refused.stderr)
        self.assertIn("gpt-other-sol", refused.stdout + refused.stderr)
        self.assertEqual(self.turn_starts, 2)

    def test_two_implementation_workers_have_distinct_board_slot_keys(self) -> None:
        _ = self.successful("implement", "--new-worker", "impl")
        self.next_thread_id = "second-implementation-thread"
        _ = self.successful("implement", "--new-worker", "impl2")
        registrations = [line for line in (self.session_dir / "board.log").read_text().splitlines()
                         if "] register:" in line]
        for address in ("impl", "impl2"):
            matched = [line for line in registrations if f"mesh={address};" in line]
            self.assertEqual(len(matched), 1, registrations)
            self.assertIn(f"[{address}] register:", matched[0])
            self.assertIn("fixture-production · fixture ·", matched[0])

    def prepare_launcher(self) -> tuple[Path, Path]:
        # Allocate only the folder here; the phase and dispatch helpers stay real.
        tree = self.session_dir.parent / "launcher/scripts"
        direct = tree / "direct"
        direct.mkdir(parents=True)
        for source in (ROOT / "scripts/direct").iterdir():
            if source.is_file() and source.name not in {"assign.sh", "assign.py", "prepare_session.sh"}:
                (direct / source.name).symlink_to(source)
        for directory in ("agents", "lib", "production"):
            (tree / directory).symlink_to(ROOT / "scripts" / directory, target_is_directory=True)
        _ = shutil.copy2(ASSIGN, direct / "assign.sh")
        coordinator = ASSIGN.with_suffix(".py")
        if coordinator.exists():
            _ = shutil.copy2(coordinator, direct / "assign.py")
        prepared = self.session_dir.parent / "prepare-called"
        prepare = direct / "prepare_session.sh"
        _ = prepare.write_text(f"#!/bin/sh\ntouch '{prepared}'\nprintf '%s\\n' '{self.session_dir}'\n")
        prepare.chmod(0o755)
        return direct / "assign.sh", prepared

    def test_missing_run_calls_prepare_and_prints_the_prepared_folder(self) -> None:
        script, prepared = self.prepare_launcher()
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, script=script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        self.assertIn(str(self.session_dir), result.stdout)
        self.assertEqual(self.turn_starts, 1)

    def test_a_fresh_run_takes_its_phase_title_from_the_dispatch_prompt(self) -> None:
        (self.session_dir / "progress_history_state.json").unlink()
        shutil.rmtree(self.history)
        shutil.rmtree(self.session_dir / "phases", ignore_errors=True)
        script, prepared = self.prepare_launcher()
        director = "fresh-assignment-director"
        transcript = Path.home() / ".claude/projects/assign-fixture" / f"{director}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "assistant", "message": {"model": "fixture-director"},
                                              "effort": "high"}) + "\n")
        self.addCleanup(transcript.unlink, missing_ok=True)
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        environment["CLAUDE_CODE_SESSION_ID"] = director
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, script=script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        phase = cast(dict[str, object], read_object(self.session_dir / "progress_history_state.json")["phase"])
        self.assertEqual((phase["id"], phase["title"]), ("7", "Assignment core"))
        self.assertEqual(self.turn_starts, 1)
        self.assertTrue(any(event.get("event_type") == "pass_finished" for event in self.events()))

    def test_default_session_uses_the_callers_marker_instead_of_starting_another_run(self) -> None:
        marker = Path(os.environ["UNIT_ACTIVE_DIR"]) / "fixture-caller"
        marker.parent.mkdir(parents=True, exist_ok=True)
        _ = marker.write_text(str(self.session_dir))
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "fixture-caller"}):
            result = self.launch(session=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(marker.read_text(), str(self.session_dir))

    def test_an_early_review_receives_its_final_diff_once_after_the_ready_event(self) -> None:
        ready = self.session_dir.parent / "final.ready"
        self.outcomes = ["disconnect"]
        self.server.hold_turn_completion = True
        self.server.answer_requests_during_held_turn = True
        self.server.held_turn_completes_successfully = True
        original = self.server.respond
        deliveries: list[str] = []

        @mesh_fixture.bounded_fixture
        def final_diff(request: dict[str, object]) -> list[dict[str, object]] | None:
            params = cast(dict[str, object], request.get("params", {}))
            if request.get("method") == "turn/start" and "Stage 2" in json.dumps(params):
                self.assertTrue(ready.exists(), "final diff was sent before its ready event")
                deliveries.append(json.dumps(params))
            frames = original(request)
            if request.get("method") == "turn/start" and self.turn_starts == 1 and frames is not None:
                self.live_turn = f"turn-{self.turn_starts}"
                frames.append({"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": self.live_turn,
                    "item": {"id": "review-answer", "type": "agentMessage", "text": "Review complete."},
                }})
            return frames

        self.server.respond = final_diff
        with ThreadPoolExecutor(max_workers=1) as executor:
            launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                       "--early-ready", str(ready))
            try:
                self.assertTrue(self.server.held_turn_ready.wait(timeout=15), "review did not start")
                self.assertEqual(deliveries, [])
                address = next(iter(self.assignment_records()))
                pending = self.session_dir / f"{address}.pending.json"
                watch = (codex_mesh.LinuxFileChanges({pending}) if sys.platform.startswith("linux")
                         else codex_mesh.KqueueFileChanges({pending}))
                try:
                    wake, peer = socket.socketpair()
                    with wake, peer:
                        budget = mesh_fixture.FixtureLoopBudget("Stage 2 inbox delivery", seconds=15)
                        ready.touch()
                        while True:
                            remaining = budget.check()
                            if pending.exists():
                                with pending.open() as inbox:
                                    fcntl.flock(inbox, fcntl.LOCK_SH)
                                    if "Stage 2" in inbox.read():
                                        break
                            event = watch.wait(wake, time.monotonic() + remaining)
                            self.assertNotEqual(event, codex_mesh.WaitDeadlineExpired.EXPIRED,
                                                "ready event delivered no Stage 2 message")
                finally:
                    watch.close()
                self.live_turn = ""
                self.server.hold_turn_completion = False
                self.server.release_held_turn.set()
                result = launched.result(timeout=30)
            finally:
                self.server.release_held_turn.set()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(deliveries), 1, deliveries)
        self.assertIn("Stage 2", deliveries[0])
        self.assertIn(str(ready.with_suffix(".diff")), deliveries[0])

    def test_an_early_review_that_finishes_stage_one_waits_for_stage_two_to_finish(self) -> None:
        ready = self.session_dir.parent / "final.ready"
        reply = self.session_dir / "phases/7/review_reply_contract_1.txt"
        status = reply.with_name("review_status_contract_1")
        original = self.server.respond
        deliveries: list[str] = []

        @mesh_fixture.bounded_fixture
        def hold_final_review(request: dict[str, object]) -> list[dict[str, object]] | None:
            params = cast(dict[str, object], request.get("params", {}))
            stage_two = request.get("method") == "turn/start" and "Stage 2:" in json.dumps(params)
            if stage_two:
                self.assertTrue(ready.exists())
                deliveries.append(json.dumps(params))
                self.outcomes = ["disconnect"]
                self.server.hold_turn_completion = True
                self.server.answer_requests_during_held_turn = True
                self.server.held_turn_completes_successfully = True
            frames = original(request)
            if stage_two and frames is not None:
                self.live_turn = f"turn-{self.turn_starts}"
                frames.append({"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": self.live_turn,
                    "item": {"id": "final-review-answer", "type": "agentMessage", "text": "Final review."},
                }})
            elif request.get("method") == "turn/start" and self.turn_starts == 1 and frames is not None:
                frames.insert(-1, {"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": "turn-1",
                    "item": {"id": "provisional-answer", "type": "agentMessage", "text": "Provisional review."},
                }})
            return frames

        self.server.respond = hold_final_review
        watch = (codex_mesh.LinuxFileChanges({reply, status}) if sys.platform.startswith("linux")
                 else codex_mesh.KqueueFileChanges({reply, status}))
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                           "--early-ready", str(ready))
                try:
                    wake, peer = socket.socketpair()
                    with wake, peer:
                        budget = mesh_fixture.FixtureLoopBudget("provisional review reply", seconds=20)
                        while not reply.exists() or not reply.read_text().strip():
                            self.assertFalse(launched.done(), "review exited before its final diff existed")
                            event = watch.wait(wake, time.monotonic() + budget.check())
                            self.assertNotEqual(event, codex_mesh.WaitDeadlineExpired.EXPIRED)
                        # Observe ordinary settlement events after Stage 1 has actually written its reply.
                        _ = watch.wait(wake, time.monotonic() + 1)
                    self.assertFalse(launched.done(), "Stage 1 alone completed the review")
                    self.assertEqual(deliveries, [])
                    self.assertIn("Provisional review.", reply.read_text())
                    self.assertFalse(any(event.get("event_type") in {"pass_started", "pass_finished"}
                                         for event in self.events()))
                    ready.touch()
                    self.assertTrue(self.server.held_turn_ready.wait(timeout=15), "Stage 2 never started")
                    self.assertEqual(len([event for event in self.events()
                                          if event.get("event_type") == "pass_started"]), 1)
                    self.assertFalse(any(event.get("event_type") == "pass_finished" for event in self.events()))
                    self.assertEqual(status.read_text().strip(), "running")
                    self.live_turn = ""
                    self.server.release_held_turn.set()
                    result = launched.result(timeout=20)
                finally:
                    ready.touch()
                    self.server.release_held_turn.set()
        finally:
            watch.close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(deliveries), 1, deliveries)
        self.assertEqual(self.turn_starts, 2)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "completed")
        self.assertEqual(status.read_text().strip(), "done")
        self.assertIn("Final review.", status.with_name("review_summary_contract_1.txt").read_text())

    def test_fix_requires_a_cause_and_a_wrong_phase_prompt_never_claims_the_worker(self) -> None:
        refused = self.launch("fix")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.turn_starts, 0)
        _ = self.successful("fix", "--fix-cause", "repair failed acceptance gate")
        self.assertTrue(any(event.get("pass_kind") == "fix" for event in self.events()))
        _ = self.prompt.write_text("Phase 6 — Another phase\nDo the wrong work.\n")
        result = self.launch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)

    def review_after_stage_one(self, *, adopt: bool) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Change delivery ownership only after the first follower has released its turn."""
        ready = self.session_dir.parent / "handoff.ready"
        destination = self.session_dir.parent / "taking-run"
        reply = self.session_dir / "phases/7/review_reply_contract_1.txt"
        script = ASSIGN
        if not adopt:
            # A successful transport acknowledgment alone must not complete a review.
            # Keep the recorder, follower and app-server real, but lose the send.
            script = self.prepare_launcher()[0]
            transport = self.session_dir.parent / "codex_mesh.py"
            _ = transport.write_text(
                "import runpy, sys\n" +
                "if sys.argv[1] == 'send':\n" +
                "    print('queued transport acknowledgment without delivery')\n" +
                "else:\n" +
                f"    runpy.run_path({str(ROOT / 'scripts/agents/codex_mesh.py')!r}, run_name='__main__')\n")
            _ = script.with_suffix(".py").write_text(
                "from scripts.direct import assign\n" +
                f"assign.MESH = {str(transport)!r}\n" +
                "raise SystemExit(assign.main())\n")
        original = self.server.respond

        @mesh_fixture.bounded_fixture
        def provisional_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") == "turn/start" and frames is not None:
                params = cast(dict[str, object], request["params"])
                frames.insert(-1, {"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": "turn-1",
                    "item": {"id": "provisional", "type": "agentMessage", "text": "Stage 1 only."},
                }})
            return frames

        self.server.respond = provisional_reply
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        watch = (codex_mesh.LinuxFileChanges({reply, roster_path}) if sys.platform.startswith("linux")
                 else codex_mesh.KqueueFileChanges({reply, roster_path}))
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                           "--early-ready", str(ready), script=script)
                try:
                    wake, peer = socket.socketpair()
                    with wake, peer:
                        budget = mesh_fixture.FixtureLoopBudget("Stage 1 follower settlement", seconds=20)
                        while True:
                            self.assertFalse(launched.done(), "Stage 1 completed the entire review")
                            if reply.exists() and reply.read_text().strip() and roster_path.exists():
                                with roster_path.open() as handle:
                                    fcntl.flock(handle, fcntl.LOCK_SH)
                                    roster = cast(dict[str, dict[str, object]], json.load(handle))
                                if roster and all(record.get("status") == "done" for record in roster.values()):
                                    break
                            event = watch.wait(wake, time.monotonic() + budget.check())
                            if event == codex_mesh.WaitDeadlineExpired.EXPIRED:
                                ready.touch()
                                stopped = launched.result(timeout=20)
                                self.fail(f"Stage 1 never settled: {stopped.stdout}{stopped.stderr}; " +
                                          f"roster={roster_path.read_text() if roster_path.exists() else 'missing'}")
                    self.assertEqual(self.turn_starts, 1)
                    self.assertFalse(any(event.get("event_type") == "pass_finished" for event in self.events()))
                    if adopt:
                        with roster_path.open("r+") as handle:
                            fcntl.flock(handle, fcntl.LOCK_EX)
                            roster = cast(dict[str, dict[str, object]], json.load(handle))
                            name, record = next(iter(roster.items()))
                            destination.mkdir()
                            _ = (destination / codex_mesh.ROSTER_FILE).write_text(json.dumps({name: record.copy()}))
                            record["adopted_to"] = str(destination)
                            codex_mesh.advance_worker_roster_revision(record)
                            _ = handle.seek(0)
                            _ = handle.write(json.dumps(roster))
                            _ = handle.truncate()
                    ready.touch()
                    result = launched.result(timeout=25)
                finally:
                    ready.touch()
        finally:
            watch.close()
        return result, destination

    def test_an_early_review_adopted_after_stage_one_reports_handoff_without_a_second_prompt(self) -> None:
        result, destination = self.review_after_stage_one(adopt=True)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "handed_off")
        self.assertIn(str(destination), (folder / "review_summary_contract_1.txt").read_text())
        name = next(iter(self.assignment_records()))
        self.assertIn(f"handed off: {name} to {destination}; completion unknown", result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and
                             event.get("status") == "completed" for event in self.events()))
        delivered = read_object(destination / f"{name}.pending.json")
        messages = cast(list[str], delivered["messages"])
        self.assertEqual(len(messages), 1, delivered)
        self.assertIn("Stage 2:", messages[0])

    def test_an_early_review_without_stage_two_delivery_records_error_instead_of_done(self) -> None:
        result = self.review_after_stage_one(adopt=False)[0]
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "error")
        self.assertEqual(self.turn_starts, 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "error")
        self.assertFalse(any(record.get("adopted_to") for record in self.assignment_records().values()))

    def review_adoption_at_boundary(self, boundary: str) -> subprocess.CompletedProcess[str]:
        """Insert ownership changes at exact coordinator boundaries around real followers."""
        script = self.prepare_launcher()[0]
        ready = self.session_dir.parent / "boundary.ready"
        trace = self.session_dir.parent / "follow-boundaries.jsonl"
        delivered = self.session_dir.parent / "boundary-delivered"
        destination = self.session_dir.parent / "boundary-destination"
        # The injected code changes only the ownership boundary. Real followers
        # submit input, write receipts, and settle actual loopback-server turns.
        _ = script.with_suffix(".py").write_text(
            "import fcntl, json\nfrom pathlib import Path\nfrom scripts.direct import assign\n" +
            f"boundary = {boundary!r}\nready = Path({str(ready)!r})\n" +
            f"trace = Path({str(trace)!r})\ndestination = Path({str(destination)!r})\n" +
            f"delivered = Path({str(delivered)!r})\n" +
            "real_follow = assign.Dispatch.follow\nreal_claim = assign.Dispatch.claim_follow\n" +
            "real_mesh_call = assign.Dispatch.mesh_call\n" +
            "real_read = assign.read_roster\nactive_dispatch = None\n" +
            "follow_count = 0\nclaim_count = 0\nadopted = False\n" +
            "def adopt(dispatch):\n" +
            "    global adopted\n" +
            "    if adopted: return\n" +
            "    adopted = True\n" +
            "    destination.mkdir()\n" +
            "    with (dispatch.run / 'mesh_roster.json').open('r+') as handle:\n" +
            "        fcntl.flock(handle, fcntl.LOCK_EX)\n" +
            "        roster = json.load(handle)\n" +
            "        record = roster[dispatch.worker]\n" +
            "        (destination / 'mesh_roster.json').write_text(json.dumps({dispatch.worker: record.copy()}))\n" +
            "        record['adopted_to'] = str(destination)\n" +
            "        assign.codex_mesh.advance_worker_roster_revision(record)\n" +
            "        handle.seek(0); json.dump(roster, handle); handle.truncate()\n" +
            "    if boundary == 'before_inbox':\n" +
            "        pending = dispatch.run / (dispatch.worker + '.pending.json')\n" +
            "        with pending.open('r+') as handle:\n" +
            "            fcntl.flock(handle, fcntl.LOCK_EX)\n" +
            "            (destination / pending.name).write_text(handle.read())\n" +
            "            handle.seek(0); json.dump({'messages': []}, handle); handle.truncate()\n" +
            "    with trace.open('a') as handle: handle.write(json.dumps({'adopted': True}) + '\\n')\n" +
            "def follow(dispatch, command, shown, log):\n" +
            "    global follow_count, active_dispatch\n" +
            "    active_dispatch = dispatch\n" +
            "    follow_count += 1\n" +
            "    if boundary == 'after_claim' and follow_count == 2: adopt(dispatch)\n" +
            "    if boundary == 'first_claim' and follow_count == 1: adopt(dispatch)\n" +
            "    result = real_follow(dispatch, command, shown, log)\n" +
            "    with trace.open('a') as handle:\n" +
            "        handle.write(json.dumps({'follow': follow_count, 'code': result}) + '\\n')\n" +
            "    if result == 0:\n" +
            "        if boundary == 'combined' or boundary == 'completed' and follow_count == 2: adopt(dispatch)\n" +
            "        if follow_count == 1 and boundary != 'combined': ready.touch()\n" +
            "    return result\n" +
            "def claim(dispatch):\n" +
            "    global claim_count\n" +
            "    claim_count += 1\n" +
            "    if boundary == 'before_claim' and claim_count == 2: adopt(dispatch)\n" +
            "    return real_claim(dispatch)\n" +
            "def read(run, name):\n" +
            "    record = real_read(run, name)\n" +
            "    if boundary == 'before_inbox' and follow_count == 1 and active_dispatch is not None:\n" +
            "        adopt(active_dispatch)\n" +
            "    return record\n" +
            "def mesh_call(dispatch, verb, *options, **kwargs):\n" +
            "    result = real_mesh_call(dispatch, verb, *options, **kwargs)\n" +
            "    if verb == 'send' and result.returncode == 0: delivered.touch()\n" +
            "    return result\n" +
            "assign.Dispatch.follow = follow\nassign.Dispatch.claim_follow = claim\n" +
            "assign.read_roster = read\n" +
            "assign.Dispatch.mesh_call = mesh_call\n" +
            "raise SystemExit(assign.main())\n")
        original = self.server.respond

        @mesh_fixture.bounded_fixture
        def final_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") == "turn/start" and frames is not None:
                params = cast(dict[str, object], request["params"])
                stage_two = "Stage 2:" in json.dumps(params)
                if boundary == "combined" and self.turn_starts == 1:
                    self.live_turn = "turn-1"
                answer: dict[str, object] = {"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": f"turn-{self.turn_starts}",
                    "item": {"id": f"answer-{self.turn_starts}", "type": "agentMessage",
                             "text": "Final boundary review." if stage_two else "Provisional boundary review."}}}
                frames.insert(len(frames) if boundary == "combined" and self.turn_starts == 1 else -1, answer)
            return frames

        self.server.respond = final_reply
        if boundary == "combined":
            self.outcomes = ["disconnect"]
            self.server.hold_turn_completion = True
            self.server.answer_requests_during_held_turn = True
            self.server.held_turn_completes_successfully = True
            with ThreadPoolExecutor(max_workers=1) as executor:
                launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                           "--early-ready", str(ready), script=script)
                try:
                    self.assertTrue(self.server.held_turn_ready.wait(timeout=20), "Stage 1 did not start")
                    watch = (codex_mesh.LinuxFileChanges({delivered}) if sys.platform.startswith("linux")
                             else codex_mesh.KqueueFileChanges({delivered}))
                    try:
                        wake, peer = socket.socketpair()
                        with wake, peer:
                            budget = mesh_fixture.FixtureLoopBudget("combined review delivery", seconds=20)
                            ready.touch()
                            while not delivered.exists():
                                event = watch.wait(wake, time.monotonic() + budget.check())
                                self.assertNotEqual(event, codex_mesh.WaitDeadlineExpired.EXPIRED)
                    finally:
                        watch.close()
                    self.live_turn = ""
                    self.server.hold_turn_completion = False
                    self.server.release_held_turn.set()
                    result = launched.result(timeout=30)
                finally:
                    ready.touch()
                    self.server.release_held_turn.set()
        else:
            result = self.launch("review", "--lens", "contract", "--pass", "1",
                                 "--early-ready", str(ready), script=script)
        transitions = [cast(dict[str, object], json.loads(line)) for line in trace.read_text().splitlines()]
        self.assertEqual(len([entry for entry in transitions if entry.get("adopted")]), 1, transitions)
        follows = [entry for entry in transitions if "follow" in entry]
        expected_codes = ([2] if boundary == "first_claim" else
                          [0] if boundary in {"combined", "before_claim", "before_inbox"} else
                          [0, 2] if boundary == "after_claim" else [0, 0])
        self.assertEqual([entry["code"] for entry in follows], expected_codes, transitions)
        return result

    def assert_completed_boundary_review(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "done")
        self.assertIn("Final boundary review.", (folder / "review_summary_contract_1.txt").read_text())
        self.assertNotIn("completion unknown", result.stdout + result.stderr)
        receipts = [cast(dict[str, object], json.loads(line)) for line in
                    (folder / "dispatch_1_submissions.jsonl").read_text().splitlines()]
        self.assertTrue(any("Stage 2:" in json.dumps(receipt.get("messages")) for receipt in receipts), receipts)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "completed")
        self.assertEqual(self.turn_starts, 2)

    def test_adoption_after_the_second_review_follow_keeps_its_completed_result(self) -> None:
        self.assert_completed_boundary_review(self.review_adoption_at_boundary("completed"))

    def test_adoption_after_one_follow_consumes_both_review_stages_keeps_its_completed_result(self) -> None:
        self.assert_completed_boundary_review(self.review_adoption_at_boundary("combined"))

    def assert_handed_off_boundary_review(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "handed_off")
        self.assertIn(str(self.session_dir.parent / "boundary-destination"),
                      (folder / "review_summary_contract_1.txt").read_text())
        self.assertIn("completion unknown", result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and event.get("status") == "completed"
                             for event in self.events()))

    def test_adoption_before_the_second_review_claim_settles_as_handoff(self) -> None:
        self.assert_handed_off_boundary_review(self.review_adoption_at_boundary("before_claim"))

    def test_adoption_after_the_roster_read_before_the_inbox_read_settles_as_handoff(self) -> None:
        self.assert_handed_off_boundary_review(self.review_adoption_at_boundary("before_inbox"))

    def test_adoption_after_the_second_claim_before_the_child_check_settles_as_handoff(self) -> None:
        self.assert_handed_off_boundary_review(self.review_adoption_at_boundary("after_claim"))

    def test_adoption_after_the_first_claim_before_the_child_check_settles_as_handoff(self) -> None:
        result = self.review_adoption_at_boundary("first_claim")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        status = self.session_dir / "phases/7/review_status_contract_1"
        self.assertEqual(status.read_text().strip(), "handed_off")
        self.assertEqual(self.turn_starts, 0)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and event.get("status") == "completed"
                             for event in self.events()))

    def test_a_review_launcher_follow_uses_its_roster_model_instead_of_the_implementation_anchor(self) -> None:
        _ = self.registry.write_text(self.registry.read_text().replace(
            "review=gpt-kept-sol:high", "review=gpt-other-sol:high"))
        for arguments in (["init", "-q"], ["add", "plan.md"],
                          ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "-qm", "Initial fixture"]):
            _ = subprocess.run(["git", *arguments], cwd=self.project, check=True, capture_output=True)

        def review(option: str, name: str, pass_number: str) -> None:
            command = ["bash", str(ROOT / "scripts/direct/review.sh"), option, name, "--phase", "7",
                       str(self.session_dir), str(self.project), str(self.prompt), "review", "Code review",
                       "Reviewing assigned changes", pass_number, "contract"]
            with subprocess.Popen(command, cwd=ROOT, env=os.environ.copy(), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
                output, errors = communicate_launcher(child, timeout=45)
                self.assertEqual(child.returncode, 0, output + errors)

        review("--slot", "review", "1")
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["model"], "gpt-other-sol")
        identity_path = next(path for path in self.session_dir.glob("impl_worker_*.json")
                             if read_object(path).get("name") == name)
        identity = read_object(identity_path)
        self.assertEqual(identity["model"], "gpt-kept-sol")
        self.assertEqual(workers.resolve(self.session_dir, "review"), name)
        reservation = workers.allocation_for_follow(self.session_dir, name)
        workers.release_allocation(self.session_dir, name, reservation)
        self.assertTrue(self.assignment_records()[name].get("unit_ledger"))
        review("--to", name, "2")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-other-sol")
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(identity_path), identity)

    def test_a_failed_worker_records_an_error_pass_and_returns_worker_failure(self) -> None:
        self.outcomes = ["failed"]
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "phases/7/impl_status").read_text().strip(), "error")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(ended), 1, self.events())
        self.assertEqual(ended[0].get("status"), "error")
        self.assertNotIn("launcher_pid", next(iter(self.assignment_records().values())))

    def test_a_large_named_worker_is_compacted_before_its_next_assignment(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record["context_tokens"] = 200_000
        self.write_pool_roster({name: record})
        self.context_sizes = [200_000]
        _ = self.successful("implement", "--to", name)
        methods = [request.get("method") for request in self.server.requests]
        self.assertEqual(methods.count("thread/compact/start"), 1, methods)
        self.assertLess(methods.index("thread/compact/start"),
                        max(index for index, method in enumerate(methods) if method == "turn/start"))
        self.assertEqual(self.turn_starts, 2)

    def test_a_busy_named_worker_refuses_without_creating_another_dispatch_or_pass(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record.update({"status": "running", "launcher_pid": os.getpid(), "turn_id": "busy-turn"})
        self.write_pool_roster({name: record})
        started = [event for event in self.events() if event.get("event_type") == "pass_started"]
        result = self.launch("implement", "--to", name)
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

    def test_handoff_records_the_destination_without_finishing_or_resending_the_assignment(self) -> None:
        original = self.server.respond
        destination = str(self.session_dir.parent / "taking-run")

        @mesh_fixture.bounded_fixture
        def handoff(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") != "turn/start":
                return original(request)
            with (self.session_dir / codex_mesh.ROSTER_FILE).open("r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                roster = cast(dict[str, dict[str, object]], json.load(handle))
                record = next(iter(roster.values()))
                record["adopted_to"] = destination
                codex_mesh.advance_worker_roster_revision(record)
                _ = handle.seek(0)
                _ = handle.write(json.dumps(roster))
                _ = handle.truncate()
            return [{"id": request["id"], "result": {"turn": {"id": "handoff-turn"}}}]

        self.server.respond = handoff
        result = self.launch()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "impl_status").read_text().strip(), "handed_off")
        self.assertIn(destination, (folder / "impl_summary.txt").read_text())
        self.assertIn("completion unknown", result.stdout + result.stderr)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and
                             event.get("status") == "completed" for event in self.events()))
