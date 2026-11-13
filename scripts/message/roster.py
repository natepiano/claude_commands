#!/usr/bin/env python3
"""List Claude and Codex sessions on both workstations; --here reads only this one."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.production import showrunners  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402
from scripts.herdr import herdr, herdr_types  # noqa: E402
from scripts.herdr.place import HerdrPlace  # noqa: E402
from scripts.message import codex_daemon  # noqa: E402
from scripts.message import process_table  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.astra import design_roles  # noqa: E402
from scripts.production import staff, session_staff, worker_admission
from scripts.production.production_doc import Refusal as ProductionDocumentRefused
from scripts.lib import names  # noqa: E402
from scripts.lib.names import JobKey  # noqa: E402
if TYPE_CHECKING:
    from scripts.accounts.role_map import role_map_command
    from scripts.whoami import account, agent_accounts, note_file
else:
    import importlib.util
    from scripts.accounts.role_map import role_map_command

    def _lazy_module(name: str) -> ModuleType:
        """Label-only callers do not need account discovery dependencies."""
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            raise ImportError(name)
        loader = importlib.util.LazyLoader(spec.loader)
        spec.loader = loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        return module

    account = _lazy_module("scripts.whoami.account")
    agent_accounts = _lazy_module("scripts.whoami.agent_accounts")
    note_file = _lazy_module("scripts.whoami.note_file")
from scripts.agents import remote as remote  # noqa: E402
from scripts.message.sessions import RegistryRead, SessionRecord, messaging_socket_exists  # noqa: E402

Kind = Literal["claude", "codex"]
Role = Literal["showrunner", "unit director", "worker", "freestanding", "astra-designer", "astra-reviewer"]
WorkerRole = staff.WorkerRole
KNOWN_SLOT_ROLES = frozenset(staff.WORKER_ROLES)
MESSAGEABLE_WORKER_STATUSES = frozenset(("running", "waiting_capacity"))


@dataclass(frozen=True)
class AllRosterSessions:
    """Read every session for discovery."""


@dataclass(frozen=True)
class MessageParticipants:
    """Read worker details for a proved recipient and its sender."""

    native_ids: frozenset[str]


ALL_ROSTER_SESSIONS = AllRosterSessions()


from scripts.message.unit_labels import (
    staff_relationships,
    UnderShowrunner as UnderShowrunner,
    RunsProduction as RunsProduction,
    ProductionWithoutRunningShowrunner as ProductionWithoutRunningShowrunner,
    NoShowrunner as NoShowrunner,
    ShowrunnerRelationship as ShowrunnerRelationship,
    UnderDirector as UnderDirector,
    DirectsUnit as DirectsUnit,
    NoDirector as NoDirector,
    UnitWithoutRunningDirector as UnitWithoutRunningDirector,
    DirectorRelationship as DirectorRelationship,
    UnitLabels as UnitLabels,
    make_labels as _unit_labels,
    production_of as _production_of,
    unit_of as _unit_of,
    ProductionPlace as _ProductionPlace,
    StaffDiagnostics as _StaffDiagnostics,
    StaffRowAnomaly as _StaffRowAnomaly,
    DirectorSession as _DirectorSession,
    NoRunningDirector as _NoRunningDirector,
    LiveRun as _LiveRun,
    ClaudeDetails as _ClaudeDetails,
    RunPlace as _RunPlace,
    as_string as _as_string,
    read_object as _read_object,
    problem as _problem,
    newest_sessions as _newest_sessions,
    registered_showrunners as _registered_showrunners,
    showrunner_ids as _showrunner_ids,
    every_labeled_session as every_labeled_session,
    production_places as _production_places,
    unit_labels_for as unit_labels_for,
    live_runs as _live_runs,
    stopped_run_place as _stopped_run_place,
)


# Keep the discovery boundary for existing callers.
_every_labeled_session = every_labeled_session

@dataclass(frozen=True)
class ThisMachine:
    """Read only this workstation's sources."""


@dataclass(frozen=True)
class AllMachines:
    """Read this workstation and its peer."""


RosterScope = ThisMachine | AllMachines
ALL_MACHINES = AllMachines()


@dataclass(frozen=True)
class CodexThread:
    thread_id: str


@dataclass(frozen=True)
class ClaudeSession:
    session_id: str


@dataclass(frozen=True)
class NoSession:
    """The sender runs in a shell outside a Claude or Codex session."""


SenderIdentity = CodexThread | ClaudeSession | NoSession


@dataclass(frozen=True)
class NoCodexThread:
    """This row has no Codex thread identity."""


@dataclass(frozen=True)
class NoClaudeSession:
    """This row has no Claude session identity."""


def sender_identity(environment: Mapping[str, str]) -> SenderIdentity:
    """Prefer the caller's thread; resolve Claude from its current process registry."""
    if thread_id := environment.get("CODEX_THREAD_ID", ""):
        return CodexThread(thread_id)
    if session_id := staff.runtime_id():
        return ClaudeSession(session_id)
    return NoSession()


@dataclass(frozen=True)
class Messageable:
    address: str


@dataclass(frozen=True)
class CannotReceiveMessages:
    reason: str


MessageReach = Messageable | CannotReceiveMessages


@dataclass(frozen=True)
class AssignmentUnrecorded:
    """A legacy worker record has no assignment field."""


@dataclass(frozen=True)
class AssignmentUnreadable:
    value: str


WorkerRoleState = WorkerRole | AssignmentUnrecorded | AssignmentUnreadable


def worker_role(record: Mapping[str, object]) -> WorkerRoleState:
    if "role" not in record:
        return AssignmentUnrecorded()
    value = record["role"]
    if isinstance(value, str) and value in KNOWN_SLOT_ROLES:
        return value
    return AssignmentUnreadable(str(value))


@dataclass(frozen=True)
class WorkerAssignment:
    slot_role: str
    session_dir: str
    role: WorkerRoleState = AssignmentUnrecorded()
    slot: str = ""


@dataclass(frozen=True)
class NotAWorker:
    """This session is not assigned to a worker."""


@dataclass(frozen=True)
class HeldDesignRole:
    role: Literal["designer", "reviewer"]
    run: str


Assignment = WorkerAssignment | HeldDesignRole | NotAWorker


@dataclass(frozen=True)
class AccountLabel:
    label: str


@dataclass(frozen=True)
class AccountUnread:
    reason: str


@dataclass(frozen=True)
class AccountAttributionUnavailable:
    """A peer running an older roster did not send account attribution."""
    reason: str


AccountAttribution = AccountLabel | AccountUnread | AccountAttributionUnavailable


@dataclass(frozen=True)
class NotDaemonSession:
    """This row has no local Codex daemon route."""


@dataclass(frozen=True)
class NoHerdrPane:
    """This session has no herdr pane route."""


@dataclass(frozen=True)
class RegistryName:
    value: str


@dataclass(frozen=True)
class NoRegistryName:
    """The session registry has no name for this row."""


@dataclass(frozen=True)
class RosterEntry:
    machine: str
    kind: str
    role: Role
    name: str
    status: str
    reach: MessageReach
    cwd: str
    showrunner: ShowrunnerRelationship
    director: DirectorRelationship
    assignment: Assignment
    thread: CodexThread | NoCodexThread = NoCodexThread()
    session: ClaudeSession | NoClaudeSession = NoClaudeSession()
    account: AccountAttribution = AccountUnread("not read")
    daemon: codex_daemon.CodexDaemon | NotDaemonSession = NotDaemonSession()
    pane: HerdrPlace | NoHerdrPane = NoHerdrPane()
    registry_name: RegistryName | NoRegistryName = NoRegistryName()


@dataclass(frozen=True)
class JobNumber:
    number: int


@dataclass(frozen=True)
class JobSlot:
    slot: str


@dataclass(frozen=True)
class Unqualified:
    """The job needs no holder qualifier."""


JobQualifier = JobNumber | JobSlot | Unqualified


@dataclass(frozen=True)
class ShownJob:
    job: JobKey
    qualifier: JobQualifier


@dataclass(frozen=True)
class JobUnassigned:
    reason: str


@dataclass(frozen=True)
class ProductionPart:
    name: str


@dataclass(frozen=True)
class NoProduction:
    """The record has no production."""


