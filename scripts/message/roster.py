#!/usr/bin/env python3
"""List Claude and Codex sessions on both workstations; --here reads only this one."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for _directory in (SCRIPTS / "production", SCRIPTS / "direct", SCRIPTS / "herdr", SCRIPTS / "whoami", SCRIPTS / "message", SCRIPTS / "lib"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import remove_workers  # noqa: E402
from worker_state import worker_ledger  # noqa: E402
import showrunners  # noqa: E402
import unit_lookup  # noqa: E402
import unit_runs  # noqa: E402
import herdr  # noqa: E402
import codex_daemon  # noqa: E402
import process_table  # noqa: E402
from scripts.astra import design_roles  # noqa: E402
from scripts.lib import names  # noqa: E402
from scripts.lib.names import JobKey  # noqa: E402
if TYPE_CHECKING:
    from ..accounts.role_map import role_map_command
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
from scripts.shutdown import remote as remote  # noqa: E402
from sessions import SessionRecord, messaging_socket_exists  # noqa: E402

from scripts.message.unit_labels import (
    DirectorRelationship as DirectorRelationship,
    DirectsUnit as DirectsUnit,
    NoDirector as NoDirector,
    NoShowrunner as NoShowrunner,
    ProductionWithoutRunningShowrunner as ProductionWithoutRunningShowrunner,
    RunsProduction as RunsProduction,
    ShowrunnerRelationship as ShowrunnerRelationship,
    UnderDirector as UnderDirector,
    UnderShowrunner as UnderShowrunner,
    UnitLabels as UnitLabels,
    UnitWithoutRunningDirector as UnitWithoutRunningDirector,
    DirectorSession,
    LiveUnitRun,
    MeshWorker,
    NoRunningDirector,
    ProductionPlace,
    RunOwnership,
    string_value,
    listed_mesh_worker,
    live_runs,
    mesh_records,
    newest_sessions,
    record_problem,
    production_of,
    production_places_from_marks,
    MarkedDirectorDiscovery,
    WorktreeDirectorDiscovery,
    read_object,
    registered_showrunners,
    short_unit,
    showrunner_ids,
    stopped_run_place,
    make_labels,
    unit_of,
    unit_labels_for as unit_labels_for,
)

Kind = Literal["claude", "codex"]
Role = Literal["showrunner", "unit director", "worker", "freestanding", "astra-designer", "astra-reviewer"]
WorkerRole = Literal["impl", "test", "fix", "review"]
KNOWN_SLOT_ROLES = frozenset(("impl", "test", "fix", "review"))
MESSAGEABLE_WORKER_STATUSES = frozenset(("running", "waiting_capacity"))


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
    """A Codex shell can inherit its launching Claude session's id."""
    if thread_id := environment.get("CODEX_THREAD_ID", ""):
        return CodexThread(thread_id)
    if session_id := environment.get("CLAUDE_CODE_SESSION_ID", ""):
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
    reviewer_type: Literal["code", "ui"] = "code"


@dataclass(frozen=True)
class NotAWorker:
    """This session is not assigned to a worker."""


@dataclass(frozen=True)
class HeldDesignRole:
    role: Literal["designer", "reviewer"]
    mesh: str


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
    slug: str


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

    def render(self) -> str:
        parts: list[str] = []
        if isinstance(self.production, ProductionPart) and self.production.slug:
            parts.append(self.production.slug)
        if isinstance(self.unit, UnitPart) and self.unit.unit:
            parts.append(short_unit(self.unit.unit))
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
        return " · ".join(parts) or self.address


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
        record = str(Path(assignment.session_dir) / ("mesh_roster.json" if entry.kind == "codex" else "board.log")) if assignment.session_dir else entry.name
        if isinstance(assignment.role, AssignmentUnrecorded):
            job = JobUnassigned(f"assignment not recorded: {record}")
        elif isinstance(assignment.role, AssignmentUnreadable):
            job = JobUnassigned(f"assignment record not readable: {record}: {assignment.role.value}")
        else:
            slot = assignment.slot
            pool = re.fullmatch(r"(code|ui)([0-9]+)", slot)
            if assignment.role == "review" and pool:
                job = ShownJob("ui_reviewer" if assignment.reviewer_type == "ui" else "code_reviewer",
                               JobNumber(int(pool[2])))
            else:
                job = ShownJob("code_reviewer" if assignment.role == "review" else "worker",
                               JobSlot(slot) if slot else Unqualified())
    address = _address(entry) or _session_address(entry)
    return ShownName(ProductionPart(production) if production else NoProduction(),
                     UnitPart(unit) if unit else NoUnit(), job, address or entry.name)


@dataclass(frozen=True)
class Roster:
    entries: list[RosterEntry]
    problems: list[str]


def unit_labels(entries: Iterable[RosterEntry]) -> dict[str, UnitLabels]:
    labels: dict[str, UnitLabels] = {}
    for entry in entries:
        identity = make_labels(entry.name, entry.showrunner, entry.director)
        if entry.kind == "claude" and isinstance(entry.session, ClaudeSession):
            labels[entry.session.session_id] = identity
        elif entry.kind == "codex" and isinstance(entry.thread, CodexThread):
            labels[entry.thread.thread_id] = identity
    return labels


