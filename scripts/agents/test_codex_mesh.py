"""Retained fixtures for the held launcher tests. Round 2 removes them."""

from __future__ import annotations

from scripts.shutdown import remote

from scripts.production import worker_admission
from scripts.production.staff_test_support import fixture_worker, fixture_runtime_id, fixture_roster, isolated_staff_file, isolated_source_readers, source_environment, unit_membership, fixture_worker_records
from scripts.production import staff, session_staff


from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS


from scripts.lib import models_registry, names
from scripts.lib.test_names import fixture_names


import argparse
import base64
import contextlib
import dataclasses
import fcntl
import io
import inspect
import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import traceback
import weakref
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
from collections.abc import Generator, Sequence
from typing import Callable, Literal, TypedDict, cast, final, override
from unittest.mock import MagicMock, patch

from scripts.agents.codex_mesh_test_support import FixtureLoopBudget, LifecycleTestClock, StubAppServer, THREAD_ID, test_object as _as_test_dict, notice as _notice, reply as _reply, assert_fixture_environment, select_fixture_workers, setUpModule as setUpModule, set_environment
from scripts.agents import codex_mesh, worker_lifecycle
from scripts.direct import assign_gate, plan_history
from scripts.lib import atomic_file
from scripts.lib.test_models_registry import write_registry
from scripts.message import sessions, unit_labels, roster as message_roster
from scripts.herdr import herdr
from scripts.usage import agents_log
from scripts.accounts import accounts
from scripts.astra import design_roles
from scripts.whoami import agent_accounts


MESH_PROCESS_ROOT = Path("/proc")
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
# Intentional contention probes bypass fixture observers and bookkeeping.
KERNEL_FLOCK = fcntl.flock


def write_server_record(session_dir: Path, port: int, workers_home: Path, dev_shell_worktree: str = "") -> None:
    """Seed a complete server record for the fixture's checkout."""
    _ = (session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
        "port": port, "pid": os.getpid(), "codex_home": str(workers_home),
        "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
        "dev_shell_worktree": dev_shell_worktree,
    }), encoding="utf-8")


def sign_in_moves(folder: Path) -> tuple[codex_mesh.WorkersRunSwitch, ...]:
    """Exercise sign-in migration and retain its results for safety checks."""
    current = current_server(folder)
    source = codex_mesh.server_record_home(current)
    results: list[codex_mesh.WorkersRunSwitch] = []
    finish = codex_mesh._finish_run_moves  # pyright: ignore[reportPrivateUsage]

    def completed(runs: list[codex_mesh.WorkersRunMove | codex_mesh.EndedWorkersRun]) -> tuple[codex_mesh.WorkersRunSwitch, ...]:
        moves = finish(runs)
        results.extend(moves)
        return moves

    with (patch.object(codex_mesh, "codex_home", return_value=source),
          patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
          patch.object(codex_mesh, "_holds_old_sign_in", return_value=True),
          patch.object(codex_mesh, "_finish_run_moves", side_effect=completed)):
        code = codex_mesh.command_signin_changed(argparse.Namespace(root=str(folder.parent)))
    if code:
        raise AssertionError(f"sign-in migration failed: {code}")
    return tuple(results)


def private_worker_staff(case: unittest.TestCase) -> None:
    """Give each worker fixture a private parent and backend record."""
    root = Path(case.enterContext(tempfile.TemporaryDirectory(prefix="mesh-worker-staff-")))
    _ = case.enterContext(isolated_staff_file(root / "staff.json", {}))
    parent = unit_membership("fixture-production", "fixture")
    session_staff.inbox_path(parent.id).parent.mkdir(parents=True, exist_ok=True)
    _ = cast(object, case.enterContext(patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id})))


def published_worker_records(entries: dict[str, object]) -> dict[str, object]:
    return publish_fixture_roster(entries, Path(os.environ["CODEX_HOME"]))


def publish_fixture_roster(entries: dict[str, object], home: Path) -> dict[str, object]:
    """Store fixed references for workers supplied by the private backend."""
    parent = staff.local_snapshot().by_current_session(os.environ["CODEX_THREAD_ID"])
    assert parent is not None
    session_staff.inbox_path(parent.id).parent.mkdir(parents=True, exist_ok=True)
    roles = {name: _as_test_dict(raw).get("role") for name, raw in entries.items()}
    stored = fixture_roster(entries, parent.id, home)
    assignments = codex_mesh.task_assignments()
    for name, raw in stored.items():
        role = roles[name]
        if role in codex_mesh.WORKER_ROLES:
            identity = session_staff.worker_session(_as_test_dict(raw))
            current = codex_mesh.task_role(identity, assignments)
            if not isinstance(current, codex_mesh.AssignedWorkerRole) or current.role != role:
                codex_mesh._record_assignment(identity, cast(codex_mesh.WorkerRole, role), None)  # pyright: ignore[reportPrivateUsage]
    membership = staff.local_snapshot()
    for name, raw in entries.items():
        if isinstance(raw, dict) and isinstance(stored[name], dict) and "staff_id" in cast(dict[str, object], stored[name]):
            details = cast(dict[str, object], raw)
            projected = cast(dict[str, object], fixture_worker_records({name: stored[name]}, membership)[name])
            details.clear()
            details.update(projected)
    return stored


def worker_state_lock_path(path: Path) -> Path:
    """Locate the persistent lock inode used by real state contention fixtures."""
    return path.with_name(path.name + ".lock")


def current_server(run: Path) -> dict[str, object]:
    """Read the active server and its retained history."""
    path = run / codex_mesh.SERVER_FILE
    return _as_test_dict(cast(object, json.loads(path.read_text()))) if path.exists() else {}


def server_history(run: Path) -> dict[str, object]:
    """Read previous servers from the one current server record."""
    return {"servers": current_server(run).get("previous_servers", [])}


def write_server_history(run: Path, records: Sequence[object]) -> None:
    current = current_server(run)
    current["previous_servers"] = list(records)
    atomic_file.write_text_atomic(run / codex_mesh.SERVER_FILE, json.dumps(current))


def assert_shutdown_deadline(deadline: float) -> None:
    """A synthetic cleanup still receives the bounded production operation budget."""
    now = time.monotonic()
    if not now < deadline <= now + codex_mesh.THREAD_RELEASE_TIMEOUT_SECS:
        raise AssertionError(f"cleanup deadline {deadline} is outside the remaining shutdown budget at {now}")


def assert_reaped_once(reaped: MagicMock, pid: int) -> None:
    call = reaped.call_args
    assert call is not None, "server was never reaped"
    deadline = cast(float, call.kwargs["deadline"])
    reaped.assert_called_once_with(pid, deadline=deadline)
    assert_shutdown_deadline(deadline)


@dataclasses.dataclass(frozen=True)
class HeldRosterLock:
    handle: weakref.ReferenceType[io.IOBase]
    thread: int
    site: str
    kind: str
    rank: int


