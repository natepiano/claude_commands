#!/usr/bin/env python3
"""See docs/as-built/long-lived-workers.md for worker and server operations."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import base64
import contextlib
import errno
import fcntl
import importlib
import json
import math
import os
import re
import secrets
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, NotRequired, NamedTuple, Protocol, TextIO, TypedDict, cast, final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.accounts.role_map import is_role_map_host, role_map_command, RoleMapUnavailable, RoleAccountUnavailable  # noqa: E402
from scripts.direct.worker_state import ROSTER_LIVE
from scripts.direct import assign_gate, plan_history, progress_history
from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, write_text_atomic
from scripts.agents import worker_lifecycle
from scripts.lib.agent_scope import agent_scope_prefix

NO_LOCK_DEADLINE = worker_lifecycle.NO_LOCK_DEADLINE
from scripts.lib import build_folders, nix_env, process_group, unit_runs, models_registry
from scripts.lib.unit_runs import SERVER_FILE as SERVER_FILE
from scripts.lib.probe_session import PROBE_ENV
from scripts.production import staff, worker_admission, session_staff
from scripts.production.worker_admission import WorkerStartAllowed
from scripts.shutdown.stop_results import SourceRelease, SourceReleased, SourceReleaseUnknown
from scripts.astra.design_roles import PhaseBoundDesigner, ReadOnlyReviewer, ThreadAccessPolicy
from scripts.message import sessions, process_table
from scripts.message import repeat_guard
from scripts.message import send_budget
from scripts.whoami import account
from scripts.usage import agents_log
from scripts.whoami.agent_accounts import (
    workers_home, codex_home, codex_home_display, user_zone,
    KnownCodexAccount, UnknownCodexAccount, RefusingCodexAccount, codex_account, codex_home_role_label,
    codex_home_matches_label,
)


if TYPE_CHECKING:
    from scripts.accounts.moves import RequestedMove


class UnitWorkerLedgerLocation(Protocol):
    @property
    def path(self) -> Path: ...


class MeshTitleRenderer(Protocol):
    def mesh_title(self, session_dir: str, name: str, record: Mapping[str, object]) -> str: ...


class UnitWorkerRegistry(Protocol):
    """Ledger operations used by mesh without importing its allocation client."""

    WorkerUnavailable: type[Exception]

    def ending_worker(self, run: Path, name: str) -> contextlib.AbstractContextManager[Callable[[], None]]: ...
    def run_has_live_director(self, run: Path) -> bool: ...
    def note_backend(self, run: Path, name: str, thread_id: str, home: Path) -> None: ...
    def ledger_for_run(self, run: Path) -> UnitWorkerLedgerLocation: ...
    def shown_name(self, run: Path, address: str, *, role: str = "", held_design_id: str = "",
                   held_role: str = "") -> str: ...
    def allocation_is_live(self, record: Mapping[str, object]) -> bool: ...
    def clear_allocation(self, record: dict[str, object]) -> None: ...
    def allocation_for_follow(self, run: Path, name: str) -> dict[str, object]: ...
    def release_allocation(self, run: Path, name: str, expected: Mapping[str, object]) -> None: ...


# The registry calls mesh to allocate threads; load its small interface dynamically
# at this boundary rather than creating a static import cycle.
unit_workers = cast(UnitWorkerRegistry, cast(object, importlib.import_module("scripts.direct.workers")))


@dataclass(frozen=True)
class ServingCodexServer:
    """The process and sign-in retained before a request reaches its server."""

    port: int
    pid: int
    home: Path
    account: RefusingCodexAccount
    started_here: bool = False


@dataclass(frozen=True)
class NotBoundCodexServer:
    """No serving process was identified; the selected home remains context only."""

    home: Path = field(default_factory=workers_home)
    reason: str = "serving server is not bound"
    started_here: bool = False

    @property
    def account(self) -> UnknownCodexAccount:
        return UnknownCodexAccount(self.home, self.reason)


CodexServingState = ServingCodexServer | NotBoundCodexServer


_STARTED_SERVERS: set[tuple[int, int]] = set()


def _capture_serving_server(session_dir: str, port: int) -> CodexServingState:
    records = [record for record in _recorded_run_servers(session_dir)
               if record.get("port") == port and isinstance(record.get("pid"), int)]
    live = [record for record in records if _pid_alive(_as_int(record.get("pid")))]
    candidates = live
    identities = {(_as_int(record.get("pid")), server_record_home(record)) for record in candidates}
    if len(identities) != 1:
        return NotBoundCodexServer(reason="serving process cannot be identified")
    pid, home = identities.pop()
    return ServingCodexServer(port, pid, home, codex_account(home), (port, pid) in _STARTED_SERVERS)


def _bind_serving_server(client: Client, session_dir: str, *, started_here: bool = False) -> CodexServingState:
    serving = _capture_serving_server(session_dir, client.port)
    if started_here:
        serving = replace(serving, started_here=True)
    client.serving = serving
    client.session_dir = session_dir
    return serving


class ServingRequestFailure(SystemExit):
    """An RPC failed with its serving identity retained, including removed records."""

    def __init__(self, detail: str, serving: CodexServingState, reported: bool) -> None:
        super().__init__(detail)
        self.serving: CodexServingState = serving
        self.reported: bool = reported


class WorkerQuotaRecoveryStopped(ServingRequestFailure):
    """Quota recovery stopped on this thread, which may replace the original."""

    def __init__(self, detail: str, serving: CodexServingState, port: int, thread_id: str) -> None:
        super().__init__(detail, serving, True)
        self.port: int = port
        self.thread_id: str = thread_id


@dataclass(frozen=True)
class KnownWorkerDirector:
    session_id: str


@dataclass(frozen=True)
class UnknownWorkerDirector:
    """The command's director could not be uniquely identified."""


@dataclass(frozen=True)
class ResolveWorkerDirector:
    """Resolve the director while the command's launcher evidence is intact."""


RESOLVE_WORKER_DIRECTOR = ResolveWorkerDirector()


@dataclass(frozen=True)
class RefusalWithoutCommandExit:
    """Report routing while the command can still recover."""


@dataclass(frozen=True)
class CommandStoppedBeforeThread:
    session_dir: str
    director: KnownWorkerDirector | UnknownWorkerDirector | ResolveWorkerDirector = RESOLVE_WORKER_DIRECTOR


@dataclass(frozen=True)
class CommandStoppedOnThread:
    session_dir: str
    thread_id: str
    director: KnownWorkerDirector | UnknownWorkerDirector | ResolveWorkerDirector = RESOLVE_WORKER_DIRECTOR


RefusalCommandContext = RefusalWithoutCommandExit | CommandStoppedBeforeThread | CommandStoppedOnThread
REFUSAL_WITHOUT_COMMAND_EXIT = RefusalWithoutCommandExit()


def _serving_request(
    client: Client, method: str, params: dict[str, object], *, submits_work: bool = False,
    stopped: RefusalCommandContext = REFUSAL_WITHOUT_COMMAND_EXIT,
) -> dict[str, object]:
    opened = time.time()
    try:
        return _require(client.call(method, params), method)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as error:
        serving = client.serving
        detail = str(error) or type(error).__name__
        proven = submits_work and QUOTA_REFUSAL in detail and (
            serving.started_here or time.time() - opened > RETRY_FAST_FAILURE_SECS)
        if proven:
            _owe_quota_continuation(client, _as_str(params.get("threadId")))
            _report_quota_refusal(client.name, serving, detail, session_dir=client.session_dir,
                                  thread_id=_as_str(params.get("threadId")), stopped=stopped)
        raise ServingRequestFailure(detail, serving, proven) from error

DEV_SHELL_FILE = "mesh_dev_shell.json"
FAILED_DEV_SHELL_FILE = "mesh_failed_dev_shell.json"
SERVER_START_FILE = "mesh_server_start.json"
SERVER_LOG = "mesh_server.log"
ROSTER_FILE = "mesh_roster.json"
# Runs a worker detached with mesh_runs.py: one `<run id>.json` record and `<run id>.log` each.
from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR
# A run record written before its supervisor's pid counts as live for this long.
from scripts.agents.worker_lifecycle import DETACHED_RUN_START_GRACE_SECS
# mesh_runs.py ends a run whose supervisor is gone; its delivery may wait on send.py.
MESH_RUNS_SCRIPT = Path(__file__).with_name("mesh_runs.py")
DEAD_RUN_END_TIMEOUT_SECS = 300
# The folders that hold unit run folders. An app-server whose session directory is one of their
# children serves that run alone, so its workers get the run as UNIT_RUN_DIR.
RUN_ROOTS: tuple[Path, ...] = unit_runs.run_roots()
# Servers dropped by _retire_server, kept so `stop` can still reap them. A
# retired server is abandoned rather than signalled: it may still be finishing a
# peer worker's turn, and the end of the run is the only moment that knows
# nobody needs it.
LOCK_FILE = "mesh_server.lock"
CONNECT_TIMEOUT_SECS = 20
CALL_TIMEOUT_SECS = 300
THREAD_RELEASE_TIMEOUT_SECS = 20.0
# How long `compact` waits for a thread to read idle: after it interrupts a compaction turn that
# did not finish, and before it refuses a finished worker that still has a live turn.
COMPACT_IDLE_WAIT_SECS = 60
COMPACT_IDLE_POLL_SECS = 1
# `compact` exits with this when the thread still has a live turn, so no follow-up can be sent.
COMPACT_TURN_STILL_LIVE = 3
# The compaction finished, but its result no longer belongs to the roster entry.
COMPACT_ROSTER_CHANGED = 4
SERVER_START_TIMEOUT_SECS = 30
# Keep the dev-shell server's startup allowance and sweep grace consistent.
DEV_SHELL_START_TIMEOUT_SECS = 300
# Codex compacts a thread on its own once its context passes this many tokens. Every request
# re-reads the whole context, and threads left to Codex's default grew to 460K; a 200K cap cut a
# replayed day of worker usage by 19% (user, 2026-10-15). A server started with another value is
# retired at the next dispatch, so each worker moves to the new limit after its current turn.
AUTO_COMPACT_TOKENS = 200_000
# The longest one write_stdin call may block on a command a worker started, in milliseconds.
# Codex's default ends a wait after a short time, so a worker waiting on a build made a model
# call every few seconds; 68% of worker calls were such waits (2026-10-15). Not part of the
# server record: an older server keeps its value until it retires for another reason.
BACKGROUND_TERMINAL_MAX_TIMEOUT_MS = 300_000
# How long to keep watching after a turn ends for a queued turn already in flight.
QUEUE_GRACE_SECS = 3.0
# Remote role changes have no local file notification. Check only that role
# every five seconds off the authority; detached runs still use sixty seconds.
REMOTE_ROLE_CHECK_SECS = 5.0
# The SandboxMode strings a thread starts with: workers write, standing reviewers only read.
WORKER_SANDBOX = "danger-full-access"
READ_ONLY_SANDBOX = "read-only"
SANDBOX_MODES = (WORKER_SANDBOX, READ_ONLY_SANDBOX)
# Production gives a server five seconds to stop; disposable child processes need no long grace.
REAP_WAIT_SECS = 5.0
REAP_POLL_SECS = 0.2
# stop --pause interrupts its workers within this, so the reap after it ends inside the
# 30 s a shutdown gives the whole stop; a hung server is reaped, never left running.
PAUSE_INTERRUPT_SECS = 15.0
# A launcher waiting on a turn wakes this often to check that the turn has not ended unseen.
UNSEEN_END_CHECK_SECS = 60.0
DETACHED_RUN_CHECK_SECS = 60.0
UNSEEN_END_CALL_SECS = 10.0
# How many of the newest turns a read lists to find the watched or live turn.
RECENT_TURNS = 8
# How fast a failure has to arrive to be read as this machine's fault rather
# than the provider's. An app-server that has wedged answers from what it
# cached, without a round trip, so it fails in seconds; a refusal that travelled
# to the provider and back does not arrive this quickly and this consistently.
RETRY_FAST_FAILURE_SECS = 120.0
# The provider's words for an account with no allowance or credits left to spend.
QUOTA_REFUSAL = "hit your usage limit"
# What an app-server says when Codex was signed in again after it started: it holds the old token
# and cannot refresh it. A server started now reads the current sign-in.
STALE_SIGN_IN = "access token could not be refreshed"
# agent_notes.py's `blocked` verb moves Codex work to Claude; one switch took 8 s
# and one relay 9 s when measured, and the relays run in parallel.
QUOTA_REPORT = Path(__file__).resolve().parent.parent / "whoami" / "agent_notes.py"
QUOTA_REPORT_TIMEOUT_SECS = 120
CAPACITY_WAIT_SECS = 30.0
CAPACITY_MAX_WAIT_SECS = 300.0
CAPACITY_BUDGET_SECS = 1200.0
WAITING_CAPACITY = "waiting_capacity"
CAPACITY_EXHAUSTED = "capacity_exhausted"
LAUNCHER_ATTACHED_STATUSES = frozenset(("running", WAITING_CAPACITY))
ENDABLE_STATUSES = frozenset((
    "starting", "running", "done", "failed", "paused", WAITING_CAPACITY, CAPACITY_EXHAUSTED,
))


class RpcMessage(TypedDict, total=False):
    """One JSON-RPC frame. Every field is optional; which appear names the kind."""

    id: str
    method: str
    params: dict[str, object]
    result: dict[str, object]
    error: dict[str, object]


class ServerRecord(TypedDict):
    port: int
    pid: int
    codex_home: NotRequired[str]
    auto_compact_tokens: NotRequired[int]
    design_context: NotRequired[dict[str, object]]
    dev_shell_worktree: NotRequired[str]


@dataclass(frozen=True)
class InheritedServerEnvironment:
    """No unit worktree dev-shell applies to this launch."""


@dataclass(frozen=True)
class WorktreeDevShell:
    """The nearest unit worktree shell to load and prove before launch."""

    worktree: Path
    input_revision: str


ServerDevShell = InheritedServerEnvironment | WorktreeDevShell
_INHERITED_SERVER_ENVIRONMENT = InheritedServerEnvironment()


@dataclass(frozen=True)
class DevShellProbeUnavailable:
    """The probe cannot decide whether the server's dev-shell should change."""

    reason: str


@dataclass(frozen=True)
class RememberedDevShellFailure:
    """Suppress this failed shell's unchanged inputs until its retry deadline."""

    worktree: Path
    input_revision: str
    retry_at: float

    def suppresses(self, dev_shell: WorktreeDevShell) -> bool:
        return (self.worktree == dev_shell.worktree
                and self.input_revision == dev_shell.input_revision
                and time.time() < self.retry_at)


DevShellProbe = ServerDevShell | DevShellProbeUnavailable | RememberedDevShellFailure


@dataclass(frozen=True)
class LiveServer:
    port: int


@dataclass(frozen=True)
class ServerRestartRequired:
    """No usable recorded server; unreadable evidence must be repaired first."""
    doubt: str = ""


ServerAvailability = LiveServer | ServerRestartRequired


WorkerRole = Literal["impl", "test", "fix", "review"]
WORKER_ROLES = staff.WORKER_ROLES


@dataclass(frozen=True)
class AssignedWorkerRole:
    """A unit-run thread whose current assignment belongs in the roster."""

    role: WorkerRole


@dataclass(frozen=True)
class RolelessThread:
    """A mesh thread outside a unit run, such as a consultation friend."""


RosterRole = AssignedWorkerRole | RolelessThread


class WorkerTaskRecord(TypedDict):
    staff_id: NotRequired[staff.StaffId]
    task: NotRequired[str]
    model: NotRequired[str]
    effort: NotRequired[str]
    turn_model: NotRequired[str]
    turn_effort: NotRequired[str]
    held_design_id: NotRequired[str]
    held_role: NotRequired[str]
    access_policy: NotRequired[dict[str, object]]
    authorized_prompt: NotRequired[str]
    authorized_generation: NotRequired[int]
    unit_ledger: NotRequired[str]


class FollowableRecord(WorkerTaskRecord):
    settled_launcher_pid: NotRequired[int]
    paused_from: NotRequired[str]
    paused_turn: NotRequired[bool]
    thread_id: str
    status: Literal["done", "failed", "paused"]
    revision: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]
    preserved_work: NotRequired[bool]


# `port` is the app-server the worker's launcher is attached to. It stays the worker's way in while
# that launcher lives, even after the run has moved to a newer server.
class ActiveRecord(WorkerTaskRecord):
    thread_id: str
    status: Literal["running"]
    revision: NotRequired[int]
    turn_id: str
    launcher_pid: NotRequired[int]
    port: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]
    preserved_work: NotRequired[bool]


class StartingRecord(WorkerTaskRecord):
    thread_id: str
    status: Literal["starting"]
    previous_task_role: NotRequired[WorkerRole]
    previous_task_lens: NotRequired[agents_log.Lens | None]
    previous_status: NotRequired[Literal["done", "failed", "paused"]]
    revision: NotRequired[int]
    launcher_pid: NotRequired[int]
    port: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]
    preserved_work: NotRequired[bool]


class WaitingCapacityRecord(WorkerTaskRecord):
    thread_id: str
    status: Literal["waiting_capacity"]
    revision: NotRequired[int]
    launcher_pid: int
    port: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]
    preserved_work: NotRequired[bool]


class ExhaustedRecord(WorkerTaskRecord):
    settled_launcher_pid: NotRequired[int]
    thread_id: str
    status: Literal["capacity_exhausted"]
    revision: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]
    preserved_work: NotRequired[bool]


class EndedRecord(WorkerTaskRecord):
    thread_id: str
    status: Literal["ended"]
    revision: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]
    preserved_work: NotRequired[bool]


ThreadRecord = (FollowableRecord | ActiveRecord | StartingRecord |
                WaitingCapacityRecord | ExhaustedRecord | EndedRecord)


FOLLOWABLE_STATES = frozenset(("done", "failed", "paused"))


@dataclass(frozen=True)
class FollowableThread:
    thread_id: str
    status: Literal["done", "failed", "paused"]


@dataclass(frozen=True)
class ActiveThread:
    thread_id: str
    turn_id: str
    launcher_pid: int


@dataclass(frozen=True)
class WaitingCapacityThread:
    thread_id: str
    launcher_pid: int


@dataclass(frozen=True)
class StartingThread:
    thread_id: str
    launcher_pid: int


@dataclass(frozen=True)
class ExhaustedThread:
    thread_id: str


@dataclass(frozen=True)
class EndedThread:
    thread_id: str




@dataclass(frozen=True)
class InvalidThread:
    reason: str


RosterThread = (FollowableThread | ActiveThread | WaitingCapacityThread |
                StartingThread | ExhaustedThread | EndedThread | InvalidThread)


@dataclass(frozen=True)
class KnownContextSize:
    """A context size reported by the app-server or saved in the roster."""

    tokens: int


@dataclass(frozen=True)
class ContextSizeUnavailable:
    """No trustworthy context size was present at this protocol boundary."""


ContextSize = KnownContextSize | ContextSizeUnavailable


@dataclass(frozen=True)
class NotMeasuredSinceCompaction:
    """Compaction finished, but the next turn has not measured the worker's size."""


RecordedContextSize = KnownContextSize | NotMeasuredSinceCompaction | ContextSizeUnavailable


def _roster_thread(value: object) -> RosterThread:
    """Validate the readable JSON record before acting on its state."""
    entry = _as_dict(value)
    thread_id = _as_str(session_staff.worker_session(entry))
    status = entry.get("status")
    turn_id = _as_str(entry.get("turn_id"))
    pid = entry.get("launcher_pid")
    launcher_pid = pid if isinstance(pid, int) else 0
    if not thread_id:
        return InvalidThread("missing thread id")
    if status == "done":
        return FollowableThread(thread_id, "done")
    if status == "failed":
        return FollowableThread(thread_id, "failed")
    if status == "paused":
        return FollowableThread(thread_id, "paused")
    if status == "running":
        return ActiveThread(thread_id, turn_id, launcher_pid) if turn_id else InvalidThread("running thread has no turn id")
    if status == WAITING_CAPACITY:
        return WaitingCapacityThread(thread_id, launcher_pid)
    if status == "starting":
        return StartingThread(thread_id, launcher_pid)
    if status == CAPACITY_EXHAUSTED:
        return ExhaustedThread(thread_id)
    if status == "ended":
        return EndedThread(thread_id)
    return InvalidThread(f"unknown status {status}")


# The three places an untyped value enters this module. Each is confined to one
# helper so the rest of the file stays fully typed.


def _loads(text: str) -> object:
    """json.loads as an `object`, so callers must narrow before use."""
    try:
        return json.loads(text)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None


def _attr(args: argparse.Namespace, key: str) -> object:
    return getattr(args, key, None)


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _unpack_len(fmt: str, data: bytes) -> int:
    return int(struct.unpack(fmt, data)[0])  # pyright: ignore[reportAny]


def _bound_port(sock: socket.socket) -> int:
    return int(sock.getsockname()[1])  # pyright: ignore[reportAny]


def _now_stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", cast("object", value))


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _roster_role(value: object) -> RosterRole:
    """Convert a recorded role to the task role used by the dispatcher."""
    role = _as_str(value)
    if role == "astra-reviewer":
        return AssignedWorkerRole("review")
    if role == "astra-designer":
        return AssignedWorkerRole("impl")
    if role not in WORKER_ROLES:
        return RolelessThread()
    return AssignedWorkerRole(cast(WorkerRole, role))


def task_assignments() -> agents_log.RoleLog:
    try:
        return agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
    except Exception as error:
        print("agents_log: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return agents_log.RoleLog({}, ())


def task_role(thread_id: str, assignments: agents_log.RoleLog, *, default_role: str = "") -> RosterRole:
    assignment = agents_log.role_at(assignments, thread_id, datetime.now(timezone.utc))
    if isinstance(assignment, agents_log.Assignment):
        return _roster_role(assignment.role)
    return _roster_role(default_role) if default_role in (*WORKER_ROLES, "astra-reviewer", "astra-designer") else RolelessThread()


def _worker_role(args: argparse.Namespace) -> WorkerRole:
    role = _roster_role(_attr(args, "role"))
    if isinstance(role, RolelessThread):
        raise SystemExit("codex_mesh: this worker action requires a role")
    return role.role


def _dispatch_lens(args: argparse.Namespace) -> agents_log.Lens | None:
    lens = _attr(args, "lens")
    return cast(agents_log.Lens, lens) if lens else None


def _record_assignment(thread_id: str, role: WorkerRole, lens: agents_log.Lens | None,
                       only_if_changed: bool = False) -> None:
    try:
        now = datetime.now(timezone.utc)
        if only_if_changed:
            log = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
            current = agents_log.role_at(log, thread_id, now)
            if isinstance(current, agents_log.Assignment) and current.role == role and current.lens == lens:
                return
        agents_log.append(thread_id, role, lens, now)
    except Exception as exc:
        # Recording must never interrupt dispatch.
        print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)


def _as_float(value: object, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) else fallback


def _context_size_from_record(value: object) -> RecordedContextSize:
    tokens = _as_dict(value).get("context_tokens")
    if tokens == "not_measured_since_compaction":
        return NotMeasuredSinceCompaction()
    if isinstance(tokens, int) and tokens >= 0:
        return KnownContextSize(tokens)
    return ContextSizeUnavailable()


def _context_size_from_usage(params: dict[str, object]) -> ContextSize:
    usage = _as_dict(_as_dict(params.get("tokenUsage")).get("last"))
    tokens = usage.get("inputTokens")
    if isinstance(tokens, int) and tokens >= 0:
        return KnownContextSize(tokens)
    return ContextSizeUnavailable()


def _parse_frame(text: str) -> RpcMessage | None:
    """Decode one frame, returning None for anything that is not a JSON object."""
    raw = _loads(text)
    if not isinstance(raw, dict):
        return None
    return cast("RpcMessage", cast("object", raw))


# ---------------------------------------------------------------------------
# Minimal websocket client
#
# The app-server's loopback listener needs no auth, so a client is a handshake
# plus RFC 6455 framing. Pulling in a dependency for that would put an install
# step between a worker and its peers.
# ---------------------------------------------------------------------------


class SessionStateChanged(Exception):
    """A marker or inbox changed while the launcher was waiting for a frame."""


@dataclass(frozen=True)
class SocketOnlyWait:
    """RPC replies must finish even when local state changes during the call."""


SOCKET_ONLY_WAIT = SocketOnlyWait()


from scripts.agents.worker_lifecycle import (SocketReady as SocketReady, WaitDeadlineExpired as WaitDeadlineExpired, StateFilesChanged as StateFilesChanged, FileWaitResult, LinuxFileChanges as LinuxFileChanges, KqueueFileChanges as KqueueFileChanges)


@final
class FollowEvents:
    """One follower's relevant file changes, multiplexed with its current socket."""

    def __init__(self, session_dir: str, name: str) -> None:
        from scripts.accounts.worker_names import accounts_config_file
        from scripts.accounts.role_map import groups_file

        folder = Path(session_dir)
        paths = {folder / filename for filename in (
            ROSTER_FILE, SERVER_FILE, plan_history.STATE_FILENAME,
            f"{name}.end", f"{name}.account-move.json",
        )}
        paths.add(session_staff.worker_inbox_path(session_dir, name))
        paths.update((accounts_config_file(), groups_file()))
        self.roster_path: Path = folder / ROSTER_FILE
        self.server_path: Path = folder / SERVER_FILE
        self.name: str = name
        self._changes: LinuxFileChanges | KqueueFileChanges = (
            LinuxFileChanges(paths) if sys.platform.startswith("linux") else KqueueFileChanges(paths))
        self.roster_state: dict[str, object] = self._roster_state()
        self.server_state: dict[str, object] = _read_json_object(self.server_path)

    def _roster_state(self) -> dict[str, object]:
        entry = _as_dict(_read_json_object(self.roster_path).get(self.name))
        return {key: entry.get(key) for key in (
            "staff_id", "turn_id", "status", "port", "launcher_pid", "continuation_owed")}

    def wait(self, connection: socket.socket, deadline: float) -> FileWaitResult:
        while time.monotonic() < deadline:
            result = self._changes.wait(connection, deadline)
            if not isinstance(result, StateFilesChanged):
                return result
            relevant = set(result.paths)
            if self.roster_path in relevant:
                roster = self._roster_state()
                if roster == self.roster_state:
                    relevant.remove(self.roster_path)
                self.roster_state = roster
            if self.server_path in relevant:
                server = _read_json_object(self.server_path)
                if server == self.server_state:
                    relevant.remove(self.server_path)
                self.server_state = server
            if relevant:
                return StateFilesChanged(frozenset(relevant))
        return WaitDeadlineExpired.EXPIRED

    def close(self) -> None:
        self._changes.close()


# These deltas are never consumed by the launcher. Match only the top-level
# method, before decoding potentially large output text; replies and completions
# still go through the JSON decoder unchanged.
_IGNORED_DELTA = re.compile(b"".join((
    rb'^[{]\s*(?:"jsonrpc"\s*:\s*"2\.0"\s*,\s*)?"method"\s*:\s*"(?:',
    rb'item/(?:commandExecution/outputDelta|agentMessage/delta|reasoning/[^"\\]*Delta|',
    rb'plan/delta)|',
    rb'thread/realtime/[^"\\]*delta)"\s*[,}]',
)))


@dataclass(frozen=True)
class TransportDeadline:
    """One wait's local limit and operation limit, both on the monotonic clock.

    Retaining both limits prevents a late transport failure from being relabelled
    as shutdown expiry. Operation expiry wins ties, but never an early timeout.
    """
    local: float
    operation: float

    @property
    def expires(self) -> float:
        return min(self.local, self.operation)

    def check_operation(self) -> None:
        if self.operation <= self.local and time.monotonic() >= self.operation:
            raise worker_lifecycle.OperationDeadlineExpired("shutdown incomplete: RPC operation deadline expired")

    def remaining(self) -> float:
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            self.check_operation()
            raise TimeoutError("app-server transport deadline expired")
        return remaining


@final
class WebSocket:
    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = CONNECT_TIMEOUT_SECS,
                 *, client_limit: float = NO_LOCK_DEADLINE,
                 operation_deadline: float = NO_LOCK_DEADLINE) -> None:
        setup = TransportDeadline(min(client_limit, time.monotonic() + timeout),
                                  worker_lifecycle.effective_deadline(operation_deadline))
        try:
            connection = socket.create_connection((host, port), timeout=setup.remaining())
        except TimeoutError:
            setup.check_operation()
            raise
        self._sock: socket.socket = connection
        self._deadline: TransportDeadline = setup
        try:
            _ = setup.remaining()
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            with self._waiting():
                self._sock.sendall(
                    (
                        f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                    ).encode()
                )
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                with self._waiting():
                    chunk = self._sock.recv(4096)
                if not chunk:
                    raise ConnectionError("app-server closed during handshake")
                buffer += chunk
            head, _, rest = buffer.partition(b"\r\n\r\n")
            if b" 101 " not in head.split(b"\r\n")[0]:
                detail = head.decode(errors="replace")[:200]
                raise ConnectionError(f"handshake refused: {detail}")
            self._buffer: bytearray = bytearray(rest)
            # Setup's allowance is not the lifetime of the connected socket.
            self._deadline = TransportDeadline(NO_LOCK_DEADLINE, NO_LOCK_DEADLINE)
        except BaseException:
            self.close()
            raise

    def set_deadline(self, deadline: TransportDeadline) -> None:
        """Install the same absolute budget for sending and assembling a reply."""
        self._deadline = deadline

    @contextlib.contextmanager
    def _waiting(self) -> Generator[None]:
        deadline = self._deadline
        remaining = deadline.remaining()
        self._sock.settimeout(None if remaining == NO_LOCK_DEADLINE else remaining)
        try:
            yield
        except TimeoutError:
            deadline.check_operation()
            raise
        _ = deadline.remaining()

    def send(self, payload: str) -> None:
        data = payload.encode()
        mask = secrets.token_bytes(4)
        frame = bytearray([0x81])
        length = len(data)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", length)
        frame += mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        with self._waiting():
            self._sock.sendall(bytes(frame))

    def _fill(self, count: int, wake: FollowEvents | SocketOnlyWait) -> None:
        while len(self._buffer) < count:
            if isinstance(wake, FollowEvents):
                with self._waiting():
                    result = wake.wait(self._sock, self._deadline.expires)
                if isinstance(result, StateFilesChanged):
                    raise SessionStateChanged()
                if result is WaitDeadlineExpired.EXPIRED:
                    self._deadline.check_operation()
                    raise TimeoutError("app-server frame timed out")
            with self._waiting():
                chunk = self._sock.recv(262144)
            if not chunk:
                raise ConnectionError("app-server closed the connection")
            self._buffer.extend(chunk)

    @property
    def has_pending_frames(self) -> bool:
        """Bytes already read or ready on the socket must precede lifecycle readbacks."""
        return bool(self._buffer) or bool(select.select([self._sock], [], [], 0)[0])

    def wait_for_activity(self, wake: FollowEvents, deadline: float, *,
                          operation_deadline: float = NO_LOCK_DEADLINE) -> None:
        """Wake for unread bytes or file changes without consuming a notice."""
        self.set_deadline(TransportDeadline(deadline, worker_lifecycle.effective_deadline(operation_deadline)))
        _ = self._deadline.remaining()
        if not self._buffer:
            with self._waiting():
                _ = wake.wait(self._sock, self._deadline.expires)

    def recv(self, wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT) -> RpcMessage | None:
        """Read one frame; None skips ignored deltas and non-JSON frames."""
        _ = self._deadline.remaining()
        self._fill(2, wake)
        opcode = self._buffer[0] & 0x0F
        length = self._buffer[1] & 0x7F
        offset = 2
        if length == 126:
            self._fill(4, wake)
            length = _unpack_len(">H", bytes(self._buffer[2:4]))
            offset = 4
        elif length == 127:
            self._fill(10, wake)
            length = _unpack_len(">Q", bytes(self._buffer[2:10]))
            offset = 10
        self._fill(offset + length, wake)
        payload = bytes(self._buffer[offset : offset + length])
        del self._buffer[:offset + length]
        _ = self._deadline.remaining()
        if opcode == 0x8:
            raise ConnectionError("app-server sent close")
        if opcode not in (0x1, 0x2):
            return None
        if _IGNORED_DELTA.match(payload):
            return None
        return _parse_frame(payload.decode(errors="replace"))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


@final
class Client:
    """A JSON-RPC conversation with the app-server over one websocket."""

    def __init__(self, port: int, name: str, deadline: float | None = None, *,
                 operation_deadline: float = NO_LOCK_DEADLINE) -> None:
        self.port: int = port
        self.name: str = name
        self.session_dir: str = ""
        self.launcher_pid: int = os.getpid()
        self.serving: CodexServingState = NotBoundCodexServer()
        self.turn_submitted_at: float = time.time()
        # Legacy wall-clock limits are converted exactly once at this boundary.
        self._client_limit: float = (NO_LOCK_DEADLINE if deadline is None else
                                     time.monotonic() + max(0.0, deadline - time.time()))
        self._operation_deadline: float = worker_lifecycle.effective_deadline(operation_deadline)
        self._ws: WebSocket = WebSocket(port, client_limit=self._client_limit,
                                        operation_deadline=self._operation_deadline)
        self._name: str = name
        self._counter: int = 0
        self._pending: list[RpcMessage] = []
        self.wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT
        try:
            # experimentalApi unlocks thread/queue/*.
            _ = self.call(
                "initialize",
                {"clientInfo": {"name": name, "version": "1.0"},
                 "capabilities": {"experimentalApi": True}},
            )
        except BaseException:
            self.close()
            raise

    def _call_deadline(self, timeout: float) -> TransportDeadline:
        return TransportDeadline(min(self._client_limit, time.monotonic() + timeout),
                                 worker_lifecycle.effective_deadline(self._operation_deadline))

    def call(
        self, method: str, params: dict[str, object], timeout: float = CALL_TIMEOUT_SECS
    ) -> RpcMessage:
        deadline = self._call_deadline(timeout)
        try:
            _ = deadline.remaining()
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except TimeoutError:
            return {"error": {"message": f"operation deadline reached before {method}"}}
        self._counter += 1
        request_id = f"{self._name}-{self._counter}"
        self._ws.set_deadline(deadline)
        self._ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        try:
            while True:
                _ = deadline.remaining()
                for index, message in enumerate(self._pending):
                    if message.get("id") == request_id:
                        return self._pending.pop(index)
                frame = self._read_until(deadline)
                _ = deadline.remaining()
                if frame is None:
                    # An early transport timeout keeps its ordinary RPC result.
                    break
                if frame.get("id") == request_id:
                    return frame
                self._pending.append(frame)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except TimeoutError:
            deadline.check_operation()
        deadline.check_operation()
        return {"error": {"message": f"timed out waiting for {method}"}}

    def _read_until(self, deadline: TransportDeadline,
                    wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT) -> RpcMessage | None:
        self._ws.set_deadline(deadline)
        try:
            while True:
                _ = deadline.remaining()
                frame = self._ws.recv(wake)
                _ = deadline.remaining()
                if frame is not None:
                    return frame
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except TimeoutError:
            deadline.check_operation()
            return None
        except SessionStateChanged:
            return None

    def _read(self, deadline: float, wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT) -> RpcMessage | None:
        """Legacy wall-clock frame boundary; assembly uses one monotonic limit."""
        return self._read_until(self._call_deadline(max(0.0, deadline - time.time())), wake)

    @property
    def has_pending_frames(self) -> bool:
        """Whether an RPC read buffered notices that the stream still must handle."""
        return bool(self._pending) or self._ws.has_pending_frames

    def next_frame(self, deadline: float) -> RpcMessage | None:
        """Next frame of any kind, or None once the legacy wall deadline passes."""
        if self._pending:
            TransportDeadline(NO_LOCK_DEADLINE,
                worker_lifecycle.effective_deadline(self._operation_deadline)).check_operation()
            return self._pending.pop(0)
        return self._read(deadline, self.wake)

    def push_back(self, frame: RpcMessage) -> None:
        """Return a frame to the head of the stream for the next reader."""
        self._pending.insert(0, frame)

    def wait_for_activity(self, wake: FollowEvents, deadline: float) -> None:
        """Wait for new input without consuming buffered notices."""
        budget = TransportDeadline(min(deadline, self._client_limit),
                                   worker_lifecycle.effective_deadline(self._operation_deadline))
        try:
            self._ws.wait_for_activity(wake, budget.local, operation_deadline=budget.operation)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except TimeoutError:
            return

    def close(self) -> None:
        self._ws.close()


def _require(message: RpcMessage, what: str) -> dict[str, object]:
    error = message.get("error")
    if error:
        raise SystemExit(f"codex_mesh: {what} failed: {json.dumps(error)[:300]}")
    return _as_dict(message.get("result"))


# ---------------------------------------------------------------------------
# Session state. The workers write the roster concurrently, so every
# read-modify-write takes an exclusive lock on its permanent sidecar.
# ---------------------------------------------------------------------------


def _session_path(session_dir: str, filename: str) -> Path:
    return Path(session_dir) / filename


def _read_json_object(path: Path) -> dict[str, object]:
    if path.name == ROSTER_FILE or path.name.endswith(".pending.json"):
        with worker_lifecycle.locked_state(path, mode=fcntl.LOCK_SH) as state:
            records = state.read_object()
        return worker_admission.resolve_roster(path.parent, records) if path.name == ROSTER_FILE else records
    return worker_lifecycle.read_worker_state(path)


def _post_board_status(session_dir: str, name: str, message: str) -> None:
    """Best-effort lifecycle narration under the worker's board slot."""
    try:
        record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    except worker_lifecycle.WorkerStateUnreadable as error:
        print(f"codex_mesh: board worker identity unavailable: {error}", file=sys.stderr)
        return
    slot = _as_str(session_staff.worker_name(record))
    if not slot:
        return
    message = f"{unit_workers.shown_name(Path(session_dir), name)}: {message}"
    with contextlib.suppress(OSError):
        _ = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "direct/board.sh"),
                            "post", session_dir, slot, "status", message], capture_output=True, text=True)


@dataclass(frozen=True)
class RosterRevision:
    value: int


def worker_roster_revision(value: object) -> int:
    return _roster_revision(value).value


def _roster_revision(value: object) -> RosterRevision:
    revision = _as_dict(value).get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        return RosterRevision(revision)
    return RosterRevision(0)


def _advance_roster_revision(entry: dict[str, object]) -> None:
    entry["revision"] = _roster_revision(entry).value + 1


def advance_worker_roster_revision(entry: dict[str, object]) -> None:
    """Fence a unit-ledger transaction with the same revision as mesh writers."""
    _advance_roster_revision(entry)


@dataclass(frozen=True)
class RosterOnThread:
    thread_id: str


@dataclass(frozen=True)
class RosterActiveLauncher:
    """Only the launcher still watching worker work may record its notices."""

    thread_id: str
    launcher_pids: tuple[int, ...]

    def owns(self, record: dict[str, object]) -> bool:
        return (session_staff.worker_session(record) == self.thread_id
                and record.get("status") in ("starting", "running", WAITING_CAPACITY)
                and record.get("launcher_pid") in self.launcher_pids)


@dataclass(frozen=True)
class RosterRefusingLauncher:
    """Only the active or just-settled launcher may hand its refusal off."""

    thread_id: str
    launcher_pids: tuple[int, ...]

    def owns(self, record: dict[str, object]) -> bool:
        return (RosterActiveLauncher(self.thread_id, self.launcher_pids).owns(record)
                or (session_staff.worker_session(record) == self.thread_id
                    and record.get("status") in ("failed", "capacity_exhausted")
                    and record.get("settled_launcher_pid") in self.launcher_pids))


@dataclass(frozen=True)
class RosterAbsent:
    pass


@dataclass(frozen=True)
class RosterEndedOnThread:
    thread_id: str


@dataclass(frozen=True)
class RosterUnchanged:
    thread_id: str
    revision: RosterRevision


@dataclass(frozen=True)
class RosterBackendReplaced:
    """Staff changed the backend while this roster revision remained unchanged."""

    staff_id: staff.StaffId
    thread_id: str
    revision: RosterRevision
    status: str


ExpectedRoster = RosterOnThread | RosterActiveLauncher | RosterRefusingLauncher | RosterAbsent | RosterEndedOnThread | RosterUnchanged | RosterBackendReplaced


@dataclass(frozen=True)
class RosterAdministrativeWrite:
    """Explicit low-level writes used by existing probes and fixture setup."""


ROSTER_ADMINISTRATIVE_WRITE = RosterAdministrativeWrite()


@dataclass(frozen=True)
class WorkerLaunchPolicy:
    """The worker uses the sandbox selected by its launch arguments."""


def roster_access_policy(record: Mapping[str, object]) -> ThreadAccessPolicy | WorkerLaunchPolicy:
    """Resolve explicit policies and reviewer records made before policies were stored."""
    if "access_policy" in record:
        policy = _as_dict(record["access_policy"])
        if policy.get("kind") == "ReadOnlyReviewer":
            return ReadOnlyReviewer()
        if policy.get("kind") == "PhaseBoundDesigner":
            return PhaseBoundDesigner(_as_int(policy.get("binding_generation")),
                                      _as_str(policy.get("launch_sandbox")) or WORKER_SANDBOX)
        if policy.get("kind") == "WorkerLaunchPolicy":
            return WorkerLaunchPolicy()
        raise SystemExit("codex_mesh: invalid access policy; use design_roles.py recover for a held role")
    if record.get("held_design_id"):
        raise SystemExit("codex_mesh: held role has no access policy; use design_roles.py recover")
    if record.get("instructions_file"):
        return ReadOnlyReviewer()
    return WorkerLaunchPolicy()


def _store_reviewer_policy(entry: dict[str, object], *, new_reviewer: bool = False) -> dict[str, object]:
    if "access_policy" not in entry and not entry.get("held_design_id"):
        if new_reviewer or isinstance(roster_access_policy(entry), ReadOnlyReviewer):
            entry["access_policy"] = {"kind": "ReadOnlyReviewer"}
    return entry


def _load_roster_for_write(handle: worker_lifecycle.WorkerStateFile) -> dict[str, object]:
    return worker_admission.resolve_roster(handle.path.parent, handle.read_object())


def _write_roster_record(handle: worker_lifecycle.WorkerStateFile, roster: dict[str, object], name: str,
                        record: ThreadRecord, *, backend_replaced: bool = False) -> None:
    previous = _as_dict(roster.get(name))
    stored = dict(record)
    if isinstance(previous.get("staff_id"), str):
        stored["staff_id"] = previous["staff_id"]
    for key in ("assignment_id", "dispatch_state"):
        if key in previous:
            stored[key] = previous[key]
    if session_staff.worker_session(previous) == record["thread_id"]:
        for key in ("held_design_id", "held_role", "access_policy", "model", "effort",
                    "turn_model", "turn_effort",
                    "unit_ledger"):
            if key in previous and key not in stored:
                stored[key] = previous[key]
    if "continuation_owed" not in record and session_staff.worker_session(previous) == record["thread_id"]:
        owed = previous.get("continuation_owed")
        if isinstance(owed, bool):
            stored["continuation_owed"] = owed
    size = _context_size_from_record(record if "context_tokens" in record else previous)
    task = record.get("task", previous.get("task"))
    if isinstance(task, str) and task:
        stored["task"] = task
    instructions_file = record.get("instructions_file")
    if instructions_file is None and session_staff.worker_session(previous) == record["thread_id"]:
        instructions_file = previous.get("instructions_file")
    if isinstance(instructions_file, str) and instructions_file:
        stored["instructions_file"] = instructions_file
    if record["status"] in ("starting", "running", WAITING_CAPACITY):
        pid = record.get("launcher_pid", previous.get("launcher_pid"))
        if isinstance(pid, int) and pid > 0:
            stored["launcher_pid"] = pid
        port = record.get("port", previous.get("port"))
        if isinstance(port, int) and port > 0:
            stored["port"] = port
    if (
        not backend_replaced and session_staff.worker_session(previous) == record["thread_id"]
        and (
            (previous.get("status") == "ended" and record["status"] != "ended")
            or (previous.get("status") == "paused" and record["status"] not in ("ended", "paused"))
        )
    ):
        stored = dict(previous)
    if isinstance(size, KnownContextSize):
        stored["context_tokens"] = size.tokens
    elif isinstance(size, NotMeasuredSinceCompaction):
        stored["context_tokens"] = "not_measured_since_compaction"
    stored["revision"] = _roster_revision(previous).value + 1
    roster[name] = _store_reviewer_policy(
        stored, new_reviewer=session_staff.worker_session(previous) != record["thread_id"] and record.get("role") == "review")
    session_staff.publish_worker_roster(handle, roster)


def _update_roster(session_dir: str, name: str, record: ThreadRecord, *,
                   expected: ExpectedRoster | RosterAdministrativeWrite = ROSTER_ADMINISTRATIVE_WRITE) -> bool:
    """One lock scope for guarded production writes and existing administrative callers."""
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with worker_lifecycle.locked_state(path) as handle:
        roster = _load_roster_for_write(handle)
        previous = _as_dict(roster.get(name))
        if isinstance(expected, RosterAdministrativeWrite):
            matches = True
        elif isinstance(expected, RosterAbsent):
            matches = name not in roster
        elif isinstance(expected, RosterBackendReplaced):
            matches = (previous.get("staff_id") == expected.staff_id
                       and session_staff.worker_session(previous) == expected.thread_id
                       and _roster_revision(previous) == expected.revision
                       and _as_str(previous.get("status")) == expected.status)
        else:
            matches = session_staff.worker_session(previous) == expected.thread_id
            if isinstance(expected, RosterEndedOnThread):
                matches = matches and previous.get("status") == "ended"
            elif isinstance(expected, RosterUnchanged):
                matches = matches and _roster_revision(previous) == expected.revision
            elif isinstance(expected, (RosterActiveLauncher, RosterRefusingLauncher)):
                matches = expected.owns(previous)
        if not matches:
            return False
        _write_roster_record(handle, roster, name, record,
                             backend_replaced=isinstance(expected, RosterBackendReplaced))
        return True


def _update_roster_if_expected(session_dir: str, name: str, record: ThreadRecord,
                              expected: ExpectedRoster) -> bool:
    """Compare the name's owner and write while holding the same roster lock."""
    return _update_roster(session_dir, name, record, expected=expected)


def _set_roster_assignment(session_dir: str, name: str, role: WorkerRole, task: str,
                           expected: RosterActiveLauncher) -> None:
    """Record the work assigned to an existing worker without changing its state."""
    with worker_lifecycle.lock_worker(session_dir, name):
        path = _session_path(session_dir, ROSTER_FILE)
        with worker_lifecycle.locked_state(path) as handle:
            roster = _load_roster_for_write(handle)
            entry = _as_dict(roster.get(name))
            if expected.owns(entry):
                if entry.get("unit_ledger") and not entry.get("held_design_id") and not entry.get("instructions_file"):
                    entry["access_policy"] = {"kind": "ReadOnlyReviewer" if role == "review" else "WorkerLaunchPolicy"}
                if task:
                    entry["task"] = task
                _advance_roster_revision(entry)
                roster[name] = entry
                session_staff.publish_worker_roster(handle, roster)


def _mark_all_roster_ended(session_dir: str, roster: dict[str, object], *, deadline: float) -> None:
    """End the validated work set."""
    for name, value in roster.items():
        entry = _as_dict(value)
        if entry:
            entry["status"] = "ended"
            _advance_roster_revision(entry)
            roster[name] = entry
    _publish_bulk_records(session_dir, roster, deadline=deadline)


def _lookup(session_dir: str, name: str) -> ThreadRecord:
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    entry = roster.get(name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(roster)) or "(none)"
        raise SystemExit(f"codex_mesh: no worker named '{name}'. Known: {known}")
    return cast("ThreadRecord", cast("object", entry))


def _lookup_state(session_dir: str, name: str) -> RosterThread:
    return _roster_thread(_lookup(session_dir, name))


def _worker_sandbox(args: argparse.Namespace, session_dir: str, name: str) -> str:
    """Select the sandbox from the worker's resolved access policy."""
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    policy = roster_access_policy(record)
    if isinstance(policy, ReadOnlyReviewer):
        return READ_ONLY_SANDBOX
    if isinstance(policy, PhaseBoundDesigner):
        return policy.launch_sandbox
    return _as_str(_attr(args, "sandbox")) or WORKER_SANDBOX


@contextlib.contextmanager
def _design_claim_lock(session_dir: str, name: str, *, lock_inbox: bool = True) -> Generator[None]:
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    design_id = _as_str(record.get("held_design_id"))
    if not design_id:
        with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, name):
            yield
        return
    from scripts.astra import design_roles
    directory = design_roles.design_directory(design_id)
    with (directory / "lock").open("a", encoding="utf-8") as design_lock:
        worker_lifecycle.acquire_flock(design_lock, mode=fcntl.LOCK_EX)
        with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, name):
            with _pending_file(session_dir, name) if lock_inbox else contextlib.nullcontext():
                yield


def _validate_held_claim(name: str, entry: dict[str, object], thread_id: str) -> bool:
    design_id = _as_str(entry.get("held_design_id"))
    if not design_id:
        return True
    from scripts.astra import design_roles
    design = design_roles.read_design(design_id)
    role = next((role for role in design.roles if role.name == name), None)
    generation = design.placement.generation
    if (role is None or isinstance(role.lifecycle, (design_roles.RoleNotOpened, design_roles.RoleEnded, design_roles.RoleNeedsRecovery))
            or role.lifecycle.thread_id != thread_id
            or entry.get("authorized_generation") != generation
            or not entry.get("authorized_prompt")
            or role.assignment_claim != entry.get("authorized_prompt")
            or (design_roles.design_directory(design_id) / "transition.json").exists()):
        return False
    entry["instructions_file"] = entry["authorized_prompt"]
    entry["access_policy"] = {"kind": "ReadOnlyReviewer"} if role.role == "reviewer" else {
        "kind": "PhaseBoundDesigner", "binding_generation": generation, "launch_sandbox": WORKER_SANDBOX}
    return True


def _turn_sandbox_policy(mode: str) -> dict[str, object]:
    """The turn-level SandboxPolicy object for a SandboxMode string."""
    if mode == READ_ONLY_SANDBOX:
        return {"type": "readOnly", "networkAccess": False}
    return {"type": "dangerFullAccess"}


def _resume_params(session_dir: str, name: str, thread_id: str) -> dict[str, object]:
    """Restore a roster thread's standing instructions whenever it is reopened."""
    params: dict[str, object] = {"threadId": thread_id,
        **({"cwd": cwd} if (cwd := _as_str(plan_history.staff_directory(
            {"owner_session": thread_id}, staff.snapshot()).get("working_dir"))) else {})}
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    policy = roster_access_policy(record)
    if isinstance(policy, (ReadOnlyReviewer, PhaseBoundDesigner)):
        params["sandbox"] = READ_ONLY_SANDBOX if isinstance(policy, ReadOnlyReviewer) else policy.launch_sandbox
        params["approvalPolicy"] = "never"
    instructions_file = _as_str(record.get("instructions_file"))
    if instructions_file:
        try:
            params["developerInstructions"] = Path(instructions_file).read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise OSError(f"cannot read instructions file {instructions_file}: {exc}") from exc
    return params


def _roster_still_on_thread(session_dir: str, name: str, thread_id: str) -> bool:
    entry = _read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)
    state = _roster_thread(entry)
    return not isinstance(state, InvalidThread) and state.thread_id == thread_id


def _claim_follow(
    session_dir: str, name: str, thread_id: str, launcher_pid: int, role: WorkerRole,
    lens: agents_log.Lens | None = None,
) -> bool:
    """Take a finished roster entry before another follow can take it."""
    with _design_claim_lock(session_dir, name):
        observed = _lookup_state(session_dir, name)
        # Only abandoned live statuses need recovery evidence at claim time.
        # A normal claim reserves the entry; its caller checks server requests
        # with the command's own error and idle-state semantics before submission.
        if _launcher_died(observed) and _follow_refusal(session_dir, observed, name=name):
            return False
        return _claim_follow_locked(session_dir, name, thread_id, launcher_pid, role, lens)


def _claim_follow_locked(
    session_dir: str, name: str, thread_id: str, launcher_pid: int, role: WorkerRole,
    lens: agents_log.Lens | None = None,
) -> bool:
    """Publish a claim after the operation proved eligibility under its lifecycle lock."""
    path = _session_path(session_dir, ROSTER_FILE)
    with worker_lifecycle.locked_state(path) as handle:
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if entry.get("adopted_to"):
            return False
        if not _validate_held_claim(name, entry, thread_id):
            return False
        current = _roster_thread(entry)
        stale = _launcher_died(current)
        if isinstance(current, InvalidThread) or not (
            isinstance(current, FollowableThread) or stale
        ) or current.thread_id != thread_id:
            return False
        if entry.get("held_design_id") and stale:
            return False
        previous_status = current.status if isinstance(current, FollowableThread) else "failed"
        task: agents_log.Assignment | agents_log.Unassigned = agents_log.Unassigned()
        usage_available = True
        try:
            history = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
            task = agents_log.role_at(history, thread_id, datetime.now(timezone.utc))
        except Exception as exc:
            usage_available = False
            print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)
        previous_role: RosterRole = RolelessThread()
        if isinstance(task, agents_log.Assignment):
            previous_role = _roster_role(task.role)
        claimed = dict(entry)
        _ = claimed.pop("settled_launcher_pid", None)
        unit_workers.clear_allocation(claimed)
        _ = claimed.pop("turn_id", None)
        _ = claimed.pop("paused_from", None)
        _ = claimed.pop("paused_turn", None)
        _ = claimed.pop("previous_task_role", None)
        _ = claimed.pop("previous_task_lens", None)
        if isinstance(previous_role, AssignedWorkerRole):
            claimed["previous_task_role"] = previous_role.role
            claimed["previous_task_lens"] = task.lens if isinstance(task, agents_log.Assignment) else None
        if stale:
            claimed["dead_launcher_recovery"] = True
        claimed.update({
            "thread_id": thread_id,
            "status": "starting",
            "launcher_pid": launcher_pid,
            "previous_status": previous_status,
            "role": role,
        })
        _ = claimed.pop("authorized_prompt", None)
        _advance_roster_revision(claimed)
        roster[name] = claimed
        session_staff.publish_worker_roster(handle, roster)
        if usage_available:
            _record_assignment(thread_id, role, lens)
        return True


def _restore_follow_claim(session_dir: str, name: str, launcher_pid: int,
                          live_turn: str = "", *, finished: bool = False,
                          expected_thread_id: str = "") -> None:
    """Release only this launcher's unstarted claim; retain a peer's live turn."""
    with worker_lifecycle.lock_worker(session_dir, name):
        path = _session_path(session_dir, ROSTER_FILE)
        with worker_lifecycle.locked_state(path) as handle:
            roster = _load_roster_for_write(handle)
            entry = _as_dict(roster.get(name))
            status = entry.get("status")
            if expected_thread_id and session_staff.worker_session(entry) != expected_thread_id:
                return
            if entry.get("launcher_pid") != launcher_pid or not (
                status == "starting" or (finished and status == "running")
            ):
                return
            thread_id = _as_str(session_staff.worker_session(entry))
            previous_status = entry.get("previous_status")
            previous_role = _roster_role(entry.get("previous_task_role"))
            previous_lens = entry.get("previous_task_lens")
            lens = previous_lens if previous_lens in agents_log.LENSES else None
            restored = dict(entry)
            _advance_roster_revision(restored)
            if live_turn and not finished:
                restored.update({
                    "thread_id": thread_id,
                    "status": "running",
                    "turn_id": live_turn,
                    "launcher_pid": launcher_pid,
                })
                roster[name] = restored
            else:
                _ = restored.pop("previous_status", None)
                _ = restored.pop("previous_task_role", None)
                _ = restored.pop("previous_task_lens", None)
                _ = restored.pop("turn_id", None)
                _ = restored.pop("launcher_pid", None)
                _ = restored.pop("port", None)
                restored.update({
                    "thread_id": thread_id,
                    "status": previous_status if previous_status in FOLLOWABLE_STATES else "failed",
                })
                roster[name] = restored
            session_staff.publish_worker_roster(handle, roster)
            if isinstance(previous_role, AssignedWorkerRole):
                _record_assignment(thread_id, previous_role.role, lens, only_if_changed=True)


def _wait_for_claimed_peer(client: Client, thread_id: str, turn_id: str,
                           deadline: float) -> bool:
    """Keep the claimed peer turn active until it finishes or the wait expires."""
    expires = time.monotonic() + max(0.0, deadline - time.time())
    while time.monotonic() < expires:
        frame = client.next_frame(time.time() + min(
            UNSEEN_END_CHECK_SECS, max(0.0, expires - time.monotonic())))
        if frame is not None and frame.get("method") in ("turn/completed", "turn/failed"):
            params = _as_dict(frame.get("params"))
            notice_id = _as_str(_as_dict(params.get("turn")).get("id"))
            if _as_str(params.get("threadId")) in ("", thread_id) and notice_id == turn_id:
                return True
        if client.has_pending_frames:
            continue
        live = _read_live_turn(client, thread_id)
        if isinstance(live, ThreadIdle):
            return True
    return False


def _signal_process(pid: object, signum: int, *, group: bool = False) -> None:
    """Never let coerced IDs select init, a process group or all processes."""
    if not worker_lifecycle.is_process_pid(pid):
        raise ProcessLookupError(errno.ESRCH, "refusing unsafe process ID")
    try:
        if group:
            process_group.send_process_group(pid, signum)
        else:
            os.kill(pid, signum)
    except OverflowError as error:
        raise ProcessLookupError(errno.ESRCH, "process ID is outside the OS range") from error


def _pid_alive(pid: int) -> bool:
    """A recorded pid is live only if the OS can represent and find it."""
    if pid <= 0:
        return False
    try:
        _signal_process(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except (OSError, UnicodeError):
        return True
    return state[:1] != ["Z"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return _bound_port(probe)


def _recorded_server_availability(record: Mapping[str, object]) -> ServerAvailability:
    """Only a valid endpoint and live process identify a recorded server."""
    port, pid = record.get("port"), record.get("pid")
    if (type(port) is int and 0 < port <= 65535
            and type(pid) is int and pid > 0 and _pid_alive(pid)):
        return LiveServer(port)
    return ServerRestartRequired()


def _server_availability(session_dir: str) -> ServerAvailability:
    """The running app-server or the need to restart it.

    `ensure_server` would start one; the verbs that only ever wind things down
    must not, or ending a worker whose server already died would leave a
    fresh server behind for nothing.
    """
    try:
        return _recorded_server_availability(_read_json_object(_session_path(session_dir, SERVER_FILE)))
    except worker_lifecycle.WorkerStateUnreadable as error:
        return ServerRestartRequired(str(error))


def _worker_port(session_dir: str, record: ThreadRecord) -> int:
    """The server that reaches a worker: the one its launcher is attached to while that launcher
    lives, which after a sign-in change can be one the run has retired; otherwise the run's own.

    A worker whose launcher lives is never given a new server. Its conversation stays open on the
    server it is on, and a second server cannot open it while that one holds it.
    """
    port, pid = record.get("port"), record.get("launcher_pid")
    if not (isinstance(pid, int) and pid > 0 and _pid_alive(pid)):
        return ensure_server(session_dir)[0]
    if isinstance(port, int):
        return port
    # A launcher that has not recorded its server yet, or one started before workers recorded it.
    thread_id = session_staff.worker_session(record)
    servers = _run_servers(session_dir)
    for candidate in servers:
        if _server_holds(candidate, thread_id):
            return candidate
    current = _record_port(session_dir)
    if current in servers:
        return current
    raise SystemExit(f"codex_mesh: thread {thread_id}'s launcher {pid} is running, but no running app-server "
                     + "of this run holds it; nothing was started and the message was not delivered")


def _recorded_run_servers(session_dir: str, *, retained_only: bool = False) -> list[dict[str, object]]:
    """Read retained servers and the current server from the same record."""
    records: list[dict[str, object]] = []
    current = _read_json_object(_session_path(session_dir, SERVER_FILE))
    records.extend(_as_dict(record) for record in cast(list[object], current.get("previous_servers", [])))
    if not retained_only and "pid" in current:
        records.append({key: value for key, value in current.items() if key != "previous_servers"})
    unique: dict[tuple[object, object], dict[str, object]] = {}
    for record in records:
        unique[(record.get("pid"), record.get("port"))] = record
    return list(unique.values())


def recorded_run_ports(session_dir: str) -> tuple[int, ...]:
    return tuple(_run_servers(session_dir))


def run_server_records(run: Path) -> list[dict[str, object]]:
    return _recorded_run_servers(str(run))


def read_worker_thread_state(client: Client, thread_id: str) -> ThreadState:
    return _read_live_turn(client, thread_id)


def _run_servers(session_dir: str) -> list[int]:
    """Ports of the run's servers still running: those it retired, oldest first, then its current one."""
    ports: list[int] = []
    try:
        records = _recorded_run_servers(session_dir)
    except worker_lifecycle.WorkerStateUnreadable as error:
        print(f"codex_mesh: server availability unknown: {error}; kept", file=sys.stderr)
        return []
    for record in records:
        server = _recorded_server_availability(record)
        if isinstance(server, LiveServer) and server.port not in ports:
            ports.append(server.port)
    return ports


from scripts.herdr.herdr import loaded_thread_ids as _loaded_thread_ids


from scripts.herdr.herdr import loaded_threads as loaded_threads


def _server_holds(port: int, thread_id: str) -> bool:
    try:
        client = Client(port, f"worker-lookup-{os.getpid()}")
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit):
        return False
    try:
        return thread_id in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit):
        return False
    finally:
        client.close()


def _note_worker_port(session_dir: str, name: str, launcher_pid: int, port: int) -> None:
    """Record the server a follow-up launcher attached to, while its claim is still its own."""
    path = _session_path(session_dir, ROSTER_FILE)
    with worker_lifecycle.locked_state(path) as handle:
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if entry.get("launcher_pid") != launcher_pid:
            return
        entry["port"] = port
        _advance_roster_revision(entry)
        roster[name] = entry
        session_staff.publish_worker_roster(handle, roster)


def _record_port(session_dir: str) -> int | None:
    port = _read_json_object(_session_path(session_dir, SERVER_FILE)).get("port")
    return port if isinstance(port, int) else None


def _end_marker_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.end")


ACCOUNT_MOVE_CONTINUATION = (
    "Your turn was interrupted to move your Codex account; any command you had running "
    "was stopped with it, so run it again. Continue where you stopped."
)
ACCOUNT_MOVE_RESUME_TIMEOUT_SECS = 30.0


class StoredAccountMove(TypedDict):
    thread_id: str
    turn_id: str
    label: str
    move_id: str


@dataclass(frozen=True)
class AccountMoveMarker:
    thread_id: str
    turn_id: str
    label: str
    move_id: str


@dataclass(frozen=True)
class NoAccountMove:
    """This thread has no pending account-move interrupt."""


def _account_move_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.account-move.json")


def _account_move_result_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.account-move-result.json")


def _write_account_move_json(path: Path, value: Mapping[str, object]) -> None:
    previous = worker_lifecycle.read_worker_state(path)
    value = dict(value)
    if path.name.endswith(".account-move.json"):
        native = value.pop("thread_id", None)
        member = staff.local_snapshot().by_current_session(native) if isinstance(native, str) else None
        if member is None or member.kind != "worker":
            print(f"anomaly: {path}: account move has no worker staff row", file=sys.stderr)
            return
        value["staff_id"] = member.id
    if path.name == SERVER_FILE:
        servers = list(cast(list[object], previous.get("previous_servers", [])))
        if "previous_servers" not in value and servers:
            value["previous_servers"] = servers
    _ = worker_lifecycle.validate_worker_state(path, dict(value))
    write_text_atomic(path, json.dumps(value))


def _read_account_move(session_dir: str, name: str, thread_id: str) -> AccountMoveMarker | NoAccountMove:
    record = _read_json_object(_account_move_path(session_dir, name))
    thread = session_staff.worker_session(record)
    turn, label, move_id = (record.get(key) for key in ("turn_id", "label", "move_id"))
    if (thread == thread_id and thread
            and isinstance(turn, str) and turn and isinstance(label, str) and label
            and isinstance(move_id, str) and move_id):
        return AccountMoveMarker(thread, turn, label, move_id)
    return NoAccountMove()


def _finish_account_move(session_dir: str, name: str, marker: AccountMoveMarker) -> None:
    current = _read_account_move(session_dir, name, marker.thread_id)
    if isinstance(current, AccountMoveMarker) and current.move_id == marker.move_id:
        _account_move_path(session_dir, name).unlink(missing_ok=True)
    _write_account_move_json(_account_move_result_path(session_dir, name),
                            {"move_id": marker.move_id, "resumed": True})
    _post_board_status(session_dir, name, f"account moved to {marker.label}; resumed")


def _fail_account_move(session_dir: str, name: str, marker: AccountMoveMarker, reason: str) -> None:
    _write_account_move_json(_account_move_result_path(session_dir, name),
                            {"move_id": marker.move_id, "error": reason})




@contextlib.contextmanager
def _pending_file(session_dir: str, name: str, *, blocking: bool = True,
                  deadline: float = NO_LOCK_DEADLINE, reference: str = "") -> Generator[worker_lifecycle.WorkerStateFile]:
    path = (session_staff.inbox_path(staff.native_id(reference)) if reference
            else session_staff.worker_inbox_path(session_dir, name))
    mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    with worker_lifecycle.locked_state(path, mode=mode, deadline=deadline) as state:
        yield state


def _pending_messages(handle: worker_lifecycle.WorkerStateFile) -> list[str]:
    return cast(list[str], handle.read_object().get("messages", []))


def _messages_closed(handle: worker_lifecycle.WorkerStateFile) -> bool:
    return handle.read_object().get("accepting_messages") is False


def _write_pending(handle: worker_lifecycle.WorkerStateFile, messages: list[str], *, accepting_messages: bool = True) -> None:
    content = {**handle.read_object(), "messages": messages, "accepting_messages": accepting_messages}
    if handle.read_object() != content:
        handle.publish(json.dumps(content))


@dataclass(frozen=True)
class FreshDetachedRunScan:
    """A command outside a follower requests a new, uncached run observation."""


FRESH_DETACHED_RUN_SCAN = FreshDetachedRunScan()


def _close_messages_if_empty(
    session_dir: str, name: str, launcher: RosterActiveLauncher,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> bool:
    """Exclude senders while claiming an empty inbox and closing its launcher."""
    with worker_lifecycle.lock_worker(session_dir, name):
        entry = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if not launcher.owns(entry):
            return True
        with _pending_file(session_dir, name) as handle:
            current = _run_watch(session_dir, name, runs).current()
            if _pending_messages(handle) or current.live or current.dead:
                return False
            _write_pending(handle, [], accepting_messages=False)
            return True


def _close_messages_and_keep_pending(session_dir: str, name: str, launcher: RosterActiveLauncher) -> int:
    """Close the launcher's inbox while keeping its parked messages for the next launch."""
    with worker_lifecycle.lock_worker(session_dir, name):
        entry = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if not launcher.owns(entry):
            return 0
        with _pending_file(session_dir, name) as handle:
            messages = _pending_messages(handle)
            _write_pending(handle, messages, accepting_messages=False)
            return len(messages)


def _detached_run_live(
    session_dir: str, name: str,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> bool:
    """Refresh the watch before reporting whether a supervisor is live."""
    return _run_watch(session_dir, name, runs).current().live


def _end_dead_runs(
    session_dir: str, name: str,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> None:
    """Reap dead supervisors outside the inbox lock, sharing the follower's one scan."""
    watch = _run_watch(session_dir, name, runs)
    watch.refresh()
    watch.reap()


@dataclass(frozen=True)
class LastObservedDetachedRuns:
    """A safety scan's liveness, which may be stale until the next scan."""

    live: bool = False
    dead: bool = False


@dataclass(frozen=True)
class CurrentDetachedRuns:
    """Liveness scanned for the lifecycle decision the caller is making now."""

    live: bool
    dead: bool


def worker_run_history(session_dir: str, name: str) -> tuple[str, ...]:
    """Return the runs that contain the worker's staff identity."""
    roster = worker_lifecycle.read_worker_state(_session_path(session_dir, ROSTER_FILE))
    entry = _as_dict(roster.get(name))
    reference = _as_str(entry.get("staff_id"))
    runs = [session_dir]
    ledger_path = _as_str(entry.get("unit_ledger"))
    if reference and ledger_path:
        ledger = worker_lifecycle.read_worker_state(Path(ledger_path))
        worker = _as_dict(_as_dict(ledger.get("workers")).get(reference))
        for events in (worker.get("events"), _as_dict(ledger.get("history")).get(reference)):
            if isinstance(events, list):
                for event in cast(list[object], events):
                    run = _as_str(_as_dict(event).get("run"))
                    if run:
                        runs.append(run)
    source = _as_str(entry.get("adopted_from"))
    if source:
        runs.append(source)
    return tuple(dict.fromkeys(runs))


@dataclass
class DetachedRunWatch:
    """One launcher's run liveness, retaining immutable finished record names."""

    session_dir: str
    name: str
    finished: set[str] = field(default_factory=set)
    next_check: float = 0.0
    last_observed: LastObservedDetachedRuns = field(default_factory=LastObservedDetachedRuns)

    def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self.next_check:
            return
        self.next_check = now + DETACHED_RUN_CHECK_SECS
        live = dead = False
        wall_now = time.time()
        paths = (path for run in worker_run_history(self.session_dir, self.name)
                 for path in _session_path(run, DETACHED_RUNS_DIR).glob("*.json"))
        for path in paths:
            if str(path) in self.finished:
                continue
            record = _read_json_object(path)
            if record.get("status") == "finished":
                self.finished.add(str(path))
                continue
            if record.get("worker") != self.name or record.get("status") != "running":
                continue
            pid = _as_int(record.get("pid"))
            alive = (_pid_alive(pid) if pid > 0 else
                     wall_now - _as_float(record.get("started_at"), 0.0) < DETACHED_RUN_START_GRACE_SECS)
            live |= alive
            dead |= not alive
        self.last_observed = LastObservedDetachedRuns(live, dead)

    def current(self) -> CurrentDetachedRuns:
        """Force discovery before a launcher closes or classifies a turn."""
        self.refresh(force=True)
        return CurrentDetachedRuns(self.last_observed.live, self.last_observed.dead)

    def reap(self) -> None:
        if not self.last_observed.dead:
            return
        self.last_observed = LastObservedDetachedRuns(self.last_observed.live, False)
        try:
            _ = subprocess.run([sys.executable, str(MESH_RUNS_SCRIPT), "reap", "--session-dir", self.session_dir,
                                "--worker", self.name], stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=DEAD_RUN_END_TIMEOUT_SECS, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def _run_watch(session_dir: str, name: str, runs: DetachedRunWatch | FreshDetachedRunScan) -> DetachedRunWatch:
    return runs if isinstance(runs, DetachedRunWatch) else DetachedRunWatch(session_dir, name)


def _waiting_on_detached_run(
    session_dir: str, name: str,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> bool:
    """A live run with nothing held yet: the launcher stays attached and makes no calls."""
    with _pending_file(session_dir, name) as handle:
        held = bool(_pending_messages(handle))
    watch = _run_watch(session_dir, name, runs)
    live = _detached_run_live(session_dir, name, watch)
    return not held and (live or watch.last_observed.dead)


@dataclass(frozen=True)
class HeldForLauncher:
    """The worker's launcher is attached and opens the next turn from this message."""

    thread_id: str


@dataclass(frozen=True)
class HeldForNextDispatch:
    """No launcher is attached; the message waits for the worker's next dispatch."""

    thread_id: str


@dataclass(frozen=True)
class NoLauncherAttached:
    """No launcher is attached and the caller asked not to hold; nothing was written."""

    thread_id: str
    status: str


@dataclass(frozen=True)
class WorkerEnded:
    """The worker is ended or gone from the roster; nothing was written."""

    reason: str


@dataclass(frozen=True)
class RepeatHeld:
    copies: int


HoldOutcome = HeldForLauncher | HeldForNextDispatch | NoLauncherAttached | WorkerEnded | RepeatHeld


def hold_message(session_dir: str, name: str, text: str, *, without_launcher: bool,
                 sender: str = "codex-mesh", human: bool = False, repeat_checked: bool = False,
                 guard_text: str = "") -> HoldOutcome:
    return _hold_owned_message(session_dir, name, text, without_launcher=without_launcher,
        sender=sender, human=human, repeat_checked=repeat_checked, guard_text=guard_text)


def _hold_owned_message(session_dir: str, name: str, text: str, *, without_launcher: bool,
                        sender: str, human: bool, repeat_checked: bool, guard_text: str) -> HoldOutcome:
    """Add one message to a worker's held messages for the thread the roster names now.

    The roster is read under the pending lock, so a run that finished after an account move or a
    server change reaches the thread the worker is on at this moment. The attached check and the
    write share that lock: a launcher closing its inbox either sees the message or closed first,
    and then this call reports it not attached. The stored accepting flag is kept, so a closing
    launcher never refuses the message. With `without_launcher` false, a message no launcher
    would read is not written.
    """
    with _pending_file(session_dir, name) as handle:
        if _end_marker_path(session_dir, name).exists():
            return WorkerEnded(f"{name} is being ended")
        entry = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        thread_id = _as_str(session_staff.worker_session(entry))
        status = _as_str(entry.get("status"))
        if not entry or not thread_id:
            return WorkerEnded(f"{name} is not on the roster")
        if status == "ended":
            return WorkerEnded(f"{name} is ended")
        closed = _messages_closed(handle)
        launcher_pid = _as_int(entry.get("launcher_pid"))
        attached = status in LAUNCHER_ATTACHED_STATUSES and _pid_alive(launcher_pid) and not closed
        if not attached and not without_launcher:
            return NoLauncherAttached(thread_id, status)
        receiver = repeat_guard.recipient_identity("", thread_id=thread_id)
        if not repeat_checked:
            decision = repeat_guard.transport_check(sender, receiver, guard_text or text, human=human)
            if decision.held:
                return RepeatHeld(decision.copies)
        messages = _pending_messages(handle)
        messages.append(text)
        _write_pending(handle, messages, accepting_messages=not closed)
        if not repeat_checked:
            repeat_guard.transport_result(sender, receiver, guard_text or text, "queued", human=human)
        return HeldForLauncher(thread_id) if attached else HeldForNextDispatch(thread_id)


def _add_pending(session_dir: str, name: str, message: str, submission: str = "") -> None:
    with _pending_file(session_dir, name) as handle:
        _park_submission(handle, message, submission)


def _park_submission(handle: worker_lifecycle.WorkerStateFile, message: str, submission: str) -> None:
    content = handle.read_object()
    preserved = cast(list[str], content.get("preserved_submissions", []))
    if submission and submission in preserved:
        return
    content.update(messages=[*_pending_messages(handle), message],
                   preserved_submissions=[*preserved, submission] if submission else preserved)
    handle.publish(json.dumps(content))


def _move_queue_to_pending(client: Client, thread_id: str, park: Callable[[str, str], None]) -> None:
    """Park queued text locally before an idle server opens another turn. Each message is parked
    before it is deleted from the server's queue."""
    for _ in range(END_QUEUE_DRAIN_ROUNDS):
        listed = _require(client.call("thread/queue/list", {"threadId": thread_id}), "thread/queue/list")
        items = listed.get("data")
        if not isinstance(items, list) or not items:
            return
        for item in cast("list[object]", items):
            queued = _as_dict(item)
            submission = _as_str(queued.get("id"))
            inputs = queued.get("input")
            if not submission or not isinstance(inputs, list):
                continue
            messages = [
                _as_str(_as_dict(part).get("text")) for part in cast("list[object]", inputs)
                if _as_str(_as_dict(part).get("type")) == "text"
            ]
            if messages:
                park("\n".join(messages), f"{thread_id}/{submission}")
            _ = _require(client.call("thread/queue/delete", {
                "threadId": thread_id, "queuedSubmissionId": submission
            }), "thread/queue/delete")
    raise RuntimeError("server queue did not drain; preservation unconfirmed")


def _message_text(args: argparse.Namespace) -> str:
    """The message for send/steer: --message-file wins, so a multi-line body
    never has to survive shell quoting."""
    path = _as_str(_attr(args, "message_file"))
    if path:
        return Path(path).read_text(encoding="utf-8")
    return _as_str(_attr(args, "message"))


def _record_reply(reply_path: Path, name: str, index: int, body: str) -> None:
    """Append one reply to the reply file, framed the way stdout frames it."""
    with reply_path.open("a", encoding="utf-8") as replies:
        _ = replies.write(f"=== reply from {name} ({index}) ===\n{body}\n=== end reply ===\n")


def _deliver_reply(
    name: str,
    index: int,
    text: str,
    failure: str,
    summary_path: Path,
    reply_path: Path | None,
    log: TextIO,
) -> None:
    """Hand one finished resident turn to whoever is watching.

    stdout is for a caller polling the terminal this runs in. The reply file,
    when the caller named one, keeps every reply in order and leaves the
    summary file to the worker, which writes it as its own last act; without
    one, the summary file receives the reply. The log line records that it
    landed.
    """
    if text and failure:
        body = f"{text}\n[error] {failure}"
    elif text:
        body = text
    elif failure:
        body = f"[error] {failure}"
    else:
        body = f"The worker {name} produced no reply."
    if reply_path is None:
        _ = summary_path.write_text(body + "\n", encoding="utf-8")
    else:
        _record_reply(reply_path, name, index, body)
    print(f"=== reply from {name} ({index}) ===\n{body}\n=== end reply ===", flush=True)
    _ = log.write(f"[{_now_stamp()}] reply {index} delivered\n")
    log.flush()


def _finish_summary(
    summary_path: Path, reply_path: Path | None, name: str, index: int, final_answer: str
) -> None:
    """Leave the worker's answer where the caller will read it.

    With no reply file the summary file receives the answer. With one, the
    answer goes there, and fills the summary only when it is empty. Launchers
    truncate the summary at launch. Read-only reviewers return their entire
    result in the reply; writable workers may instead write their own summary.
    A non-empty worker summary must not be replaced by the last chat reply.
    """
    if reply_path is None:
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")
        return
    _record_reply(reply_path, name, index, final_answer)
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")


@contextlib.contextmanager
def _server_lock(session_dir: str, *, deadline: float = NO_LOCK_DEADLINE) -> Generator[None]:
    """Serialize starting and dropping this session's app-server.

    The workers of a phase launch in one message and reach `ensure_server`
    within the same second. Unlocked, each reads no record, each starts a server
    and each writes the file: all but one are then orphaned, listening and
    unreachable, and a retry that started a fresh server for every failed worker
    would multiply them.
    """
    with worker_lifecycle.lock_server(session_dir, LOCK_FILE, deadline=deadline):
        yield


def _retire_server(session_dir: str, port: int) -> bool:
    """Retain the failed current server as history before a replacement starts.

    The server record must still name the failed server.
    The stop operation reaps servers from the current record and its history.
    """
    with worker_lifecycle.membership_operation(session_dir), _server_lock(session_dir):
        record = _read_json_object(_session_path(session_dir, SERVER_FILE))
        if record.get("port") != port:
            return False
        _retire_record(session_dir, record)
        return True


def _retire_record(session_dir: str, record: dict[str, object]) -> None:
    """Retain the server in its file until its process stops."""
    servers: list[object] = list(_recorded_run_servers(session_dir))
    _write_account_move_json(_session_path(session_dir, SERVER_FILE), {"previous_servers": servers})
    pid = record.get("pid")
    if worker_lifecycle.is_process_pid(pid):
        _collect_exited_child(pid)


def _record_started_server(session_dir: str, served: ServerRecord) -> None:
    """Publish the new server and retain live previous servers in the same record.

    The caller holds the server lock.
    The watcher stops previous servers after their conversations become idle.
    """
    path = _session_path(session_dir, SERVER_FILE)
    previous = _read_json_object(path)
    same_server = (
        previous.get("pid") == served["pid"]
        and previous.get("port") == served["port"]
    )
    if previous and not same_server:
        previous_pid = previous.get("pid")
        if isinstance(previous_pid, int) and _pid_alive(previous_pid):
            _retire_record(session_dir, previous)
        else:
            if worker_lifecycle.is_process_pid(previous_pid):
                _ = _reap(previous_pid)
    _write_account_move_json(path, served)


def _log_server(session_dir: str, line: str) -> None:
    with contextlib.suppress(OSError), _session_path(session_dir, SERVER_LOG).open("a", encoding="utf-8") as log:
        _ = log.write(f"[{_now_stamp()}] mesh: {line}\n")


def _sign_in_changed_at(home: Path) -> float | None:
    """When Codex's sign-in file last changed. Only its change time is read, never its contents."""
    try:
        return (home / "auth.json").stat().st_mtime
    except OSError:
        return None


def _elapsed_secs(text: str) -> float | None:
    """Seconds from `ps`'s etime, written [[dd-]hh:]mm:ss."""
    days, _dash, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts) or (days and not days.isdigit()):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds + int(days or "0") * 86400)


def _list_process_elapsed(pid: int) -> str:
    return _cleanup_command(("ps", "-o", "etime=", "-p", str(pid)))


def _server_started_at(pid: int) -> float | None:
    """When the process started. `ps -o etime` reads the same on Linux and macOS."""
    try:
        elapsed = _elapsed_secs(_list_process_elapsed(pid))
    except OSError as error:
        print(f"codex_mesh: cannot read app-server {pid} start time ({error}); kept", file=sys.stderr)
        return None
    if elapsed is None:
        print(f"codex_mesh: ps -o etime= -p {pid}: unreadable elapsed time; kept", file=sys.stderr)
        return None
    return time.time() - elapsed


def _holds_old_sign_in(pid: int, home: Path) -> bool:
    """Whether the server started before Codex's sign-in last changed. It read the sign-in once, at
    start, and cannot refresh a token the change revoked."""
    changed = _sign_in_changed_at(home)
    if changed is None:
        return False
    started = _server_started_at(pid)
    return started is not None and started < changed


@dataclass(frozen=True)
class ExistingServerWatcher:
    """Another watcher already holds this server's lifetime lock."""


def _start_watcher(session_dir: str, pid: int, port: int) -> subprocess.Popen[bytes] | ExistingServerWatcher:
    """Stop an unused server after its record expires.

    Check retained servers at each wake.
    Wait for SERVER_IDLE_SECS before checking the current server.
    The inherited lock prevents duplicate watchers.
    """
    script = (
        'while ps -p "$1" >/dev/null 2>&1; do sleep "$5"; '
        + '[ -n "$(find "$3/$6" -mmin "-$7" 2>/dev/null)" ] '
        + '&& ! python3 -c \'import json,sys; from pathlib import Path; '
        + 'path=Path(sys.argv[1]); '
        + 'records=json.loads(path.read_text()).get("previous_servers",[]) if path.exists() else []; '
        + 'sys.exit(not any(record.get("pid")==int(sys.argv[2]) for record in records))\' '
        + '"$3/$8" "$1" 2>/dev/null && continue; '
        + '"$4" "$0" idle-stop --session-dir "$3" --pid "$1" --port "$2" && exit 0; done'
    )
    with _session_path(session_dir, f"mesh-watcher-{pid}-{port}.lock").open("a") as lock:
        try:
            worker_lifecycle.acquire_flock(lock, mode=fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ExistingServerWatcher()
        return subprocess.Popen(
            ["sh", "-c", script, str(Path(__file__).resolve()), str(pid), str(port),
             str(Path(session_dir).resolve()), sys.executable, str(WATCH_WAKE_SECS), SERVER_FILE,
             str(int(SERVER_IDLE_SECS // 60)), SERVER_FILE],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(lock.fileno(),),
        )


def server_record_home(record: dict[str, object]) -> Path:
    """A recorded home, or the default used by launchers before homes were recorded."""
    recorded = _as_str(record.get("codex_home"))
    return Path(recorded) if recorded else Path.home() / ".codex"


def _design_context(session_dir: str) -> dict[str, object]:
    path = _session_path(session_dir, "design_context.json")
    if not path.exists():
        return {}
    from scripts.astra import design_roles
    context = _read_json_object(path)
    design = design_roles.read_design(_as_str(context.get("design_id")))
    context = {"design_id": design.placement.design_id, "generation": design.placement.generation,
                    "binding": {"kind": type(design.binding).__name__, **asdict(design.binding)},
                    "cwd": design.repository}
    return _as_dict(_loads(json.dumps(context)))


@dataclass(frozen=True)
class BulkWorkers:
    """Fresh operation work set validated under membership and every lifecycle."""
    records: dict[str, object]
    membership: worker_lifecycle.MembershipLease
    deadline: float


@dataclass(frozen=True)
class AcquireMembership:
    """This operation must acquire its own run admission boundary."""


ACQUIRE_MEMBERSHIP = AcquireMembership()


@worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS)
def _release_held_server(session_dir: str, *,
                         membership: worker_lifecycle.MembershipLease | AcquireMembership = ACQUIRE_MEMBERSHIP,
                         owned_workers: tuple[worker_lifecycle.WorkerIdentity, ...] = ()) -> None:
    """Park the validated work set through shared-server cleanup under membership."""
    deadline = worker_lifecycle.effective_deadline()
    try:
        with _bulk_workers(session_dir, deadline=deadline, membership=membership,
                           owned_workers=owned_workers, allow_held_roles=True) as workers, _server_lock(session_dir, deadline=deadline):
            _release_held_server_locked(session_dir, workers)
    except (worker_lifecycle.OperationDeadlineExpired, worker_lifecycle.WorkerStateUnreadable) as error:
        raise SystemExit(f"RoleNeedsRecovery: shutdown incomplete: {error}") from error


def _release_held_server_locked(session_dir: str, workers: BulkWorkers) -> None:
    """Release a shared server while the caller owns its validated worker set."""
    roster = workers.records
    for server in _recorded_run_servers(session_dir):
        port, pid = _as_int(server.get("port")), _as_int(server.get("pid"))
        if not _pid_alive(pid):
            _forget_run_server(Path(session_dir), pid, port)
            continue
        for name, value in roster.items():
            entry = _as_dict(value)
            thread_id = _as_str(session_staff.worker_session(entry))
            if not thread_id:
                continue
            client = Client(port, "held-environment-release", operation_deadline=workers.deadline)
            try:
                with _pending_file(session_dir, name, deadline=workers.deadline) as pending:
                    def park(message: str, submission: str) -> None:
                        _park_submission(pending, message, submission)
                    state = _read_live_turn(client, thread_id)
                    if isinstance(state, ThreadStateUnknown):
                        if thread_id not in _loaded_thread_ids(client, workers.deadline):
                            continue
                    _move_queue_to_pending(client, thread_id, park)
                    if not isinstance(state, ThreadIdle):
                        raise SystemExit("RoleNeedsRecovery: old server turn is not proven idle")
                    released = _release_worker_thread(client, thread_id, session_dir)
                    if isinstance(released, WorkerThreadStateUnknown):
                        raise SystemExit("RoleNeedsRecovery: " + released.reason)
            finally:
                client.close()
        if time.monotonic() >= workers.deadline:
            raise worker_lifecycle.OperationDeadlineExpired("shutdown incomplete: held server release deadline expired")
        outcome = _reap_recorded_server(server, workers.deadline)
        if outcome is RecordedServerReap.UNCONFIRMED and _pid_alive(pid):
            raise SystemExit("RoleNeedsRecovery: old server release is uncertain")
        _forget_run_server(Path(session_dir), pid, port)


def command_release_held_server(args: argparse.Namespace) -> int:
    _release_held_server(_as_str(_attr(args, "session_dir")))
    return 0


def held_thread_end_confirmed(session_dir: str, thread_id: str) -> bool:
    """Check every recorded live server before releasing a durable role's hold."""
    for record in _recorded_run_servers(session_dir):
        if not record or not ("pid" in record or "port" in record):
            continue
        pid, port = record.get("pid"), record.get("port")
        if not isinstance(pid, int) or not isinstance(port, int):
            return False
        if not _pid_alive(pid):
            continue
        client = Client(port, "held-end-proof")
        try:
            state = _read_live_turn(client, thread_id)
            if isinstance(state, ThreadIdle):
                continue
            if isinstance(state, ThreadStateUnknown):
                if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                    continue
            return False
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (ConnectionError, OSError, SystemExit):
            return False
        finally:
            client.close()
    return True


def _server_dev_shell(session_dir: str, cwd: str | None) -> DevShellProbe:
    """Select the nearest unit shell; nix_env owns approval and evaluation."""
    if not cwd or Path(session_dir).resolve().parent not in {root.resolve() for root in RUN_ROOTS}:
        return InheritedServerEnvironment()
    try:
        envrc = nix_env.nearest_envrc(Path(cwd))
        if envrc is None:
            return InheritedServerEnvironment()
        revisions: dict[str, str] = {}
        for name in (".envrc", "flake.nix", "flake.lock", "shell.nix", "default.nix", "devenv.nix"):
            try:
                info = (envrc.parent / name).stat()
                revisions[name] = f"{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}"
            except FileNotFoundError:
                revisions[name] = "absent"
        dev_shell = WorktreeDevShell(envrc.parent, json.dumps(revisions, sort_keys=True))
        failed = _read_json_object(_session_path(session_dir, FAILED_DEV_SHELL_FILE))
        failure = RememberedDevShellFailure(
            Path(_as_str(failed.get("worktree"))), _as_str(failed.get("input_revision")),
            _as_float(failed.get("retry_at"), 0),
        )
        return failure if failure.suppresses(dev_shell) else dev_shell
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (OSError, ValueError) as error:
        return DevShellProbeUnavailable(str(error))


def _remember_server_dev_shell(session_dir: str, dev_shell: ServerDevShell) -> None:
    """Keep the last successful shell across idle-stop, separately from the live record."""
    path = _session_path(session_dir, DEV_SHELL_FILE)
    if isinstance(dev_shell, WorktreeDevShell):
        _write_account_move_json(path, {"worktree": str(dev_shell.worktree)})
    else:
        path.unlink(missing_ok=True)


def _remember_failed_dev_shell(session_dir: str, dev_shell: WorktreeDevShell) -> None:
    """Retry failed evaluations after an input change or a thirty-minute cooldown."""
    failure = RememberedDevShellFailure(dev_shell.worktree, dev_shell.input_revision, time.time() + 30 * 60)
    _write_account_move_json(_session_path(session_dir, FAILED_DEV_SHELL_FILE), {
        "worktree": str(failure.worktree), "input_revision": failure.input_revision,
        "retry_at": failure.retry_at,
    })


def _reconcile_server_start(session_dir: str) -> None:
    """Resolve the previous launch under the server lock before admitting another.

    A live record is evidence, not permission to overwrite it. Only the existing
    process/endpoint checks and a successful handshake permit its promotion.
    """
    path = _session_path(session_dir, SERVER_START_FILE)

    def refuse(reason: str) -> NoReturn:
        raise SystemExit(f"codex_mesh: {path}: {reason}; retained start kept; "
                         + "confirm or stop the recorded process, then retry ensure_server")

    try:
        pending = _read_json_object(path)
    except worker_lifecycle.WorkerStateUnreadable as error:
        refuse(str(error))
    if not pending:
        return
    pid, port = _as_int(pending.get("pid")), _as_int(pending.get("port"))
    if pid <= 0 or not 0 < port <= 65535:
        refuse("startup process or endpoint is invalid")
    if not _pid_alive(pid):
        path.unlink()
        return
    # Older startup records did not keep the serving identity/configuration.
    # Guessing those fields could admit the process under a different authority.
    if not _as_str(pending.get("codex_home")):
        refuse("startup codex_home is unconfirmed")
    limit = pending.get("auto_compact_tokens")
    if type(limit) is not int or limit <= 0:
        refuse("startup auto_compact_tokens is unconfirmed")
    current = _read_json_object(_session_path(session_dir, SERVER_FILE))
    if (current and not _record_names_server(current, pid, port)
            and isinstance(_recorded_server_availability(current), LiveServer)):
        refuse("another live current server conflicts with the retained start")
    try:
        if not _server_owned_by_user(pid) or not any(
                server.pid == pid and server.port == port for server in _running_servers()):
            refuse("startup process ownership or listening endpoint is unconfirmed")
        probe = WebSocket(port)
        probe.close()
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (OSError, ConnectionError) as error:
        refuse(f"startup process or endpoint could not be checked ({error})")
    if not _pid_alive(pid):
        refuse("startup process exited during its endpoint check")
    served: ServerRecord = {"pid": pid, "port": port,
                            "codex_home": _as_str(pending["codex_home"]),
                            "auto_compact_tokens": limit}
    if "dev_shell_worktree" in pending:
        served["dev_shell_worktree"] = _as_str(pending["dev_shell_worktree"])
    if "design_context" in pending:
        served["design_context"] = _as_dict(pending["design_context"])
    _record_started_server(session_dir, served)
    _ = _start_watcher(session_dir, pid, port)
    path.unlink()


def ensure_server(session_dir: str, home: Path | None = None, cwd: str | None = None) -> tuple[int, bool]:
    """This session's app-server port, and whether this call is what started it.

    A caller that started the server just proved the provider was reachable
    through a process nothing else has touched, so a failure against it is the
    provider's answer. A caller that attached to one already running has proved
    nothing of the kind -- see _retry_warranted.
    """
    with worker_lifecycle.membership_operation(session_dir) as membership:
        selected_home = workers_home() if home is None else home
        context = _design_context(session_dir)
        previous = _read_json_object(_session_path(session_dir, SERVER_FILE))
        if context and previous.get("pid"):
            pid = _as_int(previous.get("pid"))
            previous_home = server_record_home(previous)
            if (previous.get("design_context") != context or previous_home != selected_home
                    or previous.get("auto_compact_tokens") != AUTO_COMPACT_TOKENS
                    or (_pid_alive(pid) and _holds_old_sign_in(pid, previous_home))):
                _release_held_server(session_dir, membership=membership, owned_workers=worker_lifecycle.held_workers())
        with _server_lock(session_dir):
            _reconcile_server_start(session_dir)
            # Read failures under the same lock as launch: another caller may have
            # just fallen back for this revision while this caller was acquiring it.
            dev_shell = _server_dev_shell(session_dir, cwd)
            dev_shell_worktree = str(dev_shell.worktree) if isinstance(dev_shell, WorktreeDevShell) else ""
            path = _session_path(session_dir, SERVER_FILE)
            record = _read_json_object(path)
            server = _recorded_server_availability(record)
            if isinstance(server, LiveServer):
                port, pid = server.port, _as_int(record.get("pid"))
                recorded_home = server_record_home(record)
                matching_home = recorded_home == selected_home
                old_sign_in = matching_home and _holds_old_sign_in(pid, recorded_home)
                old_limit = record.get("auto_compact_tokens") != AUTO_COMPACT_TOKENS
                old_dev_shell = (cwd is not None and not isinstance(dev_shell, DevShellProbeUnavailable)
                                 and _as_str(record.get("dev_shell_worktree")) != dev_shell_worktree)
                if matching_home and not old_sign_in and not old_limit and not old_dev_shell:
                    # The record's change time is when the server was last used; its watcher reads it.
                    os.utime(path)
                    if isinstance(dev_shell, WorktreeDevShell):
                        _remember_server_dev_shell(session_dir, dev_shell)
                    elif cwd is not None and isinstance(dev_shell, InheritedServerEnvironment):
                        _remember_server_dev_shell(session_dir, dev_shell)
                    return port, False
                # A sign-in change `signin-changed` did not catch. The workers still on this server keep
                # it until they let go; its watcher then stops it.
                _retire_record(session_dir, record)
                cause = (f"no longer serves the workers home {codex_home_display(selected_home)}" if not matching_home
                         else f"holds the old sign-in at {codex_home_display(selected_home)}" if old_sign_in
                         else f"serves another dev-shell than {dev_shell_worktree or 'the caller environment'}" if old_dev_shell
                         else f"compacts at another limit than {AUTO_COMPACT_TOKENS} tokens")
                _log_server(session_dir, f"app-server {pid} {cause}; retired, starting a new one")
                if not _stop_unused_with_lock_held(None, pid, port):
                    _ = _start_watcher(session_dir, pid, port)
            if isinstance(dev_shell, DevShellProbeUnavailable):
                _log_server(session_dir, f"dev-shell probe unavailable: {dev_shell.reason}; starting with caller environment")
            if isinstance(dev_shell, WorktreeDevShell):
                return _start_server(session_dir, selected_home, dev_shell), True
            if isinstance(dev_shell, InheritedServerEnvironment):
                _remember_server_dev_shell(session_dir, dev_shell)
            return _start_server(session_dir, selected_home), True


def _server_environment(session_dir: str, workers_home: Path) -> dict[str, str]:
    """The app-server's environment: the caller's, with the workers home and, for a unit run, the run.

    A worker's hooks and verify.sh runs read the run from UNIT_RUN_DIR.
    assign.py can reach a server that another run started.
    This server sets its run folder directly.
    A mesh directory outside the run roots keeps the caller's environment unchanged.
    """
    environment = staff.child_environment(os.environ)
    environment["CODEX_HOME"] = str(workers_home)
    context = _design_context(session_dir)
    if context:
        for key in tuple(environment):
            if key.startswith(("UNIT_", "PLAN_DELEGATE_")) or key in ("CLAUDE_CODE_SESSION_ID", "HELD_DESIGN_ID", "DESIGN_BINDING_GENERATION"):
                del environment[key]
        environment["HELD_DESIGN_ID"] = _as_str(context.get("design_id"))
        environment["DESIGN_BINDING_GENERATION"] = str(context.get("generation"))
        binding = _as_dict(context.get("binding"))
        if binding.get("kind") == "DesignBoundToPhase":
            _set_run(environment, _as_str(binding.get("run_directory")))
        return environment
    run = Path(session_dir).absolute()
    if run.resolve().parent in {root.resolve() for root in RUN_ROOTS}:
        _set_run(environment, str(run))
    return environment


def _set_run(environment: dict[str, str], run: str) -> None:
    """Name the run, and point TMPDIR into its folder so test temp folders end with the run."""
    environment["UNIT_RUN_DIR"] = run
    try:
        temp = build_folders.create_build_folder(build_folders.StableOwnerFolder(
            unit_runs.RUN_TEMP_NAME, build_folders.ExplicitBuildParent(Path(run))))
    except OSError:
        return
    environment["TMPDIR"] = str(temp)


def _start_server(
    session_dir: str, workers_home: Path, dev_shell: ServerDevShell = _INHERITED_SERVER_ENVIRONMENT,
) -> int:
    """Start this session's app-server and record it. The caller holds the server lock."""
    prefix, unavailable = agent_scope_prefix()
    if unavailable:
        _log_server(session_dir, "agent scope unavailable: " + " ".join(unavailable.splitlines()))
    while True:
        chosen = _free_port()
        log_path = _session_path(session_dir, SERVER_LOG)
        environment = _server_environment(session_dir, workers_home)
        command = ["codex", "-c", "thread_unload_delay_secs=0", "-c", f"model_auto_compact_token_limit={AUTO_COMPACT_TOKENS}",
                   "-c", f"background_terminal_max_timeout={BACKGROUND_TERMINAL_MAX_TIMEOUT_MS}",
                   "app-server", "--listen", f"ws://127.0.0.1:{chosen}"]
        if isinstance(dev_shell, WorktreeDevShell):
            loaded = nix_env.ensure(dev_shell.worktree, environment)
            if isinstance(loaded, nix_env.Loaded):
                # A worktree can choose build flags, but cannot choose the server's
                # account, run, temporary folder or held-design identity.
                protected = ("CODEX_HOME", "UNIT_RUN_DIR", "TMPDIR", "CLAUDE_CODE_SESSION_ID",
                             "HELD_DESIGN_ID", "DESIGN_BINDING_GENERATION")
                loaded_environment = dict(loaded.env)
                for key in protected:
                    if key in environment:
                        loaded_environment[key] = environment[key]
                    elif key in loaded_environment:
                        del loaded_environment[key]
                environment = loaded_environment
                _ = environment.pop("STAFF_ID", None)
            else:
                if isinstance(loaded, nix_env.Refused):
                    _log_server(session_dir, loaded.reason)
                    _remember_failed_dev_shell(session_dir, dev_shell)
                dev_shell = _INHERITED_SERVER_ENVIRONMENT
        try:
            with log_path.open("ab") as log_handle:
                process = subprocess.Popen(
                    # A scope executes the server in place, so its PID remains the recorded PID.
                    [*prefix, *command],
                    stdout=subprocess.DEVNULL,
                    stderr=log_handle,
                    start_new_session=True,
                    env=staff.child_environment(environment),
                )
        except OSError as error:
            if not isinstance(dev_shell, WorktreeDevShell):
                raise
            _remember_failed_dev_shell(session_dir, dev_shell)
            _log_server(session_dir, f"dev-shell app-server {dev_shell.worktree} failed: {error}; falling back to caller environment")
            dev_shell = _INHERITED_SERVER_ENVIRONMENT
            continue
        served: ServerRecord = {"port": chosen, "pid": process.pid, "codex_home": str(workers_home),
                                "auto_compact_tokens": AUTO_COMPACT_TOKENS}
        if isinstance(dev_shell, WorktreeDevShell):
            served["dev_shell_worktree"] = str(dev_shell.worktree)
        context = _design_context(session_dir)
        if context:
            served["design_context"] = context
        try:
            _write_account_move_json(_session_path(session_dir, SERVER_START_FILE), {
                **served, "started_at": time.time(),
            })
            timeout = DEV_SHELL_START_TIMEOUT_SECS if isinstance(dev_shell, WorktreeDevShell) else SERVER_START_TIMEOUT_SECS
            deadline = time.time() + timeout
            while time.time() < deadline:
                if process.poll() is not None:
                    raise SystemExit(
                        f"codex_mesh: app-server exited immediately; see {log_path}"
                    )
                try:
                    probe = WebSocket(chosen)
                except worker_lifecycle.OperationDeadlineExpired:
                    raise
                except (OSError, ConnectionError):
                    time.sleep(0.4)
                    continue
                probe.close()
                _record_started_server(session_dir, served)
                if isinstance(dev_shell, WorktreeDevShell):
                    _remember_server_dev_shell(session_dir, dev_shell)
                    _session_path(session_dir, FAILED_DEV_SHELL_FILE).unlink(missing_ok=True)
                if context:
                    from scripts.astra import design_roles
                    design_roles.refresh_census(_as_str(context.get("design_id")))
                _STARTED_SERVERS.add((chosen, process.pid))
                _ = _start_watcher(session_dir, process.pid, chosen)
                return chosen
            raise SystemExit(f"codex_mesh: app-server did not accept connections on {chosen}")
        except AtomicDurabilityUnconfirmed:
            # The complete server record is visible. Preserve it and its process for
            # reconciliation; treating this as an unpublished start would lose evidence.
            raise
        except BaseException as error:
            _discard_failed_server_start(session_dir, served, process)
            if not isinstance(error, SystemExit) or not isinstance(dev_shell, WorktreeDevShell):
                raise
            _remember_failed_dev_shell(session_dir, dev_shell)
            _log_server(session_dir, f"dev-shell app-server {dev_shell.worktree} failed: {error}; falling back to caller environment")
            dev_shell = _INHERITED_SERVER_ENVIRONMENT
        finally:
            pending = _session_path(session_dir, SERVER_START_FILE)
            # A visible startup record may be the only process evidence after
            # its directory fsync fails. Retain it until publication or exit.
            published = _record_names_server(
                _read_json_object(_session_path(session_dir, SERVER_FILE)), process.pid, chosen)
            if (published or process.poll() is not None) and _record_names_server(
                    _read_json_object(pending), process.pid, chosen):
                pending.unlink(missing_ok=True)


def _discard_failed_server_start(
    session_dir: str, served: ServerRecord, process: subprocess.Popen[bytes]
) -> None:
    """Leave neither a process nor a current record after a failed start."""
    # Popen starts a new session: its pid is also the group id. Signal even when
    # the leader already exited, since a failed Nix evaluation can leave children.
    try:
        _signal_process(process.pid, signal.SIGTERM, group=True)
    except OSError:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _ = process.wait(timeout=5)
    with contextlib.suppress(OSError):
        _signal_process(process.pid, signal.SIGKILL, group=True)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            _ = process.wait(timeout=5)
    if process.poll() is None:
        raise SystemExit(f"codex_mesh: {_session_path(session_dir, SERVER_START_FILE)}: "
                         + "failed startup process is not confirmed stopped; confirm or stop it before retrying")
    path = _session_path(session_dir, SERVER_FILE)
    if _record_names_server(_read_json_object(path), served["pid"], served["port"]):
        _forget_run_server(Path(session_dir), served["pid"], served["port"])


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def _describe_item(item: dict[str, object]) -> str:
    """One log line for a stream item, so heartbeat_watch.sh can narrate."""
    kind = _as_str(item.get("type"))
    if kind == "agentMessage":
        return f"agent: {' '.join(_as_str(item.get('text')).split())}"
    if kind == "commandExecution":
        command = _as_str(item.get("command")) or _as_str(item.get("commandLine"))
        return f"exec: {' '.join(command.split())}"
    if kind == "fileChange":
        return f"edit: {_as_str(item.get('path')) or 'file change'}"
    if kind == "reasoning":
        return "thinking"
    return kind or "working"


def _thread_has_no_active_turn(status: str) -> bool:
    """These app-server thread states have no turn left to finish."""
    return status in ("idle", "notLoaded", "systemError")


def _recent_turns(client: Client, thread_id: str, timeout: float = CALL_TIMEOUT_SECS) -> list[dict[str, object]]:
    """The thread's newest turns, newest first.

    A full-history `thread/read` queues a deprecation notice ahead of its reply,
    which a following launcher takes as a frame still to handle, so turns come
    from the paged list. A refused call lists none.
    """
    reply = client.call("thread/turns/list", {
        "threadId": thread_id, "limit": RECENT_TURNS, "sortDirection": "desc"}, timeout=timeout)
    turns = _as_dict(reply.get("result")).get("data")
    if not isinstance(turns, list):
        return []
    return [_as_dict(turn) for turn in cast("list[object]", turns)]


def _ended_unseen(client: Client, thread_id: str, turn_id: str = "", *, start_seen: bool = True) -> bool:
    """True when no turn is active and nothing is queued, so no turn is left to end.

    A launcher that reads this while it waits for a completion notice has missed
    the notice. Given the watched turn, the thread must also list that turn,
    either after a later turn or, once its start was seen, as ended: the server
    can read a thread idle, and list a turn as ended, just after the turn starts.
    A turn not yet listed has not ended. With no turn the read asks for none, so
    it stays small on a long thread.
    A call that is refused or times out says nothing, and the launcher keeps waiting.
    """
    try:
        listed = client.call("thread/queue/list", {"threadId": thread_id}, UNSEEN_END_CALL_SECS)
        items = _as_dict(listed.get("result")).get("data")
        if isinstance(items, list) and items:
            return False
        reply = client.call(
            "thread/read", {"threadId": thread_id, "includeTurns": False}, UNSEEN_END_CALL_SECS)
        thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
        if not _thread_has_no_active_turn(_as_str(_as_dict(thread.get("status")).get("type"))):
            return False
        if not turn_id:
            return True
        turns = _recent_turns(client, thread_id, UNSEEN_END_CALL_SECS)
    except SystemExit:
        return False
    # Newest first: a later turn proves the watched one is over.
    for index, turn in enumerate(turns):
        if turn.get("id") == turn_id:
            return index > 0 or (start_seen and turn.get("status") in ("completed", "failed", "interrupted"))
    return False


def queue_grace_secs() -> float:
    """Read the queue grace for this wait, including in child processes."""
    variable = "CODEX_MESH_QUEUE_GRACE_SECS"
    value = os.environ.get(variable, "")
    if not value:
        return QUEUE_GRACE_SECS
    try:
        grace = float(value)
    except ValueError:
        raise SystemExit(f"{variable} must be a finite number of 0 or more; got {value!r}") from None
    if not math.isfinite(grace) or grace < 0:
        raise SystemExit(f"{variable} must be a finite number of 0 or more; got {value!r}")
    return grace


def _more_work_coming(client: Client, thread_id: str, deadline: float) -> bool:
    """True when a turn that just ended is not the worker's last.

    Peer sends land in the worker's locked pending file, checked by the caller
    before it closes the launcher. This check covers older server-queued work:
    a message still queued, or a dequeued turn whose start notice is in flight.
    """
    listed = client.call("thread/queue/list", {"threadId": thread_id})
    items = _as_dict(listed.get("result")).get("data")
    if isinstance(items, list) and items:
        return True
    live = _read_live_turn(client, thread_id)
    if isinstance(live, ThreadStateUnknown):
        raise SystemExit(f"thread {thread_id}: thread/read failed: {live.reason}")
    if not isinstance(live, ThreadIdle):
        return True
    # Frames are held locally rather than pushed back as they arrive: a frame
    # returned to the buffer is the next thing `next_frame` hands out, so
    # push-as-you-go would re-serve the same frame and never reach the socket.
    grace = min(time.time() + queue_grace_secs(), deadline)
    expires = time.monotonic() + max(0.0, grace - time.time())
    held: list[RpcMessage] = []
    started = False
    while time.monotonic() < expires:
        frame = client.next_frame(time.time() + max(0.0, expires - time.monotonic()))
        if frame is None:
            continue
        held.append(frame)
        if (frame.get("method") == "turn/started"
                and _as_str(_as_dict(frame.get("params")).get("threadId")) in ("", thread_id)):
            started = True
            break
    for frame in reversed(held):
        client.push_back(frame)
    return started


@dataclass(frozen=True)
class TurnCompleted:
    """The turn finished successfully."""


@dataclass(frozen=True)
class TurnRefusedForCapacity:
    detail: str


@dataclass(frozen=True)
class TurnContextWindowExceeded:
    detail: str


@dataclass(frozen=True)
class TurnFailed:
    detail: str


TurnOutcome = TurnCompleted | TurnRefusedForCapacity | TurnContextWindowExceeded | TurnFailed


@dataclass(frozen=True)
class RunCompleted:
    """The worker ended without a failure."""




@dataclass(frozen=True)
class WorkerOwnershipLost:
    """The launcher no longer holds its claim."""

    def message(self) -> str:
        return "worker ownership changed; assignment completion unknown"







@dataclass(frozen=True)
class FailedBeforeThread:
    """No thread id exists, so a fast retry may repeat the original prompt."""

    failure: str
    seconds: float
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    refusal_reported: bool = False


@dataclass(frozen=True)
class AwaitingQuotaContinuation:
    """No continuation submission has yet been accepted."""


@dataclass(frozen=True)
class SubmittedQuotaContinuation:
    thread_id: str
    turn_id: str


@dataclass(frozen=True)
class QuotaContinuationTurn:
    """This submission resumes the work interrupted by the pending refusal."""


@dataclass(frozen=True)
class UnrelatedWorkerTurn:
    """This submission cannot finish an earlier quota continuation."""


UNRELATED_WORKER_TURN = UnrelatedWorkerTurn()


@dataclass(frozen=True)
class PendingQuotaRefusal:
    """A reported refusal whose owed continuation has not completed."""

    serving: CodexServingState
    detail: str
    continuation: AwaitingQuotaContinuation | SubmittedQuotaContinuation = field(
        default_factory=AwaitingQuotaContinuation, kw_only=True)


@dataclass(frozen=True)
class NoPendingQuotaRefusal:
    """This command has no reported quota refusal to hand back on exit."""


@dataclass(frozen=True)
class FailedWithThread:
    """The thread exists; its original prompt must never be submitted again."""

    thread_id: str
    failure: str
    seconds: float
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    refusal_reported: bool = False
    refusal_checked: bool = False
    pending_refusal: PendingQuotaRefusal | NoPendingQuotaRefusal = field(default_factory=NoPendingQuotaRefusal, kw_only=True)
    director: KnownWorkerDirector | UnknownWorkerDirector = field(default_factory=UnknownWorkerDirector, kw_only=True)


@dataclass(frozen=True)
class CapacityRetriesExhausted:
    """The original task still needs a turn, but its launcher is stopping."""

    thread_id: str
    retries: int
    minutes: int
    pending_refusal: PendingQuotaRefusal | NoPendingQuotaRefusal = field(default_factory=NoPendingQuotaRefusal, kw_only=True)
    director: KnownWorkerDirector | UnknownWorkerDirector = field(default_factory=UnknownWorkerDirector, kw_only=True)

    def message(self, name: str) -> str:
        return (
            f"model still at capacity after {self.retries} retries over {self.minutes} min; "
            f"thread {self.thread_id} stays on the roster (codex_mesh.py end --to {name})"
        )


@dataclass(frozen=True)
class WorkerLaunchRefused:
    """The existing worker cannot be replaced; no launch was attempted."""


RunOutcome = RunCompleted | WorkerLaunchRefused | WorkerOwnershipLost | FailedBeforeThread | FailedWithThread | CapacityRetriesExhausted


@dataclass(frozen=True)
class ThreadIdle:
    """The server says no turn is active."""


@dataclass(frozen=True)
class ThreadLive:
    turn_id: str


@dataclass(frozen=True)
class ThreadActiveWithoutTurn:
    """A turn is active but its id is not persisted in thread/read yet."""


@dataclass(frozen=True)
class ThreadStateUnknown:
    reason: str


ThreadState = ThreadIdle | ThreadLive | ThreadActiveWithoutTurn | ThreadStateUnknown


@dataclass(frozen=True)
class RelaunchAllowed:
    """Cleanup found no confirmed live turn that it could not interrupt."""
    interrupt_requested: bool = False


@dataclass(frozen=True)
class RelaunchBlockedByLiveTurn:
    """The old thread still has a turn this launcher could not stop."""
    interrupt_requested: bool = False


UnwatchedTurnCleanupResult = RelaunchAllowed | RelaunchBlockedByLiveTurn


def _capacity_clock() -> float:
    return time.monotonic()


def _capacity_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _turn_outcome(method: str, params: dict[str, object]) -> TurnOutcome:
    turn = _as_dict(params.get("turn"))
    error = _as_dict(turn.get("error")) if method == "turn/completed" else _as_dict(params.get("error"))
    detail = _as_str(error.get("message"))
    info = error.get("codexErrorInfo")
    if info == "contextWindowExceeded" or (
        isinstance(info, dict) and "contextWindowExceeded" in info
    ):
        return TurnContextWindowExceeded(detail or "context window exceeded")
    if info in ("serverOverloaded", "flexUnavailable") or (
        isinstance(info, dict) and ("serverOverloaded" in info or "flexUnavailable" in info)
    ) or "Selected model is at capacity" in detail:
        return TurnRefusedForCapacity(detail)
    if method == "turn/failed" or detail or turn.get("status") in ("failed", "interrupted"):
        return TurnFailed(detail or "turn failed")
    return TurnCompleted()


def _interrupted_for_held_work(
    method: str, params: dict[str, object], session_dir: str, name: str,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> bool:
    """An interrupted turn with no error while a run is live or a message is held.

    `interrupt` stops a turn so a held message opens the next one; that stop is not a failure,
    so the slot is not recorded failed for it.
    """
    turn = _as_dict(params.get("turn"))
    if method != "turn/completed" or turn.get("status") != "interrupted":
        return False
    if _as_str(_as_dict(turn.get("error")).get("message")):
        return False
    with _pending_file(session_dir, name) as handle:
        held = bool(_pending_messages(handle))
    current = _run_watch(session_dir, name, runs).current()
    return held or current.live or current.dead


def _retry_warranted(outcome: RunOutcome, fresh_server: bool, resident: bool) -> bool:
    """Whether this failure is worth one more run against a clean app-server.

    A wedged app-server replays a provider message it cached earlier to every
    thread that attaches, with no round trip -- so the text reads exactly like a
    usage limit or an outage while nothing is wrong with the account. Reading
    the message cannot tell the two apart; running the same work against a
    server this process just started can, because a fresh server has cached
    nothing. Same failure twice means the provider really did refuse.

    Four conditions, each of them narrowing:
      * the failed server was inherited, so nothing has tested it this run;
      * no thread exists, so repeating the prompt cannot repeat work -- or the
        thread's turn was refused for a stale sign-in, which the provider turns
        away before any work, so a new thread repeats nothing either;
      * the failure arrived too fast to have reached the provider, or to have
        done work before the refusal;
      * and the worker is not resident, where a caller is already holding the
        replies and a silent second attempt would arrive behind them.
    """
    return (
        not fresh_server
        and not resident
        and (isinstance(outcome, FailedBeforeThread)
             or (isinstance(outcome, FailedWithThread) and STALE_SIGN_IN in outcome.failure))
        and outcome.seconds <= RETRY_FAST_FAILURE_SECS
    )


def _quota_refused(outcome: RunOutcome, tested: bool, resident: bool) -> bool:
    """Whether the provider itself turned this run away for quota.

    A wedged app-server replays a usage limit it cached, so the words count only
    from a server this launcher started or retried on, or from a run long enough
    to have reached the provider. A resident's clock spans many turns and proves
    nothing about the last one.
    """
    return (
        isinstance(outcome, (FailedBeforeThread, FailedWithThread))
        and QUOTA_REFUSAL in outcome.failure
        and (tested or (not resident and outcome.seconds > RETRY_FAST_FAILURE_SECS))
    )


def _quota_window(detail: str) -> str:
    weekly = bool(re.search(r"\bweek(?:ly)?\b|7[ -]?day", detail, re.IGNORECASE))
    five = bool(re.search(r"5[ -]?(?:hour|hr)|five[ -]?hour", detail, re.IGNORECASE))
    return "weekly" if weekly and not five else "five_hour" if five and not weekly else "unknown"


def _continuation_is_owed(session_dir: str, name: str) -> bool:
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    return record.get("continuation_owed") is True


def _owe_quota_continuation(client: Client, thread_id: str) -> None:
    if client.session_dir and thread_id:
        if not _update_roster_if_expected(client.session_dir, client.name,
            {"thread_id": thread_id, "status": "starting", "continuation_owed": True},
            RosterActiveLauncher(thread_id, (client.launcher_pid, os.getpid()))):
            raise SystemExit(f"codex_mesh: {client.name}: roster ownership changed; quota continuation not recorded")


def _worker_director(folder: Path) -> KnownWorkerDirector | UnknownWorkerDirector:
    try:
        holders = tuple(_marker_sessions(SWEEP_ROOTS, folder)) or _launcher_sessions(folder)
    except OSError:
        return UnknownWorkerDirector()
    return KnownWorkerDirector(holders[0]) if len(holders) == 1 else UnknownWorkerDirector()


def _mesh_phase_wait(session_dir: str, name: str, thread_id: str, kind: str, reason: str = "", *,
                     finished: bool = False) -> None:
    """Keep a wait across launcher exits until this worker starts a turn again."""
    if not session_dir or os.environ.get(PROBE_ENV) == "1":
        return
    directory = Path(session_dir)
    if not (directory / "progress_history_state.json").is_file():
        return
    marker = directory / f"mesh-wait.{name}.{kind}"
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "direct/progress_history.py")]
    try:
        if finished:
            if not marker.is_file():
                return
            command += ["finish-wait", "--session-dir", session_dir, "--wait-id", marker.read_text().strip()]
        else:
            if marker.is_file():
                return
            command += ["start-wait", "--session-dir", session_dir, "--kind", kind, "--reason", reason]
            command += ["--worker-thread-id", thread_id, "--owner-session", thread_id]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode:
            print(f"codex_mesh: {name}: wait NotRecorded: {result.stderr.strip()}", file=sys.stderr)
        elif finished:
            marker.unlink(missing_ok=True)
        elif result.stdout.strip():
            _ = marker.write_text(result.stdout.strip(), encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"codex_mesh: {name}: wait NotRecorded: {error}", file=sys.stderr)


def _report_quota_refusal(name: str, serving: CodexServingState, detail: str = "", *,
                         session_dir: str, thread_id: str,
                         stopped: RefusalCommandContext = REFUSAL_WITHOUT_COMMAND_EXIT) -> None:
    """Hand the serving identity, named limit and refusal time to the account mover."""
    _mesh_phase_wait(session_dir, name, thread_id, "WaitingForQuotaRefusal", detail)
    try:
        account = serving.account
        identity = ([str(account.home), account.login, account.label]
                    if isinstance(account, KnownCodexAccount)
                    else [str(account.home), account.reason])
        stopped_arguments: list[str] = []
        if isinstance(stopped, (CommandStoppedBeforeThread, CommandStoppedOnThread)):
            director = stopped.director
            if isinstance(director, ResolveWorkerDirector):
                director = _worker_director(Path(stopped.session_dir))
            if isinstance(director, KnownWorkerDirector):
                stopped_arguments = ["--stopped", director.session_id, name, shlex.join(sys.argv)]
            if isinstance(stopped, CommandStoppedOnThread):
                with worker_lifecycle.lock_worker(stopped.session_dir, name):
                    if not _update_roster_if_expected(stopped.session_dir, name,
                        {"thread_id": stopped.thread_id, "status": "failed", "continuation_owed": False,
                         "settled_launcher_pid": os.getpid()},
                        RosterRefusingLauncher(stopped.thread_id, (os.getpid(),))):
                        # Still report the account's refusal, but never request a
                        # rerun of work now owned by another launcher.
                        stopped_arguments = []
                        print(f"codex_mesh: {name}: roster ownership changed; quota handoff did not change the worker",
                              file=sys.stderr)
        done = subprocess.run(
            [sys.executable, str(QUOTA_REPORT), "blocked", *identity,
             "--window", _quota_window(detail), "--refused-at", datetime.now(timezone.utc).isoformat(), *stopped_arguments],
            capture_output=True, text=True, timeout=QUOTA_REPORT_TIMEOUT_SECS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"codex_mesh: {name}: quota refusal not reported: {exc}", file=sys.stderr)
        return
    for line in (done.stdout + done.stderr).splitlines():
        print(f"codex_mesh: {name}: quota: {line}", file=sys.stderr)


def _relaunch_refusal(name: str, record: dict[str, object], membership: staff.StaffSnapshot) -> str:
    """A live capacity waiter owns its worker even if its server record is gone."""
    thread_id = session_staff.codex_thread(record, membership)
    if thread_id and record.get("status") == WAITING_CAPACITY and _pid_alive(_as_int(record.get("launcher_pid"))):
        return (f"thread {thread_id} is waiting for capacity; "
                + f"use codex_mesh.py end --to {name} before relaunching")
    return ""


@dataclass(frozen=True)
class WorkerAdmission:
    """Selection ownership transferred to the thread publisher, never its watcher."""
    locks: contextlib.ExitStack
    membership: worker_lifecycle.MembershipLease

    def published(self) -> None:
        # Lifecycle, inbox and roster scopes have exited; membership is last.
        self.locks.close()


@dataclass(frozen=True)
class IndependentWorkerAdmission:
    """Direct callers acquire admission at the attachment operation itself."""

    def published(self) -> None:
        pass


INDEPENDENT_WORKER_ADMISSION = IndependentWorkerAdmission()


def command_start(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    resident = getattr(args, "resident", False) is True
    try:
        staff_admission = worker_admission.admit(name, Path(session_dir), reference=_as_str(_attr(args, "staff_id")))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    name = staff_admission.name
    args.name = name
    with contextlib.ExitStack() as admission:
        membership = admission.enter_context(worker_lifecycle.lock_membership(session_dir))
        with worker_lifecycle.lock_worker(session_dir, name):
            record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            membership_record = staff.local_snapshot()
            reason = _relaunch_refusal(name, record, membership_record)
            if reason:
                print(f"codex_mesh: {name}: {reason}", file=sys.stderr)
                return 2
            port, fresh_server = ensure_server(session_dir, cwd=_as_str(_attr(args, "cwd")) or str(session_staff.worker_directory(membership_record, staff_admission.parent)))
        outcome = _run_worker(args, port, started_here=fresh_server,
                              admission=WorkerAdmission(admission, membership), staff_admission=staff_admission)
    if isinstance(outcome, WorkerLaunchRefused):
        return 2
    tested = fresh_server
    if _retry_warranted(outcome, fresh_server, resident):
        _ = _retire_server(session_dir, port)
        assert isinstance(outcome, (FailedBeforeThread, FailedWithThread))
        if isinstance(outcome, FailedWithThread):
            note = "the inherited app-server holds a Codex sign-in that has since changed"
        else:
            note = f"the inherited app-server failed in {outcome.seconds:.0f}s without reaching the provider"
        print(f"codex_mesh: {name}: {note}; retrying on a new one", file=sys.stderr)
        with contextlib.ExitStack() as admission:
            membership = admission.enter_context(worker_lifecycle.lock_membership(session_dir))
            port, _fresh = ensure_server(session_dir, cwd=_as_str(_attr(args, "cwd")) or str(session_staff.worker_directory(membership_record, staff_admission.parent)))
            outcome = _run_worker(args, port, started_here=_fresh,
                                  admission=WorkerAdmission(admission, membership), staff_admission=staff_admission)
        tested = True
    if (isinstance(outcome, (FailedWithThread, CapacityRetriesExhausted))
            and isinstance(outcome.pending_refusal, PendingQuotaRefusal)):
        refusal = outcome.pending_refusal
        _report_quota_refusal(name, refusal.serving, refusal.detail,
                             session_dir=session_dir, thread_id=outcome.thread_id,
                             stopped=CommandStoppedOnThread(session_dir, outcome.thread_id, outcome.director))
    if isinstance(outcome, (FailedBeforeThread, FailedWithThread)):
        print(f"codex_mesh: {name}: {outcome.failure}", file=sys.stderr)
        if not outcome.refusal_reported and not (isinstance(outcome, FailedWithThread) and outcome.refusal_checked) and _quota_refused(outcome, tested, resident):
            if isinstance(outcome, FailedWithThread):
                _report_quota_refusal(name, outcome.serving, outcome.failure,
                                     session_dir=session_dir, thread_id=outcome.thread_id,
                                     stopped=CommandStoppedOnThread(session_dir, outcome.thread_id, outcome.director))
            else:
                _report_quota_refusal(name, outcome.serving, outcome.failure,
                                     session_dir=session_dir, thread_id="",
                                     stopped=CommandStoppedBeforeThread(session_dir))
        return 1
    if isinstance(outcome, CapacityRetriesExhausted):
        print(f"codex_mesh: {name}: {outcome.message(name)}", file=sys.stderr)
        return 1
    if isinstance(outcome, WorkerLaunchRefused):
        return 2
    if isinstance(outcome, WorkerOwnershipLost):
        print(f"codex_mesh: {name}: {outcome.message()}", file=sys.stderr)
        return 2
    return 0


def command_follow(args: argparse.Namespace) -> int:
    run = Path(_as_str(_attr(args, "session_dir")))
    name = _as_str(_attr(args, "to"))
    reservation = unit_workers.allocation_for_follow(run, name)
    try:
        return _follow_worker(args)
    finally:
        unit_workers.release_allocation(run, name, reservation)


def _follow_worker(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, name)
    if record.get("held_design_id") and _as_str(_attr(args, "message_file")) != record.get("authorized_prompt"):
        moving = bool(_attr(args, "account_move")) and isinstance(
            _read_account_move(session_dir, name, _as_str(session_staff.worker_session(record))), AccountMoveMarker)
        if not moving:
            raise SystemExit("codex_mesh: held role assignment must come from design_roles.py")
    role = _worker_role(args)
    display_name = unit_workers.shown_name(Path(session_dir), name, role=role)
    claim_pid = _as_int(_attr(args, "claim_pid"))
    try:
        state = _lookup_state(session_dir, name)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1 if claim_pid else 2
    if claim_pid:
        if not isinstance(state, StartingThread) or state.launcher_pid != claim_pid:
            print(f"codex_mesh: {display_name}: follow claim changed after dispatch", file=sys.stderr)
            return 1
    else:
        reason = _follow_refusal(session_dir, state, name=name)
        if reason:
            print(f"codex_mesh: {display_name}: {reason}; follow refused", file=sys.stderr)
            return 2
    try:
        staff_admission = _admit_worker(name, session_dir)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    assert not isinstance(state, InvalidThread)
    thread_id = state.thread_id
    if not claim_pid and not _claim_follow(
        session_dir, name, thread_id, os.getpid(), role, _dispatch_lens(args)
    ):
        print(f"codex_mesh: {display_name} was already claimed", file=sys.stderr)
        return 2
    _set_roster_assignment(session_dir, name, role, _as_str(_attr(args, "task")),
                           RosterActiveLauncher(thread_id, (claim_pid or os.getpid(), os.getpid())))
    if claim_pid:
        _record_assignment(thread_id, role, _dispatch_lens(args), only_if_changed=True)
    owner_pid = claim_pid or os.getpid()
    opened = time.time()
    client: Client | None = None
    live_turn = ""
    try:
        _ = ensure_server(session_dir, workers_home(), cwd=_as_str(_attr(args, "turn_cwd")) or None)
        with worker_lifecycle.membership_operation(session_dir), contextlib.ExitStack() as lifecycle:
            _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
            current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            if not RosterActiveLauncher(thread_id, (owner_pid, os.getpid())).owns(current):
                raise SystemExit("worker ownership changed before resume; follow-up did not start")
            home = workers_home()
            port, _fresh = ensure_server(session_dir, home, cwd=_as_str(_attr(args, "turn_cwd")) or None)
            _note_worker_port(session_dir, name, owner_pid, port)
            released = _release_other_worker_writers(session_dir, name, thread_id, port)
            if isinstance(released, WorkerThreadStateUnknown):
                raise SystemExit(f"thread {thread_id}: {released.reason}; follow-up did not start")
            if isinstance(released, WorkerWaitingOnLiveTurn):
                raise SystemExit(released.reason)
            client = Client(port, name)
            _ = _bind_serving_server(client, session_dir, started_here=_fresh)
            lifecycle.close()
            resumed = _resume_worker_thread(args, session_dir, name, thread_id, client, home,
                                            staff_admission=staff_admission)
            _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
            if resumed.thread_replaced:
                thread_id = resumed.thread_id
                if not _update_roster_if_expected(session_dir, name, {
                    "thread_id": thread_id, "status": "starting", "launcher_pid": owner_pid, "port": port,
                }, RosterOnThread(thread_id)):
                    _refuse_taken_follow_up(client, thread_id, session_dir)
                _record_assignment(thread_id, role, _dispatch_lens(args))
            live = _read_live_turn(client, thread_id)
            if not isinstance(live, ThreadIdle):
                if isinstance(live, ThreadLive):
                    live_turn = live.turn_id
                reason = live.reason if isinstance(live, ThreadStateUnknown) else "live turn"
                raise SystemExit(f"thread {thread_id}: {reason}; follow-up failed")
            if record.get("unit_ledger"):
                _ = _require(client.call("thread/name/set", {
                    "threadId": thread_id,
                    "name": unit_workers.shown_name(Path(session_dir), name, role=role),
                }), "thread/name/set")
        if record.get("unit_ledger"):
            unit_workers.note_backend(Path(session_dir), name, resumed.thread_id, home)
        prompt = _message_text(args)
        timeout = _as_float(_attr(args, "timeout"), 86400.0)
        outcome = _stream_turn(args, client, port, session_dir, name, thread_id, prompt, timeout, opened, follow=True)
        client = None
        if isinstance(outcome, (FailedBeforeThread, FailedWithThread)):
            print(f"codex_mesh: {display_name}: {outcome.failure}", file=sys.stderr)
            return 1
        if isinstance(outcome, CapacityRetriesExhausted):
            print(f"codex_mesh: {display_name}: {outcome.message(name)}", file=sys.stderr)
            return 1
        if isinstance(outcome, WorkerOwnershipLost):
            print(f"codex_mesh: {display_name}: {outcome.message()}", file=sys.stderr)
            return 2
        return 0
    except (ConnectionError, OSError, SystemExit, unit_workers.WorkerUnavailable, ValueError) as exc:
        _restore_follow_claim(session_dir, name, owner_pid, live_turn)
        if live_turn and client is not None:
            try:
                timeout = _as_float(_attr(args, "timeout"), 86400.0)
                if _wait_for_claimed_peer(client, thread_id, live_turn, opened + timeout):
                    _restore_follow_claim(session_dir, name, owner_pid, finished=True)
            except (ConnectionError, OSError, SystemExit):
                pass
        print(f"codex_mesh: {display_name}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


class CompactionTimedOut(Exception):
    """The compaction turn did not finish in time, and may still be live on the thread."""

    def __init__(self, message: str, turn_id: str) -> None:
        super().__init__(message)
        # Empty when the server never named the compaction turn.
        self.turn_id: str = turn_id


def _compact_thread(client: Client, thread_id: str) -> KnownContextSize:
    """Start compaction and return the usage update for its compact turn.

    Codex 0.162.0 schemas retain `thread/compacted` but mark it deprecated in
    favor of the `contextCompaction` item. Servers in the same release may send
    either completion shape, so both identify the compact turn here.
    """
    _ = _require(
        client.call("thread/compact/start", {"threadId": thread_id}),
        "thread/compact/start",
    )
    expires = time.monotonic() + CALL_TIMEOUT_SECS
    compacted_turn = ""
    started_turn = ""
    sizes_by_turn: dict[str, KnownContextSize] = {}
    completed_turns: set[str] = set()
    while time.monotonic() < expires:
        frame = client.next_frame(time.time() + max(0.0, expires - time.monotonic()))
        if frame is None:
            continue
        params = _as_dict(frame.get("params"))
        if _as_str(params.get("threadId")) != thread_id:
            continue
        method = frame.get("method")
        if method == "thread/tokenUsage/updated":
            size = _context_size_from_usage(params)
            usage_turn = _as_str(params.get("turnId"))
            if isinstance(size, KnownContextSize) and usage_turn:
                sizes_by_turn[usage_turn] = size
        elif method == "turn/started":
            started_turn = _as_str(_as_dict(params.get("turn")).get("id")) or started_turn
        elif method == "thread/compacted":
            compacted_turn = _as_str(params.get("turnId"))
        elif method == "item/completed":
            item = _as_dict(params.get("item"))
            if _as_str(item.get("type")) == "contextCompaction":
                compacted_turn = _as_str(params.get("turnId"))
        elif method == "turn/completed":
            completed = _as_str(_as_dict(params.get("turn")).get("id"))
            if completed:
                completed_turns.add(completed)
        if (
            compacted_turn
            and compacted_turn in sizes_by_turn
            and compacted_turn in completed_turns
        ):
            return sizes_by_turn[compacted_turn]
    if compacted_turn and compacted_turn in sizes_by_turn:
        raise CompactionTimedOut(
            f"thread {thread_id}: compaction turn {compacted_turn} did not complete", compacted_turn
        )
    if compacted_turn:
        raise CompactionTimedOut(
            f"thread {thread_id}: compacted without a final token-usage update", compacted_turn
        )
    raise CompactionTimedOut(
        f"thread {thread_id}: no thread/compacted within {CALL_TIMEOUT_SECS}s", started_turn
    )


def _wait_for_idle(client: Client, thread_id: str) -> ThreadState:
    """Read the thread until it is idle or `COMPACT_IDLE_WAIT_SECS` pass; returns the last state read."""
    expires = time.monotonic() + COMPACT_IDLE_WAIT_SECS
    while True:
        state = _read_live_turn(client, thread_id)
        if (isinstance(state, (ThreadIdle, ThreadStateUnknown))
                or time.monotonic() >= expires):
            return state
        time.sleep(COMPACT_IDLE_POLL_SECS)


def _abandon_compaction(client: Client, name: str, thread_id: str, timeout: CompactionTimedOut) -> int:
    """Interrupt a compaction turn that did not finish, so the worker can take its follow-up.

    A compaction left live refuses every later follow-up and compaction of the worker.
    """
    state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
    interrupted = False
    # A live turn that is not the compaction's belongs to someone else and is left alone.
    if isinstance(state, ThreadLive) and timeout.turn_id in ("", state.turn_id):
        # Best effort, as in `end`: a turn that finished since the read answers with an error.
        _ = _interrupt_turn(client, thread_id, state.turn_id)
        interrupted = True
        state = _wait_for_idle(client, thread_id)
    if isinstance(state, ThreadIdle):
        how = "interrupted the compaction turn, " if interrupted else ""
        print(f"codex_mesh: {name}: {timeout}; {how}the worker is idle and not compacted", file=sys.stderr)
        return 1
    reason = state.reason if isinstance(state, ThreadStateUnknown) else "a turn is still live on the thread"
    print(f"codex_mesh: {name}: {timeout}; {reason}", file=sys.stderr)
    return COMPACT_TURN_STILL_LIVE


def command_compact(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, name):
        try:
            record = _lookup(session_dir, name)
            state = _roster_thread(record)
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 2
        if not isinstance(state, FollowableThread):
            detail = ("launcher died; use follow or reopen to recover its idle thread" if _launcher_died(state)
                      else "worker is running or otherwise busy; compact requires a finished worker")
            print(
                f"codex_mesh: {name}: {detail}",
                file=sys.stderr,
            )
            return 2
        before = _context_size_from_record(record)
        revision = _roster_revision(record)
        if isinstance(before, NotMeasuredSinceCompaction):
            print(
                f"codex_mesh: {name}: size not measured since its last compaction; compact refused",
                file=sys.stderr,
            )
            return 2
        if not isinstance(before, KnownContextSize):
            print(
                f"codex_mesh: {name}: no recorded context size; compact refused",
                file=sys.stderr,
            )
            return 2
        client: Client | None = None
        try:
            home = workers_home()
            port, _fresh = ensure_server(session_dir, home)
            released = _release_other_worker_writers(session_dir, name, state.thread_id, port)
            if not isinstance(released, _ReleasedWorkerThread):
                line = f"{name}: {released.reason}; compact refused; follow-up checks release again"
                _post_board_status(session_dir, name, line)
                print(f"codex_mesh: {line}", file=sys.stderr)
                return COMPACT_TURN_STILL_LIVE if isinstance(released, WorkerWaitingOnLiveTurn) else 2
            client = Client(port, f"compact-{name}")
            _ = _require(
                client.call("thread/resume", _resume_params(session_dir, name, state.thread_id)),
                "thread/resume",
            )
            # A finished worker with a live turn is finishing one nobody waits on; give it time to end.
            live = _wait_for_idle(client, state.thread_id)
            if isinstance(live, ThreadStateUnknown):
                print(f"codex_mesh: {name}: {live.reason}; compact refused", file=sys.stderr)
                return 2
            if not isinstance(live, ThreadIdle):
                print(f"codex_mesh: {name}: thread has a live turn; compact refused", file=sys.stderr)
                return COMPACT_TURN_STILL_LIVE
            try:
                after = _compact_thread(client, state.thread_id)
            except CompactionTimedOut as timeout:
                return _abandon_compaction(client, name, state.thread_id, timeout)
            size: RecordedContextSize = after if after.tokens > 0 else NotMeasuredSinceCompaction()
            compacted_record: FollowableRecord = {"thread_id": state.thread_id, "status": state.status}
            if isinstance(size, KnownContextSize):
                compacted_record["context_tokens"] = size.tokens
            else:
                compacted_record["context_tokens"] = "not_measured_since_compaction"
            if not _update_roster_if_expected(
                session_dir,
                name,
                compacted_record,
                RosterUnchanged(state.thread_id, revision),
            ):
                current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                detail = (f"status={current.get('status', 'missing')}, thread={session_staff.worker_session(current)}, "
                          + f"turn={current.get('turn_id', 'none')}, revision={_roster_revision(current).value}")
                print(f"codex_mesh: {name}: compacted, but the roster changed during compaction ({detail}); "
                      + "follow-up not sent; inspect the worker and use reopen if its thread is idle", file=sys.stderr)
                return COMPACT_ROSTER_CHANGED
            if isinstance(size, KnownContextSize):
                print(f"compacted {name}: {before.tokens} -> {size.tokens} tokens")
            else:
                print(f"compacted {name}: {before.tokens} tokens, size not measured until its next turn")
            return 0
        except worker_admission.AdmissionRefused as error:
            print(str(error), file=sys.stderr)
            return 2
        except (ConnectionError, OSError, SystemExit) as exc:
            print(f"codex_mesh: {name}: {exc}", file=sys.stderr)
            return 1
        finally:
            if client is not None:
                client.close()


def _launcher_died(state: RosterThread) -> bool:
    """True when a live status names a launcher that is gone, which leaves the worker followable."""
    return isinstance(state, (ActiveThread, StartingThread, WaitingCapacityThread)) and (
        state.launcher_pid > 0 and not _pid_alive(state.launcher_pid))


def _follow_refusal(session_dir: str, state: RosterThread, *, name: str) -> str:
    if isinstance(state, InvalidThread):
        return state.reason
    stale_launcher = _launcher_died(state)
    if not isinstance(state, FollowableThread) and not stale_launcher:
        return "worker is busy or not followable"
    del name
    availability = _server_availability(session_dir)
    if isinstance(availability, ServerRestartRequired) and availability.doubt:
        return f"no live server evidence: {availability.doubt}; pending work kept; repair server evidence before recovery"
    if isinstance(availability, ServerRestartRequired):
        if stale_launcher:
            return "launcher died; no live server can prove the thread idle; pending work kept; use reopen to recover"
        return ""
    try:
        probe = Client(availability.port, f"follow-check-{os.getpid()}",
                       deadline=time.time() + THREAD_RELEASE_TIMEOUT_SECS)
        try:
            live = _read_live_turn(probe, state.thread_id)
        finally:
            probe.close()
    except (ConnectionError, OSError, SystemExit) as exc:
        return ("launcher died; " if stale_launcher else "") + f"thread/read failed: {exc}; use follow or reopen once idle"
    if isinstance(live, ThreadIdle):
        return ""
    if stale_launcher and isinstance(live, ThreadStateUnknown) and (
            any(port != availability.port for port in _run_servers(session_dir))):
        # Missing or stale roster port telemetry cannot establish placement.
        # A recorded giving server permits claiming and resuming
        # the exact thread through the same retired-writer release/idle checks
        # An unknown read does not prove that work stopped.
        return ""
    reason = live.reason if isinstance(live, ThreadStateUnknown) else "live turn"
    return ("launcher died; " if stale_launcher else "") + reason + "; pending work kept; use follow or reopen once idle"


def command_can_follow(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    role = _worker_role(args)
    try:
        state = _lookup_state(session_dir, name)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    reason = _follow_refusal(session_dir, state, name=name)
    if reason:
        print(f"codex_mesh: {name}: {reason}; follow refused", file=sys.stderr)
        return 2
    claim_pid = _as_int(_attr(args, "claim_pid"))
    if claim_pid:
        assert not isinstance(state, InvalidThread)
        if not _claim_follow(session_dir, name, state.thread_id, claim_pid, role, _dispatch_lens(args)):
            print(f"codex_mesh: {name} was already claimed", file=sys.stderr)
            return 2
    return 0


def command_release_follow(args: argparse.Namespace) -> int:
    _restore_follow_claim(_as_str(_attr(args, "session_dir")),
                          _as_str(_attr(args, "to")), _as_int(_attr(args, "claim_pid")))
    return 0


def _run_worker(args: argparse.Namespace, port: int, *, started_here: bool = False,
                admission: WorkerAdmission | IndependentWorkerAdmission = INDEPENDENT_WORKER_ADMISSION,
                staff_admission: WorkerStartAllowed) -> RunOutcome:
    """Attach one worker to the app-server on `port` and run it to its end."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = staff_admission.name
    prompt_path = Path(_as_str(_attr(args, "prompt_file")))
    request = worker_admission.assignment_id(Path(_as_str(_attr(args, "assignment_file")) or prompt_path))
    args.switch_request = request
    previous = worker_admission.dispatch_record(staff_admission.run, staff_admission.name)
    if previous.assignment_id == request and previous.state is worker_admission.SwitchState.ASSIGNMENT_ACCEPTED:
        print("unchanged")
        return RunCompleted()
    timeout = _as_float(_attr(args, "timeout"), 86400.0)
    opened = time.time()
    try:
        with contextlib.ExitStack() as dispatch_lock:
            _ = dispatch_lock.enter_context(worker_admission.worker_operation(staff_admission))
            return _attach_and_run(args, port, session_dir, name, timeout, opened, started_here=started_here, admission=admission, staff_admission=staff_admission, dispatch_lock=dispatch_lock)
    except worker_admission.AdmissionRefused as error:
        print(str(error), file=sys.stderr)
        return WorkerLaunchRefused()
    except (ConnectionError, OSError, SystemExit) as exc:
        return FailedBeforeThread(str(exc) or exc.__class__.__name__, time.time() - opened,
            exc.serving if isinstance(exc, ServingRequestFailure) else NotBoundCodexServer(),
            exc.reported if isinstance(exc, ServingRequestFailure) else False)


def _thread_start_params(args: argparse.Namespace, cwd: Path) -> dict[str, object]:
    """The `thread/start` request for one worker."""
    params: dict[str, object] = {
        "cwd": _as_str(_attr(args, "cwd")) or str(cwd),
        "approvalPolicy": "never",
        # The SandboxMode string, not a SandboxPolicy object: the server rejects
        # {"type": "dangerFullAccess"} with "unknown variant `type`".
        "sandbox": _as_str(_attr(args, "sandbox")),
        # Deliberately NOT ephemeral, unlike `codex exec --ephemeral`. An
        # ephemeral thread refuses `thread/queue/add` outright ("ephemeral thread
        # does not support queued submissions"), which is the one call peers use
        # most -- so ephemerality would cost the mesh the thing it exists for.
        # The price is the usual codex rollout file under ~/.codex/sessions.
    }
    model = _as_str(_attr(args, "model"))
    if model:
        params["model"] = model
    # Empty inherits `service_tier` from ~/.codex/config.toml, as `codex exec`
    # does. The server takes the config spelling and maps it itself ("fast"
    # starts the thread on "priority").
    service_tier = _as_str(_attr(args, "service_tier"))
    if service_tier:
        params["serviceTier"] = service_tier
    return params


def _cleanup_created_thread(client: Client, session_dir: str, identity: str) -> str:
    released = _release_worker_thread(client, identity, session_dir)
    return (f"; cleanup unproved: {released.reason}" if isinstance(released, WorkerThreadStateUnknown)
            else "; created thread stopped")


def _thread_id(result: dict[str, object]) -> str:
    return _as_str(_as_dict(result.get("thread")).get("id"))


def _create_published_worker(client: Client, session_dir: str, allowed: WorkerStartAllowed,
                             method: str, params: dict[str, object], *, request: str = "",
                             submits_work: bool = False,
                             stopped: RefusalCommandContext = REFUSAL_WITHOUT_COMMAND_EXIT,
                             ) -> worker_admission.PublishedWorker:
    def start(handoff: worker_admission.FamilyHandoff) -> str:
        if not submits_work and isinstance(handoff, worker_admission.TranscriptHandoff):
            params["developerInstructions"] = handoff.opening(str(params.get("developerInstructions", "")))
        with worker_lifecycle.lock_worker(session_dir, allowed.name):
            result = _serving_request(client, method, params, submits_work=submits_work, stopped=stopped)
            return _thread_id(result)

    return worker_admission.switch_worker(allowed, "codex", start=start,
        dispatch=worker_admission.DispatchOwner.TURN_SUBMISSION,
        cleanup=lambda identity: _cleanup_created_thread(client, session_dir, identity), request=request)


def _attach_and_run(
    args: argparse.Namespace,
    port: int,
    session_dir: str,
    name: str,
    timeout: float,
    opened: float,
    *, staff_admission: WorkerStartAllowed, started_here: bool = False,
    admission: WorkerAdmission | IndependentWorkerAdmission = INDEPENDENT_WORKER_ADMISSION,
    dispatch_lock: contextlib.ExitStack | None = None,
) -> RunOutcome:
    if staff_admission.name != name:
        raise ValueError("staff admission names another worker")
    prompt = Path(_as_str(_attr(args, "prompt_file"))).read_text(encoding="utf-8")
    log_path = Path(_as_str(_attr(args, "log_file")))

    client: Client | None = None
    try:
        with worker_lifecycle.membership_operation(session_dir), contextlib.ExitStack() as lifecycle:
            membership_record = staff.local_snapshot()
            _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
            existing = _read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)
            if isinstance(existing, dict):
                if _as_dict(cast(object, existing)).get("held_design_id"):
                    if isinstance(staff_admission.slot, worker_admission.ExistingWorkerSlot):
                        raise SystemExit("codex_mesh: held roles must resume through their holder")
                    print(f"anomaly: {name}: ended held role has no staff row", file=sys.stderr)
            availability = _server_availability(session_dir)
            if isinstance(availability, ServerRestartRequired) and availability.doubt:
                print(f"codex_mesh: {name}: server evidence unavailable: {availability.doubt}; launch refused", file=sys.stderr)
                return WorkerLaunchRefused()
            old_port = availability.port if isinstance(availability, LiveServer) and not started_here else None
            if isinstance(existing, dict):
                old_entry = _as_dict(cast("object", existing))
                reason = _relaunch_refusal(name, old_entry, membership_record)
                if reason:
                    print(f"codex_mesh: {name}: {reason}", file=sys.stderr)
                    return WorkerLaunchRefused()
                old_thread = session_staff.codex_thread(old_entry, membership_record)
                if old_thread:
                    if old_entry.get("status") == "failed":
                        # A failed entry is always its launcher's last write.
                        if old_port is not None:
                            cleanup = _end_unwatched_turn(
                                old_port, old_thread, Path(_as_str(_attr(args, "log_file")))
                            )
                            if isinstance(cleanup, RelaunchBlockedByLiveTurn):
                                if _pid_alive(_as_int(old_entry.get("launcher_pid"))):
                                    print(
                                        f"codex_mesh: {name}: thread {old_thread} still has a live turn that could not be interrupted; relaunch once it ends",
                                        file=sys.stderr,
                                    )
                                    return WorkerLaunchRefused()
                                print(
                                    f"codex_mesh: {name}: thread {old_thread} has a stale turn with no live launcher; interrupt not confirmed; continuing relaunch",
                                    file=sys.stderr,
                                )
                        old_thread = ""
                    elif old_entry.get("status") == WAITING_CAPACITY:
                        # A dead waiting launcher cannot resume this thread.
                        old_thread = ""
                if old_thread and old_port is not None:
                    try:
                        check = Client(old_port, f"relaunch-{os.getpid()}")
                        try:
                            live = _read_live_turn(check, old_thread)
                        finally:
                            check.close()
                    except (ConnectionError, OSError, SystemExit) as exc:
                        live = ThreadStateUnknown(str(exc) or exc.__class__.__name__)
                    if isinstance(live, ThreadStateUnknown):
                        if _pid_alive(_as_int(old_entry.get("launcher_pid"))):
                            print(
                                f"codex_mesh: {name}: thread {old_thread}: thread state unknown ({live.reason}); "
                                + f"recorded launcher is alive; use codex_mesh.py end --to {name} before relaunching",
                                file=sys.stderr,
                            )
                            return WorkerLaunchRefused()
                        cleanup = _end_unwatched_turn(old_port, old_thread, log_path)
                        detail = ("interrupt requested" if cleanup.interrupt_requested
                                  else "cleanup attempted; interrupt not confirmed")
                        print(
                            f"codex_mesh: {name}: thread {old_thread}: thread state unknown ({live.reason}); "
                            + f"{detail}; continuing relaunch",
                            file=sys.stderr,
                        )
                    elif not isinstance(live, ThreadIdle):
                        if _pid_alive(_as_int(old_entry.get("launcher_pid"))):
                            print(
                                f"codex_mesh: {name}: thread {old_thread} has a live turn; use codex_mesh.py end --to {name} before relaunching",
                                file=sys.stderr,
                            )
                            return WorkerLaunchRefused()
                        cleanup = _end_unwatched_turn(old_port, old_thread, log_path)
                        detail = ("interrupt requested" if cleanup.interrupt_requested
                                  else "cleanup attempted; interrupt not confirmed")
                        print(f"codex_mesh: {name}: thread {old_thread} has a stale turn with no live launcher; "
                              + f"{detail}; continuing relaunch", file=sys.stderr)
            client = Client(port, name)
            _ = _bind_serving_server(client, session_dir, started_here=started_here)
            lifecycle.close()
            published = _create_published_worker(client, session_dir, staff_admission, "thread/start",
                _thread_start_params(args, session_staff.worker_directory(membership_record, staff_admission.parent)),
                submits_work=True, stopped=CommandStoppedBeforeThread(session_dir),
                request=_as_str(_attr(args, "switch_request")))
            _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
            thread_id = published.row.current_session
            if isinstance(published.handoff, worker_admission.TranscriptHandoff):
                prompt = published.handoff.opening(prompt)
            name = published.row.name
            with _pending_file(session_dir, name) as pending_file:
                previous = _as_dict(cast(object, existing))
                previous_thread = session_staff.worker_session(previous, membership_record)
                if previous_thread and previous_thread != thread_id:
                    carried = _pending_messages(pending_file)
                    _write_pending(pending_file, carried,
                                   accepting_messages=not _messages_closed(pending_file))
                starting: StartingRecord = {
                    "staff_id": published.row.id,
                    "model": _as_str(_attr(args, "model")), "effort": _as_str(_attr(args, "effort")),
                    "thread_id": thread_id,
                    "status": "starting",
                    "port": port,
                    "launcher_pid": os.getpid(),
                }
                task = _as_str(_attr(args, "task"))
                if task:
                    starting["task"] = task
                role = _roster_role(_attr(args, "role"))
                if isinstance(role, AssignedWorkerRole):
                    starting["role"] = role.role
                expected: ExpectedRoster = (RosterUnchanged(previous_thread, _roster_revision(previous))
                                            if previous else RosterAbsent())
                if (previous_thread and previous_thread != thread_id
                        or not isinstance(published.handoff, worker_admission.NoFamilyHandoff)):
                    expected = RosterBackendReplaced(published.row.id, thread_id,
                        _roster_revision(previous), _as_str(previous.get("status")))
                if not _update_roster_if_expected(session_dir, name, starting, expected):
                    raise SystemExit(f"codex_mesh: {name}: roster changed while opening the worker; launch refused")
                if isinstance(role, AssignedWorkerRole):
                    _record_assignment(thread_id, role.role, _dispatch_lens(args))

        admission.published()
    except BaseException:
        if client is not None:
            client.close()
        raise
    assert client is not None
    return _stream_turn(args, client, port, session_dir, name, thread_id, prompt, timeout, opened, dispatch_lock=dispatch_lock)


def _resume_account_move(
    args: argparse.Namespace,
    session_dir: str,
    name: str,
    thread_id: str,
    client: Client,
    marker: AccountMoveMarker,
    *, submission: QuotaContinuationTurn | UnrelatedWorkerTurn = UNRELATED_WORKER_TURN,
) -> MovedWorker | WorkerWaitingForRoom:
    """Resume the marked worker and acknowledge its accepted continuation."""
    try:
        resumed = _start_workers_turn(
            args, session_dir, name, thread_id, client, ACCOUNT_MOVE_CONTINUATION, submission=submission)
        if isinstance(resumed, WorkerWaitingForRoom):
            return resumed
        if not resumed.turn_id:
            raise SystemExit("account move continuation returned no turn id")
        with worker_lifecycle.lock_worker(session_dir, name):
            if not _update_roster_if_expected(
                session_dir,
                name,
                {"thread_id": resumed.thread_id, "turn_id": resumed.turn_id,
                 "status": "running", "port": resumed.port},
                RosterActiveLauncher(resumed.thread_id, (client.launcher_pid, os.getpid())),
            ):
                raise SystemExit(f"codex_mesh: {name}: roster ownership changed during account move; continuation not acknowledged")
            _finish_account_move(session_dir, name, marker)
        return resumed
    except (ConnectionError, OSError, SystemExit) as error:
        _fail_account_move(session_dir, name, marker, f"resume failed: {error}")
        raise


@dataclass
class TurnStream:
    """What one `_stream_turn` call carries from frame to frame."""

    args: argparse.Namespace
    session_dir: str
    name: str
    follow: bool
    resident: bool
    timeout: float
    opened: float
    deadline: float
    log_path: Path
    summary_path: Path
    reply_path: Path | None
    end_marker: Path
    director: KnownWorkerDirector | UnknownWorkerDirector
    client: Client
    port: int
    thread_id: str
    failure_serving: CodexServingState
    runs: DetachedRunWatch
    turn_id: str = ""
    # Whether a notice for `turn_id` arrived; a turn this launcher just submitted has none yet.
    turn_start_seen: bool = False
    # The turn a follow watches; empty between its turns.
    followed_turn: str = ""
    final_answer: str = ""
    failure: str = ""
    followed_failure: str = ""
    replies: int = 0
    capacity_waited: float = 0.0
    capacity_retries: int = 0
    resume_owed: bool = False
    # A peer turn or an actual backoff satisfies the refused turn's wait.
    capacity_backoff_owed: bool = False
    # Whether the owed capacity resume continues an earlier quota continuation.
    capacity_submission: QuotaContinuationTurn | UnrelatedWorkerTurn = UNRELATED_WORKER_TURN
    waiting_for_progress_after_overflow: bool = False
    run_outcome: RunOutcome = field(default_factory=RunCompleted)
    pending_refusal: PendingQuotaRefusal | NoPendingQuotaRefusal = field(default_factory=NoPendingQuotaRefusal)
    reported_turns: set[tuple[CodexServingState, str, str]] = field(default_factory=set)
    room_wait_logged: bool = False
    events: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT
    state_change_owed: bool = False
    # A prior launcher can remain subscribed after another process settled its work.
    ownership_released: bool = False
    ownership_loss: WorkerOwnershipLost = field(default_factory=WorkerOwnershipLost)
    completed_turns: set[tuple[str, str]] = field(default_factory=set)


class StreamStep(Enum):
    """What the frame loop does after one step."""

    NEXT_FRAME = "next_frame"
    FINISH = "finish"


def _quota_retry_submission(
    pending: PendingQuotaRefusal | NoPendingQuotaRefusal, thread_id: str, turn_id: str,
) -> QuotaContinuationTurn | UnrelatedWorkerTurn:
    if (isinstance(pending, PendingQuotaRefusal)
            and isinstance(pending.continuation, SubmittedQuotaContinuation)
            and pending.continuation.thread_id == thread_id and pending.continuation.turn_id == turn_id):
        return QuotaContinuationTurn()
    return UNRELATED_WORKER_TURN


def _pending_after_worker_start(
    pending: PendingQuotaRefusal | NoPendingQuotaRefusal, started: MovedWorker,
) -> PendingQuotaRefusal | NoPendingQuotaRefusal:
    if isinstance(started.pending_refusal, PendingQuotaRefusal):
        pending = started.pending_refusal
    if (isinstance(pending, PendingQuotaRefusal) and started.turn_id
            and isinstance(started.submission, QuotaContinuationTurn)):
        return replace(pending, continuation=SubmittedQuotaContinuation(started.thread_id, started.turn_id))
    return pending


def _quota_continuation_finished(client: Client, thread_id: str, turn_id: str) -> bool:
    """An idle read-back must confirm this continuation completed successfully."""
    try:
        reply = client.call("thread/read", {"threadId": thread_id, "includeTurns": False}, UNSEEN_END_CALL_SECS)
        if reply.get("error"):
            return False
        thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
        if _as_str(_as_dict(thread.get("status")).get("type")) != "idle":
            return False
        turns = _recent_turns(client, thread_id, UNSEEN_END_CALL_SECS)
    except (ConnectionError, OSError, SystemExit):
        return False
    return any(turn.get("id") == turn_id and turn.get("status") == "completed" for turn in turns)


def _pending_after_completion_readback(
    pending: PendingQuotaRefusal | NoPendingQuotaRefusal, client: Client,
) -> PendingQuotaRefusal | NoPendingQuotaRefusal:
    if (isinstance(pending, PendingQuotaRefusal)
            and isinstance(pending.continuation, SubmittedQuotaContinuation)
            and _quota_continuation_finished(client, pending.continuation.thread_id, pending.continuation.turn_id)):
        return NoPendingQuotaRefusal()
    return pending


def _await_preserved_results(stream: TurnStream) -> None:
    """A carried command finishes before its worker receives the next assignment."""
    deferred: list[RpcMessage] = []
    expires = time.monotonic() + max(0.0, stream.deadline - time.time())
    try:
        stream.runs.refresh(force=True)
        while stream.runs.last_observed.live or stream.runs.last_observed.dead:
            if time.monotonic() >= expires:
                raise SystemExit("carried detached run is still live at the assignment timeout")
            _end_dead_runs(stream.session_dir, stream.name, stream.runs)
            # File changes and the socket wake this read. A 60-second safety
            # scan discovers a supervisor lost without delivering its result.
            stream.client.wake = stream.events
            frame = stream.client.next_frame(time.time() + min(
                DETACHED_RUN_CHECK_SECS, max(0.0, expires - time.monotonic())))
            if frame is not None:
                deferred.append(frame)
                stream.runs.refresh()
            else:
                stream.runs.refresh(force=True)
            if stream.end_marker.exists():
                raise SystemExit("worker ended while its carried command was running")
    finally:
        for frame in reversed(deferred):
            stream.client.push_back(frame)


def _stream_turn(
    args: argparse.Namespace,
    client: Client,
    initial_port: int,
    session_dir: str,
    name: str,
    initial_thread_id: str,
    prompt: str,
    timeout: float,
    opened: float,
    *,
    follow: bool = False,
    dispatch_lock: contextlib.ExitStack | None = None,
) -> RunOutcome:
    """Stream a new or resumed thread's turn through the same lifecycle."""
    client.session_dir = session_dir
    client.launcher_pid = _as_int(_attr(args, "claim_pid")) or os.getpid()
    log_path = Path(_as_str(_attr(args, "log_file")))
    summary_path = Path(_as_str(_attr(args, "summary_file")))
    reply_file = _as_str(_attr(args, "reply_file"))
    resident = getattr(args, "resident", False) is True
    end_marker = _end_marker_path(session_dir, name)
    director = UnknownWorkerDirector() if follow else _worker_director(Path(session_dir))
    failure_serving = client.serving
    stream = TurnStream(
        args=args, session_dir=session_dir, name=name, follow=follow, resident=resident,
        timeout=timeout, opened=opened, deadline=time.time() + timeout,
        log_path=log_path, summary_path=summary_path,
        reply_path=Path(reply_file) if reply_file else None,
        end_marker=end_marker, director=director,
        client=client, port=initial_port, thread_id=initial_thread_id, failure_serving=failure_serving,
        runs=DetachedRunWatch(session_dir, name),
    )
    with worker_lifecycle.lock_worker(session_dir, name):
        record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if not _stream_launcher(stream).owns(record):
            return WorkerOwnershipLost()
        end_marker.unlink(missing_ok=True)
        with _pending_file(session_dir, name) as handle:
            _write_pending(handle, _pending_messages(handle))
    try:
        with contextlib.closing(FollowEvents(session_dir, name)) as events, log_path.open("a", encoding="utf-8") as log:
            stream.events = events
            _ = log.write(f"[{_now_stamp()}] mesh: {name} thread {stream.thread_id}\n")
            log.flush()
            prior = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            if prior.get("preserved_work"):
                _await_preserved_results(stream)
            started = _start_streamed_turn(stream, prompt, log)
            if dispatch_lock is not None:
                dispatch_lock.close()
            stopped = _stream_frames(stream, log) if started is StreamStep.NEXT_FRAME else None
        if stopped is not None:
            return stopped
        return _finish_stream(stream)
    except (ConnectionError, OSError, SystemExit) as exc:
        return _fail_stream(stream, exc)
    finally:
        _close_stream_inbox(stream)
        stream.client.close()


def _start_streamed_turn(stream: TurnStream, prompt: str, log: TextIO) -> StreamStep:
    """Name the thread, submit its first turn and mark it running on the roster."""
    # The roster is written before this call: a disconnect after thread
    # creation must still leave its id reachable.
    with worker_lifecycle.lock_worker(stream.session_dir, stream.name):
        if _stream_was_released(stream):
            return StreamStep.FINISH
        _ = stream.client.call("thread/name/set", {"threadId": stream.thread_id,
                                                   "name": unit_workers.shown_name(Path(stream.session_dir), stream.name,
                                                       role=_as_str(_attr(stream.args, "role")))})
    initial_move = (_read_account_move(stream.session_dir, stream.name, stream.thread_id)
                    if _attr(stream.args, "account_move") is True else NoAccountMove())
    started = _start_workers_turn(
        stream.args, stream.session_dir, stream.name, stream.thread_id, stream.client, prompt)
    if isinstance(started, WorkerWaitingForRoom):
        return _wait_for_room(stream, log, started)
    _adopt_started_turn(stream, started)
    if not stream.turn_id:
        raise SystemExit("codex_mesh: turn/start returned no turn id")
    if isinstance(initial_move, AccountMoveMarker):
        _finish_account_move(stream.session_dir, stream.name, initial_move)
    if not _record_stream_state(stream, {
            "thread_id": stream.thread_id,
            "turn_id": stream.turn_id,
            "status": "running",
            "port": stream.port,
        }):
        return StreamStep.FINISH
    return StreamStep.NEXT_FRAME


def _stream_frames(stream: TurnStream, log: TextIO) -> FailedWithThread | None:
    """Handle frames until the run finishes or its deadline passes.

    Returns the outcome of a capacity launcher that stopped without a resume turn.
    """
    run_watch = stream.runs
    run_watch.refresh()
    next_check = time.monotonic() + UNSEEN_END_CHECK_SECS
    expires = time.monotonic() + max(0.0, stream.deadline - time.time())
    next_role_check = (time.monotonic() + REMOTE_ROLE_CHECK_SECS
                       if stream.resident and not is_role_map_host() else expires)
    while time.monotonic() < expires:
        # File events wake immediately; the one-minute deadline only repairs
        # missing server notices and checks detached supervisors. Streaming
        # output cannot postpone those checks indefinitely.
        # Run maintenance never waits for notices to drain. In particular its
        # next deadline cannot remain expired while bytes await a socket read.
        runs_due = time.monotonic() >= run_watch.next_check
        if runs_due:
            _end_dead_runs(stream.session_dir, stream.name, run_watch)
        if time.monotonic() >= next_role_check:
            _check_remote_role(stream, log)
            next_role_check = time.monotonic() + REMOTE_ROLE_CHECK_SECS
        if (time.monotonic() >= next_check or runs_due
                or (stream.state_change_owed and not stream.client.has_pending_frames)):
            step = _on_quiet_poll(stream, log)
            next_check = time.monotonic() + UNSEEN_END_CHECK_SECS
            if isinstance(step, FailedWithThread):
                return step
            if step is StreamStep.FINISH:
                break
        stream.client.wake = stream.events
        read_until = min(expires, next_check, run_watch.next_check, next_role_check)
        frame = stream.client.next_frame(time.time() + max(0.0, read_until - time.monotonic()))
        if frame is None:
            if time.monotonic() >= next_role_check:
                # A remote role tick performs no thread/read or run scan.
                continue
            stream.state_change_owed = True
        step = _on_quiet_poll(stream, log) if frame is None else _on_frame(stream, log, frame)
        if frame is None and time.monotonic() >= next_check:
            next_check = time.monotonic() + UNSEEN_END_CHECK_SECS
        if isinstance(step, FailedWithThread):
            return step
        if step is StreamStep.FINISH:
            break
    else:
        stream.failure = f"no turn/completed within {stream.timeout:.0f}s"
    return None


def _adopt_started_turn(stream: TurnStream, started: MovedWorker) -> None:
    """Watch the server, thread and turn a submission landed on."""
    stream.client, stream.port = started.client, started.port
    stream.room_wait_logged = False
    stream.thread_id, stream.turn_id = started.thread_id, started.turn_id
    stream.turn_start_seen = False
    stream.pending_refusal = _pending_after_worker_start(stream.pending_refusal, started)
    if stream.follow:
        stream.followed_turn = stream.turn_id


def _adopt_moved_worker(stream: TurnStream, moved: MovedWorker) -> bool:
    """Watch the server a move landed on; true when it left a turn or a continuation to watch."""
    stream.client, stream.port, stream.thread_id = moved.client, moved.port, moved.thread_id
    stream.room_wait_logged = False
    stream.pending_refusal = _pending_after_worker_start(stream.pending_refusal, moved)
    if not (moved.turn_id or _continuation_is_owed(stream.session_dir, stream.name)):
        return False
    if moved.turn_id:
        _mesh_phase_wait(stream.session_dir, stream.name, stream.thread_id, "WaitingForQuotaRefusal", finished=True)
        _mesh_phase_wait(stream.session_dir, stream.name, stream.thread_id, "ReadyWithoutCapacity", finished=True)
        stream.turn_id, stream.turn_start_seen = moved.turn_id, False
    if stream.follow:
        stream.followed_turn = stream.turn_id
    return True


def _move_worker(stream: TurnStream) -> WorkerMoveResult:
    """Move the worker to the selected workers home and keep any quota refusal it reports.

    A move that may have started a turn it cannot account for stops the run.
    """
    moved = _move_resident(stream.args, stream.session_dir, stream.name, stream.thread_id, stream.client)
    if isinstance(moved, WorkerAwaitingQuotaRecovery):
        stream.pending_refusal = PendingQuotaRefusal(moved.refusal.serving, str(moved.refusal))
    if isinstance(moved, WorkerThreadStateUnknown) and isinstance(moved.cleanup, WorkerTurnCleanup):
        raise WorkerMoveUncertain(stream.thread_id, moved)
    return moved


def _resume_marked_account_move(stream: TurnStream, log: TextIO, marker: AccountMoveMarker) -> StreamStep:
    """Open the marked move's continuation and forget the answer of the turn it replaced."""
    resumed = _resume_account_move(
        stream.args, stream.session_dir, stream.name, stream.thread_id, stream.client, marker,
        submission=_quota_retry_submission(stream.pending_refusal, stream.thread_id, marker.turn_id))
    if isinstance(resumed, WorkerWaitingForRoom):
        return _wait_for_room(stream, log, resumed)
    _adopt_started_turn(stream, resumed)
    stream.failure = stream.followed_failure = stream.final_answer = ""
    return StreamStep.NEXT_FRAME


def _hold_messages_for_next_launch(stream: TurnStream, log: TextIO, reason: str) -> None:
    """Close the thread to messages and keep the held ones for the next launch."""
    if stream.end_marker.exists():
        return
    kept = _close_messages_and_keep_pending(stream.session_dir, stream.name, _stream_launcher(stream))
    if kept:
        messages = "1 message" if kept == 1 else f"{kept} messages"
        held = f"{messages} held for the next launch"
        _post_board_status(stream.session_dir, stream.name, f"launcher: {held}: {reason}")
        _ = log.write(f"[{_now_stamp()}] mesh: {stream.name} cannot move: {reason}; {held}\n")
        log.flush()


def _wait_for_room(stream: TurnStream, log: TextIO, waiting: WorkerWaitingForRoom) -> StreamStep:
    """Keep the task owed without watching an idle thread as a live turn."""
    _mesh_phase_wait(stream.session_dir, stream.name, waiting.thread_id, "ReadyWithoutCapacity", waiting.reason)
    if stream.client is not waiting.client or stream.thread_id != waiting.thread_id:
        stream.room_wait_logged = False
    stream.client, stream.port, stream.thread_id = waiting.client, waiting.port, waiting.thread_id
    account = stream.client.serving.account
    label = account.label if isinstance(account, KnownCodexAccount) else "unknown account"
    line = f"{stream.name} waits for room on {label}: {waiting.reason}; the continuation stays owed"
    if not stream.room_wait_logged:
        _ = log.write(f"[{_now_stamp()}] mesh: {line}\n")
        log.flush()
        stream.room_wait_logged = True
    if stream.resident:
        return StreamStep.NEXT_FRAME
    _hold_messages_for_next_launch(stream, log, waiting.reason)
    # Keep recovery here. A stopped-command handoff would clear continuation_owed.
    stream.pending_refusal = NoPendingQuotaRefusal()
    stream.failure = line
    return StreamStep.FINISH


def _on_quiet_poll(stream: TurnStream, log: TextIO) -> StreamStep | FailedWithThread:
    """A file changed or the safety deadline passed: check lifecycle and owed work."""
    # Notifications can be in the WebSocket buffer after an RPC reply, rather
    # than Client's notice queue. Drain both before a readback can infer idle or
    # consume a queued peer's completion. Keep the file wake owed until then.
    _end_dead_runs(stream.session_dir, stream.name, stream.runs)
    if stream.client.has_pending_frames:
        stream.state_change_owed = True
        return StreamStep.NEXT_FRAME
    stream.state_change_owed = False
    if _stream_was_released(stream):
        return StreamStep.FINISH
    if not stream.resume_owed:
        stream.pending_refusal = _pending_after_completion_readback(stream.pending_refusal, stream.client)
    account_move = _read_account_move(stream.session_dir, stream.name, stream.thread_id)
    if (isinstance(account_move, AccountMoveMarker) and not stream.end_marker.exists()
            and _ended_unseen(stream.client, stream.thread_id)):
        return _resume_marked_account_move(stream, log, account_move)
    if stream.resume_owed:
        return _resume_owed_turn(stream, log, refused_for_capacity=stream.capacity_backoff_owed)
    # The frame loop bounds the run; a wall-clock jump past the deadline must
    # not leave a one-turn launcher holding results it never checks again.
    if not stream.resident:
        return _finish_if_ended_unseen(stream, log)
    if stream.end_marker.exists():
        return StreamStep.FINISH
    if stream.resident:
        entry = _as_dict(_read_json_object(_session_path(stream.session_dir, ROSTER_FILE)).get(stream.name))
        if (finished_run_status(stream.session_dir) or entry.get("status") in ("done", "failed", "paused", "ended")):
            if _ended_unseen(stream.client, stream.thread_id):
                if stream.client.has_pending_frames:
                    stream.state_change_owed = True
                    return StreamStep.NEXT_FRAME
                if _close_messages_if_empty(stream.session_dir, stream.name, _stream_launcher(stream), stream.runs):
                    return StreamStep.FINISH
        _keep_resident_on_selected_server(stream, log)
    return StreamStep.NEXT_FRAME


def _finish_if_ended_unseen(stream: TurnStream, log: TextIO) -> StreamStep:
    """Finish a one-turn launcher whose thread went idle without a completion notice."""
    _end_dead_runs(stream.session_dir, stream.name, stream.runs)
    if _waiting_on_detached_run(stream.session_dir, stream.name, stream.runs):
        return StreamStep.NEXT_FRAME
    quota_owed = _continuation_is_owed(stream.session_dir, stream.name)
    if stream.resume_owed:
        return StreamStep.NEXT_FRAME
    if not _ended_unseen(stream.client, stream.thread_id, stream.turn_id, start_seen=stream.turn_start_seen):
        return StreamStep.NEXT_FRAME
    # The read-back RPC may have received the completion or answer ahead of
    # its reply. Process those frames before inferring a missing notice.
    if stream.client.has_pending_frames:
        stream.state_change_owed = True
        return StreamStep.NEXT_FRAME
    # A move can write its marker and interrupt after the first marker read.
    if (isinstance(_read_account_move(stream.session_dir, stream.name, stream.thread_id), AccountMoveMarker)
            and not stream.end_marker.exists()):
        return StreamStep.NEXT_FRAME
    moved = _move_worker(stream)
    if isinstance(moved, WorkerWaitingForRoom):
        return _wait_for_room(stream, log, moved)
    if isinstance(moved, WorkerThreadStateUnknown):
        _hold_messages_for_next_launch(stream, log, moved.reason)
    else:
        if quota_owed and isinstance(moved, WorkerWaitingOnLiveTurn):
            return StreamStep.NEXT_FRAME
        if isinstance(moved, MovedWorker) and _adopt_moved_worker(stream, moved):
            return StreamStep.NEXT_FRAME
        if not _close_messages_if_empty(stream.session_dir, stream.name, _stream_launcher(stream), stream.runs):
            return StreamStep.NEXT_FRAME
    if quota_owed:
        return StreamStep.NEXT_FRAME
    _ = log.write(f"[{_now_stamp()}] mesh: {stream.name} thread is idle and no "
                  + "completion notice arrived; finishing\n")
    log.flush()
    stream.failure = stream.followed_failure or stream.failure or (
        "" if stream.final_answer
        else "the thread went idle with no completion notice and no answer")
    return StreamStep.FINISH


@role_map_command
def _check_remote_role(stream: TurnStream, log: TextIO) -> None:
    """A remote role tick checks selection only; unchanged roles do no RPCs."""
    if not stream.resident:
        return
    selected_home = workers_home()
    record = _read_json_object(_session_path(stream.session_dir, SERVER_FILE))
    if server_record_home(record) != selected_home or record.get("port") != stream.port:
        _keep_resident_on_selected_server(stream, log)


def _keep_resident_on_selected_server(stream: TurnStream, log: TextIO) -> None:
    """Move an idle resident that sits off the selected server or owes work."""
    selected_home = workers_home()
    record = _read_json_object(_session_path(stream.session_dir, SERVER_FILE))
    with _pending_file(stream.session_dir, stream.name) as handle:
        held_messages = bool(_pending_messages(handle))
    if (server_record_home(record) == selected_home
            and record.get("port") == stream.port and not held_messages
            and not _continuation_is_owed(stream.session_dir, stream.name)):
        return
    moved = _move_worker(stream)
    if isinstance(moved, WorkerWaitingForRoom):
        _ = _wait_for_room(stream, log, moved)
    elif isinstance(moved, WorkerThreadStateUnknown):
        _ = log.write(f"[{_now_stamp()}] mesh: {stream.name} cannot move: {moved.reason}; waiting\n")
        log.flush()
    elif isinstance(moved, MovedWorker):
        stream.room_wait_logged = False
        changed = moved.port != stream.port or moved.thread_id != stream.thread_id
        stream.client, stream.port, stream.thread_id = moved.client, moved.port, moved.thread_id
        stream.pending_refusal = _pending_after_worker_start(stream.pending_refusal, moved)
        if moved.turn_id:
            stream.turn_id, stream.turn_start_seen = moved.turn_id, False
        if changed:
            _ = log.write(f"[{_now_stamp()}] mesh: {stream.name} moved to the workers app-server on port {stream.port}\n")
            log.flush()


def _on_frame(stream: TurnStream, log: TextIO, frame: RpcMessage) -> StreamStep | FailedWithThread:
    """Handle one frame for the watched thread; frames for other threads are skipped."""
    method = frame.get("method")
    params = _as_dict(frame.get("params"))
    if _as_str(params.get("threadId")) not in ("", stream.thread_id):
        return StreamStep.NEXT_FRAME
    if stream.ownership_released:
        return StreamStep.FINISH
    # Notices for our watched turn need no extra roster read. A maintenance
    # turn broadcast to an old subscriber has a different (or unknown) id.
    # Completed items only collect buffered output, which may omit a turn id.
    own_turn = bool(stream.turn_id and stream.turn_id in (
        _as_str(params.get("turnId")), _as_str(_as_dict(params.get("turn")).get("id"))))
    if (method in ("turn/started", "thread/tokenUsage/updated", "turn/completed", "turn/failed")
            and not own_turn and _stream_was_released(stream)):
        return StreamStep.FINISH
    if own_turn:
        stream.turn_start_seen = True
    if method == "turn/started":
        _on_turn_started(stream, params)
    elif method == "thread/tokenUsage/updated":
        _record_context_size(stream, params)
    elif method == "item/completed":
        _log_completed_item(stream, log, params)
    elif method in ("turn/completed", "turn/failed"):
        return _on_turn_end(stream, log, method, params)
    return StreamStep.NEXT_FRAME


def _stream_launcher(stream: TurnStream) -> RosterActiveLauncher:
    return RosterActiveLauncher(stream.thread_id,
                               (_as_int(_attr(stream.args, "claim_pid")) or os.getpid(), os.getpid()))


def _stream_was_released(stream: TurnStream) -> bool:
    """A subscriber may act only while its launcher still owns worker work.

    A paused, finished or newly claimed entry no longer holds this task.
    Broadcast maintenance notices must never settle or clean up that entry.
    """
    if stream.ownership_released:
        return True
    record = _as_dict(_read_json_object(_session_path(stream.session_dir, ROSTER_FILE)).get(stream.name))
    resident_done = (stream.resident and record.get("status") == "done"
                     and session_staff.worker_session(record) == stream.thread_id
                     and record.get("launcher_pid") in _stream_launcher(stream).launcher_pids)
    if not resident_done and not _stream_launcher(stream).owns(record):
        stream.ownership_loss = WorkerOwnershipLost()
        stream.ownership_released = True
    return stream.ownership_released


def _record_stream_state(stream: TurnStream, record: ThreadRecord) -> bool:
    """A failed notice write releases the subscriber, rather than being ignored."""
    with worker_lifecycle.lock_worker(stream.session_dir, stream.name):
        if stream.ownership_released:
            return False
        if not _update_roster_if_expected(stream.session_dir, stream.name, record, _stream_launcher(stream)):
            stream.ownership_released = True
            return False
        return True


def _close_stream_inbox(stream: TurnStream) -> None:
    with worker_lifecycle.lock_worker(stream.session_dir, stream.name):
        _close_stream_inbox_locked(stream)


def _close_stream_inbox_locked(stream: TurnStream) -> None:
    """Close only this launcher's inbox, excluding a concurrent follow claim."""
    if stream.end_marker.exists() or stream.ownership_released:
        return
    with _pending_file(stream.session_dir, stream.name) as handle:
        if not _stream_was_released(stream):
            _write_pending(handle, _pending_messages(handle),
                           accepting_messages=False)


def _on_turn_started(stream: TurnStream, params: dict[str, object]) -> None:
    live = _as_str(_as_dict(params.get("turn")).get("id"))
    if live and (not stream.follow or not stream.followed_turn or live == stream.followed_turn):
        stream.turn_id, stream.turn_start_seen = live, True
        if stream.resume_owed:
            stream.capacity_backoff_owed = False
        if stream.follow:
            stream.followed_turn = live
        if not _record_stream_state(stream, {"thread_id": stream.thread_id, "turn_id": live, "status": "running"}):
            stream.ownership_released = True


def _record_context_size(stream: TurnStream, params: dict[str, object]) -> None:
    context_size = _context_size_from_usage(params)
    if isinstance(context_size, KnownContextSize):
        if not _record_stream_state(stream, {
                "thread_id": stream.thread_id,
                "turn_id": stream.turn_id,
                "status": "running",
                "context_tokens": context_size.tokens,
            }):
            stream.ownership_released = True


def _log_completed_item(stream: TurnStream, log: TextIO, params: dict[str, object]) -> None:
    item = _as_dict(params.get("item"))
    _ = log.write(f"[{_now_stamp()}] {_describe_item(item)}\n")
    log.flush()
    if _as_str(item.get("type")) not in ("userMessage", "contextCompaction"):
        stream.waiting_for_progress_after_overflow = False
    if _as_str(item.get("type")) == "agentMessage":
        text = _as_str(item.get("text"))
        if text:
            stream.final_answer = text


def _on_turn_end(
    stream: TurnStream, log: TextIO, method: str, params: dict[str, object],
) -> StreamStep | FailedWithThread:
    """Handle a completed or failed turn: resume, continue, retry, reply or finish."""
    if _stream_was_released(stream):
        return StreamStep.FINISH
    notice_turn = _as_str(_as_dict(params.get("turn")).get("id"))
    account_move = _read_account_move(stream.session_dir, stream.name, stream.thread_id)
    moved_turn = isinstance(account_move, AccountMoveMarker) and notice_turn == account_move.turn_id
    if (stream.follow and notice_turn and stream.followed_turn and notice_turn != stream.followed_turn
            and not moved_turn):
        return StreamStep.NEXT_FRAME
    # The followed turn has ended. A later turn names itself in its own
    # `turn/started`; a thread read at this moment can still name this one.
    if stream.follow:
        stream.followed_turn = ""
    turn_outcome = _turn_outcome(method, params)
    if isinstance(turn_outcome, TurnCompleted):
        stream.completed_turns.add((stream.thread_id, notice_turn or stream.turn_id))
    _settle_quota_continuation(stream, notice_turn, turn_outcome)
    if (isinstance(account_move, AccountMoveMarker) and not stream.end_marker.exists()
            and (not notice_turn or notice_turn == account_move.turn_id)):
        return _resume_marked_account_move(stream, log, account_move)
    if _interrupted_for_held_work(method, params, stream.session_dir, stream.name, stream.runs):
        turn_outcome = TurnCompleted()
    if isinstance(turn_outcome, TurnFailed):
        _record_turn_failure(stream, notice_turn, turn_outcome)
    if isinstance(turn_outcome, TurnContextWindowExceeded):
        return _continue_after_context_overflow(stream, log, notice_turn)
    if isinstance(turn_outcome, TurnRefusedForCapacity):
        return _on_capacity_refusal(stream, log, notice_turn)
    stream.capacity_waited = 0.0
    stream.capacity_retries = 0
    stream.capacity_backoff_owed = False
    stream.waiting_for_progress_after_overflow = False
    if stream.resume_owed:
        return _resume_owed_turn(stream, log, refused_for_capacity=False)
    return _after_turn_end(stream, log, turn_outcome)


def _settle_quota_continuation(stream: TurnStream, notice_turn: str, turn_outcome: TurnOutcome) -> None:
    """Clear the pending refusal once its submitted continuation completes."""
    pending = stream.pending_refusal
    if (isinstance(pending, PendingQuotaRefusal)
            and isinstance(pending.continuation, SubmittedQuotaContinuation)
            and isinstance(turn_outcome, TurnCompleted) and not stream.resume_owed
            and pending.continuation.thread_id == stream.thread_id
            and (notice_turn or stream.turn_id) == pending.continuation.turn_id
            and not _continuation_is_owed(stream.session_dir, stream.name)):
        stream.pending_refusal = NoPendingQuotaRefusal()
    elif not stream.resume_owed:
        stream.pending_refusal = _pending_after_completion_readback(stream.pending_refusal, stream.client)


def _record_turn_failure(stream: TurnStream, notice_turn: str, turn_outcome: TurnFailed) -> None:
    """Report a quota refusal once per turn and keep the server a failure came from."""
    serving = stream.client.serving
    identity = (serving, stream.thread_id, notice_turn or stream.turn_id)
    failed = FailedWithThread(stream.thread_id, turn_outcome.detail,
        time.time() - stream.client.turn_submitted_at, serving)
    if identity not in stream.reported_turns:
        stream.reported_turns.add(identity)
        if _quota_refused(failed, serving.started_here, False):
            with worker_lifecycle.lock_worker(stream.session_dir, stream.name):
                _owe_quota_continuation(stream.client, stream.thread_id)
            _report_quota_refusal(stream.name, serving, turn_outcome.detail,
                                  session_dir=stream.session_dir, thread_id=stream.thread_id)
            stream.pending_refusal = PendingQuotaRefusal(serving, turn_outcome.detail)
    if not stream.followed_failure:
        stream.failure_serving = serving


def _continue_after_context_overflow(stream: TurnStream, log: TextIO, notice_turn: str) -> StreamStep:
    """Start one continuation turn, or finish when the last one made no progress."""
    if stream.waiting_for_progress_after_overflow:
        stream.failure = (
            "context window filled twice in a row without progress; "
            "automatic continuation stopped"
        )
        _ = log.write(f"[{_now_stamp()}] {stream.failure}\n")
        log.flush()
        return StreamStep.FINISH
    continuation = "Continue where you stopped."
    _post_board_status(
        stream.session_dir,
        stream.name,
        f'context window filled; sent "{continuation}"',
    )
    _ = log.write(
        f"[{_now_stamp()}] context window filled; starting one continuation turn\n"
    )
    log.flush()
    started = _start_workers_turn(
        stream.args, stream.session_dir, stream.name, stream.thread_id, stream.client, continuation,
        submission=_quota_retry_submission(stream.pending_refusal, stream.thread_id, notice_turn or stream.turn_id))
    if isinstance(started, WorkerWaitingForRoom):
        return _wait_for_room(stream, log, started)
    _adopt_started_turn(stream, started)
    if not _record_stream_state(stream, {"thread_id": stream.thread_id, "turn_id": stream.turn_id, "status": "running"}):
        return StreamStep.FINISH
    stream.waiting_for_progress_after_overflow = True
    return StreamStep.NEXT_FRAME


def _on_capacity_refusal(stream: TurnStream, log: TextIO, notice_turn: str) -> StreamStep | FailedWithThread:
    """Park queued messages and owe the task a resume turn, or stop once the budget is spent."""
    if not stream.resume_owed:
        stream.capacity_submission = _quota_retry_submission(
            stream.pending_refusal, stream.thread_id, notice_turn or stream.turn_id)
    stream.resume_owed = True
    stream.capacity_backoff_owed = True
    thread_id = stream.thread_id
    _move_queue_to_pending(
        stream.client, thread_id, lambda message, submission: _add_pending(stream.session_dir, stream.name, message, submission))
    remaining = CAPACITY_BUDGET_SECS - stream.capacity_waited
    if remaining <= 0:
        exhausted = CapacityRetriesExhausted(
            stream.thread_id, stream.capacity_retries, int(CAPACITY_BUDGET_SECS / 60)
        )
        stream.run_outcome = exhausted
        stream.failure = exhausted.message(stream.name)
        return StreamStep.FINISH
    # A queued peer message may already have opened a turn.
    # Stream it, then return here with the task still owed.
    with _pending_file(stream.session_dir, stream.name):
        stopped = _capacity_launcher_stopped(stream, log)
        if stopped is not None:
            return stopped
    live = _read_known_turn(stream)
    if isinstance(live, ThreadLive) and live.turn_id != stream.turn_id:
        stream.capacity_backoff_owed = False
    # The readback can overtake the stream's dispatch of a peer's start/end.
    # Those notices decide whether a backoff is still owed. Keep this check
    # owed until they drain, even when this read already says idle.
    if stream.client.has_pending_frames:
        stream.state_change_owed = True
        return StreamStep.NEXT_FRAME
    if not isinstance(live, ThreadIdle):
        return _watch_turn_opened_while_owed(stream, log, live)
    return _resume_owed_turn(stream, log, refused_for_capacity=stream.capacity_backoff_owed)


def _capacity_launcher_stopped(stream: TurnStream, log: TextIO) -> FailedWithThread | None:
    """The outcome when the run ended or another launcher took the thread; the caller holds the pending lock."""
    if stream.end_marker.exists() or not _roster_still_on_thread(stream.session_dir, stream.name, stream.thread_id):
        reason = "ended" if stream.end_marker.exists() else "replaced"
        _ = log.write(f"[{_now_stamp()}] capacity launcher {reason}; no resume turn started\n")
        log.flush()
        return FailedWithThread(stream.thread_id, f"capacity launcher {reason}", time.time() - stream.opened,
            pending_refusal=stream.pending_refusal, director=stream.director)
    return None


def _read_known_turn(stream: TurnStream) -> ThreadIdle | ThreadLive | ThreadActiveWithoutTurn:
    """Read the thread's live turn; an unreadable thread stops the run."""
    live = _read_live_turn(stream.client, stream.thread_id)
    if isinstance(live, ThreadStateUnknown):
        raise SystemExit(f"thread {stream.thread_id}: thread/read failed: {live.reason}")
    return live


def _resume_owed_turn(stream: TurnStream, log: TextIO, *, refused_for_capacity: bool) -> StreamStep | FailedWithThread:
    """Start the turn a capacity refusal owes, after a backoff wait when a refusal just arrived."""
    if refused_for_capacity and not _wait_out_capacity(stream, log):
        return StreamStep.NEXT_FRAME
    live = _read_known_turn(stream)
    if isinstance(live, ThreadLive) and live.turn_id != stream.turn_id:
        stream.capacity_backoff_owed = False
    if stream.client.has_pending_frames:
        stream.state_change_owed = True
        return StreamStep.NEXT_FRAME
    if not isinstance(live, ThreadIdle):
        return _watch_turn_opened_while_owed(stream, log, live)
    with _pending_file(stream.session_dir, stream.name):
        stopped = _capacity_launcher_stopped(stream, log)
        if stopped is not None:
            return stopped
    started = _start_workers_turn(
        stream.args, stream.session_dir, stream.name, stream.thread_id, stream.client,
        "Your last turn stopped because the model was at capacity. Continue from where you stopped; your edits are already in the tree.",
        submission=stream.capacity_submission,
    )
    if isinstance(started, WorkerWaitingForRoom):
        stream.resume_owed = False
        return _wait_for_room(stream, log, started)
    _adopt_started_turn(stream, started)
    stream.resume_owed = False
    stream.capacity_backoff_owed = False
    return StreamStep.NEXT_FRAME


def _wait_out_capacity(stream: TurnStream, log: TextIO) -> bool:
    """Mark the thread waiting and sleep one backoff; false when a turn opened meanwhile."""
    remaining = CAPACITY_BUDGET_SECS - stream.capacity_waited
    wait = min(CAPACITY_WAIT_SECS * 2.0 ** stream.capacity_retries,
               CAPACITY_MAX_WAIT_SECS, remaining)
    with worker_lifecycle.lock_worker(stream.session_dir, stream.name), _pending_file(stream.session_dir, stream.name):
        if not _update_roster_if_expected(stream.session_dir, stream.name, {
                "thread_id": stream.thread_id,
                "status": WAITING_CAPACITY,
                "launcher_pid": os.getpid(),
            }, _stream_launcher(stream)):
            stream.ownership_released = True
            return False
    live = _read_known_turn(stream)
    if isinstance(live, ThreadLive) and live.turn_id != stream.turn_id:
        stream.capacity_backoff_owed = False
    if stream.client.has_pending_frames:
        stream.state_change_owed = True
        return False
    if not isinstance(live, ThreadIdle):
        return False
    stream.capacity_retries += 1
    next_at = datetime.now().astimezone().timestamp() + wait
    next_stamp = datetime.fromtimestamp(next_at).astimezone().isoformat(timespec="seconds")
    _ = log.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] capacity retry {stream.capacity_retries}: next turn at {next_stamp}\n")
    log.flush()
    wait_started = _capacity_clock()
    _capacity_sleep(wait)
    stream.capacity_waited += max(wait, _capacity_clock() - wait_started)
    stream.capacity_backoff_owed = False
    return True


def _watch_turn_opened_while_owed(
    stream: TurnStream, log: TextIO, live: ThreadLive | ThreadActiveWithoutTurn,
) -> StreamStep | FailedWithThread:
    """Watch a turn a queued message opened, keeping the resume turn owed."""
    stream.turn_id = live.turn_id if isinstance(live, ThreadLive) else ""
    with _pending_file(stream.session_dir, stream.name):
        stopped = _capacity_launcher_stopped(stream, log)
        if stopped is not None:
            return stopped
        if stream.turn_id:
            if not _record_stream_state(stream, {
                    "thread_id": stream.thread_id,
                    "turn_id": stream.turn_id,
                    "status": "running",
                }):
                return StreamStep.FINISH
        else:
            if not _record_stream_state(stream, {
                    "thread_id": stream.thread_id,
                    "status": "starting",
                    "launcher_pid": os.getpid(),
                }):
                return StreamStep.FINISH
    return StreamStep.NEXT_FRAME


def _after_turn_end(stream: TurnStream, log: TextIO, turn_outcome: TurnOutcome) -> StreamStep:
    """Deliver a resident's reply, then move the worker or wait for the work still owed."""
    quota_owed = _continuation_is_owed(stream.session_dir, stream.name)
    if (stream.follow and not stream.resident and isinstance(turn_outcome, TurnFailed)
            and not stream.followed_failure and not quota_owed):
        stream.followed_failure = turn_outcome.detail
    stream.failure = stream.followed_failure or (turn_outcome.detail if isinstance(turn_outcome, TurnFailed) else "")
    if stream.resident:
        stream.replies += 1
        _deliver_reply(unit_workers.shown_name(Path(stream.session_dir), stream.name,
                           role=_as_str(_attr(stream.args, "role"))),
                       stream.replies, stream.final_answer, stream.failure,
                       stream.summary_path, stream.reply_path, log)
        stream.final_answer = ""
        stream.failure = ""
        if stream.end_marker.exists():
            return StreamStep.FINISH
    with _pending_file(stream.session_dir, stream.name) as handle:
        held_messages = bool(_pending_messages(handle))
    owed = _continuation_is_owed(stream.session_dir, stream.name)
    if stream.resident or held_messages or owed:
        moved = _move_worker(stream)
        if isinstance(moved, WorkerWaitingForRoom):
            return _wait_for_room(stream, log, moved)
        if isinstance(moved, WorkerThreadStateUnknown):
            if stream.resident:
                _ = log.write(f"[{_now_stamp()}] mesh: {stream.name} cannot move: {moved.reason}; waiting\n")
                log.flush()
                return StreamStep.NEXT_FRAME
            _hold_messages_for_next_launch(stream, log, moved.reason)
            return StreamStep.FINISH
        if isinstance(moved, MovedWorker) and _adopt_moved_worker(stream, moved):
            return StreamStep.NEXT_FRAME
        if owed and isinstance(moved, WorkerWaitingOnLiveTurn):
            return StreamStep.NEXT_FRAME
    if not stream.resident:
        return _finish_one_turn_launch(stream, log)
    if not _record_stream_state(stream, {"thread_id": stream.thread_id, "turn_id": stream.turn_id, "status": "running"}):
        return StreamStep.FINISH
    return StreamStep.FINISH if stream.end_marker.exists() else StreamStep.NEXT_FRAME


def _finish_one_turn_launch(stream: TurnStream, log: TextIO) -> StreamStep:
    """Finish a one-turn launcher unless more work is coming to its thread."""
    if _waiting_on_detached_run(stream.session_dir, stream.name, stream.runs):
        return StreamStep.NEXT_FRAME
    # A finished turn may have a peer turn opening behind it.
    if (_more_work_coming(stream.client, stream.thread_id, stream.deadline)
            or stream.client.has_pending_frames):
        # An idle readback can arrive before dispatch of a peer's completion.
        # Finish only after every notice it buffered has had its turn.
        stream.state_change_owed = True
        return StreamStep.NEXT_FRAME
    if _close_messages_if_empty(stream.session_dir, stream.name, _stream_launcher(stream), stream.runs):
        return StreamStep.FINISH
    moved = _move_worker(stream)
    if isinstance(moved, WorkerWaitingForRoom):
        return _wait_for_room(stream, log, moved)
    if isinstance(moved, WorkerThreadStateUnknown):
        _hold_messages_for_next_launch(stream, log, moved.reason)
        return StreamStep.FINISH
    if isinstance(moved, MovedWorker):
        _ = _adopt_moved_worker(stream, moved)
    return StreamStep.NEXT_FRAME


def _released_stream_outcome[Outcome: RunOutcome](stream: TurnStream, outcome: Outcome) -> Outcome | WorkerOwnershipLost:
    """Keep observed turn completion; ownership loss alone cannot prove it."""
    if isinstance(outcome, RunCompleted) and (stream.thread_id, stream.turn_id) in stream.completed_turns:
        return outcome
    record = _as_dict(_read_json_object(_session_path(stream.session_dir, ROSTER_FILE)).get(stream.name))
    # A resident can be marked done, or explicitly ended, without changing its
    # launcher. That terminal publication is not a transfer to another owner.
    same_launcher = (session_staff.worker_session(record) == stream.thread_id
                     and record.get("launcher_pid") in _stream_launcher(stream).launcher_pids)
    if same_launcher and record.get("status") in ("done", "ended"):
        return outcome
    return stream.ownership_loss if isinstance(outcome, RunCompleted) else outcome


def _finished_stream_outcome(stream: TurnStream) -> RunOutcome:
    if isinstance(stream.run_outcome, CapacityRetriesExhausted):
        return replace(stream.run_outcome, pending_refusal=stream.pending_refusal, director=stream.director)
    if stream.failure:
        return FailedWithThread(stream.thread_id, stream.failure, time.time() - stream.opened, stream.failure_serving,
                                isinstance(stream.pending_refusal, PendingQuotaRefusal), True,
                                pending_refusal=stream.pending_refusal, director=stream.director)
    return RunCompleted()


def _finish_stream(stream: TurnStream) -> RunOutcome:
    """Clean up outside the roster lock; fence the terminal write by launcher ownership."""
    with worker_lifecycle.lock_worker(stream.session_dir, stream.name):
        _close_stream_inbox_locked(stream)
        if _stream_was_released(stream):
            return _released_stream_outcome(stream, _finished_stream_outcome(stream))
        if not stream.resume_owed:
            stream.pending_refusal = _pending_after_completion_readback(stream.pending_refusal, stream.client)
        if (not stream.failure and isinstance(stream.pending_refusal, PendingQuotaRefusal)
                and _continuation_is_owed(stream.session_dir, stream.name)):
            stream.failure = stream.pending_refusal.detail
        # Readbacks, cleanup and summary callbacks can send messages or look up the
        # worker. None may run under the roster lock. Check ownership after each
        # boundary, then compare it again inside the final locked write.
        if _stream_was_released(stream):
            return _released_stream_outcome(stream, _finished_stream_outcome(stream))
        if not stream.resident or stream.failure or stream.end_marker.exists():
            _ = _end_unwatched_turn(stream.port, stream.thread_id, stream.log_path)
        if _stream_was_released(stream):
            return _released_stream_outcome(stream, _finished_stream_outcome(stream))
        if not stream.resident or stream.replies == 0:
            shown = unit_workers.shown_name(Path(stream.session_dir), stream.name,
                                           role=_as_str(_attr(stream.args, "role")))
            _finish_summary(stream.summary_path, stream.reply_path, shown, stream.replies + 1,
                            stream.final_answer or stream.failure or f"The worker {stream.name} produced no summary.")
        outcome = _finished_stream_outcome(stream)
        terminal: FollowableRecord | ExhaustedRecord
        if isinstance(outcome, CapacityRetriesExhausted):
            terminal = {"thread_id": stream.thread_id, "status": "capacity_exhausted",
                        "settled_launcher_pid": os.getpid()}
        else:
            terminal = {"thread_id": stream.thread_id, "status": "failed" if stream.failure else "done"}
            if stream.failure:
                terminal["settled_launcher_pid"] = os.getpid()
        if not _update_roster_if_expected(stream.session_dir, stream.name,
                                         terminal, _stream_launcher(stream)):
            stream.ownership_released = True
            return _released_stream_outcome(stream, outcome)
        return outcome


def _fail_stream(stream: TurnStream, exc: ConnectionError | OSError | SystemExit) -> FailedWithThread | WorkerOwnershipLost:
    """Keep the error outcome without cleaning up or changing another launcher's worker."""
    with worker_lifecycle.lock_worker(stream.session_dir, stream.name):
        # The first ownership check permanently releases this subscriber. Normalize
        # replacement identity before that check, closing the inbox or building the result.
        if isinstance(exc, WorkerQuotaRecoveryStopped):
            stream.port, stream.thread_id = exc.port, exc.thread_id
        elif isinstance(exc, WorkerMoveUncertain) and isinstance(exc.cleanup, WorkerTurnCleanup):
            stream.port, stream.thread_id = exc.cleanup.port, exc.cleanup.thread_id
        failure = str(exc) or exc.__class__.__name__
        serving = exc.serving if isinstance(exc, (ServingRequestFailure, WorkerMoveUncertain)) else stream.client.serving
        reported = exc.reported if isinstance(exc, (ServingRequestFailure, WorkerMoveUncertain)) else False
        pending = PendingQuotaRefusal(serving, failure) if reported else stream.pending_refusal
        outcome = FailedWithThread(stream.thread_id, failure, time.time() - stream.opened, serving,
                                   isinstance(pending, PendingQuotaRefusal), True,
                                   pending_refusal=pending, director=stream.director)
        _close_stream_inbox_locked(stream)
        if _stream_was_released(stream):
            return _released_stream_outcome(stream, outcome)
        # Once thread/start returned, even a disconnected turn/start may have
        # begun work. Keep its id and never resubmit the original prompt.
        if not stream.resume_owed:
            stream.pending_refusal = _pending_after_completion_readback(stream.pending_refusal, stream.client)
        if _stream_was_released(stream):
            return _released_stream_outcome(stream, outcome)
        _ = _end_unwatched_turn(stream.port, stream.thread_id, stream.log_path)
        if reported:
            stream.pending_refusal = PendingQuotaRefusal(serving, failure)
        outcome = replace(outcome, thread_id=stream.thread_id, seconds=time.time() - stream.opened,
                          refusal_reported=isinstance(stream.pending_refusal, PendingQuotaRefusal),
                          pending_refusal=stream.pending_refusal)
        if not _update_roster_if_expected(stream.session_dir, stream.name,
                                         {"thread_id": stream.thread_id, "status": "failed",
                                          "settled_launcher_pid": os.getpid()}, _stream_launcher(stream)):
            stream.ownership_released = True
            return _released_stream_outcome(stream, outcome)
        return outcome


@dataclass(frozen=True)
class MovedWorker:
    client: Client
    port: int
    # Empty when no new turn was accepted.
    turn_id: str
    thread_id: str
    thread_replaced: bool
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    pending_refusal: PendingQuotaRefusal | NoPendingQuotaRefusal = field(
        default_factory=NoPendingQuotaRefusal, kw_only=True)
    submission: QuotaContinuationTurn | UnrelatedWorkerTurn = field(
        default_factory=UnrelatedWorkerTurn, kw_only=True)


@dataclass(frozen=True)
class WorkerWaitingOnLiveTurn:
    """The outgoing thread is still working and keeps its server."""

    reason: str = "the outgoing thread still has a live turn"


@dataclass(frozen=True)
class WorkerWaitingForRoom:
    """The idle thread owes a continuation and waits for account room."""

    reason: str
    client: Client
    port: int
    thread_id: str


@dataclass(frozen=True)
class WorkerAwaitingQuotaRecovery(MovedWorker):
    """The thread resumed, but the destination refused its owed continuation."""

    refusal: ServingRequestFailure = field(kw_only=True)


@dataclass(frozen=True)
class NoSubmissionQuotaRefusal:
    """No refused submission needs recovery during this attempt."""


@dataclass(frozen=True)
class WorkerTurnCleanup:
    """A turn may have started here while the move could not finish."""

    port: int
    thread_id: str


@dataclass(frozen=True)
class NoWorkerTurnToCleanUp:
    """No new turn was submitted during this failed move."""


@dataclass(frozen=True)
class WorkerThreadStateUnknown:
    """The outgoing thread cannot be proved idle; no new turn may start."""

    reason: str
    cleanup: WorkerTurnCleanup | NoWorkerTurnToCleanUp = field(default_factory=NoWorkerTurnToCleanUp)
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    refusal_reported: bool = False


WorkerMoveResult = MovedWorker | WorkerWaitingOnLiveTurn | WorkerWaitingForRoom | WorkerThreadStateUnknown


class WorkerMoveUncertain(SystemExit):
    def __init__(self, thread_id: str, result: WorkerThreadStateUnknown) -> None:
        super().__init__(f"thread {thread_id}: state unknown ({result.reason}); no turn resubmitted")
        self.cleanup: WorkerTurnCleanup | NoWorkerTurnToCleanUp = result.cleanup
        self.serving: CodexServingState = result.serving
        self.reported: bool = result.refusal_reported


@dataclass(frozen=True)
class _ReleasedWorkerThread:
    """The old server no longer holds the thread's writer."""


def _recorded_server_gone(session_dir: str, port: int) -> bool:
    pids = [pid for record in _recorded_run_servers(session_dir)
            if record.get("port") == port and isinstance(pid := record.get("pid"), int)]
    return bool(pids) and not any(_pid_alive(pid) for pid in pids)


def _release_blocker_reason(client: Client, session_dir: str, thread_id: str) -> str:
    workers = _roster_threads(Path(session_dir))
    busy: list[str] = []
    for identity, name in workers.items():
        try:
            reply = client.call("thread/read", {"threadId": identity, "includeTurns": False}, timeout=1.0)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (ConnectionError, OSError, SystemExit):
            continue
        thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
        if _as_str(_as_dict(thread.get("status")).get("type")) in ("active", "running"):
            busy.append(name)
    held_by = ", ".join(busy) or workers.get(thread_id, thread_id)
    return (f"run {Path(session_dir).name}, worker {held_by}: old server has not released the thread; "
            + "follow-up goes through once that turn ends")


def _release_worker_thread(client: Client, thread_id: str, session_dir: str) -> _ReleasedWorkerThread | WorkerThreadStateUnknown:
    deadline = worker_lifecycle.effective_deadline(time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS)
    try:
        unsubscribed = _require(client.call("thread/unsubscribe", {"threadId": thread_id},
                                           timeout=THREAD_RELEASE_TIMEOUT_SECS), "thread/unsubscribe")
        if unsubscribed.get("status") == "notLoaded":
            return _ReleasedWorkerThread()
        while time.monotonic() < deadline:
            if thread_id not in _loaded_thread_ids(client, deadline):
                return _ReleasedWorkerThread()
            frame = client.next_frame(time.time() + min(0.1, max(0.0, deadline - time.monotonic())))
            if frame and frame.get("method") == "thread/closed" and _as_str(_as_dict(frame.get("params")).get("threadId")) == thread_id:
                return _ReleasedWorkerThread()
        TransportDeadline(deadline, worker_lifecycle.effective_deadline()).check_operation()
        if _recorded_server_gone(session_dir, client.port):
            return _ReleasedWorkerThread()
        return WorkerThreadStateUnknown(_release_blocker_reason(client, session_dir, thread_id))
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as error:
        if _recorded_server_gone(session_dir, client.port):
            return _ReleasedWorkerThread()
        return WorkerThreadStateUnknown(f"old thread release could not be confirmed: {error}")


@dataclass(frozen=True)
class _WorkerThreadForHome:
    thread_id: str
    thread_replaced: bool


def _refuse_taken_follow_up(client: Client, replacement: str, session_dir: str) -> NoReturn:
    released = _release_worker_thread(client, replacement, session_dir)
    if isinstance(released, WorkerThreadStateUnknown):
        raise SystemExit("name was taken while the follow-up ran; "
                         + f"replacement thread {replacement} not released: {released.reason}")
    raise SystemExit("name was taken while the follow-up ran; follow-up did not start")


def _worker_rollout_path(session_dir: str, thread_id: str, home: Path) -> Path | None:
    """Find the saved history in the worker and recorded server homes."""
    homes = {home, *(server_record_home(record) for record in _recorded_run_servers(session_dir) if record)}
    paths: set[Path] = set()
    for source in homes:
        for directory in ("sessions", "archived_sessions"):
            paths.update(path.resolve() for path in (source / directory).glob(f"**/rollout-*-{thread_id}.jsonl"))
    if len(paths) > 1:
        raise SystemExit(f"RoleNeedsRecovery: multiple saved histories for {thread_id}; replacement refused")
    path = next(iter(paths), None)
    if path is not None:
        try:
            with path.open("rb") as history:
                _ = history.read(1)
        except OSError as error:
            raise SystemExit(f"RoleNeedsRecovery: saved history is unreadable at {path}: {error}; replacement refused") from error
    return path


def _resume_worker_thread(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    workers_home: Path, *, staff_admission: WorkerStartAllowed,
) -> _WorkerThreadForHome:
    with worker_lifecycle.lock_worker(session_dir, name):
        resumed = _resume_locked_worker_thread(args, session_dir, name, thread_id, client,
                                              workers_home, staff_admission=staff_admission)
    role = _as_str(_attr(args, "role"))
    if resumed.thread_replaced and not role:
        assignment = task_role(thread_id, task_assignments())
        role = assignment.role if isinstance(assignment, AssignedWorkerRole) else ""
    _ = worker_admission.publish_created(staff_admission, resumed.thread_id,
        lambda identity: _cleanup_created_thread(client, session_dir, identity))
    if resumed.thread_replaced:
        _ = client.call("thread/name/set", {"threadId": resumed.thread_id,
            "name": unit_workers.shown_name(Path(session_dir), name, role=role)})
    return resumed


def _resume_locked_worker_thread(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    workers_home: Path, *, staff_admission: WorkerStartAllowed,
) -> _WorkerThreadForHome:
    if staff_admission.name != name:
        raise ValueError("staff admission names another worker")
    params = _resume_params(session_dir, name, thread_id)
    previous_record = _lookup(session_dir, name)
    instructions_file = _as_str(previous_record.get("instructions_file"))
    held = bool(previous_record.get("held_design_id"))
    own_reviewer = (isinstance(roster_access_policy(previous_record), ReadOnlyReviewer)
                    and bool(previous_record.get("unit_ledger")))
    sandbox = _worker_sandbox(args, session_dir, name)
    resumed = client.call("thread/resume", params)
    if resumed.get("error"):
        path = _worker_rollout_path(session_dir, thread_id, workers_home)
        if path is not None:
            resumed = client.call("thread/resume", {**params, "path": str(path)})
            if resumed.get("error"):
                _post_board_status(session_dir, name,
                    f"saved history for {thread_id} could not resume from {path}: {json.dumps(resumed.get('error'))}; replacement refused")
                _ = _require(resumed, "thread/resume")
    if not resumed.get("error"):
        result = _require(resumed, "thread/resume")
        if _thread_id(result) != thread_id:
            raise SystemExit("RoleNeedsRecovery: resumed thread identity differs; replacement refused")
        return _WorkerThreadForHome(thread_id, False)
    if isinstance(_read_account_move(session_dir, name, thread_id), AccountMoveMarker):
        _ = _require(resumed, "thread/resume")
    if own_reviewer or previous_record.get("dead_launcher_recovery"):
        _ = _require(resumed, "thread/resume")
    if held:
        raise SystemExit("RoleNeedsRecovery: held thread could not resume; replacement refused")
    start_params = _thread_start_params(args, session_staff.worker_directory(staff.local_snapshot(), staff_admission.parent))
    start_params["sandbox"] = sandbox
    if "developerInstructions" in params:
        start_params.update({
            "developerInstructions": params["developerInstructions"], "approvalPolicy": "never",
        })
    created = _serving_request(client, "thread/start", start_params, submits_work=True)
    replacement = _thread_id(created)
    name = staff_admission.name
    record: StartingRecord = {"thread_id": replacement, "status": "starting", "port": client.port,
                              "launcher_pid": _as_int(previous_record.get("launcher_pid")) or client.launcher_pid}
    if "access_policy" in previous_record:
        # Assignment policy is durable for unit workers, even on a new thread.
        # Ordinary workers without one still take each launch's sandbox.
        record["access_policy"] = dict(previous_record["access_policy"])
    if instructions_file:
        record["instructions_file"] = instructions_file
    if not _update_roster_if_expected(session_dir, name, record,
                                     RosterUnchanged(thread_id, _roster_revision(previous_record))):
        _refuse_taken_follow_up(client, replacement, session_dir)
    _post_board_status(
        session_dir, name,
        f"{codex_home_display(workers_home)} refused to resume {thread_id}; worker continues as new thread {replacement}; "
        + f"no saved rollout found; resume error: {json.dumps(resumed.get('error'))}",
    )
    return _WorkerThreadForHome(replacement, True)


def _release_other_worker_writers(
    session_dir: str, name: str, thread_id: str, target_port: int,
) -> _ReleasedWorkerThread | WorkerWaitingOnLiveTurn | WorkerThreadStateUnknown:
    for port in _run_servers(session_dir):
        if port == target_port:
            continue
        records = _recorded_run_servers(session_dir)
        pids = {pid for record in records
                if record.get("port") == port and isinstance(pid := record.get("pid"), int)}
        live_pids = {pid for pid in pids if _pid_alive(pid)}
        current = _read_json_object(_session_path(session_dir, SERVER_FILE))
        current_pid = current.get("pid") if current.get("port") == port else None
        entry = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        held = bool(entry.get("held_design_id"))
        stopped_pids = {pid for pid in live_pids if not held and pid != current_pid
                        and _stop_unused(None, pid, port, RetiredRunServer(Path(session_dir)))}
        if pids and live_pids <= stopped_pids:
            continue
        client: Client | None = None
        try:
            client = Client(port, f"release-{name}", deadline=time.time() + THREAD_RELEASE_TIMEOUT_SECS)
            if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                continue
            state = _read_live_turn(client, thread_id)
            if isinstance(state, ThreadStateUnknown):
                return WorkerThreadStateUnknown(state.reason)
            if not isinstance(state, ThreadIdle):
                return WorkerWaitingOnLiveTurn(
                    f"run {Path(session_dir).name}, worker {name}: old server still has a live turn; "
                    + "follow-up goes through once that turn ends")
            with _pending_file(session_dir, name) as handle:
                messages = _pending_messages(handle)
                def park(message: str, submission: str) -> None:
                    _park_submission(handle, message, submission)
                    messages[:] = _pending_messages(handle)
                _move_queue_to_pending(client, thread_id, park)
                state = _read_live_turn(client, thread_id)
                if not isinstance(state, ThreadIdle):
                    return WorkerThreadStateUnknown("old thread is no longer idle")
                released = _release_worker_thread(client, thread_id, session_dir)
                if isinstance(released, WorkerThreadStateUnknown):
                    return released
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (ConnectionError, OSError, SystemExit) as error:
            if _recorded_server_gone(session_dir, port):
                continue
            return WorkerThreadStateUnknown(f"old writer could not be checked: {error}")
        finally:
            if client is not None:
                client.close()
    return _ReleasedWorkerThread()


@role_map_command
def _move_resident(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    *, message: str = "",
) -> WorkerMoveResult:
    try:
        return _move_idle_worker(args, session_dir, name, thread_id, client, message=message)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as error:
        return WorkerThreadStateUnknown(str(error) or type(error).__name__)


def _quota_continuation_ready(client: Client, selected_home: Path) -> bool:
    from scripts.accounts import moves, refusals

    now = datetime.now(timezone.utc)
    serving = client.serving
    if selected_home != serving.home:
        return True
    identity = serving.account
    if isinstance(identity, UnknownCodexAccount):
        return False
    refusal = refusals.load().get(refusals.key("codex", identity.login))
    reading = refusals.stored_reading("codex", identity.login, now)
    if refusal is None:
        return (isinstance(reading, refusals.FreshReading) and reading.weekly.left > 0
                and (isinstance(reading.five_hour, refusals.NoWindow) or reading.five_hour.left > 0))
    if not refusals.resolved(refusal, reading, now):
        return False
    entries = moves.read_moves()
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if (entry["tool"] == "codex" and entry["subject"] == "workers" and entry["kind"] == "waited"
                and entry["move_id"] not in ended and datetime.fromisoformat(entry["reset"]) <= now):
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="codex",
                subject="workers", move_id=entry["move_id"]))
    return True


@dataclass(frozen=True)
class NoRequestedWorkerMove:
    """No workers transition requests this source-to-destination resume."""


def _requested_worker_move(source: CodexServingState, destination: Path) -> RequestedMove | NoRequestedWorkerMove:
    from scripts.accounts import moves

    if not isinstance(source, ServingCodexServer) or source.home == destination:
        return NoRequestedWorkerMove()
    requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"
                and entry["tool"] == "codex" and entry["subject"] == "workers"]
    origin = requests[-1]["from"] if requests else moves.HomeAccountUnresolved(source.home)
    target = requests[-1]["to"] if requests else moves.HomeAccountUnresolved(destination)
    if (requests and isinstance(origin, str) and isinstance(target, str)
            and codex_home_matches_label(source.home, origin)
            and codex_home_matches_label(destination, target)):
        # The reader resolves legacy home labels from historical evidence; leave the journal unchanged.
        matched = requests[-1].copy()
        matched["to"] = codex_home_role_label(destination)
        return matched
    return NoRequestedWorkerMove()


def _record_worker_move_failure(request: RequestedMove | NoRequestedWorkerMove, reason: str) -> None:
    from scripts.accounts import moves

    if isinstance(request, NoRequestedWorkerMove):
        return
    moves.append_move(moves.FailedMove(kind="failed", at=datetime.now(timezone.utc).isoformat(),
        tool="codex", subject="workers", move_id=request["move_id"], reason=reason))


def _confirm_worker_move(session_dir: str, name: str, request: RequestedMove | NoRequestedWorkerMove,
                       serving: CodexServingState) -> None:
    from scripts.accounts import moves

    if not isinstance(serving, ServingCodexServer):
        return
    if isinstance(request, NoRequestedWorkerMove) or codex_home_role_label(serving.home) != request["to"]:
        return
    moves.append_move(moves.ConfirmedMove(kind="confirmed", at=datetime.now(timezone.utc).isoformat(),
        tool="codex", subject="workers", move_id=request["move_id"], home=str(serving.home),
        worker=name, run=str(Path(session_dir).resolve())))


def _move_idle_worker(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    *, message: str = "",
) -> WorkerMoveResult:
    """Check one new-turn boundary and move an idle thread onto the workers home."""
    try:
        staff_admission = _admit_worker(name, session_dir)
    except ValueError as error:
        return WorkerThreadStateUnknown(str(error))
    with worker_lifecycle.membership_operation(session_dir), contextlib.ExitStack() as lifecycle:
        _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
        owner = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if not RosterActiveLauncher(thread_id, (client.launcher_pid, os.getpid())).owns(owner):
            return WorkerThreadStateUnknown("worker ownership changed before the new turn")
        state = _read_live_turn(client, thread_id)
        if isinstance(state, ThreadStateUnknown):
            return WorkerThreadStateUnknown(state.reason)
        if not isinstance(state, ThreadIdle):
            return WorkerWaitingOnLiveTurn()
        if isinstance(client.serving, NotBoundCodexServer):
            _ = _bind_serving_server(client, session_dir)
        source = client.serving
        worker_home_for_turn = workers_home()
        request = _requested_worker_move(source, worker_home_for_turn)
        cleanup: WorkerTurnCleanup | NoWorkerTurnToCleanUp = NoWorkerTurnToCleanUp()
        moved = client
        replaced = False
        try:
            with _pending_file(session_dir, name) as handle:
                if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                    return WorkerThreadStateUnknown("worker ended or was replaced before the new turn")
                state = _read_live_turn(client, thread_id)
                if isinstance(state, ThreadStateUnknown):
                    return WorkerThreadStateUnknown(state.reason)
                if not isinstance(state, ThreadIdle):
                    return WorkerWaitingOnLiveTurn()
                messages = _pending_messages(handle)

                def park(text: str, submission: str) -> None:
                    _park_submission(handle, text, submission)
                    messages[:] = _pending_messages(handle)

                _move_queue_to_pending(client, thread_id, park)
                state = _read_live_turn(client, thread_id)
                if isinstance(state, ThreadStateUnknown):
                    return WorkerThreadStateUnknown(state.reason)
                if not isinstance(state, ThreadIdle):
                    return WorkerWaitingOnLiveTurn()
            # Refresh may park every role's queue, so it must run without a pending lock.
            port, _fresh = ensure_server(
                session_dir, worker_home_for_turn,
                cwd=_as_str(_attr(args, "turn_cwd")) or _as_str(_attr(args, "cwd")) or None,
            )
            with contextlib.ExitStack() as inbox:
                handle = inbox.enter_context(_pending_file(session_dir, name))
                if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                    return WorkerThreadStateUnknown("worker ended or was replaced before the new turn")
                messages = _pending_messages(handle)
                previous = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                old_port = client.port
                owes_continuation = previous.get("continuation_owed") is True
                if owes_continuation and not _quota_continuation_ready(client, worker_home_for_turn):
                    from scripts.accounts import refusals
                    identity = client.serving.account
                    if isinstance(identity, UnknownCodexAccount):
                        return WorkerThreadStateUnknown("quota handoff did not establish a recoverable account; continuation is held")
                    if refusals.key("codex", identity.login) not in refusals.load():
                        return WorkerWaitingForRoom("no fresh reading shows room on the account", client, old_port, thread_id)
                    return WorkerWaitingForRoom("the refused account has not recovered", client, old_port, thread_id)
                if isinstance(client.serving, NotBoundCodexServer):
                    _ = _bind_serving_server(client, session_dir, started_here=_fresh)
                if _fresh and port == old_port:
                    serving = client.serving
                    client.serving = replace(serving, started_here=True)
                if port != old_port:
                    released = _release_worker_thread(client, thread_id, session_dir)
                    if isinstance(released, WorkerThreadStateUnknown):
                        _record_worker_move_failure(request, released.reason)
                        return released
                    moved = Client(port, name)
                    moved.launcher_pid = client.launcher_pid
                    _ = _bind_serving_server(moved, session_dir, started_here=_fresh)
                    try:
                        inbox.close()
                        lifecycle.close()
                        resumed = _resume_worker_thread(args, session_dir, name, thread_id, moved,
                            worker_home_for_turn, staff_admission=staff_admission)
                        _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
                        handle = inbox.enter_context(_pending_file(session_dir, name))
                        messages = _pending_messages(handle)
                        thread_id, replaced = resumed.thread_id, resumed.thread_replaced
                        if replaced:
                            _write_pending(handle, messages)
                            if not _update_roster_if_expected(session_dir, name,
                                {"thread_id": thread_id, "status": "starting", "port": port,
                                 "continuation_owed": owes_continuation},
                                RosterActiveLauncher(thread_id, (client.launcher_pid, os.getpid()))):
                                raise SystemExit(f"codex_mesh: {name}: roster ownership changed during replacement")
                            role = task_role(thread_id, task_assignments())
                            if isinstance(role, AssignedWorkerRole):
                                _record_assignment(thread_id, role.role, _dispatch_lens(args))
                    except (ConnectionError, OSError, SystemExit, worker_admission.AdmissionRefused) as error:
                        _record_worker_move_failure(request, str(error) or type(error).__name__)
                        moved.close()
                        current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                        current_thread = _as_str(session_staff.worker_session(current)) or thread_id
                        cleanup = (WorkerTurnCleanup(port, current_thread)
                                   if replaced or current_thread != thread_id else NoWorkerTurnToCleanUp())
                        return WorkerThreadStateUnknown(str(error) or type(error).__name__, cleanup,
                            error.serving if isinstance(error, ServingRequestFailure) else moved.serving,
                            error.reported if isinstance(error, ServingRequestFailure) else False)
                    _confirm_worker_move(session_dir, name, request, moved.serving)
                    client.close()
                turn_id = ""
                if owes_continuation or message or messages:
                    if not _update_roster_if_expected(session_dir, name,
                        {"thread_id": thread_id, "status": "starting", "port": port},
                        RosterActiveLauncher(thread_id, (client.launcher_pid, os.getpid()))):
                        raise SystemExit(f"codex_mesh: {name}: roster ownership changed before continuation")
                    if previous.get("preserved_work") and not message:
                        # Preserve the existing result-first recovery order when
                        # there is no new assignment. New tasks retain held order.
                        messages.sort(key=lambda text: 0 if text.startswith(("Run finished:", "Run ended with no result:")) else 1)
                    inputs = list(messages)
                    if owes_continuation:
                        # Without an assignment, ordinary recovery still leads
                        # with continuation; every new task follows all held text.
                        continuation_at = len(inputs) if message or previous.get("preserved_work") else 0
                        inputs.insert(continuation_at, "Continue where you stopped.")
                    if message:
                        inputs.append(message)
                    cleanup = WorkerTurnCleanup(port, thread_id)
                    turn_id = _start_turn(moved, args, thread_id, _worker_sandbox(args, session_dir, name),
                                          inputs[0], tuple(inputs[1:]))
                    if owes_continuation and not turn_id:
                        raise SystemExit("quota continuation was not confirmed by turn/start; task is still owed")
                    _write_pending(handle, [])
                prior_turn = _as_str(previous.get("turn_id")) if not replaced else ""
                if not _update_roster_if_expected(session_dir, name, {
                    "thread_id": thread_id, "turn_id": turn_id or prior_turn, "status": "running", "port": port,
                    "continuation_owed": False, "preserved_work": False
                }, RosterActiveLauncher(thread_id, (client.launcher_pid, os.getpid()))):
                    raise SystemExit(f"codex_mesh: {name}: roster ownership changed during continuation")
            result = MovedWorker(moved, port, turn_id, thread_id, replaced, moved.serving,
                submission=QuotaContinuationTurn() if owes_continuation and turn_id else UNRELATED_WORKER_TURN)
        except (ConnectionError, OSError, SystemExit) as error:
            if isinstance(error, ServingRequestFailure) and error.reported:
                return WorkerAwaitingQuotaRecovery(moved, moved.port, "", thread_id, replaced, moved.serving, refusal=error)
            _record_worker_move_failure(request, str(error) or type(error).__name__)
            if isinstance(cleanup, NoWorkerTurnToCleanUp):
                raise
            if moved is not client:
                moved.close()
            return WorkerThreadStateUnknown(str(error) or type(error).__name__, cleanup,
                error.serving if isinstance(error, ServingRequestFailure) else moved.serving,
                error.reported if isinstance(error, ServingRequestFailure) else False)

    # Submission records telemetry on the roster first. Refresh the ledger
    # after releasing the inbox lock, preserving ledger -> roster lock order.
    if turn_id and previous.get("unit_ledger"):
        unit_workers.note_backend(Path(session_dir), name, thread_id, moved.serving.home)
    return result


def _start_workers_turn(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str,
    client: Client, message: str,
    *, submission: QuotaContinuationTurn | UnrelatedWorkerTurn = UNRELATED_WORKER_TURN,
) -> MovedWorker | WorkerWaitingForRoom:
    initial_client = client
    quota_recovery: WorkerAwaitingQuotaRecovery | NoSubmissionQuotaRefusal = NoSubmissionQuotaRefusal()
    try:
        deadline = time.monotonic() + _as_float(_attr(args, "timeout"), 86400.0)
        # Arm before submission: the refusal reporter may synchronously change
        # the selected account. Its notification must survive until we wait.
        with contextlib.closing(FollowEvents(session_dir, name)) as events:
            while time.monotonic() < deadline:
                result = _move_resident(args, session_dir, name, thread_id, client, message=message)
                if isinstance(result, WorkerWaitingForRoom):
                    return result
                if isinstance(result, WorkerThreadStateUnknown):
                    raise WorkerMoveUncertain(thread_id, result)
                if isinstance(result, MovedWorker) and not isinstance(result, WorkerAwaitingQuotaRecovery):
                    if result.turn_id:
                        _mesh_phase_wait(session_dir, name, result.thread_id, "WaitingForQuotaRefusal", finished=True)
                        _mesh_phase_wait(session_dir, name, result.thread_id, "ReadyWithoutCapacity", finished=True)
                    if isinstance(submission, QuotaContinuationTurn):
                        result = replace(result, submission=submission)
                    if isinstance(quota_recovery, WorkerAwaitingQuotaRecovery):
                        refusal = quota_recovery.refusal
                        return replace(result, pending_refusal=PendingQuotaRefusal(refusal.serving, str(refusal)))
                    return result
                if isinstance(result, WorkerAwaitingQuotaRecovery):
                    quota_recovery = result
                    client, thread_id = result.client, result.thread_id
                if _continuation_is_owed(session_dir, name):
                    # A rejected submission's prompt was already handed to Codex; only the continuation is owed.
                    message = ""
                if _end_marker_path(session_dir, name).exists():
                    raise SystemExit(f"thread {thread_id}: ended while waiting; no turn started")
                # The following stream owns every notice, including ones the
                # move's RPCs buffered. Waiting must neither pop those notices
                # nor let their continued presence trigger a busy retry loop.
                client.wait_for_activity(events, min(deadline, time.monotonic() + UNSEEN_END_CHECK_SECS))
        raise SystemExit(f"thread {thread_id}: still live at the timeout; no new turn started")

    except (ConnectionError, OSError, SystemExit) as error:
        if client is not initial_client:
            client.close()
        if isinstance(quota_recovery, WorkerAwaitingQuotaRecovery):
            refusal = quota_recovery.refusal
            detail = f"{refusal}; recovery stopped: {error}"
            if isinstance(error, WorkerMoveUncertain):
                raise WorkerMoveUncertain(thread_id, WorkerThreadStateUnknown(
                    detail, error.cleanup, refusal.serving, True)) from error
            raise WorkerQuotaRecoveryStopped(detail, refusal.serving, client.port, thread_id) from error
        raise


def _registry_shell(name: str, script: str, *arguments: str,
                    configuration: models_registry.LaunchModelConfiguration, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; models_launch_configuration || exit 2; ' + script,
         name, str(Path(__file__).with_name("models_config.sh")), *arguments],
        capture_output=True, text=True, timeout=timeout, env=configuration.environment(os.environ))


def _turn_service_tier(client: Client, args: argparse.Namespace,
                       configuration: models_registry.LaunchModelConfiguration) -> str:
    """Resolve the current task's tier once for each turn, without a server cache."""
    session_dir = _as_str(_attr(args, "session_dir")) or client.session_dir
    name = _as_str(_attr(args, "to")) or _as_str(_attr(args, "name")) or client.name
    log_file = _as_str(_attr(args, "log_file"))
    try:
        record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)) if session_dir else {}
        task = _as_str(record.get("task"))
        resolved = _registry_shell("codex-mesh-tier", 'agents_turn_service_tier "$2"', task or "delegate",
                                   configuration=configuration, timeout=5)
        if resolved.returncode:
            raise ValueError(resolved.stderr.strip() or "agent tier resolver failed")
        if resolved.stderr.strip():
            raise ValueError(resolved.stderr.strip())
        return _as_str(resolved.stdout).strip() or "default"
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        detail = f"[{_now_stamp()}] mesh: {name}: cannot resolve service tier: {error}\n"
        try:
            if log_file:
                with Path(log_file).open("a", encoding="utf-8") as log:
                    _ = log.write(detail)
        except OSError:
            print(detail, end="", file=sys.stderr)
        return "default"


def _turn_model_effort(args: argparse.Namespace,
                       configuration: models_registry.LaunchModelConfiguration) -> tuple[str, str]:
    """Select explicit dispatch settings or the current role default at send time."""
    role = _roster_role(_attr(args, "role"))
    # Freestanding resident threads may omit assignment metadata. Their task
    # still selects a live registry default; an unnamed task uses direct.impl.
    task = f"direct.{role.role}" if isinstance(role, AssignedWorkerRole) else (
        _as_str(_attr(args, "task")) or "direct.impl")
    model = _as_str(_attr(args, "model"))
    effort = _as_str(_attr(args, "effort"))
    resolved = _registry_shell("mesh-turn", '''AGENTS_CALLER_FAMILY=codex agents_resolve "$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_MODEL}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "$2" "$AGENT_FAMILY" "$pair" || exit 2
fi
printf '%s\\n' "$AGENT_MODEL" "$AGENT_EFFORT"
''', task, model, effort, configuration=configuration, timeout=30)
    if resolved.returncode:
        raise SystemExit(resolved.stderr.strip() or "cannot resolve turn model and effort")
    fields = resolved.stdout.splitlines()
    if len(fields) != 2:
        raise SystemExit("turn model resolver returned an invalid model/effort pair")
    return fields[0], fields[1]


def _accepted_assignment(client: Client, thread_id: str, message: str) -> str:
    result = _serving_request(client, "thread/read", {"threadId": thread_id, "includeTurns": True})
    turns = _as_dict(result.get("thread")).get("turns")
    if not isinstance(turns, list):
        raise SystemExit("initial dispatch is unresolved: server returned no turn history")
    for raw in cast(list[object], turns):
        turn = _as_dict(raw)
        items = turn.get("items", [])
        for value in cast(list[object], items) if isinstance(items, list) else []:
            item = _as_dict(value)
            content = item.get("content", [])
            if item.get("type") == "userMessage" and isinstance(content, list) and any(
                    isinstance(text := _as_dict(part).get("text"), str) and text.endswith(message) for part in cast(list[object], content)):
                return _as_str(turn.get("id"))
    return ""


def _start_turn(
    client: Client, args: argparse.Namespace, thread_id: str, worker_sandbox: str, message: str,
    pending: tuple[str, ...] = (),
) -> str:
    inputs: list[dict[str, str]] = [{"type": "text", "text": message}]
    inputs.extend({"type": "text", "text": item} for item in pending)
    images = _attr(args, "image")
    if isinstance(images, list):
        inputs.extend(
            {"type": "localImage", "path": path}
            for path in cast("list[object]", images) if isinstance(path, str)
        )
    params: dict[str, object] = {"threadId": thread_id, "input": inputs}
    cwd = _as_str(_attr(args, "turn_cwd")) or _as_str(_attr(args, "cwd"))
    if cwd:
        params["cwd"] = cwd
    # The server keeps a thread's last turn policy. Each turn names both policies
    # to prevent inherited approval prompts or a previous read-only sandbox.
    params["approvalPolicy"] = "never"
    params["sandboxPolicy"] = _turn_sandbox_policy(
        READ_ONLY_SANDBOX if _attr(args, "read_only") is True else worker_sandbox)
    # Resolve at every submission, including continuations and capacity retries.
    # Roster/identity values are display history, never input to this choice.
    fresh = isinstance(_attr(args, "launch_configuration"), models_registry.FreshLaunchConfiguration)
    configuration = models_registry.launch_configuration({} if fresh else os.environ)
    args.launch_configuration = models_registry.FreshLaunchConfiguration()
    model, effort = _turn_model_effort(args, configuration)
    params["model"], params["effort"] = model, effort
    service_tier = _turn_service_tier(client, args, configuration)
    params["serviceTier"] = service_tier
    client.turn_submitted_at = time.time()
    request = _as_str(_attr(args, "switch_request"))
    allowed: WorkerStartAllowed | None = None
    accepted = ""
    if request:
        allowed = worker_admission.admit(_as_str(_attr(args, "name")), Path(client.session_dir),
                                         reference=_as_str(_attr(args, "staff_id")))
        previous = worker_admission.dispatch_record(allowed.run, allowed.name)
        if previous.assignment_id == request:
            if previous.state is worker_admission.SwitchState.DISPATCH_IN_FLIGHT:
                accepted = _accepted_assignment(client, thread_id, message)
            worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT, expected_request=request)
    result = {} if accepted else _serving_request(client, "turn/start", params, submits_work=True)
    turn_id = accepted or _as_str(_as_dict(result.get("turn")).get("id"))
    if turn_id:
        if allowed is not None:
            worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.ASSIGNMENT_ACCEPTED, expected_request=request)
        args.switch_request = ""
        session_dir = _as_str(_attr(args, "session_dir"))
        name = _as_str(_attr(args, "name")) or _as_str(_attr(args, "to"))
        with worker_lifecycle.locked_state(_session_path(session_dir, ROSTER_FILE)) as handle:
            roster = _load_roster_for_write(handle)
            entry = _as_dict(roster.get(name))
            if session_staff.worker_session(entry) == thread_id:
                entry.update({"turn_model": model, "turn_effort": effort})
                _advance_roster_revision(entry)
                roster[name] = entry
                session_staff.publish_worker_roster(handle, roster)
        log_file = _as_str(_attr(args, "log_file"))
        if log_file:
            with Path(log_file).open("a", encoding="utf-8") as log:
                _ = log.write(f"turn/start thread={thread_id} turn={turn_id} model={model} effort={effort}\n")
    receipt_file = _as_str(_attr(args, "submission_receipt_file"))
    if turn_id and receipt_file:
        with Path(receipt_file).open("a", encoding="utf-8") as receipt:
            _ = receipt.write(json.dumps({"thread_id": thread_id, "turn_id": turn_id,
                                          "messages": [message, *pending]}) + "\n")
    return turn_id


def _log_uninterrupted_thread(log_path: Path, thread_id: str, reason: str) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        _ = log.write(
            f"[{_now_stamp()}] thread {thread_id} could not be interrupted ({reason})\n"
        )


def _end_unwatched_turn(
    port: int, thread_id: str, log_path: Path
) -> UnwatchedTurnCleanupResult:
    """Best effort cleanup when this launcher cannot watch another turn."""
    interrupt_requested = False
    try:
        with worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS) as deadline:
            client = Client(port, f"cleanup-{os.getpid()}", operation_deadline=deadline)
            try:
                _ = _drop_queued_messages(client, thread_id)
                live = _read_live_turn(client, thread_id)
                turn_id = _interruptible_turn_id(client, thread_id, live)
                if isinstance(turn_id, ThreadLive):
                    reply = _interrupt_turn(client, thread_id, turn_id.turn_id)
                    interrupt_requested = True
                    if "error" in reply:
                        reason = _as_str(_as_dict(reply["error"]).get("message")) or "turn/interrupt failed"
                        remaining = _read_live_turn(client, thread_id)
                        if isinstance(remaining, (ThreadLive, ThreadActiveWithoutTurn)):
                            _log_uninterrupted_thread(log_path, thread_id, reason)
                            return RelaunchBlockedByLiveTurn(interrupt_requested=True)
                        if isinstance(remaining, ThreadStateUnknown):
                            _log_uninterrupted_thread(log_path, thread_id, reason)
                elif isinstance(turn_id, (ThreadStateUnknown, ThreadActiveWithoutTurn)):
                    reason = turn_id.reason if isinstance(turn_id, ThreadStateUnknown) else "no turn id"
                    _log_uninterrupted_thread(log_path, thread_id, reason)
                    if isinstance(turn_id, ThreadActiveWithoutTurn):
                        return RelaunchBlockedByLiveTurn()
            finally:
                client.close()
    except (ConnectionError, OSError, SystemExit) as exc:
        try:
            _log_uninterrupted_thread(log_path, thread_id, str(exc))
        except OSError as log_error:
            print(f"codex_mesh: thread {thread_id}: cleanup failed ({exc}); cleanup log failed ({log_error})",
                  file=sys.stderr)
    return RelaunchAllowed(interrupt_requested=interrupt_requested)


def command_send(args: argparse.Namespace) -> int:
    with repeat_guard.accounting_scope(owner=_attr(args, "no_accounting") is not True,
                                       report=_attr(args, "report_recipient") is True):
        return _command_send(args)


def _command_send(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    text = _message_text(args)
    guard_text = _as_str(_attr(args, "guard_text")) or text
    record = _lookup(session_dir, target)
    design_id = record.get("held_design_id")
    if design_id:
        receiver = repeat_guard.recipient_identity("", thread_id=_as_str(session_staff.worker_session(record)))
        sender = _as_str(_attr(args, "sender")) or "codex-mesh"
        if _attr(args, "repeat_checked") is not True:
            decision = repeat_guard.transport_check(sender, receiver, guard_text, human=_attr(args, "human") is True)
            if decision.held:
                print(f"HELD: repeat guard: copy {decision.copies}")
                return 4
        from scripts.astra import design_roles
        role = record.get("held_role")
        if role not in ("designer", "reviewer"):
            raise SystemExit("codex_mesh: unknown held role")
        _ = design_roles.queue_message(design_id, role, _as_str(_attr(args, "source")) or "mesh send", text)
        if _attr(args, "repeat_checked") is not True:
            repeat_guard.transport_result(sender, receiver, guard_text, "queued", human=_attr(args, "human") is True)
        print(f"queued for {target}; holder adds it to the next authorized assignment")
        return 0
    if record.get("status") == CAPACITY_EXHAUSTED:
        print(
            f"codex_mesh: {target} is out of retries and has no launcher; a message would start a turn nobody watches (codex_mesh.py end --to {target})",
            file=sys.stderr,
        )
        return 2
    outcome = hold_message(session_dir, target, text, without_launcher=True,
                           sender=_as_str(_attr(args, "sender")) or "codex-mesh",
                           human=_attr(args, "human") is True,
                           repeat_checked=_attr(args, "repeat_checked") is True,
                           guard_text=guard_text)
    if isinstance(outcome, RepeatHeld):
        print(f"HELD: repeat guard: copy {outcome.copies}")
        return 4
    if isinstance(outcome, WorkerEnded):
        print(f"codex_mesh: {outcome.reason}; message not delivered", file=sys.stderr)
        return 2
    if isinstance(outcome, HeldForNextDispatch):
        print(f"queued for {target}; no launcher is attached, so the message waits for its next dispatch")
        return 0
    if isinstance(outcome, HeldForLauncher):
        home = workers_home()
        # Retiring is enough here. A live launcher keeps its outgoing connection
        # and claims this held message at its next checked turn boundary.
        with worker_lifecycle.membership_operation(session_dir), _server_lock(session_dir):
            current = _read_json_object(_session_path(session_dir, SERVER_FILE))
            if current and server_record_home(current) != home:
                _retire_record(session_dir, current)

    print(f"queued for {target}")
    return 0


def command_steer(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, target):
        record = _lookup(session_dir, target)
        turn_id = record.get("turn_id", "")
        status = record.get("status", "unknown")
        thread_id = session_staff.worker_session(record)
        if not turn_id or status != "running":
            detail = f"status {status}"
            raise SystemExit(
                f"codex_mesh: {target} has no running turn to steer ({detail}); use `send`"
            )
        receiver = repeat_guard.recipient_identity("", thread_id=thread_id)
        decision = repeat_guard.transport_check("codex-mesh-steer", receiver, _message_text(args), human=_attr(args, "human") is True)
        if decision.held:
            print(f"HELD: repeat guard: copy {decision.copies}")
            return 4
        client = Client(_worker_port(session_dir, record), f"steer-{os.getpid()}")
        message = _message_text(args)
        reply = client.call(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": message}],
            },
        )
        # The roster learns a new turn id only from the `start` loop streaming
        # `turn/started`. Once that launcher is gone, a queued message still opens
        # a new turn and the recorded id goes stale; the server names the live one
        # in its refusal, so steer that turn and record it.
        live = _live_turn_from_mismatch(reply)
        if live:
            reply = client.call(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": live,
                    "input": [{"type": "text", "text": message}],
                },
            )
            if not reply.get("error"):
                if not _update_roster_if_expected(
                    session_dir,
                    target,
                    {"thread_id": thread_id, "turn_id": live, "status": "running"},
                    RosterUnchanged(thread_id, _roster_revision(record)),
                ):
                    client.close()
                    print(f"codex_mesh: {target}: steered, but the roster changed; current worker state preserved",
                          file=sys.stderr)
                    return 2
        _ = _require(reply, "turn/steer")
        client.close()
        repeat_guard.transport_result("codex-mesh-steer", receiver, message, "sent", human=_attr(args, "human") is True)
        print(f"steered {target}")
        return 0


def command_interrupt(args: argparse.Namespace) -> int:
    """Stop a worker's running turn without ending the worker.

    `steer` only adds input to the turn and `end` ends the worker, so neither stops just the turn.
    The message, if any, is held first, so the launcher opens the next turn from it once the
    interrupted turn ends; with no launcher attached it waits for the worker's next dispatch.
    """
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    message = _message_text(args)
    held = ""
    if message:
        outcome = hold_message(session_dir, target, message, without_launcher=True,
            sender=_as_str(_attr(args, "sender")) or "codex-mesh", human=_attr(args, "human") is True)
        if isinstance(outcome, RepeatHeld):
            print(f"HELD: repeat guard: copy {outcome.copies}; nothing interrupted")
            return 4
        if isinstance(outcome, WorkerEnded):
            print(f"codex_mesh: {outcome.reason}; nothing held or interrupted", file=sys.stderr)
            return 2
        held = ("; the message opens its next turn" if isinstance(outcome, HeldForLauncher)
                else "; no launcher is attached, so the message waits for its next dispatch")
    with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, target):
        record = _lookup(session_dir, target)
        launcher_pid = _as_int(record.get("launcher_pid"))
        if _pid_alive(launcher_pid):
            port = _worker_port(session_dir, record)
        else:
            availability = _server_availability(session_dir)
            if not isinstance(availability, LiveServer):
                if availability.doubt:
                    print(f"codex_mesh: {target}: server evidence unavailable: {availability.doubt}; nothing interrupted{held}", file=sys.stderr)
                    return 1
                print(f"{target} has no running app-server; no turn to interrupt{held}")
                return 0
            port = availability.port
        thread_id = session_staff.worker_session(record)
        client = Client(port, f"interrupt-{os.getpid()}")
        try:
            state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
            if isinstance(state, ThreadIdle):
                print(f"{target} has no running turn; nothing interrupted{held}")
                return 0
            if isinstance(state, ThreadStateUnknown):
                print(f"codex_mesh: {target}: thread {thread_id} could not be read ({state.reason}){held}",
                      file=sys.stderr)
                return 1
            if isinstance(state, ThreadActiveWithoutTurn):
                print(f"codex_mesh: {target}: thread {thread_id} could not be interrupted (no turn id){held}",
                      file=sys.stderr)
                return 1
            reply = _interrupt_turn(client, thread_id, state.turn_id)
        finally:
            client.close()
        error = _as_str(_as_dict(reply.get("error")).get("message"))
        if error:
            print(f"codex_mesh: {target}: turn {state.turn_id} not interrupted: {error}{held}", file=sys.stderr)
            return 1
        print(f"interrupted {target}'s turn {state.turn_id}{held}")
        return 0


def _live_turn_from_mismatch(reply: RpcMessage) -> str:
    """The active turn id a `turn/steer` refusal names, or "" for any other
    reply."""
    text = _as_str(_as_dict(reply.get("error")).get("message"))
    marker = "but found `"
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker) :]
    end = rest.find("`")
    return rest[:end] if end > 0 else ""


def command_end(args: argparse.Namespace) -> int:
    """End one identity while excluding launchers through inbox -> roster locks.

    Keep both locks through bounded server cleanup: a reopen or follow on the
    same thread must not slip between the terminal write and interruption.
    Drain queued submissions before interrupting: each queued message can open
    another turn. End never starts a server, and retrying an ended worker
    retries cleanup that may have exhausted its budget.
    Cleanup-only refuses any status other than ended before changing worker state.
    """
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    with unit_workers.ending_worker(Path(session_dir), target) as remember_end, worker_lifecycle.membership_operation(session_dir) as membership:
        with worker_lifecycle.lock_worker(session_dir, target):
            result = end_worker_locked(args)
        roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
        entry = _as_dict(roster.get(target))
        if entry.get("status") != "ended":
            return result
        remember_end()
        if not entry.get("held_design_id"):
            if all(_as_dict(worker).get("status") == "ended" for worker in roster.values()):
                with worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS) as deadline:
                    with _bulk_workers(session_dir, deadline=deadline, membership=membership) as workers, _server_lock(session_dir, deadline=deadline):
                        _reap_run_servers(session_dir, operation=workers)
        return result


def end_worker_locked(args: argparse.Namespace) -> int:
    """Run the one end operation for a caller already holding its lifecycle lock."""
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    with _pending_file(session_dir, target) as pending_file:
        with worker_lifecycle.locked_state(_session_path(session_dir, ROSTER_FILE)) as handle:
            roster = _load_roster_for_write(handle)
            record = _as_dict(roster.get(target))
            if not record:
                raise SystemExit(f"codex_mesh: no worker named '{target}'")
            thread_id = _as_str(session_staff.worker_session(record))
            expected = _as_str(_attr(args, "expected_thread_id"))
            if expected and thread_id != expected:
                print(f"codex_mesh: {target}: thread changed; nothing ended", file=sys.stderr)
                return 2
            status = _as_str(record.get("status")) or "unknown"
            if status == "ended" and not thread_id and record.get("staff_id"):
                print("unchanged")
                return 0
            deadline = time.time() + END_OPERATION_TIMEOUT_SECS
            if _attr(args, "idle_only"):
                reason = "worker is running" if status in ("running", "starting", WAITING_CAPACITY) else _worker_idle_reason_with_inbox(
                    session_dir, target, record, pending_file, paused=False, adopting=True, deadline=deadline,
                    membership=staff.local_snapshot())
                if reason:
                    print(f"codex_mesh: {target}: {reason}; nothing ended", file=sys.stderr)
                    return 2
            if status not in ENDABLE_STATUSES and status != "ended":
                print(f"{target} is {status}; nothing to end")
                return 0
            _end_marker_path(session_dir, target).touch()
            if not record.get("held_design_id"):
                _write_pending(pending_file, [])
            record["status"] = "ended"
            _advance_roster_revision(record)
            roster[target] = record
            session_staff.publish_worker_roster(handle, roster)
            availability = _server_availability(session_dir)
            if isinstance(availability, ServerRestartRequired) and availability.doubt:
                print(f"codex_mesh: {target}: server cleanup unconfirmed: {availability.doubt}", file=sys.stderr)
                return 1
            dropped = 0
            if isinstance(availability, LiveServer):
                if time.time() >= deadline:
                    print(f"codex_mesh: {target}: server cleanup timed out", file=sys.stderr)
                    return 1
                client = Client(availability.port, f"end-{os.getpid()}", deadline=deadline)
                try:
                    if record.get("held_design_id"):
                        def park(message: str, submission: str) -> None:
                            _park_submission(pending_file, message, submission)
                        _move_queue_to_pending(client, thread_id, park)
                    else:
                        dropped = _drop_queued_messages(client, thread_id)
                    state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
                    if isinstance(state, ThreadLive):
                        _ = _interrupt_turn(client, thread_id, state.turn_id)
                    if isinstance(state, ThreadStateUnknown):
                        print(f"codex_mesh: {target}: thread {thread_id} could not be read ({state.reason})", file=sys.stderr)
                        return 1
                    if isinstance(state, ThreadActiveWithoutTurn):
                        print(f"codex_mesh: {target}: thread {thread_id} could not be interrupted (no turn id)", file=sys.stderr)
                        return 1
                finally:
                    client.close()
    suffix = f", dropped {dropped} queued message(s)" if dropped else ""
    print(f"ending {target}{suffix}")
    return 0


# One short budget covers connection, initialization, idle probes and cleanup.
END_OPERATION_TIMEOUT_SECS = 5.0

# Upper bound on queue reads while draining; a list returns one page at a time.
END_QUEUE_DRAIN_ROUNDS = 50


def _drop_queued_messages(client: Client, thread_id: str) -> int:
    """Delete every message queued for a thread; returns how many went."""
    dropped = 0
    for _ in range(END_QUEUE_DRAIN_ROUNDS):
        listed = client.call("thread/queue/list", {"threadId": thread_id})
        items = _as_dict(listed.get("result")).get("data")
        if not isinstance(items, list) or not items:
            break
        for item in cast("list[object]", items):
            submission = _as_str(_as_dict(item).get("id"))
            if not submission:
                continue
            reply = client.call(
                "thread/queue/delete",
                {"threadId": thread_id, "queuedSubmissionId": submission},
            )
            if not reply.get("error"):
                dropped += 1
    return dropped


def _read_live_turn(client: Client, thread_id: str) -> ThreadState:
    """Read the active turn without sending text into the thread."""
    reply = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    error = reply.get("error")
    if error:
        return ThreadStateUnknown(_as_str(_as_dict(error).get("message")) or "thread/read failed")
    thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
    if not thread:
        return ThreadStateUnknown("thread/read returned no thread")
    status = _as_str(_as_dict(thread.get("status")).get("type"))
    # app-server/src/thread_status.rs: systemError means the last turn failed; none is active.
    if _thread_has_no_active_turn(status):
        return ThreadIdle()
    if status != "active":
        return ThreadStateUnknown(f"unrecognized status {status or '(missing)'}")
    for turn in _recent_turns(client, thread_id):
        if turn.get("status") == "inProgress":
            turn_id = _as_str(turn.get("id"))
            if turn_id:
                return ThreadLive(turn_id)
    # Active is enough to refuse a relaunch, even if the server has not yet
    # persisted the new turn in the read response.
    return ThreadActiveWithoutTurn()


def _interrupt_turn(client: Client, thread_id: str, turn_id: str) -> RpcMessage:
    """Interrupt one identified turn without conflating account moves with user stops."""
    return client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})


def _interruptible_turn_id(client: Client, thread_id: str, state: ThreadState) -> ThreadState:
    if not isinstance(state, ThreadActiveWithoutTurn):
        return state
    # This probe can steer only if the expected id matches. An empty id cannot
    # match an active turn, and the mismatch names the real id.
    reply = client.call("turn/steer", {
        "threadId": thread_id, "expectedTurnId": "", "input": []
    })
    turn_id = _live_turn_from_mismatch(reply)
    if turn_id:
        return ThreadLive(turn_id)
    reread = _read_live_turn(client, thread_id)
    return reread


def command_list(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    if not roster:
        print("no workers registered")
        return 0
    for name in sorted(roster):
        entry = roster.get(name)
        if not isinstance(entry, dict):
            continue
        record = cast("ThreadRecord", cast("object", entry))
        print(f"{name}\t{record.get('status', '?')}\t{session_staff.worker_session(record)}")
    return 0



def command_reopen(args: argparse.Namespace) -> int:
    """Recover an ended or abandoned worker without creating a new thread."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, name):
        record = _lookup(session_dir, name)
        if record.get("held_design_id"):
            raise SystemExit("codex_mesh: held roles recover through design_roles.py")
        finished = finished_run_status(session_dir)
        if finished:
            print(f"codex_mesh: run finished ({finished}); use progress_history.py start-phase to reactivate this run, "
                  + f"or workers.py resolve --session-dir <new-run> --role {_as_str(session_staff.worker_name(record)) or 'impl'} "
                  + "to carry its worker forward", file=sys.stderr)
            return 2
        status = _as_str(record.get("status"))
        thread_id = _as_str(session_staff.worker_session(record))
        launcher = _as_int(record.get("launcher_pid"))
        revision = _roster_revision(record)
        reason = ""
        if status not in ROSTER_LIVE | {WAITING_CAPACITY, "ended"}:
            reason = "worker is not ended, running or starting"
        elif not thread_id:
            reason = "missing thread id"
        elif _pid_alive(launcher):
            reason = "launcher is alive"
        recovered_idle = False
        # An ended worker with no server retains the established reopen behavior.
        # An abandoned live status always needs positive same-thread evidence.
        availability = _server_availability(session_dir)
        if not reason and isinstance(availability, ServerRestartRequired) and availability.doubt:
            reason = f"no live server evidence: {availability.doubt}"
        if not reason and (status != "ended" or _run_servers(session_dir)):
            reason = _recovery_idle_reason(session_dir, name, thread_id)
            recovered_idle = not reason
        if reason:
            prefix = "launcher died; " if status in ROSTER_LIVE and launcher > 0 and not _pid_alive(launcher) else ""
            print(f"codex_mesh: {name}: {prefix}{reason}; pending work kept; reopen refused", file=sys.stderr)
            return 2
        path = _session_path(session_dir, ROSTER_FILE)
        with worker_lifecycle.locked_state(path) as handle:
            roster = _load_roster_for_write(handle)
            entry = _as_dict(roster.get(name))
            if (session_staff.worker_session(entry) != thread_id or _roster_revision(entry) != revision
                    or _as_int(entry.get("launcher_pid")) != launcher
                    or entry.get("status") != status):
                print(f"codex_mesh: {name}: worker changed during idle recovery; reopen refused", file=sys.stderr)
                return 2
            _end_marker_path(session_dir, name).unlink(missing_ok=True)
            entry.update({"status": "paused", "paused_from": status, "paused_turn": status != "ended",
                          "dead_launcher_recovery": True})
            if recovered_idle:
                entry.update({"context_tokens": "not_measured_since_compaction", "paused_turn": False})
                _ = entry.pop("turn_id", None)
                _ = entry.pop("launcher_pid", None)
            _advance_roster_revision(entry)
            roster[name] = entry
            session_staff.publish_worker_roster(handle, roster)
        if _attr(args, "quiet") is not True:
            if recovered_idle:
                print(f"reopened {name} on its idle thread {thread_id}; "
                      + "context size not measured since compaction; " + f"send its next turn with --to {name}")
            else:
                print(f"reopened {name}; send its next turn with --to {name}")
        return 0


def _recovery_idle_reason(session_dir: str, name: str, thread_id: str) -> str:
    """Prove idle on the recorded servers, or resume that exact thread to prove it.

    The lifecycle lock is held, but never the roster lock, during these probes.
    A retired server can be the only process that knows the thread.
    """
    deadline = time.time() + THREAD_RELEASE_TIMEOUT_SECS
    availability = _server_availability(session_dir)
    current_idle = False
    try:
        for port in _run_servers(session_dir):
            client = Client(port, f"recover-{name}", deadline=deadline)
            try:
                if thread_id not in _loaded_thread_ids(client, time.monotonic() + max(0.0, deadline - time.time())):
                    continue
                live = _read_live_turn(client, thread_id)
                if not isinstance(live, ThreadIdle):
                    return live.reason if isinstance(live, ThreadStateUnknown) else "thread has a live turn"
                current_idle |= isinstance(availability, LiveServer) and port == availability.port
            finally:
                client.close()
        if current_idle:
            return ""
        port, _fresh = ensure_server(session_dir)
        released = _release_other_worker_writers(session_dir, name, thread_id, port)
        if not isinstance(released, _ReleasedWorkerThread):
            return released.reason
        client = Client(port, f"recover-{name}", deadline=deadline)
        try:
            result = _require(client.call("thread/resume", _resume_params(session_dir, name, thread_id)), "thread/resume")
            if _thread_id(result) != thread_id:
                return "resumed thread identity differs; replacement refused"
            live = _read_live_turn(client, thread_id)
            if not isinstance(live, ThreadIdle):
                return live.reason if isinstance(live, ThreadStateUnknown) else "thread has a live turn"
        finally:
            client.close()
        return ""
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as error:
        return f"cannot establish idle state: {error}"


from scripts.agents.worker_lifecycle import finished_run_status as finished_run_status


def worker_launcher_attached(record: Mapping[str, object]) -> bool:
    """A launcher claim is occupied while its recorded process is alive."""
    return _pid_alive(_as_int(record.get("launcher_pid")))


def worker_idle_reason(session_dir: str, name: str, record: Mapping[str, object],
                       *, membership: staff.StaffSnapshot, paused: bool = False, adopting: bool = False) -> str:
    """Require positive idle evidence before a unit worker changes run owners."""
    try:
        with _pending_file(session_dir, name, blocking=False) as inbox:
            return _worker_idle_reason_with_inbox(
                session_dir, name, record, inbox, paused=paused, adopting=adopting,
                deadline=time.time() + THREAD_RELEASE_TIMEOUT_SECS, membership=membership)
    except BlockingIOError:
        # Adoption holds the roster: never wait for an inbox in that order.
        return "pending inbox is in use; retry allocation"


def _worker_idle_reason_with_inbox(
    session_dir: str, name: str, record: Mapping[str, object], inbox: worker_lifecycle.WorkerStateFile,
    *, paused: bool, adopting: bool, deadline: float, membership: staff.StaffSnapshot,
) -> str:
    """One idle rule over a caller-owned snapshot and locked inbox.

    Adoption and explicit end may carry or cancel queued work; reservations,
    launchers, detached runs and active turns still prevent an idle transition.
    """
    thread_id = session_staff.worker_session(record, membership)
    if not thread_id:
        return "missing thread id"
    if record.get("held_design_id"):
        return "held roles transfer through design_roles.py"
    if not paused and unit_workers.allocation_is_live(record) and not finished_run_status(session_dir):
        return "worker is reserved for its next dispatch"
    if worker_launcher_attached(record):
        return "launcher is attached"
    if not (paused or adopting) and record.get("continuation_owed") is True:
        return "continuation is owed"
    if not (paused or adopting) and _pending_messages(inbox):
        return "pending messages are queued"
    runs = DetachedRunWatch(session_dir, name).current()
    if not paused and (runs.live or runs.dead):
        return "detached run is live or its result is not reaped"
    try:
        # An account move can leave the outgoing thread on a retired server.
        # Every live server must either not hold it or positively report idle.
        # The caller may hold this run's roster lock. Start from its supplied
        # snapshot rather than recursively acquiring a shared lock on that file.
        ports = _run_servers(session_dir)
        for port in sorted(ports):
            if time.time() >= deadline:
                return "idle check timed out"
            client = Client(port, f"idle-check-{name}", deadline=deadline)
            try:
                if thread_id not in _loaded_thread_ids(client, time.monotonic() + max(0.0, deadline - time.time())):
                    continue
                queued = _require(client.call("thread/queue/list", {"threadId": thread_id}), "thread/queue/list")
                if queued.get("data") and not (paused or adopting):
                    return "server messages are queued"
                live = _read_live_turn(client, thread_id)
                if isinstance(live, ThreadStateUnknown):
                    return live.reason
                if isinstance(live, (ThreadLive, ThreadActiveWithoutTurn)):
                    return "live turn"
            finally:
                client.close()
        return ""
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as error:
        return f"cannot establish idle state: {error}"


@dataclass(frozen=True)
class NoThreadToRelease:
    """No unrecorded allocation is owned by this operation."""


@dataclass(frozen=True)
class UnrecordedWorkerThread:
    thread_id: str


def open_unit_worker(session_dir: str, name: str, role: str, *, model: str = "",
                     effort: str = "", cwd: str = "",
                     configuration: models_registry.LaunchModelConfiguration) -> dict[str, object]:
    """Open an idle thread; assignment starts its first turn later through follow."""
    try:
        admitted = worker_admission.admit(name, Path(session_dir))
    except ValueError as error:
        raise unit_workers.WorkerUnavailable(str(error)) from error
    with worker_admission.worker_operation(admitted):
        return _open_admitted_worker(session_dir, role, admitted, model=model, effort=effort, cwd=cwd, configuration=configuration)


def _open_admitted_worker(session_dir: str, role: str, admitted: WorkerStartAllowed, *,
                          model: str, effort: str, cwd: str,
                          configuration: models_registry.LaunchModelConfiguration) -> dict[str, object]:
    name = admitted.name
    with worker_lifecycle.membership_operation(session_dir):
        with contextlib.ExitStack() as lifecycle:
            _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
            resolved = _registry_shell("workers",
                'agents_resolve "direct.$2" || exit; printf "%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}"',
                role, configuration=configuration, timeout=30)
            fields = resolved.stdout.strip().split()
            if resolved.returncode or len(fields) != 4:
                raise unit_workers.WorkerUnavailable(resolved.stderr.strip() or "cannot resolve worker model")
            family, configured_model, configured_effort, service_tier = fields
            model = model or configured_model
            effort = effort or configured_effort
            if family != "codex":
                raise unit_workers.WorkerUnavailable("new persistent worker requires a Codex assignment")
            membership_record = staff.local_snapshot()
            cwd = cwd or str(session_staff.worker_directory(membership_record, admitted.parent))
            home = workers_home()
            port, fresh = ensure_server(session_dir, home, cwd=cwd)
            client = Client(port, name)
            allocation: NoThreadToRelease | UnrecordedWorkerThread = NoThreadToRelease()
            try:
                _ = _bind_serving_server(client, session_dir, started_here=fresh)
                policy: dict[str, object] = {"kind": "ReadOnlyReviewer" if role == "review" else "WorkerLaunchPolicy"}
                launch = argparse.Namespace(cwd=cwd, model=model,
                                            sandbox=READ_ONLY_SANDBOX if role == "review" else WORKER_SANDBOX,
                                            service_tier=service_tier)
                lifecycle.close()
                previous = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                existing = admitted.slot.row if isinstance(admitted.slot, worker_admission.ExistingWorkerSlot) else None
                params = _thread_start_params(launch, Path(cwd))
                resume = existing is not None and isinstance(membership_record.backends.get(existing.id), staff.CodexBackend)
                if resume and existing is not None:
                    params["threadId"] = existing.current_session
                published = _create_published_worker(client, session_dir, admitted,
                    "thread/resume" if resume else "thread/start", params)
                _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
                thread_id = published.row.current_session
                allocation = UnrecordedWorkerThread(thread_id)
                name = published.row.name
                display_name = unit_workers.shown_name(Path(session_dir), name, role=role)
                _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": display_name}), "thread/name/set")
                record: FollowableRecord = {"staff_id": published.row.id, "thread_id": thread_id, "status": "paused", "role": cast(WorkerRole, role),
                                            "model": model, "effort": effort, "access_policy": policy}
                expected: ExpectedRoster = (RosterUnchanged(thread_id, _roster_revision(previous))
                                            if previous else RosterAbsent())
                if not isinstance(published.handoff, worker_admission.NoFamilyHandoff):
                    expected = RosterBackendReplaced(published.row.id, thread_id,
                        _roster_revision(previous), _as_str(previous.get("status")))
                if not _update_roster_if_expected(session_dir, name, record, expected):
                    raise unit_workers.WorkerUnavailable(f"{name}: worker name was claimed")
                allocation = NoThreadToRelease()
                _record_assignment(thread_id, cast(WorkerRole, role), None, only_if_changed=True)
                opened = {**record, "codex_home": str(home)}
            finally:
                try:
                    if isinstance(allocation, UnrecordedWorkerThread):
                        released = _release_worker_thread(client, allocation.thread_id, session_dir)
                        if isinstance(released, WorkerThreadStateUnknown):
                            print(f"codex_mesh: unrecorded thread {allocation.thread_id}: {released.reason}; release unconfirmed",
                                  file=sys.stderr)
                finally:
                    client.close()
        # Admission projection belongs here, after lifecycle and before membership release.
        return opened


def adopt_unit_worker(source: Path, destination: Path, name: str) -> str:
    """Transfer worker routes through the unit ledger."""
    adopt = cast(Callable[[Path, Path, str], str],
                 importlib.import_module("scripts.hierarchies.worker_adoption").adopt_unit_worker)
    try:
        return adopt(source, destination, name)
    except staff.StaffRefused as error:
        raise unit_workers.WorkerUnavailable(str(error)) from error


def command_adopt(args: argparse.Namespace) -> int:
    source = Path(_as_str(_attr(args, "from_run")))
    destination = Path(_as_str(_attr(args, "session_dir")))
    name = _as_str(_attr(args, "name"))
    try:
        _ = adopt_unit_worker(source, destination, name)
    except unit_workers.WorkerUnavailable as error:
        print(f"codex_mesh: {error}", file=sys.stderr)
        return 2
    return 0


def stop_worker_for_switch(row: staff.StaffRow, runs: tuple[Path, ...]) -> SourceRelease:
    """Preserve and release the source identified by its fixed staff id."""
    source = staff.local_snapshot().by_id(row.id)
    if source is None or source.current_session != row.current_session:
        print(f"anomaly: {row.id}: source identity changed", file=sys.stderr)
        return SourceReleased()
    for folder in dict.fromkeys(runs):
        try:
            with worker_lifecycle.membership_operation(folder), worker_lifecycle.lock_worker(folder, row.name):
                released = _release_source_servers(str(folder), row, deadline=worker_lifecycle.effective_deadline())
                if isinstance(released, SourceReleaseUnknown):
                    return released
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (OSError, ValueError, RuntimeError, SystemExit) as error:
            print(f"anomaly: {folder}: {error}", file=sys.stderr)
    return SourceReleased()


def _release_source_servers(session_dir: str, row: staff.StaffRow, *, deadline: float) -> SourceRelease:
    """Release loaded source threads and report unavailable server records."""
    path = _session_path(session_dir, SERVER_FILE)
    evidence = worker_lifecycle.read_json_object(path)
    retained = evidence.get("previous_servers", [])
    records: list[object] = []
    if isinstance(retained, list):
        records.extend(cast(list[object], retained))
    else:
        print(f"anomaly: {path}: invalid previous server list", file=sys.stderr)
    if any(key != "previous_servers" for key in evidence):
        records.append({key: value for key, value in evidence.items() if key != "previous_servers"})
    for raw in records:
        try:
            record = worker_lifecycle.validate_worker_state(path, raw)
        except worker_lifecycle.WorkerStateUnreadable as error:
            print(f"anomaly: {error}", file=sys.stderr)
            continue
        server = _recorded_server_availability(record)
        if not isinstance(server, LiveServer):
            continue
        try:
            with contextlib.closing(Client(server.port, f"release-{os.getpid()}", operation_deadline=deadline)) as client:
                if row.current_session in _loaded_thread_ids(client, deadline):
                    _ = _preserve_paused_thread(client, session_dir, row.name, row.current_session,
                        deadline=deadline, reference=row.id)
                    released = _release_worker_thread(client, row.current_session, session_dir)
                    if isinstance(released, WorkerThreadStateUnknown):
                        return SourceReleaseUnknown(released.reason)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (OSError, RuntimeError, SystemExit) as error:
            print(f"anomaly: {path}: source server on port {server.port}: {error}", file=sys.stderr)
    return SourceReleased()


def _preserve_worker_servers(session_dir: str, name: str, thread_id: str, *, deadline: float,
                             reference: str = "") -> tuple[bool, bool]:
    """Preserve loaded threads while the caller holds the lifecycle lock."""
    observed, interrupted = False, False
    for server_record in _recorded_run_servers(session_dir):
        server = _recorded_server_availability(server_record)
        if not isinstance(server, LiveServer):
            continue
        client = Client(server.port, f"pause-{os.getpid()}", operation_deadline=deadline)
        try:
            if thread_id not in _loaded_thread_ids(client, deadline):
                continue
            interrupted = _preserve_paused_thread(client, session_dir, name, thread_id,
                deadline=deadline, reference=reference) or interrupted
            observed = True
        finally:
            client.close()
    return observed, interrupted


def _preserve_paused_thread(client: Client, session_dir: str, name: str, thread_id: str, *, deadline: float, reference: str = "") -> bool:
    with _pending_file(session_dir, name, deadline=deadline, reference=reference) as pending:
        def park(message: str, submission: str) -> None:
            _park_submission(pending, message, submission)
        _move_queue_to_pending(client, thread_id, park)
    state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
    if isinstance(state, ThreadStateUnknown):
        raise RuntimeError(state.reason)
    if isinstance(state, ThreadActiveWithoutTurn):
        raise RuntimeError("active turn has no interruptible id")
    if isinstance(state, ThreadLive):
        _ = _interrupt_turn(client, thread_id, state.turn_id)
    return isinstance(state, ThreadLive)


@contextlib.contextmanager
def _bulk_workers(session_dir: str, *, deadline: float,
                  membership: worker_lifecycle.MembershipLease | AcquireMembership = ACQUIRE_MEMBERSHIP,
                  owned_workers: tuple[worker_lifecycle.WorkerIdentity, ...] = (),
                  allow_held_roles: bool = False) -> Generator[BulkWorkers]:
    """Validate an authoritative work set; no caller rereads membership for effects."""
    with worker_lifecycle.operation_deadline(deadline=deadline) as deadline, contextlib.ExitStack() as operation:
        if isinstance(membership, AcquireMembership):
            if any(worker.session == Path(session_dir).resolve() for worker in worker_lifecycle.held_workers()):
                raise SystemExit("RoleNeedsRecovery: shared-server maintenance needs membership before lifecycle; "
                                 + "run release-held-server, then retry through the holder")
            membership = operation.enter_context(worker_lifecycle.lock_membership(session_dir, deadline=deadline))
        elif not membership.covers(session_dir):
            raise RuntimeError("bulk operation was passed membership it does not own")
        staff.wait_for_writes()
        while True:
            records = _read_json_object(_session_path(session_dir, ROSTER_FILE))
            identities = tuple(worker_lifecycle.WorkerIdentity(Path(session_dir), name) for name in records)
            with contextlib.ExitStack() as workers:
                if owned_workers:
                    if not set(identities).issubset(owned_workers):
                        raise SystemExit("RoleNeedsRecovery: server environment changed during a partial worker operation; "
                                         + "run release-held-server, then retry through the holder")
                else:
                    _ = workers.enter_context(worker_lifecycle.lock_workers(*identities, deadline=deadline))
                validated = _read_json_object(_session_path(session_dir, ROSTER_FILE))
                if set(validated) != set(records):
                    if time.monotonic() >= deadline:
                        raise worker_lifecycle.OperationDeadlineExpired("shutdown incomplete: membership did not stabilize")
                    continue
                for value in validated.values():
                    entry = _as_dict(value)
                    if entry.get("held_design_id") and not allow_held_roles:
                        raise SystemExit("codex_mesh: raw stop refuses held roles; use design_roles.py")
                yield BulkWorkers(validated, membership, deadline)
                break
        # All worker/server/inbox/roster scopes have exited; membership remains
        # available here for a future retirement staff projection.


def _publish_bulk_records(session_dir: str, records: dict[str, object], *, deadline: float) -> None:
    """Publish only the validated work set, preserving entries outside that set."""
    path = _session_path(session_dir, ROSTER_FILE)
    with worker_lifecycle.locked_state(path, deadline=deadline) as handle:
        current = _load_roster_for_write(handle)
        current.update(records)
        session_staff.publish_worker_roster(handle, current)


@worker_lifecycle.operation_deadline(timeout=PAUSE_INTERRUPT_SECS)
def _pause_roster(session_dir: str, *, stop_servers: bool = False, deadline: float = NO_LOCK_DEADLINE) -> None:
    deadline = worker_lifecycle.effective_deadline(deadline)
    try:
        with contextlib.ExitStack() as ledger_lock:
            path = unit_workers.ledger_for_run(Path(session_dir)).path
            ledger: dict[str, object] = {}
            if path.exists():
                lock = ledger_lock.enter_context(path.with_suffix(".lock").open("a+", encoding="utf-8"))
                worker_lifecycle.acquire_flock(lock, deadline=deadline)
                ledger = _read_json_object(path)
            with _bulk_workers(session_dir, deadline=deadline) as workers, _server_lock(session_dir, deadline=deadline):
                _pause_roster_locked(session_dir, workers)
                if stop_servers:
                    _reap_run_servers(session_dir, operation=workers)
            if ledger:
                for raw in _as_dict(ledger.get("workers")).values():
                    entry = _as_dict(raw)
                    if entry.get("ownership") == {"kind": "HeldBy", "run": session_dir}:
                        entry["ownership"] = {"kind": "Paused"}
                write_text_atomic(path, json.dumps(ledger, indent=2) + "\n")
    except (worker_lifecycle.OperationDeadlineExpired, worker_lifecycle.WorkerStateUnreadable) as error:
        raise SystemExit(f"codex_mesh: shutdown incomplete; workers and servers are not confirmed stopped: {error}") from error


def _pause_roster_locked(session_dir: str, workers: BulkWorkers) -> None:
    """Fence launcher completion writes before interrupting each retained thread."""
    roster = workers.records
    for name, raw in roster.items():
        record = _as_dict(raw)
        status = _as_str(record.get("status"))
        if status == "ended":
            continue
        if status != "paused":
            record.update({"status": "paused", "paused_from": status,
                           "paused_turn": status in ROSTER_LIVE})
        _advance_roster_revision(record)
        roster[name] = record
    _publish_bulk_records(session_dir, roster, deadline=workers.deadline)
    interrupt_by = workers.deadline
    failures: list[str] = []
    for name, raw in roster.items():
        record = _as_dict(raw)
        thread_id = _as_str(session_staff.worker_session(record))
        if record.get("status") != "paused" or not thread_id:
            continue
        try:
            observed, interrupted = _preserve_worker_servers(session_dir, name, thread_id, deadline=interrupt_by)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (OSError, RuntimeError, SystemExit) as error:
            failures.append(f"{name}: pause preservation failed: {error}")
            continue
        # A thread may still have work on a retired server even when its
        # recorded port names the current server. Preserve all before reaping.
        if observed:
            record["paused_turn"] = interrupted
            _publish_bulk_records(session_dir, {name: record}, deadline=workers.deadline)
    if failures:
        raise SystemExit("codex_mesh: servers kept alive: " + "; ".join(failures))


def command_stop(args: argparse.Namespace) -> int:
    """Keep one shutdown budget from director/ledger lookup through server exit."""
    session_dir = _as_str(_attr(args, "session_dir"))
    pause = bool(_attr(args, "pause"))
    try:
        with worker_lifecycle.operation_deadline(
                timeout=PAUSE_INTERRUPT_SECS if pause else THREAD_RELEASE_TIMEOUT_SECS) as deadline:
            if pause and unit_workers.run_has_live_director(Path(session_dir)):
                print("kept workers and app-servers: unit director is live")
                return 0
            if pause:
                _pause_roster(session_dir, stop_servers=True, deadline=deadline)
            else:
                with _bulk_workers(session_dir, deadline=deadline) as workers, _server_lock(session_dir, deadline=deadline):
                    _mark_all_roster_ended(session_dir, workers.records, deadline=deadline)
                    _reap_run_servers(session_dir, operation=workers)
    except (worker_lifecycle.OperationDeadlineExpired, worker_lifecycle.WorkerStateUnreadable) as error:
        raise SystemExit(f"codex_mesh: shutdown incomplete; workers and servers are not confirmed stopped: {error}") from error
    return 0


@worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS)
def _reap_run_servers(session_dir: str, *, operation: BulkWorkers | AcquireMembership = ACQUIRE_MEMBERSHIP) -> None:
    """Select, reap and conditionally forget servers under passed or acquired ownership."""
    with contextlib.ExitStack() as locks:
        if isinstance(operation, AcquireMembership):
            deadline = worker_lifecycle.effective_deadline()
            operation = locks.enter_context(_bulk_workers(session_dir, deadline=deadline))
            _ = locks.enter_context(_server_lock(session_dir, deadline=deadline))
        elif not operation.membership.covers(session_dir):
            raise RuntimeError("run reaping requires its passed membership ownership")
        _reap_validated_servers(session_dir, operation.deadline)


class RecordedServerReap(Enum):
    NOT_SIGNALED = "not_signaled"
    STOPPED = "stopped"
    UNCONFIRMED = "unconfirmed"


def _reap_recorded_server(record: dict[str, object], deadline: float) -> RecordedServerReap:
    """Signal only the app-server whose process arguments match the record."""
    pid, port = _as_int(record.get("pid")), _as_int(record.get("port"))
    if not _pid_alive(pid):
        return RecordedServerReap.NOT_SIGNALED
    try:
        running: set[tuple[int, int]] = {(server.pid, server.port) for server in _running_servers()}
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"anomaly: app-server {pid} on port {port}: process scan failed: {error}; not signaled", file=sys.stderr)
        return RecordedServerReap.NOT_SIGNALED
    if (pid, port) not in running:
        print(f"anomaly: app-server {pid} on port {port} is not the recorded server; not signaled", file=sys.stderr)
        return RecordedServerReap.NOT_SIGNALED
    return RecordedServerReap.STOPPED if _reap(pid, deadline=deadline) else RecordedServerReap.UNCONFIRMED


def _reap_validated_servers(session_dir: str, deadline: float) -> None:
    """Remove only identities selected under this operation's server lock."""
    selected = [record for record in _recorded_run_servers(session_dir)
                if _as_int(record.get("pid")) > 0 and _as_int(record.get("port")) > 0]
    stopped: list[int] = []
    for record in selected:
        if time.monotonic() >= deadline:
            raise worker_lifecycle.OperationDeadlineExpired("shutdown incomplete: server cleanup deadline expired")
        pid, port = _as_int(record.get("pid")), _as_int(record.get("port"))
        outcome = _reap_recorded_server(record, deadline)
        if outcome is RecordedServerReap.STOPPED:
            stopped.append(pid)
        if outcome is RecordedServerReap.UNCONFIRMED and _pid_alive(pid):
            raise SystemExit(f"codex_mesh: shutdown incomplete; workers and servers are not confirmed stopped: app-server {pid} is still alive")
        _forget_run_server(Path(session_dir), pid, port)
    print(f"stopped app-server {', '.join(str(pid) for pid in stopped)}" if stopped else "no app-server running")


def _collect_exited_child(pid: int) -> None:
    """Collect an exited child without waiting for a live process."""
    with contextlib.suppress(ChildProcessError):
        _ = os.waitpid(pid, os.WNOHANG)


@worker_lifecycle.operation_deadline(timeout=2 * REAP_WAIT_SECS)
def _reap(pid: int, *, deadline: float = NO_LOCK_DEADLINE) -> bool:
    """Stop one app-server; success requires confirmed exit within the budget."""
    deadline = worker_lifecycle.effective_deadline(deadline)

    def exited() -> bool:
        # A child can report zombie state before waitpid can collect it.
        # Only waitpid confirms a child's exit. Other processes use liveness.
        try:
            return os.waitpid(pid, os.WNOHANG)[0] == pid
        except OSError:
            return not _pid_alive(pid)

    if not worker_lifecycle.is_process_pid(pid) or exited():
        return False
    if time.monotonic() >= deadline:
        return False
    try:
        _signal_process(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"could not signal app-server {pid}: {exc}")
        return False

    term_until = min(deadline, time.monotonic() + REAP_WAIT_SECS)
    while not exited() and time.monotonic() < term_until:
        time.sleep(min(REAP_POLL_SECS, max(0.0, term_until - time.monotonic())))
    if exited():
        return True
    if time.monotonic() >= deadline:
        return False
    try:
        _signal_process(pid, signal.SIGKILL)
    except OSError as exc:
        print(f"could not kill app-server {pid}: {exc}")
        return False
    while not exited() and time.monotonic() < deadline:
        time.sleep(min(REAP_POLL_SECS, max(0.0, deadline - time.monotonic())))
    return exited()


# The run roots, each holding run folders and their run-active markers: unit/, and delegate/ for
# a run started before the rename. A sweep always judges a server against every real root, since a
# server another root owns would otherwise look unowned.
SWEEP_ROOTS = unit_runs.run_roots()
# How long a server and its run folder must have sat untouched before it counts as unused: the
# one figure behind both the server's own watcher and the sweep.
SERVER_IDLE_SECS = 1800.0
# How often a server's watcher wakes to look at its record.
WATCH_WAKE_SECS = 300
# Files in a run folder whose change means the run is still working. `ensure_server` touches the
# server's record on every use.
SWEEP_ACTIVITY_FILES = (unit_runs.HEARTBEAT, "board.log", ROSTER_FILE, SERVER_LOG, SERVER_FILE)
SERVER_ARGUMENTS = ("app-server", "--listen")


@dataclass(frozen=True)
class ServerInUse:
    reason: str


@dataclass(frozen=True)
class ServerUnused:
    reason: str


SweepVerdict = ServerInUse | ServerUnused


@dataclass(frozen=True)
class CurrentRunServer:
    """A run's current mesh_server.json names this server."""

    session_dir: Path


@dataclass(frozen=True)
class HeldDesignServer:
    """A durable mesh's current server belongs to a role still held for design work."""

    session_dir: Path


@dataclass(frozen=True)
class RetiredRunServer:
    """A run retains this previous server until it becomes idle."""

    session_dir: Path


@dataclass(frozen=True)
class UnownedServerWithLog:
    """No record names this server, but its log handle identifies its old run."""

    session_dir: Path


@dataclass(frozen=True)
class StartingDevShellServer:
    """A pending dev-shell launch names this pid before its listening record is installed."""

    session_dir: Path
    started_at: float


@dataclass(frozen=True)
class UnownedServerWithoutLog:
    """No record or mesh log handle identifies this server's run."""


@dataclass(frozen=True)
class UnreadServerOwnership:
    reason: str


KnownServerOwnership = (
    CurrentRunServer
    | HeldDesignServer
    | RetiredRunServer
    | StartingDevShellServer
    | UnownedServerWithLog
    | UnownedServerWithoutLog
)
ServerOwnership = KnownServerOwnership | UnreadServerOwnership


@dataclass(frozen=True)
class SweepJudgment:
    """A verdict together with the ownership state used to reach it."""

    verdict: SweepVerdict
    ownership: ServerOwnership


@dataclass(frozen=True)
class CheckedRunLifecycle:
    """Run-liveness evidence collected by a root-wide sweep."""

    marked_active: bool
    running_workers: tuple[str, ...]


@dataclass(frozen=True)
class UncheckedRunLifecycle:
    """Run-liveness checks do not apply to this owner or this idle-watcher judgment."""


RunLifecycle = CheckedRunLifecycle | UncheckedRunLifecycle


@dataclass(frozen=True)
class ServerFacts:
    """What the sweep gathered about one running app-server without talking to it."""

    pid: int
    port: int
    age_secs: float
    clients: int
    ownership: KnownServerOwnership
    folder_exists: bool
    run_lifecycle: RunLifecycle
    # Workers in the run folder's roster whose launcher process is alive.
    live_launchers: tuple[str, ...]
    # Seconds since the run folder's newest activity file changed; None when it has none.
    quiet_secs: float | None


def _sweep_verdict(facts: ServerFacts, busy_threads: Callable[[], tuple[str, ...]]) -> SweepVerdict:
    """Whether anything still needs this app-server. Every doubt counts as in use, and the server
    is asked about its conversations last, so none can be called unused without that answer."""
    if facts.clients:
        return ServerInUse(f"{facts.clients} client connection(s) on its port")
    if isinstance(facts.ownership, CurrentRunServer) and isinstance(
        facts.run_lifecycle, CheckedRunLifecycle
    ):
        if facts.run_lifecycle.marked_active:
            return ServerInUse("its run has an active marker")
        if facts.run_lifecycle.running_workers:
            return ServerInUse(
                "its roster has a running worker: " + ", ".join(facts.run_lifecycle.running_workers)
            )
    if isinstance(facts.ownership, RetiredRunServer):
        # The run's marker, launchers and activity belong to its newer server. Only a worker still
        # attached here holds a connection, and a turn whose launcher died shows as a conversation.
        if busy := busy_threads():
            return ServerInUse("a conversation is not idle: " + "; ".join(busy))
        return ServerUnused("retired by its run; no client and no conversation mid-turn")
    if isinstance(facts.ownership, StartingDevShellServer):
        if 0 <= time.time() - facts.ownership.started_at < DEV_SHELL_START_TIMEOUT_SECS:
            return ServerInUse(f"dev-shell launch is within its {DEV_SHELL_START_TIMEOUT_SECS:.0f} s record-start window")
    if isinstance(facts.ownership, (StartingDevShellServer, UnownedServerWithLog, UnownedServerWithoutLog)):
        # A server may begin listening just before _start_server records it. Its run-folder lock
        # closes that race on the second look when the log names one; the age guard closes it on
        # platforms that cannot recover the log handle.
        if facts.age_secs < SERVER_START_TIMEOUT_SECS:
            return ServerInUse(
                f"unowned, but still within the {SERVER_START_TIMEOUT_SECS:.0f} s record-start window"
            )
        if busy := busy_threads():
            return ServerInUse("unowned, but a conversation is not idle: " + "; ".join(busy))
        return ServerUnused(
            "no current or retired server record; no client and no conversation mid-turn"
        )
    if facts.live_launchers:
        # A resident worker waits between turns with nothing on the socket.
        return ServerInUse("a worker's launcher is still running: " + ", ".join(facts.live_launchers))
    if facts.age_secs < SERVER_IDLE_SECS:
        return ServerInUse(f"started {facts.age_secs / 60:.0f} min ago")
    folder_kind = "mesh" if isinstance(facts.ownership, HeldDesignServer) else "run"
    if facts.quiet_secs is not None and facts.quiet_secs < SERVER_IDLE_SECS:
        return ServerInUse(f"its {folder_kind} folder changed {facts.quiet_secs / 60:.0f} min ago")
    if busy := busy_threads():
        return ServerInUse("a conversation is not idle: " + "; ".join(busy))
    if not facts.folder_exists:
        folder = f"its {folder_kind} folder was deleted"
    else:
        folder = "no activity file" if facts.quiet_secs is None else f"quiet for {facts.quiet_secs / 3600:.1f} h"
    if isinstance(facts.ownership, HeldDesignServer):
        return ServerUnused(f"no client, no launcher, no conversation mid-turn; mesh folder: {folder}")
    return ServerUnused(f"no client, no launcher, no active marker, no conversation mid-turn, {folder}")


def _cleanup_command(arguments: tuple[str, ...], *, empty_selection: bool = False) -> str:
    """Read a command without including its potentially private output in a failure."""
    command = shlex.join(arguments)
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OSError(f"{command}: command unavailable") from error
    if result.returncode == 0 and not result.stderr.strip():
        return result.stdout
    if empty_selection and result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return ""
    raise OSError(f"{command}: command failed (exit {result.returncode})")


def _list_server_processes() -> str:
    fields = "pid=,etime=,args=" if sys.platform == "darwin" else "pid=,etimes=,args="
    return _cleanup_command(("ps", "-eo", fields))


def _list_established_connections() -> str:
    if sys.platform == "darwin":
        return _cleanup_command(("lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"), empty_selection=True)
    return _cleanup_command(("ss", "-Htn", "state", "established"))


def _list_server_stderr(pid: int) -> str:
    return _cleanup_command(("lsof", "-p", str(pid), "-a", "-d", "2", "-Fn"), empty_selection=True)


def _server_owned_by_user(pid: int) -> bool:
    output = _cleanup_command(("ps", "-p", str(pid), "-o", "uid="))
    return output.strip() == str(os.getuid())


class WebSocketServer(NamedTuple):
    pid: int
    port: int
    age_secs: float


def _running_servers() -> list[WebSocketServer]:
    """(pid, WebSocket port, age in seconds) for each standalone app-server."""
    servers: list[WebSocketServer] = []
    for line in _list_server_processes().splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            command = shlex.split(fields[2])
        except ValueError:
            continue
        if not command or Path(command[0]).name != "codex":
            continue
        server_index = next((index for index in range(1, len(command) - 2)
                             if tuple(command[index:index + 2]) == SERVER_ARGUMENTS), -1)
        if server_index < 0:
            continue
        address = command[server_index + 2]
        if not address.startswith("ws://"):
            continue
        port = address.rpartition(":")[2]
        if port.isdigit() and fields[0].isdigit():
            try:
                if _application_codex(str(_process_executable(int(fields[0])))):
                    continue
            except OSError:
                # Retain the candidate: _judge reports why it cannot be stopped.
                pass
            if sys.platform == "darwin":
                elapsed = _elapsed_secs(fields[1])
                if elapsed is None:
                    raise OSError("ps -eo pid=,etime=,args=: unreadable elapsed time")
                age = elapsed
            elif fields[1].isdigit():
                age = float(fields[1])
            else:
                continue
            servers.append(WebSocketServer(int(fields[0]), int(port), age))
    return servers


def _client_counts() -> dict[int, int]:
    """Established connections per local port. Never substitute zero for a failed command."""
    counts: dict[int, int] = {}
    for line in _list_established_connections().splitlines():
        fields = line.split()
        if sys.platform == "darwin":
            if fields[:1] == ["COMMAND"]:
                continue
            address = next((part for part in fields if "->" in part), "")
            if not address:
                raise OSError("lsof -nP -iTCP -sTCP:ESTABLISHED: unreadable connection")
            port = address.partition("->")[0].rpartition(":")[2]
        else:
            port = fields[2].rpartition(":")[2] if len(fields) >= 4 else ""
        if port.isdigit():
            counts[int(port)] = counts.get(int(port), 0) + 1
        elif sys.platform == "darwin":
            raise OSError("lsof -nP -iTCP -sTCP:ESTABLISHED: unreadable local port")
    return counts


@dataclass(frozen=True)
class MeshServerLog:
    folder: Path


@dataclass(frozen=True)
class NonMeshServerStderr:
    """The stderr handle was read and does not name a mesh log."""


@dataclass(frozen=True)
class UnreadServerStderr:
    reason: str


ServerStderr = MeshServerLog | NonMeshServerStderr | UnreadServerStderr


def _server_folder(pid: int) -> ServerStderr:
    """Read stderr without erasing the distinction between no mesh log and no evidence."""
    try:
        if sys.platform == "darwin":
            output = _list_server_stderr(pid)
            targets = [line[1:] for line in output.splitlines() if line.startswith("n")]
            if not targets:
                if not _server_owned_by_user(pid):
                    return UnreadServerStderr("lsof: stderr unavailable; process ownership could not be confirmed")
                return NonMeshServerStderr()
            target = targets[0]
        else:
            target = os.readlink(f"/proc/{pid}/fd/2")
    except OSError as error:
        command = "lsof" if sys.platform == "darwin" else f"/proc/{pid}/fd/2"
        return UnreadServerStderr(f"{command}: stderr log could not be read ({error})")
    log = Path(target.removesuffix(" (deleted)"))
    return MeshServerLog(log.parent) if log.name == SERVER_LOG else NonMeshServerStderr()


def _application_codex(executable: str) -> bool:
    return any(part.endswith(".app") and following == "Contents"
               for part, following in zip(Path(executable).parts, Path(executable).parts[1:]))


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started: str
    arguments: tuple[str, ...]
    executable: Path


@dataclass(frozen=True)
class CodexDaemon(ProcessIdentity):
    """An executable-backed identity whose argv selects app-server daemon."""


@dataclass(frozen=True)
class UnclassifiedCodexProcess(ProcessIdentity):
    """A readable process identity that is not an app-server daemon."""


@dataclass(frozen=True)
class UnreadCodexProcess:
    pid: int
    reason: str


DaemonTarget = CodexDaemon | UnclassifiedCodexProcess | UnreadCodexProcess


@dataclass(frozen=True)
class ProcessSession:
    sid: int


@dataclass(frozen=True)
class SessionUnread:
    reason: str


@dataclass(frozen=True)
class SessionGone:
    """The listed process exited before its session could be read."""


@dataclass(frozen=True)
class FamilyProcess:
    parent: int
    identity: ProcessIdentity | UnreadCodexProcess
    session: ProcessSession | SessionGone | SessionUnread


def _process_session(pid: int) -> ProcessSession | SessionGone | SessionUnread:
    """Read a session id for a candidate from the own-user process listing."""
    try:
        return ProcessSession(os.getsid(pid))
    except ProcessLookupError:
        return SessionGone()
    except OSError:
        return SessionUnread("process session could not be read")


@contextlib.contextmanager
def _daemon_stop_lock() -> Generator[None, None, None]:
    path = Path.home() / ".local/state/codex-mesh/daemon-stop.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as handle:
        worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_UN)


def _list_codex_processes() -> str:
    name = "ucomm=" if sys.platform == "darwin" else "comm="
    return _cleanup_command(("ps", "-u", str(os.getuid()), "-o", f"pid=,{name}"))


def _process_executable(pid: int) -> Path:
    """Read the executable path independently of the caller-controlled argv[0]."""
    if sys.platform == "darwin":
        record = account.mac_process_record(pid)
        if isinstance(record, account.ProcessEnvironmentUnread):
            raise OSError("kernel executable path could not be read")
        return record.executable
    return Path(os.readlink(f"/proc/{pid}/exe"))


def _process_identity(pid: int) -> ProcessIdentity | UnreadCodexProcess:
    before = sessions.process_start(pid)
    try:
        if sys.platform == "darwin":
            record = account.mac_process_record(pid)
            if isinstance(record, account.ProcessEnvironmentUnread):
                return UnreadCodexProcess(pid, "process argument record could not be read")
            arguments, executable = record.arguments, record.executable
        else:
            arguments = tuple(os.fsdecode(part) for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part)
            executable = _process_executable(pid)
    except OSError:
        return UnreadCodexProcess(pid, "process arguments or executable path could not be read")
    after = sessions.process_start(pid)
    if not before or before != after or not arguments or not executable.is_absolute():
        return UnreadCodexProcess(pid, "process start, arguments or executable path could not be read consistently")
    return ProcessIdentity(pid, before, arguments, executable)


def _is_standalone_daemon(target: ProcessIdentity) -> bool:
    arguments = target.arguments
    if target.executable.name != "codex" or _application_codex(str(target.executable)):
        return False
    index = 1
    while index < len(arguments):
        option = arguments[index]
        if option in ("-c", "--config", "--enable", "--disable"):
            index += 2
        elif option.startswith(("--config=", "--enable=", "--disable=")):
            index += 1
        else:
            break
    return arguments[index:index + 2] == ("app-server", "daemon")


def _daemon_record(pid: int) -> DaemonTarget:
    identity = _process_identity(pid)
    if isinstance(identity, UnreadCodexProcess):
        return identity
    kind = CodexDaemon if _is_standalone_daemon(identity) else UnclassifiedCodexProcess
    return kind(identity.pid, identity.started, identity.arguments, identity.executable)


def _running_daemons() -> list[DaemonTarget]:
    targets: list[DaemonTarget] = []
    for line in _list_codex_processes().splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit() or fields[1].strip() != "codex":
            continue
        target = _daemon_record(int(fields[0]))
        if isinstance(target, (UnreadCodexProcess, CodexDaemon)):
            targets.append(target)
        elif _daemon_process_state(target.pid) is ProcessRunState.STOPPED:
            targets.append(target)
    return targets


def _process_cleanup_verdict(target: ProcessIdentity) -> SweepVerdict:
    if _application_codex(str(target.executable)):
        return ServerInUse("executable belongs to an application bundle")
    setting = account.process_codex_home(target.pid)
    if isinstance(setting, account.ProcessEnvironmentUnread):
        return ServerInUse("process environment could not be read")
    home = Path.home() / ".codex"
    if isinstance(setting, account.ProcessCodexHome) and setting.home != home:
        return ServerInUse("daemon uses another Codex home")
    try:
        _ = (home / "auth.json").lstat()
    except FileNotFoundError:
        return ServerUnused("~/.codex/auth.json is absent; shared home has no login")
    except OSError:
        return ServerInUse("cannot establish whether ~/.codex/auth.json is absent")
    return ServerInUse("~/.codex/auth.json exists; shared home may hold a login")


def _daemon_verdict(target: DaemonTarget) -> SweepVerdict:
    if isinstance(target, UnreadCodexProcess):
        return ServerInUse(target.reason)
    if not isinstance(target, CodexDaemon) or not _is_standalone_daemon(target):
        return ServerInUse("not a standalone Codex app-server daemon")
    return _process_cleanup_verdict(target)


def _list_family_processes() -> str:
    return _cleanup_command(("ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,args="))


def _own_process_table() -> dict[int, process_table.Process]:
    table: dict[int, process_table.Process] = {}
    for line in _list_family_processes().splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise OSError("ps -u: unreadable process family listing")
        pid, parent = int(fields[0]), int(fields[1])
        # Only the last column is free text. A path containing spaces cannot
        # shift the ancestry columns or be mistaken for part of argv.
        table[pid] = process_table.Process(pid, parent, "", fields[2])
    return table


def _table_process_sessions(
    table: Mapping[int, process_table.Process],
) -> dict[int, ProcessSession | SessionGone | SessionUnread]:
    return {pid: _process_session(pid) for pid in table}


def _session_process_ids(
    process_sessions: Mapping[int, ProcessSession | SessionGone | SessionUnread],
    swept_sessions: frozenset[int],
) -> set[int]:
    members: set[int] = set()
    if not swept_sessions:
        return members
    for pid, session in process_sessions.items():
        if isinstance(session, SessionUnread):
            raise DaemonFreezeRefused(pid, session.reason)
        if isinstance(session, ProcessSession) and session.sid in swept_sessions:
            members.add(pid)
    return members


def _family_candidate_ids(
    table: Mapping[int, process_table.Process], parent: int,
    tracked: Mapping[int, ProcessIdentity], swept_sessions: frozenset[int],
    process_sessions: Mapping[int, ProcessSession | SessionGone | SessionUnread],
) -> set[int]:
    members = {parent, *tracked} & table.keys()
    members.update(_session_process_ids(process_sessions, swept_sessions))
    pending = list(members)
    while pending:
        current = pending.pop()
        for pid, process in table.items():
            if process.parent == current and pid not in members:
                members.add(pid)
                pending.append(pid)
    return {pid for pid in members if not isinstance(process_sessions[pid], SessionGone)}


def _daemon_process_tree(
    parent: int, tracked: Mapping[int, ProcessIdentity], swept_sessions: frozenset[int],
) -> dict[int, FamilyProcess]:
    """Read candidate identities against consistent own-user ancestry snapshots.

    Each table's session results are read once and retained for its consumers.
    """
    before = _own_process_table()
    before_sessions = _table_process_sessions(before)
    identities = {pid: _process_identity(pid) for pid in _family_candidate_ids(
        before, parent, tracked, swept_sessions, before_sessions)}
    table = _own_process_table()
    process_sessions = _table_process_sessions(table)
    result: dict[int, FamilyProcess] = {}
    for pid in _family_candidate_ids(table, parent, tracked, swept_sessions, process_sessions):
        process = table[pid]
        identity = identities.get(pid, UnreadCodexProcess(pid, "process appeared during family snapshot"))
        if before.get(pid) != process or _process_identity(pid) != identity:
            identity = UnreadCodexProcess(pid, "process changed during family snapshot")
        result[pid] = FamilyProcess(process.parent, identity, process_sessions[pid])
    return result


def _family_members(table: dict[int, FamilyProcess], parent: int) -> list[FamilyProcess]:
    pending, seen = [parent], {parent}
    members: list[FamilyProcess] = []
    while pending:
        current = pending.pop(0)
        for pid, process in table.items():
            if process.parent == current and pid not in seen:
                seen.add(pid)
                members.append(process)
                pending.append(pid)
    return members


def _identity_matches(target: ProcessIdentity) -> bool:
    current = _process_identity(target.pid)
    return (isinstance(current, ProcessIdentity)
            and (current.pid, current.started, current.arguments, current.executable)
            == (target.pid, target.started, target.arguments, target.executable))


class ProcessRunState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    GONE = "gone"


class DaemonFreezeRefused(Exception):
    def __init__(self, pid: int, reason: str) -> None:
        super().__init__(reason)
        self.pid: int = pid
        self.reason: str = reason


def _daemon_process_state(pid: int) -> ProcessRunState:
    """Read kernel scheduling state; zombies no longer execute or fork."""
    if sys.platform == "darwin":
        state = _cleanup_command(("ps", "-p", str(pid), "-o", "stat="), empty_selection=True).strip()
        if not state:
            return ProcessRunState.GONE
    else:
        try:
            record = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return ProcessRunState.GONE
        closing = record.rfind(")")
        fields = record[closing + 1:].split()
        if closing < 0 or not fields:
            raise OSError("process scheduling state could not be read")
        state = fields[0]
    if state[0] in ("Z", "X"):
        return ProcessRunState.GONE
    if state[0] == "T":
        return ProcessRunState.STOPPED
    if state[0] not in "RSDIUtWPN":
        raise OSError("process scheduling state could not be read")
    return ProcessRunState.RUNNING


def _signal_daemon_process(target: ProcessIdentity, signum: int) -> bool:
    """Never signal a pid without rechecking its captured identity."""
    try:
        if not _identity_matches(target):
            return False
        _signal_process(target.pid, signum)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


@dataclass(frozen=True)
class FrozenDaemonMember:
    identity: ProcessIdentity
    stopped_by_cleanup: bool


def _freeze_daemon_process(target: ProcessIdentity, frozen: list[FrozenDaemonMember]) -> None:
    unconfirmed = "member exited before the family was frozen; family absence is unconfirmed"
    initial_state = _daemon_process_state(target.pid)
    if initial_state is ProcessRunState.GONE:
        raise DaemonFreezeRefused(target.pid, unconfirmed)
    if initial_state is ProcessRunState.STOPPED:
        if not _identity_matches(target):
            raise DaemonFreezeRefused(target.pid, "member identity changed while freezing")
        frozen.append(FrozenDaemonMember(target, stopped_by_cleanup=False))
        return
    if not _signal_daemon_process(target, signal.SIGSTOP):
        reason = unconfirmed if _daemon_process_state(target.pid) is ProcessRunState.GONE else "member identity changed or SIGSTOP failed"
        raise DaemonFreezeRefused(target.pid, reason)
    # Record our STOP before any further reads, so even a failed read rolls back.
    frozen.append(FrozenDaemonMember(target, stopped_by_cleanup=True))
    deadline = time.monotonic() + REAP_WAIT_SECS
    while True:
        state = _daemon_process_state(target.pid)
        if state is ProcessRunState.GONE:
            raise DaemonFreezeRefused(target.pid, unconfirmed)
        if not _identity_matches(target):
            raise DaemonFreezeRefused(target.pid, "member identity changed while freezing")
        if state is ProcessRunState.STOPPED:
            return
        if time.monotonic() >= deadline:
            raise DaemonFreezeRefused(target.pid, "SIGSTOP was not confirmed")
        time.sleep(min(REAP_POLL_SECS, max(0, deadline - time.monotonic())))


def _resume_daemon_family(frozen: list[FrozenDaemonMember]) -> None:
    for member in reversed(frozen):
        if not member.stopped_by_cleanup:
            continue
        identity = member.identity
        try:
            if _daemon_process_state(identity.pid) is ProcessRunState.GONE:
                continue
        except (OSError, ValueError, subprocess.SubprocessError):
            # Scheduling state is not needed to identify a surviving process.
            pass
        try:
            if sessions.process_start(identity.pid) != identity.started:
                _ = _family_kept(identity.pid, "process start could not be confirmed; may still be stopped")
                continue
            # A stopped process retains its pid. Its start time is enough for
            # rollback even when its argv or executable can no longer be read.
            _signal_process(identity.pid, signal.SIGCONT)
        except (OSError, ValueError, subprocess.SubprocessError):
            _ = _family_kept(identity.pid, "could not resume family member; may still be stopped")


def _daemon_identity_gone(target: ProcessIdentity) -> bool:
    try:
        if _daemon_process_state(target.pid) is ProcessRunState.GONE:
            return True
        current = _process_identity(target.pid)
        return isinstance(current, ProcessIdentity) and (
            current.started, current.arguments, current.executable
        ) != (target.started, target.arguments, target.executable)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _family_kept(pid: int, reason: str) -> bool:
    print(f"codex_mesh: daemon family not stopped: pid {pid}: {reason}", file=sys.stderr)
    return False


@dataclass(frozen=True)
class ProcessTerminal:
    name: str


@dataclass(frozen=True)
class TerminalUnread:
    reason: str


def _process_terminal(pid: int) -> ProcessTerminal | TerminalUnread:
    try:
        terminal = _cleanup_command(("ps", "-p", str(pid), "-o", "tty="), empty_selection=True).strip()
    except (OSError, subprocess.SubprocessError):
        return TerminalUnread("controlling terminal could not be read")
    if not terminal:
        return TerminalUnread("controlling terminal could not be read")
    return ProcessTerminal(terminal)


def _recover_stopped_codex_members(
    members: Sequence[ProcessIdentity | UnreadCodexProcess], *, resume: bool = True,
) -> None:
    """Report prior stops; recover verified detached default-home members only."""
    for member in members:
        try:
            if _daemon_process_state(member.pid) is not ProcessRunState.STOPPED:
                continue
            reason = ""
            if isinstance(member, UnreadCodexProcess):
                reason = member.reason
            elif _application_codex(str(member.executable)):
                reason = "executable belongs to an application bundle"
            elif not _identity_matches(member):
                reason = "member identity could not be confirmed"
            else:
                setting = account.process_codex_home(member.pid)
                if isinstance(setting, account.ProcessEnvironmentUnread):
                    reason = "process environment could not be read"
                elif isinstance(setting, account.ProcessCodexHome) and setting.home != Path.home() / ".codex":
                    reason = "member uses another Codex home"
                else:
                    terminal = _process_terminal(member.pid)
                    if isinstance(terminal, TerminalUnread):
                        reason = terminal.reason
                    elif terminal.name not in ("?", "??"):
                        reason = f"member has controlling terminal {terminal.name}"
            if reason:
                _ = _family_kept(member.pid, f"found stopped; kept: {reason}")
            elif not resume:
                _ = _family_kept(member.pid, "found stopped; report only, member kept")
            elif isinstance(member, ProcessIdentity):
                # Recovery of a prior stop needs a full identity match, unlike
                # rollback of a STOP issued by this invocation.
                if sessions.process_start(member.pid) != member.started or not _signal_daemon_process(member, signal.SIGCONT):
                    _ = _family_kept(member.pid, "found stopped; kept: could not resume member; may still be stopped")
                else:
                    print(f"codex_mesh: daemon family: pid {member.pid} found stopped; resuming kept member", file=sys.stderr)
        except (OSError, ValueError, subprocess.SubprocessError):
            _ = _family_kept(member.pid, "stopped member kept: could not be checked; may still be stopped")


def _recover_kept_daemon(target: ProcessIdentity, *, resume: bool = True) -> bool:
    def recover() -> bool:
        table = _daemon_process_tree(target.pid, {}, frozenset())
        root = table.get(target.pid)
        if root is None:
            return _family_kept(target.pid, "kept family identity could not be confirmed")
        members = [root, *_family_members(table, target.pid)]
        if (not isinstance(root.identity, ProcessIdentity)
                or (root.identity.pid, root.identity.started, root.identity.arguments, root.identity.executable)
                != (target.pid, target.started, target.arguments, target.executable)):
            _recover_stopped_codex_members([member.identity for member in members], resume=False)
            return _family_kept(target.pid, "kept family identity could not be confirmed")
        was_stopped = False
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            was_stopped = _daemon_process_state(target.pid) is ProcessRunState.STOPPED
        _recover_stopped_codex_members([member.identity for member in members], resume=resume)
        return (resume and was_stopped and _identity_matches(target)
                and _daemon_process_state(target.pid) is ProcessRunState.RUNNING)

    try:
        if not resume:
            return recover()
        with _daemon_stop_lock():
            return recover()
    except BlockingIOError:
        return _family_kept(target.pid, "another daemon cleanup holds the stop lock")
    except (OSError, ValueError, subprocess.SubprocessError):
        return _family_kept(target.pid, "stopped member recovery failed; may still be stopped")


def _daemon_home(target: DaemonTarget) -> str:
    if isinstance(target, UnreadCodexProcess):
        return "unread"
    setting = account.process_codex_home(target.pid)
    if isinstance(setting, account.ProcessEnvironmentUnread):
        return "unread"
    home = setting.home if isinstance(setting, account.ProcessCodexHome) else Path.home() / ".codex"
    return codex_home_display(home)


def _frozen_daemon_sessions(frozen: Sequence[FrozenDaemonMember]) -> frozenset[int]:
    leaders: set[int] = set()
    for member in frozen:
        session = _process_session(member.identity.pid)
        if isinstance(session, SessionUnread):
            raise DaemonFreezeRefused(member.identity.pid, session.reason)
        if isinstance(session, ProcessSession) and session.sid == member.identity.pid:
            leaders.add(session.sid)
    return frozenset(leaders)


def _stop_daemon(target: DaemonTarget) -> bool:
    """Serialize family cleanup, including rollback and recovery of earlier stops."""
    if not isinstance(target, CodexDaemon):
        return False
    try:
        with _daemon_stop_lock():
            return _stop_daemon_locked(target)
    except BlockingIOError:
        return _family_kept(target.pid, "another daemon cleanup holds the stop lock")
    except OSError:
        return _family_kept(target.pid, "daemon stop lock could not be acquired")


def _stop_daemon_locked(target: CodexDaemon) -> bool:
    """Freeze ancestry and sessions led by frozen members before killing them.

    An unseen descendant that calls setsid leaves those sessions and is out of
    reach. Mac daemon session leadership has not been checked live. Never sweep
    a session led by a process outside the tracked family.
    """
    frozen: list[FrozenDaemonMember] = []
    tracked: dict[int, FamilyProcess] = {}
    succeeded = False
    try:
        measured = time.monotonic()
        table = _daemon_process_tree(target.pid, {}, frozenset())
        listing_seconds = time.monotonic() - measured
        root = table.get(target.pid)
        if root is None or isinstance(root.identity, UnreadCodexProcess) or not _identity_matches(target):
            return _family_kept(target.pid, "parent identity could not be confirmed")
        if (root.identity.started, root.identity.arguments, root.identity.executable) != (
                target.started, target.arguments, target.executable):
            return _family_kept(target.pid, "parent identity changed")
        # Remember every seen member before stopping the parent. Reparenting
        # never removes a member from this identity ledger.
        tracked = {target.pid: root}
        for member in _family_members(table, target.pid):
            tracked[member.identity.pid] = member
        # Budget from the measured candidate listing on this machine, including
        # Darwin's kernel/start-time reads, rather than a fixed five seconds.
        started = time.monotonic()
        deadline = started + max(REAP_WAIT_SECS, listing_seconds * 4)
        swept_sessions: frozenset[int] = frozenset()
        while True:
            for member in list(tracked.values())[len(frozen):]:
                identity = member.identity
                if isinstance(identity, UnreadCodexProcess):
                    raise DaemonFreezeRefused(identity.pid, identity.reason)
                # An excluded process is never signalled, not even by STOP.
                verdict = _process_cleanup_verdict(identity)
                if isinstance(verdict, ServerInUse):
                    raise DaemonFreezeRefused(identity.pid, verdict.reason)
                _freeze_daemon_process(identity, frozen)
            swept_sessions = swept_sessions | _frozen_daemon_sessions(frozen)
            identities = {member.identity.pid: member.identity for member in frozen}
            measured = time.monotonic()
            table = _daemon_process_tree(target.pid, identities, swept_sessions)
            listing_seconds = max(listing_seconds, time.monotonic() - measured)
            deadline = max(deadline, started + max(REAP_WAIT_SECS, listing_seconds * 4))
            added = False
            for pid in list(tracked):
                for member in _family_members(table, pid):
                    if member.identity.pid not in tracked:
                        tracked[member.identity.pid] = member
                        added = True
            # Session membership reaches an orphan even after its intermediate
            # parent has exited. The user-filtered table bounds all session reads.
            for pid, member in table.items():
                if pid in tracked:
                    continue
                session = member.session
                if isinstance(session, SessionUnread):
                    raise DaemonFreezeRefused(pid, session.reason)
                if isinstance(session, ProcessSession) and session.sid in swept_sessions:
                    tracked[pid] = member
                    added = True
            for frozen_member in frozen:
                member = frozen_member.identity
                if _daemon_process_state(member.pid) is not ProcessRunState.STOPPED:
                    raise DaemonFreezeRefused(member.pid, "member exited before the family was frozen; family absence is unconfirmed")
                if not _identity_matches(member):
                    raise DaemonFreezeRefused(member.pid, "member identity changed while freezing")
            if not added:
                break
            if time.monotonic() >= deadline:
                raise DaemonFreezeRefused(target.pid, "family could not be frozen before the deadline")
        # A frozen member cannot fork or exit. Validate every exclusion only
        # after the family reaches this fixed point, before sending any KILL.
        for frozen_member in frozen:
            member = frozen_member.identity
            verdict = _process_cleanup_verdict(member)
            if isinstance(verdict, ServerInUse):
                raise DaemonFreezeRefused(member.pid, verdict.reason)
        for frozen_member in reversed(frozen):
            member = frozen_member.identity
            if not isinstance(_daemon_verdict(target), ServerUnused) or not _identity_matches(target):
                raise DaemonFreezeRefused(target.pid, "parent identity or login changed")
            if _daemon_process_state(member.pid) is not ProcessRunState.STOPPED:
                raise DaemonFreezeRefused(member.pid, "member is no longer frozen")
            verdict = _process_cleanup_verdict(member)
            if isinstance(verdict, ServerInUse):
                raise DaemonFreezeRefused(member.pid, verdict.reason)
            if not _signal_daemon_process(member, signal.SIGKILL):
                raise DaemonFreezeRefused(member.pid, "frozen member identity changed or SIGKILL failed")
        deadline = time.monotonic() + REAP_WAIT_SECS
        while True:
            remaining = [member.identity for member in frozen if not _daemon_identity_gone(member.identity)]
            if not remaining:
                # A new unrelated ps pid is not family evidence. Only surviving
                # members of sessions whose leaders we froze can prevent success.
                for pid in sorted(_session_process_ids(_table_process_sessions(_own_process_table()), swept_sessions)):
                    if _daemon_process_state(pid) is not ProcessRunState.GONE:
                        return _family_kept(pid, "process remains in a swept session after shutdown")
                succeeded = True
                return True
            if time.monotonic() >= deadline:
                return _family_kept(remaining[0].pid, "family member remains after shutdown")
            time.sleep(min(REAP_POLL_SECS, max(0, deadline - time.monotonic())))
    except DaemonFreezeRefused as error:
        return _family_kept(error.pid, error.reason)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _family_kept(target.pid, "process family could not be read")
    finally:
        _resume_daemon_family(frozen)
        if not succeeded:
            frozen_ids = {member.identity.pid for member in frozen if member.stopped_by_cleanup}
            _recover_stopped_codex_members([
                member.identity for pid, member in tracked.items()
                if pid not in frozen_ids
            ])


def _record_names_server(record: dict[str, object], pid: int, port: int) -> bool:
    return record.get("pid") == pid and record.get("port") == port


def _server_ownership(
    roots: tuple[Path, ...] | None, pid: int, port: int, stderr: MeshServerLog | NonMeshServerStderr
) -> KnownServerOwnership:
    """Resolve the record that owns a server; a live current owner wins stale duplicates."""
    if roots is None:
        folders = (stderr.folder,) if isinstance(stderr, MeshServerLog) else ()
    else:
        current_folders = {path.parent for root in roots for path in root.glob(f"*/{SERVER_FILE}")}
        retired_folders = {path.parent for root in roots for path in root.glob(f"*/{SERVER_FILE}")}
        starting_folders = {path.parent for root in roots for path in root.glob(f"*/{SERVER_START_FILE}")}
        folders = tuple(sorted(current_folders | retired_folders | starting_folders, key=str))
    current_owners = tuple(
        folder
        for folder in folders
        if _record_names_server(_read_json_object(folder / SERVER_FILE), pid, port)
    )
    if roots is not None:
        for folder in current_owners:
            quiet = _quiet_secs(folder, time.time())
            if (
                _marked_active(roots, folder)
                or _running_roster_workers(folder)
                or _live_launchers(folder)
                or (quiet is not None and quiet < SERVER_IDLE_SECS)
            ):
                return CurrentRunServer(folder)
        for folder in _durable_meshes():
            if _record_names_server(_read_json_object(folder / SERVER_FILE), pid, port) and any(
                _as_dict(entry).get("held_design_id")
                for entry in _read_json_object(folder / ROSTER_FILE).values()
            ):
                return HeldDesignServer(folder)
    for folder in folders:
        if any(_record_names_server(record, pid, port)
               for record in _recorded_run_servers(str(folder), retained_only=True)):
            return RetiredRunServer(folder)
    if current_owners:
        return CurrentRunServer(current_owners[0])
    for folder in folders:
        pending = _read_json_object(folder / SERVER_START_FILE)
        if _record_names_server(pending, pid, port) and _as_str(pending.get("dev_shell_worktree")):
            return StartingDevShellServer(folder, _as_float(pending.get("started_at"), 0.0))
    if isinstance(stderr, MeshServerLog):
        return UnownedServerWithLog(stderr.folder)
    return UnownedServerWithoutLog()


def _marked_active(roots: tuple[Path, ...], folder: Path) -> bool:
    """Whether a run-active marker names `folder`. A marker that cannot be read counts as naming it."""
    actives = [root / "active" for root in roots]
    for marker in (found for active in actives if active.is_dir() for found in sorted(active.iterdir())):
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            return True
        if lines and lines[0].strip() and Path(lines[0].strip()).resolve() == folder.resolve():
            return True
    return False


def _quiet_secs(folder: Path, now: float) -> float | None:
    changed = [(folder / name).stat().st_mtime for name in SWEEP_ACTIVITY_FILES if (folder / name).exists()]
    return now - max(changed) if changed else None


def _live_launchers(folder: Path) -> tuple[str, ...]:
    roster = _read_json_object(folder / ROSTER_FILE)
    return tuple(
        name
        for name, entry in sorted(roster.items())
        for pid in [_as_dict(entry).get("launcher_pid")]
        if isinstance(pid, int) and _pid_alive(pid)
    )


def _running_roster_workers(folder: Path) -> tuple[str, ...]:
    """Workers whose roster state says a turn is in progress, whether or not its launcher is alive."""
    roster = _read_json_object(folder / ROSTER_FILE)
    return tuple(
        name
        for name, entry in sorted(roster.items())
        if _as_dict(entry).get("status") == "running"
    )


def _forget_run_server(folder: Path, pid: int, port: int) -> None:
    """Remove one stopped process without losing other server records."""
    path = folder / SERVER_FILE
    current = _read_json_object(path)
    records = [record for record in _recorded_run_servers(str(folder))
               if not _record_names_server(record, pid, port)]
    if current.get("pid") and not _record_names_server(current, pid, port):
        value = {key: item for key, item in current.items() if key != "previous_servers"}
        value["previous_servers"] = [record for record in records
                                     if not _record_names_server(record, _as_int(current.get("pid")), _as_int(current.get("port")))]
    else:
        value = {"previous_servers": records}
    _write_account_move_json(path, value)
    if not records:
        path.unlink(missing_ok=True)


def _roster_threads(folder: Path) -> dict[str, str]:
    """Thread id to worker name for every conversation the run folder's roster lists."""
    roster = _read_json_object(folder / ROSTER_FILE)
    membership = staff.local_snapshot()
    named = {session_staff.worker_session(_as_dict(entry), membership): name for name, entry in sorted(roster.items())}
    return {thread_id: name for thread_id, name in named.items() if thread_id}


def _busy_threads(port: int, workers: dict[str, str],
                  *, deadline: float = NO_LOCK_DEADLINE) -> tuple[str, ...]:
    """Conversations on this server that are not idle, or why they could not be read.

    The server lists what it has loaded, so a turn left running by a launcher killed at its time
    limit is seen with no client connected and no roster. `workers` adds the roster's conversations
    and names them."""
    try:
        client = Client(port, f"sweep-{os.getpid()}", operation_deadline=deadline)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as exc:
        return (f"the server could not be asked ({str(exc) or exc.__class__.__name__})",)
    try:
        listed = client.call("thread/loaded/list", {})
        result = _as_dict(listed.get("result"))
        loaded = result.get("data")
        if listed.get("error") or not isinstance(loaded, list) or result.get("nextCursor"):
            return ("its loaded conversations could not be listed in full",)
        thread_ids = {*workers, *(_as_str(thread_id) for thread_id in cast("list[object]", loaded))} - {""}
        busy: list[str] = []
        for thread_id in sorted(thread_ids):
            if thread_id in loaded:
                queued = client.call("thread/queue/list", {"threadId": thread_id})
                queue_data = _as_dict(queued.get("result")).get("data")
                if queued.get("error") or not isinstance(queue_data, list) or _as_dict(queued.get("result")).get("nextCursor"):
                    busy.append(f"{workers.get(thread_id, thread_id)}: queued work could not be read in full")
                elif queue_data:
                    busy.append(f"{workers.get(thread_id, thread_id)}: queued work")
            state = _read_live_turn(client, thread_id)
            if not isinstance(state, ThreadIdle):
                busy.append(f"{workers.get(thread_id, thread_id)}: {state.__class__.__name__}")
        return tuple(busy)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (ConnectionError, OSError, SystemExit) as exc:
        return (f"the server stopped answering ({str(exc) or exc.__class__.__name__})",)
    finally:
        client.close()


def _judge(roots: tuple[Path, ...] | None, pid: int, port: int, age: float,
           clients: int, *, deadline: float = NO_LOCK_DEADLINE) -> SweepJudgment:
    try:
        return _judge_readable(roots, pid, port, age, clients, deadline=deadline)
    except worker_lifecycle.WorkerStateUnreadable as error:
        reason = f"worker evidence unavailable ({error}); server kept"
        return SweepJudgment(ServerInUse(reason), UnreadServerOwnership(reason))


def _judge_readable(roots: tuple[Path, ...] | None, pid: int, port: int, age: float,
           clients: int, *, deadline: float = NO_LOCK_DEADLINE) -> SweepJudgment:
    """Judge one running app-server without erasing its ownership state.

    `roots` hold the run-active markers. A server judging itself passes None: a stopped server
    comes back at the next use, so a run that is only between dispatches does not need it kept.
    """
    try:
        executable = _process_executable(pid)
        if _application_codex(str(executable)):
            reason = "executable belongs to an application bundle"
            return SweepJudgment(ServerInUse(reason), UnreadServerOwnership(reason))
    except OSError:
        reason = "process executable path could not be read"
        return SweepJudgment(ServerInUse(reason), UnreadServerOwnership(reason))
    stderr = _server_folder(pid)
    if isinstance(stderr, UnreadServerStderr):
        return SweepJudgment(ServerInUse(stderr.reason), UnreadServerOwnership(stderr.reason))
    ownership = _server_ownership(roots, pid, port, stderr)
    folder = None if isinstance(ownership, UnownedServerWithoutLog) else ownership.session_dir
    exists = folder is not None and folder.is_dir()
    lifecycle: RunLifecycle = UncheckedRunLifecycle()
    if roots is not None and isinstance(ownership, CurrentRunServer):
        lifecycle = CheckedRunLifecycle(
            _marked_active(roots, ownership.session_dir),
            _running_roster_workers(ownership.session_dir) if exists else (),
        )
    facts = ServerFacts(
        pid, port, age, clients, ownership, exists, lifecycle,
        live_launchers=_live_launchers(folder) if folder is not None and exists else (),
        quiet_secs=_quiet_secs(folder, time.time()) if folder is not None and exists else None,
    )
    workers = _roster_threads(folder) if folder is not None and exists else {}
    return SweepJudgment(
        _sweep_verdict(facts, lambda: _busy_threads(port, workers, deadline=deadline)), ownership
    )


@worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS)
def _stop_unused(
    roots: tuple[Path, ...] | None, pid: int, port: int, ownership: ServerOwnership
) -> bool:
    """Stop a server a first look called unused, if a second look at this moment agrees.

    The second look runs under the run folder's server lock, so a launcher reaching
    `ensure_server` meanwhile waits and then starts a server of its own. A deleted folder has no
    launcher to wait and is not created again for the lock."""
    try:
        deadline = worker_lifecycle.effective_deadline()
        if isinstance(ownership, UnownedServerWithoutLog):
            return _stop_unused_with_lock_held(roots, pid, port, deadline=deadline)
        if not isinstance(ownership, (CurrentRunServer, HeldDesignServer, RetiredRunServer,
                                      StartingDevShellServer, UnownedServerWithLog)):
            return False
        folder = ownership.session_dir
        if not folder.is_dir():
            return _stop_unused_with_lock_held(roots, pid, port, deadline=deadline)
        with worker_lifecycle.membership_operation(folder, deadline=deadline), _server_lock(str(folder), deadline=deadline):
            return _stop_unused_with_lock_held(roots, pid, port, deadline=deadline)
    except (worker_lifecycle.OperationDeadlineExpired, worker_lifecycle.WorkerStateUnreadable) as error:
        print(f"codex_mesh: shutdown incomplete: {error}; app-server {pid} kept", file=sys.stderr)
        return False


def _stop_unused_with_lock_held(roots: tuple[Path, ...] | None, pid: int, port: int,
                                *, deadline: float = NO_LOCK_DEADLINE) -> bool:
    """Repeat an unused-server judgment, then stop and forget that exact owner."""
    try:
        running = {(found, at): age for found, at, age in _running_servers()}
        clients = _client_counts()
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"codex_mesh: cleanup: cannot judge app-server {pid} ({error}); kept", file=sys.stderr)
        return False
    # A pid that no longer runs this server on this port is not the process that was judged.
    age = running.get((pid, port))
    if age is None:
        return False
    judgment = _judge(roots, pid, port, age, clients.get(port, 0), deadline=deadline)
    if time.monotonic() >= deadline:
        print(f"codex_mesh: shutdown incomplete: idle check deadline reached; app-server {pid} kept", file=sys.stderr)
        return False
    if not isinstance(judgment.verdict, ServerUnused):
        return False
    if not _reap(pid, deadline=deadline):
        if _pid_alive(pid):
            print(f"codex_mesh: shutdown incomplete: app-server {pid} remains alive; records kept", file=sys.stderr)
        return False
    if isinstance(judgment.ownership, UnownedServerWithoutLog):
        return True
    ownership = judgment.ownership
    if not isinstance(ownership, (CurrentRunServer, HeldDesignServer, RetiredRunServer,
                                  StartingDevShellServer, UnownedServerWithLog)):
        return False
    folder = ownership.session_dir
    if folder.is_dir():
        _forget_run_server(folder, pid, port)
        pending = folder / SERVER_START_FILE
        if _record_names_server(_read_json_object(pending), pid, port):
            pending.unlink(missing_ok=True)
    return True


def command_idle_stop(args: argparse.Namespace) -> int:
    """Stop this run folder's app-server if nothing uses it. Its watcher runs this.

    Exit 0 tells the watcher to leave: the server was stopped here, or no longer runs. Exit 1
    keeps it watching. A server that is kept has its record touched, so the watcher sleeps through
    the next SERVER_IDLE_SECS before it asks again. Any doubt keeps the server, as in the sweep.
    """
    folder = Path(_as_str(_attr(args, "session_dir")))
    pid, port = _as_int(_attr(args, "pid")), _as_int(_attr(args, "port"))
    record = folder / SERVER_FILE
    try:
        running = {(found, at): age for found, at, age in _running_servers()}
        clients = _client_counts()
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"codex_mesh: idle-stop: cannot judge app-server {pid} ({error}); kept", file=sys.stderr)
        running, clients = None, {}
    if running is not None:
        age = running.get((pid, port))
        if age is None:
            return 0
        judgment = _judge(None, pid, port, age, clients.get(port, 0))
        if isinstance(judgment.verdict, ServerInUse):
            print(f"codex_mesh: idle-stop: kept app-server {pid}: {judgment.verdict.reason}")
        if isinstance(judgment.verdict, ServerUnused) and _stop_unused(
            None, pid, port, judgment.ownership
        ):
            _log_server(
                str(folder),
                f"app-server {pid} stopped itself: {judgment.verdict.reason}",
            )
            return 0
    if _read_json_object(record).get("pid") == pid:
        with contextlib.suppress(OSError):
            os.utime(record)
    return 1


def _sweep_roots(args: argparse.Namespace) -> tuple[Path, ...]:
    """The roots `--root` names: every real root when it names one of them, since a run under the
    other root keeps its server too."""
    given = _as_str(_attr(args, "root"))
    if not given:
        return SWEEP_ROOTS
    real = {root.resolve() for root in SWEEP_ROOTS}
    return SWEEP_ROOTS if Path(given).resolve() in real else (Path(given),)


def command_sweep(args: argparse.Namespace) -> int:
    """Print every running app-server with whether anything still needs it. Stops one only
    under `--stop`, and then only a server two looks in a row called unused."""
    roots = _sweep_roots(args)
    stop = _attr(args, "stop") is True
    try:
        servers, clients, daemons = _running_servers(), _client_counts(), _running_daemons()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"codex_mesh: sweep: cannot list servers or connections ({exc}); nothing judged", file=sys.stderr)
        return 1
    unused = stopped = 0
    for pid, port, age in sorted(servers):
        judgment = _judge(roots, pid, port, age, clients.get(port, 0))
        label = "unused" if isinstance(judgment.verdict, ServerUnused) else "in use"
        reason = judgment.verdict.reason
        if isinstance(judgment.verdict, ServerUnused):
            unused += 1
            if stop and _stop_unused(roots, pid, port, judgment.ownership):
                stopped += 1
                label = "stopped"
            elif stop:
                label, reason = "kept", "unused at the first look, not stopped at the second"
        owner = (
            "-"
            if isinstance(judgment.ownership, (UnownedServerWithoutLog, UnreadServerOwnership))
            else str(judgment.ownership.session_dir)
        )
        print(f"{pid}\t{port}\t{label}\t{reason}\t{owner}")
    for daemon in daemons:
        if not isinstance(daemon, CodexDaemon):
            reason = daemon.reason if isinstance(daemon, UnreadCodexProcess) else "not an app-server daemon"
            label, kind = "kept", "unclassified"
            if isinstance(daemon, ProcessIdentity):
                kind, reason = "stopped member", "Codex-family process found stopped"
                if _recover_kept_daemon(daemon, resume=stop):
                    label = "resumed"
            print(f"{daemon.pid}\t{kind}\t{label}\t{reason}\t{_daemon_home(daemon)}")
            continue
        home = _daemon_home(daemon)
        verdict = _daemon_verdict(daemon)
        label = "unused" if isinstance(verdict, ServerUnused) else "in use"
        reason = verdict.reason
        if isinstance(verdict, ServerUnused):
            unused += 1
            if stop and _stop_daemon(daemon):
                stopped += 1
                label = "stopped"
            elif stop:
                label, reason = "kept", "unused at the first look, not stopped at the second"
        if label != "stopped":
            if _daemon_process_state(daemon.pid) is ProcessRunState.STOPPED:
                reason += "; found stopped"
            if _recover_kept_daemon(daemon, resume=stop):
                label = "resumed"
        print(f"{daemon.pid}\tdaemon\t{label}\t{reason}\t{home}")
    tail = f"{stopped} stopped" if stop else "report only, nothing was stopped"
    print(f"{len(servers) + sum(isinstance(target, CodexDaemon) for target in daemons)} app-server(s), {unused} unused; {tail}")
    return 0


# Delivers the sign-in change notice to the session holding a run.
SEND_SCRIPT = Path(__file__).resolve().parent.parent / "message" / "send.py"


def _marker_sessions(roots: tuple[Path, ...], folder: Path) -> list[str]:
    """The sessions whose run-active marker names `folder`: the marker's file name is the session id."""
    actives = [root / "active" for root in roots]
    holders: list[str] = []
    for marker in (found for active in actives if active.is_dir() for found in sorted(active.iterdir())):
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if lines and lines[0].strip() and Path(lines[0].strip()).resolve() == folder.resolve():
            holders.append(marker.name)
    return holders


def _run_is_live(folder: Path, holders: tuple[str, ...]) -> bool:
    """A holder takes precedence over a surviving launcher from an ended session.

    Sessions are read per run, under its lock, so a holder that ends while earlier runs move is
    already ended here."""
    if holders:
        running = {record["sessionId"] for record in sessions.running_sessions()}
        return any(holder in running for holder in holders)
    return bool(_live_launchers(folder))


def _launcher_sessions(folder: Path) -> tuple[str, ...]:
    """Name live launchers by their nearest process-validated Claude session ancestor."""
    roster = _read_json_object(folder / ROSTER_FILE)
    pids = tuple(pid for entry in roster.values()
                 for pid in [_as_dict(entry).get("launcher_pid")]
                 if isinstance(pid, int) and _pid_alive(pid))
    if not pids:
        return ()
    try:
        table = process_table.snapshot()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()
    names: dict[int, str] = {}
    for record in sessions.running_sessions():
        # Only a recorded process start proves the pid still belongs to this session.
        started = record.get("procStart", "")
        if started and started == sessions.process_start(record["pid"]):
            _ = names.setdefault(record["pid"], record["sessionId"])
    found: list[str] = []
    for pid in pids:
        process = table.get(pid)
        if process is None:
            continue
        session_id = process_table.nearest_named(process, table, names)
        if session_id is not None and session_id not in found:
            found.append(session_id)
    return tuple(found)


def _notify_holder(session_id: str, line: str) -> None:
    try:
        sent = subprocess.run(
            [sys.executable, str(SEND_SCRIPT), "--to", f"session:{session_id}", "--from", "codex-sign-in",
             "--summary", "Codex sign-in changed", "--text", line],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=send_budget.seconds(), check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"codex_mesh: could not tell session {session_id}: {exc}", file=sys.stderr)
        return
    if sent.returncode:
        print(f"codex_mesh: could not tell session {session_id}: {sent.stderr.strip()}", file=sys.stderr)


def command_signin_changed(args: argparse.Namespace) -> int:
    """Move every run off an app-server started before the Codex sign-in last changed.

    A path watch on the sign-in file runs this the moment it changes; `ensure_server` makes the
    same check at each launch for a run this missed. Each run's record is retired, so its next
    dispatch starts a server that reads the current sign-in. Running workers are interrupted and
    resume immediately on that server. The old server is stopped now if
    nothing uses it, otherwise by its watcher once nothing does. Run twice for one change, the
    second run finds nothing older and moves nothing.
    """
    with worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS):
        roots = _sweep_roots(args)
        home = codex_home()
        changed = _sign_in_changed_at(home)
        if changed is None:
            print("codex_mesh: no Codex sign-in file; nothing moved")
            return 0
        at = datetime.fromtimestamp(changed).astimezone(user_zone()).strftime("%H:%M %Z")
        runs: list[WorkersRunMove | EndedWorkersRun] = []
        from scripts.astra import design_roles
        design_staff: staff.StaffSnapshot | None = None
        folders = sorted({path.parent for root in roots for path in root.glob(f"*/{SERVER_FILE}")}
                         | set(_durable_meshes()))
        for folder in folders:
            deadline = worker_lifecycle.effective_deadline()
            with _bulk_workers(str(folder), deadline=deadline, allow_held_roles=True) as operation, _server_lock(str(folder), deadline=deadline):
                current = _read_json_object(folder / SERVER_FILE)
                context = _design_context(str(folder))
                holder_sessions: list[str] = list(_marker_sessions(roots, folder))
                if context:
                    design = design_roles.read_design(_as_str(context.get("design_id")))
                    if design_staff is None:
                        design_staff = staff.snapshot()
                    for role in design.roles:
                        if isinstance(role.lifecycle, (design_roles.RoleNotOpened, design_roles.RoleEnded)):
                            continue
                        try:
                            parent = design_roles.worker_parent(design, role.role, design_staff)
                        except design_roles.RoleRefused as error:
                            print(f"codex_mesh: design {design.placement.design_id}: {role.role}: {error}",
                                  file=sys.stderr)
                            continue
                        holder_sessions.append(parent.current_session)
                holders = tuple(dict.fromkeys(holder_sessions))
                live = bool(context) or _run_is_live(folder, holders)
                outgoing: dict[int, int] = {}
                records = [current] if live else _recorded_run_servers(str(folder))
                for record in records:
                    port, pid = record.get("port"), record.get("pid")
                    recorded_home = server_record_home(record)
                    if (recorded_home == home
                            and isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid)
                            and _holds_old_sign_in(pid, recorded_home)):
                        outgoing[port] = pid
                if not outgoing:
                    continue
                current_port = current.get("port")
                if isinstance(current_port, int) and current_port in outgoing:
                    _retire_record(str(folder), current)
                if not live:
                    runs.append(EndedWorkersRun(folder, tuple(outgoing.items())))
                    continue
                workers = tuple(worker for port in outgoing for worker in _switch_run_workers(
                    folder, port, codex_home_role_label(workers_home()), operation=operation))
                runs.append(WorkersRunMove(folder, holders, tuple(outgoing.items()), workers))
    switches = _finish_run_moves(runs)
    for run, switched in zip(runs, switches, strict=True):
        if isinstance(run, EndedWorkersRun):
            continue
        folder = run.folder
        old = "is stopped" if switched.stopped_servers == len(run.servers) else "stops once nothing uses it"
        pid = run.servers[0][1]
        line = (f"Codex sign-in changed at {at}: run {folder.name}'s app-server {pid} started before it, so the "
                + "run's running workers were interrupted and resumed on a new server; "
                + f"idle workers move at their next turn; the old server {old}.")
        failures = [f"{worker.name}: {worker.failure}; resume with {worker.resume_command}"
                    for worker in switched.workers if worker.failure]
        if failures:
            line += " Worker move failures: " + "; ".join(failures)
        _log_server(str(folder), line)
        for session_id in switched.holders:
            _notify_holder(session_id, line)
        print(line)
    for line in leftover_server_lines(switches):
        print(line)
    print(f"{sum(isinstance(run, WorkersRunMove) for run in runs)} run(s) moved off the old sign-in")
    return 0


@dataclass(frozen=True)
class WorkerMoveReady:
    """An idle worker moves at its next turn."""


@dataclass(frozen=True)
class WorkerMoveResumed:
    """The interrupted worker confirmed a continuation on its new server."""


@dataclass(frozen=True)
class AccountMoveResumeRefused:
    reason: str


@dataclass(frozen=True)
class WorkerMoveResumeAvailable:
    reason: str
    command: str


@dataclass(frozen=True)
class WorkerMoveResumeRefused:
    reason: str
    refusal: AccountMoveResumeRefused


WorkerMoveFailed = WorkerMoveResumeAvailable | WorkerMoveResumeRefused
WorkerSwitchOutcome = WorkerMoveReady | WorkerMoveResumed | WorkerMoveFailed


@dataclass(frozen=True)
class SwitchingWorker:
    name: str
    mid_turn: bool
    outcome: WorkerSwitchOutcome = field(default_factory=WorkerMoveReady)

    @property
    def failure(self) -> str:
        return self.outcome.reason if isinstance(self.outcome, (WorkerMoveResumeAvailable, WorkerMoveResumeRefused)) else ""

    @property
    def resume_command(self) -> str:
        if isinstance(self.outcome, WorkerMoveResumeRefused):
            return f"resume refused: {self.outcome.refusal.reason}"
        return self.outcome.command if isinstance(self.outcome, WorkerMoveResumeAvailable) else ""


@dataclass(frozen=True)
class KeptWorkersServer:
    """An ended run's server that could not safely be stopped."""

    pid: int
    port: int
    reason: str


@dataclass(frozen=True)
class WorkersRunSwitch:
    folder: Path
    holders: tuple[str, ...]
    workers: tuple[SwitchingWorker, ...]
    leftover_servers: int = 0
    stopped_servers: int = 0
    kept_servers: tuple[KeptWorkersServer, ...] = ()


@dataclass(frozen=True)
class EndedWorkersRun:
    """Outgoing servers to clean up without touching an ended run's workers."""

    folder: Path
    servers: tuple[tuple[int, int], ...]  # port, pid


@dataclass(frozen=True)
class InterruptedWorkerMove:
    """An interrupted worker whose continuation has not yet been acknowledged."""

    session_dir: str
    name: str
    marker: AccountMoveMarker
    launcher_pid: int


@dataclass(frozen=True)
class WorkersRunMove:
    """A retired run with all interrupts dispatched, ready to await continuations."""

    folder: Path
    holders: tuple[str, ...]
    servers: tuple[tuple[int, int], ...]  # port, pid
    workers: tuple[SwitchingWorker | InterruptedWorkerMove, ...]


@dataclass(frozen=True)
class AccountMoveDispatch:
    role: WorkerRole
    lens: str | progress_history.DispatchDetailNotRecorded


def _account_move_dispatch(record: dict[str, object]) -> AccountMoveDispatch | AccountMoveResumeRefused:
    worker_id = record.get("staff_id")
    if not isinstance(worker_id, str) or not worker_id:
        return AccountMoveResumeRefused("worker has no staff id")
    dispatch = progress_history.last_dispatch(staff.StaffId(worker_id))
    if isinstance(dispatch, progress_history.NoDispatchRecorded):
        return AccountMoveResumeRefused("worker has no recorded dispatch")
    role = dispatch.role
    if role not in ("impl", "test", "fix", "review"):
        return AccountMoveResumeRefused(f"recorded role is not a worker role: {role}")
    return AccountMoveDispatch(role, dispatch.lens)


def _worker_move_failure(reason: str, session_dir: str, name: str) -> WorkerMoveFailed:
    record = _as_dict(session_staff.read_worker_roster(_session_path(session_dir, ROSTER_FILE)).get(name))
    dispatch = _account_move_dispatch(record)
    if isinstance(dispatch, AccountMoveResumeRefused):
        return WorkerMoveResumeRefused(reason, dispatch)
    options = [("--role", dispatch.role), ("--operation", "resume-account-move"),
               ("--session-dir", session_dir), ("--to", name)]
    return WorkerMoveResumeAvailable(reason, assign_gate.assign_command(session_dir, name, dispatch.role, options))


def command_resume_account_move(args: argparse.Namespace) -> int:
    session_dir, name = _as_str(_attr(args, "session_dir")), _as_str(_attr(args, "to"))
    record = _as_dict(session_staff.read_worker_roster(_session_path(session_dir, ROSTER_FILE)).get(name))
    dispatch = _account_move_dispatch(record)
    marker = _read_account_move(session_dir, name, _as_str(session_staff.worker_session(record)))
    if isinstance(dispatch, AccountMoveResumeRefused):
        if isinstance(marker, AccountMoveMarker):
            _fail_account_move(session_dir, name, marker, f"resume failed: {dispatch.reason}")
        else:
            print(f"codex_mesh: {name}: resume refused: {dispatch.reason}", file=sys.stderr)
        return 1
    if isinstance(marker, NoAccountMove):
        port = record.get("port")
        if not isinstance(port, int) or not _as_str(session_staff.worker_session(record)):
            print(f"codex_mesh: {name}: no recorded thread to resume", file=sys.stderr)
            return 1
        retried = _interrupt_worker_for_account_move(
            session_dir, name, record, port, codex_home_role_label(workers_home()))
        if isinstance(retried, InterruptedWorkerMove) and not _pid_alive(retried.launcher_pid):
            marker = retried.marker
        else:
            outcome = (_wait_account_move_resumes((retried,))[(session_dir, name)]
                       if isinstance(retried, InterruptedWorkerMove) else retried.outcome)
            if isinstance(outcome, (WorkerMoveResumeAvailable, WorkerMoveResumeRefused)):
                print(f"codex_mesh: {name}: {outcome.reason}", file=sys.stderr)
                return 1
            return 0
    with worker_lifecycle.membership_operation(session_dir), worker_lifecycle.lock_worker(session_dir, name):
        current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if current != record:
            print(f"codex_mesh: {name}: worker changed before account move claim", file=sys.stderr)
            return 2
        lens = cast(agents_log.Lens, dispatch.lens) if isinstance(dispatch.lens, str) else None
        launcher_pid = _as_int(record.get("launcher_pid"))
        if launcher_pid > 0 and _pid_alive(launcher_pid):
            _fail_account_move(session_dir, name, marker, "resume failed: existing launcher is still running")
            return 1
        summary = _session_path(session_dir, f"{name}.account-move-summary.txt")
        if record.get("held_design_id"):
            from scripts.astra import astra_work
            header = astra_work.read_header(Path(_as_str(record.get("instructions_file"))))
            if (not isinstance(header, astra_work.AstraWork) or header.member != name
                    or not Path(header.reply_path).is_absolute()):
                _fail_account_move(session_dir, name, marker, "resume failed: saved assignment header is invalid")
                return 1
            summary = Path(header.reply_path)
            summary.parent.mkdir(parents=True, exist_ok=True)
        claimed: StartingRecord = {
            "thread_id": marker.thread_id, "status": "starting", "launcher_pid": os.getpid(),
            "role": dispatch.role, "previous_task_role": dispatch.role, "previous_status": "failed",
            "previous_task_lens": lens,
        }
        if not _update_roster_if_expected(session_dir, name, claimed,
                                         RosterUnchanged(marker.thread_id, _roster_revision(record))):
            _fail_account_move(session_dir, name, marker, "resume failed: worker changed before launcher claim")
            return 1
        message = _session_path(session_dir, f"{name}.account-move-prompt.txt")
        _ = message.write_text(ACCOUNT_MOVE_CONTINUATION, encoding="utf-8")
        follow_args = argparse.Namespace(
            session_dir=session_dir, to=name, role=dispatch.role, lens=lens, claim_pid=os.getpid(),
            message_file=str(message), summary_file=str(summary),
            reply_file="", log_file=str(_session_path(session_dir, f"{name}.account-move.log")),
            model="", effort="", service_tier="", turn_cwd="", read_only=False, image=[],
            timeout=86400.0, account_move=True,
        )
    try:
        result = command_follow(follow_args)
    except (ConnectionError, OSError, SystemExit) as error:
        _fail_account_move(session_dir, name, marker, f"resume failed: {error}")
        return 1
    if result and isinstance(_read_account_move(session_dir, name, marker.thread_id), AccountMoveMarker):
        _fail_account_move(session_dir, name, marker, "resume failed; see account-move launcher log")
    return result


def _wait_account_move_resumes(
    moves: tuple[InterruptedWorkerMove, ...],
) -> dict[tuple[str, str], WorkerSwitchOutcome]:
    """Await every dispatched move against one deadline, without serial worker waits."""
    pending = {(move.session_dir, move.name): move for move in moves}
    outcomes: dict[tuple[str, str], WorkerSwitchOutcome] = {}
    deadline = time.monotonic() + ACCOUNT_MOVE_RESUME_TIMEOUT_SECS
    while pending:
        for key, move in tuple(pending.items()):
            try:
                result = _read_json_object(_account_move_result_path(move.session_dir, move.name))
                if result.get("move_id") == move.marker.move_id:
                    if result.get("resumed") is True:
                        outcomes[key] = WorkerMoveResumed()
                    elif reason := _as_str(result.get("error")):
                        outcomes[key] = _worker_move_failure(reason, *key)
                if key in outcomes:
                    del pending[key]
                    continue
                if move.launcher_pid <= 0 or not _pid_alive(move.launcher_pid):
                    outcomes[key] = _worker_move_failure("the director must resume this worker through assign.sh",
                                                         *key)
                    del pending[key]
            except (ConnectionError, OSError, SystemExit) as error:
                outcomes[key] = _worker_move_failure(f"resume failed: {error}", *key)
                del pending[key]
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for key in pending:
                outcomes[key] = _worker_move_failure("resume not confirmed before timeout", *key)
            break
        # The last poll ends at the deadline: summed 0.05 s steps can fall one rounding short of it.
        time.sleep(min(0.05, remaining))
    return outcomes


def _leftover_server_verdict(folder: Path, pid: int, port: int) -> SweepVerdict:
    """Explain why an ended run's server remains active."""
    stderr = _server_folder(pid)
    if isinstance(stderr, UnreadServerStderr):
        return ServerInUse(stderr.reason)
    try:
        clients = _client_counts().get(port, 0)
    except (OSError, subprocess.CalledProcessError) as error:
        return ServerInUse(f"client connections could not be read ({error})")
    facts = ServerFacts(pid, port, 0, clients, RetiredRunServer(folder), folder.is_dir(),
                        UncheckedRunLifecycle(), (), None)
    return _sweep_verdict(facts, lambda: _busy_threads(port, _roster_threads(folder)))


def leftover_server_lines(switches: Sequence[WorkersRunSwitch]) -> tuple[str, ...]:
    total = sum(run.leftover_servers for run in switches)
    if not total:
        return ()
    line = f"{total} leftover servers from ended sessions, {sum(run.stopped_servers for run in switches if run.leftover_servers)} stopped"
    return (line, *(f"left running: {server.pid} on port {server.port} "
                    + f"({run.folder}): {server.reason}"
                    for run in switches for server in run.kept_servers))


def _finish_run_moves(
    runs: list[WorkersRunMove | EndedWorkersRun],
) -> tuple[WorkersRunSwitch, ...]:
    outcomes = _wait_account_move_resumes(tuple(
        worker for run in runs if isinstance(run, WorkersRunMove)
        for worker in run.workers if isinstance(worker, InterruptedWorkerMove)
    ))
    servers: dict[tuple[int, int], bool] = {}
    switches: list[WorkersRunSwitch] = []
    for run in runs:
        stopped = 0
        kept: list[KeptWorkersServer] = []
        for port, pid in run.servers:
            identity = (pid, port)
            if identity not in servers:
                servers[identity] = _stop_unused(None, pid, port, RetiredRunServer(run.folder))
                if not servers[identity]:
                    _ = _start_watcher(str(run.folder), pid, port)
            if servers[identity]:
                stopped += 1
            elif isinstance(run, EndedWorkersRun):
                verdict = _leftover_server_verdict(run.folder, pid, port)
                reason = verdict.reason if isinstance(verdict, ServerInUse) else "not stopped at the second safety check"
                kept.append(KeptWorkersServer(pid, port, reason))
        if isinstance(run, EndedWorkersRun):
            switches.append(WorkersRunSwitch(run.folder, (), (), len(run.servers), stopped, tuple(kept)))
        else:
            switches.append(WorkersRunSwitch(run.folder, run.holders, tuple(
                SwitchingWorker(worker.name, True, outcomes[(worker.session_dir, worker.name)])
                if isinstance(worker, InterruptedWorkerMove) else worker for worker in run.workers
            ), stopped_servers=stopped))
    return tuple(switches)


@worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS)
def _interrupt_worker_for_account_move(session_dir: str, name: str, entry: dict[str, object],
                                       port: int, label: str, *,
                                       operation: BulkWorkers | AcquireMembership = ACQUIRE_MEMBERSHIP) -> SwitchingWorker | InterruptedWorkerMove:
    entry = _as_dict(session_staff.stored_worker_records({name: entry}).get(name))
    deadline = operation.deadline if isinstance(operation, BulkWorkers) else worker_lifecycle.effective_deadline()
    with contextlib.ExitStack() as locks:
        if isinstance(operation, AcquireMembership):
            _ = locks.enter_context(worker_lifecycle.membership_operation(session_dir, deadline=deadline))
            _ = locks.enter_context(worker_lifecycle.lock_worker(session_dir, name, deadline=deadline))
        elif (not operation.membership.covers(session_dir)
              or worker_lifecycle.WorkerIdentity(Path(session_dir), name) not in worker_lifecycle.held_workers()):
            raise RuntimeError("account move requires its passed bulk ownership")
        current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if current != entry or current.get("status") != "running":
            return SwitchingWorker(name, False, _worker_move_failure("worker ownership changed before account move", session_dir, name))
        thread_id = _as_str(session_staff.worker_session(entry))
        marker: AccountMoveMarker | NoAccountMove = NoAccountMove()
        client: Client | None = None
        try:
            if not thread_id:
                raise SystemExit("worker has no recorded thread")
            availability = _server_availability(session_dir)
            if isinstance(availability, ServerRestartRequired) and availability.doubt:
                raise SystemExit(f"no live server evidence: {availability.doubt}")
            state: ThreadState = ThreadIdle()
            for candidate in sorted(_run_servers(session_dir), key=lambda candidate: candidate != port):
                client = Client(candidate, f"account-move-{name}", operation_deadline=deadline)
                if thread_id in _loaded_thread_ids(client, deadline):
                    state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
                    break
                client.close()
                client = None
            if isinstance(state, ThreadStateUnknown):
                raise SystemExit(state.reason)
            if isinstance(state, ThreadActiveWithoutTurn):
                raise SystemExit("live turn has no interruptible id")
            # The turn may have just ended. It still needs a continuation if its launcher
            # has not consumed that completion, so retain the roster's last turn id.
            turn_id = state.turn_id if isinstance(state, ThreadLive) else _as_str(entry.get("turn_id"))
            if not turn_id:
                return SwitchingWorker(name, False)
            marker = AccountMoveMarker(thread_id, turn_id, label, secrets.token_hex(16))
            stored: StoredAccountMove = {"thread_id": marker.thread_id, "turn_id": marker.turn_id,
                                        "label": marker.label, "move_id": marker.move_id}
            with _pending_file(session_dir, name, deadline=deadline):
                if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                    raise SystemExit("worker ended or was replaced before interruption")
                _write_account_move_json(_account_move_path(session_dir, name), stored)
            if client is not None:
                with _pending_file(session_dir, name, deadline=deadline) as pending:
                    def park(message: str, submission: str) -> None:
                        _park_submission(pending, message, submission)
                    _move_queue_to_pending(client, thread_id, park)
                if isinstance(state, ThreadLive):
                    reply = _interrupt_turn(client, thread_id, state.turn_id)
                    if "error" in reply and not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
                        _account_move_path(session_dir, name).unlink(missing_ok=True)
                        _ = _require(reply, "turn/interrupt")
                client.close()
                client = None
            return InterruptedWorkerMove(session_dir, name, marker, _as_int(entry.get("launcher_pid")))
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (ConnectionError, OSError, SystemExit) as error:
            reason = f"interrupt failed: {error}"
            if isinstance(marker, AccountMoveMarker):
                with contextlib.suppress(OSError):
                    _fail_account_move(session_dir, name, marker, reason)
            return SwitchingWorker(name, True, _worker_move_failure(reason, session_dir, name))
        finally:
            if client is not None:
                client.close()


@worker_lifecycle.operation_deadline(timeout=THREAD_RELEASE_TIMEOUT_SECS)
def _switch_run_workers(
    folder: Path, port: int, label: str, *,
    operation: BulkWorkers | AcquireMembership = ACQUIRE_MEMBERSHIP,
) -> tuple[SwitchingWorker | InterruptedWorkerMove, ...]:
    if isinstance(operation, AcquireMembership):
        deadline = worker_lifecycle.effective_deadline()
        with _bulk_workers(str(folder), deadline=deadline, allow_held_roles=True) as owned, _server_lock(str(folder), deadline=deadline):
            return _switch_run_workers(folder, port, label, operation=owned)
    entries = operation.records
    workers: list[SwitchingWorker | InterruptedWorkerMove] = []
    for name, value in sorted(entries.items()):
        entry = _as_dict(value)
        if entry.get("status") == "ended" or entry.get("port", port) != port:
            continue
        if entry.get("status") == "running":
            workers.append(_interrupt_worker_for_account_move(str(folder), name, entry, port, label, operation=operation))
        else:
            if entry.get("held_design_id"):
                thread_id = _as_str(session_staff.worker_session(entry))
                client = Client(port, "held-idle-account-move", operation_deadline=operation.deadline)
                try:
                    with _pending_file(str(folder), name, deadline=operation.deadline) as pending:
                        def park(message: str, submission: str) -> None:
                            _park_submission(pending, message, submission)
                        _move_queue_to_pending(client, thread_id, park)
                        released = _release_worker_thread(client, thread_id, str(folder))
                        if isinstance(released, WorkerThreadStateUnknown):
                            raise SystemExit("RoleNeedsRecovery: " + released.reason)
                finally:
                    client.close()
            workers.append(SwitchingWorker(name, entry.get("status") == "running"))
    return tuple(workers)


def command_serve(args: argparse.Namespace) -> int:
    port, _fresh = ensure_server(_as_str(_attr(args, "session_dir")))
    print(port)
    return 0



def _failure_account_detail(error: BaseException, serving: CodexServingState) -> str:
    account = serving.account
    name = account.label if isinstance(account, KnownCodexAccount) else "unknown Codex account"
    return f"{name} ({codex_home_display(serving.home)}): {error}"


def _admit_worker(name: str, session_dir: str, *, held_design: str = "") -> WorkerStartAllowed:
    """Use the staff parent of an existing worker, or admit a new worker."""
    recorded = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    held = held_design or _as_str(recorded.get("held_design_id"))
    if held and recorded.get("staff_id"):
        from scripts.astra import design_roles
        design = design_roles.read_design(held)
        instance = next((value for value in design.roles if value.name == name), None)
        if instance is None:
            raise ValueError("design worker is unavailable")
        membership = staff.snapshot()
        owner = design_roles.worker_parent(design, instance.role, membership)
        row = membership.by_id(staff.StaffId(str(recorded["staff_id"])))
        if row is None or row.parent != staff.ReportsTo(owner.id):
            raise ValueError("design worker membership is unavailable")
        return WorkerStartAllowed(row.name, owner.id, Path(session_dir), worker_admission.ExistingWorkerSlot(row))
    return worker_admission.admit(name, Path(session_dir))


def command_prime(args: argparse.Namespace) -> int:
    """Create a read-only reviewer template without starting a turn."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    try:
        admitted = _admit_worker(name, session_dir, held_design=_as_str(_attr(args, "held_design")))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return _prime_admitted_worker(args, admitted)


def _prime_admitted_worker(args: argparse.Namespace, admitted: WorkerStartAllowed) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = admitted.name
    with worker_lifecycle.membership_operation(session_dir), contextlib.ExitStack() as lifecycle:
        _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
        client: Client | None = None
        try:
            existing = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            if existing.get("held_design_id"):
                raise SystemExit("held role already has a thread; use design_roles.py resume")
            held_design = _as_str(_attr(args, "held_design"))
            held_role = _as_str(_attr(args, "held_role"))
            if held_design:
                from scripts.astra import design_roles
                design = design_roles.read_design(held_design)
                instance = next((value for value in design.roles if value.name == name and value.role == held_role), None)
                if (instance is None or not isinstance(instance.lifecycle, design_roles.RoleNotOpened)
                        or not instance.lifecycle.claim):
                    raise SystemExit("held thread creation lacks its reserved claim")
                claim = design_roles.design_directory(held_design) / f"{held_role}.creation-claimed"
                with claim.open("x", encoding="utf-8") as handle:
                    _ = handle.write(instance.lifecycle.claim)
            instructions_path = Path(_as_str(_attr(args, "instructions_file"))).resolve()
            instructions = instructions_path.read_text(encoding="utf-8")
            membership_record = staff.local_snapshot()
            cwd = session_staff.worker_directory(membership_record, admitted.parent)
            params: dict[str, object] = {
                "developerInstructions": instructions,
                "cwd": _as_str(_attr(args, "cwd")) or str(cwd),
                "sandbox": _as_str(_attr(args, "sandbox")) or READ_ONLY_SANDBOX,
                "approvalPolicy": "never",
            }
            if held_design:
                params["sandbox"] = READ_ONLY_SANDBOX if held_role == "reviewer" else WORKER_SANDBOX
            model = _as_str(_attr(args, "model"))
            if model:
                params["model"] = model
            service_tier = _as_str(_attr(args, "service_tier"))
            if service_tier:
                params["serviceTier"] = service_tier
            port, _fresh = ensure_server(session_dir, cwd=str(cwd))
            client = Client(port, name)
            _ = _bind_serving_server(client, session_dir, started_here=_fresh)
            lifecycle.close()
            published = _create_published_worker(client, session_dir, admitted, "thread/start", params)
            _ = lifecycle.enter_context(worker_lifecycle.lock_worker(session_dir, name))
            thread_id = published.row.current_session
            name = published.row.name
            if held_design:
                roster = cast(MeshTitleRenderer, cast(object, importlib.import_module("scripts.message.roster")))
                title = roster.mesh_title(session_dir, name, {
                    "held_design_id": held_design, "held_role": held_role,
                    "staff_id": str(published.row.id),
                })
            else:
                title = unit_workers.shown_name(Path(session_dir), name, role="review")
            _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": title}), "thread/name/set")
            record: FollowableRecord = {
                "staff_id": published.row.id,
                "thread_id": thread_id, "status": "done", "role": "review",
                "context_tokens": 0, "instructions_file": str(instructions_path),
            }
            record["model"] = model
            record["effort"] = _as_str(_attr(args, "effort"))
            record["access_policy"] = {"kind": "ReadOnlyReviewer"}
            if held_design:
                context = _read_json_object(_session_path(session_dir, "design_context.json"))
                record.update({"held_design_id": held_design, "held_role": held_role,
                               "access_policy": {"kind": "ReadOnlyReviewer"} if held_role == "reviewer" else {
                                   "kind": "PhaseBoundDesigner", "binding_generation": context.get("generation"),
                                   "launch_sandbox": params["sandbox"]}})
            task = _as_str(_attr(args, "task"))
            if task:
                record["task"] = task
            expected: ExpectedRoster = (RosterUnchanged(_as_str(session_staff.worker_session(existing)), _roster_revision(existing))
                                        if existing else RosterAbsent())
            if existing and session_staff.worker_session(existing) != thread_id:
                expected = RosterBackendReplaced(published.row.id, thread_id,
                    _roster_revision(existing), _as_str(existing.get("status")))
            if not _update_roster_if_expected(session_dir, name, record, expected):
                _refuse_taken_follow_up(client, thread_id, session_dir)
            print(thread_id)
            return 0
        except worker_admission.AdmissionRefused as error:
            print(str(error), file=sys.stderr)
            return 2
        except (ConnectionError, OSError, UnicodeError, SystemExit) as exc:
            if isinstance(exc, ServingRequestFailure):
                detail = _failure_account_detail(exc, exc.serving)
            else:
                detail = str(exc)
            print(f"codex_mesh: {name}: {detail}", file=sys.stderr)
            return 1
        finally:
            if client is not None:
                client.close()


def command_fork(args: argparse.Namespace) -> int:
    """Load a template and create a named read-only reviewer from it."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    try:
        admitted = worker_admission.admit(name, Path(session_dir))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return _fork_admitted_worker(args, admitted)


def _fork_admitted_worker(args: argparse.Namespace, admitted: WorkerStartAllowed) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = admitted.name
    with worker_lifecycle.membership_operation(session_dir), contextlib.ExitStack() as lifecycle:
        workers = (worker_lifecycle.WorkerIdentity(Path(session_dir), name),
                   worker_lifecycle.WorkerIdentity(Path(session_dir), _as_str(_attr(args, "from_name"))))
        _ = lifecycle.enter_context(worker_lifecycle.lock_workers(*workers))
        client: Client | None = None
        try:
            existing = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            membership_record = staff.local_snapshot()
            previous_thread = session_staff.worker_session(existing, membership_record)
            if previous_thread and existing.get("status") != "ended":
                print(f"codex_mesh: {name}: name already belongs to a thread; fork refused", file=sys.stderr)
                return 2
            expected: ExpectedRoster = RosterEndedOnThread(previous_thread) if previous_thread else RosterAbsent()
            source_name = _as_str(_attr(args, "from_name"))
            source_record = _lookup(session_dir, source_name)
            if source_record.get("held_design_id"):
                raise SystemExit("held role thread cannot be forked; use its holder route")
            source = _roster_thread(source_record)
            if isinstance(source, InvalidThread):
                raise SystemExit(source.reason)
            port, _fresh = ensure_server(session_dir)
            released = _release_other_worker_writers(session_dir, source_name, source.thread_id, port)
            if not isinstance(released, _ReleasedWorkerThread):
                print(f"codex_mesh: {name}: {released.reason}; fork refused", file=sys.stderr)
                return 2
            client = Client(port, name)
            _ = _bind_serving_server(client, session_dir, started_here=_fresh)
            params = _resume_params(session_dir, source_name, source.thread_id)
            _ = _serving_request(client, "thread/resume", params)
            fork_params = dict(params)
            fork_params.update({"sandbox": READ_ONLY_SANDBOX, "approvalPolicy": "never"})
            lifecycle.close()
            published = _create_published_worker(client, session_dir, admitted, "thread/fork", fork_params)
            _ = lifecycle.enter_context(worker_lifecycle.lock_workers(*workers))
            thread_id = published.row.current_session
            name = published.row.name
            if existing:
                expected = RosterBackendReplaced(published.row.id, thread_id,
                    _roster_revision(existing), _as_str(existing.get("status")))
            _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": unit_workers.shown_name(Path(session_dir), name, role="review")}), "thread/name/set")
            record: FollowableRecord = {
                "staff_id": published.row.id,
                "thread_id": thread_id, "status": "done", "role": "review", "context_tokens": 0,
                "access_policy": {"kind": "ReadOnlyReviewer"},
            }
            task = _as_str(source_record.get("task"))
            if task:
                record["task"] = task
            instructions_file = _as_str(source_record.get("instructions_file"))
            if instructions_file:
                record["instructions_file"] = instructions_file
            if not _update_roster_if_expected(session_dir, name, record, expected):
                released = _release_worker_thread(client, thread_id, session_dir)
                if isinstance(released, WorkerThreadStateUnknown):
                    print(f"codex_mesh: {name}: name was taken while the fork ran; "
                          + f"fork thread {thread_id} not released: {released.reason}", file=sys.stderr)
                    return 1
                print(f"codex_mesh: {name}: name was taken while the fork ran; fork refused", file=sys.stderr)
                return 2
            print(thread_id)
            return 0
        except worker_admission.AdmissionRefused as error:
            print(str(error), file=sys.stderr)
            return 2
        except (ConnectionError, OSError, SystemExit) as exc:
            if isinstance(exc, ServingRequestFailure):
                detail = _failure_account_detail(exc, exc.serving)
            else:
                detail = str(exc)
            print(f"codex_mesh: {name}: {detail}", file=sys.stderr)
            return 1
        finally:
            if client is not None:
                client.close()


def command_set_instructions(args: argparse.Namespace) -> int:
    """Point an idle reviewer's standing prompt at a new file; its next resume sends that file.

    The thread and its turns stay. A reviewer that is busy or not followable is refused (exit 2),
    so a turn already running keeps the prompt it started with.
    """
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    with worker_lifecycle.lock_worker(session_dir, name):
        if _lookup(session_dir, name).get("held_design_id"):
            raise SystemExit("codex_mesh: held-role set-instructions refused; use design_roles.py resume")
        instructions = Path(_as_str(_attr(args, "instructions_file"))).resolve()
        if not instructions.is_file():
            print(f"codex_mesh: {name}: no instructions file at {instructions}", file=sys.stderr)
            return 1
        state = _lookup_state(session_dir, name)
        if not isinstance(state, FollowableThread):
            reason = state.reason if isinstance(state, InvalidThread) else "worker is busy or not followable"
            print(f"codex_mesh: {name}: {reason}; instructions unchanged", file=sys.stderr)
            return 2
        path = _session_path(session_dir, ROSTER_FILE)
        with worker_lifecycle.locked_state(path) as handle:
            roster = _load_roster_for_write(handle)
            entry = _as_dict(roster.get(name))
            if session_staff.worker_session(entry) != state.thread_id or entry.get("status") != "done":
                print(f"codex_mesh: {name}: worker changed while setting instructions; instructions unchanged",
                      file=sys.stderr)
                return 2
            entry["instructions_file"] = str(instructions)
            _advance_roster_revision(entry)
            roster[name] = entry
            session_staff.publish_worker_roster(handle, roster)
        return 0


def _clear_reset_context(session_dir: str, name: str, thread_id: str, launcher_pid: int) -> bool:
    """Clear context only while reset still owns this thread's worker claim."""
    path = _session_path(session_dir, ROSTER_FILE)
    with worker_lifecycle.locked_state(path) as handle:
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if (session_staff.worker_session(entry) != thread_id or entry.get("status") != "starting"
                or entry.get("launcher_pid") != launcher_pid):
            return False
        entry["context_tokens"] = 0
        _advance_roster_revision(entry)
        roster[name] = entry
        session_staff.publish_worker_roster(handle, roster)
        return True


def command_reset(args: argparse.Namespace) -> int:
    """Remove all review turns from an idle, followable thread."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    client: Client | None = None
    claimed = False
    claimed_thread_id = ""
    owner_pid = os.getpid()
    try:
        with _design_claim_lock(session_dir, name, lock_inbox=False):
            state = _lookup_state(session_dir, name)
            if not isinstance(state, FollowableThread):
                reason = state.reason if isinstance(state, InvalidThread) else "worker is busy or not followable"
                print(f"codex_mesh: {name}: {reason}; reset refused", file=sys.stderr)
                return 2
            role = task_role(state.thread_id, task_assignments())
            reset_role = role.role if isinstance(role, AssignedWorkerRole) else "review"
            port, _fresh = ensure_server(session_dir)
            released = _release_other_worker_writers(session_dir, name, state.thread_id, port)
            if not isinstance(released, _ReleasedWorkerThread):
                print(f"codex_mesh: {name}: {released.reason}; reset refused", file=sys.stderr)
                return 2
            client = Client(port, name)
            _ = _bind_serving_server(client, session_dir, started_here=_fresh)
            _ = _serving_request(client, "thread/resume", _resume_params(session_dir, name, state.thread_id))
            read = _serving_request(client, "thread/read", {
                "threadId": state.thread_id, "includeTurns": False,
            })
            thread = _as_dict(read.get("thread"))
            status = _as_str(_as_dict(thread.get("status")).get("type"))
            if not _thread_has_no_active_turn(status):
                print(f"codex_mesh: {name}: thread state is {status or 'unknown'}; reset refused", file=sys.stderr)
                return 2
            if not _claim_follow_locked(session_dir, name, state.thread_id, owner_pid, reset_role):
                print(f"codex_mesh: {name}: worker was already claimed; reset refused", file=sys.stderr)
                return 2
            claimed = True
            claimed_thread_id = state.thread_id
            result = _serving_request(client, "thread/turns/list", {
                "threadId": state.thread_id, "sortDirection": "asc", "limit": 1,
            })
            turns = result.get("data")
            if not isinstance(turns, list):
                raise SystemExit("thread/turns/list returned no turn list")
            if turns:
                first_turn_id = _as_str(_as_dict(cast("list[object]", turns)[0]).get("id"))
                if not first_turn_id:
                    raise SystemExit("thread/turns/list returned no first turn id")
                _ = _serving_request(client, "thread/revert", {
                    "threadId": state.thread_id, "beforeTurnId": first_turn_id,
                })
            if not _clear_reset_context(session_dir, name, state.thread_id, owner_pid):
                print(f"codex_mesh: {name}: worker claim changed; reset refused", file=sys.stderr)
                return 2
            return 0
    except (ConnectionError, OSError, SystemExit) as exc:
        if isinstance(exc, ServingRequestFailure):
            detail = _failure_account_detail(exc, exc.serving)
        else:
            detail = str(exc)
        print(f"codex_mesh: {name}: {detail}", file=sys.stderr)
        return 1
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            if claimed:
                _restore_follow_claim(session_dir, name, owner_pid, expected_thread_id=claimed_thread_id)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in {"start", "follow", "reopen", "fork", "adopt", "prime", "resume-account-move"}:
        assign_gate.require_assign()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    def command_parser(verb: str, handler: Callable[[argparse.Namespace], int], help_text: str,
                       required: tuple[str, ...] = ("session-dir",)) -> argparse.ArgumentParser:
        command = subparsers.add_parser(verb, help=help_text)
        command.set_defaults(handler=handler)
        for option in required:
            _ = command.add_argument("--" + option, required=True)
        return command

    _ = command_parser("serve", command_serve, "start the session app-server")
    _ = command_parser("release-held-server", command_release_held_server, "")

    start = command_parser("start", command_start, "launch a worker and block", ("session-dir", "name", "prompt-file", "summary-file", "log-file"))
    _ = start.add_argument("--staff-id", default="")
    _ = start.add_argument("--assignment-file", default="", help="the file that identifies this assignment across retries; default is the prompt file")
    _ = start.add_argument("--role", choices=WORKER_ROLES, default="")
    _ = start.add_argument("--lens", choices=agents_log.LENSES)
    _ = start.add_argument(
        "--reply-file",
        default="",
        help="append replies here; the summary file is then the worker's own",
    )
    _ = start.add_argument("--cwd", default="")
    _ = start.add_argument("--sandbox", choices=SANDBOX_MODES, default=WORKER_SANDBOX)
    _ = start.add_argument("--timeout", type=float, default=86400.0)
    _ = start.add_argument(
        "--resident",
        action="store_true",
        help="stay attached across turns, delivering each reply, until `end`",
    )

    _ = command_parser("resume-account-move", command_resume_account_move, "resume an interrupted account move", ("session-dir", "to"))

    follow = command_parser("follow", command_follow, "run one turn on a finished worker", ("session-dir", "to", "message-file", "summary-file", "log-file"))
    _ = follow.add_argument("--role", choices=WORKER_ROLES, required=True)
    _ = follow.add_argument("--lens", choices=agents_log.LENSES)
    _ = follow.add_argument("--claim-pid", type=int, default=0)
    _ = follow.add_argument("--reply-file", default="")
    _ = follow.add_argument("--submission-receipt-file", default="", help=argparse.SUPPRESS)
    _ = follow.add_argument("--cwd", dest="turn_cwd", default="")
    _ = follow.add_argument("--read-only", action="store_true")
    _ = follow.add_argument("--image", action="append", default=[])
    _ = follow.add_argument("--timeout", type=float, default=86400.0)

    prime = command_parser("prime", command_prime, "create a read-only reviewer template without a turn", ("session-dir", "name", "instructions-file"))
    _ = prime.add_argument("--cwd", default="")
    _ = prime.add_argument("--sandbox", choices=SANDBOX_MODES, default=READ_ONLY_SANDBOX)
    _ = prime.add_argument("--held-design", default="")
    _ = prime.add_argument("--held-role", default="")

    fork = command_parser("fork", command_fork, "create a named read-only fork of a template", ("session-dir", "name"))
    _ = fork.add_argument("--from", dest="from_name", required=True)

    _ = command_parser("set-instructions", command_set_instructions, "point an idle reviewer at a new standing prompt", ("session-dir", "to", "instructions-file"))

    _ = command_parser("reset", command_reset, "remove all turns from an idle reviewer", ("session-dir", "to"))

    _ = command_parser("compact", command_compact, "compact a finished worker", ("session-dir", "to"))

    can_follow = command_parser("can-follow", command_can_follow, "check whether a worker can take a follow-up", ("session-dir", "to"))
    _ = can_follow.add_argument("--role", choices=WORKER_ROLES, required=True)
    _ = can_follow.add_argument("--lens", choices=agents_log.LENSES)
    _ = can_follow.add_argument("--claim-pid", type=int, default=0)

    release_follow = command_parser("release-follow", command_release_follow, "release an unstarted follow claim", ("session-dir", "to"))
    _ = release_follow.add_argument("--claim-pid", type=int, required=True)

    send = command_parser("send", command_send, "queue a message for a worker; without a launcher it waits for the next dispatch", ("session-dir", "to"))
    _ = send.add_argument("--from", dest="sender", default="codex-mesh")
    _ = send.add_argument("--human", action="store_true")
    _ = send.add_argument("--no-accounting", action="store_true", help=argparse.SUPPRESS)
    _ = send.add_argument("--report-recipient", action="store_true", help=argparse.SUPPRESS)
    _ = send.add_argument("--guard-text", default="", help=argparse.SUPPRESS)
    _ = send.add_argument("--repeat-checked", action="store_true", help=argparse.SUPPRESS)
    send_body = send.add_mutually_exclusive_group(required=True)
    _ = send_body.add_argument("--message")
    _ = send_body.add_argument("--message-file", help="read the message from this file")

    steer = command_parser("steer", command_steer, "interrupt a worker's running turn", ("session-dir", "to"))
    _ = steer.add_argument("--human", action="store_true")
    steer_body = steer.add_mutually_exclusive_group(required=True)
    _ = steer_body.add_argument("--message")
    _ = steer_body.add_argument("--message-file", help="read the message from this file")

    interrupt = command_parser("interrupt", command_interrupt, "stop a worker's running turn, keeping the worker", ("session-dir", "to"))
    _ = interrupt.add_argument("--human", action="store_true")
    _ = interrupt.add_argument("--from", dest="sender", default="codex-mesh")
    interrupt_body = interrupt.add_mutually_exclusive_group()
    _ = interrupt_body.add_argument("--message", default="")
    _ = interrupt_body.add_argument("--message-file", help="read the message from this file")

    end = command_parser("end", command_end, "release a resident worker", ("session-dir", "to"))
    _ = end.add_argument("--expected-thread-id", default="", help="refuse if this worker now names another thread")
    _ = end.add_argument("--idle-only", action="store_true", help="refuse a running worker without changing it")

    stop = command_parser("stop", command_stop, "stop the session app-server")
    _ = stop.add_argument("--pause", action="store_true", help="retain worker threads for a director restart")

    adopt = command_parser("adopt", command_adopt, "carry an idle unit worker into this run", ("session-dir", "name"))
    _ = adopt.add_argument("--from", dest="from_run", required=True)

    _ = command_parser("reopen", command_reopen, "recover an ended or abandoned worker on its recorded thread", ("session-dir", "to"))

    idle_stop = command_parser("idle-stop", command_idle_stop, "stop this session's app-server if nothing uses it")
    _ = idle_stop.add_argument("--pid", type=int, required=True)
    _ = idle_stop.add_argument("--port", type=int, required=True)

    sweep = command_parser("sweep", command_sweep, "report app-servers nothing is using", ())
    _ = sweep.add_argument("--root", default="")
    _ = sweep.add_argument("--stop", action="store_true", help="stop each server two looks in a row call unused")

    signin_changed = command_parser("signin-changed", command_signin_changed, "move every run off an app-server older than the Codex sign-in", ())
    _ = signin_changed.add_argument("--root", default="")

    _ = command_parser("list", command_list, "print the worker roster")

    for command in (start, follow, prime):
        for option in ("model", "effort", "service-tier", "task"):
            _ = command.add_argument("--" + option, default="")

    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", _attr(args, "handler"))
    try:
        if handler is command_start:
            # Preserve the CLI's account-configuration refusal before any admission write.
            _ = workers_home()
        return handler(args)
    except worker_lifecycle.WorkerStateUnreadable as error:
        print(f"codex_mesh: {error}; worker operation refused", file=sys.stderr)
        return 2
    except (RoleMapUnavailable, RoleAccountUnavailable) as error:
        print(error, file=sys.stderr)
        return 1




def _durable_meshes() -> tuple[Path, ...]:
    from scripts.astra import design_roles

    return tuple(sorted(path.parent for path in design_roles.state_root().glob("*/mesh/mesh_roster.json")))


if __name__ == "__main__":
    sys.exit(main())
