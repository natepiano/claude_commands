#!/usr/bin/env python3
"""codex_mesh.py — Run codex workers as addressable threads on one app-server.

Why this exists rather than agent_exec.sh: a worker launched with `codex exec`
is unreachable. Nothing outside its own process can hand it a message, so peers
coordinate only through the board and a worker that goes down the wrong path
stays there until its turn ends. `codex app-server` fixes that by hosting every
worker as a thread on one local websocket: any process that can open a socket
can queue a message for a named worker, or steer one mid-turn.

The pieces this depends on, each verified against codex 0.162.0:

  * `initialize` must declare `capabilities.experimentalApi`. Without it the
    queue methods return -32600 with no hint that a flag is missing.
  * `sandbox` takes the SandboxMode STRING ("danger-full-access"), not the
    SandboxPolicy object the generated schema shows for other fields.
  * `thread/queue/add` reaches a thread between turns; `turn/steer` interrupts a
    running one and needs the turn id the worker is currently on.

`start` deliberately blocks for the worker's lifetime. Launches that return
immediately would break every caller that waits on a pid, so this stays in the
foreground and implement.sh's `wait`, heartbeat watch, and pass recording work
unchanged.

Verbs:
  serve  Start the session's app-server and record where it listens.
  start  Launch one worker as a named thread; block until its turn ends.
         The last reply lands in --summary-file, unless --reply-file names
         where replies go: then the summary file belongs to the worker,
         which writes it as its own last act, and the reply fills it only
         when the worker left it empty.
         With --resident, stay attached across turns instead: each finished
         turn is printed to stdout and delivered the same way, the thread
         keeps accepting `send`, and only `end` releases the block.
  follow Resume a finished named thread, start one turn, and block for that turn.
  compact
         Compact a finished named thread and print its context size before and
         after compaction.
         A compaction that does not finish in time is interrupted, so the
         worker can still take a follow-up (exit 1). Exit 3 means a turn is
         still live on the thread and no follow-up can be sent.
  send   Queue a message for a named worker, delivered at its next turn.
  steer  Inject into a named worker's running turn.
  interrupt
         Stop a named worker's running turn and keep the worker. A --message
         is held first and opens its next turn.
  end    Finish a resident worker: drop its queued messages, interrupt its
         running turn, if any, and release its `start`.
  stop   Stop the session's app-server, and any it replaced mid-run.
  signin-changed
         Move every run off an app-server started before the Codex sign-in
         last changed, and tell the session holding each run. A path watch
         on the sign-in file runs it.
  list   Print the roster of named workers and what each is doing.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import errno
import fcntl
import importlib
import json
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
from scripts.direct import plan_history
from scripts.lib import nix_env, unit_runs
from scripts.lib.probe_session import PROBE_ENV
from scripts.astra.design_roles import PhaseBoundDesigner, ReadOnlyReviewer, ThreadAccessPolicy
from scripts.message import sessions, process_table
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


class UnitWorkerRegistry(Protocol):
    """Ledger operations used by mesh without importing its allocation client."""

    WorkerUnavailable: type[Exception]

    def recover_adoptions(self, run: Path) -> None: ...
    def pause_run(self, run: Path) -> None: ...
    def run_has_live_director(self, run: Path) -> bool: ...
    def note_backend(self, run: Path, name: str, thread_id: str, home: Path) -> None: ...
    def ledger_for_run(self, run: Path) -> UnitWorkerLedgerLocation: ...
    def shown_name(self, run: Path, address: str, *, role: str = "", held_design_id: str = "",
                   held_role: str = "") -> str: ...
    def adopt(self, source: Path, destination: Path, name: str) -> str: ...
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

SERVER_FILE = "mesh_server.json"
DEV_SHELL_FILE = "mesh_dev_shell.json"
FAILED_DEV_SHELL_FILE = "mesh_failed_dev_shell.json"
SERVER_START_FILE = "mesh_server_start.json"
SERVER_LOG = "mesh_server.log"
ROSTER_FILE = "mesh_roster.json"
# Runs a worker detached with mesh_runs.py: one `<run id>.json` record and `<run id>.log` each.
DETACHED_RUNS_DIR = "mesh_runs"
# A run record written before its supervisor's pid counts as live for this long.
DETACHED_RUN_START_GRACE_SECS = 60.0
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
RETIRED_FILE = "mesh_retired.json"
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
    """The recorded app-server is gone; resume from the thread rollout."""


ServerAvailability = LiveServer | ServerRestartRequired


WorkerRole = Literal["impl", "test", "fix", "review"]
WORKER_ROLES = ("impl", "test", "fix", "review")


@dataclass(frozen=True)
class AssignedWorkerRole:
    """A unit-run thread whose current assignment belongs in the roster."""

    role: WorkerRole


@dataclass(frozen=True)
class RolelessThread:
    """A mesh thread outside a unit run, such as a consultation friend."""


RosterRole = AssignedWorkerRole | RolelessThread


class WorkerTaskRecord(TypedDict):
    task: NotRequired[str]
    model: NotRequired[str]
    effort: NotRequired[str]
    held_design_id: NotRequired[str]
    held_role: NotRequired[str]
    access_policy: NotRequired[dict[str, object]]
    authorized_prompt: NotRequired[str]
    authorized_generation: NotRequired[int]
    authorized_holder: NotRequired[str]
    transferring_role: NotRequired[dict[str, object]]
    role_redirect: NotRequired[dict[str, object]]
    adopted_from: NotRequired[str]
    adoption_id: NotRequired[str]
    unit_ledger: NotRequired[str]


class FollowableRecord(WorkerTaskRecord):
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
    previous_role: NotRequired[WorkerRole]
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
class AdoptedThread:
    thread_id: str
    to_run: str


@dataclass(frozen=True)
class InvalidThread:
    reason: str


RosterThread = (FollowableThread | ActiveThread | WaitingCapacityThread |
                StartingThread | ExhaustedThread | EndedThread | AdoptedThread | InvalidThread)


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
    thread_id = _as_str(entry.get("thread_id"))
    status = entry.get("status")
    turn_id = _as_str(entry.get("turn_id"))
    pid = entry.get("launcher_pid")
    launcher_pid = pid if isinstance(pid, int) else 0
    if not thread_id:
        return InvalidThread("missing thread id")
    adopted_to = _as_str(entry.get("adopted_to"))
    if adopted_to:
        return AdoptedThread(thread_id, adopted_to)
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
    role = _as_str(value)
    if not role:
        return RolelessThread()
    if role not in WORKER_ROLES:
        raise SystemExit(
            f"codex_mesh: role must be impl, test, fix, or review; got '{role}'"
        )
    return AssignedWorkerRole(role)


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


class SocketReady(Enum):
    """The watched connection can be read without blocking."""

    READY = "ready"


class WaitDeadlineExpired(Enum):
    """No relevant event arrived before the monotonic wait deadline."""

    EXPIRED = "expired"


@dataclass(frozen=True)
class StateFilesChanged:
    paths: frozenset[Path]


FileWaitResult = SocketReady | StateFilesChanged | WaitDeadlineExpired

# inotify's parent watches discover replacements; only file watches subscribe
# to content writes. Unrelated logs and heartbeats never enter our event queue.
_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_REPLACEMENT = _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE
_IN_INVALIDATED = _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_IGNORED


@final
class LinuxFileChanges:
    """Watch state files directly, and names in parents only for replacements."""

    def __init__(self, paths: set[Path]) -> None:
        library = ctypes.CDLL(None, use_errno=True)
        initialize = cast(Callable[[int], int], library.inotify_init1)
        self._add_watch = cast(Callable[[int, bytes, int], int], library.inotify_add_watch)
        self._remove_watch = cast(Callable[[int, int], int], library.inotify_rm_watch)
        self.descriptor: int = initialize(os.O_CLOEXEC | os.O_NONBLOCK)
        if self.descriptor < 0:
            raise OSError(ctypes.get_errno(), "cannot watch follower state")
        self.paths: set[Path] = paths
        self.watches: dict[int, Path] = {}
        self.directories: set[Path] = set()
        try:
            self._arm()
        except BaseException:
            self.close()
            raise

    def _arm(self) -> None:
        wanted: dict[Path, int] = {}
        for path in self.paths:
            directory = path.parent
            while not directory.is_dir():
                directory = directory.parent
            self.directories.add(directory)
            wanted[directory] = _IN_REPLACEMENT | _IN_DELETE_SELF | _IN_MOVE_SELF
            if path.is_file():
                # ensure_server touches its record to extend the server lease.
                # That metadata-only write does not change its serving identity.
                wanted[path] = _IN_MODIFY | _IN_DELETE_SELF | _IN_MOVE_SELF
                if path.name != SERVER_FILE:
                    wanted[path] |= _IN_ATTRIB
        for path, mask in wanted.items():
            if path in self.watches.values():
                continue
            watch = self._add_watch(self.descriptor, os.fsencode(path), mask)
            if watch < 0:
                if ctypes.get_errno() == errno.ENOENT:
                    continue  # The already armed parent reports the next creation.
                raise OSError(ctypes.get_errno(), f"cannot watch {path}")
            self.watches[watch] = path

    def wait(self, connection: socket.socket, deadline: float) -> FileWaitResult:
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [connection, self.descriptor], [], [], max(0.0, deadline - time.monotonic()))
            changed: set[Path] = set()
            if self.descriptor in ready:
                data = os.read(self.descriptor, 65536)
                offset = 0
                while offset < len(data):
                    watch, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                    name = os.fsdecode(data[offset + 16:offset + 16 + length].split(b"\0", 1)[0])
                    offset += 16 + length
                    path = self.watches.get(watch)
                    if mask & _IN_Q_OVERFLOW:
                        changed.update(self.paths)
                    if path is None:
                        continue
                    if path in self.directories:
                        target = path / name
                        changed.update(wanted for wanted in self.paths
                                       if target == wanted or target in wanted.parents)
                        # A replacement's pathname now names a different inode.
                        for old_watch, old_path in tuple(self.watches.items()):
                            if old_path == target and old_path not in self.directories:
                                _ = self._remove_watch(self.descriptor, old_watch)
                                _ = self.watches.pop(old_watch, None)
                    else:
                        changed.add(path)
                    if mask & _IN_INVALIDATED:
                        _ = self.watches.pop(watch, None)
                self._arm()
            if changed:
                return StateFilesChanged(frozenset(changed))
            if connection in ready:
                return SocketReady.READY
        return WaitDeadlineExpired.EXPIRED

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(frozen=True)
class WatchedFileVersion:
    """The identity and content-write timestamp of a named state file."""

    inode: int
    size: int
    modified_ns: int


class MissingWatchedFile(Enum):
    MISSING = "missing"


def _watched_file_version(path: Path) -> WatchedFileVersion | MissingWatchedFile:
    try:
        record = path.stat()
        return WatchedFileVersion(record.st_ino, record.st_size, record.st_mtime_ns)
    except FileNotFoundError:
        return MissingWatchedFile.MISSING


@final
class KqueueFileChanges:
    """Filter parent vnode events through the versions of the watched files."""

    def __init__(self, paths: set[Path]) -> None:
        self.paths: set[Path] = paths
        self.queue: select.kqueue = select.kqueue()
        self.watches: dict[Path, int] = {}
        self.versions: dict[Path, WatchedFileVersion | MissingWatchedFile] = {
            path: _watched_file_version(path) for path in paths}
        try:
            self._arm()
        except BaseException:
            self.close()
            raise

    def _arm(self) -> None:
        wanted = set(self.paths)
        for path in self.paths:
            directory = path.parent
            while not directory.is_dir():
                directory = directory.parent
            wanted.add(directory)
        for path in wanted - self.watches.keys():
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            self.watches[path] = descriptor
            flags = select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
            if path in self.paths and path.name != SERVER_FILE:
                flags |= select.KQ_NOTE_ATTRIB
            _ = self.queue.control([select.kevent(descriptor, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=flags)], 0)

    def wait(self, connection: socket.socket, deadline: float) -> FileWaitResult:
        descriptor = connection.fileno()
        _ = self.queue.control([select.kevent(descriptor, filter=select.KQ_FILTER_READ,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR)], 0)
        try:
            while time.monotonic() < deadline:
                events = self.queue.control([], 64, max(0.0, deadline - time.monotonic()))
                changed: set[Path] = set()
                if any(event.filter == select.KQ_FILTER_VNODE for event in events):
                    for path in self.paths:
                        version = _watched_file_version(path)
                        if version != self.versions[path]:
                            self.versions[path] = version
                            changed.add(path)
                            old_descriptor = self.watches.pop(path, -1)
                            if old_descriptor >= 0:
                                os.close(old_descriptor)
                    for event in events:
                        if (event.filter == select.KQ_FILTER_VNODE
                                and event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)):
                            for path, watched_descriptor in tuple(self.watches.items()):
                                if watched_descriptor == event.ident:
                                    os.close(self.watches.pop(path))
                    self._arm()
                if changed:
                    return StateFilesChanged(frozenset(changed))
                if any(event.filter == select.KQ_FILTER_READ and event.ident == descriptor for event in events):
                    return SocketReady.READY
            return WaitDeadlineExpired.EXPIRED
        finally:
            # No READ registration for an earlier connection may wake a moved worker.
            with contextlib.suppress(OSError):
                _ = self.queue.control([select.kevent(descriptor, filter=select.KQ_FILTER_READ,
                    flags=select.KQ_EV_DELETE)], 0)

    def close(self) -> None:
        self.queue.close()
        for descriptor in self.watches.values():
            os.close(descriptor)


@final
class FollowEvents:
    """One follower's relevant file changes, multiplexed with its current socket."""

    def __init__(self, session_dir: str, name: str) -> None:
        from scripts.accounts.worker_names import accounts_config_file
        from scripts.accounts.role_map import groups_file

        folder = Path(session_dir)
        paths = {folder / filename for filename in (
            ROSTER_FILE, SERVER_FILE, plan_history.STATE_FILENAME,
            f"{name}.pending.json", f"{name}.end", f"{name}.account-move.json",
        )}
        for run in worker_run_history(session_dir, name)[1:]:
            paths.add(Path(run) / f"{name}.pending.json")
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
            "thread_id", "turn_id", "status", "port", "launcher_pid", "continuation_owed", "adopted_to")}

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


@final
class WebSocket:
    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = CONNECT_TIMEOUT_SECS) -> None:
        self._sock: socket.socket = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self._sock.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buffer = b""
        expires = time.monotonic() + timeout
        while b"\r\n\r\n" not in buffer:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("app-server handshake timed out")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("app-server closed during handshake")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            detail = head.decode(errors="replace")[:200]
            raise ConnectionError(f"handshake refused: {detail}")
        self._buffer: bytearray = bytearray(rest)

    def settimeout(self, seconds: float) -> None:
        self._sock.settimeout(seconds)

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
        self._sock.sendall(bytes(frame))

    def _fill(self, count: int, wake: FollowEvents | SocketOnlyWait) -> None:
        expires = time.monotonic() + (self._sock.gettimeout() or CALL_TIMEOUT_SECS)
        while len(self._buffer) < count:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("app-server frame timed out")
            self._sock.settimeout(remaining)
            if isinstance(wake, FollowEvents):
                result = wake.wait(self._sock, expires)
                if isinstance(result, StateFilesChanged):
                    raise SessionStateChanged()
                if result is WaitDeadlineExpired.EXPIRED:
                    raise TimeoutError("app-server frame timed out")
            chunk = self._sock.recv(262144)
            if not chunk:
                raise ConnectionError("app-server closed the connection")
            self._buffer.extend(chunk)

    @property
    def has_pending_frames(self) -> bool:
        """Bytes already read or ready on the socket must precede lifecycle readbacks."""
        return bool(self._buffer) or bool(select.select([self._sock], [], [], 0)[0])

    def wait_for_activity(self, wake: FollowEvents, deadline: float) -> None:
        """Wake for unread bytes or file changes without consuming a notice."""
        if not self._buffer:
            _ = wake.wait(self._sock, deadline)

    def recv(self, wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT) -> RpcMessage | None:
        """Read one frame; None skips ignored deltas and non-JSON frames."""
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

    def __init__(self, port: int, name: str, deadline: float | None = None) -> None:
        self.port: int = port
        self.name: str = name
        self.session_dir: str = ""
        self.serving: CodexServingState = NotBoundCodexServer()
        self.turn_submitted_at: float = time.time()
        # Every call ends by this wall-clock time, whatever timeout it asks for.
        self._deadline: float | None = deadline
        self._ws: WebSocket = WebSocket(port, timeout=self._bounded(CONNECT_TIMEOUT_SECS))
        self._name: str = name
        self._counter: int = 0
        self._pending: list[RpcMessage] = []
        self.wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT
        # experimentalApi is what unlocks thread/queue/*; without it those calls
        # fail with a bare -32600 that never mentions a capability.
        _ = self.call(
            "initialize",
            {
                "clientInfo": {"name": name, "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )

    def call(
        self, method: str, params: dict[str, object], timeout: float = CALL_TIMEOUT_SECS
    ) -> RpcMessage:
        if self._deadline is not None and time.time() >= self._deadline:
            return {"error": {"message": f"operation deadline reached before {method}"}}
        self._counter += 1
        request_id = f"{self._name}-{self._counter}"
        self._ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        )
        deadline = time.time() + self._bounded(timeout)
        expires = time.monotonic() + max(0.0, deadline - time.time())
        while time.monotonic() < expires:
            for index, message in enumerate(self._pending):
                if message.get("id") == request_id:
                    return self._pending.pop(index)
            frame = self._read(time.time() + max(0.0, expires - time.monotonic()))
            if frame is None:
                continue
            if frame.get("id") == request_id:
                return frame
            self._pending.append(frame)
        return {"error": {"message": f"timed out waiting for {method}"}}

    def _bounded(self, timeout: float) -> float:
        if self._deadline is None:
            return timeout
        return max(0.1, min(timeout, self._deadline - time.time()))

    def _read(self, deadline: float, wake: FollowEvents | SocketOnlyWait = SOCKET_ONLY_WAIT) -> RpcMessage | None:
        expires = time.monotonic() + max(0.0, deadline - time.time())
        while time.monotonic() < expires:
            self._ws.settimeout(max(0.001, expires - time.monotonic()))
            try:
                frame = self._ws.recv(wake)
            except (TimeoutError, SessionStateChanged):
                return None
            if frame is not None:
                return frame
        return None

    @property
    def has_pending_frames(self) -> bool:
        """Whether an RPC read buffered notices that the stream still must handle."""
        return bool(self._pending) or self._ws.has_pending_frames

    def next_frame(self, deadline: float) -> RpcMessage | None:
        """Next frame of any kind, or None once `deadline` passes."""
        if self._pending:
            return self._pending.pop(0)
        if time.time() >= deadline:
            return None
        return self._read(deadline, self.wake)

    def push_back(self, frame: RpcMessage) -> None:
        """Return a frame to the head of the stream for the next reader."""
        self._pending.insert(0, frame)

    def wait_for_activity(self, wake: FollowEvents, deadline: float) -> None:
        """Wait for new input, leaving RPC-buffered notices for the stream.

        Pending notices deliberately do not wake this wait: the next state RPC
        reads new socket bytes and preserves their order behind those notices.
        """
        self._ws.wait_for_activity(wake, deadline)

    def close(self) -> None:
        self._ws.close()


def _require(message: RpcMessage, what: str) -> dict[str, object]:
    error = message.get("error")
    if error:
        raise SystemExit(f"codex_mesh: {what} failed: {json.dumps(error)[:300]}")
    return _as_dict(message.get("result"))


# ---------------------------------------------------------------------------
# Session state. The workers write the roster concurrently, so every
# read-modify-write takes an exclusive lock on the file itself.
# ---------------------------------------------------------------------------


def _session_path(session_dir: str, filename: str) -> Path:
    return Path(session_dir) / filename


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            text = handle.read()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return {}
    return _as_dict(_loads(text))


def _post_board_status(session_dir: str, name: str, message: str) -> None:
    """Best-effort lifecycle narration under the worker's board slot."""
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    slot = _as_str(record.get("role"))
    if not slot:
        return
    message = f"{unit_workers.shown_name(Path(session_dir), name)}: {message}"
    with contextlib.suppress(OSError):
        _ = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "direct/board.sh"),
                            "post", session_dir, slot, "status", message], capture_output=True, text=True)


