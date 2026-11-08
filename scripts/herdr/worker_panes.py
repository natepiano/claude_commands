"""Start, observe, and close staff workers through proved native panes."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Event
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Literal, cast, final

from scripts.herdr import herdr, pane_launch
from scripts.lib import conf, models_registry, nix_env, unit_runs
from scripts.production import staff, worker_admission
from scripts.agents import worker_lifecycle
from scripts.direct import recorder_state

if TYPE_CHECKING:
    from scripts.accounts import accounts

StaffId = staff.StaffId
WorkerName = staff.WorkerName
WorkerRole = staff.WorkerRole


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
class OpenWorker:
    row: staff.StaffRow
    pane: str
    terminal: str
    status: herdr.AgentStatus


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


@dataclass(frozen=True)
class AdmissionRefused:
    reason: str


@dataclass(frozen=True)
class IncompleteStart:
    row: staff.StaffRow
    pane: str
    reason: str


@dataclass(frozen=True)
class UnpublishedWorker:
    pane: str
    cause: str


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


class AllWorkers(Enum):
    CURRENT = "all workers"


@dataclass(frozen=True)
class PaneRead:
    panes: list[dict[str, object]]
    scan: herdr.RowPaneScan
    statuses: dict[str, herdr.AgentStatus]


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
    stop = cast(Callable[..., bool], import_module("scripts.shutdown.restart").stop_row)
    return stop(row, client, deadline=deadline)


def _read_panes(record: staff.StaffSnapshot, client: herdr.Herdr) -> PaneRead | PaneReadUnavailable:
    try:
        panes = [dict(pane) for pane in client.panes()]
        scan = client.scan_row_panes(record, panes)
    except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return PaneReadUnavailable(str(error))
    try:
        statuses = {agent["pane_id"]: agent["agent_status"] for agent in agent_inventory(client)}
    except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
        statuses = {}
    return PaneRead(panes, scan, statuses)


def _parent_evidence(parent: StaffId, record: staff.StaffSnapshot, client: herdr.Herdr,
                     read: PaneRead | PaneReadUnavailable, *,
                     selection: SelectedWorkers | AllWorkers = AllWorkers.CURRENT) -> PaneEvidence:
    workers = record.find(kind="worker", parent=parent)
    selected = [row for row in workers if not isinstance(selection, SelectedWorkers) or row.name in selection.names]
    if isinstance(read, PaneReadUnavailable):
        return PaneEvidence([], herdr.RowPaneScan({}, []), [_uncertain(row, read.reason) for row in selected], frozenset())
    panes, scan, statuses = read.panes, read.scan, read.statuses
    parent_row = record.by_id(parent)
    unread = {item.pane for item in scan.unknown}
    parent_panes = scan.found.get(parent, []) + [
        pane for pane in panes if parent_row is not None and pane.get("label") == parent_row.name
        and _text(pane, "pane_id") in unread]
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
        labels = {row.name, agent_name(parent_row, row.name)} if parent_row else {row.name}
        labelled = [pane for pane in panes if pane.get("label") in labels
                    and _text(pane, "tab_id") in parent_tabs]
        related_unknown = [item for item in scan.unknown if any(
            _text(pane, "pane_id") == item.pane for pane in labelled)]
        if related_unknown:
            unknown = related_unknown[0]
            location = next(pane for pane in labelled if _text(pane, "pane_id") == unknown.pane)
            states.append(ProcessUnknownWorker(row, unknown.pane, _text(location, "terminal_id"), unknown.reason))
        elif len(labelled) > 1:
            states.append(_uncertain(row, "multiple labelled recovery panes"))
        elif labelled:
            pane = labelled[0]
            identity, terminal = _text(pane, "pane_id"), _text(pane, "terminal_id")
            if not terminal or sum(_text(item, "terminal_id") == terminal for item in panes) != 1:
                states.append(_uncertain(row, "recovery terminal identity is missing or duplicated"))
                continue
            owners = [key for key, found in scan.found.items() if pane in found]
            if owners:
                states.append(_uncertain(row, "labelled pane belongs to another native session"))
                continue
            try:
                activity = client.pane_activity(identity)
                if isinstance(activity, herdr.PaneAtShell):
                    states.append(StoppedWorker(row, identity, terminal))
                else:
                    states.append(_uncertain(row, "labelled recovery pane runs other work"))
            except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"anomaly: {row.name}: {error}", file=sys.stderr)
                states.append(ProcessUnknownWorker(row, identity, terminal, str(error)))
        elif isinstance(selection, AllWorkers) and any(item.tab in parent_tabs for item in scan.unknown):
            states.append(_uncertain(row, "pane evidence is incomplete; absence is unproved"))
        else:
            states.append(ClosedWorker(row))
    terminals: frozenset[str] = frozenset()
    if isinstance(selection, SelectedWorkers):
        labels = {label for row in workers for label in (row.name, f"{parent_row.name}-{row.name}" if parent_row else row.name)}
        terminals = frozenset(_text(pane, "terminal_id") for pane in panes
                               if pane.get("label") in labels and _text(pane, "tab_id") in parent_tabs)
    return PaneEvidence(panes, scan, states, terminals)


def _evidence(parent: StaffId, record: staff.StaffSnapshot, client: herdr.Herdr, *,
              selection: SelectedWorkers | AllWorkers = AllWorkers.CURRENT) -> PaneEvidence:
    return _parent_evidence(parent, record, client, _read_panes(record, client), selection=selection)


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


def observe_parents(parents: Sequence[staff.StaffRow], record: staff.StaffSnapshot) -> dict[StaffId, list[WorkerPane]]:
    """Join all requested parents to one pane read."""
    if not parents:
        return {}
    client = _client()
    read = _read_panes(record, client)
    return {parent.id: _parent_evidence(parent.id, record, client, read).states for parent in parents}


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


def worker_labelled_pane(panes: Sequence[Mapping[str, object]], tab: str,
                         owner: staff.StaffRow, name: str) -> Mapping[str, object] | herdr.NoPane:
    """Find the full worker label, or a retained short label."""
    for label in (agent_name(owner, name), name):
        pane = labelled_pane(panes, tab, label)
        if not isinstance(pane, herdr.NoPane):
            return pane
    return herdr.NoPane(name)


def worker_run(member: staff.StaffRow, *,
               record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL,
               scanned: dict[str, list[str]] | None = None) -> Path:
    """Use the worker's newest state pass, with the parent's run as the default."""
    parent = member
    if member.kind == "worker":
        record = staff.local_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
        parent = record.parent_of(member) or member
    runs = unit_runs.held(str(parent.current_session), os.environ.get("ROSTER_RUN_ROOT"), record=record, scanned=scanned)
    fallback = (Path(runs[0]) if runs else Path(unit_runs.environ("RUN_DIR")) if unit_runs.environ("RUN_DIR")
                else unit_runs.state_dir() / "staff" / "workers" / parent.id)
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
    """Read the worker's role from its newest open or finished dispatch."""
    from scripts.direct import progress_history

    dispatch = progress_history.last_dispatch(row.id)
    if isinstance(dispatch, progress_history.RecordedDispatch) and dispatch.role in staff.WORKER_ROLES:
        return dispatch.role
    return WorkerRoleUnavailable("dispatch role is unavailable")