TaggedSnapshotState = (MessageReach | ShowrunnerRelationship | DirectorRelationship | NotAWorker
                       | CodexThread | NoCodexThread | ClaudeSession | NoClaudeSession | AccountAttribution)
SnapshotState = TaggedSnapshotState | WorkerAssignment | HeldDesignRole
_SNAPSHOT_STATES: tuple[type[TaggedSnapshotState], ...] = (
    Messageable, CannotReceiveMessages, UnderShowrunner, RunsProduction,
    ProductionWithoutRunningShowrunner, NoShowrunner, UnderDirector, DirectsUnit,
    UnitWithoutRunningDirector, NoDirector, NotAWorker, CodexThread, NoCodexThread,
    ClaudeSession, NoClaudeSession, AccountLabel, AccountUnread, AccountAttributionUnavailable,
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


def _tagged_state(state: TaggedSnapshotState) -> dict[str, str]:
    return {"tag": type(state).__name__, **cast(dict[str, str], asdict(state))}


def _assignment_snapshot(assignment: Assignment) -> dict[str, object]:
    if isinstance(assignment, NotAWorker):
        return {"tag": "NotAWorker"}
    if isinstance(assignment, HeldDesignRole):
        return {"tag": "HeldDesignRole", "slot_role": "astra-" + assignment.role,
                "session_dir": assignment.mesh}
    role = assignment.role
    encoded_role: object = role if isinstance(role, str) else {
        "tag": type(role).__name__, **cast(dict[str, str], asdict(role))}
    return {"tag": "WorkerAssignment", "slot_role": assignment.slot_role,
            "session_dir": assignment.session_dir, "role": encoded_role,
            "slot": assignment.slot, "reviewer_type": assignment.reviewer_type}


def _snapshot_entry(entry: RosterEntry) -> SnapshotEntry:
    return SnapshotEntry(
        machine=entry.machine, kind=entry.kind, role=entry.role, name=entry.name,
        status=entry.status, cwd=entry.cwd, reach=_tagged_state(entry.reach),
        showrunner=_tagged_state(entry.showrunner), director=_tagged_state(entry.director),
        assignment=_assignment_snapshot(entry.assignment), thread=_tagged_state(entry.thread),
        session=_tagged_state(entry.session), account=_tagged_state(entry.account),
        daemon=({"home": str(entry.daemon.home), "socket": str(entry.daemon.socket)}
                if isinstance(entry.daemon, codex_daemon.CodexDaemon) else {}),
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
        if not required <= set(values) or set(values) - required - {"role", "slot", "reviewer_type"}:
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
        reviewer_type = values.get("reviewer_type", "code")
        if reviewer_type not in ("code", "ui"):
            raise ValueError("invalid snapshot reviewer type")
        return WorkerAssignment(_snapshot_string(values, "slot_role"),
                                _snapshot_string(values, "session_dir"), role,
                                _snapshot_string(values, "slot") if "slot" in values else "",
                                reviewer_type)
    for state in _SNAPSHOT_STATES:
        if state.__name__ == tag:
            names = [field.name for field in fields(state)]
            if set(values) != {"tag", *names}:
                raise ValueError(f"invalid fields for snapshot state {tag}")
            return state(**{name: _snapshot_string(values, name) for name in names})
    raise ValueError(f"unknown snapshot state {tag}")


def _decode_entry(value: object) -> RosterEntry:
    values = _snapshot_object(value)
    required = set(SnapshotEntry.__annotations__) - {"account", "daemon"}
    if not required <= set(values) or set(values) - required - {"account", "daemon"}:
        raise ValueError("invalid snapshot entry fields")
    reach = _decode_state(values.get("reach"))
    showrunner = _decode_state(values.get("showrunner"))
    director = _decode_state(values.get("director"))
    assignment = _decode_state(values.get("assignment"))
    thread = _decode_state(values.get("thread"))
    session = _decode_state(values.get("session"))
    attributed = (_decode_state(values["account"]) if "account" in values
                  else AccountAttributionUnavailable("peer sent no account"))
    daemon: codex_daemon.CodexDaemon | NotDaemonSession = NotDaemonSession()
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
            and isinstance(attributed, AccountAttribution)):
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
        thread=thread, session=session, account=attributed, daemon=daemon,
    )


@dataclass(frozen=True)
class RosterSnapshot:
    """Lossless states exchanged between the two workstations."""

    result: Roster

    def encode(self) -> str:
        return json.dumps({"tag": "RosterSnapshot", "entries": [
            _snapshot_entry(entry) for entry in self.result.entries
        ], "problems": self.result.problems})

    @classmethod
    def decode(cls, text: str) -> RosterSnapshot:
        values = _snapshot_object(cast(object, json.loads(text)))
        entries, problems = values.get("entries"), values.get("problems")
        if (values.get("tag") != "RosterSnapshot" or not isinstance(entries, list)
                or not isinstance(problems, list)):
            raise ValueError("not a roster snapshot")
        raw_problems = cast(list[object], problems)
        if not all(isinstance(problem, str) for problem in raw_problems):
            raise ValueError("snapshot problems must be strings")
        return cls(Roster([_decode_entry(entry) for entry in cast(list[object], entries)],
                          cast(list[str], raw_problems)))


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
    return production_of(entry.showrunner)