@dataclass(frozen=True)
class RosterRevision:
    value: int


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
class RosterAbsent:
    pass


@dataclass(frozen=True)
class RosterEndedOnThread:
    thread_id: str


@dataclass(frozen=True)
class RosterUnchanged:
    thread_id: str
    revision: RosterRevision


ExpectedRoster = RosterOnThread | RosterAbsent | RosterEndedOnThread | RosterUnchanged


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


def _migrate_legacy_reviewers(roster: dict[str, object]) -> dict[str, object]:
    for name, value in roster.items():
        entry = _as_dict(value)
        if "access_policy" not in entry and not entry.get("held_design_id"):
            if isinstance(roster_access_policy(entry), ReadOnlyReviewer):
                entry["access_policy"] = {"kind": "ReadOnlyReviewer"}
                roster[name] = entry
    return roster


def _load_roster_for_write(handle: TextIO) -> dict[str, object]:
    return _migrate_legacy_reviewers(_as_dict(_loads(handle.read())))


def _write_roster_record(handle: TextIO, roster: dict[str, object], name: str,
                        record: ThreadRecord) -> None:
    previous = _as_dict(roster.get(name))
    # A launcher on the giving run can observe a late RPC after adoption.
    # Its thread id still matches, but it no longer owns any lifecycle write.
    if previous.get("adopted_to"):
        return
    stored = dict(record)
    if previous.get("thread_id") == record["thread_id"]:
        for key in ("held_design_id", "held_role", "access_policy", "transferring_role", "role_redirect", "model", "effort",
                    "adopted_from", "adoption_id", "unit_ledger"):
            if key in previous and key not in stored:
                stored[key] = previous[key]
    if "continuation_owed" not in record and previous.get("thread_id") == record["thread_id"]:
        owed = previous.get("continuation_owed")
        if isinstance(owed, bool):
            stored["continuation_owed"] = owed
    size = _context_size_from_record(record if "context_tokens" in record else previous)
    role = record.get("role", previous.get("role"))
    if isinstance(role, str) and role in WORKER_ROLES:
        stored["role"] = role
    task = record.get("task", previous.get("task"))
    if isinstance(task, str) and task:
        stored["task"] = task
    instructions_file = record.get("instructions_file")
    if instructions_file is None and previous.get("thread_id") == record["thread_id"]:
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
        previous.get("thread_id") == record["thread_id"]
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
    roster[name] = stored
    _ = handle.seek(0)
    _ = handle.truncate()
    _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
    handle.flush()


def _update_roster(session_dir: str, name: str, record: ThreadRecord) -> None:
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        _write_roster_record(handle, roster, name, record)


def _update_roster_if_expected(session_dir: str, name: str, record: ThreadRecord,
                              expected: ExpectedRoster) -> bool:
    """Compare the name's owner and write while holding the same roster lock."""
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        previous = _as_dict(roster.get(name))
        if isinstance(expected, RosterAbsent):
            matches = name not in roster
        else:
            matches = previous.get("thread_id") == expected.thread_id
            if previous.get("adopted_to"):
                matches = False
            if isinstance(expected, RosterEndedOnThread):
                matches = matches and previous.get("status") == "ended"
            elif isinstance(expected, RosterUnchanged):
                matches = matches and _roster_revision(previous) == expected.revision
        if not matches:
            return False
        _write_roster_record(handle, roster, name, record)
        return True


def _set_roster_assignment(session_dir: str, name: str, role: WorkerRole, task: str) -> None:
    """Record the work assigned to an existing worker without changing its state."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if entry and not entry.get("adopted_to"):
            entry["role"] = role
            if entry.get("unit_ledger") and not entry.get("held_design_id") and not entry.get("instructions_file"):
                entry["access_policy"] = {"kind": "ReadOnlyReviewer" if role == "review" else "WorkerLaunchPolicy"}
            if task:
                entry["task"] = task
            _advance_roster_revision(entry)
            roster[name] = entry
            _ = handle.seek(0)
            _ = handle.truncate()
            _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
            handle.flush()


def _mark_all_roster_ended(session_dir: str) -> None:
    """Make every worker in this run terminal while retaining its metadata."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        for name, value in roster.items():
            entry = _as_dict(value)
            if entry and not entry.get("adopted_to"):
                entry["status"] = "ended"
                _advance_roster_revision(entry)
                roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()


def _lookup(session_dir: str, name: str) -> ThreadRecord:
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    entry = roster.get(name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(roster)) or "(none)"
        raise SystemExit(f"codex_mesh: no worker named '{name}'. Known: {known}")
    _role_fence(_as_dict(cast(object, entry)))
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


def _role_fence(record: dict[str, object]) -> None:
    from scripts.astra import design_roles
    redirected = design_roles.transfer_route(_as_str(record.get("thread_id")), _as_str(record.get("held_design_id")))
    if record.get("transferring_role") or record.get("role_redirect") or redirected:
        raise SystemExit("codex_mesh: TransferringRole: use the destination holder route")


@contextlib.contextmanager
def _design_claim_lock(session_dir: str, name: str) -> Generator[None]:
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    _role_fence(record)
    design_id = _as_str(record.get("held_design_id"))
    if not design_id:
        yield
        return
    from scripts.astra import design_roles
    directory = design_roles.design_directory(design_id)
    with (directory / "lock").open("a", encoding="utf-8") as design_lock:
        fcntl.flock(design_lock, fcntl.LOCK_EX)
        with _pending_file(session_dir, name):
            yield


def _validate_held_claim(name: str, entry: dict[str, object], thread_id: str) -> bool:
    _role_fence(entry)
    design_id = _as_str(entry.get("held_design_id"))
    if not design_id:
        return True
    from scripts.astra import design_roles
    design = design_roles.read_design(design_id)
    role = next((role for role in design.roles if role.name == name), None)
    reach = design_roles.resolve_holder(design.holder)
    generation = design.placement.generation
    if (role is None or isinstance(role.lifecycle, (design_roles.RoleNotOpened, design_roles.RoleEnded, design_roles.RoleNeedsRecovery))
            or role.lifecycle.thread_id != thread_id or not isinstance(reach, design_roles.HolderReach)
            or reach.session_id != os.environ.get("CLAUDE_CODE_SESSION_ID")
            or entry.get("authorized_generation") != generation
            or entry.get("authorized_holder") != reach.session_id
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
    params: dict[str, object] = {"threadId": thread_id}
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
    return not isinstance(state, (InvalidThread, AdoptedThread)) and state.thread_id == thread_id


def _claim_follow(
    session_dir: str, name: str, thread_id: str, launcher_pid: int, role: WorkerRole,
    lens: agents_log.Lens | None = None,
) -> bool:
    """Take a finished roster entry before another follow can take it."""
    path = _session_path(session_dir, ROSTER_FILE)
    with _design_claim_lock(session_dir, name), path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
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
        previous_role = _roster_role(entry.get("role"))
        claimed = dict(entry)
        unit_workers.clear_allocation(claimed)
        _ = claimed.pop("turn_id", None)
        _ = claimed.pop("paused_from", None)
        _ = claimed.pop("paused_turn", None)
        _ = claimed.pop("previous_role", None)
        if isinstance(previous_role, AssignedWorkerRole):
            claimed["previous_role"] = previous_role.role
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
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        _record_assignment(thread_id, role, lens)
        return True


def _restore_follow_claim(session_dir: str, name: str, launcher_pid: int,
                          live_turn: str = "", *, finished: bool = False,
                          expected_thread_id: str = "") -> None:
    """Release only this launcher's unstarted claim; retain a peer's live turn."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        status = entry.get("status")
        if entry.get("adopted_to"):
            return
        if expected_thread_id and entry.get("thread_id") != expected_thread_id:
            return
        if entry.get("launcher_pid") != launcher_pid or not (
            status == "starting" or (finished and status == "running")
        ):
            return
        thread_id = _as_str(entry.get("thread_id"))
        previous_status = entry.get("previous_status")
        previous_role = _roster_role(entry.get("previous_role"))
        restored = dict(entry)
        _advance_roster_revision(restored)
        if isinstance(previous_role, AssignedWorkerRole):
            restored["role"] = previous_role.role
        else:
            _ = restored.pop("role", None)
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
            _ = restored.pop("previous_role", None)
            _ = restored.pop("turn_id", None)
            _ = restored.pop("launcher_pid", None)
            _ = restored.pop("port", None)
            restored.update({
                "thread_id": thread_id,
                "status": previous_status if previous_status in FOLLOWABLE_STATES else "failed",
            })
            roster[name] = restored
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        if isinstance(previous_role, AssignedWorkerRole) and entry.get("role") != previous_role.role:
            _record_assignment(thread_id, previous_role.role, None)


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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return _bound_port(probe)


def _server_availability(session_dir: str) -> ServerAvailability:
    """The running app-server or the need to restart it.

    `ensure_server` would start one; the verbs that only ever wind things down
    must not, or ending a worker whose server already died would leave a
    fresh server behind for nothing.
    """
    record = _read_json_object(_session_path(session_dir, SERVER_FILE))
    port = record.get("port")
    pid = record.get("pid")
    if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
        return LiveServer(port)
    return ServerRestartRequired()


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
    thread_id = record["thread_id"]
    servers = _run_servers(session_dir)
    for candidate in servers:
        if _server_holds(candidate, thread_id):
            return candidate
    current = _record_port(session_dir)
    if current in servers:
        return current
    raise SystemExit(f"codex_mesh: thread {thread_id}'s launcher {pid} is running, but no running app-server "
                     + "of this run holds it; nothing was started and the message was not delivered")


def _recorded_run_servers(session_dir: str) -> list[dict[str, object]]:
    """Retired process records followed by the run's current process record."""
    stored = _read_json_object(_session_path(session_dir, RETIRED_FILE)).get("servers")
    records = [_as_dict(record) for record in (cast("list[object]", stored) if isinstance(stored, list) else [])]
    records.append(_read_json_object(_session_path(session_dir, SERVER_FILE)))
    return records


def _run_servers(session_dir: str) -> list[int]:
    """Ports of the run's servers still running: those it retired, oldest first, then its current one."""
    ports: list[int] = []
    for record in _recorded_run_servers(session_dir):
        port, pid = record.get("port"), record.get("pid")
        if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid) and port not in ports:
            ports.append(port)
    return ports


def _loaded_thread_ids(client: Client, deadline: float) -> set[str]:
    threads: set[str] = set()
    cursor = ""
    seen: set[str] = set()
    while time.monotonic() < deadline:
        params: dict[str, object] = {"cursor": cursor} if cursor else {}
        listed = _require(client.call("thread/loaded/list", params,
                                      timeout=max(0.01, deadline - time.monotonic())), "thread/loaded/list")
        data = listed.get("data")
        if not isinstance(data, list):
            raise SystemExit("server returned no loaded-thread list")
        for item in cast(list[object], data):
            if not isinstance(item, str) or not item:
                raise SystemExit("server returned an invalid loaded-thread id")
            threads.add(item)
        next_cursor = listed.get("nextCursor")
        if next_cursor is None or next_cursor == "":
            return threads
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise SystemExit("server returned an invalid loaded-thread cursor")
        cursor = next_cursor
        seen.add(cursor)
    raise SystemExit("loaded-thread list timed out")


def _server_holds(port: int, thread_id: str) -> bool:
    try:
        client = Client(port, f"worker-lookup-{os.getpid()}")
    except (ConnectionError, OSError, SystemExit):
        return False
    try:
        return thread_id in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS)
    except (ConnectionError, OSError, SystemExit):
        return False
    finally:
        client.close()


def _note_worker_port(session_dir: str, name: str, launcher_pid: int, port: int) -> None:
    """Record the server a follow-up launcher attached to, while its claim is still its own."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if entry.get("adopted_to") or entry.get("launcher_pid") != launcher_pid:
            return
        entry["port"] = port
        _advance_roster_revision(entry)
        roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()


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
    temporary = path.with_name(path.name + f".{secrets.token_hex(8)}.tmp")
    try:
        _ = temporary.write_text(json.dumps(value), encoding="utf-8")
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_account_move(session_dir: str, name: str, thread_id: str) -> AccountMoveMarker | NoAccountMove:
    record = _read_json_object(_account_move_path(session_dir, name))
    thread, turn, label, move_id = (record.get(key) for key in ("thread_id", "turn_id", "label", "move_id"))
    if (thread == thread_id and isinstance(thread, str) and thread
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
def _pending_file(session_dir: str, name: str, *, blocking: bool = True) -> Generator[TextIO]:
    path = _session_path(session_dir, f"{name}.pending.json")
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pending_for_thread(handle: TextIO, thread_id: str) -> list[str]:
    _ = handle.seek(0)
    stored = _as_dict(_loads(handle.read()))
    if stored.get("thread_id") != thread_id:
        return []
    messages = stored.get("messages")
    return [item for item in cast("list[object]", messages) if isinstance(item, str)] if isinstance(messages, list) else []


class WorkerPendingMessages(TypedDict):
    adoption_id: NotRequired[str]
    thread_id: str
    messages: list[str]
    accepting_messages: bool


def _messages_closed(handle: TextIO, thread_id: str) -> bool:
    _ = handle.seek(0)
    stored = _as_dict(_loads(handle.read()))
    return stored.get("thread_id") == thread_id and stored.get("accepting_messages") is False


def _write_pending(handle: TextIO, thread_id: str, messages: list[str], *, accepting_messages: bool = True) -> None:
    stored: WorkerPendingMessages = {
        "thread_id": thread_id, "messages": messages, "accepting_messages": accepting_messages,
    }
    _ = handle.seek(0)
    previous = _as_dict(_loads(handle.read()))
    adoption_id = _as_str(previous.get("adoption_id"))
    if previous.get("thread_id") == thread_id and adoption_id:
        stored["adoption_id"] = adoption_id
    if previous == stored:
        return
    _ = handle.seek(0)
    _ = handle.truncate()
    _ = handle.write(json.dumps(stored))
    handle.flush()


@dataclass(frozen=True)
class FreshDetachedRunScan:
    """A command outside a follower requests a new, uncached run observation."""


FRESH_DETACHED_RUN_SCAN = FreshDetachedRunScan()


def _close_messages_if_empty(
    session_dir: str, name: str, thread_id: str,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> bool:
    """Exclude senders while claiming an empty inbox and closing its launcher."""
    with _pending_file(session_dir, name) as handle:
        current = _run_watch(session_dir, name, runs).current()
        if _pending_for_thread(handle, thread_id) or current.live or current.dead:
            return False
        _write_pending(handle, thread_id, [], accepting_messages=False)
        return True


def _close_messages_and_keep_pending(session_dir: str, name: str, thread_id: str) -> int:
    """Close the launcher's inbox while keeping its parked messages for the next launch."""
    with _pending_file(session_dir, name) as handle:
        messages = _pending_for_thread(handle, thread_id)
        _write_pending(handle, thread_id, messages, accepting_messages=False)
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