@final
class RosterRelockGuard:
    """Observe actual flock calls, including locks acquired by test fixtures."""

    def __init__(self) -> None:
        self.flock: Callable[[int | io.IOBase, int], None] = fcntl.flock
        self.held: dict[tuple[int, int, int, int], HeldRosterLock] = {}
        self.mutex = threading.Lock()

    def __call__(self, descriptor: int | io.IOBase, operation: int) -> None:
        fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
        info = os.fstat(fd)
        # Independent threads legitimately read the same roster concurrently.
        # Detect recursive acquisition in each thread, rather than treating
        # ordinary cross-thread lock contention as recursion.
        key = (os.getpid(), info.st_dev, info.st_ino, threading.get_ident())
        # Mesh passes integer fds; retain only a weak reference to the enclosing
        # file object so implicit unlock-on-close needs no patched close method.
        handle = descriptor if isinstance(descriptor, io.IOBase) else self.open_handle(fd)
        with self.mutex:
            self.held = {inode: held for inode, held in self.held.items()
                         if (owner := held.handle()) is not None and not owner.closed}
            held = self.held.get(key)
            filename = Path(str(getattr(handle, "name", ""))).name if handle is not None else ""
            kind, rank = ("membership", 0) if filename == "membership.lock" else (
                ("staff", 1) if filename == "staff.json.lock" else (
                ("lifecycle", 2) if filename.endswith(".lifecycle.lock") else (
                ("server", 3) if filename == codex_mesh.LOCK_FILE else (
                ("inbox", 4) if filename.endswith((".pending.json", ".pending.json.lock")) else (
                ("roster", 5) if filename in (codex_mesh.ROSTER_FILE, codex_mesh.ROSTER_FILE + ".lock") else ("", -1))))))
            is_roster = bool(kind) or held is not None
            if kind and not operation & fcntl.LOCK_UN:
                for inode, owner in self.held.items():
                    # Adoption checks an inbox while holding its roster using a
                    # try-lock only; contention refuses admission instead of waiting.
                    inbox_try = kind == "inbox" and owner.kind == "roster" and operation & fcntl.LOCK_NB
                    if (inode[0] == os.getpid() and owner.thread == threading.get_ident()
                            and owner.rank > rank and not inbox_try):
                        site = "".join(traceback.format_list(traceback.extract_stack()[:-1][-6:]))
                        reason = ("lifecycle flock after inbox or roster flock" if kind == "lifecycle"
                                  and owner.kind in ("inbox", "roster") else f"{kind} flock after {owner.kind} flock")
                        raise AssertionError(reason + ":\nfirst lock:\n" + owner.site + f"{kind} lock:\n" + site)
            if is_roster:
                if operation & fcntl.LOCK_UN:
                    self.flock(descriptor, operation)
                    _ = self.held.pop(key, None)
                    return
                site = "".join(traceback.format_list(traceback.extract_stack()[:-1][-6:]))
                if held is not None:
                    raise AssertionError(
                        f"nested roster flock: pid={key[0]} inode={key[1:3]}\n" +
                        f"first lock (thread={held.thread}):\n{held.site}" +
                        f"second lock (thread={threading.get_ident()}):\n{site}")
                assert handle is not None, "roster flock has no observable file lifetime"
                self.held[key] = HeldRosterLock(weakref.ref(handle), threading.get_ident(), site, kind, rank)
        # Never hold the bookkeeping mutex across a potentially blocking kernel
        # lock: another thread may need it to release an unrelated roster.
        try:
            self.flock(descriptor, operation)
        except BaseException:
            if is_roster:
                with self.mutex:
                    _ = self.held.pop(key, None)
            raise

    @staticmethod
    def open_handle(fd: int) -> io.IOBase | None:
        frame = inspect.currentframe()
        try:
            while frame is not None:
                for value in tuple(cast(dict[str, object], frame.f_locals).values()):
                    if isinstance(value, io.IOBase) and not value.closed:
                        try:
                            if value.fileno() == fd:
                                return value
                        except (OSError, ValueError):
                            continue
                frame = frame.f_back
        finally:
            del frame
        return None


LegacyWorkerMoveRequest = TypedDict("LegacyWorkerMoveRequest", {
    "kind": Literal["requested"], "at": str, "tool": Literal["codex"],
    "subject": Literal["seats"], "move_id": str, "from": str, "to": str,
    "why": str, "window": Literal["unknown"],
})


class LegacyWorkerMoveConfirmation(TypedDict):
    kind: Literal["confirmed"]
    at: str
    tool: Literal["codex"]
    subject: Literal["seats"]
    move_id: str
    home: str
    seat: str
    run: str


class FixtureModelDefaults:
    """Reuse real resolution only for the module's unchanged baseline fixture.

    Custom registries and injected subprocess failures always run the product
    resolver. No second registry parser or model-selection rule lives here.
    """

    def __init__(self, resolve: Callable[[argparse.Namespace, models_registry.LaunchModelConfiguration], tuple[str, str]]) -> None:
        self.resolve: Callable[[argparse.Namespace, models_registry.LaunchModelConfiguration], tuple[str, str]] = resolve
        self.environment: dict[str, str] = self.resolver_environment()
        self.files: dict[Path, tuple[int, bytes]] = {
            Path(os.environ[key]): (Path(os.environ[key]).stat().st_mtime_ns, Path(os.environ[key]).read_bytes())
            for key in ("MODELS_CONFIG_FILE", "CODEX_CONFIG_FILE", "CODEX_PACER_TIER_FILE",
                        "CODEX_CATALOG_SYNC_STATE_FILE")
        }
        self.run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
        self.defaults: dict[tuple[str, ...], tuple[str, str]] = {}

    @staticmethod
    def resolver_environment() -> dict[str, str]:
        return {key: value for key, value in os.environ.items()
                if (key.startswith(("AGENTS_", "MODELS_", "CODEX_")) or key in ("BASH_ENV", "ENV", "PATH", "SHELLOPTS", "BASHOPTS"))
                and key not in ("CODEX_HOME",)}

    def unchanged(self) -> bool:
        if self.resolver_environment() != self.environment or subprocess.run is not self.run:
            return False
        return all(path.is_file() and (path.stat().st_mtime_ns, path.read_bytes()) == value
                   for path, value in self.files.items())

    def __call__(self, args: argparse.Namespace, configuration: models_registry.LaunchModelConfiguration) -> tuple[str, str]:
        if getattr(args, "model", "") not in ("", "gpt-test") or not self.unchanged():
            return self.resolve(args, configuration)
        key = tuple(str(getattr(args, name, "")) for name in ("role", "task", "model", "effort"))
        if key not in self.defaults:
            value = self.resolve(args, configuration)
            if self.unchanged():
                self.defaults[key] = value
            return value
        return self.defaults[key]


USAGE_LIMIT = "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"
CAPACITY = "Selected model is at capacity. Please try a different model."
STALE_SIGN_IN = ("Your access token could not be refreshed because you have since logged out or signed in to "
                 "another account. Please sign in again.")

WRITE_POLICY: dict[str, object] = {"type": "dangerFullAccess"}
READ_ONLY_POLICY: dict[str, object] = {"type": "readOnly", "networkAccess": False}


def running_holder(session_id: str) -> sessions.SessionRecord:
    """A process-validated holder with no messaging socket is still running."""
    return sessions.SessionRecord(pid=os.getpid(), sessionId=session_id, name=session_id,
        messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])