def _showrunner_name(entry: RosterEntry) -> str:
    return entry.showrunner.name if isinstance(entry.showrunner, UnderShowrunner) else ""


def _showrunner_address(entry: RosterEntry) -> str:
    return entry.showrunner.address if isinstance(entry.showrunner, UnderShowrunner) else ""


def _unit(entry: RosterEntry) -> str:
    return unit_of(entry.director)


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
        return entry.assignment.mesh
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
class _ClaudeDetails:
    cwd: str
    status: str


@dataclass(frozen=True)
class _WorkerPlace:
    run: LiveUnitRun
    worker_name: str


@dataclass(frozen=True)
class _ReviewerProcess:
    kind: Kind
    pid: int


@dataclass(frozen=True)
class _ReviewerProcessNotFound:
    """No Claude or Codex descendant is visible in the process table."""


_ReviewerProcessState = _ReviewerProcess | _ReviewerProcessNotFound


def _raw_session_details(records: list[SessionRecord], problems: list[str]) -> dict[str, _ClaudeDetails]:
    directory = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))
    try:
        paths = [path for path in directory.iterdir() if path.suffix == ".json"]
    except OSError as error:
        record_problem(problems, "Claude session records", error)
        paths = []
    chosen = {(record["sessionId"], record["pid"]) for record in records}
    chosen_files = {str(record["pid"]) for record in records}
    raw: dict[tuple[str, int], tuple[int, _ClaudeDetails]] = {}
    for path in paths:
        try:
            data = read_object(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            if path.stem in chosen_files:
                record_problem(problems, f"Claude session record {path}", error)
            continue
        pid = data.get("pid")
        key = (string_value(data.get("sessionId")), pid if isinstance(pid, int) else 0)
        if key not in chosen:
            continue
        updated_value = data.get("updatedAt")
        updated = updated_value if isinstance(updated_value, int) else 0
        if key not in raw or updated > raw[key][0]:
            raw[key] = (updated, _ClaudeDetails(
                cwd=string_value(data.get("cwd")), status=string_value(data.get("status"))
            ))
    details: dict[str, _ClaudeDetails] = {}
    for record in records:
        session_id = record["sessionId"]
        detail = raw.get((session_id, record["pid"]), (0, _ClaudeDetails("", "")))[1]
        cwd = detail.cwd
        if not cwd:
            try:
                cwd = os.readlink(f"/proc/{record['pid']}/cwd")
            except OSError:
                cwd = ""
        details[session_id] = _ClaudeDetails(cwd, detail.status or "running")
    return details


def _every_marked_unit() -> unit_lookup.MarkedProductions | OSError:
    """Every production's marked units, or why the included hosts could not answer."""
    try:
        return unit_lookup.every_marked_unit()
    except OSError as error:
        return error


@dataclass(frozen=True)
class _DiscoverProductionMarks:
    """The full roster discovers marks across all running panes."""


_DISCOVER_PRODUCTION_MARKS = _DiscoverProductionMarks()


def _production_places(
    records: list[SessionRecord], details: Mapping[str, _ClaudeDetails],
    registered: list[showrunners.Showrunner],
    runner_ids: Mapping[str, showrunners.Showrunner], ledger_workers: set[str],
    problems: list[str],
    *, marks: unit_lookup.MarkedProductions | _DiscoverProductionMarks = _DISCOVER_PRODUCTION_MARKS,
) -> dict[str, ProductionPlace]:
    # One read of every production's marks serves every showrunner below.
    every = (marks if isinstance(marks, unit_lookup.MarkedProductions)
             else _every_marked_unit() if registered else unit_lookup.MarkedProductions({}, {}))
    resolved = production_places_from_marks(registered, runner_ids, every, problems)
    places = resolved.sessions
    # Only the full roster discovers socketless directors or falls back to worktrees.
    record_places: dict[int, herdr.HerdrPlace] | None = None
    for pending in resolved.discovery:
        if isinstance(pending, MarkedDirectorDiscovery) and record_places is None:
            record_places = herdr.herdr_places()
        for record in records:
            if record["sessionId"] in ledger_workers:
                continue
            match pending:
                case MarkedDirectorDiscovery(ownership, mark):
                    if record_places is not None and record_places.get(record["pid"]) == mark.place:
                        _ = places.setdefault(record["sessionId"], ownership)
                        break
                case WorktreeDirectorDiscovery(ownership, worktree):
                    if worktree and details[record["sessionId"]].cwd == worktree:
                        _ = places.setdefault(record["sessionId"], ownership)
    return places


def _worker_ledger(run: LiveUnitRun, problems: list[str]) -> list[tuple[str, str]]:
    path = run.path / remove_workers.LEDGER
    try:
        path = worker_ledger(run.path)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        record_problem(problems, f"worker ledger {path}", error)
        return []
    workers: list[tuple[str, str]] = []
    for line in lines:
        session_id, separator, name = line.partition("\t")
        if session_id.strip():
            workers.append((session_id.strip(), name.strip() if separator else ""))
    return workers


def _slot_role(name: str) -> str:
    """Read a slot identity for legacy launch records, never a job assignment."""
    candidate = name.rpartition("-")[2]
    return candidate


def _worker_slot(run: Path, name: str, record: Mapping[str, object], problems: list[str]) -> str:
    """The recorded slot can contain hyphens and is independent of the address."""
    if slot := string_value(record.get("slot")):
        return slot
    fallback = _slot_role(name)
    for path in sorted(run.glob("impl_worker_*.json")):
        try:
            launch = read_object(path)
        except (OSError, UnicodeError, ValueError) as error:
            record_problem(problems, f"worker launch {path}", error)
            continue
        if launch.get("name") == name:
            return string_value(launch.get("slot")) or fallback
    return fallback


def _worker_assignment(run: Path, slot: str, record: Mapping[str, object]) -> WorkerAssignment:
    role = worker_role(record)
    legacy_role = string_value(record.get("role"))
    if not legacy_role:
        label = "no role"
    elif legacy_role not in KNOWN_SLOT_ROLES:
        label = "role unknown"
    else:
        lens = string_value(record.get("lens"))
        label = f"review ({lens})" if legacy_role == "review" and lens else legacy_role
    return WorkerAssignment(label, str(run), role, slot)


@dataclass(frozen=True)
class _PoolReviewer:
    reviewer_type: Literal["code", "ui"]
    director: UnderDirector | NoDirector


def _pool_root() -> Path:
    return Path(os.environ.get("REVIEW_POOL_STATE", "~/.local/state/review-pool")).expanduser().resolve()


def _pool_reviewers(path: Path) -> dict[str, _PoolReviewer]:
    reviewers = _snapshot_object(read_object(path).get("reviewers"))
    result: dict[str, _PoolReviewer] = {}
    for name, raw in reviewers.items():
        reviewer = _snapshot_object(raw)
        kind = reviewer.get("type")
        if kind not in ("code", "ui"):
            raise ValueError(f"invalid reviewer type for {name}")
        lease = reviewer.get("lease")
        unit = _snapshot_string(_snapshot_object(lease), "unit") if lease is not None else ""
        result[name] = _PoolReviewer(kind, UnderDirector(unit, "", "") if unit else NoDirector())
    return result


def _pool_context(run: Path, name: str) -> tuple[ShowrunnerRelationship, DirectorRelationship, str]:
    """A pool reviewer's current lease supplies its unit, never its mesh name."""
    root = _pool_root()
    try:
        relative = run.resolve().relative_to(root)
    except ValueError:
        return NoShowrunner(), NoDirector(), ""
    if len(relative.parts) != 3 or relative.parts[1] != "mesh":
        return NoShowrunner(), NoDirector(), ""
    slug = relative.parts[0]
    try:
        reviewer = _pool_reviewers(root / slug / "pool.json")[name]
    except (OSError, UnicodeError, ValueError, KeyError):
        return ProductionWithoutRunningShowrunner(slug), NoDirector(), ""
    return ProductionWithoutRunningShowrunner(slug), reviewer.director, reviewer.reviewer_type


def _held_location(holder: design_roles.Holder) -> tuple[ShowrunnerRelationship, DirectorRelationship]:
    return (ProductionWithoutRunningShowrunner(holder.production_slug),
            UnderDirector(holder.unit_name, "", "") if isinstance(holder, design_roles.UnitHolder) else NoDirector())


def unit_worker_title(production: str, unit: str, job: JobKey, ordinal: int, address: str) -> str:
    """Render a unit worker's title from its ledger placement; a second holder of a job gets a number."""
    qualifier: JobQualifier = JobNumber(ordinal) if ordinal > 1 else Unqualified()
    return ShownName(ProductionPart(production), UnitPart(unit), ShownJob(job, qualifier), address).render()


def mesh_title(session_dir: str, name: str, record: Mapping[str, object]) -> str:
    return mesh_shown_name(session_dir, name, record).render()


def mesh_shown_name(session_dir: str, name: str, record: Mapping[str, object]) -> ShownName:
    """Build a thread title with the same production and unit lookup as the roster."""
    run = Path(session_dir)
    if design_id := string_value(record.get("held_design_id")):
        held_role = record.get("held_role")
        try:
            design = design_roles.read_design(design_id)
        except (OSError, UnicodeError, ValueError, TypeError):
            return ShownName(NoProduction(), NoUnit(), JobUnassigned("design unreadable"), name)
        if held_role not in ("designer", "reviewer"):
            return ShownName(NoProduction(), NoUnit(), JobUnassigned("held role unreadable"), name)
        showrunner, director = _held_location(design.holder)
        return shown_name(RosterEntry(
            machine="", kind="codex", role="astra-designer" if held_role == "designer" else "astra-reviewer",
            name=name, status="", reach=Messageable(name), cwd=design.repository,
            showrunner=showrunner, director=director, assignment=HeldDesignRole(held_role, session_dir)))
    showrunner, director, reviewer_type = _pool_context(run, name)
    if isinstance(showrunner, NoShowrunner):
        problems: list[str] = []
        records = newest_sessions()
        details = _raw_session_details(records, problems)
        registered = registered_showrunners(os.environ, problems)
        places = _production_places(records, details, registered,
                                    showrunner_ids(records, registered), set(), problems)
        place = places.get(unit_runs.director(run))
        if place is not None:
            showrunner, director = place.showrunner, UnderDirector(place.director.unit, "", "")
    assignment = _worker_assignment(run, _worker_slot(run, name, record, []), record)
    if reviewer_type in ("code", "ui"):
        assignment = WorkerAssignment(assignment.slot_role, session_dir, assignment.role, name,
                                      reviewer_type)
    entry = RosterEntry(machine="", kind="codex", role="worker", name=name, status="",
                        reach=Messageable(name), cwd="", showrunner=showrunner,
                        director=director, assignment=assignment)
    # A detached, unplaced thread retains its existing address as its title.
    if isinstance(showrunner, NoShowrunner) and isinstance(director, NoDirector):
        return ShownName(NoProduction(), NoUnit(), JobUnassigned("no unit record"), name)
    return shown_name(entry)


def _claude_assignment(run: Path, name: str, problems: list[str]) -> WorkerAssignment:
    """The launch names the slot; the board records its current assignment."""
    slot = _slot_role(name)
    legacy_label = slot if slot in KNOWN_SLOT_ROLES else "role unknown"
    slot = _worker_slot(run, name, {}, problems)
    try:
        result = subprocess.run(["bash", str(SCRIPTS / "direct/board.sh"), "roles", str(run)],
                                capture_output=True, text=True, timeout=5, check=True,
                                stdin=subprocess.DEVNULL)
        roles = dict(line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line)
    except (OSError, subprocess.SubprocessError) as error:
        record_problem(problems, f"worker assignments {run}", error)
        return WorkerAssignment(legacy_label, str(run), AssignmentUnreadable(str(error)), slot)
    assignment = worker_role({"role": roles[slot]} if slot in roles else {})
    return WorkerAssignment(legacy_label, str(run), assignment, slot)


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
    run: LiveUnitRun,
    claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, ProductionPlace],
) -> RunOwnership:
    if isinstance(run.director, NoRunningDirector):
        return stopped_run_place(run.path)
    director = claude_entries.get(run.director.session_id)
    if director is None:
        return stopped_run_place(run.path)
    place = production_places.get(run.director.session_id)
    unit = place.director.unit if place is not None else ""
    director_link = UnderDirector(unit, director.name, _session_address(director))
    return RunOwnership(
        director=director_link,
        showrunner=place.showrunner if place is not None else NoShowrunner(),
        cwd=director.cwd,
    )