@dataclass
class DetachedRunWatch:
    """One launcher's run liveness, retaining immutable finished record names."""

    session_dir: str
    name: str
    finished: set[str] = field(default_factory=set)
    next_check: float = 0.0
    last_observed: LastObservedDetachedRuns = field(default_factory=LastObservedDetachedRuns)
    run_history: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_history:
            self.run_history = worker_run_history(self.session_dir, self.name)

    def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self.next_check:
            return
        self.next_check = now + DETACHED_RUN_CHECK_SECS
        live = dead = False
        wall_now = time.time()
        paths = (path for run in self.run_history
                 for path in _session_path(run, DETACHED_RUNS_DIR).glob("*.json"))
        for path in paths:
            if str(path) in self.finished or (path.parent.parent == Path(self.session_dir) and path.name in self.finished):
                continue
            record = _read_json_object(path)
            if record.get("status") == "finished":
                self.finished.add(path.name if path.parent.parent == Path(self.session_dir) else str(path))
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
            for run in self.run_history:
                _ = subprocess.run([sys.executable, str(MESH_RUNS_SCRIPT), "reap", "--session-dir", run,
                                    "--worker", self.name], stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=DEAD_RUN_END_TIMEOUT_SECS, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def _run_watch(session_dir: str, name: str, runs: DetachedRunWatch | FreshDetachedRunScan) -> DetachedRunWatch:
    return runs if isinstance(runs, DetachedRunWatch) else DetachedRunWatch(session_dir, name)


def _waiting_on_detached_run(
    session_dir: str, name: str, thread_id: str,
    runs: DetachedRunWatch | FreshDetachedRunScan = FRESH_DETACHED_RUN_SCAN,
) -> bool:
    """A live run with nothing held yet: the launcher stays attached and makes no calls."""
    with _pending_file(session_dir, name) as handle:
        held = bool(_pending_for_thread(handle, thread_id))
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


HoldOutcome = HeldForLauncher | HeldForNextDispatch | NoLauncherAttached | WorkerEnded


@dataclass(frozen=True)
class MessageHandedOff:
    to_run: str


def hold_message(session_dir: str, name: str, text: str, *, without_launcher: bool) -> HoldOutcome:
    visited: set[str] = set()
    while session_dir not in visited:
        visited.add(session_dir)
        entry = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        target = _as_str(entry.get("adopted_to"))
        if not target:
            held = _hold_owned_message(session_dir, name, text, without_launcher=without_launcher)
            if not isinstance(held, MessageHandedOff):
                return held
            target = held.to_run
        session_dir = target
    return WorkerEnded(f"{name}: adoption route contains a cycle")


def _hold_owned_message(session_dir: str, name: str, text: str, *, without_launcher: bool) -> HoldOutcome | MessageHandedOff:
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
        thread_id = _as_str(entry.get("thread_id"))
        status = _as_str(entry.get("status"))
        if not entry or not thread_id:
            return WorkerEnded(f"{name} is not on the roster")
        if entry.get("adopted_to"):
            return MessageHandedOff(_as_str(entry.get("adopted_to")))
        if status == "ended":
            return WorkerEnded(f"{name} is ended")
        closed = _messages_closed(handle, thread_id)
        launcher_pid = _as_int(entry.get("launcher_pid"))
        attached = status in LAUNCHER_ATTACHED_STATUSES and _pid_alive(launcher_pid) and not closed
        if not attached and not without_launcher:
            return NoLauncherAttached(thread_id, status)
        messages = _pending_for_thread(handle, thread_id)
        messages.append(text)
        _write_pending(handle, thread_id, messages, accepting_messages=not closed)
        return HeldForLauncher(thread_id) if attached else HeldForNextDispatch(thread_id)


def _add_pending(session_dir: str, name: str, thread_id: str, message: str) -> None:
    with _pending_file(session_dir, name) as handle:
        messages = _pending_for_thread(handle, thread_id)
        messages.append(message)
        _write_pending(handle, thread_id, messages)


def _move_queue_to_pending(client: Client, thread_id: str, park: Callable[[str], None]) -> None:
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
                park("\n".join(messages))
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
    answer goes there, and the summary file is written only when the worker
    left it empty: the launcher truncates it at launch and the worker fills
    it as its last act, so a non-empty file is the worker's own summary and
    the last chat reply must not replace it.
    """
    if reply_path is None:
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")
        return
    _record_reply(reply_path, name, index, final_answer)
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")


@contextlib.contextmanager
def _server_lock(session_dir: str) -> Generator[None]:
    """Serialize starting and dropping this session's app-server.

    The workers of a phase launch in one message and reach `ensure_server`
    within the same second. Unlocked, each reads no record, each starts a server
    and each writes the file: all but one are then orphaned, listening and
    unreachable, and a retry that started a fresh server for every failed worker
    would multiply them.
    """
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    with _session_path(session_dir, LOCK_FILE).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _retire_server(session_dir: str, port: int) -> bool:
    """Drop the recorded server so the next `ensure_server` starts a fresh one.

    Only when the record still names the server that failed: a peer worker racing
    the same recovery may already have replaced it, and dropping its new server
    would restart the cycle it just ended. The process is left running -- see
    RETIRED_FILE -- and `stop` reaps it with the rest at the end of the run.

    The roster is deliberately untouched. Its entries for other names may point
    at threads that are live on a server this worker knows nothing about, and this
    worker rewrites its own entry when it re-attaches.
    """
    with _server_lock(session_dir):
        record = _read_json_object(_session_path(session_dir, SERVER_FILE))
        if record.get("port") != port:
            return False
        _retire_record(session_dir, record)
        return True


def _retire_record(session_dir: str, record: dict[str, object]) -> None:
    """Move the server record onto RETIRED_FILE. The caller holds the server lock."""
    retired_path = _session_path(session_dir, RETIRED_FILE)
    stored = _read_json_object(retired_path).get("servers")
    servers: list[object] = cast("list[object]", stored) if isinstance(stored, list) else []
    servers.append(record)
    _ = retired_path.write_text(json.dumps({"servers": servers}, indent=2), encoding="utf-8")
    _session_path(session_dir, SERVER_FILE).unlink(missing_ok=True)


def _record_started_server(session_dir: str, served: ServerRecord) -> None:
    """Install a new current record without orphaning the server it replaces.

    The caller holds the server lock. A live prior server stays reachable through
    RETIRED_FILE, whose watcher stops it once its conversations are idle. A prior
    record whose process is already gone needs only to be removed.
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
            path.unlink(missing_ok=True)
    _ = path.write_text(json.dumps(served, indent=2), encoding="utf-8")


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
    """Leave a sleeping shell behind that has the server stop itself once nothing uses it.

    The server is detached so it outlives each worker, and only a run's end step stops one; a
    run folder that never reaches that step kept its server for good. The shell wakes every
    WATCH_WAKE_SECS, and only when the record has gone SERVER_IDLE_SECS without a touch does it
    run `idle-stop`, which stops the server or touches the record. A server RETIRED_FILE lists is
    asked about at every wake instead: the run's newer server keeps the record fresh. Its cost is a shell and a
    `sleep`, under 1 MB of their own memory, one `find` per wake, and one short Python run about
    twice an hour while the server is in use. It leaves with the server. Its inherited lock
    prevents repeated retirement passes from starting another watcher for the same server.
    """
    script = (
        'while kill -0 "$1" 2>/dev/null; do sleep "$5"; '
        + '[ -n "$(find "$3/$6" -mmin "-$7" 2>/dev/null)" ] '
        + '&& ! grep -Eq "\\"pid\\": $1([^0-9]|\\$)" "$3/$8" 2>/dev/null && continue; '
        + '"$4" "$0" idle-stop --session-dir "$3" --pid "$1" --port "$2" && exit 0; done'
    )
    with _session_path(session_dir, f"mesh-watcher-{pid}-{port}.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ExistingServerWatcher()
        return subprocess.Popen(
            ["sh", "-c", script, str(Path(__file__).resolve()), str(pid), str(port),
             str(Path(session_dir).resolve()), sys.executable, str(WATCH_WAKE_SECS), SERVER_FILE,
             str(int(SERVER_IDLE_SECS // 60)), RETIRED_FILE],
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
    reach = design_roles.resolve_holder(design.holder)
    if not isinstance(reach, design_roles.HolderReach):
        raise SystemExit("RoleNeedsRecovery: holder session is unavailable")
    context.update({"generation": design.placement.generation, "holder_session": reach.session_id,
                    "binding": {"kind": type(design.binding).__name__, **asdict(design.binding)},
                    "cwd": design.repository})
    return _as_dict(_loads(json.dumps(context)))


def _release_held_server(session_dir: str) -> None:
    """Park every queue before releasing a proven-idle server. Keep evidence on uncertainty."""
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    record = _read_json_object(_session_path(session_dir, SERVER_FILE))
    if record.get("codex_home"):
        home_path = _session_path(session_dir, "serving_home.json")
        temporary = home_path.with_name(home_path.name + f".{secrets.token_hex(8)}.tmp")
        try:
            _ = temporary.write_text(json.dumps({"codex_home": record["codex_home"]}), encoding="utf-8")
            _ = temporary.replace(home_path)
        finally:
            temporary.unlink(missing_ok=True)
    for server in _recorded_run_servers(session_dir):
        port, pid = _as_int(server.get("port")), _as_int(server.get("pid"))
        if not _pid_alive(pid):
            continue
        for name, value in roster.items():
            entry = _as_dict(value)
            _role_fence(entry)
            thread_id = _as_str(entry.get("thread_id"))
            if not thread_id:
                continue
            client = Client(port, "held-environment-release")
            try:
                with _pending_file(session_dir, name) as pending:
                    def park(message: str) -> None:
                        _write_pending(pending, thread_id, [*_pending_for_thread(pending, thread_id), message])
                    state = _read_live_turn(client, thread_id)
                    if isinstance(state, ThreadStateUnknown):
                        if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                            continue
                    _move_queue_to_pending(client, thread_id, park)
                    if not isinstance(state, ThreadIdle):
                        raise SystemExit("RoleNeedsRecovery: old server turn is not proven idle")
                    released = _release_worker_thread(client, thread_id, session_dir)
                    if isinstance(released, WorkerThreadStateUnknown):
                        raise SystemExit("RoleNeedsRecovery: " + released.reason)
            finally:
                client.close()
        with _server_lock(session_dir):
            if not _reap(pid) and _pid_alive(pid):
                raise SystemExit("RoleNeedsRecovery: old server release is uncertain")
            current = _read_json_object(_session_path(session_dir, SERVER_FILE))
            if current.get("pid") == pid:
                _session_path(session_dir, SERVER_FILE).unlink(missing_ok=True)


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
    except (OSError, ValueError) as error:
        return DevShellProbeUnavailable(str(error))


def _remember_server_dev_shell(session_dir: str, dev_shell: ServerDevShell) -> None:
    """Keep the last successful shell across idle-stop, separately from the live record."""
    path = _session_path(session_dir, DEV_SHELL_FILE)
    if isinstance(dev_shell, WorktreeDevShell):
        _ = path.write_text(json.dumps({"worktree": str(dev_shell.worktree)}), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _remember_failed_dev_shell(session_dir: str, dev_shell: WorktreeDevShell) -> None:
    """Retry failed evaluations after an input change or a thirty-minute cooldown."""
    failure = RememberedDevShellFailure(dev_shell.worktree, dev_shell.input_revision, time.time() + 30 * 60)
    _ = _session_path(session_dir, FAILED_DEV_SHELL_FILE).write_text(json.dumps({
        "worktree": str(failure.worktree), "input_revision": failure.input_revision,
        "retry_at": failure.retry_at,
    }), encoding="utf-8")


def ensure_server(session_dir: str, home: Path | None = None, cwd: str | None = None) -> tuple[int, bool]:
    """This session's app-server port, and whether this call is what started it.

    A caller that started the server just proved the provider was reachable
    through a process nothing else has touched, so a failure against it is the
    provider's answer. A caller that attached to one already running has proved
    nothing of the kind -- see _retry_warranted.
    """
    selected_home = workers_home() if home is None else home
    context = _design_context(session_dir)
    previous = _read_json_object(_session_path(session_dir, SERVER_FILE))
    if context and previous:
        pid = _as_int(previous.get("pid"))
        previous_home = server_record_home(previous)
        if (previous.get("design_context") != context or previous_home != selected_home
                or previous.get("auto_compact_tokens") != AUTO_COMPACT_TOKENS
                or (_pid_alive(pid) and _holds_old_sign_in(pid, previous_home))):
            _release_held_server(session_dir)
    with _server_lock(session_dir):
        # Read failures under the same lock as launch: another caller may have
        # just fallen back for this revision while this caller was acquiring it.
        dev_shell = _server_dev_shell(session_dir, cwd)
        dev_shell_worktree = str(dev_shell.worktree) if isinstance(dev_shell, WorktreeDevShell) else ""
        path = _session_path(session_dir, SERVER_FILE)
        record = _read_json_object(path)
        port = record.get("port")
        pid = record.get("pid")
        if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
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
        if cwd is None:
            remembered = _as_str(_read_json_object(_session_path(session_dir, DEV_SHELL_FILE)).get("worktree"))
            # Also migrate a shell recorded before the persistent preference existed.
            remembered = remembered or _as_str(record.get("dev_shell_worktree"))
            dev_shell = _server_dev_shell(session_dir, remembered or None)
        if isinstance(dev_shell, DevShellProbeUnavailable):
            _log_server(session_dir, f"dev-shell probe unavailable: {dev_shell.reason}; starting with caller environment")
        if isinstance(dev_shell, WorktreeDevShell):
            return _start_server(session_dir, selected_home, dev_shell), True
        if isinstance(dev_shell, InheritedServerEnvironment):
            _remember_server_dev_shell(session_dir, dev_shell)
        return _start_server(session_dir, selected_home), True


def _server_environment(session_dir: str, workers_home: Path) -> dict[str, str]:
    """The app-server's environment: the caller's, with the workers home and, for a unit run, the run.

    A worker's hooks and verify.sh runs read the run from UNIT_RUN_DIR. The caller's copy
    cannot be trusted: implement.sh exports it only after its claim step, whose `compact` call may
    already have started this server. A run folder is known here, so the server names it itself. A
    review pool's mesh directory, or any other mesh directory, is not a run: one pool server
    serves the turns of many units, so it keeps the caller's environment unchanged.
    """
    environment = {**os.environ, "CODEX_HOME": str(workers_home)}
    context = _design_context(session_dir)
    if context:
        for key in tuple(environment):
            if key.startswith(("UNIT_", "PLAN_DELEGATE_")) or key in ("CLAUDE_CODE_SESSION_ID", "HELD_DESIGN_ID", "DESIGN_BINDING_GENERATION"):
                del environment[key]
        environment["CLAUDE_CODE_SESSION_ID"] = _as_str(context.get("holder_session"))
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
    temp = Path(run) / "tmp"
    try:
        temp.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    environment["TMPDIR"] = str(temp)


def _start_server(
    session_dir: str, workers_home: Path, dev_shell: ServerDevShell = _INHERITED_SERVER_ENVIRONMENT,
) -> int:
    """Start this session's app-server and record it. The caller holds the server lock."""
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
            else:
                if isinstance(loaded, nix_env.Refused):
                    _log_server(session_dir, loaded.reason)
                    _remember_failed_dev_shell(session_dir, dev_shell)
                dev_shell = _INHERITED_SERVER_ENVIRONMENT
        try:
            with log_path.open("ab") as log_handle:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=log_handle,
                    start_new_session=True,
                    env=environment,
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
            if isinstance(dev_shell, WorktreeDevShell):
                _ = _session_path(session_dir, SERVER_START_FILE).write_text(json.dumps({
                    "pid": process.pid, "port": chosen, "started_at": time.time(),
                    "dev_shell_worktree": str(dev_shell.worktree),
                }), encoding="utf-8")
            timeout = DEV_SHELL_START_TIMEOUT_SECS if isinstance(dev_shell, WorktreeDevShell) else SERVER_START_TIMEOUT_SECS
            deadline = time.time() + timeout
            while time.time() < deadline:
                if process.poll() is not None:
                    raise SystemExit(
                        f"codex_mesh: app-server exited immediately; see {log_path}"
                    )
                try:
                    probe = WebSocket(chosen)
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
        except BaseException as error:
            _discard_failed_server_start(session_dir, served, process)
            if not isinstance(error, SystemExit) or not isinstance(dev_shell, WorktreeDevShell):
                raise
            _remember_failed_dev_shell(session_dir, dev_shell)
            _log_server(session_dir, f"dev-shell app-server {dev_shell.worktree} failed: {error}; falling back to caller environment")
            dev_shell = _INHERITED_SERVER_ENVIRONMENT
        finally:
            pending = _session_path(session_dir, SERVER_START_FILE)
            if _record_names_server(_read_json_object(pending), process.pid, chosen):
                pending.unlink(missing_ok=True)


def _discard_failed_server_start(
    session_dir: str, served: ServerRecord, process: subprocess.Popen[bytes]
) -> None:
    """Leave neither a process nor a current record after a failed start."""
    # Popen starts a new session: its pid is also the group id. Signal even when
    # the leader already exited, since a failed Nix evaluation can leave children.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _ = process.wait(timeout=5)
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            _ = process.wait(timeout=5)
    path = _session_path(session_dir, SERVER_FILE)
    if _record_names_server(_read_json_object(path), served["pid"], served["port"]):
        path.unlink(missing_ok=True)


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
    grace = min(time.time() + QUEUE_GRACE_SECS, deadline)
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
class WorkerHandedOff:
    """This launcher lost ownership; its assignment has no completion result."""

    to_run: str


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


RunOutcome = RunCompleted | WorkerHandedOff | FailedBeforeThread | FailedWithThread | CapacityRetriesExhausted


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


@dataclass(frozen=True)
class RelaunchBlockedByLiveTurn:
    """The old thread still has a turn this launcher could not stop."""


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
    method: str, params: dict[str, object], session_dir: str, name: str, thread_id: str,
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
        held = bool(_pending_for_thread(handle, thread_id))
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
        _ = _update_roster_if_expected(client.session_dir, client.name,
            {"thread_id": thread_id, "status": "starting", "continuation_owed": True},
            RosterOnThread(thread_id))


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
                _ = _update_roster_if_expected(stopped.session_dir, name,
                    {"thread_id": stopped.thread_id, "status": "failed", "continuation_owed": False},
                                               RosterOnThread(stopped.thread_id))
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


def command_start(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    resident = getattr(args, "resident", False) is True
    existing = _read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)
    if isinstance(existing, dict):
        _role_fence(_as_dict(cast(object, existing)))
        if _as_dict(cast(object, existing)).get("held_design_id"):
            raise SystemExit("codex_mesh: held roles must resume through their holder")
    availability = _server_availability(session_dir)
    old_port = availability.port if isinstance(availability, LiveServer) else None
    if isinstance(existing, dict):
        old_entry = _as_dict(cast("object", existing))
        old_thread = _as_str(old_entry.get("thread_id"))
        if old_thread:
            launcher_pid = old_entry.get("launcher_pid")
            if old_entry.get("status") == "failed":
                # A failed entry is always its launcher's last write.
                if old_port is not None:
                    cleanup = _end_unwatched_turn(
                        old_port, old_thread, Path(_as_str(_attr(args, "log_file")))
                    )
                    if isinstance(cleanup, RelaunchBlockedByLiveTurn):
                        print(
                            f"codex_mesh: {name}: thread {old_thread} still has a live turn that could not be interrupted; relaunch once it ends",
                            file=sys.stderr,
                        )
                        return 2
                old_thread = ""
            elif old_entry.get("status") == WAITING_CAPACITY:
                if isinstance(launcher_pid, int) and _pid_alive(launcher_pid):
                    print(
                        f"codex_mesh: {name}: thread {old_thread} is waiting for capacity; use codex_mesh.py end --to {name} before relaunching",
                        file=sys.stderr,
                    )
                    return 2
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
                print(
                    f"codex_mesh: {name}: thread {old_thread}'s state could not be read ({live.reason}); use codex_mesh.py end --to {name} before relaunching",
                    file=sys.stderr,
                )
                return 2
            if not isinstance(live, ThreadIdle):
                print(
                    f"codex_mesh: {name}: thread {old_thread} has a live turn; use codex_mesh.py end --to {name} before relaunching",
                    file=sys.stderr,
                )
                return 2
    port, fresh_server = ensure_server(session_dir, cwd=_as_str(_attr(args, "cwd")) or None)
    outcome = _run_worker(args, port, started_here=fresh_server)
    tested = fresh_server
    if _retry_warranted(outcome, fresh_server, resident):
        _ = _retire_server(session_dir, port)
        port, _fresh = ensure_server(session_dir, cwd=_as_str(_attr(args, "cwd")) or None)
        assert isinstance(outcome, (FailedBeforeThread, FailedWithThread))
        if isinstance(outcome, FailedWithThread):
            note = "the inherited app-server holds a Codex sign-in that has since changed"
        else:
            note = f"the inherited app-server failed in {outcome.seconds:.0f}s without reaching the provider"
        print(f"codex_mesh: {name}: {note}; retrying on a new one", file=sys.stderr)
        outcome = _run_worker(args, port, started_here=_fresh)
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
    if isinstance(outcome, WorkerHandedOff):
        print(f"codex_mesh: {name}: worker handed off to {outcome.to_run}; assignment completion unknown", file=sys.stderr)
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
    try:
        unit_workers.recover_adoptions(Path(session_dir))
    except (unit_workers.WorkerUnavailable, OSError, ValueError) as error:
        print(f"codex_mesh: follow: {error}", file=sys.stderr)
        return 2
    record = _lookup(session_dir, name)
    if record.get("held_design_id") and _as_str(_attr(args, "message_file")) != record.get("authorized_prompt"):
        moving = bool(_attr(args, "account_move")) and isinstance(
            _read_account_move(session_dir, name, _as_str(record.get("thread_id"))), AccountMoveMarker)
        if not moving:
            raise SystemExit("codex_mesh: held role assignment must come from design_roles.py")
    display_name = unit_workers.shown_name(Path(session_dir), name)
    role = _worker_role(args)
    claim_pid = _as_int(_attr(args, "claim_pid"))
    try:
        state = _lookup_state(session_dir, name)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1 if claim_pid else 2
    if claim_pid:
        if isinstance(state, AdoptedThread):
            print(f"codex_mesh: {display_name}: worker handed off to {state.to_run}; assignment completion unknown",
                  file=sys.stderr)
            return 2
        if not isinstance(state, StartingThread) or state.launcher_pid != claim_pid:
            print(f"codex_mesh: {display_name}: follow claim changed after dispatch", file=sys.stderr)
            return 1
    else:
        reason = _follow_refusal(session_dir, state, name=name)
        if reason:
            print(f"codex_mesh: {display_name}: {reason}; follow refused", file=sys.stderr)
            return 2
    assert not isinstance(state, InvalidThread)
    thread_id = state.thread_id
    if not claim_pid and not _claim_follow(
        session_dir, name, thread_id, os.getpid(), role, _dispatch_lens(args)
    ):
        print(f"codex_mesh: {display_name} was already claimed", file=sys.stderr)
        return 2
    _set_roster_assignment(session_dir, name, role, _as_str(_attr(args, "task")))
    display_name = unit_workers.shown_name(Path(session_dir), name)
    if claim_pid:
        _record_assignment(thread_id, role, _dispatch_lens(args), only_if_changed=True)
    owner_pid = claim_pid or os.getpid()
    opened = time.time()
    client: Client | None = None
    live_turn = ""
    try:
        home = workers_home()
        port, _fresh = ensure_server(session_dir, home, cwd=_as_str(_attr(args, "turn_cwd")) or None)
        _note_worker_port(session_dir, name, owner_pid, port)
        adopted_from = _as_str(record.get("adopted_from"))
        if adopted_from:
            _release_adopted_writer(adopted_from, name, thread_id, port)
        released = _release_other_worker_writers(session_dir, name, thread_id, port)
        if isinstance(released, WorkerThreadStateUnknown):
            raise SystemExit(f"thread {thread_id}: {released.reason}; follow-up did not start")
        if isinstance(released, WorkerWaitingOnLiveTurn):
            raise SystemExit(released.reason)
        client = Client(port, name)
        _ = _bind_serving_server(client, session_dir, started_here=_fresh)
        # Held under the pending lock: a message held while the roster moves to a replacement
        # thread waits here, then lands on the replacement with the ones carried over.
        with _pending_file(session_dir, name) as handle:
            resumed = _resume_worker_thread(args, session_dir, name, thread_id, client, home)
            if resumed.thread_replaced:
                carried = _pending_for_thread(handle, thread_id)
                if carried:
                    _write_pending(handle, resumed.thread_id, carried,
                                   accepting_messages=not _messages_closed(handle, thread_id))
        if record.get("unit_ledger"):
            unit_workers.note_backend(Path(session_dir), name, resumed.thread_id, home)
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
        if isinstance(outcome, WorkerHandedOff):
            print(f"codex_mesh: {display_name}: worker handed off to {outcome.to_run}; assignment completion unknown", file=sys.stderr)
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
    try:
        record = _lookup(session_dir, name)
        state = _roster_thread(record)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    if not isinstance(state, FollowableThread):
        print(
            f"codex_mesh: {name}: worker is running or otherwise busy; compact requires a finished worker",
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
        _ = _update_roster_if_expected(
            session_dir,
            name,
            compacted_record,
            RosterUnchanged(state.thread_id, revision),
        )
        if isinstance(size, KnownContextSize):
            print(f"compacted {name}: {before.tokens} -> {size.tokens} tokens")
        else:
            print(f"compacted {name}: {before.tokens} tokens, size not measured until its next turn")
        return 0
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
    if isinstance(state, AdoptedThread):
        return f"worker adopted to {state.to_run}"
    stale_launcher = _launcher_died(state)
    if not isinstance(state, FollowableThread) and not stale_launcher:
        return "worker is busy or not followable"
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    if record.get("adopted_to"):
        return f"worker adopted to {record['adopted_to']}"
    if record.get("adopted_from"):
        # The taking server may not know this thread until thread/resume.
        # follow claims it first, releases the giving writer, then checks idle
        # on the resumed thread before submitting any work.
        return ""
    availability = _server_availability(session_dir)
    if isinstance(availability, ServerRestartRequired):
        return ""
    try:
        probe = Client(availability.port, f"follow-check-{os.getpid()}")
        try:
            live = _read_live_turn(probe, state.thread_id)
        finally:
            probe.close()
    except (ConnectionError, OSError, SystemExit) as exc:
        return f"thread/read failed: {exc}"
    if isinstance(live, ThreadIdle):
        return ""
    return live.reason if isinstance(live, ThreadStateUnknown) else "live turn"


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


def _run_worker(args: argparse.Namespace, port: int, *, started_here: bool = False) -> RunOutcome:
    """Attach one worker to the app-server on `port` and run it to its end."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    timeout = _as_float(_attr(args, "timeout"), 86400.0)
    opened = time.time()
    try:
        return _attach_and_run(args, port, session_dir, name, timeout, opened, started_here=started_here)
    except (ConnectionError, OSError, SystemExit) as exc:
        return FailedBeforeThread(str(exc) or exc.__class__.__name__, time.time() - opened,
            exc.serving if isinstance(exc, ServingRequestFailure) else NotBoundCodexServer(),
            exc.reported if isinstance(exc, ServingRequestFailure) else False)


def _thread_start_params(args: argparse.Namespace) -> dict[str, object]:
    """The `thread/start` request for one worker."""
    params: dict[str, object] = {
        "cwd": _as_str(_attr(args, "cwd")),
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


def _attach_and_run(
    args: argparse.Namespace,
    port: int,
    session_dir: str,
    name: str,
    timeout: float,
    opened: float,
    *, started_here: bool = False,
) -> RunOutcome:
    prompt = Path(_as_str(_attr(args, "prompt_file"))).read_text(encoding="utf-8")
    log_path = Path(_as_str(_attr(args, "log_file")))

    client = Client(port, name)
    _ = _bind_serving_server(client, session_dir, started_here=started_here)
    started = _serving_request(client, "thread/start", _thread_start_params(args),
                               submits_work=True, stopped=CommandStoppedBeforeThread(session_dir))
    thread_id = _as_str(_as_dict(started.get("thread")).get("id"))
    if not thread_id:
        raise SystemExit("codex_mesh: thread/start returned no thread id")

    with _pending_file(session_dir, name) as pending_file:
        previous = _as_dict(_read_json_object(
            _session_path(session_dir, ROSTER_FILE)
        ).get(name))
        previous_thread = _as_str(previous.get("thread_id"))
        if previous_thread and previous_thread != thread_id:
            dropped = len(_pending_for_thread(pending_file, previous_thread))
            _ = pending_file.seek(0)
            _ = pending_file.truncate()
            with log_path.open("a", encoding="utf-8") as log:
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                _ = log.write(
                    f"[{stamp}] mesh: dropped {dropped} held messages for thread {previous_thread} on relaunch\n"
                )
        starting: StartingRecord = {
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
        _update_roster(session_dir, name, starting)
        if isinstance(role, AssignedWorkerRole):
            _record_assignment(thread_id, role.role, _dispatch_lens(args))

    return _stream_turn(args, client, port, session_dir, name, thread_id, prompt, timeout, opened)


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
        _ = _update_roster_if_expected(
            session_dir,
            name,
            {"thread_id": resumed.thread_id, "turn_id": resumed.turn_id,
             "status": "running", "port": resumed.port},
            RosterOnThread(resumed.thread_id),
        )
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
) -> RunOutcome:
    """Stream a new or resumed thread's turn through the same lifecycle."""
    client.session_dir = session_dir
    log_path = Path(_as_str(_attr(args, "log_file")))
    summary_path = Path(_as_str(_attr(args, "summary_file")))
    reply_file = _as_str(_attr(args, "reply_file"))
    resident = getattr(args, "resident", False) is True
    end_marker = _end_marker_path(session_dir, name)
    end_marker.unlink(missing_ok=True)
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
    with _pending_file(session_dir, name) as handle:
        _write_pending(handle, initial_thread_id, _pending_for_thread(handle, initial_thread_id))
    try:
        with contextlib.closing(FollowEvents(session_dir, name)) as events, log_path.open("a", encoding="utf-8") as log:
            stream.events = events
            _ = log.write(f"[{_now_stamp()}] mesh: {name} thread {stream.thread_id}\n")
            log.flush()
            prior = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            if prior.get("preserved_work"):
                _await_preserved_results(stream)
            started = _start_streamed_turn(stream, prompt, log)
            stopped = _stream_frames(stream, log) if started is StreamStep.NEXT_FRAME else None
        if stopped is not None:
            return stopped
        return _finish_stream(stream)
    except (ConnectionError, OSError, SystemExit) as exc:
        return _fail_stream(stream, exc)
    finally:
        if not end_marker.exists():
            with _pending_file(session_dir, name) as handle:
                _write_pending(handle, stream.thread_id, _pending_for_thread(handle, stream.thread_id),
                               accepting_messages=False)
        stream.client.close()


def _start_streamed_turn(stream: TurnStream, prompt: str, log: TextIO) -> StreamStep:
    """Name the thread, submit its first turn and mark it running on the roster."""
    # The roster is written before this call: a disconnect after thread
    # creation must still leave its id reachable.
    _ = stream.client.call("thread/name/set", {"threadId": stream.thread_id,
                                               "name": unit_workers.shown_name(Path(stream.session_dir), stream.name)})
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
    _ = _update_roster_if_expected(
        stream.session_dir,
        stream.name,
        {
            "thread_id": stream.thread_id,
            "turn_id": stream.turn_id,
            "status": "running",
            "port": stream.port,
        },
        RosterOnThread(stream.thread_id),
    )
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
    kept = _close_messages_and_keep_pending(stream.session_dir, stream.name, stream.thread_id)
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
    if isinstance(_roster_thread(_read_json_object(
            _session_path(stream.session_dir, ROSTER_FILE)).get(stream.name)), AdoptedThread):
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
        if (_finished_run(stream.session_dir) or entry.get("status") in ("done", "failed", "paused", "ended")):
            if _ended_unseen(stream.client, stream.thread_id):
                if stream.client.has_pending_frames:
                    stream.state_change_owed = True
                    return StreamStep.NEXT_FRAME
                if _close_messages_if_empty(stream.session_dir, stream.name, stream.thread_id, stream.runs):
                    return StreamStep.FINISH
        _keep_resident_on_selected_server(stream, log)
    return StreamStep.NEXT_FRAME


def _finish_if_ended_unseen(stream: TurnStream, log: TextIO) -> StreamStep:
    """Finish a one-turn launcher whose thread went idle without a completion notice."""
    _end_dead_runs(stream.session_dir, stream.name, stream.runs)
    if _waiting_on_detached_run(stream.session_dir, stream.name, stream.thread_id, stream.runs):
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
        if not _close_messages_if_empty(stream.session_dir, stream.name, stream.thread_id, stream.runs):
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
        held_messages = bool(_pending_for_thread(handle, stream.thread_id))
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
    if stream.turn_id and stream.turn_id in (
            _as_str(params.get("turnId")), _as_str(_as_dict(params.get("turn")).get("id"))):
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


def _on_turn_started(stream: TurnStream, params: dict[str, object]) -> None:
    live = _as_str(_as_dict(params.get("turn")).get("id"))
    if live and (not stream.follow or not stream.followed_turn or live == stream.followed_turn):
        stream.turn_id, stream.turn_start_seen = live, True
        if stream.resume_owed:
            stream.capacity_backoff_owed = False
        if stream.follow:
            stream.followed_turn = live
        _ = _update_roster_if_expected(
            stream.session_dir,
            stream.name,
            {"thread_id": stream.thread_id, "turn_id": live, "status": "running"},
            RosterOnThread(stream.thread_id),
        )


def _record_context_size(stream: TurnStream, params: dict[str, object]) -> None:
    context_size = _context_size_from_usage(params)
    if isinstance(context_size, KnownContextSize):
        _ = _update_roster_if_expected(
            stream.session_dir,
            stream.name,
            {
                "thread_id": stream.thread_id,
                "turn_id": stream.turn_id,
                "status": "running",
                "context_tokens": context_size.tokens,
            },
            RosterOnThread(stream.thread_id),
        )


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
    if isinstance(_roster_thread(_read_json_object(
            _session_path(stream.session_dir, ROSTER_FILE)).get(stream.name)), AdoptedThread):
        if stream.client.has_pending_frames:
            stream.state_change_owed = True
            return StreamStep.NEXT_FRAME
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
    _settle_quota_continuation(stream, notice_turn, turn_outcome)
    if (isinstance(account_move, AccountMoveMarker) and not stream.end_marker.exists()
            and (not notice_turn or notice_turn == account_move.turn_id)):
        return _resume_marked_account_move(stream, log, account_move)
    if _interrupted_for_held_work(method, params, stream.session_dir, stream.name, stream.thread_id, stream.runs):
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
    _ = _update_roster_if_expected(
        stream.session_dir,
        stream.name,
        {"thread_id": stream.thread_id, "turn_id": stream.turn_id, "status": "running"},
        RosterOnThread(stream.thread_id),
    )
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
        stream.client, thread_id, lambda message: _add_pending(stream.session_dir, stream.name, thread_id, message))
    remaining = CAPACITY_BUDGET_SECS - stream.capacity_waited
    if remaining <= 0:
        exhausted = CapacityRetriesExhausted(
            stream.thread_id, stream.capacity_retries, int(CAPACITY_BUDGET_SECS / 60)
        )
        stream.run_outcome = exhausted
        stream.failure = exhausted.message(stream.name)
        _ = _update_roster_if_expected(
            stream.session_dir,
            stream.name,
            {
                "thread_id": stream.thread_id,
                "status": CAPACITY_EXHAUSTED,
            },
            RosterOnThread(stream.thread_id),
        )
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
    with _pending_file(stream.session_dir, stream.name):
        _ = _update_roster_if_expected(
            stream.session_dir,
            stream.name,
            {
                "thread_id": stream.thread_id,
                "status": WAITING_CAPACITY,
                "launcher_pid": os.getpid(),
            },
            RosterOnThread(stream.thread_id),
        )
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
            _ = _update_roster_if_expected(
                stream.session_dir,
                stream.name,
                {
                    "thread_id": stream.thread_id,
                    "turn_id": stream.turn_id,
                    "status": "running",
                },
                RosterOnThread(stream.thread_id),
            )
        else:
            _ = _update_roster_if_expected(
                stream.session_dir,
                stream.name,
                {
                    "thread_id": stream.thread_id,
                    "status": "starting",
                    "launcher_pid": os.getpid(),
                },
                RosterOnThread(stream.thread_id),
            )
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
        _deliver_reply(unit_workers.shown_name(Path(stream.session_dir), stream.name),
                       stream.replies, stream.final_answer, stream.failure,
                       stream.summary_path, stream.reply_path, log)
        stream.final_answer = ""
        stream.failure = ""
        if stream.end_marker.exists():
            return StreamStep.FINISH
    with _pending_file(stream.session_dir, stream.name) as handle:
        held_messages = bool(_pending_for_thread(handle, stream.thread_id))
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
    _ = _update_roster_if_expected(
        stream.session_dir,
        stream.name,
        {"thread_id": stream.thread_id, "turn_id": stream.turn_id, "status": "running"},
        RosterOnThread(stream.thread_id),
    )
    return StreamStep.FINISH if stream.end_marker.exists() else StreamStep.NEXT_FRAME


def _finish_one_turn_launch(stream: TurnStream, log: TextIO) -> StreamStep:
    """Finish a one-turn launcher unless more work is coming to its thread."""
    # A run may have started since the last safety scan. Discover it before
    # claiming an empty inbox; finished records are still never reopened.
    _ = stream.runs.current()
    # A finished turn may have a peer turn opening behind it.
    if (_more_work_coming(stream.client, stream.thread_id, stream.deadline)
            or stream.client.has_pending_frames):
        # An idle readback can arrive before dispatch of a peer's completion.
        # Finish only after every notice it buffered has had its turn.
        stream.state_change_owed = True
        return StreamStep.NEXT_FRAME
    _end_dead_runs(stream.session_dir, stream.name, stream.runs)
    if _close_messages_if_empty(stream.session_dir, stream.name, stream.thread_id, stream.runs):
        return StreamStep.FINISH
    # The turn ended on a run still going: stay attached until its result is held.
    if _waiting_on_detached_run(stream.session_dir, stream.name, stream.thread_id, stream.runs):
        return StreamStep.NEXT_FRAME
    moved = _move_worker(stream)
    if isinstance(moved, WorkerWaitingForRoom):
        return _wait_for_room(stream, log, moved)
    if isinstance(moved, WorkerThreadStateUnknown):
        _hold_messages_for_next_launch(stream, log, moved.reason)
        return StreamStep.FINISH
    if isinstance(moved, MovedWorker):
        _ = _adopt_moved_worker(stream, moved)
    return StreamStep.NEXT_FRAME


def _finish_stream(stream: TurnStream) -> RunOutcome:
    """End the watch: close an unwatched turn, write the summary and settle the roster."""
    owner = _roster_thread(_read_json_object(_session_path(stream.session_dir, ROSTER_FILE)).get(stream.name))
    if isinstance(owner, AdoptedThread):
        return WorkerHandedOff(owner.to_run)
    if not stream.resume_owed:
        stream.pending_refusal = _pending_after_completion_readback(stream.pending_refusal, stream.client)
    if (not stream.failure and isinstance(stream.pending_refusal, PendingQuotaRefusal)
            and _continuation_is_owed(stream.session_dir, stream.name)):
        stream.failure = stream.pending_refusal.detail
    # A queued peer message can open a turn after the last completion
    # notification. Check once more before this launcher stops watching.
    if not stream.resident or stream.failure or stream.end_marker.exists():
        _ = _end_unwatched_turn(stream.port, stream.thread_id, stream.log_path)
    if not stream.resident or stream.replies == 0:
        _finish_summary(stream.summary_path, stream.reply_path,
                        unit_workers.shown_name(Path(stream.session_dir), stream.name), stream.replies + 1,
                        stream.final_answer or stream.failure or f"The worker {stream.name} produced no summary.")
    if isinstance(stream.run_outcome, CapacityRetriesExhausted):
        return replace(stream.run_outcome, pending_refusal=stream.pending_refusal, director=stream.director)
    _ = _update_roster_if_expected(
        stream.session_dir,
        stream.name,
        {"thread_id": stream.thread_id, "status": "failed" if stream.failure else "done"},
        RosterOnThread(stream.thread_id),
    )
    return FailedWithThread(stream.thread_id, stream.failure, time.time() - stream.opened, stream.failure_serving,
                            isinstance(stream.pending_refusal, PendingQuotaRefusal), True,
                            pending_refusal=stream.pending_refusal, director=stream.director) if stream.failure else RunCompleted()


def _fail_stream(stream: TurnStream, exc: ConnectionError | OSError | SystemExit) -> FailedWithThread | WorkerHandedOff:
    """Record a run that stopped on an error after its thread existed."""
    owner = _roster_thread(_read_json_object(
        _session_path(stream.session_dir, ROSTER_FILE)).get(stream.name))
    if isinstance(owner, AdoptedThread):
        # Cleanup on the outgoing server could interrupt the new owner's work.
        return WorkerHandedOff(owner.to_run)
    # Once thread/start returned, even a disconnected turn/start may have
    # begun work. Keep its id and never resubmit the original prompt.
    if isinstance(exc, WorkerQuotaRecoveryStopped):
        stream.port, stream.thread_id = exc.port, exc.thread_id
    elif isinstance(exc, WorkerMoveUncertain) and isinstance(exc.cleanup, WorkerTurnCleanup):
        stream.port = exc.cleanup.port
        stream.thread_id = exc.cleanup.thread_id
    if not stream.resume_owed:
        stream.pending_refusal = _pending_after_completion_readback(stream.pending_refusal, stream.client)
    _ = _end_unwatched_turn(stream.port, stream.thread_id, stream.log_path)
    failure = str(exc) or exc.__class__.__name__
    _ = _update_roster_if_expected(
        stream.session_dir,
        stream.name,
        {"thread_id": stream.thread_id, "status": "failed"},
        RosterOnThread(stream.thread_id),
    )
    serving = exc.serving if isinstance(exc, (ServingRequestFailure, WorkerMoveUncertain)) else stream.client.serving
    reported = exc.reported if isinstance(exc, (ServingRequestFailure, WorkerMoveUncertain)) else False
    if reported:
        stream.pending_refusal = PendingQuotaRefusal(serving, failure)
    return FailedWithThread(stream.thread_id, failure, time.time() - stream.opened, serving,
                            isinstance(stream.pending_refusal, PendingQuotaRefusal), True,
                            pending_refusal=stream.pending_refusal, director=stream.director)


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
        except (ConnectionError, OSError, SystemExit):
            continue
        thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
        if _as_str(_as_dict(thread.get("status")).get("type")) in ("active", "running"):
            busy.append(name)
    held_by = ", ".join(busy) or workers.get(thread_id, thread_id)
    return (f"run {Path(session_dir).name}, worker {held_by}: old server has not released the thread; "
            + "follow-up goes through once that turn ends")


def _release_worker_thread(client: Client, thread_id: str, session_dir: str) -> _ReleasedWorkerThread | WorkerThreadStateUnknown:
    deadline = time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS
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
        if _recorded_server_gone(session_dir, client.port):
            return _ReleasedWorkerThread()
        return WorkerThreadStateUnknown(_release_blocker_reason(client, session_dir, thread_id))
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


def _resume_worker_thread(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    workers_home: Path,
) -> _WorkerThreadForHome:
    params = _resume_params(session_dir, name, thread_id)
    previous_record = _lookup(session_dir, name)
    instructions_file = _as_str(previous_record.get("instructions_file"))
    held = bool(previous_record.get("held_design_id"))
    own_reviewer = previous_record.get("role") == "review" and bool(previous_record.get("unit_ledger"))
    sandbox = _worker_sandbox(args, session_dir, name)
    resumed = client.call("thread/resume", params)
    if not resumed.get("error"):
        result = _require(resumed, "thread/resume")
        if (held or own_reviewer or previous_record.get("adopted_from")) and _as_str(_as_dict(result.get("thread")).get("id")) != thread_id:
            raise SystemExit("RoleNeedsRecovery: resumed thread identity differs; replacement refused")
        return _WorkerThreadForHome(thread_id, False)
    if isinstance(_read_account_move(session_dir, name, thread_id), AccountMoveMarker):
        _ = _require(resumed, "thread/resume")
    if own_reviewer or previous_record.get("adopted_from"):
        _ = _require(resumed, "thread/resume")
    if held:
        raise SystemExit("RoleNeedsRecovery: held thread could not resume; replacement refused")
    start_params = _thread_start_params(args)
    start_params["sandbox"] = sandbox
    if "developerInstructions" in params:
        start_params.update({
            "developerInstructions": params["developerInstructions"], "approvalPolicy": "never",
        })
    started = _serving_request(client, "thread/start", start_params, submits_work=True)
    replacement = _as_str(_as_dict(started.get("thread")).get("id"))
    if not replacement:
        raise SystemExit("thread/start returned no replacement thread id")
    record: StartingRecord = {"thread_id": replacement, "status": "starting", "port": client.port}
    if "access_policy" in previous_record:
        # Assignment policy is durable for unit workers, even on a new thread.
        # Ordinary workers without one still take each launch's sandbox.
        record["access_policy"] = dict(previous_record["access_policy"])
    if instructions_file:
        record["instructions_file"] = instructions_file
    if not _update_roster_if_expected(session_dir, name, record, RosterOnThread(thread_id)):
        _refuse_taken_follow_up(client, replacement, session_dir)
    _ = client.call("thread/name/set", {"threadId": replacement, "name": unit_workers.shown_name(Path(session_dir), name)})
    _post_board_status(
        session_dir, name,
        f"{codex_home_display(workers_home)} refused to resume {thread_id}; worker continues as new thread {replacement}",
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
        current_pid = records[-1].get("pid") if records[-1].get("port") == port else None
        entry = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        held = bool(entry.get("held_design_id"))
        stopped_pids = {pid for pid in live_pids if not held and pid != current_pid
                        and _stop_unused(None, pid, port, RetiredRunServer(Path(session_dir)))}
        if pids and live_pids <= stopped_pids:
            continue
        client: Client | None = None
        try:
            client = Client(port, f"release-{name}")
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
                messages = _pending_for_thread(handle, thread_id)
                def park(message: str) -> None:
                    messages.append(message)
                    _write_pending(handle, thread_id, messages)
                _move_queue_to_pending(client, thread_id, park)
                state = _read_live_turn(client, thread_id)
                if not isinstance(state, ThreadIdle):
                    return WorkerThreadStateUnknown("old thread is no longer idle")
                released = _release_worker_thread(client, thread_id, session_dir)
                if isinstance(released, WorkerThreadStateUnknown):
                    return released
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
            messages = _pending_for_thread(handle, thread_id)

            def park(text: str) -> None:
                messages.append(text)
                _write_pending(handle, thread_id, messages)

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
        with _pending_file(session_dir, name) as handle:
            if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                return WorkerThreadStateUnknown("worker ended or was replaced before the new turn")
            messages = _pending_for_thread(handle, thread_id)
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
                _ = _bind_serving_server(moved, session_dir, started_here=_fresh)
                try:
                    resumed = _resume_worker_thread(args, session_dir, name, thread_id, moved, worker_home_for_turn)
                    thread_id, replaced = resumed.thread_id, resumed.thread_replaced
                    if replaced:
                        _write_pending(handle, thread_id, messages)
                        _update_roster(session_dir, name, {"thread_id": thread_id, "status": "starting",
                            "port": port, "continuation_owed": owes_continuation})
                        role = _roster_role(previous.get("role"))
                        if isinstance(role, AssignedWorkerRole):
                            _record_assignment(thread_id, role.role, _dispatch_lens(args))
                except (ConnectionError, OSError, SystemExit) as error:
                    _record_worker_move_failure(request, str(error) or type(error).__name__)
                    moved.close()
                    current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                    current_thread = _as_str(current.get("thread_id")) or thread_id
                    cleanup = (WorkerTurnCleanup(port, current_thread)
                               if replaced or current_thread != thread_id else NoWorkerTurnToCleanUp())
                    return WorkerThreadStateUnknown(str(error) or type(error).__name__, cleanup,
                        error.serving if isinstance(error, ServingRequestFailure) else moved.serving,
                        error.reported if isinstance(error, ServingRequestFailure) else False)
                _confirm_worker_move(session_dir, name, request, moved.serving)
                client.close()
            turn_id = ""
            if owes_continuation or message or messages:
                _update_roster(session_dir, name, {"thread_id": thread_id, "status": "starting", "port": port})
                if previous.get("preserved_work"):
                    messages.sort(key=lambda text: 0 if text.startswith(("Run finished:", "Run ended with no result:")) else 1)
                    inputs = [*messages, *(["Continue where you stopped."] if owes_continuation else []),
                              *([message] if message else [])]
                else:
                    inputs = (["Continue where you stopped.", *([message] if message else []), *messages] if owes_continuation
                              else [message, *messages] if message else messages)
                cleanup = WorkerTurnCleanup(port, thread_id)
                turn_id = _start_turn(moved, args, thread_id, _worker_sandbox(args, session_dir, name),
                                      inputs[0], tuple(inputs[1:]))
                if owes_continuation and not turn_id:
                    raise SystemExit("quota continuation was not confirmed by turn/start; task is still owed")
                _write_pending(handle, thread_id, [])
            prior_turn = _as_str(previous.get("turn_id")) if not replaced else ""
            _update_roster(session_dir, name, {
                "thread_id": thread_id, "turn_id": turn_id or prior_turn, "status": "running", "port": port,
                "continuation_owed": False, "preserved_work": False
            })
        return MovedWorker(moved, port, turn_id, thread_id, replaced, moved.serving,
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
                owner = _roster_thread(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                if isinstance(owner, AdoptedThread):
                    raise SystemExit(f"worker adopted to {owner.to_run}; no turn started")
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


def _turn_service_tier(client: Client, args: argparse.Namespace) -> str:
    """Resolve the current task's tier once for each turn, without a server cache."""
    session_dir = _as_str(_attr(args, "session_dir")) or client.session_dir
    name = _as_str(_attr(args, "to")) or _as_str(_attr(args, "name")) or client.name
    log_file = _as_str(_attr(args, "log_file"))
    try:
        record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)) if session_dir else {}
        task = _as_str(record.get("task"))
        resolved = subprocess.run(
            ["bash", "-c", 'AGENTS_CONFIG_TIER_ONLY=1; source "$1"; agents_turn_service_tier "$2"',
             "codex-mesh-tier", str(Path(__file__).with_name("agents_config.sh")), task or "delegate"],
            capture_output=True, text=True, timeout=5,
        )
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


def _turn_model_effort(args: argparse.Namespace) -> tuple[str, str]:
    """Select explicit dispatch settings or the current role default at send time."""
    role = _roster_role(_attr(args, "role"))
    # Freestanding resident threads may omit assignment metadata. Their task
    # still selects a live registry default; an unnamed task uses direct.impl.
    task = f"direct.{role.role}" if isinstance(role, AssignedWorkerRole) else (
        _as_str(_attr(args, "task")) or "direct.impl")
    model = _as_str(_attr(args, "model"))
    effort = _as_str(_attr(args, "effort"))
    resolved = subprocess.run([
        "bash", "-c", '''source "$1"; AGENTS_CALLER_FAMILY=codex agents_resolve "$2" || exit 2
if [[ -n "$3" || -n "$4" ]]; then
 pair="${3:-$AGENT_MODEL}"
 [[ "$pair" == *:* ]] || pair="$pair:${4:-$AGENT_EFFORT}"
 [[ -z "$4" ]] || pair="${pair%%:*}:$4"
 _agents_validate_pair "$2" "$AGENT_FAMILY" "$pair" || exit 2
fi
printf '%s\\n' "$AGENT_MODEL" "$AGENT_EFFORT"
''', "mesh-turn", str(Path(__file__).with_name("agents_config.sh")), task, model, effort,
    ], capture_output=True, text=True, timeout=30)
    if resolved.returncode:
        raise SystemExit(resolved.stderr.strip() or "cannot resolve turn model and effort")
    fields = resolved.stdout.splitlines()
    if len(fields) != 2:
        raise SystemExit("turn model resolver returned an invalid model/effort pair")
    return fields[0], fields[1]


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
    cwd = _as_str(_attr(args, "turn_cwd"))
    if cwd:
        params["cwd"] = cwd
    # Every turn names its policy: the app-server keeps a thread's last turn policy, so a
    # turn that leaves it out would inherit a previous `--read-only` turn's sandbox.
    params["sandboxPolicy"] = _turn_sandbox_policy(
        READ_ONLY_SANDBOX if _attr(args, "read_only") is True else worker_sandbox)
    # Resolve at every submission, including continuations and capacity retries.
    # Roster/identity values are display history, never input to this choice.
    model, effort = _turn_model_effort(args)
    params["model"], params["effort"] = model, effort
    service_tier = _turn_service_tier(client, args)
    params["serviceTier"] = service_tier
    client.turn_submitted_at = time.time()
    result = _serving_request(client, "turn/start", params, submits_work=True)
    turn_id = _as_str(_as_dict(result.get("turn")).get("id"))
    if turn_id:
        session_dir = _as_str(_attr(args, "session_dir"))
        name = _as_str(_attr(args, "name")) or _as_str(_attr(args, "to"))
        with _session_path(session_dir, ROSTER_FILE).open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            roster = _load_roster_for_write(handle)
            entry = _as_dict(roster.get(name))
            if entry.get("thread_id") == thread_id and not entry.get("adopted_to"):
                entry.update({"model": model, "effort": effort})
                _advance_roster_revision(entry)
                roster[name] = entry
                _ = handle.seek(0)
                _ = handle.truncate()
                _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
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
    try:
        client = Client(port, f"cleanup-{os.getpid()}")
        try:
            _ = _drop_queued_messages(client, thread_id)
            live = _read_live_turn(client, thread_id)
            turn_id = _interruptible_turn_id(client, thread_id, live)
            if isinstance(turn_id, ThreadLive):
                reply = _interrupt_turn(client, thread_id, turn_id.turn_id)
                if "error" in reply:
                    reason = _as_str(_as_dict(reply["error"]).get("message")) or "turn/interrupt failed"
                    remaining = _read_live_turn(client, thread_id)
                    if isinstance(remaining, (ThreadLive, ThreadActiveWithoutTurn)):
                        _log_uninterrupted_thread(log_path, thread_id, reason)
                        return RelaunchBlockedByLiveTurn()
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
        _log_uninterrupted_thread(log_path, thread_id, str(exc))
    return RelaunchAllowed()


def command_send(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, target)
    design_id = record.get("held_design_id")
    if design_id:
        from scripts.astra import design_roles
        role = record.get("held_role")
        if role not in ("designer", "reviewer"):
            raise SystemExit("codex_mesh: unknown held role")
        _ = design_roles.queue_message(design_id, role, _as_str(_attr(args, "source")) or "mesh send", _message_text(args))
        print(f"queued for {target}; holder adds it to the next authorized assignment")
        return 0
    if record.get("status") == CAPACITY_EXHAUSTED:
        print(
            f"codex_mesh: {target} is out of retries and has no launcher; a message would start a turn nobody watches (codex_mesh.py end --to {target})",
            file=sys.stderr,
        )
        return 2
    outcome = hold_message(session_dir, target, _message_text(args), without_launcher=True)
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
        with _server_lock(session_dir):
            current = _read_json_object(_session_path(session_dir, SERVER_FILE))
            if current and server_record_home(current) != home:
                _retire_record(session_dir, current)

    print(f"queued for {target}")
    return 0


def command_steer(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, target)
    turn_id = record.get("turn_id", "")
    status = record.get("status", "unknown")
    if not turn_id or status != "running":
        detail = f"status {status}"
        raise SystemExit(
            f"codex_mesh: {target} has no running turn to steer ({detail}); use `send`"
        )
    client = Client(_worker_port(session_dir, record), f"steer-{os.getpid()}")
    message = _message_text(args)
    reply = client.call(
        "turn/steer",
        {
            "threadId": record["thread_id"],
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
                "threadId": record["thread_id"],
                "expectedTurnId": live,
                "input": [{"type": "text", "text": message}],
            },
        )
        if not reply.get("error"):
            _ = _update_roster_if_expected(
                session_dir,
                target,
                {"thread_id": record["thread_id"], "turn_id": live, "status": "running"},
                RosterOnThread(record["thread_id"]),
            )
    _ = _require(reply, "turn/steer")
    client.close()
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
        outcome = hold_message(session_dir, target, message, without_launcher=True)
        if isinstance(outcome, WorkerEnded):
            print(f"codex_mesh: {outcome.reason}; nothing held or interrupted", file=sys.stderr)
            return 2
        held = ("; the message opens its next turn" if isinstance(outcome, HeldForLauncher)
                else "; no launcher is attached, so the message waits for its next dispatch")
    record = _lookup(session_dir, target)
    launcher_pid = _as_int(record.get("launcher_pid"))
    if _pid_alive(launcher_pid):
        port = _worker_port(session_dir, record)
    else:
        availability = _server_availability(session_dir)
        if not isinstance(availability, LiveServer):
            print(f"{target} has no running app-server; no turn to interrupt{held}")
            return 0
        port = availability.port
    thread_id = record["thread_id"]
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
    """
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    with _pending_file(session_dir, target) as pending_file:
        with _session_path(session_dir, ROSTER_FILE).open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            roster = _load_roster_for_write(handle)
            record = _as_dict(roster.get(target))
            if not record:
                raise SystemExit(f"codex_mesh: no worker named '{target}'")
            _role_fence(record)
            thread_id = _as_str(record.get("thread_id"))
            expected = _as_str(_attr(args, "expected_thread_id"))
            if expected and thread_id != expected:
                print(f"codex_mesh: {target}: thread changed; nothing ended", file=sys.stderr)
                return 2
            if record.get("adopted_to"):
                print(f"codex_mesh: {target}: worker was adopted; nothing ended", file=sys.stderr)
                return 2
            status = _as_str(record.get("status")) or "unknown"
            deadline = time.time() + END_OPERATION_TIMEOUT_SECS
            if _attr(args, "idle_only"):
                history = record.get("giving_runs")
                prior_runs = tuple(value for value in cast(list[object], history) if isinstance(value, str)) if isinstance(history, list) else ()
                giving = _as_str(record.get("adopted_from"))
                if giving and giving not in prior_runs:
                    prior_runs = (*prior_runs, giving)
                reason = "worker is running" if status in ("running", "starting", WAITING_CAPACITY) else _worker_idle_reason_with_inbox(
                    session_dir, target, record, pending_file, prior_runs=prior_runs,
                    paused=False, adopting=True, deadline=deadline)
                if reason:
                    print(f"codex_mesh: {target}: {reason}; nothing ended", file=sys.stderr)
                    return 2
            if status not in ENDABLE_STATUSES and status != "ended":
                print(f"{target} is {status}; nothing to end")
                return 0
            _end_marker_path(session_dir, target).touch()
            if not record.get("held_design_id"):
                _ = pending_file.seek(0)
                _ = pending_file.truncate()
            record["status"] = "ended"
            _advance_roster_revision(record)
            roster[target] = record
            _ = handle.seek(0)
            _ = handle.truncate()
            _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
            handle.flush()
            availability = _server_availability(session_dir)
            dropped = 0
            if isinstance(availability, LiveServer):
                if time.time() >= deadline:
                    print(f"codex_mesh: {target}: server cleanup timed out", file=sys.stderr)
                    return 1
                client = Client(availability.port, f"end-{os.getpid()}", deadline=deadline)
                try:
                    if record.get("held_design_id"):
                        def park(message: str) -> None:
                            _write_pending(pending_file, thread_id, [*_pending_for_thread(pending_file, thread_id), message])
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
        print(f"{name}\t{record.get('status', '?')}\t{record.get('thread_id', '')}")
    return 0


def _finished_run(session_dir: str) -> str:
    """Name how this run finished, or "" while it has not.

    `progress_history.py finish-run` stamps the run's progress state with its
    final status and `finished_at`; start-phase can explicitly reactivate it.
    """
    state = _read_json_object(_session_path(session_dir, plan_history.STATE_FILENAME))
    status = _as_str(state.get("status"))
    if not status or status == "active" or "finished_at" not in state:
        return ""
    return status


def command_reopen(args: argparse.Namespace) -> int:
    """Recover an ended or abandoned worker without creating a new thread."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    if _attr(args, "adoption_recovered") is not True:
        try:
            unit_workers.recover_adoptions(Path(session_dir))
        except (unit_workers.WorkerUnavailable, OSError, ValueError) as error:
            print(f"codex_mesh: reopen: {error}", file=sys.stderr)
            return 2
    record = _lookup(session_dir, name)
    if record.get("held_design_id"):
        raise SystemExit("codex_mesh: held roles recover through design_roles.py")
    finished = _finished_run(session_dir)
    if finished:
        print(f"codex_mesh: run finished ({finished}); use progress_history.py start-phase to reactivate this run, "
              + f"or workers.py resolve --session-dir <new-run> --role {_as_str(record.get('role')) or 'impl'} "
              + "to carry its worker forward", file=sys.stderr)
        return 2
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        status = _as_str(entry.get("status"))
        port = _as_int(entry.get("port"))
        reason = ""
        if entry.get("adopted_to"):
            reason = f"worker adopted to {entry['adopted_to']}"
        elif status not in ("ended", "running", "starting"):
            reason = "worker is not ended, running or starting"
        elif not _as_str(entry.get("thread_id")):
            reason = "missing thread id"
        elif _pid_alive(_as_int(entry.get("launcher_pid"))):
            reason = "launcher is alive"
        elif status != "ended" and any(
            _pid_alive(_as_int(server.get("pid")))
            for server in _recorded_run_servers(session_dir)
            if port <= 0 or server.get("port") == port
        ):
            reason = "app-server is alive"
        if reason:
            print(f"codex_mesh: {name}: {reason}", file=sys.stderr)
            return 2
        _end_marker_path(session_dir, name).unlink(missing_ok=True)
        entry.update({"status": "paused", "paused_from": status, "paused_turn": status != "ended"})
        _advance_roster_revision(entry)
        roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
    if _attr(args, "quiet") is not True:
        print(f"reopened {name}; send its next turn with --to {name}")
    return 0


def _worker_source_runs(session_dir: str, name: str) -> list[str]:
    """Include earlier giving runs when a worker moves again before resuming."""
    runs: list[str] = []
    extra: list[str] = []
    while session_dir and session_dir not in runs:
        runs.append(session_dir)
        record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        history = record.get("giving_runs")
        if isinstance(history, list):
            extra.extend(value for value in cast(list[object], history) if isinstance(value, str))
        session_dir = _as_str(record.get("adopted_from"))
    return list(dict.fromkeys([*runs, *extra]))


def finished_run_status(session_dir: str) -> str:
    """Public run-lifetime check used before a unit allocates any worker."""
    return _finished_run(session_dir)


def worker_run_history(session_dir: str, name: str) -> tuple[str, ...]:
    """Read giving-run links before an adopter acquires either roster lock."""
    return tuple(_worker_source_runs(session_dir, name))


def _release_adopted_writer(source: str, name: str, thread_id: str, target_port: int) -> None:
    """Release this thread, never the source run's other threads or processes."""
    released_ports: set[int] = {target_port}
    for run in _worker_source_runs(source, name):
        for port in _run_servers(run):
            if port in released_ports:
                continue
            released_ports.add(port)
            client = Client(port, f"adopt-release-{name}")
            try:
                if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                    continue
                if not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
                    raise SystemExit("adopted worker still has a live turn on the old server")
                released = _release_worker_thread(client, thread_id, run)
                if isinstance(released, WorkerThreadStateUnknown):
                    raise SystemExit(released.reason)
            finally:
                client.close()


def detach_reviewer_transport(source: str, name: str, thread_id: str,
                              inbox: TextIO, pending: dict[str, object]) -> None:
    """Carry server submissions and release the old writer before archiving it."""
    for port in _run_servers(source):
        client = Client(port, f"reviewer-transfer-{name}")
        try:
            if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                continue
            if not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
                raise unit_workers.WorkerUnavailable(f"{name}: reviewer still has a live turn")
            def park(message: str) -> None:
                messages = cast(list[str], pending.get("messages", []))
                pending.update({"thread_id": thread_id, "messages": [*messages, message]})
                _ = inbox.seek(0)
                _ = inbox.truncate()
                _ = inbox.write(json.dumps(pending))
                inbox.flush()
                os.fsync(inbox.fileno())
            _move_queue_to_pending(client, thread_id, park)
            released = _release_worker_thread(client, thread_id, source)
            if isinstance(released, WorkerThreadStateUnknown):
                raise unit_workers.WorkerUnavailable(released.reason)
        finally:
            client.close()


def worker_launcher_attached(record: Mapping[str, object]) -> bool:
    """A launcher claim is occupied while its recorded process is alive."""
    return _pid_alive(_as_int(record.get("launcher_pid")))


def worker_idle_reason(session_dir: str, name: str, record: Mapping[str, object],
                       *, prior_runs: tuple[str, ...] = (), paused: bool = False, adopting: bool = False) -> str:
    """Require positive idle evidence before a unit worker changes run owners."""
    try:
        with _pending_file(session_dir, name, blocking=False) as inbox:
            return _worker_idle_reason_with_inbox(
                session_dir, name, record, inbox, prior_runs=prior_runs,
                paused=paused, adopting=adopting,
                deadline=time.time() + THREAD_RELEASE_TIMEOUT_SECS)
    except BlockingIOError:
        # Adoption holds the roster: never wait for an inbox in that order.
        return "pending inbox is in use; retry allocation"


def _worker_idle_reason_with_inbox(
    session_dir: str, name: str, record: Mapping[str, object], inbox: TextIO,
    *, prior_runs: tuple[str, ...], paused: bool, adopting: bool, deadline: float,
) -> str:
    """One idle rule over a caller-owned snapshot and locked inbox.

    Adoption and explicit end may carry or cancel queued work; reservations,
    launchers, detached runs and active turns still prevent an idle transition.
    """
    thread_id = _as_str(record.get("thread_id"))
    if not thread_id:
        return "missing thread id"
    if record.get("adopted_to"):
        return f"already adopted to {record['adopted_to']}"
    if record.get("held_design_id"):
        return "held roles transfer through design_roles.py"
    if not paused and unit_workers.allocation_is_live(record) and not _finished_run(session_dir):
        return "worker is reserved for its next dispatch"
    if worker_launcher_attached(record):
        return "launcher is attached"
    if not (paused or adopting) and record.get("continuation_owed") is True:
        return "continuation is owed"
    if not (paused or adopting) and _pending_for_thread(inbox, thread_id):
        return "pending messages are queued"
    runs = DetachedRunWatch(session_dir, name, run_history=(session_dir, *prior_runs)).current()
    if not paused and (runs.live or runs.dead):
        return "detached run is live or its result is not reaped"
    try:
        # An account move can leave the outgoing thread on a retired server.
        # Every live server must either not hold it or positively report idle.
        # The caller may hold this run's roster lock. Start from its supplied
        # snapshot rather than recursively acquiring a shared lock on that file.
        source_runs = (session_dir, *prior_runs)
        ports = {port for run in source_runs for port in _run_servers(run)}
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
    except (ConnectionError, OSError, SystemExit) as error:
        return f"cannot establish idle state: {error}"


@dataclass(frozen=True)
class NoThreadToRelease:
    """No unrecorded allocation is owned by this operation."""


@dataclass(frozen=True)
class UnrecordedWorkerThread:
    thread_id: str


def open_unit_worker(session_dir: str, name: str, role: str, *, model: str = "",
                     effort: str = "") -> dict[str, object]:
    """Open an idle thread; assignment starts its first turn later through follow."""
    command = ['bash', '-c',
               'source "$1"; agents_resolve "direct.$2" || exit; printf "%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "${AGENT_SERVICE_TIER:-default}"', 'workers',
               str(Path(__file__).with_name("agents_config.sh")), role]
    resolved = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    fields = resolved.stdout.strip().split()
    if resolved.returncode or len(fields) != 4:
        raise unit_workers.WorkerUnavailable(resolved.stderr.strip() or "cannot resolve worker model")
    family, configured_model, configured_effort, service_tier = fields
    model = model or configured_model
    effort = effort or configured_effort
    if family != "codex":
        raise unit_workers.WorkerUnavailable("new persistent worker requires a Codex assignment")
    state = _read_json_object(_session_path(session_dir, plan_history.STATE_FILENAME))
    cwd = _as_str(state.get("working_dir")) or str(Path.cwd())
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
        result = _serving_request(client, "thread/start", _thread_start_params(launch))
        thread_id = _as_str(_as_dict(result.get("thread")).get("id"))
        if not thread_id:
            raise unit_workers.WorkerUnavailable("thread/start returned no thread id")
        allocation = UnrecordedWorkerThread(thread_id)
        display_name = unit_workers.shown_name(Path(session_dir), name, role=role)
        _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": display_name}), "thread/name/set")
        record: FollowableRecord = {"thread_id": thread_id, "status": "paused", "role": cast(WorkerRole, role),
                                    "model": model, "effort": effort, "access_policy": policy}
        if not _update_roster_if_expected(session_dir, name, record, RosterAbsent()):
            raise unit_workers.WorkerUnavailable(f"{name}: worker name was claimed")
        allocation = NoThreadToRelease()
        return {**record, "codex_home": str(home)}
    finally:
        try:
            if isinstance(allocation, UnrecordedWorkerThread):
                released = _release_worker_thread(client, allocation.thread_id, session_dir)
                if isinstance(released, WorkerThreadStateUnknown):
                    print(f"codex_mesh: unrecorded thread {allocation.thread_id}: {released.reason}; release unconfirmed",
                          file=sys.stderr)
        finally:
            client.close()


def command_adopt(args: argparse.Namespace) -> int:
    try:
        name = unit_workers.adopt(Path(_as_str(_attr(args, "from_run"))),
                                  Path(_as_str(_attr(args, "session_dir"))), _as_str(_attr(args, "name")))
    except (unit_workers.WorkerUnavailable, OSError, ValueError) as error:
        print(f"codex_mesh: adopt: {error}", file=sys.stderr)
        return 2
    print(name)
    return 0


def _preserve_paused_thread(client: Client, session_dir: str, name: str, thread_id: str) -> bool:
    with _pending_file(session_dir, name) as pending:
        def park(message: str) -> None:
            _write_pending(pending, thread_id, [*_pending_for_thread(pending, thread_id), message])
            os.fsync(pending.fileno())
        _move_queue_to_pending(client, thread_id, park)
    state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
    if isinstance(state, ThreadStateUnknown):
        raise RuntimeError(state.reason)
    if isinstance(state, ThreadActiveWithoutTurn):
        raise RuntimeError("active turn has no interruptible id")
    if isinstance(state, ThreadLive):
        _ = _interrupt_turn(client, thread_id, state.turn_id)
    return isinstance(state, ThreadLive)


def _pause_roster(session_dir: str) -> None:
    """Fence launcher completion writes before interrupting each retained thread."""
    for value in _read_json_object(_session_path(session_dir, ROSTER_FILE)).values():
        entry = _as_dict(value)
        _role_fence(entry)
        if entry.get("held_design_id"):
            raise SystemExit("codex_mesh: raw stop --pause refuses held roles; use design_roles.py")
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        for name, raw in roster.items():
            record = _as_dict(raw)
            status = _as_str(record.get("status"))
            if status == "ended" or record.get("adopted_to"):
                continue
            if status != "paused":
                record.update({"status": "paused", "paused_from": status,
                               "paused_turn": status in ("running", "starting")})
            _advance_roster_revision(record)
            roster[name] = record
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
    interrupt_by = time.time() + PAUSE_INTERRUPT_SECS
    failures: list[str] = []
    for name, raw in roster.items():
        record = _as_dict(raw)
        thread_id = _as_str(record.get("thread_id"))
        if record.get("status") != "paused" or not thread_id:
            continue
        interrupted = False
        observed = False
        for live_port in _run_servers(session_dir):
            if time.time() >= interrupt_by:
                failures.append(f"{name}: server did not answer in time; queue preservation unconfirmed")
                break
            try:
                client = Client(live_port, f"pause-{os.getpid()}", deadline=interrupt_by)
            except OSError as error:
                failures.append(f"{name}: cannot preserve server queue: {error}")
                continue
            try:
                if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                    continue
                interrupted = _preserve_paused_thread(client, session_dir, name, thread_id) or interrupted
                observed = True
            except (OSError, RuntimeError, TimeoutError, SystemExit) as error:
                failures.append(f"{name}: pause preservation failed: {error}")
            finally:
                client.close()
        # A thread may still have work on a retired server even when its
        # recorded port names the current server. Preserve all before reaping.
        if observed:
            with path.open("r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                current = _load_roster_for_write(handle)
                entry = _as_dict(current.get(name))
                if entry.get("status") == "paused" and entry.get("thread_id") == thread_id:
                    entry["paused_turn"] = interrupted
                    current[name] = entry
                    _ = handle.seek(0)
                    _ = handle.truncate()
                    _ = handle.write(json.dumps(current, indent=2, sort_keys=True))
    if failures:
        raise SystemExit("codex_mesh: servers kept alive: " + "; ".join(failures))


def command_stop(args: argparse.Namespace) -> int:
    """Reap this run's recorded servers, keeping paused workers and their work."""
    session_dir = _as_str(_attr(args, "session_dir"))
    if _attr(args, "pause") and unit_workers.run_has_live_director(Path(session_dir)):
        print("kept workers and app-servers: unit director is live")
        return 0
    for value in _read_json_object(_session_path(session_dir, ROSTER_FILE)).values():
        entry = _as_dict(value)
        _role_fence(entry)
        if entry.get("held_design_id"):
            raise SystemExit("codex_mesh: raw stop refuses held roles; use design_roles.py")
    if _attr(args, "pause"):
        # Queue preservation may refuse a server that cannot answer. The work
        # remains owned and protected even when that server must stay alive.
        unit_workers.pause_run(Path(session_dir))
        _pause_roster(session_dir)
    else:
        _mark_all_roster_ended(session_dir)
    retired_path = _session_path(session_dir, RETIRED_FILE)
    stored = _read_json_object(retired_path).get("servers")
    retired = cast("list[object]", stored) if isinstance(stored, list) else []
    path = _session_path(session_dir, SERVER_FILE)
    stopped = [
        pid
        for record in [*retired, _read_json_object(path)]
        for pid in [_as_dict(record).get("pid")]
        if isinstance(pid, int) and _reap(pid)
    ]
    retired_path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    if not stopped:
        print("no app-server running")
        return 0
    print(f"stopped app-server {', '.join(str(pid) for pid in stopped)}")
    return 0


def _reap(pid: int) -> bool:
    """Stop one app-server, escalating to SIGKILL. False if it was not running."""
    if not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"could not signal app-server {pid}: {exc}")
        return False
    deadline = time.time() + REAP_WAIT_SECS
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(REAP_POLL_SECS)
    if _pid_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return True


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
    """A run's mesh_retired.json names this server until it becomes idle."""

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
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        os.kill(target.pid, signum)
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
        time.sleep(REAP_POLL_SECS)


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
            os.kill(identity.pid, signal.SIGCONT)
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
            time.sleep(REAP_POLL_SECS)
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


def _retired_file_names_server(path: Path, pid: int, port: int) -> bool:
    stored = _read_json_object(path).get("servers")
    records = cast("list[object]", stored) if isinstance(stored, list) else []
    return any(_record_names_server(_as_dict(record), pid, port) for record in records)


def _server_ownership(
    roots: tuple[Path, ...] | None, pid: int, port: int, stderr: MeshServerLog | NonMeshServerStderr
) -> KnownServerOwnership:
    """Resolve the record that owns a server; a live current owner wins stale duplicates."""
    if roots is None:
        folders = (stderr.folder,) if isinstance(stderr, MeshServerLog) else ()
    else:
        current_folders = {path.parent for root in roots for path in root.glob(f"*/{SERVER_FILE}")}
        retired_folders = {path.parent for root in roots for path in root.glob(f"*/{RETIRED_FILE}")}
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
        from scripts.astra import design_roles
        for folder in design_roles.durable_meshes():
            if _record_names_server(_read_json_object(folder / SERVER_FILE), pid, port) and any(
                _as_dict(entry).get("held_design_id")
                for entry in _read_json_object(folder / ROSTER_FILE).values()
            ):
                return HeldDesignServer(folder)
    for folder in folders:
        if _retired_file_names_server(folder / RETIRED_FILE, pid, port):
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


def _forget_retired(folder: Path, pid: int) -> None:
    """Drop a stopped server from RETIRED_FILE: `stop` signals every pid listed there, and by the
    run's end this one could belong to another process."""
    path = folder / RETIRED_FILE
    stored = _read_json_object(path).get("servers")
    if not isinstance(stored, list):
        return
    records = cast("list[object]", stored)
    kept = [record for record in records if _as_dict(record).get("pid") != pid]
    if len(kept) != len(records):
        _ = path.write_text(json.dumps({"servers": kept}, indent=2), encoding="utf-8")


def _roster_threads(folder: Path) -> dict[str, str]:
    """Thread id to worker name for every conversation the run folder's roster lists."""
    roster = _read_json_object(folder / ROSTER_FILE)
    named = {_as_str(_as_dict(entry).get("thread_id")): name for name, entry in sorted(roster.items())}
    return {thread_id: name for thread_id, name in named.items() if thread_id}


def _busy_threads(port: int, workers: dict[str, str]) -> tuple[str, ...]:
    """Conversations on this server that are not idle, or why they could not be read.

    The server lists what it has loaded, so a turn left running by a launcher killed at its time
    limit is seen with no client connected and no roster. `workers` adds the roster's conversations
    and names them."""
    try:
        client = Client(port, f"sweep-{os.getpid()}")
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
    except (ConnectionError, OSError, SystemExit) as exc:
        return (f"the server stopped answering ({str(exc) or exc.__class__.__name__})",)
    finally:
        client.close()


def _judge(roots: tuple[Path, ...] | None, pid: int, port: int, age: float,
           clients: int) -> SweepJudgment:
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
        _sweep_verdict(facts, lambda: _busy_threads(port, workers)), ownership
    )


def _stop_unused(
    roots: tuple[Path, ...] | None, pid: int, port: int, ownership: ServerOwnership
) -> bool:
    """Stop a server a first look called unused, if a second look at this moment agrees.

    The second look runs under the run folder's server lock, so a launcher reaching
    `ensure_server` meanwhile waits and then starts a server of its own. A deleted folder has no
    launcher to wait and is not created again for the lock."""
    if isinstance(ownership, UnownedServerWithoutLog):
        return _stop_unused_with_lock_held(roots, pid, port)
    if not isinstance(ownership, (CurrentRunServer, HeldDesignServer, RetiredRunServer,
                                  StartingDevShellServer, UnownedServerWithLog)):
        return False
    folder = ownership.session_dir
    if not folder.is_dir():
        return _stop_unused_with_lock_held(roots, pid, port)
    with _server_lock(str(folder)):
        return _stop_unused_with_lock_held(roots, pid, port)


def _stop_unused_with_lock_held(roots: tuple[Path, ...] | None, pid: int, port: int) -> bool:
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
    judgment = _judge(roots, pid, port, age, clients.get(port, 0))
    if not isinstance(judgment.verdict, ServerUnused) or not _reap(pid):
        return False
    if isinstance(judgment.ownership, UnownedServerWithoutLog):
        return True
    ownership = judgment.ownership
    if not isinstance(ownership, (CurrentRunServer, HeldDesignServer, RetiredRunServer,
                                  StartingDevShellServer, UnownedServerWithLog)):
        return False
    folder = ownership.session_dir
    if folder.is_dir():
        _forget_retired(folder, pid)
        record = folder / SERVER_FILE
        if _record_names_server(_read_json_object(record), pid, port):
            record.unlink(missing_ok=True)
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
NOTICE_TIMEOUT_SECS = 120


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


class CodexNoticeKind(Enum):
    WORKERS_CHANGED = "workers_changed"
    SIGN_IN_CHANGED = "sign_in_changed"


def _notify_holder(session_id: str, line: str, kind: CodexNoticeKind) -> None:
    workers_changed = kind is CodexNoticeKind.WORKERS_CHANGED
    try:
        sent = subprocess.run(
            [sys.executable, str(SEND_SCRIPT), "--to", f"session:{session_id}", "--from", "codex-workers" if workers_changed else "codex-sign-in",
             "--summary", "Codex workers account changed" if workers_changed else "Codex sign-in changed", "--text", line],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=NOTICE_TIMEOUT_SECS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"codex_mesh: could not tell session {session_id}: {exc}", file=sys.stderr)
        return
    if sent.returncode:
        print(f"codex_mesh: could not tell session {session_id}: {sent.stderr.strip()}", file=sys.stderr)


@role_map_command
def command_signin_changed(args: argparse.Namespace) -> int:
    """Move every run off an app-server started before the Codex sign-in last changed.

    A path watch on the sign-in file runs this the moment it changes; `ensure_server` makes the
    same check at each launch for a run this missed. Each run's record is retired, so its next
    dispatch starts a server that reads the current sign-in. Running workers are interrupted and
    resume immediately on that server. The old server is stopped now if
    nothing uses it, otherwise by its watcher once nothing does. Run twice for one change, the
    second run finds nothing older and moves nothing.
    """
    roots = _sweep_roots(args)
    home = codex_home()
    changed = _sign_in_changed_at(home)
    if changed is None:
        print("codex_mesh: no Codex sign-in file; nothing moved")
        return 0
    at = datetime.fromtimestamp(changed).astimezone(user_zone()).strftime("%H:%M %Z")
    runs: list[WorkersRunMove | EndedWorkersRun] = []
    folders = sorted({path.parent for root in roots for filename in (SERVER_FILE, RETIRED_FILE)
                      for path in root.glob(f"*/{filename}")})
    for folder in folders:
        with _server_lock(str(folder)):
            current = _read_json_object(folder / SERVER_FILE)
            holders = tuple(_marker_sessions(roots, folder))
            live = _run_is_live(folder, holders)
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
        launcher_sessions = _launcher_sessions(folder) if not holders else ()
        workers = tuple(worker for port in outgoing for worker in _switch_run_workers(
            folder, port, codex_home_role_label(workers_home()), dry_run=False))
        runs.append(WorkersRunMove(folder, holders, tuple(outgoing.items()), workers, launcher_sessions))
    switches = _finish_run_moves(runs)
    for run, switched in zip(runs, switches, strict=True):
        if isinstance(run, EndedWorkersRun):
            continue
        folder = run.folder
        stopped = 0
        for port, pid in run.servers:
            if _stop_unused(None, pid, port, RetiredRunServer(folder)):
                stopped += 1
            else:
                _ = _start_watcher(str(folder), pid, port)
        old = "is stopped" if stopped == len(run.servers) else "stops once nothing uses it"
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
            _notify_holder(session_id, line, CodexNoticeKind.SIGN_IN_CHANGED)
        print(line)
    for line in leftover_server_lines(switches, dry_run=False):
        print(line)
    print(f"{sum(isinstance(run, WorkersRunMove) for run in runs)} run(s) moved off the old sign-in")
    return 0


@dataclass(frozen=True)
class WorkerMoveReady:
    """An idle worker moves at its next turn, or this is a dry-run prediction."""


@dataclass(frozen=True)
class WorkerMoveResumed:
    """The interrupted worker confirmed a continuation on its new server."""


@dataclass(frozen=True)
class WorkerMoveFailed:
    reason: str
    command: str


WorkerSwitchOutcome = WorkerMoveReady | WorkerMoveResumed | WorkerMoveFailed


@dataclass(frozen=True)
class SwitchingWorker:
    name: str
    mid_turn: bool
    outcome: WorkerSwitchOutcome = field(default_factory=WorkerMoveReady)

    @property
    def failure(self) -> str:
        return self.outcome.reason if isinstance(self.outcome, WorkerMoveFailed) else ""

    @property
    def resume_command(self) -> str:
        return self.outcome.command if isinstance(self.outcome, WorkerMoveFailed) else ""


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
    launcher_sessions: tuple[str, ...] = ()


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
    launcher_sessions: tuple[str, ...] = ()


def _account_move_command(session_dir: str, name: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "resume-account-move",
                       "--session-dir", session_dir, "--to", name])


def _resume_account_move_detached(session_dir: str, name: str) -> None:
    """Give an interrupted worker without a launcher its own continuation watcher."""
    with _session_path(session_dir, f"{name}.account-move-launcher.log").open("ab") as log:
        _ = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "resume-account-move",
             "--session-dir", session_dir, "--to", name],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )


def command_resume_account_move(args: argparse.Namespace) -> int:
    session_dir, name = _as_str(_attr(args, "session_dir")), _as_str(_attr(args, "to"))
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    if record.get("adopted_to"):
        print(f"codex_mesh: {name}: worker adopted to {record['adopted_to']}", file=sys.stderr)
        return 2
    _role_fence(record)
    marker = _read_account_move(session_dir, name, _as_str(record.get("thread_id")))
    if isinstance(marker, NoAccountMove):
        port = record.get("port")
        if not isinstance(port, int) or not _as_str(record.get("thread_id")):
            print(f"codex_mesh: {name}: no recorded thread to resume", file=sys.stderr)
            return 1
        retried = _interrupt_worker_for_account_move(
            session_dir, name, record, port, codex_home_role_label(workers_home()))
        outcome = (_wait_account_move_resumes((retried,))[(session_dir, name)]
                   if isinstance(retried, InterruptedWorkerMove) else retried.outcome)
        if isinstance(outcome, WorkerMoveFailed):
            print(f"codex_mesh: {name}: {outcome.reason}", file=sys.stderr)
            return 1
        return 0
    role = _roster_role(record.get("role"))
    if not isinstance(role, AssignedWorkerRole):
        _fail_account_move(session_dir, name, marker, "resume failed: worker has no recorded role")
        return 1
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
    assignment = agents_log.role_at(
        agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
        marker.thread_id, datetime.now(timezone.utc),
    )
    lens = assignment.lens if isinstance(assignment, agents_log.Assignment) and assignment.role == role.role else None
    claimed: StartingRecord = {
        "thread_id": marker.thread_id, "status": "starting", "launcher_pid": os.getpid(),
        "role": role.role, "previous_role": role.role, "previous_status": "failed",
    }
    if not _update_roster_if_expected(session_dir, name, claimed,
                                     RosterUnchanged(marker.thread_id, _roster_revision(record))):
        _fail_account_move(session_dir, name, marker, "resume failed: worker changed before launcher claim")
        return 1
    message = _session_path(session_dir, f"{name}.account-move-prompt.txt")
    _ = message.write_text(ACCOUNT_MOVE_CONTINUATION, encoding="utf-8")
    follow_args = argparse.Namespace(
        session_dir=session_dir, to=name, role=role.role, lens=lens, claim_pid=os.getpid(),
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
    detached: set[tuple[str, str]] = set()
    deadline = time.monotonic() + ACCOUNT_MOVE_RESUME_TIMEOUT_SECS
    while pending:
        for key, move in tuple(pending.items()):
            try:
                result = _read_json_object(_account_move_result_path(move.session_dir, move.name))
                if result.get("move_id") == move.marker.move_id:
                    if result.get("resumed") is True:
                        outcomes[key] = WorkerMoveResumed()
                    elif reason := _as_str(result.get("error")):
                        outcomes[key] = WorkerMoveFailed(reason, _account_move_command(*key))
                if key in outcomes:
                    del pending[key]
                    continue
                if key not in detached and (move.launcher_pid <= 0 or not _pid_alive(move.launcher_pid)):
                    _resume_account_move_detached(*key)
                    detached.add(key)
            except (ConnectionError, OSError, SystemExit) as error:
                outcomes[key] = WorkerMoveFailed(f"resume failed: {error}", _account_move_command(*key))
                del pending[key]
        if not pending:
            break
        if time.monotonic() >= deadline:
            for key in pending:
                outcomes[key] = WorkerMoveFailed("resume not confirmed before timeout", _account_move_command(*key))
            break
        time.sleep(0.05)
    return outcomes


def _leftover_server_verdict(folder: Path, pid: int, port: int) -> SweepVerdict:
    """Use the sweep's retired-server rules even in a preview before retirement."""
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


def _finish_ended_run(run: EndedWorkersRun, *, dry_run: bool) -> WorkersRunSwitch:
    stopped = 0
    kept: list[KeptWorkersServer] = []
    for port, pid in run.servers:
        if not dry_run and _stop_unused(None, pid, port, RetiredRunServer(run.folder)):
            stopped += 1
            continue
        verdict = _leftover_server_verdict(run.folder, pid, port)
        if isinstance(verdict, ServerInUse):
            kept.append(KeptWorkersServer(pid, port, verdict.reason))
        elif not dry_run:
            kept.append(KeptWorkersServer(pid, port, "not stopped at the second safety check"))
        if not dry_run:
            _ = _start_watcher(str(run.folder), pid, port)
    return WorkersRunSwitch(run.folder, (), (), len(run.servers), stopped, tuple(kept))


def leftover_server_lines(switches: Sequence[WorkersRunSwitch], *, dry_run: bool) -> tuple[str, ...]:
    total = sum(run.leftover_servers for run in switches)
    if not total:
        return ()
    kept = sum(len(run.kept_servers) for run in switches)
    line = f"{total} leftover servers from ended sessions"
    if dry_run:
        line += f": {total - kept} can be stopped, {kept} would stay"
    else:
        line += f", {sum(run.stopped_servers for run in switches)} stopped"
    return (line, *(f"{'would stay' if dry_run else 'left running'}: {server.pid} on port {server.port} "
                    + f"({run.folder}): {server.reason}"
                    for run in switches for server in run.kept_servers))


def _finish_run_moves(
    runs: list[WorkersRunMove | EndedWorkersRun], *, dry_run: bool = False,
) -> tuple[WorkersRunSwitch, ...]:
    outcomes = _wait_account_move_resumes(tuple(
        worker for run in runs if isinstance(run, WorkersRunMove)
        for worker in run.workers if isinstance(worker, InterruptedWorkerMove)
    ))
    return tuple(
        _finish_ended_run(run, dry_run=dry_run) if isinstance(run, EndedWorkersRun)
        else WorkersRunSwitch(run.folder, run.holders, tuple(
            SwitchingWorker(worker.name, True, outcomes[(worker.session_dir, worker.name)])
            if isinstance(worker, InterruptedWorkerMove) else worker for worker in run.workers
        ), launcher_sessions=run.launcher_sessions) for run in runs)


def _interrupt_worker_for_account_move(session_dir: str, name: str, entry: dict[str, object],
                                       port: int, label: str) -> SwitchingWorker | InterruptedWorkerMove:
    _role_fence(entry)
    thread_id = _as_str(entry.get("thread_id"))
    marker: AccountMoveMarker | NoAccountMove = NoAccountMove()
    client: Client | None = None
    try:
        if not thread_id:
            raise SystemExit("worker has no recorded thread")
        client = Client(port, f"account-move-{name}")
        state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
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
        with _pending_file(session_dir, name):
            if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                raise SystemExit("worker ended or was replaced before interruption")
            _write_account_move_json(_account_move_path(session_dir, name), stored)
        with _pending_file(session_dir, name) as pending:
            def park(message: str) -> None:
                _write_pending(pending, thread_id, [*_pending_for_thread(pending, thread_id), message])
            _move_queue_to_pending(client, thread_id, park)
        if isinstance(state, ThreadLive):
            reply = _interrupt_turn(client, thread_id, state.turn_id)
            if "error" in reply and not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
                _account_move_path(session_dir, name).unlink(missing_ok=True)
                _ = _require(reply, "turn/interrupt")
        client.close()
        client = None
        return InterruptedWorkerMove(session_dir, name, marker, _as_int(entry.get("launcher_pid")))
    except (ConnectionError, OSError, SystemExit) as error:
        reason = f"interrupt failed: {error}"
        if isinstance(marker, AccountMoveMarker):
            with contextlib.suppress(OSError):
                _fail_account_move(session_dir, name, marker, reason)
        return SwitchingWorker(name, True, WorkerMoveFailed(reason, _account_move_command(session_dir, name)))
    finally:
        if client is not None:
            client.close()


def _switch_run_workers(
    folder: Path, port: int, label: str, *, dry_run: bool,
) -> tuple[SwitchingWorker | InterruptedWorkerMove, ...]:
    entries = _read_json_object(folder / ROSTER_FILE)
    workers: list[SwitchingWorker | InterruptedWorkerMove] = []
    for name, value in sorted(entries.items()):
        entry = _as_dict(value)
        _role_fence(entry)
        if entry.get("status") == "ended" or entry.get("port", port) != port:
            continue
        if entry.get("status") == "running" and not dry_run:
            workers.append(_interrupt_worker_for_account_move(str(folder), name, entry, port, label))
        else:
            if entry.get("held_design_id") and not dry_run:
                thread_id = _as_str(entry.get("thread_id"))
                client = Client(port, "held-idle-account-move")
                try:
                    with _pending_file(str(folder), name) as pending:
                        def park(message: str) -> None:
                            _write_pending(pending, thread_id, [*_pending_for_thread(pending, thread_id), message])
                        _move_queue_to_pending(client, thread_id, park)
                        released = _release_worker_thread(client, thread_id, str(folder))
                        if isinstance(released, WorkerThreadStateUnknown):
                            raise SystemExit("RoleNeedsRecovery: " + released.reason)
                finally:
                    client.close()
            workers.append(SwitchingWorker(name, entry.get("status") == "running"))
    return tuple(workers)


def retire_workers_servers(workers_home: Path, label: str, *, dry_run: bool = False) -> tuple[WorkersRunSwitch, ...]:
    """Retire run servers, interrupt running turns, and confirm their continuations."""
    notices: dict[str, list[str]] = {}
    notice_failures: dict[str, list[str]] = {}
    runs: list[WorkersRunMove | EndedWorkersRun] = []
    at = datetime.now().astimezone(user_zone()).strftime("%H:%M %Z")
    from scripts.astra import design_roles
    durable = set(design_roles.durable_meshes())
    folders = sorted({path.parent for root in SWEEP_ROOTS
                      for filename in (SERVER_FILE, RETIRED_FILE) for path in root.glob(f"*/{filename}")} | durable)
    for folder in folders:
        with _server_lock(str(folder)):
            current = _read_json_object(folder / SERVER_FILE)
            outgoing: dict[int, int] = {}
            for record in _recorded_run_servers(str(folder)):
                pid, port = record.get("pid"), record.get("port")
                if (isinstance(pid, int) and isinstance(port, int) and _pid_alive(pid)
                        and server_record_home(record) != workers_home):
                    outgoing[port] = pid
            if not outgoing:
                continue
            holders = tuple(_marker_sessions(SWEEP_ROOTS, folder))
            if folder in durable:
                context = _design_context(str(folder))
                holders = (_as_str(context.get("holder_session")),)
            live = folder in durable or _run_is_live(folder, holders)
            current_port = current.get("port")
            if not dry_run and isinstance(current_port, int) and current_port in outgoing:
                _retire_record(str(folder), current)
        if not live:
            runs.append(EndedWorkersRun(folder, tuple(outgoing.items())))
            continue
        launcher_sessions = _launcher_sessions(folder) if not holders else ()
        workers = tuple(worker for port in outgoing
                        for worker in _switch_run_workers(folder, port, label, dry_run=dry_run))
        runs.append(WorkersRunMove(folder, holders, tuple(outgoing.items()), workers, launcher_sessions))
    switches = _finish_run_moves(runs, dry_run=dry_run)
    for run, switched in zip(runs, switches, strict=True):
        if isinstance(run, EndedWorkersRun):
            continue
        folder, holders, workers = switched.folder, switched.holders, switched.workers
        line = (
            f"Workers account {'would move' if dry_run else 'changed'} at {at} to {label} "
            + f"({codex_home_display(workers_home)}): run {folder.name}'s running workers "
            + "were interrupted and resumed on the new account; idle workers move at their next turn."
        )
        failures = [f"{worker.name}: {worker.failure}; resume with {worker.resume_command}"
                    for worker in workers if worker.failure]
        if failures:
            line = (f"Workers account changed at {at} to {label}: running workers were interrupted; "
                    + "some continuations failed: " + "; ".join(failures))
        if not dry_run:
            _log_server(str(folder), line)
            for port, pid in run.servers:
                if not _stop_unused(None, pid, port, RetiredRunServer(folder)):
                    _ = _start_watcher(str(folder), pid, port)
        for session_id in holders:
            notices.setdefault(session_id, []).append(folder.name)
            notice_failures.setdefault(session_id, []).extend(failures)
    if not dry_run:
        for session_id, holder_runs in notices.items():
            _notify_holder(
                session_id,
                f"Workers account changed at {at} to {label} ({codex_home_display(workers_home)}): "
                + f"runs {', '.join(holder_runs)}: running workers were interrupted and resumed on the new account; "
                + "idle workers move at their next turn."
                + (" Worker move failures: " + "; ".join(notice_failures[session_id])
                   if notice_failures[session_id] else ""),
                CodexNoticeKind.WORKERS_CHANGED,
            )
    return switches


def command_serve(args: argparse.Namespace) -> int:
    port, _fresh = ensure_server(_as_str(_attr(args, "session_dir")))
    print(port)
    return 0



def _failure_account_detail(error: BaseException, serving: CodexServingState) -> str:
    account = serving.account
    name = account.label if isinstance(account, KnownCodexAccount) else "unknown Codex account"
    return f"{name} ({codex_home_display(serving.home)}): {error}"


def command_prime(args: argparse.Namespace) -> int:
    """Create a read-only reviewer template without starting a turn."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    client: Client | None = None
    try:
        existing = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        _role_fence(existing)
        if existing.get("held_design_id"):
            raise SystemExit("held role already has a thread; use design_roles.py resume")
        held_design = _as_str(_attr(args, "held_design"))
        held_role = _as_str(_attr(args, "held_role"))
        if held_design:
            from scripts.astra import design_roles
            design = design_roles.read_design(held_design)
            instance = next((value for value in design.roles if value.name == name and value.role == held_role), None)
            reach = design_roles.resolve_holder(design.holder)
            if (instance is None or not isinstance(instance.lifecycle, design_roles.RoleNotOpened)
                    or not instance.lifecycle.claim or not isinstance(reach, design_roles.HolderReach)
                    or reach.session_id != os.environ.get("CLAUDE_CODE_SESSION_ID")):
                raise SystemExit("held thread creation lacks its holder's reserved claim")
            claim = design_roles.design_directory(held_design) / f"{held_role}.creation-claimed"
            with claim.open("x", encoding="utf-8") as handle:
                _ = handle.write(instance.lifecycle.claim)
        instructions_path = Path(_as_str(_attr(args, "instructions_file"))).resolve()
        instructions = instructions_path.read_text(encoding="utf-8")
        params: dict[str, object] = {
            "developerInstructions": instructions,
            "cwd": _as_str(_attr(args, "cwd")),
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
        port, _fresh = ensure_server(session_dir, cwd=_as_str(_attr(args, "cwd")) or None)
        client = Client(port, name)
        _ = _bind_serving_server(client, session_dir, started_here=_fresh)
        result = _serving_request(client, "thread/start", params)
        thread_id = _as_str(_as_dict(result.get("thread")).get("id"))
        if not thread_id:
            raise SystemExit("thread/start returned no thread id")
        # The held design's holder names the thread before its mesh record exists.
        title = unit_workers.shown_name(Path(session_dir), name, role="review",
                                        held_design_id=held_design, held_role=held_role)
        _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": title}), "thread/name/set")
        record: FollowableRecord = {
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
        _update_roster(session_dir, name, record)
        print(thread_id)
        return 0
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
    client: Client | None = None
    try:
        existing = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if _as_str(existing.get("thread_id")) and existing.get("status") != "ended":
            print(f"codex_mesh: {name}: name already belongs to a thread; fork refused", file=sys.stderr)
            return 2
        expected: ExpectedRoster = (RosterEndedOnThread(_as_str(existing.get("thread_id")))
                                    if _as_str(existing.get("thread_id")) else RosterAbsent())
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
        result = _serving_request(client, "thread/fork", fork_params)
        thread_id = _as_str(_as_dict(result.get("thread")).get("id"))
        if not thread_id:
            raise SystemExit("thread/fork returned no thread id")
        _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": unit_workers.shown_name(Path(session_dir), name, role="review")}), "thread/name/set")
        record: FollowableRecord = {
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
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if entry.get("thread_id") != state.thread_id or entry.get("status") != "done":
            print(f"codex_mesh: {name}: worker changed while setting instructions; instructions unchanged",
                  file=sys.stderr)
            return 2
        entry["instructions_file"] = str(instructions)
        _advance_roster_revision(entry)
        roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
    return 0


def _clear_reset_context(session_dir: str, name: str, thread_id: str, launcher_pid: int) -> bool:
    """Clear context only while reset still owns this thread's worker claim."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _load_roster_for_write(handle)
        entry = _as_dict(roster.get(name))
        if (entry.get("thread_id") != thread_id or entry.get("status") != "starting"
                or entry.get("launcher_pid") != launcher_pid):
            return False
        entry["context_tokens"] = 0
        _advance_roster_revision(entry)
        roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
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
        state = _lookup_state(session_dir, name)
        if not isinstance(state, FollowableThread):
            reason = state.reason if isinstance(state, InvalidThread) else "worker is busy or not followable"
            print(f"codex_mesh: {name}: {reason}; reset refused", file=sys.stderr)
            return 2
        role = _roster_role(_lookup(session_dir, name).get("role"))
        reset_role = role.role if isinstance(role, AssignedWorkerRole) else "review"
        if not _claim_follow(session_dir, name, state.thread_id, owner_pid, reset_role):
            print(f"codex_mesh: {name}: worker was already claimed; reset refused", file=sys.stderr)
            return 2
        claimed = True
        claimed_thread_id = state.thread_id
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
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    serve = subparsers.add_parser("serve", help="start the session app-server")
    _ = serve.add_argument("--session-dir", required=True)
    serve.set_defaults(handler=command_serve)
    release_held = subparsers.add_parser("release-held-server")
    _ = release_held.add_argument("--session-dir", required=True)
    release_held.set_defaults(handler=command_release_held_server)

    start = subparsers.add_parser("start", help="launch a worker and block")
    _ = start.add_argument("--session-dir", required=True)
    _ = start.add_argument("--name", required=True)
    _ = start.add_argument("--role", choices=WORKER_ROLES, default="")
    _ = start.add_argument("--lens", choices=agents_log.LENSES)
    _ = start.add_argument("--cwd", required=True)
    _ = start.add_argument("--prompt-file", required=True)
    _ = start.add_argument("--summary-file", required=True)
    _ = start.add_argument(
        "--reply-file",
        default="",
        help="append replies here; the summary file is then the worker's own",
    )
    _ = start.add_argument("--log-file", required=True)
    _ = start.add_argument("--model", default="")
    _ = start.add_argument("--effort", default="")
    _ = start.add_argument("--service-tier", default="")
    _ = start.add_argument("--task", default="")
    _ = start.add_argument("--sandbox", choices=SANDBOX_MODES, default=WORKER_SANDBOX)
    _ = start.add_argument("--timeout", type=float, default=86400.0)
    _ = start.add_argument(
        "--resident",
        action="store_true",
        help="stay attached across turns, delivering each reply, until `end`",
    )
    start.set_defaults(handler=command_start)

    account_move = subparsers.add_parser("resume-account-move", help="resume an interrupted account move")
    _ = account_move.add_argument("--session-dir", required=True)
    _ = account_move.add_argument("--to", required=True)
    account_move.set_defaults(handler=command_resume_account_move)

    follow = subparsers.add_parser("follow", help="run one turn on a finished worker")
    _ = follow.add_argument("--session-dir", required=True)
    _ = follow.add_argument("--to", required=True)
    _ = follow.add_argument("--role", choices=WORKER_ROLES, required=True)
    _ = follow.add_argument("--lens", choices=agents_log.LENSES)
    _ = follow.add_argument("--claim-pid", type=int, default=0)
    _ = follow.add_argument("--message-file", required=True)
    _ = follow.add_argument("--summary-file", required=True)
    _ = follow.add_argument("--reply-file", default="")
    _ = follow.add_argument("--log-file", required=True)
    _ = follow.add_argument("--submission-receipt-file", default="", help=argparse.SUPPRESS)
    _ = follow.add_argument("--model", default="")
    _ = follow.add_argument("--effort", default="")
    _ = follow.add_argument("--service-tier", default="")
    _ = follow.add_argument("--task", default="")
    _ = follow.add_argument("--cwd", dest="turn_cwd", default="")
    _ = follow.add_argument("--read-only", action="store_true")
    _ = follow.add_argument("--image", action="append", default=[])
    _ = follow.add_argument("--timeout", type=float, default=86400.0)
    follow.set_defaults(handler=command_follow)

    prime = subparsers.add_parser("prime", help="create a read-only reviewer template without a turn")
    _ = prime.add_argument("--session-dir", required=True)
    _ = prime.add_argument("--name", required=True)
    _ = prime.add_argument("--instructions-file", required=True)
    _ = prime.add_argument("--cwd", required=True)
    _ = prime.add_argument("--model", default="")
    _ = prime.add_argument("--effort", default="")
    _ = prime.add_argument("--service-tier", default="")
    _ = prime.add_argument("--task", default="")
    _ = prime.add_argument("--sandbox", choices=SANDBOX_MODES, default=READ_ONLY_SANDBOX)
    _ = prime.add_argument("--held-design", default="")
    _ = prime.add_argument("--held-role", default="")
    prime.set_defaults(handler=command_prime)

    fork = subparsers.add_parser("fork", help="create a named read-only fork of a template")
    _ = fork.add_argument("--session-dir", required=True)
    _ = fork.add_argument("--from", dest="from_name", required=True)
    _ = fork.add_argument("--name", required=True)
    fork.set_defaults(handler=command_fork)

    set_instructions = subparsers.add_parser("set-instructions", help="point an idle reviewer at a new standing prompt")
    _ = set_instructions.add_argument("--session-dir", required=True)
    _ = set_instructions.add_argument("--to", required=True)
    _ = set_instructions.add_argument("--instructions-file", required=True)
    set_instructions.set_defaults(handler=command_set_instructions)

    reset = subparsers.add_parser("reset", help="remove all turns from an idle reviewer")
    _ = reset.add_argument("--session-dir", required=True)
    _ = reset.add_argument("--to", required=True)
    reset.set_defaults(handler=command_reset)

    compact = subparsers.add_parser("compact", help="compact a finished worker")
    _ = compact.add_argument("--session-dir", required=True)
    _ = compact.add_argument("--to", required=True)
    compact.set_defaults(handler=command_compact)

    can_follow = subparsers.add_parser("can-follow", help="check whether a worker can take a follow-up")
    _ = can_follow.add_argument("--session-dir", required=True)
    _ = can_follow.add_argument("--to", required=True)
    _ = can_follow.add_argument("--role", choices=WORKER_ROLES, required=True)
    _ = can_follow.add_argument("--lens", choices=agents_log.LENSES)
    _ = can_follow.add_argument("--claim-pid", type=int, default=0)
    can_follow.set_defaults(handler=command_can_follow)

    release_follow = subparsers.add_parser("release-follow", help="release an unstarted follow claim")
    _ = release_follow.add_argument("--session-dir", required=True)
    _ = release_follow.add_argument("--to", required=True)
    _ = release_follow.add_argument("--claim-pid", type=int, required=True)
    release_follow.set_defaults(handler=command_release_follow)

    send = subparsers.add_parser(
        "send", help="queue a message for a worker; without a launcher it waits for the next dispatch")
    _ = send.add_argument("--session-dir", required=True)
    _ = send.add_argument("--to", required=True)
    send_body = send.add_mutually_exclusive_group(required=True)
    _ = send_body.add_argument("--message")
    _ = send_body.add_argument("--message-file", help="read the message from this file")
    send.set_defaults(handler=command_send)

    steer = subparsers.add_parser("steer", help="interrupt a worker's running turn")
    _ = steer.add_argument("--session-dir", required=True)
    _ = steer.add_argument("--to", required=True)
    steer_body = steer.add_mutually_exclusive_group(required=True)
    _ = steer_body.add_argument("--message")
    _ = steer_body.add_argument("--message-file", help="read the message from this file")
    steer.set_defaults(handler=command_steer)

    interrupt = subparsers.add_parser("interrupt", help="stop a worker's running turn, keeping the worker")
    _ = interrupt.add_argument("--session-dir", required=True)
    _ = interrupt.add_argument("--to", required=True)
    interrupt_body = interrupt.add_mutually_exclusive_group()
    _ = interrupt_body.add_argument("--message", default="")
    _ = interrupt_body.add_argument("--message-file", help="read the message from this file")
    interrupt.set_defaults(handler=command_interrupt)

    end = subparsers.add_parser("end", help="release a resident worker")
    _ = end.add_argument("--session-dir", required=True)
    _ = end.add_argument("--to", required=True)
    _ = end.add_argument("--expected-thread-id", default="", help="refuse if this worker now names another thread")
    _ = end.add_argument("--idle-only", action="store_true", help="refuse a running worker without changing it")
    end.set_defaults(handler=command_end)

    stop = subparsers.add_parser("stop", help="stop the session app-server")
    _ = stop.add_argument("--session-dir", required=True)
    _ = stop.add_argument("--pause", action="store_true", help="retain worker threads for a director restart")
    stop.set_defaults(handler=command_stop)

    adopt = subparsers.add_parser("adopt", help="carry an idle unit worker into this run")
    _ = adopt.add_argument("--session-dir", required=True)
    _ = adopt.add_argument("--from", dest="from_run", required=True)
    _ = adopt.add_argument("--name", required=True)
    adopt.set_defaults(handler=command_adopt)

    reopen = subparsers.add_parser("reopen", help="recover an ended or abandoned worker on its recorded thread")
    _ = reopen.add_argument("--session-dir", required=True)
    _ = reopen.add_argument("--to", required=True)
    reopen.set_defaults(handler=command_reopen)

    idle_stop = subparsers.add_parser("idle-stop", help="stop this session's app-server if nothing uses it")
    _ = idle_stop.add_argument("--session-dir", required=True)
    _ = idle_stop.add_argument("--pid", type=int, required=True)
    _ = idle_stop.add_argument("--port", type=int, required=True)
    idle_stop.set_defaults(handler=command_idle_stop)

    sweep = subparsers.add_parser("sweep", help="report app-servers nothing is using")
    _ = sweep.add_argument("--root", default="")
    _ = sweep.add_argument("--stop", action="store_true", help="stop each server two looks in a row call unused")
    sweep.set_defaults(handler=command_sweep)

    signin_changed = subparsers.add_parser(
        "signin-changed", help="move every run off an app-server older than the Codex sign-in")
    _ = signin_changed.add_argument("--root", default="")
    signin_changed.set_defaults(handler=command_signin_changed)

    roster = subparsers.add_parser("list", help="print the worker roster")
    _ = roster.add_argument("--session-dir", required=True)
    roster.set_defaults(handler=command_list)

    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", _attr(args, "handler"))
    try:
        return handler(args)
    except (RoleMapUnavailable, RoleAccountUnavailable) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
