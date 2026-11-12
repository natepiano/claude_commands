"""Start, observe, and close staff workers through proved native panes."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import io
import json
import shlex
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Event
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, redirect_stderr
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, Literal, cast, final

from scripts.herdr import herdr, pane_launch
from scripts.herdr import herdr_types
from scripts.lib import conf, models_registry, nix_env, unit_runs
from scripts.production import staff, worker_admission
from scripts.agents import detached_run_process, worker_lifecycle
from scripts.direct import recorder_state

if TYPE_CHECKING:
    from scripts.accounts import accounts

StaffId = staff.StaffId
WorkerName = staff.WorkerName
WorkerRole = staff.WorkerRole

WORKER_SANDBOX = "danger-full-access"
READ_ONLY_SANDBOX = "read-only"


@dataclass(frozen=True)
class StaffWorker:
    parent: StaffId
    name: WorkerName
    role: WorkerRole


@dataclass(frozen=True)
class ReplyToCaller:
    """Send the answer to the caller with the message command."""


@dataclass(frozen=True)
class TranscriptReply:
    """Read the answer from the consultation transcript."""


REPLY_TO_CALLER = ReplyToCaller()


@dataclass(frozen=True)
class ConsultationPane:
    label: str
    cwd: Path
    question: str = ""
    delivery: ReplyToCaller | TranscriptReply = REPLY_TO_CALLER


@dataclass(frozen=True)
class DurableDesignRole:
    parent: StaffId
    name: WorkerName
    role: WorkerRole


PaneTarget = StaffWorker | ConsultationPane | DurableDesignRole


@dataclass(frozen=True)
class DispatchRun:
    directory: Path


class HolderRun(Enum):
    CURRENT = "holder's run"


@dataclass(frozen=True)
class CreatedWorkerPane:
    """This start created the pane with this terminal."""

    terminal: str


@dataclass(frozen=True)
class NoCreatedWorkerPane:
    """This start owns no new pane."""


NO_CREATED_WORKER_PANE = NoCreatedWorkerPane()


@dataclass(frozen=True)
class OpenWorker:
    row: staff.StaffRow
    pane: str
    terminal: str
    status: herdr.AgentStatus
    created_pane: CreatedWorkerPane | NoCreatedWorkerPane = field(default=NO_CREATED_WORKER_PANE, compare=False)


@dataclass(frozen=True)
class StoppedWorker:
    row: staff.StaffRow
    pane: str
    terminal: str


@dataclass(frozen=True)
class ClosedWorker:
    row: staff.StaffRow


@dataclass(frozen=True)
class UncertainWorker:
    row: staff.StaffRow
    reason: str


@dataclass(frozen=True)
class ProcessUnknownWorker:
    row: staff.StaffRow
    pane: str
    terminal: str
    reason: str
    created_pane: CreatedWorkerPane | NoCreatedWorkerPane = field(default=NO_CREATED_WORKER_PANE, compare=False)


@dataclass(frozen=True)
class AdmissionRefused:
    reason: str


@dataclass(frozen=True)
class IncompleteStart:
    row: staff.StaffRow
    pane: str
    reason: str
    created_pane: CreatedWorkerPane | NoCreatedWorkerPane = field(default=NO_CREATED_WORKER_PANE, compare=False)


class UnpublishedCleanup(Enum):
    NOT_STARTED = "pane {pane} retained; this call did not start it"
    PANE_CLOSED = "pane {pane} closed"
    PANE_CLOSE_FAILED = "pane {pane} close failed"
    AGENT_STOPPED = "agent stopped; existing pane {pane} retained at its shell"
    AGENT_STOP_FAILED = "agent stop failed; existing pane {pane} retained"


@dataclass(frozen=True)
class UnpublishedWorker:
    pane: str
    cause: str
    cleanup: UnpublishedCleanup = UnpublishedCleanup.NOT_STARTED


@dataclass(frozen=True)
class DestinationAfterSourceRelease:
    """Select the switch destination after the source pane closes."""


class StartRefusal(Enum):
    UNKNOWN_ROLE = "unknown role"
    ROLE_UNAVAILABLE = "dispatch role unavailable"
    INVALID_ROLE_CONFIGURATION = "invalid role configuration"
    BUSY_ROLE_CHANGE = "role change blocked by active work"
    ACCOUNT_UNAVAILABLE = "account unavailable"
    PANE_NOT_AT_SHELL = "start requires a stopped pane"


@dataclass(frozen=True)
class StartRefused:
    reason: StartRefusal
    detail: str


WorkerPane = OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker
WorkerStart = OpenWorker | IncompleteStart | AdmissionRefused | UnpublishedWorker | ProcessUnknownWorker | UncertainWorker | StartRefused


@dataclass(frozen=True)
class Closed:
    """The requested close is complete."""


@dataclass(frozen=True)
class CloseAllowed:
    """The close checks permit closure; the pane remains open."""


@dataclass(frozen=True)
class Kept:
    blocker: str


@dataclass(frozen=True)
class WorkerRoleUnavailable:
    reason: str


class ExitCode(IntEnum):
    OK = 0
    REFUSED = 2
    NO_DONE = 3
    BLOCKED = 4
    STALLED = 5
    BUSY = 6
    KEPT = 7
    UNCERTAIN = 8


class ConfiguredWorkerAccount(Enum):
    CURRENT = "configured account"


@dataclass(frozen=True)
class DestinationWorkerAccount:
    label: str


class WorkerLockAtUse(Enum):
    ACQUIRE = "acquire the worker lock"


def _worker_lock_path(key: str) -> Path:
    return staff.record_path().parent / "locks" / f"{key}.lock"


@final
class WorkerLockHold:
    """Hold the worker's fixed-id lock through startup and dispatch."""

    def __init__(self, parent: StaffId, name: WorkerName, operations: ExitStack, *, blocking: bool) -> None:
        self.parent = parent
        self.name = name
        self._operations = operations
        self._held: set[StaffId] = set()
        self._blocking = blocking
        self.active = True

    def bind(self, row: staff.StaffRow) -> None:
        """Acquire the fixed-id lock before creation serialization ends."""
        if not self.active or not row.reports_to(self.parent) or row.name != self.name.value:
            raise ValueError("worker lock does not own this worker")
        if row.id not in self._held:
            path = _worker_lock_path(row.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._operations.enter_context(path.open("a"))
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if self._blocking else fcntl.LOCK_NB))
            self._held.add(row.id)

    def owns(self, row: staff.StaffRow) -> bool:
        return self.active and row.id in self._held


