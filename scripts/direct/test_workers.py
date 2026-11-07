"""Unit-owned worker resolution against an isolated real mesh protocol server."""

from __future__ import annotations

from scripts.shutdown import remote

from scripts.lib.test_models_registry import RegistryReadProbe, write_registry

import fcntl
import io
import json
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast, override
from unittest import mock
from unittest.mock import patch

from scripts.agents import codex_mesh, worker_lifecycle, test_codex_mesh as mesh_fixture
from scripts.direct import workers
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.stop_injection_test_support import InjectedStop, stop_once
from scripts.message import sessions
from scripts.production import staff, session_staff, worker_admission
from scripts.usage import agents_log
from scripts.production.staff_test_support import fixture_runtime_id, fixture_worker, StaffTestCase, isolated_source_readers, showrunner_relationship, unit_membership, fixture_worker_records


SCRIPT = Path(__file__).with_name("workers.py")
SECOND_THREAD_ID = fixture_runtime_id("second-worker-thread")


@contextmanager
def bounded_worker_change() -> Generator[None, None, None]:
    """A lock-order regression fails instead of hanging the fixture forever."""
    with LauncherHangGuard("worker change").protect():
        yield


def setUpModule() -> None:
    # Reuse the synthetic sign-in and disposable HOME, never the real ledger.
    mesh_fixture.setUpModule()