@dataclass(frozen=True)
class UnitPart:
    unit: str


@dataclass(frozen=True)
class NoUnit:
    """The record has no unit."""


@dataclass(frozen=True)
class ShownName:
    production: ProductionPart | NoProduction
    unit: UnitPart | NoUnit
    job: ShownJob | JobUnassigned
    address: str
    registry_name: RegistryName | NoRegistryName = NoRegistryName()

    def render(self) -> str:
        parts: list[str] = []
        if isinstance(self.production, ProductionPart) and self.production.name:
            parts.append(self.production.name)
        if isinstance(self.unit, UnitPart) and self.unit.unit:
            parts.append(self.unit.unit)
        if isinstance(self.job, ShownJob):
            loaded = names.runtime_names()
            if not isinstance(loaded, names.Names):
                return self.address
            label = loaded.job(self.job.job)
            qualifier = self.job.qualifier
            if isinstance(qualifier, JobNumber):
                label += f" {qualifier.number}"
            elif isinstance(qualifier, JobSlot) and qualifier.slot:
                label += f" {qualifier.slot}"
            parts.append(label)
        return " · ".join(parts) or (self.registry_name.value
                                    if isinstance(self.registry_name, RegistryName) else self.address)


def shown_name(entry: RosterEntry) -> ShownName:
    production, unit = _production(entry), _unit(entry)
    job: ShownJob | JobUnassigned = JobUnassigned("freestanding session")
    if entry.role == "showrunner":
        job = ShownJob("showrunner", Unqualified())
        unit = ""
    elif entry.role == "unit director":
        job = ShownJob("unit_director", Unqualified())
    elif entry.role in ("astra-designer", "astra-reviewer"):
        job = ShownJob("designer" if entry.role == "astra-designer" else "design_reviewer", Unqualified())
    elif entry.role == "worker" and isinstance(entry.assignment, WorkerAssignment):
        assignment = entry.assignment
        record = str(Path(assignment.session_dir) / "board.log") if assignment.session_dir else entry.name
        if isinstance(assignment.role, AssignmentUnrecorded):
            job = JobUnassigned(f"assignment not recorded: {record}")
        elif isinstance(assignment.role, AssignmentUnreadable):
            job = JobUnassigned(f"assignment record not readable: {record}: {assignment.role.value}")
        else:
            slot = assignment.slot
            job = ShownJob("code_reviewer" if assignment.role == "review" else "worker",
                           JobSlot(slot) if slot else Unqualified())
    address = _address(entry) or _session_address(entry)
    return ShownName(ProductionPart(production) if production else NoProduction(),
                     UnitPart(unit) if unit else NoUnit(), job, address or entry.name,
                     entry.registry_name if entry.kind == "claude" and entry.role == "freestanding" and not entry.name
                     else NoRegistryName())


@dataclass(frozen=True)
class Roster:
    entries: list[RosterEntry]
    problems: list[str]
    warnings: list[str] = field(default_factory=list)
    row_anomalies: tuple[_StaffRowAnomaly, ...] = ()

    @property
    def diagnostics(self) -> list[str]:
        return [*self.problems, *(f'warning: {warning}' for warning in self.warnings)]


def unit_labels(entries: Iterable[RosterEntry]) -> dict[str, UnitLabels]:
    labels: dict[str, UnitLabels] = {}
    for entry in entries:
        identity = _unit_labels(entry.name, entry.showrunner, entry.director)
        if entry.kind == "claude" and isinstance(entry.session, ClaudeSession):
            labels[entry.session.session_id] = identity
        elif entry.kind == "codex" and isinstance(entry.thread, CodexThread):
            labels[entry.thread.thread_id] = identity
    return labels


TaggedSnapshotState = (MessageReach | ShowrunnerRelationship | DirectorRelationship | NotAWorker
                       | CodexThread | NoCodexThread | ClaudeSession | NoClaudeSession | AccountAttribution
                       | RegistryName | NoRegistryName)
SnapshotState = TaggedSnapshotState | WorkerAssignment | HeldDesignRole
_SNAPSHOT_STATES: tuple[type[TaggedSnapshotState], ...] = (
    Messageable, CannotReceiveMessages, UnderShowrunner, RunsProduction,
    ProductionWithoutRunningShowrunner, NoShowrunner, UnderDirector, DirectsUnit,
    UnitWithoutRunningDirector, NoDirector, NotAWorker, CodexThread, NoCodexThread,
    ClaudeSession, NoClaudeSession, AccountLabel, AccountUnread, AccountAttributionUnavailable,
    RegistryName, NoRegistryName,
)


class SnapshotEntry(TypedDict):
    machine: str
    kind: str
    role: Role
    name: str
    status: str
    cwd: str
    reach: dict[str, str]
    showrunner: dict[str, str]
    director: dict[str, str]
    assignment: dict[str, object]
    thread: dict[str, str]
    session: dict[str, str]
    account: NotRequired[dict[str, str]]
    daemon: NotRequired[dict[str, str]]
    pane: NotRequired[dict[str, str]]
    registry_name: NotRequired[dict[str, str]]


def _tagged_state(state: TaggedSnapshotState) -> dict[str, str]:
    return {"tag": type(state).__name__, **cast(dict[str, str], asdict(state))}


def _assignment_snapshot(assignment: Assignment) -> dict[str, object]:
    if isinstance(assignment, NotAWorker):
        return {"tag": "NotAWorker"}
    if isinstance(assignment, HeldDesignRole):
        return {"tag": "HeldDesignRole", "slot_role": "astra-" + assignment.role,
                "session_dir": assignment.run}
    role = assignment.role
    encoded_role: object = role if isinstance(role, str) else {
        "tag": type(role).__name__, **cast(dict[str, str], asdict(role))}
    return {"tag": "WorkerAssignment", "slot_role": assignment.slot_role,
            "session_dir": assignment.session_dir, "role": encoded_role,
            "slot": assignment.slot}


def _snapshot_entry(entry: RosterEntry) -> SnapshotEntry:
    return SnapshotEntry(
        machine=entry.machine, kind=entry.kind, role=entry.role, name=entry.name,
        status=entry.status, cwd=entry.cwd, reach=_tagged_state(entry.reach),
        showrunner=_tagged_state(entry.showrunner), director=_tagged_state(entry.director),
        assignment=_assignment_snapshot(entry.assignment), thread=_tagged_state(entry.thread),
        session=_tagged_state(entry.session), account=_tagged_state(entry.account),
        daemon=({"home": str(entry.daemon.home), "socket": str(entry.daemon.socket)}
                if isinstance(entry.daemon, codex_daemon.CodexDaemon) else {}),
        pane=({"session": entry.pane.session, "pane": entry.pane.pane}
              if isinstance(entry.pane, HerdrPlace) else {}),
        registry_name=_tagged_state(entry.registry_name),
    )


def _snapshot_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("snapshot requires an object")
    result = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in result):
        raise ValueError("snapshot object keys must be strings")
    return cast(dict[str, object], result)


def _snapshot_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"snapshot needs string {key}")
    return value


def _decode_state(value: object) -> SnapshotState:
    values = _snapshot_object(value)
    tag = _snapshot_string(values, "tag")
    if tag == "HeldDesignRole" or (tag == "WorkerAssignment"
            and values.get("slot_role") in ("astra-designer", "astra-reviewer")):
        if set(values) != {"tag", "slot_role", "session_dir"}:
            raise ValueError("invalid fields for snapshot held design role")
        label = _snapshot_string(values, "slot_role")
        if label not in ("astra-designer", "astra-reviewer"):
            raise ValueError("invalid snapshot held design role")
        return HeldDesignRole("designer" if label == "astra-designer" else "reviewer",
                              _snapshot_string(values, "session_dir"))
    if tag == "WorkerAssignment":
        required = {"tag", "slot_role", "session_dir"}
        if not required <= set(values) or set(values) - required - {"role", "slot"}:
            raise ValueError("invalid fields for snapshot worker assignment")
        role: WorkerRoleState = AssignmentUnrecorded()
        if "role" in values:
            raw = values["role"]
            if isinstance(raw, dict):
                state = _snapshot_object(cast(object, raw))
                if state == {"tag": "AssignmentUnrecorded"}:
                    role = AssignmentUnrecorded()
                elif set(state) == {"tag", "value"} and state["tag"] == "AssignmentUnreadable":
                    role = AssignmentUnreadable(_snapshot_string(state, "value"))
                else:
                    raise ValueError("invalid snapshot worker assignment")
            else:
                role = worker_role({"role": raw})
        return WorkerAssignment(_snapshot_string(values, "slot_role"),
                                _snapshot_string(values, "session_dir"), role,
                                _snapshot_string(values, "slot") if "slot" in values else "")
    for state in _SNAPSHOT_STATES:
        if state.__name__ == tag:
            names = [field.name for field in fields(state)]
            if set(values) != {"tag", *names}:
                raise ValueError(f"invalid fields for snapshot state {tag}")
            return state(**{name: _snapshot_string(values, name) for name in names})
    raise ValueError(f"unknown snapshot state {tag}")