def _completed_turn(row: staff.StaffRow, dispatch: object) -> CloseAllowed | Kept:
    try:
        backend = staff.backend(row)
        if isinstance(backend, staff.BackendUnavailable):
            return Kept("worker transcript backend is unavailable")
        source = worker_admission.transcript_source(row, backend)
        entries = list(worker_admission.transcript_entries(source, set()))
        if any(isinstance(entry, worker_admission.TornTranscriptRecord) for entry in entries):
            return Kept("worker transcript has incomplete records")
        messages = [entry for entry in entries if isinstance(entry, str) and entry]
        if (not isinstance(dispatch, str) or not dispatch or not messages
                or not messages[-1].startswith("assistant:\n")
                or messages[-1].rstrip().splitlines()[-1] not in
                (f"DONE {dispatch}", f"DONE {stage_two_completion_id(dispatch)}")):
            return Kept("last turn does not end with the dispatch DONE line")
    except (OSError, ValueError) as error:
        return Kept(f"completion evidence is unavailable: {error}")
    return CloseAllowed()


def _idle_checks(row: staff.StaffRow, run: Path,
                 lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE, *,
                 closing_turn: bool = False) -> CloseAllowed | Kept:
    if not isinstance(lock, WorkerLockHold) or not lock.owns(row):
        path = _worker_lock_path(row.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return Kept("dispatch lock is held")
    legacy = (*run.glob("dispatch.lock"), *(run / "phases").glob("*/dispatch.lock"))
    if legacy and unit_runs.worker_transport(run) == "mesh":
        for path in legacy:
            with path.open("r") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return Kept("dispatch lock is held")
    passed = worker_pass(row, run)
    if not closing_turn and passed.get("status") in ("active", "refused"):
        return Kept("launcher has not recorded a completed pass")
    live_runs = cast(Callable[[Path, str], Sequence[object]], import_module("scripts.agents.mesh_runs").live_runs)
    if live_runs(run / "mesh_runs", row.name):
        return Kept("a detached run is live")
    return CloseAllowed()


def stage_two_completion_id(dispatch_id: str) -> str:
    """Return the completion id for the second review stage."""
    return dispatch_id + "-2"


def can_close(worker: OpenWorker | StoppedWorker, *,
              run: DispatchRun | HolderRun = HolderRun.CURRENT,
              lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> CloseAllowed | Kept:
    from scripts.direct import progress_history

    record = staff.snapshot()
    parent = record.parent_of(worker.row)
    if parent is None:
        return Kept("worker parent is unavailable")
    directory = run.directory if isinstance(run, DispatchRun) else worker_run(worker.row, record=record)
    if isinstance(worker, StoppedWorker):
        return _idle_checks(worker.row, directory, lock)
    if worker.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
        return Kept(f"agent state is {worker.status.value}")
    passed = worker_pass(worker.row, directory)
    if passed:
        status, dispatch = passed.get("status"), passed.get("instance_id")
    else:
        latest = progress_history.last_dispatch(worker.row.id)
        status, dispatch = ((latest.status, latest.instance_id)
                            if isinstance(latest, progress_history.RecordedDispatch) else ("", ""))
    if status not in ("canceled", "interrupted", "completed", "active"):
        return Kept("launcher has not recorded a completed pass")
    completion = CloseAllowed() if status in ("canceled", "interrupted") else _completed_turn(worker.row, dispatch)
    if isinstance(completion, Kept):
        return completion
    return _idle_checks(worker.row, directory, lock, closing_turn=status == "active")


def launch_policy(role: WorkerRole) -> LaunchPolicy:
    from scripts.agents.codex_mesh import READ_ONLY_SANDBOX, WORKER_SANDBOX
    from scripts.astra import design_roles
    section, key = ("design_roles", role.removeprefix("astra-")) if role.startswith("astra-") else ("direct", role)
    settings = models_registry.launch_settings(section, key)
    sandbox = READ_ONLY_SANDBOX if role in ("review", "astra-reviewer") else WORKER_SANDBOX
    if role == "astra-designer":
        sandbox = design_roles.DESIGNER_SANDBOX
    return _launch_policy(settings, sandbox)


def astra_review_policy(model: str, effort: str) -> LaunchPolicy:
    """Use the supplied review settings with fixed read-only Codex access."""
    from scripts.agents.codex_mesh import READ_ONLY_SANDBOX

    settings = models_registry.LaunchSettings("codex", model, effort, "")
    return LaunchPolicy(settings, READ_ONLY_SANDBOX, "plan")


def worker_policy(settings: models_registry.LaunchSettings) -> LaunchPolicy:
    """Use the worker's file permissions with the supplied model settings."""
    from scripts.agents.codex_mesh import WORKER_SANDBOX

    return _launch_policy(settings, WORKER_SANDBOX)


def _launch_policy(settings: models_registry.LaunchSettings, sandbox: str) -> LaunchPolicy:
    from scripts.agents.codex_mesh import READ_ONLY_SANDBOX

    permission = "plan" if sandbox == READ_ONLY_SANDBOX else "bypassPermissions"
    return LaunchPolicy(settings, sandbox, permission)


def _policy_arguments(policy: LaunchPolicy) -> list[str]:
    settings = policy.settings
    if settings.kind == "codex":
        arguments = ["--model", settings.model, "--sandbox", policy.sandbox, "--ask-for-approval", "never",
                      "-c", f'model_reasoning_effort="{settings.effort}"',
                      "-c", "notice.hide_rate_limit_model_nudge=true"]
        if settings.service_tier:
            arguments += ["-c", f'service_tier="{settings.service_tier}"']
        return arguments
    arguments = ["--model", settings.model, "--permission-mode", policy.permission]
    if settings.effort and settings.effort != "unset":
        arguments += ["--effort", settings.effort]
    return arguments


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
            return True
        return all(expected[index:index + 2] in [actual[pos:pos + 2] for pos in range(len(actual) - 1)]
                   for index in range(0, len(expected), 2))
    except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return _uncertain(worker.row, f"running launch policy is unproved: {error}")


def _destination(parent: staff.StaffRow, name: str, evidence: PaneEvidence,
                 client: herdr.Herdr) -> herdr.SplitPane | herdr.ExistingPane | StartRefused | UncertainWorker:
    parents = evidence.scan.found.get(parent.id, [])
    if len(parents) != 1:
        return _uncertain(parent, "parent pane is unavailable or duplicated")
    parent_pane = parents[0]
    tab = _text(parent_pane, "tab_id")
    label = agent_name(parent, name)
    try:
        pane = worker_labelled_pane(evidence.panes, tab, parent, name)
    except ValueError as error:
        return _uncertain(parent, str(error))
    if not isinstance(pane, herdr.NoPane):
        identity, terminal = str(pane["pane_id"]), str(pane["terminal_id"])
        if not isinstance(client.pane_activity(identity), herdr.PaneAtShell):
            return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "labelled recovery pane is live")
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


def start_or_resume(parent: StaffId, name: WorkerName, role: WorkerRole | WorkerRoleUnavailable, *,
                    account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection = ConfiguredWorkerAccount.CURRENT,
                    lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE,
                    policy: LaunchPolicy | None = None, runs: dict[str, list[str]] | None = None,
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
    if existing is not None and worker_pass(existing, run).get("status") not in ("active", "refused"):
        run = worker_run(owner, record=record, scanned=runs)
    allowed = worker_admission.WorkerStartAllowed(name.value, parent, run,
        worker_admission.ExistingWorkerSlot(existing) if existing is not None else worker_admission.NewWorkerSlot())
    try:
        policy = policy if policy is not None else launch_policy(role)
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
                return _start_locked(owner, name, role, policy, account, allowed, held, codex)
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
        except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            failure = str(error)
            _ = Event().wait(max(0, min(0.1, deadline - time.monotonic())))
            continue
        remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
        if isinstance(client.wait_turn_end(pane, remaining_ms), herdr.WaitTimedOut):
            raise herdr.HerdrFailure("first turn timed out; pane retained")
        return session
    raise herdr.HerdrFailure(f"{failure}; pane retained")


def _codex_start_session(client: herdr.Herdr, pane: str, *,
                         pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> str:
    row = client.pane_row(pane) if isinstance(pane_row, herdr.PaneNotRead) else pane_row
    if isinstance(row, herdr.NoPane):
        raise herdr.HerdrFailure("Codex pane is missing; pane retained")
    if row.get("session_id"):
        return client.native_session(pane, "codex", herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=row)
    return first_turn_session(client, pane, str(row.get("terminal_id", "")))


def _start_locked(owner: staff.StaffRow, name: WorkerName, role: WorkerRole, policy: LaunchPolicy,
                  account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection,
                  allowed: worker_admission.WorkerStartAllowed, lock: WorkerLockHold,
                  codex: herdr.CodexConversations | herdr.CodexLaunchRead) -> WorkerStart:
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
    if row is not None:
        lock.bind(row)
    if isinstance(current, (ProcessUnknownWorker, UncertainWorker)):
        return current
    if current is None:
        parents = evidence.scan.found.get(owner.id, [])
        tab = _text(parents[0], "tab_id") if len(parents) == 1 else ""
        try:
            pane = worker_labelled_pane(evidence.panes, tab, owner, name.value) if tab else herdr.NoPane(name.value)
        except ValueError as error:
            return _uncertain(owner, str(error))
        if not isinstance(pane, herdr.NoPane):
            pane = dict(pane)
            identity = _text(pane, "pane_id")
            unknown = next((item for item in evidence.scan.unknown if item.pane == identity), None)
            claimed = [key for key, found in evidence.scan.found.items() if pane in found]
            if claimed:
                return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "labelled recovery pane holds another live staff session")
            try:
                activity = client.pane_activity(identity)
                if unknown is not None:
                    info = herdr.json_object(client.result("pane", "process-info", "--pane", identity).get("process_info"),
                                             "recovery process info")
                    if not (policy.settings.kind == "codex" and herdr.codex_without_session(info)):
                        return ProcessUnknownWorker(owner, identity, _text(pane, "terminal_id"), unknown.reason)
            except (worker_admission.AdmissionRefused, herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                return ProcessUnknownWorker(owner, identity, _text(pane, "terminal_id"), str(error))
            if not isinstance(activity, herdr.PaneAtShell):
                try:
                    native = (_codex_start_session(client, identity, pane_row=pane) if policy.settings.kind == "codex" else
                              client.native_session(identity, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane))
                    row = worker_admission.publish(allowed, native).row
                    lock.bind(row)
                except (worker_admission.AdmissionRefused, herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    return UnpublishedWorker(identity, str(error))
                recovered = _evidence(owner.id, staff.snapshot(), client, selection=selection)
                state = next((item for item in recovered.states if item.row.id == row.id), None)
                if isinstance(state, (OpenWorker, ProcessUnknownWorker, UncertainWorker)):
                    _layout(owner.id, recovered)
                    return state
                return _uncertain(row, "retained launch proof is incomplete")
    if isinstance(current, OpenWorker):
        passed = worker_pass(current.row, allowed.run)
        if passed.get("status") in ("active", "refused") and passed.get("kind") != role:
            if (passed.get("status") != "active" or not current.status.idle
                    or isinstance(_completed_turn(current.row, passed.get("instance_id")), Kept)):
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, "active dispatch has another role")
        matches = _live_policy(current, policy, client, same_role=passed.get("kind") == role)
        if isinstance(matches, UncertainWorker):
            return matches
        same_run = True
        if owner.kind == "unit" and matches and not passed:
            try:
                pane_row = next(pane for pane in evidence.panes if pane.get("pane_id") == current.pane)
                launched = client.native_pane_process(current.pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane_row)
            except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                return _uncertain(current.row, f"running launch environment is unproved: {error}")
            same_run = launched.environment.get("UNIT_RUN_DIR") == str(allowed.run)
        if matches and same_run:
            _layout(owner.id, evidence)
            return current
        closable = can_close(current, lock=lock)
        if isinstance(closable, Kept):
            return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, closable.blocker)
    environment = launch_environment(policy, account, row is not None)
    if isinstance(environment, StartRefused):
        return environment
    environment["UNIT_TEAM_ROLE"] = name.value
    if owner.kind == "unit":
        environment["UNIT_RUN_DIR"] = str(allowed.run)
    if temporary := os.environ.get("TMPDIR"):
        environment["TMPDIR"] = temporary
    directory = owner.worktree.path if isinstance(owner.worktree, staff.PresentWorktree) else Path.cwd()
    prepared = herdr.prepare_session_environment(str(row.current_session) if row else "", str(directory), environment,
                                               frozenset({"STAFF_ID"}))
    if isinstance(prepared, nix_env.Refused):
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(prepared))
    if isinstance(current, OpenWorker):
        if not _stop_row(current.row, client, deadline=deadline):
            return _uncertain(current.row, "agent stop is incomplete; pane retained")
        current = StoppedWorker(current.row, current.pane, current.terminal)
    if isinstance(current, StoppedWorker):
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
        if not isinstance(client.pane_activity(destination.pane), herdr.PaneAtShell):
            return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "recovery pane has foreground work")
    try:
        launch = partial(herdr.launch_prepared_agent, codex=codex) if isinstance(codex, herdr.CodexConversations) else herdr.launch_prepared_agent
        launched = launch(client.session, destination, prepared, policy.settings.kind,
            launch_arguments(policy, str(row.current_session) if row else "", directory), herdr.NoOpeningPrompt(),
            int(start_seconds * 1000), agent=name.value, agent_name=agent_name(owner, name.value))
    except (herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return _uncertain(row or owner, f"agent launch is incomplete; pane retained: {error}")
    try:
        if row is None and policy.settings.kind == "codex":
            if isinstance(launched.outcome, herdr.PromptNotSubmitted):
                return UnpublishedWorker(launched.pane, launched.outcome.reason)
            identity = _codex_start_session(client, launched.pane)
        else:
            identity = client.native_session(launched.pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS)
        if row is not None and identity != row.current_session:
            return _uncertain(row, "launch proves another native session; pane retained")
        if row is None:
            row = worker_admission.publish(allowed, identity).row
            lock.bind(row)
    except (worker_admission.AdmissionRefused, herdr.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return (_uncertain(row, f"launch proof is incomplete: {error}") if row is not None
                else UnpublishedWorker(launched.pane, str(error)))
    if isinstance(launched.outcome, herdr.PromptNotSubmitted):
        return IncompleteStart(row, launched.pane, launched.outcome.reason)
    fresh = _evidence(owner.id, staff.snapshot(), client, selection=selection)
    result = next((state for state in fresh.states if state.row.id == row.id), None)
    _layout(owner.id, fresh)
    return result if isinstance(result, (OpenWorker, ProcessUnknownWorker, UncertainWorker)) else _uncertain(row, "agent launch is incomplete")


def _retire_launch(row: staff.StaffRow, client: herdr.Herdr, record: staff.StaffSnapshot) -> None:
    path = pane_launch.record_directory() / f"{client.session}.json"
    for pane, entry in pane_launch.read_valid_entries(path).items():
        owner = record.by_member_reference(entry["session_id"])
        if pane_launch.agent_kind(entry) == "codex" and owner is not None and owner.id == row.id:
            pane_launch.retire_launch(path, pane, entry)


def _close_target(observed: WorkerPane, evidence: PaneEvidence, parent: StaffId,
                  client: herdr.Herdr) -> WorkerPane:
    if not isinstance(observed, UncertainWorker) or observed.reason != "labelled recovery pane runs other work":
        return observed
    parents = evidence.scan.found.get(parent, [])
    if len(parents) != 1:
        return observed
    pane = labelled_pane(evidence.panes, _text(parents[0], "tab_id"), observed.row.name)
    if isinstance(pane, herdr.NoPane):
        return observed
    pane = dict(pane)
    identity = client.native_session(_text(pane, "pane_id"), "codex", herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane)
    status = client.readiness(_text(pane, "pane_id"))["agent_status"]
    return OpenWorker(replace(observed.row, current_session=staff.native_id(identity)),
                      _text(pane, "pane_id"), _text(pane, "terminal_id"), status)


def close(parent: StaffId, name: WorkerName, *,
          run: DispatchRun | HolderRun = HolderRun.CURRENT,
          lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE,
          interrupt: bool = False) -> Closed | Kept | UncertainWorker:
    record = staff.snapshot()
    row = worker_admission.find_worker(record, parent, name.value)
    if row is None:
        _layout(parent, _evidence(parent, record, _client()))
        return Closed()
    if interrupt:
        client = _client()
        evidence = _evidence(parent, record, client)
        observed = next((item for item in evidence.states if item.row.id == row.id), ClosedWorker(row))
        target = _close_target(observed, evidence, parent, client)
        if isinstance(target, (UncertainWorker, ProcessUnknownWorker)):
            return _uncertain(row, target.reason)
        if isinstance(target, OpenWorker):
            client.quit_agent(target.pane, target.terminal, str(target.row.current_session),
                              conf.read("direct").number("PANE_AGENT_START_SECONDS"))
    with ExitStack() as operations:
        held = operations.enter_context(worker_lock(parent, name, blocking=interrupt)) if isinstance(lock, WorkerLockAtUse) else lock
        if isinstance(held, Kept):
            return held
        if not held.active or held.parent != parent or held.name != name or not held.owns(row):
            return Kept("worker lock does not own this worker")
        with staff.row_operation(str(row.id)):
            client = _client()
            record = staff.snapshot()
            evidence = _evidence(parent, record, client)
            observed = next((item for item in evidence.states if item.row.id == row.id), ClosedWorker(row))
            row = observed.row
            recovered = isinstance(observed, UncertainWorker) and observed.reason == "labelled recovery pane runs other work"
            if isinstance(observed, UncertainWorker) and recovered:
                target = _close_target(observed, evidence, parent, client)
                if not isinstance(target, OpenWorker):
                    return observed
                if not interrupt and not target.status.idle:
                    return Kept(f"agent state is {target.status.value}")
                observed = target
            if isinstance(observed, UncertainWorker):
                return observed
            if isinstance(observed, ClosedWorker):
                _retire_launch(row, client, record)
                _layout(parent, evidence)
                return Closed()
            owner = record.parent_of(row)
            closable = (_idle_checks(row, run.directory if isinstance(run, DispatchRun) else worker_run(row, record=record), held,
                                     closing_turn=interrupt or recovered)
                        if (interrupt or recovered or isinstance(observed, ProcessUnknownWorker)) and owner is not None
                        else can_close(observed, run=run, lock=held) if isinstance(observed, (OpenWorker, StoppedWorker))
                        else Kept("worker parent is unavailable"))
            if isinstance(closable, Kept):
                return closable
            if isinstance(observed, OpenWorker):
                try:
                    seconds = conf.read("direct").number("PANE_AGENT_START_SECONDS")
                except conf.Unusable as error:
                    return Kept(str(error))
                if recovered:
                    client.quit_agent(observed.pane, observed.terminal, str(observed.row.current_session), seconds)
                elif not _stop_row(observed.row, client, deadline=time.monotonic() + seconds):
                    return _uncertain(row, "agent stop is incomplete; pane retained")
            panes = client.panes()
            matches = [pane for pane in panes if pane.get("terminal_id") == observed.terminal]
            if len(matches) != 1 or matches[0]["pane_id"] != observed.pane:
                return _uncertain(row, "terminal moved, disappeared, or duplicated before close")
            if not isinstance(client.pane_activity(observed.pane), herdr.PaneAtShell):
                return _uncertain(row, "terminal became busy before close")
            if not client.close_tab_confirmed(observed.pane, target=str(row.current_session), terminal=observed.terminal):
                return _uncertain(row, "pane close is incomplete")
            _retire_launch(row, client, record)
            _layout(parent, evidence)
            return Closed()


def _layout(parent: StaffId, evidence: PaneEvidence) -> None:
    client = _client()
    selected = {item.terminal for item in evidence.states if isinstance(item, (OpenWorker, StoppedWorker))
                and item.row.reports_to(parent)}
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
            print(f"{result.pane}: {result.cause}; retain the pane and retry", file=sys.stderr)
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
    interrupt: bool = False


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start-or-resume")
    _ = start.add_argument("name")
    _ = start.add_argument("--role", required=True)
    _ = start.add_argument("--account", default="")
    end = commands.add_parser("close")
    _ = end.add_argument("name")
    _ = end.add_argument("--interrupt", action="store_true")
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
            return _result(close(owner.id, name, interrupt=args.interrupt))
        account = DestinationWorkerAccount(args.account) if args.account else ConfiguredWorkerAccount.CURRENT
        return _result(start_or_resume(owner.id, name, cast(WorkerRole, args.role), account=account))
    except (staff.StaffUnreadable, staff.StaffRefused, OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"anomaly: {error}; correct the evidence and retry", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
