"""Unit-owned worker resolution against an isolated real mesh protocol server."""

from __future__ import annotations

import fcntl
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TextIO, cast, override
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import workers
from scripts.direct.recorder_fixture import invoke_recorder


SCRIPT = Path(__file__).with_name("workers.py")


class AdoptionWriteInterrupted(BaseException):
    """A simulated process loss after a durable transaction write."""


@contextmanager
def bounded_worker_change() -> Generator[None, None, None]:
    """A lock-order regression fails instead of hanging the fixture forever."""
    def expired(number: int, frame: object) -> None:
        raise AssertionError(f"worker change exceeded its 15s deadline ({number}, {type(frame).__name__})")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        yield
    finally:
        _ = signal.setitimer(signal.ITIMER_REAL, *timer)
        _ = signal.signal(signal.SIGALRM, previous)


@contextmanager
def observe_adoption_writes(source: Path, destination: Path,
                            observer: Callable[[str], None]) -> Generator[None, None, None]:
    """Inject a crash after real durable writes, without a production-only hook."""
    atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]
    write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]
    ledger_path = workers.ledger_for_run(destination).path
    observed: set[str] = set()

    def report(stage: str) -> None:
        if stage not in observed:
            observed.add(stage)
            observer(stage)

    def persisted(path: Path, data: Mapping[str, object]) -> None:
        atomic(path, data)
        if path != ledger_path:
            return
        if data.get("adoptions"):
            report("ledger" if "destination" in observed else "intent")
        elif "ledger" in observed:
            report("complete")

    def roster_written(handle: TextIO, roster: dict[str, object]) -> None:
        write_roster(handle, roster)
        path = Path(cast(str, handle.name))
        if path == source / codex_mesh.ROSTER_FILE:
            report("source")
        elif path == destination / codex_mesh.ROSTER_FILE:
            report("destination")

    with (patch.object(workers, "_atomic", new=persisted),
          patch.object(workers, "_write_roster", new=roster_written)):
        yield


def setUpModule() -> None:
    # Reuse the synthetic sign-in and disposable HOME, never the real ledger.
    mesh_fixture.setUpModule()