def _decode_entry(value: object) -> RosterEntry:
    values = _snapshot_object(value)
    optional = {"account", "daemon", "pane", "registry_name"}
    required = set(SnapshotEntry.__annotations__) - optional
    if not required <= set(values) or set(values) - required - optional:
        raise ValueError("invalid snapshot entry fields")
    reach = _decode_state(values.get("reach"))
    showrunner = _decode_state(values.get("showrunner"))
    director = _decode_state(values.get("director"))
    assignment = _decode_state(values.get("assignment"))
    thread = _decode_state(values.get("thread"))
    session = _decode_state(values.get("session"))
    registry_name = _decode_state(values["registry_name"]) if "registry_name" in values else NoRegistryName()
    attributed = (_decode_state(values["account"]) if "account" in values
                  else AccountAttributionUnavailable("peer sent no account"))
    daemon: codex_daemon.CodexDaemon | NotDaemonSession = NotDaemonSession()
    pane: HerdrPlace | NoHerdrPane = NoHerdrPane()
    if "pane" in values:
        place = _snapshot_object(values["pane"])
        if place:
            pane = HerdrPlace(_snapshot_string(place, "session"), _snapshot_string(place, "pane"))
    if "daemon" in values:
        owner = _snapshot_object(values["daemon"])
        if owner:
            if set(owner) != {"home", "socket"}:
                raise ValueError("invalid snapshot daemon fields")
            daemon = codex_daemon.CodexDaemon(Path(_snapshot_string(owner, "home")),
                                             Path(_snapshot_string(owner, "socket")))
    if not (isinstance(reach, Messageable | CannotReceiveMessages)
            and isinstance(showrunner, ShowrunnerRelationship)
            and isinstance(director, DirectorRelationship)
            and isinstance(assignment, Assignment)
            and isinstance(thread, CodexThread | NoCodexThread)
            and isinstance(session, ClaudeSession | NoClaudeSession)
            and isinstance(attributed, AccountAttribution)
            and isinstance(registry_name, RegistryName | NoRegistryName)):
        raise ValueError("snapshot state is in the wrong field")
    if isinstance(attributed, AccountLabel):
        attributed = AccountLabel(note_file.canonical_label(attributed.label))
    role = _snapshot_string(values, "role")
    if role not in ("showrunner", "unit director", "worker", "freestanding", "astra-designer", "astra-reviewer"):
        raise ValueError(f"unknown snapshot organizational role {role}")
    return RosterEntry(
        machine=_snapshot_string(values, "machine"), kind=_snapshot_string(values, "kind"),
        role=role, name=_snapshot_string(values, "name"),
        status=_snapshot_string(values, "status"), cwd=_snapshot_string(values, "cwd"),
        reach=reach, showrunner=showrunner, director=director, assignment=assignment,
        thread=thread, session=session, account=attributed, daemon=daemon, pane=pane, registry_name=registry_name,
    )


@dataclass(frozen=True)
class RosterSnapshot:
    """Lossless states exchanged between the two workstations."""

    result: Roster

    def encode(self) -> str:
        return json.dumps({"tag": "RosterSnapshot", "entries": [
            _snapshot_entry(entry) for entry in self.result.entries
        ], "problems": self.result.problems,
            **({"warnings": self.result.warnings} if self.result.warnings else {})})

    @classmethod
    def decode(cls, text: str) -> RosterSnapshot:
        values = _snapshot_object(cast(object, json.loads(text)))
        entries, problems = values.get("entries"), values.get("problems")
        warnings = values.get("warnings", [])
        if (values.get("tag") != "RosterSnapshot" or not isinstance(entries, list)
                or not isinstance(problems, list)):
            raise ValueError("not a roster snapshot")
        raw_problems = cast(list[object], problems)
        if not all(isinstance(problem, str) for problem in raw_problems):
            raise ValueError("snapshot problems must be strings")
        if not isinstance(warnings, list) or not all(isinstance(warning, str) for warning in cast(list[object], warnings)):
            raise ValueError("snapshot warnings must be strings")
        return cls(Roster([_decode_entry(entry) for entry in cast(list[object], entries)],
                          cast(list[str], raw_problems), cast(list[str], warnings)))


def cannot_receive_message_text(entry: RosterEntry) -> str:
    """The same refusal and next step for send and broadcast."""
    if not isinstance(entry.reach, CannotReceiveMessages):
        raise ValueError("a refusal needs a CannotReceiveMessages row")
    text = f"{entry.name} cannot take messages ({entry.reach.reason})"
    if entry.role == "worker":
        if isinstance(entry.director, UnderDirector):
            text += f"; contact its unit director {entry.director.name} or wait"
    elif entry.kind == "claude" and entry.reach.reason == "no messaging socket":
        text += "; wait for it to open its messaging socket, then read the roster again and send again"
    return text


def is_sender(entry: RosterEntry, identity: SenderIdentity) -> bool:
    match identity:
        case CodexThread():
            return entry.kind == "codex" and entry.thread == identity
        case ClaudeSession():
            return entry.session == identity
        case NoSession():
            return False


def is_top_level(entry: RosterEntry) -> bool:
    return (
        entry.kind == "claude"
        and entry.role != "worker"
        and not (entry.role == "unit director" and isinstance(entry.showrunner, UnderShowrunner))
    )


def _address(entry: RosterEntry) -> str:
    return entry.reach.address if isinstance(entry.reach, Messageable) else ""


def _session_address(entry: RosterEntry) -> str:
    """Hierarchy links keep their Claude identity even without a messaging socket."""
    return f"session:{entry.session.session_id}" if isinstance(entry.session, ClaudeSession) else ""


def _production(entry: RosterEntry) -> str:
    return _production_of(entry.showrunner)


def _showrunner_name(entry: RosterEntry) -> str:
    return entry.showrunner.name if isinstance(entry.showrunner, UnderShowrunner) else ""


def _showrunner_address(entry: RosterEntry) -> str:
    return entry.showrunner.address if isinstance(entry.showrunner, UnderShowrunner) else ""


def _unit(entry: RosterEntry) -> str:
    return _unit_of(entry.director)


def _director_name(entry: RosterEntry) -> str:
    return entry.director.name if isinstance(entry.director, UnderDirector) else ""


def _director_address(entry: RosterEntry) -> str:
    return entry.director.address if isinstance(entry.director, UnderDirector) else ""


def _slot_role_for(entry: RosterEntry) -> str:
    if isinstance(entry.assignment, HeldDesignRole):
        return "astra-" + entry.assignment.role
    return entry.assignment.slot_role if isinstance(entry.assignment, WorkerAssignment) else ""


def _session_dir(entry: RosterEntry) -> str:
    if isinstance(entry.assignment, HeldDesignRole):
        return entry.assignment.run
    return entry.assignment.session_dir if isinstance(entry.assignment, WorkerAssignment) else ""


class SerializedRosterEntry(TypedDict):
    machine: str
    kind: str
    role: str
    name: str
    status: str
    address: str
    cwd: str
    production: str
    showrunner: str
    showrunner_address: str
    unit: str
    director: str
    director_address: str
    slot_role: str
    session_dir: str
    thread: str | None
    shown_name: str
    job: dict[str, str]