@contextmanager
def worker_lock(parent: StaffId, name: WorkerName, *, blocking: bool = False) -> Generator[WorkerLockHold | Kept]:
    """Serialize creation, then hold the published worker's fixed-id lock."""
    with ExitStack() as operations:
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        if row is None:
            path = _worker_lock_path(f"{parent}-{name.value}")
            path.parent.mkdir(parents=True, exist_ok=True)
            creation = operations.enter_context(path.open("a"))
            try:
                fcntl.flock(creation, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield Kept("worker creation lock is held")
                return
            row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        hold = WorkerLockHold(parent, name, operations, blocking=blocking)
        try:
            if row is not None:
                hold.bind(row)
        except BlockingIOError:
            yield Kept("dispatch lock is held")
            return
        try:
            yield hold
        finally:
            hold.active = False


class UseRolePolicy(Enum):
    CURRENT = "resolve the role policy"


@dataclass(frozen=True)
class LaunchPolicy:
    settings: models_registry.LaunchSettings
    sandbox: str
    permission: str


@dataclass(frozen=True)
class PaneEvidence:
    panes: list[dict[str, object]]
    scan: herdr.RowPaneScan
    states: list[WorkerPane]
    layout_terminals: frozenset[str]


@dataclass(frozen=True)
class SelectedWorkers:
    """Judge only these workers from one complete pane read."""

    names: frozenset[str]


@dataclass(frozen=True)
class SelectedWorkerRows:
    """Judge these fixed rows and retain incomplete absence evidence."""

    ids: frozenset[StaffId]


class AllWorkers(Enum):
    CURRENT = "all workers"


@dataclass(frozen=True)
class LaunchRecords:
    entries: dict[str, pane_launch.PaneLaunch]


@dataclass(frozen=True)
class LaunchRecordsUnavailable:
    reason: str


@dataclass(frozen=True)
class PaneRead:
    panes: list[dict[str, object]]
    scan: herdr.RowPaneScan
    statuses: dict[str, herdr.AgentStatus]
    launches: LaunchRecords | LaunchRecordsUnavailable


@dataclass(frozen=True)
class PaneReadUnavailable:
    reason: str


def _client() -> herdr.Herdr:
    return herdr.Herdr(herdr.shared_session())


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _uncertain(row: staff.StaffRow, reason: str) -> UncertainWorker:
    print(f"anomaly: {row.name}: {' '.join(reason.splitlines())}; correct the evidence and retry", file=sys.stderr)
    return UncertainWorker(row, reason)


def agent_name(owner: staff.StaffRow, name: str) -> str:
    """Use the owner name to distinguish workers on the shared server."""
    prefix = f"{owner.name}-"
    return name if name.startswith(prefix) else prefix + name


def rename_workers(owner: staff.StaffRow, client: herdr.Herdr) -> None:
    """Rename worker panes before the owner name changes."""
    record = staff.snapshot()
    if not record.find(kind="worker", parent=owner.id):
        return
    for observed in _evidence(owner.id, record, client).states:
        if isinstance(observed, (OpenWorker, StoppedWorker)):
            name = agent_name(owner, observed.row.name)
            if isinstance(observed, OpenWorker):
                _ = client.run("agent", "rename", observed.pane, name)
            _ = client.retitle(observed.pane, name, tab=False)


def agent_inventory(client: herdr.Herdr) -> list[herdr.AgentOccupant]:
    """Read agent status for worker and freestanding displays."""
    return client.agents()


def _stop_row(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
    stop = cast(Callable[..., bool], import_module("scripts.agents.restart").stop_row)
    return stop(row, client, deadline=deadline)


def _read_panes(record: staff.StaffSnapshot, client: herdr.Herdr) -> PaneRead | PaneReadUnavailable:
    try:
        panes = [dict(pane) for pane in client.panes()]
        scan = client.scan_row_panes(record, panes)
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return PaneReadUnavailable(str(error))
    launches: LaunchRecords | LaunchRecordsUnavailable
    try:
        launches = LaunchRecords(pane_launch.read_valid_entries(
            pane_launch.record_directory() / f"{client.session}.json", record=record))
    except (OSError, ValueError) as error:
        launches = LaunchRecordsUnavailable(f"launch records are unavailable: {error}")
    try:
        statuses = {agent["pane_id"]: agent["agent_status"] for agent in agent_inventory(client)}
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
        statuses = {}
    return PaneRead(panes, scan, statuses, launches)


def _parent_evidence(parent: StaffId, record: staff.StaffSnapshot,
                     read: PaneRead | PaneReadUnavailable, *,
                     selection: SelectedWorkers | SelectedWorkerRows | AllWorkers = AllWorkers.CURRENT) -> PaneEvidence:
    workers = record.find(kind="worker", parent=parent)
    selected = [row for row in workers if isinstance(selection, AllWorkers)
                or isinstance(selection, SelectedWorkers) and row.name in selection.names
                or isinstance(selection, SelectedWorkerRows) and row.id in selection.ids]
    if isinstance(read, PaneReadUnavailable):
        return PaneEvidence([], herdr.RowPaneScan({}, []), [_uncertain(row, read.reason) for row in selected], frozenset())
    panes, scan, statuses = read.panes, read.scan, read.statuses
    launch_owners = ({pane: reference.id for pane, entry in read.launches.entries.items()
                      if isinstance(reference := pane_launch.launch_reference(entry, record), pane_launch.StaffLaunchReference)}
                     if isinstance(read.launches, LaunchRecords) else {})
    parent_panes = scan.found.get(parent, []) + [pane for pane in panes
        if launch_owners.get(_text(pane, "pane_id")) == parent
        and not any(pane in found for owner, found in scan.found.items() if owner != parent)]
    parent_tabs = {_text(pane, "tab_id") for pane in parent_panes} - {""}
    states: list[WorkerPane] = []
    for row in selected:
        matches = scan.found.get(row.id, [])
        if len(matches) > 1:
            states.append(_uncertain(row, "duplicate native identities; panes retained"))
            continue
        if matches:
            pane = matches[0]
            identity, terminal = _text(pane, "pane_id"), _text(pane, "terminal_id")
            conflicts = [key for key, found in scan.found.items() if key != row.id and pane in found]
            if not terminal or sum(_text(item, "terminal_id") == terminal for item in panes) != 1 or conflicts:
                states.append(_uncertain(row, "terminal identity is missing, duplicated, or shared"))
            else:
                states.append(OpenWorker(row, identity, terminal, statuses.get(identity, herdr.AgentStatus.UNKNOWN)))
            continue
        if isinstance(read.launches, LaunchRecordsUnavailable):
            states.append(_uncertain(row, read.launches.reason))
            continue
        recovery = [pane for pane in panes
                    if launch_owners.get(_text(pane, "pane_id")) == row.id
                    and _text(pane, "tab_id") in parent_tabs
                    and not any(pane in scan.found.get(sibling.id, []) for sibling in workers)]
        related_unknown = [item for item in scan.unknown if any(
            _text(pane, "pane_id") == item.pane for pane in recovery)]
        if related_unknown:
            unknown = related_unknown[0]
            location = next(pane for pane in recovery if _text(pane, "pane_id") == unknown.pane)
            states.append(ProcessUnknownWorker(row, unknown.pane, _text(location, "terminal_id"), unknown.reason))
        elif len(recovery) > 1:
            states.append(_uncertain(row, "multiple recovery panes"))
        elif recovery:
            pane = recovery[0]
            identity, terminal = _text(pane, "pane_id"), _text(pane, "terminal_id")
            if not terminal or sum(_text(item, "terminal_id") == terminal for item in panes) != 1:
                states.append(_uncertain(row, "recovery terminal identity is missing or duplicated"))
                continue
            owners = [key for key, found in scan.found.items() if pane in found]
            if owners:
                states.append(_uncertain(row, "recovery pane belongs to another native session"))
                continue
            states.append(StoppedWorker(row, identity, terminal))
        elif not isinstance(selection, SelectedWorkers) and any(item.tab in parent_tabs for item in scan.unknown):
            states.append(_uncertain(row, "pane evidence is incomplete; absence is unproved"))
        else:
            states.append(ClosedWorker(row))
    terminals: frozenset[str] = frozenset()
    if isinstance(selection, SelectedWorkers):
        worker_ids = {row.id for row in workers}
        terminals = frozenset(_text(pane, "terminal_id") for pane in panes
            if (launch_owners.get(_text(pane, "pane_id")) in worker_ids
                or any(pane in scan.found.get(row.id, []) for row in workers))
            and _text(pane, "tab_id") in parent_tabs)
    return PaneEvidence(panes, scan, states, terminals)


def _evidence(parent: StaffId, record: staff.StaffSnapshot, client: herdr.Herdr, *,
              selection: SelectedWorkers | AllWorkers = AllWorkers.CURRENT) -> PaneEvidence:
    return _parent_evidence(parent, record, _read_panes(record, client), selection=selection)


class WorkerNotObserved(Enum):
    CURRENT = "the worker has no observation"


def observe_selected(parent: StaffId, record: staff.StaffSnapshot, names: Sequence[WorkerName]) -> list[WorkerPane]:
    """Judge the requested rows from one complete identity scan."""
    selection = SelectedWorkers(frozenset(name.value for name in names))
    return _evidence(parent, record, _client(), selection=selection).states


def observe_worker(parent: StaffId, row: staff.StaffRow, record: staff.StaffSnapshot) -> WorkerPane | WorkerNotObserved:
    """Read all pane identities and judge the selected worker."""
    current = record.by_id(row.id)
    if current is None:
        return WorkerNotObserved.CURRENT
    states = observe_selected(parent, record, [WorkerName.parse(current.name)])
    return next((state for state in states if state.row.id == current.id), WorkerNotObserved.CURRENT)


def observe(parent: StaffId, record: staff.StaffSnapshot) -> list[WorkerPane]:
    return _evidence(parent, record, _client()).states


def observe_parents(parents: Sequence[staff.StaffRow], record: staff.StaffSnapshot, *,
                    selection: SelectedWorkerRows | AllWorkers = AllWorkers.CURRENT) -> dict[StaffId, list[WorkerPane]]:
    """Judge the selected workers of these parents from one pane read."""
    if not parents:
        return {}
    client = _client()
    read = _read_panes(record, client)
    return {parent.id: _parent_evidence(parent.id, record, read, selection=selection).states for parent in parents}


def worker_state(observed: WorkerPane) -> Literal["idle", "busy", "unknown"]:
    """Use one activity rule for staff workers and held roles."""
    if isinstance(observed, OpenWorker):
        return "idle" if observed.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE) else "unknown" if observed.status == herdr.AgentStatus.UNKNOWN else "busy"
    if isinstance(observed, (StoppedWorker, ClosedWorker)):
        return "idle"
    return "unknown"


def labelled_pane(panes: Sequence[Mapping[str, object]], tab: str, label: str, *,
                  exclude: str = "") -> Mapping[str, object] | herdr.NoPane:
    """Prove one labelled pane and its unique terminal in the requested tab."""
    matches = [pane for pane in panes if pane.get("pane_id") != exclude
               and pane.get("tab_id") == tab and pane.get("label") == label]
    if not matches:
        return herdr.NoPane(label)
    if len(matches) != 1:
        raise ValueError(f"{label}: duplicate labelled panes")
    pane = matches[0]
    terminal = pane.get("terminal_id")
    if not terminal or sum(item.get("terminal_id") == terminal for item in panes) != 1:
        raise ValueError(f"{label}: terminal identity is missing or duplicated")
    return pane


def worker_fallback_run(parent: staff.StaffRow) -> Path:
    """Use the inherited run or the parent's local worker directory."""
    current = unit_runs.environ("RUN_DIR")
    return Path(current) if current else unit_runs.state_dir() / "staff" / "workers" / parent.id


def _worker_run_scan(record: staff.StaffSnapshot) -> dict[str, list[str]]:
    """Read the launcher's run roots once for all parent sessions."""
    design_root = cast(Callable[[], Path], import_module("scripts.astra.design_roles").state_root)()
    return unit_runs.held_by_session(os.environ.get("ROSTER_RUN_ROOT"), record=record,
                                     additional_roots=(unit_runs.ASTRA_RUN_ROOT, design_root))


def worker_run_directories(record: staff.StaffSnapshot) -> unit_runs.LauncherRunDirectories:
    """Resolve each worker through the launcher and retain its candidate directories."""
    workers = record.find(kind="worker")
    if not workers:
        return unit_runs.LauncherRunDirectories({}, {})
    scanned = _worker_run_scan(record)
    found: dict[StaffId, tuple[Path, ...]] = {}
    for row in workers:
        parent = record.parent_of(row) or row
        selected = worker_run(row, record=record, scanned=scanned)
        held = unit_runs.held(str(parent.current_session), record=record, scanned=scanned)
        recorded = (parent.run.path,) if isinstance(parent.run, staff.OpenRun) else ()
        found[row.id] = tuple(dict.fromkeys((selected, *(Path(directory) for directory in held),
                                            *recorded, worker_fallback_run(parent))))
    return unit_runs.LauncherRunDirectories(found, scanned)


def worker_run(member: staff.StaffRow, *,
               record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL,
               scanned: dict[str, list[str]] | None = None) -> Path:
    """Use the recorded dispatch state before searching the parent's held runs."""
    parent = member
    if member.kind == "worker":
        from scripts.direct import progress_history

        dispatched = progress_history.recorded_dispatch_run(member.id)
        if isinstance(dispatched, progress_history.RecordedDispatchRun):
            return dispatched.directory
        record = staff.local_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
        parent = record.parent_of(member) or member
    if scanned is None:
        record = staff.local_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
        scanned = _worker_run_scan(record)
    runs = unit_runs.held(str(parent.current_session), record=record, scanned=scanned)
    fallback = Path(runs[0]) if runs else worker_fallback_run(parent)
    if member.kind == "worker":
        candidates = dict.fromkeys((*(Path(run) for run in runs), fallback))
        passes = [(run, passed) for run in candidates if (passed := worker_pass(member, run))]
        if passes:
            return max(passes, key=lambda item: _pass_started_at(item[1]))[0]
    return fallback


def _passes(run: Path) -> list[dict[str, object]]:
    path = run / recorder_state.STATE_FILENAME
    if not path.exists():
        return []
    state = worker_lifecycle.read_json_object(path)
    value = state.get("pass", {})
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    return [fields] if "instance_id" in fields else [cast(dict[str, object], item)
        for item in fields.values() if isinstance(item, dict)]


def _pass_started_at(item: dict[str, object]) -> float:
    value = item.get("started_at")
    return value if isinstance(value, (int, float)) else float("-inf")


def worker_pass(row: staff.StaffRow, run: Path) -> dict[str, object]:
    no_pass: dict[str, object] = {}
    return max((item for item in reversed(_passes(run))
                if item.get("worker_id") == row.id or item.get("worker_name") == row.name
                or (not item.get("worker_id") and not item.get("worker_name") and item.get("team_slot") == row.name)),
               key=_pass_started_at, default=no_pass)


def dispatch_role(row: staff.StaffRow) -> WorkerRole | WorkerRoleUnavailable:
    """Read the worker role from its staff row."""
    return row.role.value if isinstance(row.role, staff.KnownWorkerRole) else WorkerRoleUnavailable("dispatch role is unavailable")


def _transcript_messages(row: staff.StaffRow) -> list[str] | Kept:
    try:
        backend = staff.backend(row)
        if isinstance(backend, staff.BackendUnavailable):
            return Kept("worker transcript backend is unavailable")
        source = worker_admission.transcript_source(row, backend)
        entries = list(worker_admission.transcript_entries(source, set()))
        if any(isinstance(entry, worker_admission.TornTranscriptRecord) for entry in entries):
            return Kept("worker transcript has incomplete records")
        return [entry for entry in entries if isinstance(entry, str) and entry]
    except (OSError, ValueError) as error:
        return Kept(f"completion evidence is unavailable: {error}")


def _completed_turn(row: staff.StaffRow, dispatch: object) -> CloseAllowed | Kept:
    messages = _transcript_messages(row)
    if isinstance(messages, Kept):
        return messages
    if (not isinstance(dispatch, str) or not dispatch or not messages
            or not messages[-1].startswith("assistant:\n")
            or not any(worker_admission.reply_ends_dispatch(messages[-1], identity)
                       for identity in (dispatch, stage_two_completion_id(dispatch)))):
        return Kept("last turn does not end with the dispatch DONE line")
    return CloseAllowed()


def _final_reply(row: staff.StaffRow) -> CloseAllowed | Kept:
    messages = _transcript_messages(row)
    if isinstance(messages, Kept):
        return messages
    if not messages or not messages[-1].startswith("assistant:\n"):
        return Kept("last turn does not end with an assistant reply")
    return CloseAllowed()


@dataclass(frozen=True)
class DispatchHoldsWorker:
    blocker: str


@dataclass(frozen=True)
class CompletedWorkerPass:
    dispatch_id: str


class ReleasedWorkerPass(Enum):
    NO_PROMPT_DELIVERED = "no prompt was delivered"
    ENDED = "the pass ended"
    STALE_ACTIVE = "the launcher ended without finishing the pass"


WorkerDispatchState = DispatchHoldsWorker | CompletedWorkerPass | ReleasedWorkerPass


def check_worker_lock(row: staff.StaffRow,
                      lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> CloseAllowed | Kept:
    """Check the worker lock without creating a file."""
    if not isinstance(lock, WorkerLockHold) or not lock.owns(row):
        path = _worker_lock_path(row.id)
        try:
            with path.open("r") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return Kept("dispatch lock is held")
        except FileNotFoundError:
            pass
    return CloseAllowed()


class RunningDetachedWorkers(Protocol):
    @property
    def staff_ids(self) -> frozenset[staff.StaffId]: ...


class DetachedWorkAtUse(Enum):
    READ = "read detached runs for this worker"


def worker_dispatch_state(row: staff.StaffRow, run: Path,
                          lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE, *,
                          detached: RunningDetachedWorkers | DetachedWorkAtUse = DetachedWorkAtUse.READ) -> WorkerDispatchState:
    """Classify the pass through its status, worker lock, submission, and detached runs."""
    from scripts.direct import progress_history

    locked = check_worker_lock(row, lock)
    if isinstance(locked, Kept):
        return DispatchHoldsWorker(locked.blocker)
    if isinstance(detached, DetachedWorkAtUse):
        live_runs = cast(Callable[[Path, str], Sequence[object]], import_module("scripts.agents.detached_runs").live_runs)
        live = bool(live_runs(run / "detached_runs", row.name))
    else:
        live = row.id in detached.staff_ids
    if live:
        return DispatchHoldsWorker("a detached run is live")
    passed = worker_pass(row, run)
    if passed:
        status, dispatch = passed.get("status"), passed.get("instance_id")
    else:
        latest = progress_history.last_dispatch(row.id)
        if (not isinstance(latest, progress_history.RecordedDispatch)
                or latest.status is progress_history.DispatchDetailNotRecorded.UNRECORDED):
            return ReleasedWorkerPass.NO_PROMPT_DELIVERED
        status, dispatch = latest.status, latest.instance_id
    if status == "completed":
        completion = progress_history.hook_completion(passed)
        if isinstance(completion, progress_history.HookCompletion) and completion.outcome == "done":
            return ReleasedWorkerPass.ENDED
        return CompletedWorkerPass(dispatch if isinstance(dispatch, str) else "")
    if status == "active":
        if progress_history.dispatch_delivery(passed).line:
            if isinstance(_completed_turn(row, dispatch), CloseAllowed):
                return CompletedWorkerPass(dispatch if isinstance(dispatch, str) else "")
            return DispatchHoldsWorker("submitted pass awaits completion")
        pid, command = passed.get("launcher_pid"), passed.get("launcher_command")
        if isinstance(pid, int) and isinstance(command, list):
            parts = cast(list[object], command)
            if len(parts) > 1 and all(isinstance(part, str) for part in parts):
                saved = cast(list[str], parts)
                launcher = [str(Path(saved[1]).with_suffix(".py")), *saved[2:]]
                if detached_run_process.runs_command(pid, launcher):
                    return DispatchHoldsWorker("launcher is live")
        return ReleasedWorkerPass.STALE_ACTIVE
    if status in ("", "error", "refused"):
        return ReleasedWorkerPass.NO_PROMPT_DELIVERED
    if status in ("canceled", "interrupted"):
        return ReleasedWorkerPass.ENDED
    return DispatchHoldsWorker("launcher has not recorded a completed pass")


def _idle_checks(row: staff.StaffRow, run: Path,
                 lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> CloseAllowed | Kept:
    state = worker_dispatch_state(row, run, lock)
    return Kept(state.blocker) if isinstance(state, DispatchHoldsWorker) else CloseAllowed()


def stage_two_completion_id(dispatch_id: str) -> str:
    """Return the completion id for the second review stage."""
    return dispatch_id + "-2"


def can_close(worker: OpenWorker | StoppedWorker | ClosedWorker, *,
              run: DispatchRun | HolderRun = HolderRun.CURRENT,
              lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> CloseAllowed | Kept:
    record = staff.snapshot()
    parent = record.parent_of(worker.row)
    if parent is None:
        return Kept("worker parent is unavailable")
    directory = run.directory if isinstance(run, DispatchRun) else worker_run(worker.row, record=record)
    if isinstance(worker, (StoppedWorker, ClosedWorker)):
        return _idle_checks(worker.row, directory, lock)
    state = worker_dispatch_state(worker.row, directory, lock)
    if isinstance(state, DispatchHoldsWorker):
        return Kept(state.blocker)
    if isinstance(state, CompletedWorkerPass):
        completion = _completed_turn(worker.row, state.dispatch_id)
    elif state is ReleasedWorkerPass.ENDED:
        from scripts.direct import progress_history

        passed = worker_pass(worker.row, directory)
        hook = progress_history.hook_completion(passed)
        if isinstance(hook, progress_history.HookCompletion) and hook.outcome == "done":
            completion = _completed_turn(worker.row, passed.get("instance_id"))
        else:
            return _final_reply(worker.row)
    else:
        return _final_reply(worker.row)
    if worker.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
        if isinstance(completion, Kept):
            return Kept(f"agent state is {worker.status.value}")
    return completion


def launch_policy(role: WorkerRole) -> LaunchPolicy:
    from scripts.astra import design_roles
    section, key = ("design_roles", role.removeprefix("astra-")) if role.startswith("astra-") else ("direct", role)
    settings = models_registry.launch_settings(section, key)
    sandbox = READ_ONLY_SANDBOX if role in ("review", "astra-reviewer") else WORKER_SANDBOX
    if role == "astra-designer":
        sandbox = design_roles.DESIGNER_SANDBOX
    return _launch_policy(settings, sandbox)


def astra_review_policy(model: str, effort: str) -> LaunchPolicy:
    """Use the supplied review settings with fixed read-only Codex access."""
    settings = models_registry.LaunchSettings("codex", model, effort, "")
    return LaunchPolicy(settings, READ_ONLY_SANDBOX, "plan")


def worker_policy(settings: models_registry.LaunchSettings) -> LaunchPolicy:
    """Use the worker's file permissions with the supplied model settings."""
    return _launch_policy(settings, WORKER_SANDBOX)


def _launch_policy(settings: models_registry.LaunchSettings, sandbox: str) -> LaunchPolicy:
    permission = "plan" if sandbox == READ_ONLY_SANDBOX else "bypassPermissions"
    return LaunchPolicy(settings, sandbox, permission)


def turn_end_command() -> list[str]:
    """Build the shared turn handler command for the Claude launch settings."""
    scripts = Path(__file__).resolve().parents[1]
    return [str(scripts / "lib/py"), str(scripts / "herdr/turn_end.py")]


def _turn_end_arguments(kind: Literal["codex", "claude"]) -> list[str]:
    if kind == "codex":
        return []
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": shlex.join(turn_end_command()), "async": True}]}]}}
    return ["--settings", json.dumps(hooks, separators=(",", ":"))]


def _policy_arguments(policy: LaunchPolicy) -> list[str]:
    settings = policy.settings
    if settings.kind == "codex":
        arguments = ["--model", settings.model, "--sandbox", policy.sandbox, "--ask-for-approval", "never",
                      "-c", f'model_reasoning_effort="{settings.effort}"',
                      "-c", "notice.hide_rate_limit_model_nudge=true"]
        if settings.service_tier:
            arguments += ["-c", f'service_tier="{settings.service_tier}"']
    else:
        arguments = ["--model", settings.model, "--permission-mode", policy.permission]
        if settings.effort and settings.effort != "unset":
            arguments += ["--effort", settings.effort]
    return arguments + _turn_end_arguments(settings.kind)


def launch_arguments(policy: LaunchPolicy, identity: str, directory: Path | str) -> list[str]:
    if policy.settings.kind == "codex":
        launch = (["resume", identity] if identity else []) + ["--no-daemon"]
        if identity:
            launch += ["--cd", str(directory)]
    else:
        launch = ["--resume", identity] if identity else []
    return launch + _policy_arguments(policy)


def launch_environment(policy: LaunchPolicy, account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection,
                         resume: bool) -> dict[str, str] | StartRefused:
    from scripts.accounts import accounts
    from scripts.whoami import account as account_reader, agent_accounts
    try:
        if policy.settings.kind == "codex":
            if isinstance(account, accounts.CodexWorkersAccount):
                return {"CODEX_HOME": str(account.home)}
        elif isinstance(account, accounts.NamedAccount):
            return account_reader.named_account_environment(account.dir)
        if isinstance(account, (accounts.CodexWorkersAccount, accounts.NamedAccount)):
            family = "codex" if isinstance(account, accounts.CodexWorkersAccount) else "claude"
            print(f"anomaly: {account.label}: account family {family} does not match the {policy.settings.kind} worker; "
                  + "using the configured account", file=sys.stderr)
            account = ConfiguredWorkerAccount.CURRENT
        if policy.settings.kind == "codex":
            selected = (agent_accounts.codex_account_home(account.label) if isinstance(account, DestinationWorkerAccount)
                        else accounts.role_account("workers"))
            if isinstance(selected, agent_accounts.CodexSetupHome):
                return StartRefused(StartRefusal.ACCOUNT_UNAVAILABLE, f"{selected.label}: account is not ready")
            return {"CODEX_HOME": str(selected.home)}
        if isinstance(account, DestinationWorkerAccount):
            readiness = accounts.account_directory_readiness(account.label)
            if not isinstance(readiness, accounts.AccountDirectoryReady):
                return StartRefused(StartRefusal.ACCOUNT_UNAVAILABLE, "; ".join(readiness.problems))
            selected_claude = accounts.prepare_claude_launch(account.label, resume=resume)
            return selected_claude.variables
        selected_claude_account = accounts.account_for_unscoped_launch(os.environ, workers=True)
        return (account_reader.named_account_environment(selected_claude_account.dir)
                if isinstance(selected_claude_account, accounts.NamedAccount) else {})
    except (OSError, ValueError) as error:
        return StartRefused(StartRefusal.ACCOUNT_UNAVAILABLE, str(error))


def _live_policy(worker: OpenWorker, policy: LaunchPolicy, client: herdr.Herdr, *, same_role: bool) -> bool | UncertainWorker:
    try:
        info = herdr.json_object(client.result("pane", "process-info", "--pane", worker.pane).get("process_info"), "worker launch policy")
        commands = [herdr.json_object(item, "worker command") for item in herdr.json_items(info.get("foreground_processes"), "worker commands")]
        arguments = [cast(list[str], item["argv"]) for item in commands if isinstance(item.get("argv"), list)
                     and all(isinstance(arg, str) for arg in cast(list[object], item["argv"]))]
        candidates = [args for args in arguments if any(Path(arg).name in ("codex", "claude") for arg in args)]
        if len(candidates) != 1:
            return _uncertain(worker.row, "running launch policy is unproved")
        expected = _policy_arguments(policy)
        actual = candidates[0]
        required = ("--model", "--sandbox", "--ask-for-approval") if policy.settings.kind == "codex" else ("--model", "--permission-mode")
        if any(flag not in actual for flag in required):
            return _uncertain(worker.row, "running launch policy is incomplete")
        pairs = [actual[pos:pos + 2] for pos in range(len(actual) - 1)]
        same_permissions = (["--sandbox", policy.sandbox] in pairs and ["--ask-for-approval", "never"] in pairs
                            if policy.settings.kind == "codex" else ["--permission-mode", policy.permission] in pairs)
        if same_permissions and same_role:
            hook_arguments = _turn_end_arguments(policy.settings.kind)
            return not hook_arguments or hook_arguments in pairs
        if policy.settings.kind == "claude" and ["--model", policy.settings.model] not in pairs:
            launched = herdr.pane_native_process(info, "claude", {})
            expected = models_registry.claude_arguments(expected, launched.environment)
        return all(expected[index:index + 2] in [actual[pos:pos + 2] for pos in range(len(actual) - 1)]
                   for index in range(0, len(expected), 2))
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return _uncertain(worker.row, f"running launch policy is unproved: {error}")


def _interrupted_pane(parent: staff.StaffRow, name: str, evidence: PaneEvidence,
                      client: herdr.Herdr) -> Mapping[str, object] | herdr.NoPane | StartRefused | UncertainWorker:
    """Recover an interrupted start only when no record or native staff owner claims its pane."""
    parents = evidence.scan.found.get(parent.id, [])
    tab = _text(parents[0], "tab_id") if len(parents) == 1 else ""
    if not tab:
        return herdr.NoPane(name)
    try:
        for label in (agent_name(parent, name), name):
            pane = labelled_pane(evidence.panes, tab, label)
            if isinstance(pane, herdr.NoPane):
                continue
            if any(pane in found for found in evidence.scan.found.values()):
                return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "recovery pane holds another live staff session")
            records = pane_launch.read_entries(pane_launch.record_directory() / f"{client.session}.json")
            if str(pane["pane_id"]) not in records:
                return pane
    except (OSError, ValueError) as error:
        return _uncertain(parent, str(error))
    return herdr.NoPane(name)


def _destination(parent: staff.StaffRow, name: str, evidence: PaneEvidence,
                 client: herdr.Herdr) -> herdr.SplitPane | herdr.ExistingPane | StartRefused | UncertainWorker:
    parents = evidence.scan.found.get(parent.id, [])
    if len(parents) != 1:
        return _uncertain(parent, "parent pane is unavailable or duplicated")
    parent_pane = parents[0]
    tab = _text(parent_pane, "tab_id")
    label = agent_name(parent, name)
    pane = _interrupted_pane(parent, name, evidence, client)
    if isinstance(pane, (StartRefused, UncertainWorker)):
        return pane
    if not isinstance(pane, herdr.NoPane):
        identity, terminal = str(pane["pane_id"]), str(pane["terminal_id"])
        if not isinstance(client.pane_activity(identity), herdr.PaneAtShell):
            return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "interrupted recovery pane is live")
        return herdr.ExistingPane(identity, terminal)
    ratio = conf.read("direct").number("WORKER_SPLIT_RATIO")
    workers = {item.pane for item in evidence.states if isinstance(item, (OpenWorker, StoppedWorker))
               and any(_text(pane, "pane_id") == item.pane and _text(pane, "tab_id") == tab for pane in evidence.panes)}
    workers.update(_text(pane, "pane_id") for pane in evidence.panes
                   if _text(pane, "terminal_id") in evidence.layout_terminals and _text(pane, "tab_id") == tab)
    if not workers:
        return herdr.SplitPane(_text(parent_pane, "pane_id"), "right", ratio, label)
    layout = herdr.json_object(client.socket_request("layout.export", tab_id=tab).get("layout"), "worker layout")
    nodes = [layout.get("root")]
    ordered: list[str] = []
    while nodes:
        node = herdr.json_object(nodes.pop(), "layout node")
        if node.get("type") == "pane":
            if (pane := _text(node, "pane_id")) in workers:
                ordered.append(pane)
        else:
            nodes.extend((node.get("second"), node.get("first")))
    if not ordered:
        return _uncertain(parent, "worker panes are absent from the split tree")
    return herdr.SplitPane(ordered[-1], "down", ratio, label)


@herdr.pane_evidence_run()
def start_or_resume(parent: StaffId, name: WorkerName, role: WorkerRole | WorkerRoleUnavailable, *,
                    account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection = ConfiguredWorkerAccount.CURRENT,
                    lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE,
                    policy: UseRolePolicy | LaunchPolicy = UseRolePolicy.CURRENT, runs: dict[str, list[str]] | None = None,
                    codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> WorkerStart:
    if isinstance(role, WorkerRoleUnavailable):
        return StartRefused(StartRefusal.ROLE_UNAVAILABLE, role.reason)
    if role not in staff.WORKER_ROLES:
        return StartRefused(StartRefusal.UNKNOWN_ROLE, str(role))
    record = staff.snapshot()
    owner = record.by_id(parent)
    if owner is None or owner.kind not in ("unit", "showrunner"):
        return AdmissionRefused("only a unit or showrunner can admit a worker")
    existing = worker_admission.find_worker(record, parent, name.value)
    run = worker_run(existing if existing is not None else owner, record=record, scanned=runs)
    if existing is not None and not isinstance(worker_dispatch_state(existing, run, lock), DispatchHoldsWorker):
        run = worker_run(owner, record=record, scanned=runs)
    allowed = worker_admission.WorkerStartAllowed(name.value, parent, run,
        worker_admission.ExistingWorkerSlot(existing) if existing is not None else worker_admission.NewWorkerSlot())
    keep_model = isinstance(policy, UseRolePolicy)
    try:
        policy = launch_policy(role) if isinstance(policy, UseRolePolicy) else policy
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(error))
    try:
        with ExitStack() as operations:
            held = operations.enter_context(worker_lock(parent, name, blocking=existing is None)) if isinstance(lock, WorkerLockAtUse) else lock
            if isinstance(held, Kept):
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, held.blocker)
            if not held.active or held.parent != parent or held.name != name:
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, "worker lock does not own this worker")
            with worker_admission.worker_operation(allowed):
                result = _start_locked(owner, name, role, policy, account, allowed, held, codex, keep_model=keep_model)
                if isinstance(result, OpenWorker) and result.row.role != staff.KnownWorkerRole(role):
                    return replace(result, row=staff.set_worker_role(result.row.id, staff.KnownWorkerRole(role)))
                return result
    except (worker_admission.AdmissionRefused, staff.StaffRefused) as error:
        return AdmissionRefused(str(error))