def _between_turns_reason(director: DirectorRelationship) -> str:
    match director:
        case UnderDirector():
            return "between turns; its unit director resumes it"
        case _:
            return "between turns; no unit director is running"


def _run_server_homes(run: LiveUnitRun) -> dict[int, Path]:
    """Homes of serving servers, including turns finishing on retired servers."""
    homes: dict[int, Path] = {}
    records: list[dict[str, object]] = []
    try:
        retired = read_object(run.path / "mesh_retired.json").get("servers", [])
        if isinstance(retired, list):
            for item in cast(list[object], retired):
                if isinstance(item, dict):
                    records.append(cast(dict[str, object], item))
    except (OSError, UnicodeError, ValueError):
        pass
    try:
        records.append(read_object(run.path / "mesh_server.json"))
    except (OSError, UnicodeError, ValueError):
        pass
    for record in records:
        port, home = record.get("port"), record.get("codex_home")
        if isinstance(port, int) and not isinstance(port, bool):
            _ = homes.pop(port, None)
            if isinstance(home, str) and home:
                homes[port] = Path(home)
    return homes


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


def _worker_account(run: LiveUnitRun, worker: MeshWorker, homes: dict[int, Path]) -> AccountLabel | AccountUnread:
    port = worker.get("port")
    if port is None:
        try:
            port = read_object(run.path / "mesh_server.json").get("port")
        except (OSError, UnicodeError, ValueError):
            pass
    if isinstance(port, int) and not isinstance(port, bool) and port in homes:
        return _codex_home_account(homes[port])
    # Idle shutdown removes server metadata; the next turn uses the workers home.
    try:
        return _codex_home_account(agent_accounts.workers_home())
    except ValueError as error:
        return AccountUnread(str(error))