def serialized_job(job: ShownJob | JobUnassigned) -> dict[str, str]:
    if isinstance(job, JobUnassigned):
        return {"state": "unassigned", "reason": job.reason}
    loaded = names.runtime_names()
    if not isinstance(loaded, names.Names):
        return {"state": "unassigned", "reason": names.error_text(loaded)}
    qualifier = job.qualifier
    text = (str(qualifier.number) if isinstance(qualifier, JobNumber)
            else qualifier.slot if isinstance(qualifier, JobSlot) else "")
    return {"state": "assigned", "key": job.job, "display": loaded.job(job.job), "qualifier": text}


def _serialized_entry(entry: RosterEntry) -> SerializedRosterEntry:
    """Flatten domain states at the JSON compatibility boundary."""
    return {
        "machine": entry.machine,
        "kind": entry.kind,
        "role": entry.role,
        "name": entry.name,
        "status": entry.status,
        "address": _address(entry),
        "cwd": entry.cwd,
        "production": _production(entry),
        "showrunner": _showrunner_name(entry),
        "showrunner_address": _showrunner_address(entry),
        "unit": _unit(entry),
        "director": _director_name(entry),
        "director_address": _director_address(entry),
        "slot_role": _slot_role_for(entry),
        "session_dir": _session_dir(entry),
        "thread": entry.thread.thread_id if isinstance(entry.thread, CodexThread) else None,
        "shown_name": shown_name(entry).render(),
        "job": serialized_job(shown_name(entry).job),
    }


@dataclass(frozen=True)
class _WorkerPlace:
    run: _LiveRun
    member: staff.StaffRow


@dataclass(frozen=True)
class _ReviewerProcess:
    kind: Kind
    pid: int


@dataclass(frozen=True)
class _ReviewerProcessNotFound:
    """No Claude or Codex descendant is visible in the process table."""


_ReviewerProcessState = _ReviewerProcess | _ReviewerProcessNotFound


def _session_details(records: list[SessionRecord]) -> dict[str, _ClaudeDetails]:
    details: dict[str, _ClaudeDetails] = {}
    for record in records:
        cwd = record.get("cwd", "")
        if not cwd:
            try:
                cwd = os.readlink(f"/proc/{record['pid']}/cwd")
            except OSError:
                cwd = ""
        details[record["sessionId"]] = _ClaudeDetails(cwd, record.get("status", "") or "running")
    return details


def _worker_ledger(run: _LiveRun, problems: list[str], membership: staff.StaffSnapshot) -> list[staff.StaffRow]:
    try:
        owner = (membership.by_current_session(run.director.session_id)
                 if isinstance(run.director, _DirectorSession)
                 else worker_admission.parent(membership, run=run.path))
    except (ValueError, staff.StaffUnreadable) as error:
        _problem(problems, str(run.path), error)
        return []
    if owner is None:
        return []
    return [row for row in membership.find(kind="worker", parent=owner.id)
            if isinstance(membership.backends.get(row.id), staff.ClaudeBackend)]


def _slot_role(name: str) -> str:
    """Read the address suffix when no launch records a slot."""
    candidate = name.rpartition("-")[2]
    return candidate


def _worker_slot(run: Path, name: str, record: Mapping[str, object], problems: list[str]) -> str:
    """Read the saved slot through staff_id, including named fix workers."""
    if slot := _as_string(record.get("slot")):
        return slot
    fallback = _slot_role(name)
    for path in sorted(run.glob("impl_worker_*.json")):
        try:
            launch = _read_object(path)
        except (OSError, UnicodeError, ValueError) as error:
            _problem(problems, f"worker launch {path}", error)
            continue
        if record.get("staff_id") and launch.get("staff_id") == record["staff_id"]:
            return _as_string(launch.get("slot")) or fallback
    return fallback


def _worker_assignment(run: Path | str, slot: str, record: Mapping[str, object],
                       membership: staff.StaffSnapshot) -> WorkerAssignment:
    member = session_staff.worker_row(record, membership)
    if member is None or not isinstance(member.role, staff.KnownWorkerRole):
        return WorkerAssignment("no role", str(run), AssignmentUnrecorded(), slot)
    label = member.role.value
    if member.role.value == "review":
        from scripts.direct import progress_history

        try:
            with progress_history.dispatch_reads(membership, (Path(run),)):
                dispatch = progress_history.last_dispatch(member.id)
        except (OSError, ValueError):
            dispatch = progress_history.NoDispatchRecorded()
        if isinstance(dispatch, progress_history.RecordedDispatch) and isinstance(dispatch.lens, str):
            label = f"review ({dispatch.lens})"
    return WorkerAssignment(label, str(run), member.role.value, slot)


def _held_location(parent: staff.StaffRow, membership: staff.StaffSnapshot,
                   claude: Mapping[str, RosterEntry]) -> tuple[ShowrunnerRelationship, DirectorRelationship]:
    """Read helper relationships from its staff parent."""
    session = claude.get(parent.current_session)
    director: DirectorRelationship = NoDirector()
    runner = parent
    if parent.kind == "unit":
        director = UnderDirector(parent.name, session.name if session else parent.name,
                                 _session_address(session) if session else "")
        parent_runner = membership.parent_of(parent)
        if parent_runner is None or parent_runner.kind != "showrunner":
            raise ValueError("design worker has no showrunner staff parent")
        runner = parent_runner
    elif parent.kind != "showrunner":
        raise ValueError("design worker parent is not a unit or showrunner")
    runner_session = claude.get(runner.current_session)
    showrunner: ShowrunnerRelationship = (
        UnderShowrunner(runner.name, runner_session.name, _session_address(runner_session))
        if runner_session else ProductionWithoutRunningShowrunner(runner.name))
    return showrunner, director


def unit_worker_title(production: str, unit: str, job: JobKey, ordinal: int, address: str) -> str:
    """Render a unit worker's title from its ledger placement; a second holder of a job gets a number."""
    qualifier: JobQualifier = JobNumber(ordinal) if ordinal > 1 else Unqualified()
    return ShownName(ProductionPart(production), UnitPart(unit), ShownJob(job, qualifier), address).render()


def worker_title(session_dir: str, name: str, record: Mapping[str, object]) -> str:
    return worker_shown_name(session_dir, name, record).render()


def worker_shown_name(session_dir: str, name: str, record: Mapping[str, object]) -> ShownName:
    """Build a thread title with the same production and unit lookup as the roster."""
    run = Path(session_dir)
    if design_id := _as_string(record.get("held_design_id")):
        held_role = record.get("held_role")
        try:
            design = design_roles.read_design(design_id)
        except (OSError, UnicodeError, ValueError, TypeError):
            return ShownName(NoProduction(), NoUnit(), JobUnassigned("design unreadable"), name)
        if held_role not in ("designer", "reviewer"):
            return ShownName(NoProduction(), NoUnit(), JobUnassigned("held role unreadable"), name)
        try:
            membership = staff.snapshot()
            if published_id := _as_string(record.get("staff_id")):
                worker = membership.by_id(staff.StaffId(published_id))
                parent = membership.parent_of(worker) if worker is not None else None
                if worker is None or worker.kind != "worker" or parent is None:
                    raise ValueError("design worker parent is unavailable")
            else:
                parent = design_roles.worker_parent(design, held_role, membership)
            showrunner, director = _held_location(parent, membership, {})
        except (OSError, ValueError):
            return ShownName(NoProduction(), NoUnit(), JobUnassigned("design worker parent unavailable"), name)
        instance = next(instance for instance in design.roles if instance.role == held_role)
        name = design_roles.instance_name(instance, membership)
        return shown_name(RosterEntry(
            machine="", kind="codex", role="astra-designer" if held_role == "designer" else "astra-reviewer",
            name=name, status="", reach=Messageable(name), cwd=design_roles.design_repository(design, membership),
            showrunner=showrunner, director=director, assignment=HeldDesignRole(held_role, session_dir)))
    showrunner: ShowrunnerRelationship = NoShowrunner()
    director: DirectorRelationship = NoDirector()
    problems: list[str] = []
    records = _newest_sessions()
    membership = staff.initial_snapshot()
    resolver = session_staff.RowResolver(membership)
    diagnostics = _StaffDiagnostics(problems)
    places = _production_places(records, set(), diagnostics,
                                record=membership, resolver=resolver)
    place = places.get(unit_runs.director(run, record=membership))
    if place is not None:
        showrunner, director = place.showrunner, UnderDirector(place.director.unit, "", "")
    assignment = _worker_assignment(run, _worker_slot(run, name, record, []), record, membership)
    entry = RosterEntry(machine="", kind="codex", role="worker", name=name, status="",
                        reach=Messageable(name), cwd="", showrunner=showrunner,
                        director=director, assignment=assignment)
    # A detached, unplaced thread retains its existing address as its title.
    if isinstance(showrunner, NoShowrunner) and isinstance(director, NoDirector):
        return ShownName(NoProduction(), NoUnit(), JobUnassigned("no unit record"), name)
    return shown_name(entry)