def reported_stopped_context(report: MagicMock, index: int = -1) -> codex_mesh.RefusalCommandContext:
    return cast(codex_mesh.RefusalCommandContext, cast(object,
        report.call_args_list[index].kwargs.get("stopped", codex_mesh.REFUSAL_WITHOUT_COMMAND_EXIT)))


class MeshCommandFixture(unittest.TestCase):
    """Exercise the launcher and addressable verbs against a local app-server."""

    guard_frame_reads: bool = True

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    server: StubAppServer  # pyright: ignore[reportUninitializedInstanceVariable]
    outcomes: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    turn_starts: int  # pyright: ignore[reportUninitializedInstanceVariable]
    turn_inputs: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    context_sizes: list[int]  # pyright: ignore[reportUninitializedInstanceVariable]
    compact_after_tokens: int  # pyright: ignore[reportUninitializedInstanceVariable]
    compact_uses_item: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    compact_never_finishes: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    live_turn: str  # pyright: ignore[reportUninitializedInstanceVariable]
    announce_live_completion: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    queued: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    read_failure: str  # pyright: ignore[reportUninitializedInstanceVariable]
    thread_statuses: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]
    error_threads: set[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    interrupt_error: str  # pyright: ignore[reportUninitializedInstanceVariable]
    interrupt_error_ends_turn: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    hide_turn_id_from_steer: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    hide_live_turn_id: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    stale_read_turn: str  # pyright: ignore[reportUninitializedInstanceVariable]
    stale_reads: int  # pyright: ignore[reportUninitializedInstanceVariable]
    # Every turn this server started, with the status it ends in; a read that
    # asks for turns lists each one that is not live.
    started_turns: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]
    # More turn records such a read lists.
    silent_ended: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    # A started turn the server reads idle but does not list yet.
    unlisted_turn: str  # pyright: ignore[reportUninitializedInstanceVariable]
    unlisted_reads: int  # pyright: ignore[reportUninitializedInstanceVariable]
    next_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    fork_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    history_turns: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    failing_method: str  # pyright: ignore[reportUninitializedInstanceVariable]
    auto_turn_starts: int  # pyright: ignore[reportUninitializedInstanceVariable]
    waits: list[float]  # pyright: ignore[reportUninitializedInstanceVariable]
    elapsed: float  # pyright: ignore[reportUninitializedInstanceVariable]
    start_args_timeout: float  # pyright: ignore[reportUninitializedInstanceVariable]
    loop_budget: FixtureLoopBudget  # pyright: ignore[reportUninitializedInstanceVariable]
    lifecycle_clock: LifecycleTestClock  # pyright: ignore[reportUninitializedInstanceVariable]
    patches: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(assign_gate, "called_by_assign", return_value=True))
        _ = self.enterContext(patch.object(names, "load_names", return_value=fixture_names()))
        assert_fixture_environment()
        _ = self.enterContext(LauncherHangGuard(self.id()).protect())
        self.loop_budget = FixtureLoopBudget(self.id())
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="codex-mesh-test-")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.assert_no_mesh_process_leaks)
        self.home = Path(self.temporary.name).resolve() / "home"
        self.home.mkdir()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        clock = self.lifecycle_clock = LifecycleTestClock()
        _ = self.patches.enter_context(patch.object(codex_mesh, "time", clock))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_turn_model_effort",
            clock.registry_resolution(codex_mesh._turn_model_effort)))  # pyright: ignore[reportPrivateUsage]
        _ = self.patches.enter_context(patch.object(codex_mesh, "_turn_service_tier",
            clock.registry_resolution(codex_mesh._turn_service_tier)))  # pyright: ignore[reportPrivateUsage]
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "AGENT_NOTES_DIR": str(self.home),
        })))
        # resolve(): macOS /tmp is a link to /private/tmp, and the mesh records resolved paths.
        self.session_dir = Path(self.temporary.name).resolve() / "run"
        self.session_dir.mkdir()
        atomic_file.write_text_atomic(self.session_dir / "worker_transport", "mesh\n")
        from scripts.production import fake_unit_host
        host = fake_unit_host.environment(self.session_dir)
        fake_unit_host.write(host, [])
        self.patches.enter_context(patch.dict(os.environ, host))
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership
        _ = self.patches.enter_context(isolated_staff_file(self.home / "worker-staff.json", {}))
        environment = source_environment(self.home / "sources")
        environment["ROSTER_RUN_ROOT"] = str(self.session_dir.parent)
        for directory in environment.values():
            Path(directory).mkdir(parents=True, exist_ok=True)
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, environment)))
        _ = self.patches.enter_context(isolated_source_readers())
        parent = unit_membership("natedev", "models")
        _ = staff.set_worktree(parent.id, staff.PresentWorktree(remote.local_machine(), self.session_dir), expected=parent.worktree)
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id})))
        _ = (self.session_dir / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": parent.id}))
        _ = (self.session_dir / "prompt.txt").write_text("Original worker task", encoding="utf-8")
        self.outcomes = []
        self.turn_starts = 0
        self.turn_inputs = []
        self.context_sizes = []
        self.compact_after_tokens = 20000
        self.compact_uses_item = False
        self.compact_never_finishes = False
        self.live_turn = ""
        self.announce_live_completion = False
        self.queued = []
        self.read_failure = ""
        self.thread_statuses = {}
        self.error_threads = set()
        self.interrupt_error = ""
        self.interrupt_error_ends_turn = False
        self.hide_turn_id_from_steer = False
        self.hide_live_turn_id = False
        self.stale_read_turn = ""
        self.stale_reads = 0
        self.started_turns = {}
        self.silent_ended = []
        self.unlisted_turn = ""
        self.unlisted_reads = 0
        self.next_thread_id = THREAD_ID
        self.fork_thread_id = fixture_runtime_id("review-fork")
        self.history_turns = []
        self.failing_method = ""
        self.auto_turn_starts = 0
        self.waits = []
        self.elapsed = 0.0
        self.start_args_timeout = TEST_HANG_GUARD_SECS
        select_fixture_workers(self.home, notes=self.home)
        self.server = StubAppServer(self.respond)
        list_server_processes = codex_mesh._list_server_processes  # pyright: ignore[reportPrivateUsage]

        def list_fixture_servers() -> str:
            return list_server_processes() + f"\n{os.getpid()} 0 codex app-server --listen ws://127.0.0.1:{self.server.port}"

        _ = self.patches.enter_context(patch.object(codex_mesh, "_list_server_processes", list_fixture_servers))
        signal_process = codex_mesh._signal_process  # pyright: ignore[reportPrivateUsage]
        reap_process = codex_mesh._reap  # pyright: ignore[reportPrivateUsage]

        def reap_fixture(pid: int, *, deadline: float = codex_mesh.NO_LOCK_DEADLINE) -> bool:
            # The stub uses a thread in this process; real child processes use the real reaper.
            return True if pid == os.getpid() else reap_process(pid, deadline=deadline)

        self.addCleanup(setattr, codex_mesh, "_reap", reap_process)
        setattr(codex_mesh, "_reap", reap_fixture)

        def protect_runner(pid: object, signum: int, *, group: bool = False) -> None:
            if pid == os.getpid() and signum != 0:
                raise AssertionError("synthetic server cleanup must not signal the test runner")
            signal_process(pid, signum, group=group)

        _ = self.patches.enter_context(patch.object(codex_mesh, "_signal_process", protect_runner))
        _ = self.start_stub_server(str(self.session_dir), agent_accounts.workers_home())
        _ = self.patches.enter_context(patch.object(codex_mesh, "_start_server", side_effect=self.start_stub_server))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_start_watcher",
            return_value=codex_mesh.ExistingServerWatcher()))
        read_frame = codex_mesh.Client.next_frame

        def bounded_read(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            remaining = self.loop_budget.check()
            return read_frame(client, min(deadline, codex_mesh.time.time() + remaining))  # pyright: ignore[reportPrivateLocalImportUsage]

        if self.guard_frame_reads:
            _ = self.patches.enter_context(patch.object(codex_mesh.Client, "next_frame", bounded_read))
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "DESIGN_ROLES_STATE": str(self.session_dir / "design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.session_dir / "design-ownership"),
            "UNIT_RUN_DIR": "", "PLAN_DELEGATE_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "",
            "CODEX_MESH_QUEUE_GRACE_SECS": "0",
        })))
        _ = self.patches.callback(set_environment("USAGE_STATE_DIR", str(self.session_dir / "usage-state")))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_capacity_clock", self.clock, create=True))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_capacity_sleep", self.wait, create=True))

    @override
    def tearDown(self) -> None:
        self.server.close()

    def start_stub_server(self, session_dir: str, workers_home: Path,
                          *_extra: object) -> int:
        shell = next((value for value in _extra if isinstance(value, codex_mesh.WorktreeDevShell)),
                     codex_mesh.InheritedServerEnvironment())
        write_server_record(Path(session_dir), self.server.port, workers_home,
                            str(shell.worktree) if isinstance(shell, codex_mesh.WorktreeDevShell) else "")
        return self.server.port

    def assert_no_mesh_process_leaks(self, proc: Path = MESH_PROCESS_ROOT) -> None:
        root = str(Path(self.temporary.name).resolve())
        leaks: list[str] = []
        candidates: list[list[str]] = []
        if sys.platform == "darwin":
            observed = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True,
                                      text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
            candidates = [line.strip().split(None, 1) for line in observed.stdout.splitlines()]
        else:
            for process in proc.iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    command = os.fsdecode((process / "cmdline").read_bytes()).replace("\0", " ")
                except OSError:
                    continue
                candidates.append([process.name, command])
        for fields in candidates:
            if len(fields) != 2:
                continue
            pid, command = fields
            if not (("codex" in command and "app-server" in command) or "idle-stop" in command):
                continue
            evidence = command
            if sys.platform == "darwin":
                observed = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                evidence += "\n" + observed.stdout
                try:
                    environment = herdr.darwin_process_environment(int(pid))
                except herdr.HerdrFailure as error:
                    try:
                        os.kill(int(pid), 0)
                    except ProcessLookupError:
                        continue
                    except OSError:
                        pass
                    self.fail(f"Cannot check fixture mesh process {pid}: {error}")
                codex_home = environment.get("CODEX_HOME", "")
                if codex_home and Path(codex_home).expanduser().resolve().is_relative_to(Path(root)):
                    evidence += "\n" + root
            else:
                try:
                    evidence += "\n" + os.fsdecode((proc / pid / "environ").read_bytes())
                    evidence += "\n" + str((proc / pid / "cwd").resolve())
                except (OSError, ValueError):
                    pass
            if root in evidence:
                leaks.append(f"{pid} {command}")
        self.assertEqual(leaks, [], "The fixture leaves live mesh processes: " + "; ".join(leaks))

    def clock(self) -> float:
        _ = self.loop_budget.check()
        return self.elapsed

    def wait(self, seconds: float) -> None:
        _ = self.loop_budget.check()
        record = self.worker_record()
        self.assertEqual(record["thread_id"], THREAD_ID)
        self.assertEqual(record["status"], "waiting_capacity")
        self.waits.append(seconds)
        self.elapsed += seconds

    def respond(self, request: dict[str, object]) -> list[dict[str, object]] | None:
        method = request.get("method")
        params = cast("dict[str, object]", request.get("params", {}))
        if method == self.failing_method:
            return [{"id": request["id"], "error": {"message": "scripted request failure"}}]
        if method == "thread/start":
            return [_reply(request, {"thread": {"id": self.next_thread_id}})]
        if method == "thread/resume":
            return [_reply(request, {"thread": {"id": params["threadId"]}})]
        if method == "thread/fork":
            return [_reply(request, {"thread": {"id": self.fork_thread_id}})]
        if method == "thread/turns/list":
            turns = [*self.history_turns, *self.listed_turns()]
            if params.get("sortDirection") != "asc":
                turns.reverse()
            limit = params.get("limit")
            if isinstance(limit, int):
                turns = turns[:limit]
            return [_reply(request, {"data": turns, "nextCursor": None})]
        if method == "turn/start":
            self.error_threads.discard(cast("str", params.get("threadId", THREAD_ID)))
            self.turn_starts += 1
            inputs = cast("list[dict[str, object]]", params.get("input", []))
            submitted_text = cast("str", inputs[0].get("text", "")) if inputs else ""
            self.turn_inputs.append(submitted_text)
            self.queued.clear()
            turn_id = f"turn-{self.turn_starts}"
            turn_submission_frames = [
                _reply(request, {"turn": {"id": turn_id}}),
                _notice("item/completed", {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "item": {
                        "id": f"user-message-{self.turn_starts}",
                        "type": "userMessage",
                        "content": [{"type": "inputText", "text": submitted_text}],
                    },
                }),
            ]
            outcome = self.outcomes.pop(0) if self.outcomes else "completed"
            self.started_turns[turn_id] = "failed" if outcome in {
                "capacity", "capacity_final_queued", "capacity_queued", "capacity_structured",
                "capacity_completed", "overflow", "progress_overflow", "failed",
                "failed_with_queued", "capacity_with_peer",
            } else "completed"
            if outcome in {
                "capacity", "capacity_final_queued", "capacity_queued",
                "capacity_structured", "capacity_completed", "failed",
                "completed_then_capacity",
            }:
                self.error_threads.add(THREAD_ID)
            if outcome == "disconnect":
                return turn_submission_frames
            if outcome == "earlier_completed":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": "earlier-turn"}
                    }),
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                ]
            if outcome == "earlier_only":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": "earlier-turn"}
                    }),
                ]
            if outcome == "completed_without_id":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {"threadId": THREAD_ID, "turn": {}}),
                ]
            if outcome == "completed_then_instant_peer":
                # A peer turn opens the instant the followed one ends. While it runs,
                # a thread read still lists the followed turn as the one in progress.
                self.live_turn = "peer-turn"
                self.stale_read_turn = turn_id
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            if outcome == "answered_without_completion":
                return [
                    *turn_submission_frames,
                    _notice("item/completed", {
                        "threadId": THREAD_ID, "item": {"type": "agentMessage", "text": "late answer"}
                    }),
                ]
            if outcome == "idle_before_listed":
                self.unlisted_turn = turn_id
                return turn_submission_frames
            if outcome == "listed_before_notices":
                # The server reads the thread idle and lists the new turn as
                # ended before it sends any notice for it.
                return turn_submission_frames[:1]
            if outcome == "completed_then_peer":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            if outcome == "capacity":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {"message": CAPACITY}},
                )
            elif outcome == "capacity_final_queued":
                self.queued.append({"id": "final-peer", "input": [
                    {"type": "text", "text": "Queued before refusal"}
                ], "clientUserMessageId": "final-peer"})
                finished = _notice(
                    "turn/failed", {"threadId": THREAD_ID, "error": {"message": CAPACITY}}
                )
            elif outcome == "capacity_queued":
                self.queued.append({"id": "waiting-peer", "input": [
                    {"type": "text", "text": "Server queued message"}
                ], "clientUserMessageId": "waiting-peer"})
                finished = _notice(
                    "turn/failed", {"threadId": THREAD_ID, "error": {"message": CAPACITY}}
                )
            elif outcome == "capacity_structured":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {
                        "message": "provider unavailable", "codexErrorInfo": "serverOverloaded"
                    }},
                )
            elif outcome == "capacity_completed":
                finished = _notice(
                    "turn/completed",
                    {"threadId": THREAD_ID, "turn": {
                        "id": turn_id, "error": {"message": CAPACITY}
                    }},
                )
            elif outcome == "overflow":
                finished = _notice(
                    "turn/completed",
                    {"threadId": THREAD_ID, "turn": {
                        "id": turn_id,
                        "error": {
                            "message": "context window exceeded",
                            "codexErrorInfo": "contextWindowExceeded",
                        },
                    }},
                )
            elif outcome == "progress_overflow":
                frames = turn_submission_frames.copy()
                frames.append(_notice("item/completed", {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "item": {"type": "commandExecution", "command": "true"},
                }))
                frames.append(_notice(
                    "turn/completed",
                    {"threadId": THREAD_ID, "turn": {
                        "id": turn_id,
                        "error": {
                            "message": "context window exceeded",
                            "codexErrorInfo": "contextWindowExceeded",
                        },
                    }},
                ))
                return frames
            elif outcome == "failed":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {"message": "ordinary failure"}},
                )
            elif outcome == "failed_with_queued":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    *turn_submission_frames,
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": "ordinary failure"}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            elif outcome == "capacity_with_peer":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    *turn_submission_frames,
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": CAPACITY}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            elif outcome == "completed_then_capacity":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "next-turn"}
                    }),
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": CAPACITY}
                    }),
                ]
            else:
                finished = _notice(
                    "turn/completed", {"threadId": THREAD_ID, "turn": {"id": turn_id}}
                )
            frames = turn_submission_frames.copy()
            if self.context_sizes:
                frames.append(_notice("thread/tokenUsage/updated", {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "last": {"inputTokens": self.context_sizes.pop(0)},
                        "total": {"inputTokens": 0},
                    },
                }))
            frames.append(finished)
            return frames
        if method == "thread/compact/start":
            compact_turn = "compact-turn"
            if self.compact_never_finishes:
                self.live_turn = compact_turn
                return [
                    _reply(request, {}),
                    _notice("turn/started", {"threadId": params["threadId"], "turn": {"id": compact_turn}}),
                ]
            completion = _notice(
                "item/completed",
                {
                    "threadId": params["threadId"],
                    "turnId": compact_turn,
                    "item": {"id": "compact-item", "type": "contextCompaction"},
                },
            ) if self.compact_uses_item else _notice(
                "thread/compacted",
                {"threadId": params["threadId"], "turnId": compact_turn},
            )
            return [
                _reply(request, {}),
                _notice("turn/started", {"threadId": params["threadId"], "turn": {"id": compact_turn}}),
                _notice("thread/tokenUsage/updated", {
                    "threadId": params["threadId"],
                    "turnId": compact_turn,
                    "tokenUsage": {
                        "last": {"inputTokens": self.compact_after_tokens},
                        "total": {"inputTokens": 0},
                    },
                }),
                completion,
                _notice("turn/completed", {
                    "threadId": params["threadId"],
                    "turn": {"id": compact_turn},
                }),
            ]
        if method == "thread/queue/list":
            return [_reply(request, {"data": self.queued.copy(), "nextCursor": None})]
        if method == "thread/loaded/list":
            return [_reply(request, {"data": [THREAD_ID] if self.live_turn else [], "nextCursor": None})]
        if method == "thread/unsubscribe":
            return [_reply(request, {}), _notice("thread/closed", {"threadId": params["threadId"]})]
        if method == "thread/queue/add":
            item = {
                "id": f"queued-{len(self.queued) + 1}", "input": params["input"],
                "clientUserMessageId": params["clientUserMessageId"],
            }
            if self.live_turn:
                self.queued.append(item)
            else:
                self.error_threads.discard(THREAD_ID)
                self.auto_turn_starts += 1
                self.live_turn = "queued-turn"
                self.announce_live_completion = True
                return [
                    _reply(request, {"queuedSubmission": item}),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                    }),
                ]
            return [_reply(request, {"queuedSubmission": item})]
        if method == "thread/queue/delete":
            queued_id = params.get("queuedSubmissionId")
            prior = len(self.queued)
            self.queued = [item for item in self.queued if item["id"] != queued_id]
            return [_reply(request, {"deleted": len(self.queued) != prior})]
        if method == "thread/read":
            if self.read_failure == "error":
                return [{"id": request["id"], "error": {"message": "read refused"}}]
            if self.read_failure == "timeout":
                return []
            thread_id = cast("str", params.get("threadId", THREAD_ID))
            if self.stale_read_turn:
                self.stale_reads += 1
                frames = [_reply(request, {"thread": {
                    "id": thread_id, "status": {"type": "active"},
                    "turns": [{"id": self.stale_read_turn, "status": "inProgress"}],
                }})]
                # The peer turn ends after the second read; no frame arrives unasked here.
                if self.stale_reads == 2:
                    frames.append(_notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                    }))
                    self.stale_read_turn = self.live_turn = ""
                return frames
            turns = self.listed_turns() if params.get("includeTurns") else []
            unlisted_ends: list[dict[str, object]] = []
            if self.unlisted_turn:
                self.unlisted_reads += 1
                # The turn completes after the second read, with its notice.
                if self.unlisted_reads == 2:
                    unlisted_ends = [
                        _notice("item/completed", {
                            "threadId": THREAD_ID, "item": {"type": "agentMessage", "text": "listed late"}
                        }),
                        _notice("turn/completed", {"threadId": THREAD_ID, "turn": {"id": self.unlisted_turn}}),
                    ]
                    self.unlisted_turn = ""
            # Codex 0.163 queues this notice ahead of every full-history read.
            frames = [_notice("deprecationNotice", {
                "summary": "Full-history hydration is deprecated"})] if params.get("includeTurns") else []
            frames += [_reply(request, {"thread": {
                "id": thread_id,
                "status": {"type": self.thread_statuses.get(thread_id) or (
                    "active" if self.live_turn else
                    "systemError" if thread_id in self.error_threads else "idle"
                )},
                "turns": turns,
            }}), *unlisted_ends]
            if self.live_turn and self.announce_live_completion:
                frames.append(_notice("turn/completed", {
                    "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                }))
                self.live_turn = ""
            return frames
        if method == "turn/steer":
            if self.live_turn:
                if self.hide_turn_id_from_steer:
                    return [{"id": request["id"], "error": {"message": "turn id unavailable"}}]
                frames: list[dict[str, object]] = [
                    {"id": request["id"], "error": {"message": f"expected turn, but found `{self.live_turn}`"}}
                ]
                if self.announce_live_completion:
                    frames.append(_notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                    }))
                    self.live_turn = ""
                return frames
            return [{"id": request["id"], "error": {"message": "no live turn"}}]
        if method == "turn/interrupt":
            if self.interrupt_error:
                if self.interrupt_error_ends_turn:
                    self.live_turn = ""
                return [{"id": request["id"], "error": {"message": self.interrupt_error}}]
            if params.get("turnId") == self.live_turn:
                self.live_turn = ""
                return [_reply(request, {})]
            return [{"id": request["id"], "error": {"message": "wrong turn id"}}]
        return [_reply(request, {})]

    def start_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            session_dir=str(self.session_dir), name="worker", cwd=str(self.session_dir),
            prompt_file=str(self.session_dir / "prompt.txt"),
            summary_file=str(self.session_dir / "summary.txt"),
            reply_file="", log_file=str(self.session_dir / "worker.log"),
            model="gpt-test", effort="high", service_tier="", sandbox="danger-full-access",
            timeout=self.start_args_timeout, resident=False, role="impl", lens=None,
        )

    def controlled_stream_deadlines(self, *, submission_method: str = "turn/start",
                                    activity_method: str = "") -> None:
        """Advance scripted quiet waits, preserving every real notice before their boundary.

        Opt in only for deterministic scripts. Event-wakeup tests keep the real
        synchronization path. Activity deadlines are monotonic; frame deadlines
        are wall time. Both advance the same LifecycleTestClock.
        """
        self.lifecycle_clock.freeze()
        _ = self.patches.enter_context(patch.object(worker_lifecycle, "time", self.lifecycle_clock))
        self.server.coalesce_response_frames = True
        respond = self.server.respond
        read = codex_mesh.Client.next_frame
        raw_read = codex_mesh.Client._read_until  # pyright: ignore[reportPrivateUsage]
        raw_pending = cast(property, vars(codex_mesh.Client)["has_pending_frames"])
        wire_frames: dict[codex_mesh.Client, deque[codex_mesh.RpcMessage]] = {}
        quiet_ready = False
        activity_ready = False
        first_submission = self.turn_starts + 1
        waits = FixtureLoopBudget("controlled stream waits", max_calls=32)

        def response(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            method = request.get("method")
            if frames is None:
                return None
            return [*frames, _notice("fixture/quiet", {
                "activity": method == activity_method and self.turn_starts < first_submission,
                "stream": method == submission_method and (
                    submission_method != "turn/start" or self.turn_starts >= first_submission),
            })]

        def advance(seconds: float) -> None:
            waits.check_calls()
            self.lifecycle_clock.advance(max(0.0, seconds))

        def response_frames(client: codex_mesh.Client, deadline: codex_mesh.TransportDeadline,
                            wake: codex_mesh.FollowEvents | codex_mesh.SocketOnlyWait = codex_mesh.SOCKET_ONLY_WAIT
                            ) -> codex_mesh.RpcMessage | None:
            # Consume the fixture boundary below both RPC and stream reads.
            # Keep read-ahead frames here, not in Client._pending: call appends
            # each notice returned by _read_until, so preloading its queue reorders it.
            nonlocal quiet_ready, activity_ready
            frames = wire_frames.setdefault(client, deque())
            if frames:
                return frames.popleft()
            boundary = FixtureLoopBudget("scripted response boundary", max_calls=128)
            while True:
                boundary.check_calls()
                value = raw_read(client, deadline, wake)
                if value is None:
                    if frames:
                        raise AssertionError("scripted response ended before its quiet boundary")
                    return None
                if value.get("method") == "fixture/quiet":
                    params = _as_test_dict(value.get("params"))
                    quiet_ready = quiet_ready or bool(params.get("stream"))
                    activity_ready = activity_ready or bool(params.get("activity"))
                    break
                frames.append(value)
            if not frames:
                return None
            return frames.popleft()

        def pending(client: codex_mesh.Client) -> bool:
            return bool(wire_frames.get(client)) or cast(bool, raw_pending.__get__(client))

        def frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            if quiet_ready and not client.has_pending_frames:
                advance(min(codex_mesh.UNSEEN_END_CHECK_SECS,
                            max(0.0, deadline - self.lifecycle_clock.time())))
                return None
            return read(client, deadline)

        def activity(_client: codex_mesh.Client, _wake: codex_mesh.FollowEvents, deadline: float) -> None:
            self.assertTrue(activity_ready, "activity wait preceded its scripted response boundary")
            advance(deadline - self.lifecycle_clock.monotonic())

        self.server.respond = response
        _ = self.patches.enter_context(patch.object(codex_mesh.Client, "_read_until", response_frames))
        _ = self.patches.enter_context(patch.object(codex_mesh.Client, "has_pending_frames", property(pending)))
        _ = self.patches.enter_context(patch.object(codex_mesh.Client, "next_frame", frame))
        if activity_method:
            _ = self.patches.enter_context(patch.object(codex_mesh.Client, "wait_for_activity", activity))

    def roster(self) -> dict[str, object]:
        with worker_lifecycle.locked_state(self.session_dir / codex_mesh.ROSTER_FILE, mode=fcntl.LOCK_SH) as state:
            raw = state.read_object()
        membership = staff.local_snapshot()
        result: dict[str, object] = {}
        for name, value in raw.items():
            if isinstance(value, dict):
                try:
                    value = cast(dict[str, object], fixture_worker_records({name: value}, membership)[name])
                except staff.StaffUnreadable:
                    pass
            result[name] = value
        return result

    def retain_server(self, port: int) -> None:
        current = current_server(self.session_dir)
        previous = cast(list[object], current.get("previous_servers", []))
        current["previous_servers"] = [*previous, {"port": port, "pid": os.getpid(),
            "codex_home": str(agent_accounts.workers_home())}]
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps(current))

    def worker_record(self) -> dict[str, object]:
        return cast("dict[str, object]", self.roster()["worker"])

    def stored_worker_record(self, name: str = "worker") -> dict[str, object]:
        return _as_test_dict(session_staff.read_worker_roster(
            self.session_dir / codex_mesh.ROSTER_FILE).get(name))

    def inbox_staff_id(self, identity: str) -> str:
        row = staff.local_snapshot().by_current_session(identity)
        return row.id if row is not None else identity

    def usage_path(self) -> Path:
        return self.session_dir / "usage-state/agents.jsonl"

    def usage_records(self) -> list[dict[str, object]]:
        if not self.usage_path().exists():
            return []
        return [cast("dict[str, object]", json.loads(line))
                for line in self.usage_path().read_text(encoding="utf-8").splitlines()]

    def listed_turns(self) -> list[dict[str, object]]:
        """Turns the server lists, oldest first: ended ones, then any in progress."""
        turns: list[dict[str, object]] = [
            *({"id": turn, "status": status} for turn, status in self.started_turns.items()
              if turn not in (self.live_turn, self.unlisted_turn, self.stale_read_turn)),
            *self.silent_ended,
        ]
        if self.stale_read_turn:
            turns.append({"id": self.stale_read_turn, "status": "inProgress"})
        elif self.live_turn and not self.hide_live_turn_id:
            turns.append({"id": self.live_turn, "status": "inProgress"})
        return turns

    def methods(self, method: str) -> list[dict[str, object]]:
        return [request for request in self.server.requests if request.get("method") == method]

    def run_worker(self, args: argparse.Namespace, port: int, *, started_here: bool = False) -> codex_mesh.RunOutcome:
        allowed = worker_admission.admit(cast(str, args.name), self.session_dir)
        return codex_mesh._run_worker(args, port, staff_admission=allowed, started_here=started_here)  # pyright: ignore[reportPrivateUsage]

    def run_start(self) -> tuple[int, str]:
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = codex_mesh.command_start(self.start_args())
        return result, errors.getvalue()

    def run_follow(
        self, message: str = "Repair the open finding", role: str = "impl", lens: str = "",
        *, claim_pid: int = 0,
    ) -> tuple[int, str]:
        message_file = self.session_dir / "follow-up.txt"
        _ = message_file.write_text(message, encoding="utf-8")
        args = self.start_args()
        args.to = "worker"
        args.role = role
        args.lens = lens or None
        args.claim_pid = claim_pid
        args.message_file = str(message_file)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = codex_mesh.command_follow(args)
        return result, errors.getvalue()

    def run_mesh(self, arguments: list[str]) -> tuple[int, str, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = codex_mesh.main([
                arguments[0], "--session-dir", str(self.session_dir), *arguments[1:]
            ])
        return code, output.getvalue(), errors.getvalue()

    def write_pool_roster(self, entries: dict[str, object]) -> None:
        published = publish_fixture_roster(entries, agent_accounts.workers_home())
        roster = self.session_dir / codex_mesh.ROSTER_FILE
        with worker_lifecycle.locked_state(roster):
            atomic_file.write_text_atomic(roster, json.dumps(published))

    @contextlib.contextmanager
    def expire_contended_lock(self, path: Path, seconds: float) -> Generator[None]:
        """Expire fake time only after a real competing flock refuses acquisition."""
        flock = fcntl.flock
        identity = path.stat()

        def acquire(descriptor: int | io.IOBase, operation: int) -> None:
            fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
            if operation & fcntl.LOCK_UN or not os.path.samestat(identity, os.fstat(fd)):
                flock(descriptor, operation)
                return
            try:
                flock(descriptor, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                self.assertTrue(operation & fcntl.LOCK_NB,
                                f"unbounded shutdown flock on {path.name}")
                self.lifecycle_clock.advance(seconds)
                raise

        with patch.object(fcntl, "flock", acquire):
            yield

    def prime_cli(self, *options: str) -> list[str]:
        instructions_file = self.session_dir / "review-instructions.txt"
        _ = instructions_file.write_text("Review only.\nKeep café text intact.\n", encoding="utf-8")
        return [
            "prime", "--name", "template", "--instructions-file", str(instructions_file),
            *options,
        ]

    def assert_lifecycle_is_held(self) -> None:
        with (self.session_dir / "worker.lifecycle.lock").open("a") as probe:
            with self.assertRaises(BlockingIOError, msg="server effect escaped the worker lifecycle lock"):
                KERNEL_FLOCK(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @contextlib.contextmanager
    def fixture_reaping(self, reap: Callable[[int], bool]) -> Generator[None]:
        """Keep the fake process table consistent with successful fixture reaps."""
        stopped: set[int] = set()
        alive = codex_mesh._pid_alive  # pyright: ignore[reportPrivateUsage]

        def stop(pid: int, *, deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> bool:
            _ = deadline
            success = reap(pid)
            if success:
                stopped.add(pid)
            return success

        def is_alive(pid: int) -> bool:
            return pid not in stopped and alive(pid)

        with (patch.object(codex_mesh, "_reap", stop),
              patch.object(codex_mesh, "_pid_alive", is_alive)):
            yield

    @contextlib.contextmanager
    def observe_lifecycle_attempt(self, attempted: threading.Event, *, suffix: str = ".lifecycle.lock") -> Generator[None]:
        """Release a contested operation only after its competitor reaches flock."""
        flock = fcntl.flock

        def observe(descriptor: int | io.IOBase, operation: int) -> None:
            handle = descriptor if isinstance(descriptor, io.IOBase) else RosterRelockGuard.open_handle(descriptor)
            if handle is not None and str(getattr(handle, "name", "")).endswith(suffix) and operation & fcntl.LOCK_EX:
                try:
                    flock(descriptor, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    attempted.set()
                    if operation & fcntl.LOCK_NB:
                        raise
                    flock(descriptor, operation)
                else:
                    attempted.set()
                return
            flock(descriptor, operation)

        with patch.object(fcntl, "flock", observe):
            yield

    def stream_for(self, client: codex_mesh.Client, *, resident: bool = False) -> codex_mesh.TurnStream:
        client.session_dir = str(self.session_dir)
        args = self.start_args()
        args.resident = resident
        # The stand-in server started the watched turn; a turn read lists it unless it is live.
        self.started_turns["buffered-turn"] = "completed"
        return codex_mesh.TurnStream(
            args=args, session_dir=str(self.session_dir), name="worker", follow=True, resident=resident,
            timeout=TEST_HANG_GUARD_SECS, opened=time.time(), deadline=time.time() + TEST_HANG_GUARD_SECS,
            log_path=self.session_dir / "worker.log", summary_path=self.session_dir / "summary.txt",
            reply_path=None, end_marker=self.session_dir / "worker.end",
            director=codex_mesh.UnknownWorkerDirector(), client=client, port=self.server.port,
            thread_id=THREAD_ID, failure_serving=client.serving,
            runs=codex_mesh.DetachedRunWatch(str(self.session_dir), "worker"),
            turn_id="buffered-turn", followed_turn="buffered-turn",
        )

    def follow_cli(self, *options: str) -> list[str]:
        message_file = self.session_dir / "review-request.txt"
        _ = message_file.write_text("Review the second worktree", encoding="utf-8")
        return [
            "follow", "--to", "worker", "--role", "review", "--message-file", str(message_file),
            "--summary-file", str(self.session_dir / "summary.txt"),
            "--log-file", str(self.session_dir / "worker.log"), "--timeout", "5", *options,
        ]

    def assert_review_record(self, name: str, thread_id: str) -> None:
        record = _as_test_dict(self.roster().get(name))
        self.assertEqual(
            {key: record.get(key) for key in ("thread_id", "status")},
            {"thread_id": thread_id, "status": "done"},
        )

    def assert_request_failure_closes_clients(self, arguments: list[str], name: str) -> None:
        client_type = codex_mesh.Client
        original_close = client_type.close
        opened: list[codex_mesh.Client] = []
        closed: list[codex_mesh.Client] = []

        def open_client(port: int, client_name: str, *, deadline: float | None = None,
                        operation_deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> codex_mesh.Client:
            client = client_type(port, client_name, deadline=deadline, operation_deadline=operation_deadline)
            opened.append(client)
            return client

        def close_client(client: codex_mesh.Client) -> None:
            closed.append(client)
            original_close(client)

        with (
            patch.object(codex_mesh, "Client", open_client),
            patch.object(client_type, "close", close_client),
        ):
            code, output, errors = self.run_mesh(arguments)

        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertEqual(len(errors.splitlines()), 1, errors)
        self.assertTrue(errors.startswith(f"codex_mesh: {name}: "), errors)
        self.assertIn("scripted request failure", errors)
        self.assertTrue(opened, "the failed request must use a client")
        self.assertCountEqual(opened, closed)

    def replacement_record(self, name: str = "worker") -> dict[str, object]:
        record = staff.local_snapshot()
        parent = record.by_current_session(os.environ["CODEX_THREAD_ID"])
        assert parent is not None
        member = next((row for row in record.find(kind="worker", parent=parent.id)
                       if row.name == name), None)
        reference = member.id if member is not None else (self.fork_thread_id if name == "reviewer" else THREAD_ID)
        return {"staff_id": reference, "codex_home": str(agent_accounts.workers_home()), "thread_id": "04d16c0e-247d-5e60-9e96-4e1b9625e7b6",
                "session_id": "04d16c0e-247d-5e60-9e96-4e1b9625e7b6", "status": "running", "turn_id": "replacement-turn",
                "context_tokens": 777, "launcher_pid": os.getpid(),
                "port": self.server.port, "instructions_file": "replacement-instructions.txt"}

    def replace_worker(self, name: str = "worker") -> dict[str, object]:
        entries = self.roster()
        previous = _as_test_dict(entries.get(name))
        entries[name] = {**previous, "status": "ended"}
        self.write_pool_roster(entries)
        replacement = self.replacement_record(name)
        entries[name] = replacement
        self.write_pool_roster(entries)
        return replacement

    def held_reviewer_record(self, lifecycle: design_roles.RoleOpen | design_roles.RoleNeedsRecovery) -> design_roles.HeldDesign:
        from scripts.production.staff_test_support import unit_membership
        parent = staff.by_id(staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        assert parent is not None
        _ = staff.set_worktree(parent.id, staff.PresentWorktree(remote.local_machine(), self.home / parent.name), expected=parent.worktree)
        owner = unit_membership("demo", "design")
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), self.session_dir), expected=owner.worktree)
        _ = (self.session_dir / plan_history.STATE_FILENAME).write_text(json.dumps({"production": "demo", "unit": owner.name}))
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"CODEX_THREAD_ID": owner.current_session})))
        _ = fixture_worker("worker", owner.id, runtime_id=str(lifecycle.staff_id),
                       backend=staff.CodexBackend(Path(os.environ["CODEX_HOME"])))
        self.write_pool_roster({"worker": {"thread_id": session_staff.worker_session({"staff_id": lifecycle.staff_id}), "status": "done", "role": "astra-reviewer"}})
        prompt = self.session_dir / "authorized.md"
        design = design_roles.HeldDesign(design_roles.DesignPlacement("00000000-0000-4000-8000-000000000026", "fixture-host", 3),
            "fixture-repo", str(self.session_dir), str(self.session_dir / "design.md"), "design",
            "2026-10-21T12:00:00+00:00", design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "unused-designer", design_roles.RoleNotOpened()),
             design_roles.RoleInstance("reviewer", "worker", lifecycle, str(prompt))))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        _ = prompt.write_text("Current authorized reviewer instructions.")
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review",
            "held_design_id": design.placement.design_id, "held_role": "reviewer", "port": self.server.port,
            "authorized_generation": 3, "authorized_prompt": str(prompt),
            "access_policy": {"kind": "ReadOnlyReviewer"}}})
        agents_log.append(THREAD_ID, "review", None, datetime.now(timezone.utc))
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": os.environ["CODEX_THREAD_ID"],
                                                               "DESIGN_ROLES_HOST": "fixture-host"})))
        return design

    def prepare_refusing_account(self, *, stub_ensure: bool = True) -> agent_accounts.KnownCodexAccount:
        home = self.session_dir / "account-home"
        home.mkdir()
        notes = home / "notes"
        notes.mkdir()
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(notes)})))
        selected = home / ".codex"
        selected.mkdir()
        login = "serving@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (selected / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (notes / "codex2.md").write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "workers", "codex2"]), 0)
        write_server_record(self.session_dir, self.server.port, selected)
        if stub_ensure:
            _ = self.patches.enter_context(patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)))
        return agent_accounts.KnownCodexAccount(selected, login, "codex2")


    def configure_context(self) -> None:
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir / "unit-workers"),
        }))
        _ = (self.session_dir / "progress_history_state.json").write_text(json.dumps({
            "production": "natedev", "unit": "models", "working_dir": str(self.session_dir),
        }))

    @contextlib.contextmanager
    def thread_title_context(self) -> Generator[None, None, None]:
        from scripts.production.staff_test_support import isolated_staff_file, production_membership, unit_membership

        place = unit_labels.ProductionPlace(unit_labels.ProductionWithoutRunningShowrunner("sdlc"),
                                            unit_labels.DirectsUnit("roles"))
        runner = {"session": "", "socket": "", "name": "sdlc", "zone": "UTC",
                  "doc": str(self.session_dir / "production.md")}
        _ = self.patches.enter_context(isolated_staff_file(self.session_dir / "staff.json", {}))
        _ = self.patches.enter_context(patch.object(message_roster, "_registered_showrunners", return_value=[runner]))
        _ = self.patches.enter_context(patch.object(message_roster, "_production_places", return_value={"title-director": place}))
        production_membership("sdlc", "roles")
        parent = unit_membership("sdlc", "roles")
        _ = staff.set_worktree(parent.id, staff.PresentWorktree(remote.local_machine(), self.session_dir), expected=parent.worktree)
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id})))
        _ = (self.session_dir / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": parent.id}))
        yield


class FixtureModelDefaultsTests(unittest.TestCase):
    def test_only_unchanged_baseline_results_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            registry = write_registry(Path(root) / "models.conf")
            with patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry)}):
                resolve = MagicMock(return_value=("gpt-test", "high"))
                defaults = FixtureModelDefaults(resolve)
                configuration = models_registry.launch_configuration({})
                args = argparse.Namespace(role="impl", task="", model="gpt-test", effort="high")
                self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
                self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
                self.assertEqual(resolve.call_count, 1)
                _ = write_registry(registry, {"direct.codex": {"impl": "gpt-test:max"}}, update=True)
                resolve.return_value = ("gpt-test", "max")
                self.assertEqual(defaults(args, configuration), ("gpt-test", "max"))
                self.assertEqual(defaults(args, configuration), ("gpt-test", "max"))
                self.assertEqual(resolve.call_count, 3, "changed registries must resolve on every turn")

    def test_custom_registry_and_subprocess_faults_bypass_baseline_reuse(self) -> None:
        resolve = MagicMock(return_value=("gpt-test", "high"))
        defaults = FixtureModelDefaults(resolve)
        configuration = models_registry.launch_configuration({})
        args = argparse.Namespace(role="review", task="", model="gpt-test", effort="high")
        self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
        with patch.dict(os.environ, {"MODELS_CONFIG_FILE": "/tmp/custom-mesh-registry"}):
            self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
            self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
        with patch.object(subprocess, "run"):
            self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
        self.assertEqual(resolve.call_count, 4)



if __name__ == "__main__":
    _ = unittest.main()