def json_object(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    # Opening a taking roster under flock can precede a refused adoption.
    # Empty ledger/state files still fail decoding rather than being hidden.
    if path.name == codex_mesh.ROSTER_FILE and not content.strip():
        return {}
    value = cast(object, json.loads(content))
    if not isinstance(value, dict):
        raise AssertionError(f"expected an object in {path}")
    return cast(dict[str, object], value)


class WorkerResolutionTests(mesh_fixture.MeshCommandFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        working = self.session_dir.parent / "repository"
        working.mkdir()
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
        }))
        self.write_run_context(self.session_dir)

    def write_run_context(self, run: Path) -> None:
        _ = (run / "working_dir").write_text(str(self.session_dir.parent / "repository"))
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "status": "active", "working_dir": str(self.session_dir.parent / "repository"),
            "run_id": run.name,
        }))

    def next_run(self, name: str) -> Path:
        run = self.session_dir.parent / name
        run.mkdir()
        self.write_run_context(run)
        _ = (run / codex_mesh.SERVER_FILE).write_bytes(
            (self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        return run

    def resolve(self, run: Path, role: str = "impl") -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "resolve", "--session-dir", str(run), "--role", role],
            cwd=self.session_dir.parent / "repository", env=os.environ.copy(),
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1, result.stdout)
        return result.stdout.strip()

    def open_worker(self, name: str = "address-with-no-role-suffix", role: str = "impl") -> None:
        self.write_pool_roster({name: {
            "thread_id": mesh_fixture.THREAD_ID, "status": "done", "role": role,
            "model": "gpt-kept-sol", "effort": "xhigh", "family": "codex",
            "codex_home": os.environ["CODEX_HOME"], "port": self.server.port,
            "cwd": str(self.session_dir.parent / "repository"),
        }})

    def complete_assignment(self, name: str) -> None:
        """Consume any dispatch reservation through a real, completed turn."""
        args = self.start_args()
        args.to = name
        args.role = "impl"
        args.message_file = str(self.session_dir / "prompt.txt")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 0, errors.getvalue())

    def configure_new_worker(self) -> None:
        """Resolve through a temporary registry and allocate a distinct real thread."""
        original = self.server.respond

        @mesh_fixture.bounded_fixture
        def open_second(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                return [{"id": request["id"], "result": {"thread": {"id": "second-thread"}}}]
            return original(request)

        self.server.respond = open_second
        registry = self.session_dir.parent / "registry.conf"
        _ = registry.write_text("[codex.agents]\ngpt-kept-sol=high,xhigh,max\n" +
                                "[assignments]\ndirect=codex\n[direct.codex]\n" +
                                "impl=gpt-kept-sol:xhigh\n")
        checkpoint = self.session_dir.parent / "catalog-current"
        checkpoint.touch()
        self.patches.enter_context(patch.dict(os.environ, {
            "AGENTS_CONFIG_FILE": str(registry),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(checkpoint),
        }))

    def test_a_swept_run_keeps_its_worker_thread_and_home_in_the_unit_ledger(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        destination = self.next_run("after-run-sweep")
        original_home = os.environ["CODEX_HOME"]
        shutil.rmtree(self.session_dir)

        self.assertEqual(self.resolve(destination), "impl")
        self.assertFalse(self.session_dir.exists(), "carryover recreated the swept run")
        record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["impl"])
        self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
        with workers.ledger_for_run(destination).locked() as ledger:
            self.assertEqual(ledger.workers["impl"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, original_home))
        prompt = destination / "continue.txt"
        _ = prompt.write_text("Continue the unit's existing worker.")
        with bounded_worker_change(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main([
                "follow", "--session-dir", str(destination), "--to", "impl", "--role", "impl",
                "--message-file", str(prompt), "--summary-file", str(destination / "summary.txt"),
                "--log-file", str(destination / "impl.log"), "--timeout", "5",
            ])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertTrue(self.methods("thread/resume"))
        self.assertTrue(all(cast(dict[str, object], request["params"])["threadId"] == mesh_fixture.THREAD_ID
                            for request in self.methods("thread/resume")))
        self.assertEqual(self.methods("thread/start"), [])

    def test_queued_work_and_owed_continuations_exclude_same_run_allocation(self) -> None:
        self.configure_new_worker()
        for obstacle in ("continuation", "pending"):
            with self.subTest(obstacle=obstacle):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "same-run-fixture", "unit": obstacle})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                record = cast(dict[str, object], roster["impl"])
                pending = self.session_dir / "impl.pending.json"
                pending.unlink(missing_ok=True)
                if obstacle == "continuation":
                    record["continuation_owed"] = True
                else:
                    _ = pending.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                        "messages": ["Previously queued work."], "accepting_messages": True}))
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                original = dict(record)

                self.assertEqual(self.resolve(self.session_dir), "impl2")
                after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(after, original)
                if obstacle == "pending":
                    self.assertEqual(json_object(pending)["messages"], ["Previously queued work."])

    def test_two_simultaneous_resolves_in_one_run_reserve_distinct_workers(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        gate = threading.Barrier(2, timeout=4)

        def reserve() -> str:
            _ = gate.wait()
            return self.resolve(self.session_dir)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reserve) for _ in range(2)]
            names = [future.result(timeout=20) for future in futures]
        self.assertEqual(set(names), {"impl", "impl2"})
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual({cast(dict[str, object], roster[name])["thread_id"] for name in names},
                         {mesh_fixture.THREAD_ID, "second-thread"})
        self.assertTrue(all(cast(dict[str, object], roster[name]).get("allocation_pending") is True
                            for name in names))
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_reservation_from_an_exited_resolver_reuses_its_thread_in_either_run(self) -> None:
        self.configure_new_worker()
        for destination_name in ("same-run", "next-run"):
            with self.subTest(destination=destination_name):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "abandoned-resolver", "unit": destination_name})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                child = subprocess.Popen([
                    sys.executable, "-c",
                    "from pathlib import Path; import sys; from scripts.direct import workers; " +
                    "print(workers.resolve(Path(sys.argv[1]), 'impl'), flush=True)",
                    str(self.session_dir),
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
                try:
                    output, errors = child.communicate(timeout=15)
                    self.assertEqual(child.returncode, 0, errors.decode())
                    self.assertEqual(output.decode().strip(), "impl")
                finally:
                    if child.poll() is None:
                        child.kill()
                        _ = child.communicate(timeout=4)
                destination = (self.session_dir if destination_name == "same-run"
                               else self.next_run("after-resolver-exit"))
                if destination != self.session_dir:
                    _ = (destination / workers.STATE).write_text(json.dumps({**state, "run_id": destination.name}))
                self.assertEqual(self.resolve(destination), "impl")
                record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
                self.assertEqual(self.methods("thread/start"), [])

    def test_an_expired_or_legacy_reservation_does_not_strand_an_idle_worker(self) -> None:
        self.configure_new_worker()
        for reservation in ("expired", "legacy"):
            with self.subTest(reservation=reservation):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "expired-resolver", "unit": reservation})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                self.assertEqual(self.resolve(self.session_dir), "impl")
                roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                record = cast(dict[str, object], roster["impl"])
                if reservation == "expired":
                    record["allocation_expires_at"] = 0
                else:
                    for key in ("allocation_id", "allocation_owner_pid", "allocation_expires_at"):
                        _ = record.pop(key, None)
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                self.assertEqual(self.resolve(self.session_dir), "impl")
                after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(after["thread_id"], mesh_fixture.THREAD_ID)
                self.assertEqual(self.methods("thread/start"), [])

    def test_a_follow_refused_before_claim_releases_its_dispatch_reservation(self) -> None:
        self.configure_new_worker()
        for refusal in ("recovery", "ended"):
            with self.subTest(refusal=refusal):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "refused-resolver", "unit": refusal})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                self.assertEqual(self.resolve(self.session_dir), "impl")
                args = self.start_args()
                args.to = "impl"
                args.message_file = str(self.session_dir / "prompt.txt")
                if refusal == "ended":
                    roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                    cast(dict[str, object], roster["impl"])["status"] = "ended"
                    _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                with bounded_worker_change(), redirect_stderr(io.StringIO()) as errors:
                    if refusal == "recovery":
                        with patch.object(workers, "recover_adoptions",
                                          side_effect=workers.WorkerUnavailable("recovery must be retried")):
                            code = codex_mesh.command_follow(args)
                    else:
                        code = codex_mesh.command_follow(args)
                self.assertEqual(code, 2, errors.getvalue())
                roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                record = cast(dict[str, object], roster["impl"])
                self.assertFalse(record.get("allocation_pending"), "refusal left the dispatch stranded")
                record["status"] = "done"
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                self.assertEqual(self.resolve(self.session_dir), "impl")
                self.assertEqual(self.methods("turn/start"), [])
                self.assertEqual(self.methods("thread/start"), [])

    def test_a_refused_follow_does_not_release_a_newer_dispatch_reservation(self) -> None:
        self.open_worker(name="impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        old = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        recovery = workers.recover_adoptions
        replacement: dict[str, object] = {}

        def replace_dispatch(run: Path) -> None:
            recovery(run)
            roster = json_object(run / codex_mesh.ROSTER_FILE)
            cast(dict[str, object], roster["impl"])["allocation_expires_at"] = 0
            _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
            self.assertEqual(workers.resolve(run, "impl"), "impl")
            replacement.update(cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)["impl"]))
            raise workers.WorkerUnavailable("old follow lost its dispatch")

        args = self.start_args()
        args.to = "impl"
        with (bounded_worker_change(), patch.object(workers, "recover_adoptions", new=replace_dispatch),
              redirect_stderr(io.StringIO()) as errors):
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 2, errors.getvalue())
        after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        self.assertNotEqual(replacement["allocation_id"], old["allocation_id"])
        self.assertEqual(after, replacement, "stale refusal cleared a newer owner's dispatch")
        self.assertTrue(after.get("allocation_pending"))
        self.assertEqual(self.methods("turn/start"), [])

    def test_finished_run_resolution_requires_reactivation_without_allocating_a_thread(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["impl"])["status"] = "ended"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        state = json_object(self.session_dir / workers.STATE)
        state.update({"status": "completed", "finished_at": 1793200000})
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        result = subprocess.run([
            sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir), "--role", "impl",
        ], env=os.environ.copy(), capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("start-phase", result.stderr)
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_locked_roster_is_read_after_its_writer_finishes(self) -> None:
        self.open_worker()
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        original = roster_path.read_bytes()
        script = ("from pathlib import Path; import sys; sys.path.insert(0, sys.argv[2]); "
                  "from scripts.direct import workers; "
                  "print('reader-ready', flush=True); print(workers.resolve(Path(sys.argv[1]), 'impl'), flush=True)")
        with roster_path.open("r+b") as roster:
            fcntl.flock(roster, fcntl.LOCK_EX)
            _ = roster.truncate(0)
            roster.flush()
            child = subprocess.Popen([sys.executable, "-c", script, str(self.session_dir), str(SCRIPT.parents[2])],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            try:
                assert child.stdout is not None
                ready, _, _ = select.select([child.stdout], [], [], 4)
                self.assertTrue(ready, "reader did not finish importing within its fixture deadline")
                self.assertEqual(child.stdout.readline(), b"reader-ready\n")
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = child.communicate(timeout=0.2)
                _ = roster.seek(0)
                _ = roster.write(original)
                roster.flush()
                fcntl.flock(roster, fcntl.LOCK_UN)
                output, errors = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(output.decode().strip(), "address-with-no-role-suffix")
            finally:
                if child.poll() is None:
                    child.kill()
                    _ = child.communicate(timeout=4)
        self.assertEqual(self.methods("thread/start"), [])

    def test_legacy_reviewer_instructions_keep_the_read_only_ledger_policy(self) -> None:
        self.open_worker(name="reviewer-address", role="review")
        instructions = self.session_dir / "review.md"
        _ = instructions.write_text("Review only; report findings.")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["reviewer-address"])["instructions_file"] = str(instructions)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        self.assertEqual(self.resolve(self.session_dir, "review"), "reviewer-address")
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual(ledger.workers["reviewer-address"].access_policy.get("kind"), "ReadOnlyReviewer")

    def test_missing_identities_and_invalid_events_are_refused_when_loading_the_ledger(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        store = workers.ledger_for_run(self.session_dir)
        original = store.path.read_text()
        invalid_fields: tuple[tuple[str, object], ...] = (
            ("ownership", {"kind": "HeldBy"}),
            ("ownership", {"kind": "Adopted", "to_run": ""}),
            ("backend", {"kind": "CodexThread", "codex_home": os.environ["CODEX_HOME"]}),
            ("backend", {"kind": "CodexThread", "thread_id": mesh_fixture.THREAD_ID, "codex_home": ""}),
            ("backend", {"kind": "ClaudeSession", "session_id": ""}),
            ("events", [{"kind": "opened", "run": str(self.session_dir), "at": 1}]),
            ("events", [{"kind": "unknown", "cause": "invalid event", "run": str(self.session_dir), "at": 1}]),
        )
        for field, variant in invalid_fields:
            with self.subTest(field=field, variant=variant):
                data = cast(dict[str, object], json.loads(original))
                entries = cast(dict[str, object], data["workers"])
                cast(dict[str, object], entries[name])[field] = variant
                _ = store.path.write_text(json.dumps(data))
                with self.assertRaises(workers.WorkerUnavailable):
                    with store.locked():
                        self.fail("invalid persisted identity was accepted")

    def test_resolve_reuses_the_stored_role_and_live_model_identity(self) -> None:
        self.open_worker()
        self.assertEqual(self.resolve(self.session_dir), "address-with-no-role-suffix")
        record = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        worker = cast(dict[str, object], record["address-with-no-role-suffix"])
        self.assertEqual(worker["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(worker["model"], "gpt-kept-sol")
        self.assertEqual(self.methods("thread/start"), [])

    def test_the_unit_ledger_retains_backend_access_model_and_a_caused_open_event(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        store = workers.ledger_for_run(self.session_dir)
        self.assertEqual(store.path.parent.name, "adhoc")
        self.assertTrue(store.path.is_relative_to(self.session_dir.parent / "unit-workers"))
        with store.locked() as ledger:
            worker = ledger.workers[name]
            self.assertEqual(worker.role, "impl")
            self.assertEqual(worker.family, "codex")
            self.assertEqual(worker.model, "gpt-kept-sol")
            self.assertEqual(worker.effort, "xhigh")
            self.assertEqual(worker.backend, workers.CodexThread(
                mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
            self.assertEqual(worker.ownership, workers.HeldBy(str(self.session_dir)))
            self.assertEqual(worker.access_policy.get("kind"), "WorkerLaunchPolicy")
            opened = [event for event in worker.events if event.kind == "opened"]
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].cause)
        self.assertFalse((self.session_dir / "workers").exists(),
                         "the unit ledger must not overwrite the launch log")

    def test_adoption_recovers_every_durable_write_before_the_next_command_on_either_run(self) -> None:
        for stage in ("intent", "source", "destination", "ledger", "complete"):
            for command in ("adopt", "resolve", "follow"):
                for side in ("source", "destination"):
                    with self.subTest(stage=stage, command=command, side=side), bounded_worker_change():
                        self.assert_recovered_adoption(stage, command, side)

    def assert_recovered_adoption(self, stage: str, command: str, side: str) -> None:
        identifier = f"{stage}-{command}-{side}"
        source = self.next_run(identifier + "-source")
        destination = self.next_run(identifier + "-destination")
        # Each scenario owns its ledger: an earlier successful recovery must
        # not reserve a worker needed by a later crash scenario.
        context = {"status": "active", "production": "recovery-fixture", "unit": identifier,
                   "working_dir": str(self.session_dir.parent / "repository")}
        for run in (source, destination):
            _ = (run / workers.STATE).write_text(json.dumps(context))
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
            "thread_id": mesh_fixture.THREAD_ID, "role": "impl", "status": "done",
            "model": "gpt-kept-sol", "codex_home": os.environ["CODEX_HOME"],
            "port": self.server.port,
        }}))
        reached: list[str] = []

        def interrupt(write: str) -> None:
            reached.append(write)
            if write == stage:
                raise AdoptionWriteInterrupted(write)

        with observe_adoption_writes(source, destination, interrupt):
            with self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.adopt(source, destination, "worker")
        self.assertIn(stage, reached)
        recovering = source if side == "source" else destination
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if command == "resolve":
                # Recovery runs before resolution can adopt back into the
                # source or allocate another worker. Isolate that later choice
                # so this probe checks the interrupted transaction alone.
                with (patch.object(codex_mesh, "open_unit_worker",
                                   side_effect=workers.WorkerUnavailable("no allocation in recovery probe")),
                      patch.object(workers, "_adopt",
                                   side_effect=workers.WorkerUnavailable("no new adoption in recovery probe"))):
                    try:
                        _ = workers.resolve(recovering, "impl")
                    except workers.WorkerUnavailable:
                        pass
            elif command == "adopt":
                third = self.next_run(identifier + "-third")
                _ = (third / workers.STATE).write_text(json.dumps(context))
                # A fresh adoption command must recover the interrupted
                # worker even if its requested name does not exist.
                with self.assertRaises(workers.WorkerUnavailable):
                    _ = workers.adopt(recovering, third, "missing-worker")
            else:
                try:
                    _ = codex_mesh.main(["follow", "--session-dir", str(recovering),
                                         *self.follow_cli()[1:]])
                except SystemExit:
                    pass
        source_record = cast(dict[str, object], json_object(source / codex_mesh.ROSTER_FILE)["worker"])
        target_record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["worker"])
        self.assertEqual(source_record["adopted_to"], str(destination))
        self.assertEqual(target_record["thread_id"], mesh_fixture.THREAD_ID)
        store = workers.ledger_for_run(destination)
        data = json_object(store.path)
        self.assertEqual(data["adoptions"], {}, "recovery left an incomplete handoff")
        with store.locked() as ledger:
            owner = ledger.workers["worker"].ownership
            self.assertIn(owner, (workers.Adopted(str(destination)), workers.HeldBy(str(destination))))
            events = [event for event in ledger.workers["worker"].events if event.kind == "adopted"]
            self.assertEqual(len(events), 1, "recovery duplicated the handoff event")
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_new_run_adopts_the_unit_thread_and_fences_its_previous_roster(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        self.complete_assignment(name)
        destination = self.next_run("next-run")
        self.assertEqual(self.resolve(destination), name)
        source = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)[name])
        target = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)[name])
        self.assertEqual(source["adopted_to"], str(destination))
        self.assertEqual(target["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(target["model"], "gpt-kept-sol")
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_message_arriving_after_adoption_intent_rolls_back_without_losing_work(self) -> None:
        self.open_worker(name="worker")
        destination = self.next_run("message-race")
        pending = self.session_dir / "worker.pending.json"

        def enqueue_after_intent(stage: str) -> None:
            if stage == "intent":
                _ = pending.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                    "messages": ["Deliver this exactly once."], "accepting_messages": True}))

        with bounded_worker_change(), observe_adoption_writes(self.session_dir, destination, enqueue_after_intent):
            with self.assertRaises(workers.WorkerUnavailable):
                _ = workers.adopt(self.session_dir, destination, "worker")
        self.assertEqual(json_object(pending)["messages"], ["Deliver this exactly once."])
        source = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["worker"])
        self.assertNotIn("adopted_to", source)
        self.assertNotIn("worker", json_object(destination / codex_mesh.ROSTER_FILE))
        data = json_object(workers.ledger_for_run(self.session_dir).path)
        self.assertEqual(data["adoptions"], {})
        self.configure_new_worker()
        self.assertEqual(self.resolve(destination), "impl")
        self.assertEqual(json_object(pending)["messages"], ["Deliver this exactly once."])

    def test_conflicting_recovery_preserves_the_new_owner_and_unblocks_other_work(self) -> None:
        self.open_worker(name="worker")
        destination = self.next_run("recovery-conflict")

        def interrupted(stage: str) -> None:
            if stage == "source":
                raise AdoptionWriteInterrupted(stage)

        with bounded_worker_change(), observe_adoption_writes(self.session_dir, destination, interrupted):
            with self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.adopt(self.session_dir, destination, "worker")
        concurrent = {"worker": {"thread_id": "concurrent-owner-thread", "status": "done",
                                 "role": "review", "revision": 99}}
        destination_roster = destination / codex_mesh.ROSTER_FILE
        _ = destination_roster.write_text(json.dumps(concurrent))
        destination_inbox = destination / "worker.pending.json"
        _ = destination_inbox.write_text(json.dumps({"thread_id": "concurrent-owner-thread",
                                                   "messages": ["Keep the concurrent owner's work."]}))
        before_roster, before_inbox = destination_roster.read_bytes(), destination_inbox.read_bytes()
        result = subprocess.run([
            sys.executable, str(Path(codex_mesh.__file__)), "follow", "--session-dir", str(self.session_dir),
            *self.follow_cli()[1:],
        ], capture_output=True, text=True, timeout=15, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("quarantin", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(destination_roster.read_bytes(), before_roster)
        self.assertEqual(destination_inbox.read_bytes(), before_inbox)
        source = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["worker"])
        self.assertNotIn("adopted_to", source)
        data = json_object(workers.ledger_for_run(self.session_dir).path)
        self.assertEqual(data["adoptions"], {})
        self.assertTrue(data.get("quarantined_adoptions"))

        with bounded_worker_change():
            workers.recover_adoptions(destination)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        roster["other-worker"] = {"thread_id": mesh_fixture.THREAD_ID, "status": "done", "role": "test",
                                  "codex_home": os.environ["CODEX_HOME"], "model": "gpt-kept-sol"}
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        self.assertEqual(self.resolve(self.session_dir, "test"), "other-worker")

    def test_a_truncated_adoption_intent_is_quarantined_once_and_does_not_poison_resolution(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        self.complete_assignment(name)
        store = workers.ledger_for_run(self.session_dir)
        data = json_object(store.path)
        data["adoptions"] = {"truncated-intent": {"id": "truncated-intent"}}
        _ = store.path.write_text(json.dumps(data))
        with bounded_worker_change(), self.assertRaises(workers.WorkerUnavailable) as failure:
            workers.recover_adoptions(self.session_dir)
        self.assertIn("quarantin", str(failure.exception).lower())
        self.assertEqual(self.resolve(self.session_dir), name)
        after = json_object(store.path)
        self.assertEqual(after["adoptions"], {})
        self.assertIn("truncated-intent", cast(dict[str, object], after["quarantined_adoptions"]))
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_damaged_ledger_worker_is_reported_once_and_healthy_follow_and_reopen_continue(self) -> None:
        self.open_worker(name="impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        store = workers.ledger_for_run(self.session_dir)
        data = json_object(store.path)
        entries = cast(dict[str, object], data["workers"])
        healthy = entries["impl"]
        damaged = {**cast(dict[str, object], healthy),
                   "backend": {"kind": "CodexThread", "codex_home": os.environ["CODEX_HOME"]}}
        entries["damaged-worker"] = damaged
        _ = store.path.write_text(json.dumps(data))

        args = self.start_args()
        args.to = "impl"
        args.message_file = str(self.session_dir / "prompt.txt")
        with bounded_worker_change(), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 2, errors.getvalue())
        self.assertIn("damaged-worker", errors.getvalue())
        self.assertIn("quarantin", errors.getvalue().lower())
        after = json_object(store.path)
        self.assertEqual(cast(dict[str, object], after["workers"])["impl"], healthy)
        self.assertNotIn("damaged-worker", cast(dict[str, object], after["workers"]))
        quarantine = cast(dict[str, object], after["quarantined_workers"])
        saved = cast(dict[str, object], quarantine["damaged-worker"])
        self.assertEqual(saved["record"], damaged, "quarantine lost the undecodable persisted entry")
        self.assertTrue(saved["reason"])

        self.complete_assignment("impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["impl"])["status"] = "ended"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        with bounded_worker_change(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main(["reopen", "--session-dir", str(self.session_dir), "--to", "impl"])
        self.assertEqual(code, 0, errors.getvalue())
        with bounded_worker_change():
            workers.recover_adoptions(self.session_dir)
        final = json_object(store.path)
        self.assertEqual(final["quarantined_workers"], quarantine)
        with store.locked() as ledger:
            self.assertEqual(ledger.workers["impl"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
        self.assertEqual(self.methods("thread/start"), [])

    def test_reactivated_run_reopens_its_worker_on_the_same_thread(self) -> None:
        (self.session_dir / workers.STATE).unlink()
        working = self.session_dir.parent / "repository"
        plan = working / "plan.md"
        _ = plan.write_text("## Delegation Context\n\n- **Project:** Worker recovery\n" +
                            "- **Project started:** 2026-10-28T12:00:00+00:00\n\n" +
                            "## Phases\n\n### Phase 4 — Carry workers · status: todo\n")
        environment = {**os.environ, "UNIT_HISTORY_DIR": str(self.session_dir.parent / "history"),
                       "UNIT_ACTIVE_DIR": str(self.session_dir.parent / "active"),
                       "UNIT_NOW_EPOCH": "1793200000", "UNIT_PASS_OWNER": "launcher"}
        _ = invoke_recorder([
            "start-run", "--session-dir", str(self.session_dir), "--working-dir", str(working),
            "--plan-doc", str(plan), "--main-model", "fixture-director", "--main-family", "codex",
            "--main-session-id", "fixture-director",
        ], environment, working, check=True)
        self.open_worker()
        name = self.resolve(self.session_dir)
        self.complete_assignment(name)
        _ = invoke_recorder(["finish-run", "--session-dir", str(self.session_dir),
                             "--status", "completed"], environment, working, check=True)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        record = cast(dict[str, object], roster[name])
        record["status"] = "ended"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        marker = self.session_dir / f"{name}.end"
        marker.touch()
        _ = invoke_recorder([
            "start-phase", "--session-dir", str(self.session_dir), "--phase-id", "4",
            "--phase-title", "Carry workers",
        ], environment, working, check=True)
        self.assertEqual(json_object(self.session_dir / workers.STATE)["status"], "active")
        self.assertEqual(self.resolve(self.session_dir), name)
        after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)[name])
        self.assertEqual(after["thread_id"], mesh_fixture.THREAD_ID)
        self.assertNotEqual(after["status"], "ended")
        self.assertFalse(marker.exists())
        self.complete_assignment(name)
        self.assertTrue(self.methods("thread/resume"))
        self.assertTrue(all(cast(dict[str, object], request["params"])["threadId"] == mesh_fixture.THREAD_ID
                            for request in self.methods("thread/resume")))
        self.assertEqual(self.methods("thread/start"), [])

    def test_racing_run_resolutions_keep_one_existing_thread_and_allocate_a_second_worker(self) -> None:
        self.open_worker(name="impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        destinations = [self.next_run("racing-a"), self.next_run("racing-b")]
        self.configure_new_worker()
        gate = threading.Barrier(2, timeout=4)

        def resolve_after_gate(run: Path) -> str:
            _ = gate.wait()
            return self.resolve(run)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(resolve_after_gate, run) for run in destinations]
            names = [future.result(timeout=20) for future in futures]
        self.assertEqual(set(names), {"impl", "impl2"})
        threads: list[object] = []
        for run, name in zip(destinations, names):
            entry = cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)[name])
            threads.append(entry["thread_id"])
        self.assertEqual(set(threads), {mesh_fixture.THREAD_ID, "second-thread"})
        self.assertEqual(len(self.methods("thread/start")), 1)


if __name__ == "__main__":
    _ = unittest.main()