def claude_assignment(run: Path, member: staff.StaffRow, problems: list[str]) -> WorkerAssignment:
    """Read the slot from launch evidence and the current role from staff."""
    slot = _slot_role(member.name)
    legacy_label = slot if slot in KNOWN_SLOT_ROLES else "role unknown"
    slot = _worker_slot(run, member.name, {"staff_id": member.id}, problems)
    if isinstance(member.role, staff.KnownWorkerRole):
        return WorkerAssignment(member.role.value, str(run), member.role.value, slot)
    return WorkerAssignment(legacy_label, str(run), AssignmentUnrecorded(), slot)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


def _run_place(
    run: _LiveRun,
    claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, _ProductionPlace],
    membership: staff.StaffSnapshot,
) -> _RunPlace:
    if isinstance(run.director, _NoRunningDirector):
        return _stopped_run_place(run.path, membership)
    director = claude_entries.get(run.director.session_id)
    if director is None:
        return _stopped_run_place(run.path, membership)
    place = production_places.get(run.director.session_id)
    unit = place.director.unit if place is not None else ""
    director_link = UnderDirector(unit, director.name, _session_address(director))
    return _RunPlace(
        director=director_link,
        showrunner=place.showrunner if place is not None else NoShowrunner(),
        cwd=director.cwd,
    )






def _codex_home_account(home: Path) -> AccountLabel | AccountUnread:
    identity = agent_accounts.codex_account(home)
    return AccountLabel(identity.label) if isinstance(identity, agent_accounts.KnownCodexAccount) else AccountUnread(identity.reason)


@dataclass(frozen=True)
class _ReadHomeAccount:
    """Account attribution must read the home state when requested."""


_HOME_ACCOUNT_UNCHECKED = _ReadHomeAccount()


def _process_account(pid: int, kind: str, *,
                     home_state: account.LegacyHomeLogin | account.ConfigOnlyHome | account.HomeAccountUnchecked | _ReadHomeAccount = _HOME_ACCOUNT_UNCHECKED,
                     ) -> AccountLabel | AccountUnread:
    if kind == "claude":
        identity = account.account_of(pid, home_state=(account.HomeAccountUnchecked()
                                      if isinstance(home_state, _ReadHomeAccount) else home_state))
        return AccountLabel(identity.label) if identity is not None else AccountUnread("process account could not be read")
    home = account.process_codex_home(pid)
    if isinstance(home, account.ProcessEnvironmentUnread):
        return AccountUnread(home.reason)
    return _codex_home_account(home.home if isinstance(home, account.ProcessCodexHome) else Path.home() / ".codex")








def _ledger_workers(
    runs: Iterable[_LiveRun], records: Iterable[SessionRecord], problems: list[str],
    membership: staff.StaffSnapshot,
) -> dict[str, _WorkerPlace]:
    workers: dict[str, _WorkerPlace] = {}
    records_list = list(records)
    owner_runs: dict[staff.StaffId, list[str]] = {}
    scanned: dict[str, list[str]] = {}
    for run in sorted(runs, key=lambda run: (isinstance(run.director, _NoRunningDirector),
                                             not (run.path / unit_runs.STATE_FILE).exists())):
        for member in _worker_ledger(run, problems, membership):
            owner = membership.parent_of(member)
            if owner is not None:
                if owner.id not in owner_runs:
                    if not owner_runs:
                        scanned = unit_runs.held_by_session(os.environ.get("ROSTER_RUN_ROOT"), record=membership)
                    owner_runs[owner.id] = unit_runs.held(owner.current_session, os.environ.get("ROSTER_RUN_ROOT"),
                                                        record=membership, scanned=scanned)
                preferred = owner_runs[owner.id]
                if preferred and run.path.resolve() != Path(preferred[0]).resolve():
                    continue
            for record in records_list:
                session_id = record["sessionId"]
                if session_id == member.current_session:
                    if session_id not in workers:
                        workers[session_id] = _WorkerPlace(run, member)
    return workers


def _claude_entries(
    machine: str, records: list[SessionRecord], details: Mapping[str, _ClaudeDetails],
    runner_ids: Mapping[str, showrunners.Showrunner],
    production_places: Mapping[str, _ProductionPlace], runs: list[_LiveRun],
    workers: Mapping[str, _WorkerPlace], *, membership: staff.StaffSnapshot,
    home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
    problems: list[str],
) -> dict[str, RosterEntry]:
    marker_directors = {
        run.director.session_id
        for run in runs
        if isinstance(run.director, _DirectorSession)
    }
    entries: dict[str, RosterEntry] = {}
    records_by_id = {record["sessionId"]: record for record in records}
    for record in records:
        session_id = record["sessionId"]
        showrunner: ShowrunnerRelationship = NoShowrunner()
        director: DirectorRelationship = NoDirector()
        assignment: Assignment = NotAWorker()
        if session_id in runner_ids:
            role: Role = "showrunner"
            showrunner = RunsProduction(runner_ids[session_id]["name"])
        elif session_id in production_places:
            role = "unit director"
            place = production_places[session_id]
            showrunner = place.showrunner
            director = place.director
        elif session_id in marker_directors:
            role = "unit director"
        elif session_id in workers:
            role = "worker"
            worker = workers[session_id]
            assignment = claude_assignment(worker.run.path, worker.member, problems)
            run_director = worker.run.director
            if isinstance(run_director, _NoRunningDirector):
                place = _stopped_run_place(worker.run.path, membership)
                showrunner, director = place.showrunner, place.director
            director_entry = (
                records_by_id.get(run_director.session_id)
                if isinstance(run_director, _DirectorSession) else None
            )
            if director_entry is not None and isinstance(run_director, _DirectorSession):
                director_place = production_places.get(run_director.session_id)
                if director_place is not None:
                    showrunner = director_place.showrunner
                    unit = director_place.director.unit
                else:
                    unit = ""
                director = UnderDirector(
                    unit,
                    owner.name if (owner := membership.by_current_session(run_director.session_id)) is not None else "",
                    f"session:{run_director.session_id}",
                )
        else:
            role = "freestanding"
        detail = details[session_id]
        entries[session_id] = RosterEntry(
            machine=machine,
            kind="claude",
            role=role,
            name=member.name if (member := membership.by_current_session(session_id)) is not None else "",
            status=detail.status,
            reach=(
                Messageable(f"session:{session_id}")
                if messaging_socket_exists(record)
                else CannotReceiveMessages("no messaging socket")
            ),
            cwd=detail.cwd,
            showrunner=showrunner,
            director=director,
            assignment=assignment,
            session=ClaudeSession(session_id),
            registry_name=RegistryName(record["name"]) if record["name"] else NoRegistryName(),
            account=_process_account(record["pid"], "claude", home_state=home_state),
        )
    return entries


def _process_words(process: process_table.Process) -> list[str]:
    try:
        return shlex.split(process.arguments)
    except ValueError:
        return process.arguments.split()


def _descends_from(
    process: process_table.Process, ancestor: int, table: Mapping[int, process_table.Process]
) -> bool:
    parent = process.parent
    visited: set[int] = set()
    while parent > 0 and parent not in visited:
        if parent == ancestor:
            return True
        visited.add(parent)
        parent_process = table.get(parent)
        if parent_process is None:
            return False
        parent = parent_process.parent
    return False