def json_object(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    value = cast(object, json.loads(content))
    if not isinstance(value, dict):
        raise AssertionError(f"expected an object in {path}")
    fields = cast(dict[str, object], value)
    if path.name == codex_mesh.ROSTER_FILE:
        return fixture_worker_records(fields, staff.local_snapshot())
    return fields


class WorkerScriptTests(unittest.TestCase):
    def test_worker_script_starts_outside_the_repository(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        with tempfile.TemporaryDirectory(prefix="workers-script-") as directory:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--help"], cwd=directory,
                env=environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


class DirectorResolutionTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = showrunner_relationship("demo")
        _ = self.enterContext(isolated_source_readers())
        _ = self.enterContext(cast(AbstractContextManager[object], patch.dict(
            os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")},
        )))

    def test_live_unit_director_keeps_a_running_director_without_a_messaging_socket(self) -> None:
        parent = unit_membership("demo", "builders")
        registry = self.session_record(parent.current_session, "builders")
        run = self.worker_run("demo", "builders", parent.current_session)
        self.write_json(run / workers.STATE, {**json_object(run / workers.STATE), "owner_staff_id": parent.id})
        self.assertEqual(json_object(registry)["messagingSocketPath"], "")

        self.assertEqual(workers.live_unit_director(run), workers.LiveUnitDirector(parent.current_session, os.getpid()))

    def test_live_unit_director_prefers_the_newest_running_name_match(self) -> None:
        parent = unit_membership("demo", "builders")
        older = self.session_record(parent.current_session, "builders")
        current = staff.update_current_session(parent.current_session, staff.native_id(fixture_runtime_id("newer-director")))
        newer = self.session_record(current.current_session, "builders")
        record = json_object(newer)
        record["updatedAt"] = 2
        self.write_json(newer, record)
        run = self.worker_run("demo", "builders", "former-director")
        self.write_json(run / workers.STATE, {**json_object(run / workers.STATE), "owner_staff_id": parent.id})
        self.write_json(self.root / "shutdown/account/history/restart.json", {"session": {
            "session_id": "former-director", "name": "builders", "run_dirs": [str(run)],
            "host": {"kind": "unit", "unit": "builders", "session_name": "builders"},
        }})
        registry = sessions.read_registry()
        entries = {item.record["sessionId"]: item for item in registry.entries}
        # Present readable records in the older-first order that caused the defect.
        with patch.object(sessions, "read_registry", return_value=sessions.RegistryRead(
            (entries[older.stem], entries[newer.stem]), registry.unreadable,
        )):
            self.assertEqual(workers.live_unit_director(run), workers.LiveUnitDirector(current.current_session, os.getpid()))

    def test_live_director_resolves_the_renamed_owner_despite_old_state_names(self) -> None:
        parent = unit_membership("demo", "builders")
        run = self.worker_run("demo", "builders", parent.current_session)
        context = json_object(run / workers.STATE)
        context["owner_staff_id"] = parent.id
        self.write_json(run / workers.STATE, context)
        renamed = staff.rename(parent.id, "renamed-builders")
        _ = self.session_record(renamed.current_session, renamed.name)
        self.assertEqual(workers.live_unit_director(run), workers.LiveUnitDirector(renamed.current_session, os.getpid()))


class WorkerResolutionTests(mesh_fixture.MeshCommandFixture):
    cli_hang_guard: LauncherHangGuard  # pyright: ignore[reportUninitializedInstanceVariable]

    def worker_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """All worker subprocesses spend the current fixture's shared hang budget."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.session_dir.parent / "repository", env=os.environ.copy(),
            capture_output=True, text=True, timeout=self.cli_hang_guard.remaining(), check=False)

    def test_restarted_director_comes_from_its_current_unit_mark(self) -> None:
        from scripts.production import unit_lookup
        from scripts.message import sessions
        from scripts.herdr.herdr import HerdrPlace

        _ = (self.session_dir / "director").write_text("old-director")
        parent = unit_membership("demo", "builders")
        _ = (self.session_dir / workers.STATE).write_text(json.dumps({
            "production": "demo", "unit": "builders", "owner_staff_id": parent.id,
        }))
        member = staff.update_current_session(parent.current_session, staff.native_id(fixture_runtime_id("new-director")))
        current = unit_lookup.LabeledSession("builders", unit_lookup.LiveClaude("Builders", member.current_session, "/socket", 4321),
                                         HerdrPlace("agents", "w1:p2"))
        with patch.object(sessions, "live_sessions", return_value=[]), patch.object(unit_lookup, "labeled_session", return_value=current):
            found = workers.live_unit_director(self.session_dir)
        self.assertEqual(found, workers.LiveUnitDirector(member.current_session, 4321))



    @override
    def setUp(self) -> None:
        super().setUp()
        self.cli_hang_guard = LauncherHangGuard(self.id())
        working = self.session_dir.parent / "repository"
        working.mkdir()
        owner = staff.by_id(staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        assert owner is not None
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), working), expected=owner.worktree)
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
            "USAGE_STATE_DIR": str(self.session_dir.parent / "usage-state"),
            "NOTIFIER_STATE_DIR": str(self.session_dir.parent / "notifier"),
            "MODELS_CONFIG_FILE": str(write_registry(self.session_dir / "registry.conf", {
                "direct.codex": dict.fromkeys(("impl", "test", "fix", "review"), "gpt-kept-sol:xhigh"),
            })),
        }))
        self.write_run_context(self.session_dir)

    def write_run_context(self, run: Path) -> None:
        _ = (run / "working_dir").write_text(str(self.session_dir.parent / "repository"))
        membership = staff.local_snapshot()
        owner = membership.by_current_session(os.environ["CODEX_THREAD_ID"])
        assert owner is not None and isinstance(owner.parent, staff.ReportsTo)
        parent = membership.by_id(owner.parent.id)
        assert parent is not None
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "status": "active", "working_dir": str(self.session_dir.parent / "repository"),
            "run_id": run.name, "production": parent.name, "unit": owner.name,
            "owner_staff_id": owner.id,
        }))

    def next_run(self, name: str) -> Path:
        run = self.session_dir.parent / name
        run.mkdir()
        self.write_run_context(run)
        _ = (run / codex_mesh.SERVER_FILE).write_bytes(
            (self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        return run

    def resolve(self, run: Path, role: str = "impl") -> str:
        result = self.worker_cli(
            "resolve", "--session-dir", str(run), "--role", role,
            "--owner-pid", str(os.getpid()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1, result.stdout)
        return result.stdout.strip()

    def record_worker(self, name: str) -> staff.StaffRow:
        return staff.ensure_worker(staff.native_id(mesh_fixture.THREAD_ID), name,
                                   staff.native_id(os.environ["CODEX_THREAD_ID"]))

    def open_worker(self, name: str = "address-with-no-role-suffix", role: Literal["impl", "test", "fix", "review"] = "impl") -> None:
        _ = self.record_worker(name)
        agents_log.append(mesh_fixture.THREAD_ID, role, None, datetime.now(timezone.utc))
        self.write_pool_roster({name: {
            "staff_id": mesh_fixture.THREAD_ID,
            "thread_id": mesh_fixture.THREAD_ID, "status": "done", "role": role,
            "model": "gpt-kept-sol", "effort": "xhigh", "family": "codex",
            "turn_model": "gpt-kept-sol", "turn_effort": "xhigh",
            "codex_home": os.environ["CODEX_HOME"], "port": self.server.port,
            "cwd": str(self.session_dir.parent / "repository"),
        }})

    def complete_assignment(self, name: str, *, role: Literal["impl", "test", "fix", "review"] = "impl") -> None:
        """Consume any dispatch reservation through a real, completed turn."""
        args = self.start_args()
        args.name = ""
        args.to = name
        args.model = args.effort = ""
        args.role = role
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
                return [{"id": request["id"], "result": {"thread": {"id": SECOND_THREAD_ID}}}]
            frames = original(request)
            params = cast(dict[str, object], request.get("params", {}))
            if frames is not None and params.get("threadId") == SECOND_THREAD_ID:
                return cast(list[dict[str, object]], json.loads(
                    json.dumps(frames).replace(mesh_fixture.THREAD_ID, SECOND_THREAD_ID)))
            return frames

        self.server.respond = open_second
        registry = self.session_dir.parent / "registry.conf"
        _ = write_registry(registry, {'codex.agents': {'gpt-kept-sol': 'high,xhigh,max'}, 'assignments': {'direct': 'codex'}, 'direct.codex': dict.fromkeys(('impl', 'test', 'fix', 'review'), 'gpt-kept-sol:xhigh')})
        checkpoint = self.session_dir.parent / "catalog-current"
        checkpoint.touch()
        self.patches.enter_context(patch.dict(os.environ, {
            "MODELS_CONFIG_FILE": str(registry),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(checkpoint),
        }))

    def lifecycle_worker(self, name: str = "w7", *, role: Literal["impl", "test", "fix", "review"] = "impl") -> workers.UnitWorkerLedger:
        self.open_worker(name, role=role)
        store = workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            ledger.workers[name] = workers.UnitWorker(
                name, role, "codex", "gpt-kept-sol", "xhigh", {"kind": "WorkerLaunchPolicy"},
                workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]),
                workers.HeldBy(str(self.session_dir)),
                [workers.WorkerEvent("opened", "lifecycle fixture", str(self.session_dir), 1_793_188_800.0, {})],
                staff_id=staff.native_id(self.inbox_staff_id(mesh_fixture.THREAD_ID)),
            )
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster[name])["unit_ledger"] = str(store.path)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(session_staff.stored_worker_records(roster)))
        return store

    def test_roster_rerun_after_interrupted_publication_keeps_other_workers_and_receipts(self) -> None:
        self.open_worker("kept")
        path = self.session_dir / codex_mesh.ROSTER_FILE
        write_roster = cast(Callable[[worker_lifecycle.WorkerStateFile, dict[str, object]], None],
                            getattr(workers, "_write_roster"))
        with worker_lifecycle.locked_state(path) as handle:
            roster = handle.read_object()
            original = dict(roster)
            entry = cast(dict[str, object], roster["kept"])
            entry["assignment_id"] = "accepted-before-stop"
            write_roster(handle, roster)
            before = path.read_bytes()
            entry["task"] = "next assignment"
            with stop_once(os, "replace", when="before", argument=1,
                           matches=lambda destination: Path(str(destination)) == path), self.assertRaises(InjectedStop):
                write_roster(handle, roster)
        self.assertEqual(path.read_bytes(), before)
        with worker_lifecycle.locked_state(path) as handle:
            recovered = handle.read_object()
            cast(dict[str, object], recovered["kept"])["task"] = "next assignment"
            write_roster(handle, recovered)
        saved = cast(dict[str, object], json_object(path)["kept"])
        self.assertEqual(saved["task"], "next assignment")
        self.assertEqual(saved["assignment_id"], "accepted-before-stop")
        self.assertEqual(saved["staff_id"], cast(dict[str, object], original["kept"])["staff_id"])

    def test_roster_two_writers_keep_workers_and_mesh_updates(self) -> None:
        self.open_worker("kept")
        path = self.session_dir / codex_mesh.ROSTER_FILE
        barrier = threading.Barrier(2)
        write_roster = cast(Callable[[worker_lifecycle.WorkerStateFile, dict[str, object]], None],
                            getattr(workers, "_write_roster"))
        update_roster = cast(Callable[[str, str, codex_mesh.ThreadRecord], bool],
                             getattr(codex_mesh, "_update_roster"))

        def worker_write() -> None:
            _ = barrier.wait(timeout=10)
            with worker_lifecycle.locked_state(path) as handle:
                roster = handle.read_object()
                cast(dict[str, object], roster["kept"])["assignment_id"] = "accepted"
                write_roster(handle, roster)

        def mesh_write() -> None:
            _ = barrier.wait(timeout=10)
            record: codex_mesh.FollowableRecord = {
                "thread_id": mesh_fixture.THREAD_ID, "status": "done", "task": "mesh update"}
            self.assertTrue(update_roster(str(self.session_dir), "kept", record))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker_write)
            second = pool.submit(mesh_write)
            first.result(timeout=20)
            second.result(timeout=20)
        saved = cast(dict[str, object], json_object(path)["kept"])
        self.assertEqual(saved["task"], "mesh update")
        self.assertEqual(saved["assignment_id"], "accepted")
        self.assertEqual(saved["thread_id"], mesh_fixture.THREAD_ID)

    def test_unit_retirement_removes_a_worker_staff_row_after_its_run_ends(self) -> None:
        store = self.lifecycle_worker()
        member = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert member is not None
        owner = staff.local_snapshot().parent_of(member)
        assert owner is not None
        code, output, errors = self.run_mesh(["end", "--to", "w7"])
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(staff.local_snapshot().by_id(member.id), member)
        with bounded_worker_change(), redirect_stdout(io.StringIO()):
            workers.end_unit(owner)
        self.assertIsNone(staff.local_snapshot().by_id(member.id))
        with store.locked(write=False) as ledger:
            self.assertNotIn("w7", ledger.workers)

    def test_end_unit_rerun_after_end_marker_retries_transport_cleanup(self) -> None:
        store = self.lifecycle_worker()
        member = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert member is not None
        owner = staff.local_snapshot().parent_of(member)
        assert owner is not None
        with stop_once(session_staff, "publish_worker_roster", argument=1,
                       matches=lambda value: isinstance(value, dict)
                       and cast(dict[str, object], cast(dict[str, object], value).get("w7", {})).get("status") == "ended"):
            with self.assertRaises(InjectedStop):
                workers.end_unit(owner)
        self.assertTrue(store.path.exists())
        with patch.object(codex_mesh, "end_worker_locked", wraps=codex_mesh.end_worker_locked) as cleanup:
            workers.end_unit(owner)
        cleanup.assert_called_once()
        with store.locked(write=False) as ledger:
            self.assertNotIn("w7", ledger.workers)


    def test_backend_loss_preserves_unresolved_ledger_entries_on_save(self) -> None:
        store = self.lifecycle_worker()
        before = json_object(store.path)
        with (patch.object(session_staff, "worker_backend", return_value=
                           session_staff.WorkerBackendUnavailable("temporary backend loss")),
              redirect_stderr(io.StringIO()) as errors):
            with store.locked():
                pass
        self.assertEqual(json_object(store.path), before)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        with store.locked(write=False) as ledger:
            self.assertEqual(set(ledger.workers), {"w7"})

    def test_end_unit_preserves_unresolved_entries_after_backend_loss(self) -> None:
        store = self.lifecycle_worker()
        member = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert member is not None
        membership = staff.local_snapshot()
        owner = membership.parent_of(member)
        assert owner is not None
        production = membership.parent_of(owner)
        assert production is not None
        before = json_object(store.path)
        with (patch.object(session_staff, "worker_backend", return_value=
                           session_staff.WorkerBackendUnavailable("temporary backend loss")),
              patch.object(codex_mesh, "end_worker_locked") as end,
              redirect_stderr(io.StringIO()) as errors,
              redirect_stdout(io.StringIO())):
            workers.end_unit(owner)
        self.assertTrue(store.path.exists())
        self.assertEqual(json_object(store.path), before)
        self.assertEqual(staff.by_id(member.id), member)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("anomaly:", errors.getvalue())
        end.assert_not_called()

    def test_end_unit_reads_current_parent_after_acquiring_the_ledger_lock(self) -> None:
        store = self.lifecycle_worker()
        member = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert member is not None
        membership = staff.local_snapshot()
        former = membership.parent_of(member)
        assert former is not None
        production = membership.parent_of(former)
        assert production is not None
        destination = unit_membership("before", "destination")
        inbox = session_staff.inbox_path(member.id)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        _ = inbox.write_text(json.dumps({"messages": ["held during adoption"]}))
        before = inbox.read_bytes()
        acquire = worker_lifecycle.acquire_flock
        adopted = False

        def adopt_before_lock(handle: int | worker_lifecycle.LockFileDescriptor, *,
                              mode: int = fcntl.LOCK_EX,
                              deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
            nonlocal adopted
            descriptor = handle if isinstance(handle, int) else handle.fileno()
            if not adopted and os.path.samestat(os.fstat(descriptor), store.path.with_suffix(".lock").stat()):
                adopted = True
                _ = staff.move(member.id, staff.ReportsTo(destination.id))
            acquire(handle, mode=mode, deadline=deadline)

        with (patch.object(worker_lifecycle, "acquire_flock", side_effect=adopt_before_lock),
              patch.object(codex_mesh, "end_worker_locked") as end,
              redirect_stdout(io.StringIO())):
            workers.end_unit(former)
        self.assertTrue(adopted)
        self.assertEqual(staff.by_id(member.id), replace(member, parent=staff.ReportsTo(destination.id)))
        self.assertEqual(inbox.read_bytes(), before)
        with store.locked(write=False) as ledger:
            self.assertEqual(set(ledger.workers), {member.name})
        end.assert_not_called()

    def test_ending_the_former_unit_keeps_an_adopted_worker_and_its_queue(self) -> None:
        store = self.lifecycle_worker()
        worker = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert worker is not None
        membership = staff.local_snapshot()
        former = membership.parent_of(worker)
        assert former is not None
        holder = membership.parent_of(former)
        assert holder is not None
        destination = unit_membership("before", "destination")
        moved = staff.move(worker.id, staff.ReportsTo(destination.id))
        inbox = session_staff.worker_inbox_path(str(self.session_dir), "w7")
        inbox.parent.mkdir(parents=True, exist_ok=True)
        _ = inbox.write_text(json.dumps({"messages": ["held before adoption"]}))
        before = inbox.read_bytes()
        with (patch.object(workers, "ledger_path", return_value=store.path),
              patch.object(codex_mesh, "end_worker_locked") as end,
              redirect_stdout(io.StringIO())):
            workers.end_unit(former)
        self.assertEqual(staff.by_id(worker.id), moved)
        self.assertEqual(inbox.read_bytes(), before)
        end.assert_not_called()

    def test_adoption_keeps_one_inbox_across_run_folders(self) -> None:
        _ = self.lifecycle_worker()
        worker = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert worker is not None
        source = session_staff.worker_inbox_path(str(self.session_dir), "w7")
        source.parent.mkdir(parents=True, exist_ok=True)
        _ = source.write_text(json.dumps({"messages": ["held before adoption"]}))
        destination = self.next_run("adoption-destination")
        parent = unit_membership("before", "destination")
        _ = staff.move(worker.id, staff.ReportsTo(parent.id))
        _ = (destination / codex_mesh.ROSTER_FILE).write_text(json.dumps({"w7": {"staff_id": worker.id}}))
        target = session_staff.worker_inbox_path(str(destination), "w7")
        self.assertEqual(target, source)
        self.assertEqual(json_object(target)["messages"], ["held before adoption"])

    def test_reviewer_names_use_the_launcher_rule_for_nested_unusual_worktrees(self) -> None:
        working = self.session_dir.parent / ("odd worktree !" + "x" * 50)
        working.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(working)], check=True, capture_output=True)
        nested = working / "nested"
        nested.mkdir()
        state = json_object(self.session_dir / workers.STATE)
        owner = staff.by_id(staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        assert owner is not None
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), nested), expected=owner.worktree)
        del state["working_dir"]
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        expected = subprocess.run(["bash", str(SCRIPT.with_name("worker_name.sh")), str(nested), "review", "review"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.configure_new_worker()
        registry = self.session_dir.parent / "registry.conf"
        with registry.open("a") as output:
            _ = output.write("review=gpt-kept-sol:xhigh\n")
        self.assertEqual(self.resolve(self.session_dir, "review"), expected)
        self.assertTrue(expected.startswith("odd-worktree--"), expected)
        self.assertEqual(len(expected.removesuffix("-review")), 40)

    def test_launcher_worktree_names_the_same_reviewer_without_run_state_on_rerun(self) -> None:
        (self.session_dir / workers.STATE).unlink()
        (self.session_dir / "working_dir").unlink()
        store = workers.ledger_for_run(self.session_dir)
        self.configure_new_worker()
        with (self.session_dir.parent / "registry.conf").open("a") as output:
            _ = output.write("review=gpt-kept-sol:xhigh\n")

        first = self.resolve(self.session_dir, "review")
        self.assertEqual(first, "repository-review")
        self.assertEqual(workers.ledger_for_run(self.session_dir).path, store.path)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        thread = cast(dict[str, object], roster[first])["thread_id"]
        self.complete_assignment(first, role="review")

        self.assertEqual(self.resolve(self.session_dir, "review"), first)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual(set(roster), {first})
        self.assertEqual(cast(dict[str, object], roster[first])["thread_id"], thread)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertFalse((self.session_dir / workers.STATE).exists())
        self.assertFalse((self.session_dir / "working_dir").exists())

    def test_reviewer_without_staff_or_launcher_worktree_is_unavailable(self) -> None:
        (self.session_dir / workers.STATE).unlink()
        (self.session_dir / "working_dir").unlink()
        owner = staff.by_current_session(os.environ["CODEX_THREAD_ID"])
        assert owner is not None
        _ = staff.set_worktree(owner.id, staff.RemovedWorktree(remote.local_machine(), self.session_dir.parent / "repository"),
                              expected=owner.worktree)
        with self.assertRaisesRegex(workers.WorkerUnavailable, "^invalid worker state: missing working_dir$"):
            _ = workers.resolve(self.session_dir, "review")
        self.assertEqual(self.methods("thread/start"), [])

    def test_reviewer_staff_worktree_is_authoritative_over_the_launcher(self) -> None:
        state = json_object(self.session_dir / workers.STATE)
        state["working_dir"] = str(self.session_dir.parent / "stale-launcher")
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        self.configure_new_worker()
        with (self.session_dir.parent / "registry.conf").open("a") as output:
            _ = output.write("review=gpt-kept-sol:xhigh\n")
        self.assertEqual(self.resolve(self.session_dir, "review"),
                         "repository-review")




    def test_retirement_never_trusts_or_signals_a_reused_supervisor_pid(self) -> None:
        _ = self.lifecycle_worker()
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        record = records / "abandoned.json"
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as unrelated:
            _ = record.write_text(json.dumps({"worker": "w7", "status": "running", "pid": unrelated.pid,
                                             "cwd": str(self.session_dir)}))
            self.assertEqual(workers.live_detached_runs(self.session_dir), ())
            self.assertEqual(workers.protected_working_directories(self.session_dir), ())
            self.assertEqual(workers.retained_detached_runs(self.session_dir), ())
            real_kill = os.kill
            def refuse_foreign_signal(pid: int, number: int) -> None:
                if pid == unrelated.pid and number:
                    raise AssertionError("retirement signalled a process that is not the recorded supervisor")
                real_kill(pid, number)
            with patch.object(os, "kill", new=refuse_foreign_signal), redirect_stdout(io.StringIO()) as output:
                membership = staff.local_snapshot()
                worker = membership.by_current_session(mesh_fixture.THREAD_ID)
                assert worker is not None
                owner = membership.parent_of(worker)
                assert owner is not None
                parent = membership.parent_of(owner)
                assert parent is not None
                workers.end_unit(owner)
            self.assertIsNone(unrelated.poll())
            self.assertNotIn("cancelled detached run abandoned", output.getvalue())
            _ = unrelated.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_a_supervisor_started_through_a_linked_run_protects_its_resolved_checkout(self) -> None:
        _ = self.lifecycle_worker()
        linked = self.session_dir.parent / "linked-run"
        linked.symlink_to(self.session_dir, target_is_directory=True)
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        record = records / "linked-command.json"
        checkout = self.session_dir / "phases" / "5" / "gate"
        checkout.mkdir(parents=True)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
            "mesh_runs.py", "supervise", str(linked / "mesh_runs" / record.name)], stdin=subprocess.PIPE)
        self.addCleanup(child.communicate, timeout=TEST_HANG_GUARD_SECS)
        membership = staff.local_snapshot()
        worker = membership.by_current_session(mesh_fixture.THREAD_ID)
        assert worker is not None
        owner = membership.parent_of(worker)
        assert owner is not None
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), checkout), expected=owner.worktree)
        _ = record.write_text(json.dumps({"staff_id": worker.id, "worker": "w7", "status": "running", "pid": child.pid}))
        self.assertEqual([path for path, _ in workers.live_detached_runs(self.session_dir.resolve())], [record])
        self.assertEqual(workers.protected_working_directories(self.session_dir.resolve()), (checkout.resolve(),))

    def test_explicit_reservation_owner_survives_resolve_and_becomes_reclaimable_when_it_exits(self) -> None:
        _ = self.lifecycle_worker(name="impl")
        self.configure_new_worker()
        owner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            result = subprocess.run([sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir),
                "--role", "impl", "--owner-pid", str(owner.pid)], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "impl")
            record = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
            self.assertEqual(record["allocation_owner_pid"], owner.pid)
            self.assertTrue(workers.allocation_is_live(record))
            _ = owner.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertFalse(workers.allocation_is_live(record))
            self.assertEqual(self.resolve(self.session_dir), "impl")
            self.assertEqual(self.methods("thread/start"), [])
        finally:
            if owner.poll() is None:
                owner.kill()
                _ = owner.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_resolve_from_a_short_lived_shell_keeps_the_directors_reservation(self) -> None:
        _ = self.lifecycle_worker(name="impl")
        self.configure_new_worker()
        registry = self.session_dir / "director-sessions"
        registry.mkdir()
        from scripts.message import sessions
        worker = staff.by_current_session(mesh_fixture.THREAD_ID)
        assert worker is not None and isinstance(worker.parent, staff.ReportsTo)
        parent = staff.by_id(worker.parent.id)
        assert parent is not None
        director = parent.current_session
        with socket.socket(socket.AF_UNIX) as endpoint:
            address = self.session_dir / "director.sock"
            endpoint.bind(str(address))
            _ = (registry / f"{os.getpid()}.json").write_text(json.dumps({
                "pid": os.getpid(), "sessionId": director, "name": "demo · builders · director",
                "messagingSocketPath": str(address), "updatedAt": 1, "tmux": "", "formerNames": [],
                "procStart": sessions.process_start(os.getpid()), "entrypoint": "cli"}))
            self.patches.enter_context(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}))
            self.assertEqual(workers.director_pid(self.session_dir), os.getpid())
            self.check_short_lived_resolve()

    def check_short_lived_resolve(self) -> None:
        command = shlex.join([sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir), "--role", "impl"])
        first = subprocess.run(["bash", "-c", command + "; true"], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(first.stdout.strip(), "impl", first.stderr)
        record = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        self.assertEqual(record["allocation_owner_pid"], os.getpid())
        self.assertTrue(workers.allocation_is_live(record))
        self.assertEqual(self.resolve(self.session_dir), "impl2")

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

    def test_removed_run_adoption_keeps_the_latest_accepted_turn_settings(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        registry = Path(os.environ["MODELS_CONFIG_FILE"])
        _ = write_registry(registry, {"direct.codex": {"impl": "gpt-kept-sol:high"}},
                           base_text=registry.read_text())
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        submitted = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual((submitted["model"], submitted["effort"]), ("gpt-kept-sol", "high"))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual((ledger.workers["impl"].model, ledger.workers["impl"].effort),
                             ("gpt-kept-sol", "high"))
        destination = self.next_run("after-later-turn")
        shutil.rmtree(self.session_dir)
        self.assertEqual(self.resolve(destination), "impl")
        record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["impl"])
        self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual((record["turn_model"], record["turn_effort"]), ("gpt-kept-sol", "high"))

    def test_owed_continuations_exclude_same_run_allocation(self) -> None:
        self.configure_new_worker()
        self.open_worker(name="impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        record = cast(dict[str, object], roster["impl"])
        record["continuation_owed"] = True
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(session_staff.stored_worker_records(roster)))
        original = dict(record)

        self.assertEqual(self.resolve(self.session_dir), "impl2")
        after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        self.assertEqual(after, original)

    def test_queued_work_allows_reservation_and_reaches_the_next_dispatch(self) -> None:
        self.configure_new_worker()
        self.open_worker(name="impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["impl"])["status"] = "paused"
        self.write_pool_roster(roster)
        pending = session_staff.worker_inbox_path(str(self.session_dir), "impl")
        pending.parent.mkdir(parents=True, exist_ok=True)
        _ = pending.write_text(json.dumps({"messages": ["Previously queued work."], "accepting_messages": True}))

        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.assertEqual(json_object(pending)["messages"], ["Previously queued work."])
        self.complete_assignment("impl")

        turns = self.methods("turn/start")
        self.assertEqual(len(turns), 1)
        self.assertIn("Previously queued work.", json.dumps(turns[0]))
        self.assertEqual(json_object(pending)["messages"], [])
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_refused_candidate_does_not_block_a_healthy_allocation(self) -> None:
        refused = fixture_worker("first-refused", staff.native_id(os.environ["CODEX_THREAD_ID"]),
            backend=staff.CodexBackend(Path(os.environ["CODEX_HOME"])))
        agents_log.append(refused.current_session, "impl", None, datetime.now(timezone.utc))
        for boundary in ("admit", "publish"):
            with self.subTest(boundary=boundary):
                self.open_worker(name="healthy")
                roster = self.roster()
                roster[refused.name] = {"staff_id": refused.id, "status": "failed",
                    "model": "gpt-kept-sol", "effort": "xhigh", "port": self.server.port}
                self.write_pool_roster(roster)
                staff_file = Path(os.environ["STAFF_FILE"])
                before = staff_file.read_bytes(), self.roster()[refused.name]
                with (patch.object(worker_admission, boundary, wraps=getattr(worker_admission, boundary),
                                   side_effect=[worker_admission.AdmissionRefused("candidate switch refused"), cast(object, mock.DEFAULT)]) as attempted,
                      redirect_stderr(io.StringIO()) as errors):
                    selected = workers.resolve(self.session_dir, "impl")
                attempts = [cast(str, call.args[0]) if boundary == "admit" else cast(worker_admission.WorkerStartAllowed, call.args[0]).name
                            for call in attempted.call_args_list]
                self.assertEqual(errors.getvalue().count("candidate switch refused"), 1)
                self.assertEqual(selected, "healthy")
                self.assertCountEqual(attempts, [refused.name, "healthy"])
                self.assertEqual(staff_file.read_bytes(), before[0])
                after = self.roster()
                self.assertEqual(after[refused.name], before[1])
                self.assertTrue(workers.allocation_is_live(cast(dict[str, object], after["healthy"])))
                self.assertEqual(self.methods("thread/start"), [])
                self.assertEqual(self.methods("turn/start"), [])

    def test_allocation_maps_astra_tasks_and_tolerates_an_unreadable_usage_log(self) -> None:
        for name, role, unreadable in (("astra-reviewer", "review", False),
                                       ("astra-designer", "impl", False), ("impl", "impl", True)):
            with self.subTest(name=name, unreadable=unreadable):
                row = self.record_worker(name)
                self.write_pool_roster({name: {"staff_id": row.id, "status": "done",
                    "model": "gpt-kept-sol", "effort": "xhigh", "port": self.server.port}})
                history = agents_log.state_dir() / "agents.jsonl"
                history.parent.mkdir(parents=True, exist_ok=True)
                history.unlink(missing_ok=True)
                if unreadable:
                    history.mkdir()
                try:
                    with redirect_stderr(io.StringIO()) as errors:
                        selected = workers.resolve(self.session_dir, role)
                    self.assertEqual(selected, name, errors.getvalue())
                    self.assertEqual(session_staff.worker_row(cast(dict[str, object], self.roster()[name])), row)
                    self.assertTrue(workers.allocation_is_live(cast(dict[str, object], self.roster()[name])))
                    self.assertEqual(self.methods("thread/start"), [])
                finally:
                    if unreadable:
                        history.rmdir()
                    _ = staff.retire(row.id)

    def test_allocation_shares_one_staff_snapshot_across_its_readers(self) -> None:
        roles: tuple[Literal["impl", "review"], ...] = ("impl", "review")
        for role in roles:
            with self.subTest(role=role):
                self.open_worker(name=role, role=role)
                try:
                    with patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as reads:
                        selected = workers.resolve(self.session_dir, role)
                    self.assertEqual(selected, role)
                    self.assertEqual(reads.call_count, 1)
                    self.assertTrue(workers.allocation_is_live(cast(dict[str, object], self.roster()[role])))
                    self.assertEqual(self.methods("thread/start"), [])
                    self.assertEqual(self.methods("turn/start"), [])
                finally:
                    _ = staff.retire(staff.native_id(mesh_fixture.THREAD_ID))

    def test_two_simultaneous_resolves_in_one_run_reserve_distinct_workers(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        gate = threading.Barrier(2, timeout=TEST_HANG_GUARD_SECS)

        def reserve() -> str:
            _ = gate.wait()
            return self.resolve(self.session_dir)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reserve) for _ in range(2)]
            names = [future.result(timeout=TEST_HANG_GUARD_SECS) for future in futures]
        self.assertEqual(set(names), {"impl", "impl2"})
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual({cast(dict[str, object], roster[name])["thread_id"] for name in names},
                         {mesh_fixture.THREAD_ID, SECOND_THREAD_ID})
        self.assertTrue(all(cast(dict[str, object], roster[name]).get("allocation_pending") is True
                            for name in names))
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_reservation_from_an_exited_resolver_reuses_its_thread_in_either_run(self) -> None:
        self.configure_new_worker()
        for destination_name in ("same-run", "next-run"):
            with self.subTest(destination=destination_name):
                state = json_object(self.session_dir / workers.STATE)
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                child = subprocess.Popen([
                    str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c",
                    "from pathlib import Path; import sys; from scripts.direct import workers; " +
                    "print(workers.resolve(Path(sys.argv[1]), 'impl'), flush=True)",
                    str(self.session_dir),
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
                try:
                    output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(child.returncode, 0, errors.decode())
                    self.assertEqual(output.decode().strip(), "impl")
                finally:
                    if child.poll() is None:
                        child.kill()
                        _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)
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
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(session_staff.stored_worker_records(roster)))
                self.assertEqual(self.resolve(self.session_dir), "impl")
                after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(after["thread_id"], mesh_fixture.THREAD_ID)
                self.assertEqual(self.methods("thread/start"), [])


    def test_a_locked_roster_is_read_after_its_writer_finishes(self) -> None:
        self.open_worker()
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        original = roster_path.read_bytes()
        script = ("from pathlib import Path; import sys; "
                  "from scripts.direct import workers; "
                  "print('reader-ready', flush=True); print(workers.resolve(Path(sys.argv[1]), 'impl'), flush=True)")
        with mesh_fixture.worker_state_lock_path(roster_path).open("a") as owner, roster_path.open("r+b") as roster:
            fcntl.flock(owner, fcntl.LOCK_EX)
            _ = roster.truncate(0)
            roster.flush()
            child = subprocess.Popen([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", script, str(self.session_dir), str(SCRIPT.parents[2])],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            try:
                assert child.stdout is not None
                ready, _, _ = select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)
                self.assertTrue(ready, "reader did not finish importing within its fixture deadline")
                self.assertEqual(child.stdout.readline(), b"reader-ready\n")
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = child.communicate(timeout=0.2)
                _ = roster.seek(0)
                _ = roster.write(original)
                roster.flush()
                fcntl.flock(owner, fcntl.LOCK_UN)
                output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(output.decode().strip(), "address-with-no-role-suffix")
            finally:
                if child.poll() is None:
                    child.kill()
                    _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.methods("thread/start"), [])

    def test_legacy_reviewer_instructions_keep_the_read_only_ledger_policy(self) -> None:
        self.open_worker(name="reviewer-address", role="review")
        instructions = self.session_dir / "review.md"
        _ = instructions.write_text("Review only; report findings.")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["reviewer-address"])["instructions_file"] = str(instructions)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(session_staff.stored_worker_records(roster)))
        self.assertEqual(self.resolve(self.session_dir, "review"), "reviewer-address")
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual(ledger.workers["reviewer-address"].access_policy.get("kind"), "ReadOnlyReviewer")

    def test_worker_resolution_publishes_the_returned_thread_before_exposing_its_address(self) -> None:
        self.configure_new_worker()
        respond = self.server.respond
        before_creation: list[tuple[staff.StaffRow, ...]] = []

        def create(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                before_creation.append(tuple(staff.find(kind="worker")))
            return respond(request)

        self.server.respond = create
        name = self.resolve(self.session_dir, "impl")
        row = staff.by_id(staff.StaffId(SECOND_THREAD_ID))
        assert row is not None
        self.assertEqual((row.name, row.parent), (name, staff.ReportsTo(staff.StaffId(os.environ["CODEX_THREAD_ID"]))))
        self.assertEqual(before_creation, [()])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(cast(dict[str, object], self.roster()[name])["thread_id"], row.id)
        stored = cast(dict[str, dict[str, object]], json.loads(
            (self.session_dir / codex_mesh.ROSTER_FILE).read_text()))[name]
        self.assertEqual(stored["staff_id"], row.id)
        self.assertTrue({"thread_id", "session_id", "codex_home", "manager", "role"}.isdisjoint(stored))
        store = workers.ledger_for_run(self.session_dir)
        ledger = cast(dict[str, dict[str, dict[str, object]]], json.loads(store.path.read_text()))
        settled = ledger["workers"][row.id]
        self.assertEqual(settled["staff_id"], row.id)
        self.assertTrue({"name", "role", "family", "backend"}.isdisjoint(settled))

    def test_concurrent_reviews_open_distinct_read_only_unit_workers(self) -> None:
        self.configure_new_worker()
        registry = self.session_dir.parent / "registry.conf"
        with registry.open("a", encoding="utf-8") as handle:
            _ = handle.write("review=gpt-kept-sol:xhigh\n")
        original = self.server.respond
        opened: list[str] = []

        @mesh_fixture.bounded_fixture
        def open_reviewer(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                identity = fixture_runtime_id(f"review-thread-{len(opened) + 1}")
                opened.append(identity)
                return [{"id": request["id"], "result": {"thread": {"id": identity}}}]
            return original(request)

        self.server.respond = open_reviewer
        first = self.resolve(self.session_dir, "review")
        second = self.resolve(self.session_dir, "review")
        self.assertEqual((first, second), ("repository-review", "repository-review2"))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            for name, thread in zip((first, second), opened, strict=True):
                worker = ledger.workers[name]
                self.assertEqual(worker.role, "review")
                self.assertEqual(worker.backend, workers.CodexThread(thread, os.environ["CODEX_HOME"]))
                self.assertEqual(worker.access_policy.get("kind"), "ReadOnlyReviewer")
                self.assertEqual(worker.ownership, workers.HeldBy(str(self.session_dir)))
        self.assertEqual(len(self.methods("thread/start")), 2)
        for request in self.methods("thread/start"):
            self.assertEqual(cast(dict[str, object], request["params"]).get("sandbox"), "read-only")
        self.assertEqual(self.methods("turn/start"), [])

    def test_resolve_reuses_the_stored_role_and_live_model_identity(self) -> None:
        self.open_worker()
        probe = RegistryReadProbe(self.session_dir, Path(os.environ["MODELS_CONFIG_FILE"]))
        with probe.active({**os.environ, "MODELS_LAUNCH_CONFIGURATION": "", "MODELS_REGISTRY_EDIT_BASE": ""}):
            self.assertEqual(self.resolve(self.session_dir), "address-with-no-role-suffix")
        self.assertEqual(probe.total(), 0, probe.reads())
        record = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        worker = cast(dict[str, object], record["address-with-no-role-suffix"])
        self.assertEqual(worker["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(worker["turn_model"], "gpt-kept-sol")
        self.assertEqual(self.methods("thread/start"), [])

    def resolve_identity(self, model: str, effort: str) -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir),
             "--role", "impl", "--owner-pid", str(os.getpid()),
             "--model", model, "--effort", effort],
            cwd=self.session_dir.parent / "repository", capture_output=True, text=True,
            env=os.environ.copy(), timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1, result.stdout)
        return result.stdout.strip()

    def test_turn_display_does_not_filter_an_idle_worker_for_a_requested_model(self) -> None:
        self.open_worker("keep-first")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        original = cast(dict[str, object], roster["keep-first"])
        roster["select-this"] = {**original, "thread_id": "other-thread",
                                 "turn_model": "gpt-other-sol", "turn_effort": "high"}
        self.write_pool_roster(roster)
        self.assertEqual(self.resolve_identity("gpt-other-sol", "high"), "keep-first")
        after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["keep-first"])
        self.assertEqual(after["thread_id"], original["thread_id"])
        self.assertEqual((after["turn_model"], after["turn_effort"]), ("gpt-kept-sol", "xhigh"))
        self.assertEqual(self.methods("thread/start"), [])

    def test_an_effort_override_reuses_an_idle_worker_without_rewriting_its_history(self) -> None:
        self.configure_new_worker()
        store = self.lifecycle_worker("impl")
        chosen = self.resolve_identity("gpt-kept-sol", "high")
        self.assertEqual(chosen, "impl")
        with store.locked() as ledger:
            self.assertEqual(ledger.workers[chosen].effort, "xhigh")
            self.assertEqual(ledger.workers[chosen].model, "gpt-kept-sol")
        self.assertEqual(self.methods("thread/start"), [])


    def test_explicit_working_directory_reaches_a_new_worker(self) -> None:
        self.configure_new_worker()
        directory = self.session_dir.parent / "explicit-directory"
        directory.mkdir()
        result = self.worker_cli(
            "resolve", "--session-dir", str(self.session_dir), "--role", "impl",
            "--owner-pid", str(os.getpid()), "--working-dir", str(directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = self.methods("thread/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["cwd"], str(directory))

    def test_the_unit_ledger_retains_backend_access_model_and_a_caused_open_event(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        store = workers.ledger_for_run(self.session_dir)
        self.assertEqual(store.path.stem, os.environ["CODEX_THREAD_ID"])
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
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(session_staff.stored_worker_records(roster)))
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
        gate = threading.Barrier(2, timeout=TEST_HANG_GUARD_SECS)

        def resolve_after_gate(run: Path) -> str:
            _ = gate.wait()
            return self.resolve(run)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(resolve_after_gate, run) for run in destinations]
            names = [future.result(timeout=TEST_HANG_GUARD_SECS) for future in futures]
        self.assertEqual(set(names), {"impl", "impl2"})
        threads: list[object] = []
        for run, name in zip(destinations, names):
            entry = cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)[name])
            threads.append(entry["thread_id"])
        self.assertEqual(set(threads), {mesh_fixture.THREAD_ID, SECOND_THREAD_ID})
        self.assertEqual(len(self.methods("thread/start")), 1)


if __name__ == "__main__":
    _ = unittest.main()