def _worker_status(status: str) -> str:
    return {
        "done": "idle",
        "waiting_capacity": "waiting for capacity",
        "capacity_exhausted": "out of capacity",
    }.get(status, status)


def _process_cwds(table: Mapping[int, process_table.Process]) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for process in table.values():
        try:
            words = shlex.split(process.arguments)
        except ValueError:
            words = process.arguments.split()
        options = dict(zip(words, words[1:]))
        name = options.get("--name") or options.get("--to")
        directory = options.get("--session-dir")
        if process_table.is_codex_agent(process) and name and directory:
            found[(name, str(Path(directory).resolve()))] = options.get("--cwd", "")
    return found


def _ledger_workers(
    runs: Iterable[LiveUnitRun], records: Iterable[SessionRecord], problems: list[str]
) -> dict[str, _WorkerPlace]:
    workers: dict[str, _WorkerPlace] = {}
    records_list = list(records)
    for run in runs:
        for ledger_id, worker_name in _worker_ledger(run, problems):
            for record in records_list:
                session_id = record["sessionId"]
                if session_id == ledger_id or session_id.startswith(ledger_id):
                    _ = workers.setdefault(session_id, _WorkerPlace(run, worker_name))
    return workers


def _claude_entries(
    machine: str, records: list[SessionRecord], details: Mapping[str, _ClaudeDetails],
    runner_ids: Mapping[str, showrunners.Showrunner],
    production_places: Mapping[str, ProductionPlace], runs: list[LiveUnitRun],
    workers: Mapping[str, _WorkerPlace], *,
    home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
    problems: list[str],
) -> dict[str, RosterEntry]:
    marker_directors = {
        run.director.session_id
        for run in runs
        if isinstance(run.director, DirectorSession)
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
            showrunner = RunsProduction(runner_ids[session_id]["slug"])
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
            assignment = _claude_assignment(worker.run.path, worker.worker_name or record["name"], problems)
            run_director = worker.run.director
            if isinstance(run_director, NoRunningDirector):
                place = stopped_run_place(worker.run.path)
                showrunner, director = place.showrunner, place.director
            director_entry = (
                records_by_id.get(run_director.session_id)
                if isinstance(run_director, DirectorSession) else None
            )
            if director_entry is not None and isinstance(run_director, DirectorSession):
                director_place = production_places.get(run_director.session_id)
                if director_place is not None:
                    showrunner = director_place.showrunner
                    unit = director_place.director.unit
                else:
                    unit = ""
                director = UnderDirector(
                    unit,
                    director_entry["name"],
                    f"session:{run_director.session_id}",
                )
        else:
            role = "freestanding"
        detail = details[session_id]
        entries[session_id] = RosterEntry(
            machine=machine,
            kind="claude",
            role=role,
            name=record["name"],
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
    machine: str, run: LiveUnitRun, place: RunOwnership,
    table: Mapping[int, process_table.Process], problems: list[str], *,
    home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
) -> list[RosterEntry]:
    entries: list[RosterEntry] = []
    try:
        # A launch that names its phase writes in that phase's directory.
        statuses = sorted([*run.path.glob("review_status*"), *run.path.glob("phases/*/review_status*")])
    except OSError as error:
        record_problem(problems, f"review state in {run.path}", error)
        return entries
    for status_path in statuses:
        suffix = status_path.name.removeprefix("review_status")
        if suffix and not suffix.startswith("_"):
            continue
        lens = suffix.removeprefix("_")
        try:
            status = status_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            record_problem(problems, f"review status {status_path}", error)
            continue
        if status != "reviewing":
            continue
        pid_path = status_path.parent / f"review_pid{suffix}"
        try:
            launcher_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError, ValueError) as error:
            record_problem(problems, f"review pid {pid_path}", error)
            continue
        if not _pid_alive(launcher_pid):
            continue
        process = _reviewer_process(launcher_pid, table)
        if isinstance(process, _ReviewerProcessNotFound):
            continue
        name = f"{lens} reviewer" if lens else "blind reviewer"
        slot_role = f"review ({lens})" if lens else "review"
        entries.append(RosterEntry(
            machine=machine,
            kind=process.kind,
            role="worker",
            name=name,
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
    machine: str, runs: list[LiveUnitRun], claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, ProductionPlace],
    problems: list[str], *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
) -> tuple[list[RosterEntry], set[str]]:
    try:
        table = process_table.snapshot()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record_problem(problems, "process table", error)
        table = {}
    process_cwds = _process_cwds(table)
    entries: list[RosterEntry] = []
    worker_threads: set[str] = set()
    for run in runs:
        server_homes = _run_server_homes(run)
        place = _run_place(run, claude_entries, production_places)
        for name, record in sorted(mesh_records(run, problems).items()):
            status = string_value(record.get("status"))
            if not listed_mesh_worker(record):
                continue
            thread_id = string_value(record.get("thread_id"))
            if thread_id:
                worker_threads.add(thread_id)
            run_path = str(run.path.resolve())
            cwd = (
                string_value(record.get("cwd"))
                or process_cwds.get((name, run_path), "")
                or place.cwd
            )
            entries.append(RosterEntry(
                machine=machine,
                kind="codex",
                role="worker",
                name=name,
                status=_worker_status(status),
                reach=(
                    Messageable(f"worker:{run.path.name}/{name}")
                    if status in MESSAGEABLE_WORKER_STATUSES
                    else CannotReceiveMessages(_between_turns_reason(place.director))
                ),
                cwd=cwd,
                showrunner=place.showrunner,
                director=place.director,
                assignment=_worker_assignment(Path(run_path), _worker_slot(run.path, name, record, problems), record),
                thread=CodexThread(thread_id) if thread_id else NoCodexThread(),
                account=_worker_account(run, record, server_homes),
            ))
        entries.extend(_review_workers(machine, run, place, table, problems, home_state=home_state))
    return entries, worker_threads