def _reviewer_process(
    launcher_pid: int, table: Mapping[int, process_table.Process]
) -> _ReviewerProcessState:
    claude = _ReviewerProcessNotFound()
    for process in sorted(table.values(), key=lambda item: item.pid):
        if not _descends_from(process, launcher_pid, table):
            continue
        words = _process_words(process)
        command = Path(process.command).name
        arguments = words[1:] if words and Path(words[0]).name == command else words
        if command == "codex" and arguments[:1] == ["exec"]:
            return _ReviewerProcess("codex", process.pid)
        if command == "claude":
            claude = _ReviewerProcess("claude", process.pid)
    return claude


def _reviewer_cwd(process: _ReviewerProcess, fallback: str) -> str:
    try:
        return os.readlink(f"/proc/{process.pid}/cwd")
    except OSError:
        return fallback


def _review_workers(
    machine: str, run: _LiveRun, place: _RunPlace,
    table: Mapping[int, process_table.Process], problems: list[str], *,
    home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
    names_by_pid: Mapping[int, str],
) -> list[RosterEntry]:
    entries: list[RosterEntry] = []
    try:
        # A launch that names its phase writes in that phase's directory.
        statuses = sorted([*run.path.glob("*_status*"), *run.path.glob("phases/*/*_status*")])
    except OSError as error:
        _problem(problems, f"review state in {run.path}", error)
        return entries
    for status_path in statuses:
        worker, _, suffix = status_path.name.rpartition("_status")
        if suffix and not suffix.startswith("_"):
            continue
        lens = suffix.removeprefix("_")
        try:
            status = status_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            _problem(problems, f"review status {status_path}", error)
            continue
        if status != "reviewing":
            continue
        pid_path = status_path.parent / f"{worker}_pid{suffix}"
        try:
            launcher_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError, ValueError) as error:
            _problem(problems, f"review pid {pid_path}", error)
            continue
        if not _pid_alive(launcher_pid):
            continue
        process = _reviewer_process(launcher_pid, table)
        if isinstance(process, _ReviewerProcessNotFound):
            continue
        slot_role = f"review ({lens})" if lens else "review"
        entries.append(RosterEntry(
            machine=machine,
            kind=process.kind,
            role="worker",
            name=names_by_pid.get(process.pid, ""),
            status="running",
            reach=CannotReceiveMessages("blind reviewer"),
            cwd=_reviewer_cwd(process, place.cwd),
            showrunner=place.showrunner,
            director=place.director,
            assignment=WorkerAssignment(slot_role, str(run.path), "review", lens or "review"),
            account=_process_account(process.pid, process.kind, home_state=home_state),
        ))
    return entries


def _unit_workers(
    machine: str, runs: list[_LiveRun], claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, _ProductionPlace],
    problems: list[str], *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
    membership: staff.StaffSnapshot,
    records: list[SessionRecord],
    participants: AllRosterSessions | MessageParticipants = ALL_ROSTER_SESSIONS,
) -> tuple[list[RosterEntry], set[str]]:
    table: dict[int, process_table.Process] = {}
    if isinstance(participants, AllRosterSessions):
        try:
            table = process_table.snapshot()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _problem(problems, "process table", error)
    entries: list[RosterEntry] = []
    worker_threads: set[str] = set()
    try:
        parents = session_staff.pane_parents(membership)
        selected_workers = membership.find(kind="worker")
        if isinstance(participants, MessageParticipants):
            selected_workers = [row for row in selected_workers if row.current_session in participants.native_ids]
            parent_ids = {row.parent.id for row in selected_workers if isinstance(row.parent, staff.ReportsTo)}
            parents = [parent for parent in parents if parent.id in parent_ids]
        if parents:
            from scripts.herdr import worker_panes

            observations = (worker_panes.observe_parents(parents, membership,
                                selection=worker_panes.SelectedWorkerRows(frozenset(row.id for row in selected_workers)))
                            if isinstance(participants, MessageParticipants)
                            else worker_panes.observe_parents(parents, membership))
            scanned = worker_panes.parent_run_index(membership)
            for parent in parents:
                run = worker_panes.worker_run(parent, record=membership, scanned=scanned)
                for state in observations[parent.id]:
                    row = state.row
                    backend = membership.backends.get(row.id)
                    director, showrunner = staff_relationships(
                        membership, row, {key: item.name for key, item in claude_entries.items()},
                        {key: item.showrunner for key, item in claude_entries.items()})
                    worker_threads.add(row.current_session)
                    existing = claude_entries.get(row.current_session)
                    if not isinstance(backend, staff.CodexBackend) and existing is not None:
                        entries.append(replace(existing, role="worker", director=director, showrunner=showrunner))
                        continue
                    if isinstance(state, worker_panes.OpenWorker):
                        status = f"{state.status}; pane {state.pane}"
                        reach: MessageReach = Messageable(f"worker:{parent.name}/{row.name}")
                    else:
                        status = (f"stopped; pane {state.pane}" if isinstance(state, worker_panes.StoppedWorker) else
                                  "closed" if isinstance(state, worker_panes.ClosedWorker) else
                                  f"unknown; pane {state.pane}: {state.reason}" if isinstance(state, worker_panes.ProcessUnknownWorker) else
                                  f"incomplete: {state.reason}")
                        reach = CannotReceiveMessages(status)
                    entries.append(RosterEntry(
                        machine=machine, kind="codex" if isinstance(backend, staff.CodexBackend) else "claude",
                        role="worker", name=row.name, status=status, reach=reach,
                        cwd=str(session_staff.worker_directory(membership, row.id)),
                        showrunner=showrunner, director=director,
                        assignment=_worker_assignment(run, _worker_slot(run, row.name, {"staff_id": row.id}, problems),
                                                      {"staff_id": row.id}, membership),
                        thread=CodexThread(row.current_session) if isinstance(backend, staff.CodexBackend) else NoCodexThread(),
                        session=ClaudeSession(row.current_session) if isinstance(backend, staff.ClaudeBackend) else NoClaudeSession(),
                        account=_codex_home_account(backend.codex_home) if isinstance(backend, staff.CodexBackend)
                        else AccountUnread("worker account not read")))
    except (OSError, ValueError, herdr_types.HerdrFailure, ProductionDocumentRefused) as error:
        _problem(problems, "pane workers", error)
    # Blind review rows have no native address and cannot identify either participant.
    if isinstance(participants, MessageParticipants):
        return entries, worker_threads
    names_by_pid = {record["pid"]: member.name for record in records
                    if (member := membership.by_current_session(record["sessionId"])) is not None}
    for run in runs:
        try:
            place = _run_place(run, claude_entries, production_places, membership)
        except staff.StaffUnreadable as error:
            _problem(problems, str(run.path), error)
            continue
        entries.extend(_review_workers(machine, run, place, table, problems,
                                       home_state=home_state, names_by_pid=names_by_pid))
    return entries, worker_threads


def _durable_roles(machine: str, claude: Mapping[str, RosterEntry], warnings: list[str], *,
                   membership: staff.StaffSnapshot) -> tuple[list[RosterEntry], set[str]]:
    entries: list[RosterEntry] = []
    threads: set[str] = set()
    for path in sorted(design_roles.state_root().glob('*/design.json')):
        try:
            design = design_roles.read_design(path.parent.name)
            for instance in design.roles:
                if isinstance(instance.lifecycle, (design_roles.RoleNotOpened, design_roles.RoleEnded)):
                    continue
                parent = design_roles.worker_parent(design, instance.role, membership)
                showrunner, director = _held_location(parent, membership, claude)
                member = membership.by_id(instance.lifecycle.staff_id)
                evidence = session_staff.worker_backend(membership, member.id) if member is not None else session_staff.WorkerBackendUnavailable("worker is absent")
                thread = (member.current_session if member is not None and isinstance(evidence, session_staff.RecordedWorker)
                          and isinstance(evidence.backend, staff.CodexBackend) else "")
                if not thread:
                    print(f"anomaly: {design_roles.instance_name(instance, membership)}: held role has no Codex backend", file=sys.stderr)
                    continue
                if thread in threads:
                    continue
                threads.add(thread)
                status = type(instance.lifecycle).__name__
                entries.append(RosterEntry(machine=machine, kind='codex',
                    role='astra-designer' if instance.role == 'designer' else 'astra-reviewer',
                    name=design_roles.instance_name(instance, membership), status=status, reach=CannotReceiveMessages(
                        'held role; add pending messages through design_roles.py'),
                    cwd=design_roles.design_repository(design, membership), showrunner=showrunner, director=director,
                    assignment=HeldDesignRole(instance.role, str(path.parent)), thread=CodexThread(thread),
                    account=AccountUnread('serving home not read')))
        except (OSError, ValueError, TypeError) as error:
            _problem(warnings, 'durable role ' + str(path), error)
    return entries, threads