def first_turn_session(client: herdr.Herdr, pane: str, terminal: str) -> str:
    """Read the session report, then wait for the first Codex turn to end."""
    settings = conf.read("direct")
    text = settings.text("WORKER_FIRST_TURN_TEXT")
    seconds = settings.number("WORKER_FIRST_TURN_SECONDS")
    deadline = time.monotonic() + seconds
    client.prompt_terminal(pane, terminal, text)
    failure = "first turn timed out"
    while time.monotonic() < deadline:
        try:
            session = client.native_session(pane, "codex", herdr.PROGRAM_TIMEOUT_SECONDS)
        except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            failure = str(error)
            _ = Event().wait(max(0, min(0.1, deadline - time.monotonic())))
            continue
        remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
        if isinstance(client.wait_turn_end(pane, remaining_ms), herdr.WaitTimedOut):
            raise herdr_types.HerdrFailure("first turn timed out")
        return session
    raise herdr_types.HerdrFailure(failure)


def _codex_start_session(client: herdr.Herdr, pane: str, *,
                         pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> str:
    row = client.pane_row(pane) if isinstance(pane_row, herdr.PaneNotRead) else pane_row
    if isinstance(row, herdr.NoPane):
        raise herdr_types.HerdrFailure("Codex pane is missing")
    if row.get("session_id"):
        return client.native_session(pane, "codex", herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=row)
    return first_turn_session(client, pane, str(row.get("terminal_id", "")))



def unpublished_cleanup_text(result: UnpublishedWorker) -> str:
    return result.cleanup.value.format(pane=result.pane)


def _start_locked(owner: staff.StaffRow, name: WorkerName, role: WorkerRole, policy: LaunchPolicy,
                  account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection,
                  allowed: worker_admission.WorkerStartAllowed, lock: WorkerLockHold,
                  codex: herdr.CodexConversations | herdr.CodexLaunchRead, *, keep_model: bool) -> WorkerStart:
    started = time.monotonic()
    try:
        start_seconds = conf.read("direct").number("PANE_AGENT_START_SECONDS")
    except conf.Unusable as error:
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(error))
    deadline = started + start_seconds
    client = _client()
    record = staff.snapshot()
    selection = SelectedWorkers(frozenset({name.value}))
    evidence = _evidence(owner.id, record, client, selection=selection)
    current = next((state for state in evidence.states if state.row.name == name.value), None)
    row = current.row if current is not None else None
    backend = staff.backend(row) if row is not None else staff.BACKEND_UNAVAILABLE
    switching = not isinstance(backend, staff.BackendUnavailable) and backend.family != policy.settings.kind
    if row is not None:
        lock.bind(row)
    if isinstance(current, (ProcessUnknownWorker, UncertainWorker)):
        return current
    if current is None:
        pane = _interrupted_pane(owner, name.value, evidence, client)
        if isinstance(pane, (StartRefused, UncertainWorker)):
            return pane
        if not isinstance(pane, herdr.NoPane):
            pane = dict(pane)
            identity = _text(pane, "pane_id")
            unknown = next((item for item in evidence.scan.unknown if item.pane == identity), None)
            try:
                activity = client.pane_activity(identity)
                if unknown is not None:
                    info = herdr.json_object(client.result("pane", "process-info", "--pane", identity).get("process_info"),
                                             "recovery process info")
                    if not (policy.settings.kind == "codex" and herdr.codex_without_session(info)):
                        return ProcessUnknownWorker(owner, identity, _text(pane, "terminal_id"), unknown.reason)
            except (worker_admission.AdmissionRefused, herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                return ProcessUnknownWorker(owner, identity, _text(pane, "terminal_id"), str(error))
            if not isinstance(activity, herdr.PaneAtShell):
                try:
                    native = (_codex_start_session(client, identity, pane_row=pane) if policy.settings.kind == "codex" else
                              client.native_session(identity, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane))
                    if policy.settings.kind == "codex":
                        entry = client.codex_launch(identity)
                        row = worker_admission.publish(allowed, native,
                            launch_home=pane_launch.config_home(entry, record=record)).row
                        lock.bind(row)
                        pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", entry)
                    else:
                        row = worker_admission.publish(allowed, native).row
                        lock.bind(row)
                except (worker_admission.AdmissionRefused, herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    if row is not None:
                        return _uncertain(row, f"launch proof is incomplete: {error}")
                    return UnpublishedWorker(identity, str(error))
                recovered = _evidence(owner.id, staff.snapshot(), client, selection=selection)
                state = next((item for item in recovered.states if item.row.id == row.id), None)
                if isinstance(state, (OpenWorker, ProcessUnknownWorker, UncertainWorker)):
                    _layout(owner.id, recovered)
                    return state
                return _uncertain(row, "retained launch proof is incomplete")
    if isinstance(current, OpenWorker):
        passed = worker_pass(current.row, allowed.run)
        dispatch = worker_dispatch_state(current.row, worker_run(current.row, record=record), lock)
        if passed.get("kind") != role and isinstance(dispatch, DispatchHoldsWorker):
            return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, dispatch.blocker)
        if not switching:
            matches = _live_policy(current, policy, client, same_role=keep_model and passed.get("kind") == role)
            if isinstance(matches, UncertainWorker):
                return matches
            same_run = True
            same_account = True
            check_run = owner.kind == "unit" and not passed
            if matches:
                try:
                    pane_row = next(pane for pane in evidence.panes if pane.get("pane_id") == current.pane)
                    launched = client.native_pane_process(current.pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane_row)
                except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    if passed.get("status") != "active":
                        return _uncertain(current.row, f"running launch environment is unproved: {error}")
                else:
                    if check_run:
                        same_run = launched.environment.get("UNIT_RUN_DIR") == str(allowed.run)
                    expected = launch_environment(policy, account, True)
                    if isinstance(expected, StartRefused):
                        return expected
                    from scripts.accounts import accounts

                    key = "CODEX_HOME" if policy.settings.kind == "codex" else "CLAUDE_CONFIG_DIR"
                    default_home = str(Path.home() / ".codex" if policy.settings.kind == "codex" else accounts.main_config_dir())
                    running_home = Path(launched.environment.get(key, default_home)).expanduser().resolve()
                    selected_home = Path(expected.get(key, default_home)).expanduser().resolve()
                    same_account = running_home == selected_home
            if matches and same_run and same_account:
                _layout(owner.id, evidence)
                return current
            closable = can_close(current, lock=lock)
            if isinstance(closable, Kept):
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, closable.blocker)
    if switching and current is not None:
        closable = can_close(current, lock=lock)
        if isinstance(closable, Kept):
            return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, closable.blocker)
    environment = launch_environment(policy, account, row is not None and not switching)
    if isinstance(environment, StartRefused):
        return environment
    launch_home = (staff.SessionHome(Path(environment["CODEX_HOME"]))
                   if policy.settings.kind == "codex" else staff.HomeUnknown())
    environment["UNIT_TEAM_ROLE"] = name.value
    if owner.kind == "unit":
        environment["UNIT_RUN_DIR"] = str(allowed.run)
    if temporary := os.environ.get("TMPDIR"):
        environment["TMPDIR"] = temporary
    directory = owner.worktree.path if isinstance(owner.worktree, staff.PresentWorktree) else Path.cwd()
    prepared = herdr.prepare_session_environment(str(row.current_session) if row is not None and not switching else "", str(directory), environment,
                                               frozenset({"STAFF_ID"}))
    if isinstance(prepared, nix_env.Refused):
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(prepared))
    if isinstance(current, OpenWorker) and not switching:
        herdr.forget_pane_evidence(client.session, current.pane)
        if not _stop_row(current.row, client, deadline=deadline):
            return _uncertain(current.row, "agent stop is incomplete; pane retained")
        current = StoppedWorker(current.row, current.pane, current.terminal)
    if switching:
        destination = DestinationAfterSourceRelease()
    elif isinstance(current, StoppedWorker):
        _retire_launch(current.row, client, record)
        destination = herdr.ExistingPane(current.pane, current.terminal)
    else:
        try:
            destination = _destination(owner, name.value, evidence, client)
        except conf.Unusable as error:
            return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(error))
        if isinstance(destination, (StartRefused, UncertainWorker)):
            return destination
    if isinstance(destination, herdr.ExistingPane):
        panes = client.panes()
        if sum(pane.get("terminal_id") == destination.terminal for pane in panes) != 1:
            return _uncertain(row or owner, "terminal moved or duplicated before start")
        if not any(pane["pane_id"] == destination.pane and pane.get("terminal_id") == destination.terminal for pane in panes):
            return _uncertain(row or owner, "terminal moved before start")
        if not isinstance(current, StoppedWorker) and not isinstance(client.pane_activity(destination.pane), herdr.PaneAtShell):
            return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "recovery pane has foreground work")
    launches: list[herdr.AgentLaunched] = []
    launch_terminals: dict[str, str] = {}
    launch_processes: dict[str, herdr.AgentProcessIdentity] = {}
    launch_destinations: dict[str, herdr.SplitPane | herdr.ExistingPane] = {}
    first_turn_launches: dict[str, pane_launch.PaneLaunch] = {}

    def start(handoff: worker_admission.FamilyHandoff) -> str:
        del handoff
        launch_destination = destination
        if isinstance(launch_destination, DestinationAfterSourceRelease):
            if isinstance(current, (OpenWorker, StoppedWorker)):
                if not client.close_tab_confirmed(current.pane, target=str(current.row.current_session), terminal=current.terminal):
                    raise herdr_types.HerdrFailure("source pane close is incomplete")
                _retire_launch(current.row, client, record)
            fresh = _evidence(owner.id, record, client, selection=selection)
            selected = _destination(owner, name.value, fresh, client)
            if isinstance(selected, StartRefused):
                raise worker_admission.AdmissionRefused(selected.detail)
            if isinstance(selected, UncertainWorker):
                raise worker_admission.AdmissionRefused(selected.reason)
            launch_destination = selected
        identity = str(row.current_session) if row is not None and not switching else ""
        launch = partial(herdr.launch_prepared_agent, codex=codex) if isinstance(codex, herdr.CodexConversations) else herdr.launch_prepared_agent
        arguments = launch_arguments(policy, identity, directory)
        launched = launch(client.session, launch_destination, prepared, policy.settings.kind,
            arguments, herdr.NoOpeningPrompt(),
            int(start_seconds * 1000), agent=name.value, agent_name=agent_name(owner, name.value))
        launches.append(launched)
        launch_destinations[launched.pane] = launch_destination
        pane = client.pane_row(launched.pane)
        if isinstance(pane, herdr.NoPane) or not (terminal := pane.get("terminal_id", "")):
            raise herdr_types.HerdrFailure("launched terminal is missing")
        if isinstance(launch_destination, herdr.ExistingPane) and terminal != launch_destination.terminal:
            raise herdr_types.HerdrFailure("launched terminal changed")
        launch_terminals[launched.pane] = terminal
        process = client.pane_agent_process(launched.pane, terminal)
        if isinstance(process.start_ticks, herdr.ProcessGenerationUnavailable):
            raise herdr_types.HerdrFailure(process.start_ticks.reason)
        launch_processes[launched.pane] = process
        if not identity and policy.settings.kind == "codex":
            if isinstance(launched.outcome, herdr.PromptNotSubmitted):
                raise herdr_types.HerdrFailure(launched.outcome.reason)
            native = _codex_start_session(client, launched.pane)
            first_turn_launches[launched.pane] = pane_launch.launch(launched.pane, native, str(prepared.directory),
                prepared.env, ["codex", *arguments], kind="codex")
            return native
        return client.native_session(launched.pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS)

    def created_pane() -> CreatedWorkerPane | NoCreatedWorkerPane:
        launched = launches[-1]
        if isinstance(launch_destinations[launched.pane], herdr.ExistingPane):
            return NO_CREATED_WORKER_PANE
        return CreatedWorkerPane(launch_terminals.get(launched.pane, ""))

    def cleanup(identity: str) -> worker_admission.CleanupOutcome:
        if not launches:
            return worker_admission.NoDestinationToStop()
        pane = launches[-1].pane
        with redirect_stderr(io.StringIO()) as errors:
            try:
                terminal = launch_terminals.get(pane, "")
                observed = client.pane_row(pane)
                if not terminal or isinstance(observed, herdr.NoPane) or not observed.get("terminal_id"):
                    raise herdr_types.HerdrFailure("launched terminal is missing")
                if observed.get("terminal_id") != terminal:
                    raise herdr_types.HerdrFailure("launched terminal changed")
                process = launch_processes.get(pane)
                if process is None:
                    raise herdr_types.HerdrFailure("launched process identity is unavailable")
                proved = identity
                if switching or isinstance(launch_destinations[pane], herdr.ExistingPane):
                    proved = client.native_session(pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=observed)
                if client.pane_agent_process(pane, terminal) != process:
                    raise herdr_types.HerdrFailure("launched agent process changed")
                if switching or isinstance(launch_destinations[pane], herdr.ExistingPane):
                    if identity and proved != identity:
                        raise herdr_types.HerdrFailure("launched agent identity changed")
                    client.quit_agent(pane, terminal, proved, start_seconds)
                    if switching and not isinstance(launch_destinations[pane], herdr.ExistingPane):
                        if not client.close_terminal_pane(pane, terminal):
                            raise herdr_types.HerdrFailure("pane close was not confirmed")
                else:
                    close_pane = cast(Callable[..., bool], client.close_pane)
                    if not close_pane(pane, target=process):
                        raise herdr_types.HerdrFailure("pane close was not confirmed")
            except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                detail = " ".join((errors.getvalue() or str(error)).split())
                result = worker_admission.DestinationRetained(pane, detail)
            else:
                return worker_admission.DestinationStopped()
        print(f"anomaly: {pane}: cleanup failed: {result.reason}", file=sys.stderr)
        return result

    identity = ""
    try:
        if switching:
            request = hashlib.sha256(f"{row.current_session if row else ''}:{policy.settings.kind}".encode()).hexdigest()
            published = worker_admission.switch_worker(allowed, policy.settings.kind, start=start,
                dispatch=worker_admission.DispatchOwner.TURN_SUBMISSION, cleanup=cleanup, request=request,
                launch_home=launch_home)
            row = published.row
        else:
            identity = start(worker_admission.NoFamilyHandoff())
            if row is not None and identity != row.current_session:
                return _uncertain(row, "launch proves another native session; pane retained")
            if row is None:
                row = worker_admission.publish(allowed, identity, launch_home=launch_home).row
                lock.bind(row)
        launched = launches[-1]
        if launched.pane in first_turn_launches:
            pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", first_turn_launches[launched.pane])
    except (worker_admission.AdmissionRefused, herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        if not launches:
            return _uncertain(row or owner, f"agent launch is incomplete; pane retained: {error}")
        if row is not None:
            return _uncertain(row, f"launch proof is incomplete: {error}")
        launched = launches[-1]
        admitted = staff.initial_snapshot().by_current_session(identity) if identity else None
        if admitted is not None:
            return IncompleteStart(admitted, launched.pane, str(error), created_pane())
        outcome = cleanup(identity)
        existing_pane = isinstance(launch_destinations.get(launched.pane), herdr.ExistingPane)
        stopped = isinstance(outcome, worker_admission.DestinationStopped)
        status = ((UnpublishedCleanup.AGENT_STOPPED if stopped else UnpublishedCleanup.AGENT_STOP_FAILED)
                  if existing_pane else (UnpublishedCleanup.PANE_CLOSED if stopped else UnpublishedCleanup.PANE_CLOSE_FAILED))
        return UnpublishedWorker(launched.pane, str(error), status)
    launched = launches[-1]
    if isinstance(launched.outcome, herdr.PromptNotSubmitted):
        return IncompleteStart(row, launched.pane, launched.outcome.reason, created_pane())
    herdr.forget_pane_evidence(client.session, launched.pane)
    fresh = _evidence(owner.id, staff.snapshot(), client, selection=selection)
    result = next((state for state in fresh.states if state.row.id == row.id), None)
    _layout(owner.id, fresh)
    if isinstance(result, (OpenWorker, ProcessUnknownWorker)):
        return replace(result, created_pane=created_pane())
    return result if isinstance(result, UncertainWorker) else _uncertain(row, "agent launch is incomplete")


def _retire_launch(row: staff.StaffRow, client: herdr.Herdr, record: staff.StaffSnapshot) -> None:
    path = pane_launch.record_directory() / f"{client.session}.json"
    for pane, entry in pane_launch.read_valid_entries(path).items():
        owner = record.by_member_reference(entry["session_id"])
        if owner is not None and owner.id == row.id and pane_launch.agent_kind(entry) == "codex":
            pane_launch.retire_launch(path, pane, entry)


@herdr.pane_evidence_run()
def close(parent: StaffId, name: WorkerName, *,
          lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> Closed | Kept | UncertainWorker:
    with ExitStack() as operations:
        held = operations.enter_context(worker_lock(parent, name, blocking=True)) if isinstance(lock, WorkerLockAtUse) else lock
        if isinstance(held, Kept):
            return held
        client = _client()
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        if row is None:
            _layout(parent, _evidence(parent, record, client))
            return Closed()
        with staff.row_operation(str(row.id)):
            read = _read_panes(record, client)
            if isinstance(read, PaneReadUnavailable):
                return _uncertain(row, read.reason)
            evidence = _parent_evidence(parent, record, read,
                                        selection=SelectedWorkers(frozenset({name.value})))
            observed = next((item for item in evidence.states if item.row.id == row.id), ClosedWorker(row))
            if isinstance(observed, UncertainWorker):
                return observed
            if isinstance(observed, ProcessUnknownWorker):
                return _uncertain(row, observed.reason)
            if isinstance(observed, (OpenWorker, StoppedWorker)):
                permission = can_close(observed, lock=held)
                if isinstance(permission, Kept):
                    return permission
                if not client.close_terminal_pane(observed.pane, observed.terminal):
                    return _uncertain(row, "pane close is incomplete")
            _retire_launch(row, client, record)
            _layout(parent, evidence)
            return Closed()


def _layout(parent: StaffId, evidence: PaneEvidence) -> None:
    client = _client()
    parent_tabs = {_text(pane, "tab_id") for pane in evidence.scan.found.get(parent, [])} - {""}
    selected = {item.terminal for item in evidence.states if isinstance(item, (OpenWorker, StoppedWorker))
                and item.row.reports_to(parent) and any(_text(pane, "pane_id") == item.pane
                and _text(pane, "tab_id") in parent_tabs for pane in evidence.panes)}
    selected.update(evidence.layout_terminals)
    panes = client.panes()
    tabs = {pane.get("tab_id", "") for pane in panes if pane.get("terminal_id", "") in selected}
    for tab in tabs - {""}:
        key = hashlib.sha256(f"layout:{client.session}:{tab}".encode()).hexdigest()
        with staff.slot_operation(key, "worker layout"):
            layout = herdr.json_object(client.socket_request("layout.export", tab_id=tab).get("layout"), "worker layout")
            terminals = {pane["pane_id"] for pane in panes
                         if pane.get("terminal_id", "") in selected and pane.get("tab_id", "") == tab}
            def balance(value: object, path: list[bool]) -> set[str]:
                node = herdr.json_object(value, "layout node")
                if node.get("type") == "pane":
                    return {_text(node, "pane_id")}
                first = balance(node.get("first"), [*path, False])
                second = balance(node.get("second"), [*path, True])
                leaves = first | second
                if node.get("direction") == "down" and leaves <= terminals and first and second:
                    ratio = len(first) / len(leaves)
                    if node.get("ratio") != ratio:
                        _ = client.socket_request("layout.set_split_ratio", pane_id=next(iter(terminals)), path=path, ratio=ratio)
                return leaves
            _ = balance(layout.get("root"), [])


def _result(result: WorkerStart | Closed | Kept) -> ExitCode:
    if isinstance(result, StartRefused):
        print(f"{result.reason.value}: {result.detail}", file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, AdmissionRefused):
        print(result.reason, file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, Kept):
        print(result.blocker, file=sys.stderr)
        return ExitCode.KEPT
    if isinstance(result, (UncertainWorker, ProcessUnknownWorker, IncompleteStart, UnpublishedWorker)):
        if isinstance(result, UnpublishedWorker):
            print(f"{result.cause}; {unpublished_cleanup_text(result)}", file=sys.stderr)
        elif isinstance(result, IncompleteStart):
            print(f"{result.row.name}: {result.reason}; retain the pane and retry", file=sys.stderr)
        return ExitCode.UNCERTAIN
    print("done")
    return ExitCode.OK


@final
class Arguments(argparse.Namespace):
    command: str = ""
    name: str = ""
    role: str = ""
    account: str = ""
    json: bool = False


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start-or-resume")
    _ = start.add_argument("name")
    _ = start.add_argument("--role", required=True)
    _ = start.add_argument("--account", default="")
    end = commands.add_parser("close")
    _ = end.add_argument("name")
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        record = staff.snapshot()
        owner = record.by_current_session(staff.runtime_id())
        if owner is None or owner.kind not in ("unit", "showrunner"):
            return _result(AdmissionRefused("caller is not a unit or showrunner"))
        if args.command == "list":
            states = observe(owner.id, record)
            fields = [{"name": item.row.name, "id": item.row.id, "state": type(item).__name__,
                       **({"pane": item.pane, "terminal": item.terminal} if isinstance(item, (OpenWorker, StoppedWorker, ProcessUnknownWorker)) else {})}
                      for item in states]
            if args.json:
                print(json.dumps(fields))
            else:
                for item in fields:
                    print(f"{item['name']} {item['state']}")
            return ExitCode.OK
        name = WorkerName.parse(args.name)
        if args.command == "close":
            if worker_admission.find_worker(record, owner.id, name.value) is None:
                return _result(AdmissionRefused(f"no worker named {name.value}"))
            return _result(close(owner.id, name))
        account = DestinationWorkerAccount(args.account) if args.account else ConfiguredWorkerAccount.CURRENT
        return _result(start_or_resume(owner.id, name, cast(WorkerRole, args.role), account=account))
    except (staff.StaffUnreadable, staff.StaffRefused, OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"anomaly: {error}; correct the evidence and retry", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