def _pool_workers(
    machine: str, claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, ProductionPlace], problems: list[str], known_threads: set[str],
) -> tuple[list[RosterEntry], set[str]]:
    """Discover pool reviewers even when their mesh is outside the unit runs."""
    entries: list[RosterEntry] = []
    threads: set[str] = set()
    directors = {
        (place.showrunner.production, place.director.unit):
            UnderDirector(place.director.unit, director.name, _session_address(director))
        for session_id, place in production_places.items()
        if (director := claude_entries.get(session_id)) is not None
    }
    root = _pool_root()
    for path in sorted(root.glob("*/pool.json")):
        try:
            reviewers = _pool_reviewers(path)
            meshes = sorted((path.parent / "mesh").glob("*/mesh_roster.json"))
        except (OSError, UnicodeError, ValueError) as error:
            record_problem(problems, f"review pool {path}", error)
            continue
        for mesh in meshes:
            run = LiveUnitRun(mesh.parent, NoRunningDirector())
            homes = _run_server_homes(run)
            for name, record in sorted(mesh_records(run, problems).items()):
                reviewer = reviewers.get(name)
                status = string_value(record.get("status"))
                thread = string_value(record.get("thread_id"))
                if (reviewer is None or status == "ended" or record.get("transferring_role")
                        or record.get("role_redirect") or not thread or thread in known_threads or thread in threads):
                    continue
                threads.add(thread)
                assignment = _worker_assignment(run.path, name, {**record, "role": "review"})
                director = reviewer.director
                if isinstance(director, UnderDirector):
                    director = directors.get((path.parent.name, director.unit), director)
                entries.append(RosterEntry(
                    machine=machine, kind="codex", role="worker", name=name,
                    status=_worker_status(status),
                    reach=(Messageable(f"worker:review-pool/{path.parent.name}/{run.path.name}/{name}")
                           if status in MESSAGEABLE_WORKER_STATUSES else
                           CannotReceiveMessages(_between_turns_reason(director))),
                    cwd=string_value(record.get("cwd")),
                    showrunner=ProductionWithoutRunningShowrunner(path.parent.name),
                    director=director,
                    assignment=WorkerAssignment(assignment.slot_role, assignment.session_dir, "review",
                                                name, reviewer.reviewer_type),
                    thread=CodexThread(thread), account=_worker_account(run, record, homes)))
    return entries, threads