def _herdr_codex_panes(machine: str, productions: Iterable[str] = (), *,
                       worker_threads: Iterable[str] = (), membership: staff.StaffSnapshot | None = None,
                       participants: AllRosterSessions | MessageParticipants = ALL_ROSTER_SESSIONS) -> Roster:
    """Use the reported session or the pane process to address Codex."""
    membership = staff.initial_snapshot() if membership is None else membership
    entries: list[RosterEntry] = []
    problems: list[str] = []
    try:
        sessions = herdr.live_sessions()
    except herdr_types.HerdrMissing as error:
        for production in productions:
            _problem(problems, f"unit hosts for {production} were not read", error)
        return Roster(entries, problems)
    except herdr_types.HerdrFailure as error:
        _problem(problems, "herdr sessions", error)
        for production in productions:
            _problem(problems, f"unit hosts for {production} were not read", error)
        return Roster(entries, problems)
    processes = herdr.herdr_processes()
    cwds = {process.place: process.cwd for process in processes}
    codex_processes = {process.place: process for process in processes if process.command == "codex"}
    from scripts.herdr import worker_panes

    for session in sessions:
        name = session["name"]
        try:
            agents = worker_panes.agent_inventory(herdr.Herdr(name))
        except herdr_types.HerdrFailure as error:
            _problem(problems, f"herdr session {name}", error)
            continue
        for agent in agents:
            if agent.get("agent") != "codex":
                continue
            pane = agent["pane_id"]
            report = agent["agent_session"]
            thread: CodexThread | NoCodexThread = NoCodexThread()
            if isinstance(report, herdr.ReportedAgentSession):
                thread = CodexThread(report.session_id)
            else:
                try:
                    thread = CodexThread(herdr.Herdr(name).native_pane_process(
                        pane, "codex", herdr.PROGRAM_TIMEOUT_SECONDS).current_identity)
                except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
                    pass
            if isinstance(participants, MessageParticipants) and (
                    not isinstance(thread, CodexThread) or thread.thread_id not in participants.native_ids):
                continue
            process = codex_processes.get(HerdrPlace(name, pane))
            if process is not None and process.marks.get("CODEX_THREAD_ID", "") in worker_threads:
                continue
            entries.append(RosterEntry(
                machine=machine, kind="codex", role="freestanding",
                name=(member.name if isinstance(thread, CodexThread)
                      and (member := membership.by_current_session(thread.thread_id)) is not None else ""),
                status=str(agent["agent_status"]),
                reach=(Messageable(f"codex:{thread.thread_id}") if isinstance(thread, CodexThread)
                       else CannotReceiveMessages("interactive Codex in a herdr pane")),
                thread=thread, pane=HerdrPlace(name, pane),
                cwd=cwds.get(HerdrPlace(name, pane), ""),
                showrunner=NoShowrunner(), director=NoDirector(), assignment=NotAWorker(),
                account=_process_account(process.pid, "codex") if process else AccountUnread("pane process not found"),
            ))
    return Roster(entries, problems)


def machine_name() -> str:
    return remote.this_machine()


@herdr.observation_snapshot()
@herdr.pane_snapshot()
def _local_roster(environment: Mapping[str, str], *, membership: staff.StaffSnapshot,
                  registry: RegistryRead,
                  participants: AllRosterSessions | MessageParticipants = ALL_ROSTER_SESSIONS) -> Roster:
    home_state = account.home_account_state()
    problems: list[str] = []
    warnings: list[str] = []
    machine = machine_name()
    directory = sessions.sessions_dir()
    if directory in registry.unreadable:
        _problem(problems, "Claude session records", f"cannot list {directory}")
    newest: dict[str, SessionRecord] = {}
    for record in sessions.running_records(registry):
        _ = newest.setdefault(record["sessionId"], record)
    records = list(newest.values())
    details = _session_details(records)
    resolver = session_staff.RowResolver(membership)
    diagnostics = _StaffDiagnostics(problems)
    registered = _registered_showrunners(
        diagnostics, membership=membership, resolver=resolver,
        known_sessions={record["sessionId"]: record for record in records},
    )
    runner_ids = _showrunner_ids(records, registered, membership=membership)
    roots = unit_runs.run_roots(environment.get("ROSTER_RUN_ROOT"))
    runs = _live_runs(roots, {record["sessionId"] for record in records}, problems, record=membership)
    ledger_records = ([record for record in records if record["sessionId"] in participants.native_ids]
                      if isinstance(participants, MessageParticipants) else records)
    ledger_workers = {row.current_session for row in membership.find(kind="worker")}
    workers = (_ledger_workers(runs, ledger_records, problems, membership)
               if isinstance(participants, AllRosterSessions)
               or any(record["sessionId"] in ledger_workers for record in ledger_records) else {})
    production_places: dict[str, _ProductionPlace] = {}
    try:
        production_places = _production_places(
            records, set(workers), diagnostics, record=membership, resolver=resolver
        )
    except staff.StaffUnreadable as error:
        _problem(problems, "staff membership", error)
    warnings.extend(diagnostics.warnings)
    claude = _claude_entries(
        machine, records, details, runner_ids, production_places, runs, workers,
        home_state=home_state, problems=problems, membership=membership,
    )
    unit_workers, worker_threads = _unit_workers(
        machine, runs, claude, production_places, problems,
        home_state=home_state, membership=membership, records=records, participants=participants
    )
    durable_roles, durable_threads = _durable_roles(machine, claude, warnings, membership=membership)
    unit_workers = [entry for entry in unit_workers
                    if not isinstance(entry.thread, CodexThread) or entry.thread.thread_id not in durable_threads]
    unit_workers.extend(durable_roles)
    worker_threads.update(durable_threads)
    codex_sessions: list[RosterEntry] = []
    for result in codex_daemon.read_daemons(environment):
        owner, daemon = result.owner, result.sessions
        if isinstance(daemon, codex_daemon.DaemonUnreadable):
            _problem(problems, f"Codex daemon {agent_accounts.codex_home_display(owner.home)}", daemon.reason)
            continue
        codex_sessions.extend(
            RosterEntry(
                machine=machine,
                kind="codex",
                role="freestanding",
                name=member.name if (member := membership.by_current_session(session.thread_id)) is not None else "",
                status=session.status,
                reach=Messageable(f"codex:{session.thread_id}"),
                cwd=session.cwd,
                showrunner=NoShowrunner(),
                director=NoDirector(),
                assignment=NotAWorker(),
                thread=CodexThread(session.thread_id),
                account=_codex_home_account(owner.home),
                daemon=owner,
            )
            for session in daemon
            if session.thread_id not in worker_threads
            and (isinstance(participants, AllRosterSessions) or session.thread_id in participants.native_ids)
        )
    interactive = _herdr_codex_panes(machine, (runner["name"] for runner in registered),
                                    worker_threads=worker_threads, membership=membership, participants=participants)
    problems.extend(interactive.problems)
    excluded = diagnostics.excluded_sessions
    claude = {key: entry for key, entry in claude.items() if key not in worker_threads}
    entries = [entry for entry in [*claude.values(), *unit_workers, *codex_sessions, *interactive.entries]
               if not (isinstance(entry.session, ClaudeSession) and entry.session.session_id in excluded
                       or isinstance(entry.thread, CodexThread) and entry.thread.thread_id in excluded)]
    return Roster(entries, problems, warnings, tuple(diagnostics.anomalies.values()))


@dataclass(frozen=True)
class PeerNotReached:
    host: str
    reason: str