def _durable_roles(machine: str, claude: Mapping[str, RosterEntry], problems: list[str]) -> tuple[list[RosterEntry], set[str]]:
    entries: list[RosterEntry] = []
    threads: set[str] = set()
    for path in sorted(design_roles.state_root().glob('*/design.json')):
        try:
            design = design_roles.read_design(path.parent.name)
            reach = design_roles.resolve_holder(design.holder)
            session = claude.get(reach.session_id) if isinstance(reach, design_roles.HolderReach) else None
            showrunner, director = _held_location(design.holder)
            if isinstance(design.holder, design_roles.UnitHolder):
                director = UnderDirector(design.holder.unit_name, session.name if session else design.holder.unit_name,
                                         f'session:{reach.session_id}' if isinstance(reach, design_roles.HolderReach) else '')
                if session:
                    showrunner = session.showrunner
            elif session:
                showrunner = UnderShowrunner(design.holder.production_slug, session.name,
                                              f'session:{reach.session_id}' if isinstance(reach, design_roles.HolderReach) else '')
            mesh = design_roles.mesh_directory(design.placement.design_id)
            roster = read_object(mesh / 'mesh_roster.json') if (mesh / 'mesh_roster.json').exists() else {}
            for instance in design.roles:
                if isinstance(instance.lifecycle, (design_roles.RoleNotOpened, design_roles.RoleEnded)):
                    continue
                thread = instance.lifecycle.thread_id
                if thread in threads:
                    continue
                raw_entry = roster.get(instance.name, {})
                if not isinstance(raw_entry, dict):
                    raise ValueError('durable roster entry is not an object')
                entry = cast(dict[str, object], raw_entry)
                if (entry.get('transferring_role') or entry.get('role_redirect')
                        or design_roles.transfer_route(thread, design.placement.design_id)):
                    continue
                threads.add(thread)
                status = string_value(entry.get('status')) or type(instance.lifecycle).__name__
                entries.append(RosterEntry(machine=machine, kind='codex',
                    role='astra-designer' if instance.role == 'designer' else 'astra-reviewer',
                    name=instance.name, status=status, reach=CannotReceiveMessages(
                        'held role; its holder adds pending messages to an authorized assignment'),
                    cwd=design.repository, showrunner=showrunner, director=director,
                    assignment=HeldDesignRole(instance.role, str(mesh)), thread=CodexThread(thread),
                    account=AccountUnread('serving home not read')))
        except (OSError, ValueError, TypeError) as error:
            record_problem(problems, 'durable role ' + str(path), error)
    return entries, threads