def peer_snapshot(environment: Mapping[str, str]) -> Roster | PeerNotReached:
    """Read only the peer, decoding states immediately at the ssh boundary."""
    host = remote.other_machine()
    command = ["ssh", "-o", "BatchMode=yes", host,
               '\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/message/roster.py\" --here --snapshot']
    try:
        # ssh forwards its input to the peer. It gets none, so it never waits on the caller's
        # input or takes text the caller was meant to read.
        done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False,
                              stdin=subprocess.DEVNULL,
                              env={key: value for key, value in environment.items()
                                   if key not in {"CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"}})
    except (OSError, subprocess.TimeoutExpired) as error:
        why = "timed out after 20 s" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        lines = why.splitlines()
        return PeerNotReached(host, lines[0] if lines else type(error).__name__)
    try:
        result = RosterSnapshot.decode(done.stdout).result
    except (ValueError, TypeError):
        detail = (done.stderr or "invalid roster snapshot").strip().splitlines()
        return PeerNotReached(host, detail[0] if detail else "invalid roster snapshot")
    return Roster(result.entries, [f"{host}: {problem}" for problem in result.problems],
                  [f"{host}: {warning}" for warning in result.warnings])


def peer_roster(environment: Mapping[str, str]) -> Roster:
    result = peer_snapshot(environment)
    if isinstance(result, PeerNotReached):
        return Roster([], [f"{result.host}: not reached: {result.reason}"])
    return result


@role_map_command
def roster(environment: Mapping[str, str], scope: RosterScope, *,
           membership: staff.StaffSnapshot | None = None, registry: RegistryRead | None = None,
           participants: AllRosterSessions | MessageParticipants = ALL_ROSTER_SESSIONS) -> Roster:
    problems: list[str] = []
    try:
        membership = staff.initial_snapshot() if membership is None else membership
    except staff.StaffUnreadable as error:
        _problem(problems, "staff membership", error)
        other = peer_roster(environment) if isinstance(scope, AllMachines) else Roster([], [])
        return Roster(other.entries, [*problems, *other.problems], other.warnings, other.row_anomalies)
    registry = sessions.read_registry() if registry is None else registry
    match scope:
        case ThisMachine():
            local = _local_roster(environment, membership=membership, registry=registry, participants=participants)
            local.problems[:0] = problems
            return local
        case AllMachines():
            with ThreadPoolExecutor(max_workers=2) as pool:
                peer = pool.submit(peer_roster, environment)
                local = _local_roster(environment, membership=membership, registry=registry, participants=participants)
                local.problems[:0] = problems
                other = peer.result()
            return Roster([*local.entries, *other.entries], [*local.problems, *other.problems],
                          [*local.warnings, *other.warnings], (*local.row_anomalies, *other.row_anomalies))


def _home_shortened(path: str) -> str:
    if not path:
        return ""
    home = str(Path(os.environ.get("HOME", str(Path.home()))).expanduser())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _entry_line(entry: RosterEntry, *, you: bool, context: bool) -> str:
    shown = shown_name(entry)
    rendered = shown.render()
    name = f"{rendered} [{shown.address}]" if rendered != shown.address else rendered
    if you:
        name += " (you)"
    if context:
        name += " (context)"
    status = "background" if entry.kind == "claude" and entry.status == "shell" else entry.status
    details = [entry.kind, status]
    if isinstance(shown.job, JobUnassigned):
        details.append(shown.job.reason)
    unit = _unit(entry)
    if unit:
        details.append(unit)
    cwd = _home_shortened(entry.cwd)
    if cwd:
        details.append(cwd)
    prefix = "" if entry.role == "worker" and isinstance(shown.job, ShownJob) else f"{entry.role} "
    return f"{prefix}{name} — {', '.join(details)}"


def _chosen_entries(
    entries: list[RosterEntry], kind: Kind | None
) -> tuple[list[RosterEntry], set[RosterEntry]]:
    if kind is None:
        return entries, set()
    chosen = [entry for entry in entries if entry.kind == kind]
    contexts: set[RosterEntry] = set()
    for worker in (entry for entry in chosen if entry.role == "worker"):
        director = next(
            (
                entry
                for entry in entries
                if _director_address(worker)
                and entry.machine == worker.machine
                and entry.role == "unit director"
                and _session_address(entry) == _director_address(worker)
            ),
            None,
        )
        if director is not None and director not in chosen:
            contexts.add(director)
        runner = next(
            (
                entry
                for entry in entries
                if _showrunner_address(worker)
                and entry.machine == worker.machine
                and entry.role == "showrunner"
                and _session_address(entry) == _showrunner_address(worker)
            ),
            None,
        )
        if runner is not None and runner not in chosen:
            contexts.add(runner)
    return [*chosen, *contexts], contexts


def _print_text(
    entries: list[RosterEntry], contexts: set[RosterEntry], identity: SenderIdentity
) -> None:
    by_machine: dict[str, list[RosterEntry]] = {}
    for entry in entries:
        by_machine.setdefault(entry.machine, []).append(entry)
    for machine in by_machine:
        print(machine)
        rows = by_machine[machine]
        printed: set[RosterEntry] = set()

        def emit(entry: RosterEntry, depth: int) -> None:
            print("  " * depth + _entry_line(
                entry,
                you=machine == machine_name() and is_sender(entry, identity),
                context=entry in contexts,
            ))
            printed.add(entry)

        runners = sorted(
            (entry for entry in rows if entry.role == "showrunner"),
            key=lambda entry: (entry.name, _session_address(entry)),
        )
        for runner in runners:
            emit(runner, 1)
            directors = sorted(
                (
                    entry for entry in rows
                    if entry.role == "unit director"
                    and _showrunner_address(entry) == _session_address(runner)
                ),
                key=lambda entry: (_unit(entry), entry.name),
            )
            for director in directors:
                emit(director, 2)
                for worker in sorted(
                    (
                        entry for entry in rows
                        if entry.role == "worker"
                        and _director_address(entry) == _session_address(director)
                    ),
                    key=lambda entry: (_slot_role_for(entry), entry.name),
                ):
                    emit(worker, 3)
        independent = sorted(
            (
                entry for entry in rows
                if entry.role == "unit director" and entry not in printed
            ),
            key=lambda entry: (_unit(entry), entry.name),
        )
        for director in independent:
            emit(director, 1)
            for worker in sorted(
                (
                    entry for entry in rows
                    if entry.role == "worker"
                    and _director_address(entry) == _session_address(director)
                ),
                key=lambda entry: (_slot_role_for(entry), entry.name),
            ):
                emit(worker, 2)
        orphan_groups: dict[str, list[RosterEntry]] = {}
        for worker in (entry for entry in rows if entry.role == "worker" and entry not in printed):
            orphan_groups.setdefault(_session_dir(worker), []).append(worker)
        for session_dir, workers in sorted(orphan_groups.items()):
            print(f"  director not running: {session_dir}")
            for worker in sorted(workers, key=lambda entry: (_slot_role_for(entry), entry.name)):
                emit(worker, 2)
        for entry in sorted(
            (entry for entry in rows if entry.kind == "claude" and entry.role == "freestanding"),
            key=lambda entry: entry.name,
        ):
            emit(entry, 1)
        codex = sorted(
            (entry for entry in rows if entry.kind == "codex" and entry.role == "freestanding"),
            key=lambda entry: entry.name,
        )
        if codex:
            print("  your Codex sessions")
            for entry in codex:
                emit(entry, 2)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true", dest="as_json")
    _ = parser.add_argument("--kind", choices=("claude", "codex"))
    _ = parser.add_argument("--here", action="store_true", help="read this machine only")
    _ = parser.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    result = roster(os.environ, ThisMachine() if getattr(arguments, "here", False) is True else AllMachines())
    if getattr(arguments, "snapshot", False) is True:
        print(RosterSnapshot(result).encode())
        return 1 if result.problems else 0
    raw_kind = getattr(arguments, "kind", None)
    kind = raw_kind if raw_kind in ("claude", "codex") else None
    entries, contexts = _chosen_entries(result.entries, kind)
    if getattr(arguments, "as_json", False) is True:
        print(json.dumps({
            "entries": [_serialized_entry(entry) for entry in entries],
            "problems": result.problems,
            **({"warnings": result.warnings} if result.warnings else {}),
        }))
    else:
        _print_text(entries, contexts, sender_identity(os.environ))
    for problem in result.diagnostics:
        print(f"could not read: {problem}", file=sys.stderr)
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