def _herdr_codex_panes(machine: str) -> Roster:
    """Interactive Codex panes have no messaging address; preserve partial reads."""
    entries: list[RosterEntry] = []
    problems: list[str] = []
    try:
        sessions = herdr.live_sessions()
    except herdr.HerdrMissing:
        # A machine without herdr has no herdr panes to list.
        return Roster(entries, problems)
    except herdr.HerdrFailure as error:
        record_problem(problems, "herdr sessions", error)
        return Roster(entries, problems)
    processes = herdr.herdr_processes()
    cwds = {process.place: process.cwd for process in processes}
    codex_processes = {process.place: process for process in processes if process.command == "codex"}
    for session in sessions:
        name = session["name"]
        try:
            agents = herdr.Herdr(name).agents()
        except herdr.HerdrFailure as error:
            record_problem(problems, f"herdr session {name}", error)
            continue
        for agent in agents:
            if agent.get("agent") != "codex":
                continue
            pane = agent["pane_id"]
            process = codex_processes.get(herdr.HerdrPlace(name, pane))
            entries.append(RosterEntry(
                machine=machine, kind="codex", role="freestanding",
                name=f"herdr {name}:{pane}", status=agent["agent_status"],
                reach=CannotReceiveMessages("interactive Codex in a herdr pane"),
                cwd=cwds.get(herdr.HerdrPlace(name, pane), ""),
                showrunner=NoShowrunner(), director=NoDirector(), assignment=NotAWorker(),
                account=_process_account(process.pid, "codex") if process else AccountUnread("pane process not found"),
            ))
    return Roster(entries, problems)


def machine_name() -> str:
    return socket.gethostname().split(".", 1)[0]


def _local_roster(environment: Mapping[str, str]) -> Roster:
    home_state = account.home_account_state()
    problems: list[str] = []
    machine = machine_name()
    records = newest_sessions()
    details = _raw_session_details(records, problems)
    registered = registered_showrunners(environment, problems)
    runner_ids = showrunner_ids(records, registered)
    roots = unit_runs.run_roots(environment.get("ROSTER_RUN_ROOT"))
    runs = live_runs(roots, {record["sessionId"] for record in records}, problems)
    workers = _ledger_workers(runs, records, problems)
    production_places = _production_places(
        records, details, registered, runner_ids, set(workers), problems
    )
    claude = _claude_entries(
        machine, records, details, runner_ids, production_places, runs, workers,
        home_state=home_state, problems=problems,
    )
    unit_workers, worker_threads = _unit_workers(
        machine, runs, claude, production_places, problems, home_state=home_state
    )
    pool_workers, pool_threads = _pool_workers(machine, claude, production_places, problems, worker_threads)
    unit_workers.extend(pool_workers)
    worker_threads.update(pool_threads)
    durable_roles, durable_threads = _durable_roles(machine, claude, problems)
    unit_workers.extend(durable_roles)
    worker_threads.update(durable_threads)
    codex_sessions: list[RosterEntry] = []
    for result in codex_daemon.read_daemons(environment):
        owner, daemon = result.owner, result.sessions
        if isinstance(daemon, codex_daemon.DaemonUnreadable):
            record_problem(problems, f"Codex daemon {agent_accounts.codex_home_display(owner.home)}", daemon.reason)
            continue
        codex_sessions.extend(
            RosterEntry(
                machine=machine,
                kind="codex",
                role="freestanding",
                name=session.name,
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
        )
    interactive = _herdr_codex_panes(machine)
    problems.extend(interactive.problems)
    entries = [*claude.values(), *unit_workers, *codex_sessions, *interactive.entries]
    return Roster(entries, problems)


@dataclass(frozen=True)
class PeerNotReached:
    host: str
    reason: str


def peer_snapshot(environment: Mapping[str, str]) -> Roster | PeerNotReached:
    """Read only the peer, decoding states immediately at the ssh boundary."""
    host = remote.other_machine()
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
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
    return Roster(result.entries, [f"{host}: {problem}" for problem in result.problems])


def peer_roster(environment: Mapping[str, str]) -> Roster:
    result = peer_snapshot(environment)
    if isinstance(result, PeerNotReached):
        return Roster([], [f"{result.host}: not reached: {result.reason}"])
    return result


@role_map_command
def roster(environment: Mapping[str, str], scope: RosterScope) -> Roster:
    match scope:
        case ThisMachine():
            return _local_roster(environment)
        case AllMachines():
            with ThreadPoolExecutor(max_workers=2) as pool:
                peer = pool.submit(peer_roster, environment)
                local = _local_roster(environment)
                other = peer.result()
            return Roster([*local.entries, *other.entries], [*local.problems, *other.problems])


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
    details = [entry.kind, entry.status]
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
        }))
    else:
        _print_text(entries, contexts, sender_identity(os.environ))
    for problem in result.problems:
        print(f"could not read: {problem}", file=sys.stderr)
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
