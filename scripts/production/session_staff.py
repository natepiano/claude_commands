"""Matching a running session to its staff row."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
import json
import shlex
import subprocess
import tomllib
from importlib import import_module
from collections.abc import Callable, Iterable, Mapping, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple, cast

from scripts.production import staff
from scripts.production.session_labels import StaffLabels
from scripts.production import unit_config as configuration
from scripts.agents import remote
from scripts.message import morty_identity, process_table
from scripts.message.sessions import SessionRecord
from scripts.herdr.place import HerdrPlace
from scripts.herdr.pane_launch import PaneLaunch, agent_kind, without_staff_labels
from scripts.herdr import pane_launch
from scripts.lib import legacy_identity
from scripts.production import unit_names


@dataclass(frozen=True)
class ReportProcessIdentity:
    """A process identity recovered only for the units report."""

    native_id: str
    is_morty: bool


@dataclass
class AnomalyReport:
    check: Callable[[int, str], ReportProcessIdentity] | None
    lines: list[str] = field(default_factory=list)
    processes: dict[int, ReportProcessIdentity] = field(default_factory=dict)
    reported: set[int] = field(default_factory=set)

    def process(self, pid: int, identity: str) -> ReportProcessIdentity:
        if pid and pid in self.processes:
            return self.processes[pid]
        result = self.check(pid, identity) if self.check is not None else ReportProcessIdentity(identity, False)
        if pid:
            self.processes[pid] = result
        return result

    def add(self, message: str, pid: int) -> None:
        message = " ".join(message.splitlines())
        if not pid:
            line = "anomaly: " + message
            if line not in self.lines:
                self.lines.append(line)
            return
        identity = self.process(pid, "")
        if identity.is_morty:
            return
        if pid in self.reported:
            return
        self.reported.add(pid)
        if f"pid {pid}" not in message:
            message = f"pid {pid}: {message}"
        self.lines.append("anomaly: " + message)


_anomaly_report: ContextVar[AnomalyReport | None] = ContextVar("unit_anomaly_report", default=None)


@contextmanager
def anomalies(check: Callable[[int, str], ReportProcessIdentity] | None = None) -> Generator[list[str]]:
    """Collect observations by PID; other callers retain stderr."""
    report = AnomalyReport(check)
    token = _anomaly_report.set(report)
    try:
        yield report.lines
    finally:
        _anomaly_report.reset(token)


def report_process(pid: int, identity: str = "") -> ReportProcessIdentity:
    report = _anomaly_report.get()
    return report.process(pid, identity) if report is not None else ReportProcessIdentity(identity, False)


def collect_anomaly(message: str, *, pid: int = 0) -> bool:
    report = _anomaly_report.get()
    if report is None:
        return False
    report.add(message, pid)
    return True


def report_anomaly(message: str, *, pid: int = 0) -> None:
    if not collect_anomaly(message, pid=pid):
        print("anomaly: " + " ".join(message.splitlines()), file=sys.stderr)


def morty_thread(identity: str) -> bool:
    return bool(identity) and morty_identity.matches(f"codex:{identity}")


WORKER_FACT_KEYS = ("thread_id", "session_id", "codex_home", "manager", "role", "previous_role", "family", "cwd")

PresentWorktree = staff.PresentWorktree
RemovedWorktree = staff.RemovedWorktree
WorktreeUnfilled = staff.WorktreeUnfilled
UnitConfig = configuration.UnitConfig
ConfigMissing = configuration.ConfigMissing


@dataclass(frozen=True)
class RecordedWorker:
    """The worker and its backend come from the same staff generation."""

    row: staff.StaffRow
    backend: staff.ClaudeBackend | staff.CodexBackend


@dataclass(frozen=True)
class WorkerBackendUnavailable:
    reason: str


def worker_backend(record: staff.StaffSnapshot, identity: staff.StaffId
                   ) -> RecordedWorker | WorkerBackendUnavailable:
    row = record.by_id(identity)
    if row is None or row.kind != "worker":
        return WorkerBackendUnavailable(f"{identity}: no worker staff row")
    backend = record.backends.get(row.id)
    if backend is None:
        return WorkerBackendUnavailable(f"{row.id} {row.name}: backend is unavailable")
    return RecordedWorker(row, backend)


def worker_family(record: staff.StaffSnapshot, reference: staff.StaffId) -> str:
    backend = record.backends.get(reference)
    return backend.family if backend is not None else ""


def codex_thread(entry: Mapping[str, object], record: staff.StaffSnapshot | None = None) -> str:
    record = record if record is not None else staff.local_snapshot()
    row = worker_row(entry, record)
    return row.current_session if row is not None and worker_family(record, row.id) == "codex" else ""


def worker_row(entry: Mapping[str, object], record: staff.StaffSnapshot | None = None) -> staff.StaffRow | None:
    record = record if record is not None else staff.local_snapshot()
    return record.by_id(staff.StaffId(str(entry.get("staff_id", ""))))


def worker_session(entry: Mapping[str, object], record: staff.StaffSnapshot | None = None) -> str:
    member = worker_row(entry, record)
    return member.current_session if member is not None else ""


class WorkerRunEvidence(NamedTuple):
    entries: list[tuple[Path, dict[str, object]]]
    runs: list[Path]


def _worker_runs(record: staff.StaffSnapshot, rows: Iterable[staff.StaffRow]) -> dict[staff.StaffId, WorkerRunEvidence]:
    """Read records and staff run associations once."""
    from scripts.lib import unit_runs
    rows = tuple(rows)
    owners = {owner.current_session: owner for row in rows if (owner := record.parent_of(row)) is not None}
    marked: dict[Path, set[staff.StaffId]] = {}
    for owner in owners.values():
        if isinstance(owner.run, staff.OpenRun):
            marked.setdefault(owner.run.path, set()).update(row.id for row in rows if row.parent == staff.ReportsTo(owner.id))
    paths = [*marked, *unit_runs.run_folders()]
    if current := unit_runs.environ("RUN_DIR"):
        paths.append(Path(current))
    found: dict[staff.StaffId, WorkerRunEvidence] = {}
    for run in sorted(dict.fromkeys(paths), key=lambda run: run.stat().st_mtime if run.exists() else 0):
        records: dict[staff.StaffId, dict[str, object]] = {}
        try:
            entries = [cast(object, json.loads(path.read_text())) for path in run.glob("impl_worker_*.json")]
            records = {staff.StaffId(value): cast(dict[str, object], entry) for entry in entries if isinstance(entry, dict)
                       and isinstance(value := cast(dict[str, object], entry).get("staff_id"), str)}
        except (OSError, ValueError) as error:
            report_anomaly(f"{run}: {error}")
        for identity in records.keys() | marked.get(run, set[staff.StaffId]()):
            evidence = found.setdefault(identity, WorkerRunEvidence([], []))
            evidence.runs.append(run)
            if identity in records:
                evidence.entries.append((run, records[identity]))
    return found


def worker_run_entries(record: staff.StaffSnapshot, rows: Iterable[staff.StaffRow]) -> dict[staff.StaffId, tuple[tuple[Path, dict[str, object]], ...]]:
    return {identity: tuple(evidence.entries) for identity, evidence in _worker_runs(record, rows).items() if evidence.entries}


def worker_run_index(record: staff.StaffSnapshot) -> dict[staff.StaffId, tuple[Path, ...]]:
    return {identity: tuple(evidence.runs) for identity, evidence in _worker_runs(record, record.find(kind="worker")).items()}


def pane_parents(record: staff.StaffSnapshot) -> list[staff.StaffRow]:
    """Select local parents whose workers use panes."""
    parents = {row.parent.id for row in record.find(kind="worker") if isinstance(row.parent, staff.ReportsTo)}
    found: list[staff.StaffRow] = []
    for row in record.rows:
        if row.id not in parents:
            continue
        location = row.doc if row.kind == "showrunner" else row.worktree
        if (isinstance(location, (staff.LiveDoc, staff.WrappedDoc, staff.PresentWorktree, staff.RemovedWorktree))
                and location.machine == remote.local_machine()):
            found.append(row)
    return found


def worker_name(entry: Mapping[str, object], record: staff.StaffSnapshot | None = None) -> str:
    member = worker_row(entry, record)
    return member.name if member is not None else ""


def worker_home(entry: Mapping[str, object], record: staff.StaffSnapshot | None = None) -> str:
    record = record if record is not None else staff.local_snapshot()
    backend = record.backends.get(staff.StaffId(str(entry.get("staff_id", ""))))
    return str(backend.codex_home) if isinstance(backend, staff.CodexBackend) else ""


def stored_worker_records(entries: Mapping[str, object]) -> dict[str, object]:
    """Store fixed references and run details without copying staff facts."""
    return {name: {key: value for key, value in cast(dict[str, object], raw).items()
                   if key not in WORKER_FACT_KEYS}
            for name, raw in entries.items() if isinstance(raw, dict)}


def run_workers(state: Mapping[str, object], record: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """Read the selected run's workers from the same staff snapshot as its owner."""
    from scripts.lib import unit_runs

    owner = unit_runs.run_owner(state, record)
    if isinstance(owner, unit_runs.RunOwnerUnrecorded):
        return ()
    return tuple(record.find(kind="worker", parent=owner.id))


def worker_directory(record: staff.StaffSnapshot, reference: staff.StaffId) -> Path:
    row = record.by_id(reference)
    if row is not None:
        owner = record.parent_of(row) if row.kind == "worker" else row
        if owner is not None:
            if owner.kind == "showrunner" and isinstance(owner.doc, staff.LiveDoc):
                from scripts.production.production_doc import read_production
                return read_production(owner).checkout
            tree = RowResolver(record).unit_worktree(row)
            if isinstance(tree, PresentWorktree):
                return tree.path
            if row.kind == "worker" and isinstance(tree, RemovedWorktree):
                return Path.cwd()
    report_anomaly(f"{reference}: staff worktree is unavailable")
    return Path.cwd()


@dataclass(frozen=True)
class Branch:
    name: str


@dataclass(frozen=True)
class BranchUnreadable:
    reason: str


@dataclass(frozen=True)
class ProductionDoc:
    machine: str
    path: Path
    owner: staff.StaffRow
    text: str


@dataclass(frozen=True)
class DocumentUnavailable:
    reason: str


@dataclass(frozen=True)
class LocationUnfilled:
    reason: str


@dataclass(frozen=True)
class RetiredFactsUnavailable:
    reason: str


@dataclass(frozen=True)
class NoUnitConfig:
    """A showrunner or its worker has no unit configuration."""


@dataclass(frozen=True)
class ActiveUnit:
    worktree: PresentWorktree


@dataclass(frozen=True)
class RetiredUnit:
    worktree: RemovedWorktree


def _read_facts(requests: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Read files and git branches on the machine that holds them."""
    facts: dict[str, tuple[str, str]] = {}
    for kind, path in requests:
        key = kind + ":" + path
        try:
            if kind == "git":
                result = subprocess.run(
                    ["git", "--no-optional-locks", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode or not result.stdout.strip():
                    raise OSError(result.stderr.strip() or "git returned no branch")
                facts[key] = ("value", result.stdout.strip())
            else:
                facts[key] = ("value", Path(path).expanduser().read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            facts[key] = ("missing", str(error))
        except (OSError, UnicodeError) as error:
            facts[key] = ("unavailable", str(error))
    return facts


class RowResolver:
    """Resolve row facts from one snapshot without changing files."""

    def __init__(self, record: staff.StaffSnapshot, *,
                 dependencies: Callable[[Path], None] | None = None,
                 rows: Iterable[staff.StaffRow] | None = None) -> None:
        self.record: staff.StaffSnapshot = record
        self.dependencies: Callable[[Path], None] | None = dependencies
        self._remote_facts: dict[str, tuple[str, str]] = {}
        self._remote_read: bool = False
        self._rows: tuple[staff.StaffRow, ...] = tuple(record.rows if rows is None else rows)
        self._local_facts: dict[tuple[str, str], tuple[str, str]] = {}

    def _holder(self, row: staff.StaffRow) -> staff.StaffRow:
        if row.kind != "worker":
            return row
        parent = self.record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
        if parent is None:
            raise staff.StaffUnreadable(f"{row.name}: staff parent is unavailable")
        return parent

    def unit_worktree(self, row: staff.StaffRow) -> PresentWorktree | RemovedWorktree | WorktreeUnfilled:
        return self._holder(row).worktree

    def _requests(self, machine: str) -> list[tuple[str, str]]:
        requests: set[tuple[str, str]] = set()
        for row in self._rows:
            tree = self._holder(row).worktree
            if isinstance(tree, PresentWorktree) and tree.machine == machine:
                requests.update((("git", str(tree.path)), ("file", str(tree.path / ".unit.toml"))))
            elif isinstance(tree, RemovedWorktree) and tree.machine == machine:
                requests.add(("file", str(configuration.parked_path(self._holder(row).id, home=Path("~")))))
            owner = self._holder(row)
            if owner.kind == "unit" and isinstance(owner.parent, staff.ReportsTo):
                owner = self.record.by_id(owner.parent.id) or owner
            if isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) and owner.doc.machine == machine:
                requests.update((("file", str(owner.doc.path)), ("git", str(owner.doc.path.parent))))
        return sorted(requests)

    def _fact(self, machine: str, kind: str, path: Path) -> tuple[str, str]:
        if self.dependencies is not None:
            self.dependencies(path)
        if machine == remote.local_machine():
            key = (kind, str(path))
            if key not in self._local_facts:
                self._local_facts[key] = _read_facts([key])[kind + ":" + str(path)]
            return self._local_facts[key]
        if machine != remote.other_machine():
            return "unavailable", f"machine {machine} has no configured connection"
        if not self._remote_read:
            self._remote_read = True
            requests = self._requests(machine)
            code = (
                "import json,sys; "
                "from scripts.production.session_staff import _read_facts; "
                "requests=json.load(sys.stdin); "
                "print(json.dumps(_read_facts(requests)))"
            )
            try:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", machine,
                     "~/.claude/scripts/lib/py -B -c " + shlex.quote(code)],
                    input=json.dumps(requests), capture_output=True, text=True, check=False,
                )
                if result.returncode:
                    raise OSError(result.stderr.strip() or f"ssh exited {result.returncode}")
                value = cast(object, json.loads(result.stdout))
                if not isinstance(value, dict):
                    raise ValueError("remote facts are not an object")
                for key, item in cast(dict[str, object], value).items():
                    if (not isinstance(item, list) or len(cast(list[object], item)) != 2
                            or not all(isinstance(part, str) for part in cast(list[object], item))):
                        raise ValueError("remote fact is invalid")
                    parts = cast(list[str], item)
                    self._remote_facts[key] = (parts[0], parts[1])
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self._remote_facts = {kind + ":" + name: ("unavailable", f"{machine}: {error}")
                                      for kind, name in requests}
        return self._remote_facts.get(kind + ":" + str(path), ("unavailable", f"{machine}: fact was not returned"))

    def unit_config(self, row: staff.StaffRow) -> UnitConfig | ConfigMissing | NoUnitConfig | LocationUnfilled | RetiredFactsUnavailable:
        holder = self._holder(row)
        if holder.kind != "unit":
            return NoUnitConfig()
        tree = holder.worktree
        if isinstance(tree, WorktreeUnfilled):
            return LocationUnfilled("the worktree is not recorded")
        retired = isinstance(tree, RemovedWorktree)
        if retired:
            home = None if tree.machine == remote.local_machine() else Path("~")
            path = configuration.parked_path(holder.id, home=home)
        else:
            path = tree.path / ".unit.toml"
        try:
            if tree.machine == remote.local_machine():
                if self.dependencies is not None:
                    self.dependencies(path)
                config = (configuration.read_parked(holder.id, cleanup=False) if retired
                          else configuration.read(tree.path, cleanup=False))
            else:
                state, text = self._fact(tree.machine, "file", path)
                if state == "missing":
                    config = ConfigMissing(f"{path}: configuration is missing")
                elif state != "value":
                    raise configuration.ConfigRefused(text)
                else:
                    config = configuration.decode(tomllib.loads(text))
        except (ValueError, OSError, UnicodeError) as error:
            if retired:
                return RetiredFactsUnavailable(f"{path}: {error}")
            raise configuration.ConfigRefused(f"{path}: {error}") from error
        if retired and (not isinstance(config, UnitConfig)
                        or not isinstance(config.retired, configuration.RetiredSession)):
            return RetiredFactsUnavailable(config.reason if isinstance(config, ConfigMissing)
                                          else "parked retirement facts are missing")
        return config

    def unit_branch(self, row: staff.StaffRow) -> Branch | BranchUnreadable | LocationUnfilled | RetiredFactsUnavailable:
        holder = self._holder(row)
        tree = holder.worktree
        if isinstance(tree, WorktreeUnfilled):
            return LocationUnfilled("the worktree is not recorded")
        if isinstance(tree, RemovedWorktree):
            if isinstance(tree.branch, staff.ParkedBranch):
                return Branch(tree.branch.value)
            return RetiredFactsUnavailable("the parked branch is not recorded in staff")
        state, text = self._fact(tree.machine, "git", tree.path)
        return Branch(text) if state == "value" else BranchUnreadable(text)

    def production_doc(self, row: staff.StaffRow) -> ProductionDoc | DocumentUnavailable | LocationUnfilled:
        owner = self._holder(row)
        if owner.kind == "unit":
            parent = self.record.parent_of(owner)
            if parent is None or parent.kind != "showrunner":
                return DocumentUnavailable(f"{owner.name}: no showrunner staff parent")
            owner = parent
        doc = owner.doc
        if isinstance(doc, staff.DocUnfilled):
            return LocationUnfilled("the production document is not recorded")
        state, text = self._fact(doc.machine, "file", doc.path)
        return ProductionDoc(doc.machine, doc.path, owner, text) if state == "value" else DocumentUnavailable(text)

    def merge_branch(self, row: staff.StaffRow) -> Branch | BranchUnreadable:
        doc = self.production_doc(row)
        if not isinstance(doc, ProductionDoc):
            return BranchUnreadable(doc.reason)
        state, text = self._fact(doc.machine, "git", doc.path.parent)
        return Branch(text) if state == "value" else BranchUnreadable(text)

    def unit_state(self, row: staff.StaffRow) -> ActiveUnit | RetiredUnit | LocationUnfilled:
        tree = self.unit_worktree(row)
        if isinstance(tree, WorktreeUnfilled):
            return LocationUnfilled("the worktree is not recorded")
        return RetiredUnit(tree) if isinstance(tree, RemovedWorktree) else ActiveUnit(tree)

    def member_by_folder(self, machine: str, path: Path) -> MemberFound | NoSuchMember:
        row = self.record.by_worktree(machine, path)
        return MemberFound(row) if row is not None else NoSuchMember(FolderMember(machine, path))


@dataclass(frozen=True)
class ShowrunnerMember:
    name: str


@dataclass(frozen=True)
class UnitMember:
    name: str
    unit: str


@dataclass(frozen=True)
class StoredReference:
    id: str


@dataclass(frozen=True)
class LiveSession:
    id: str


@dataclass(frozen=True)
class NoSession:
    pass


@dataclass(frozen=True)
class ByName:
    pass


@dataclass(frozen=True)
class UnitExclusion:
    """Match an exclusion within the supplied production members."""

    value: str


@dataclass(frozen=True)
class RequiredParent:
    row: staff.StaffRow


@dataclass(frozen=True)
class AnyParent:
    pass


@dataclass(frozen=True)
class FolderMember:
    machine: str
    path: Path


NO_SESSION = NoSession()
BY_NAME = ByName()
ANY_PARENT = AnyParent()


@dataclass(frozen=True)
class ObservedUnit:
    """A native runtime identity observed in a pane."""

    unit: str
    staff_id: LiveSession | NoSession = NO_SESSION


MemberSelector = ShowrunnerMember | UnitMember | ObservedUnit | FolderMember


@dataclass(frozen=True)
class MemberFound:
    row: staff.StaffRow


@dataclass(frozen=True)
class NoSuchMember:
    selector: MemberSelector


def unit_match(selector: UnitMember | ObservedUnit, record: staff.StaffSnapshot,
                reference: StoredReference | LiveSession | ByName | UnitExclusion = BY_NAME, *,
                parent: RequiredParent | AnyParent = ANY_PARENT) -> MemberFound | NoSuchMember:
    """One id-first join; production requests also require their current parent."""
    if isinstance(reference, StoredReference):
        row = record.by_member_reference(reference.id)
        matches = [row] if row is not None and row.kind == "unit" else []
    elif isinstance(reference, LiveSession):
        row = record.by_current_session(reference.id)
        matches = [row] if row is not None and row.kind == "unit" else []
    elif isinstance(reference, UnitExclusion):
        units = record.find(kind="unit")
        matches = [row for row in units if str(row.id) == reference.value]
        if not matches:
            matches = [row for row in units if row.name == reference.value]
        if not matches:
            unit_owner = cast(Callable[[str], str], import_module("scripts.copies.copies").unit_owner)
            matches = [row for row in units if unit_owner(row.name) == reference.value]
    else:
        matches = [row for row in record.find(kind="unit") if row.name == unit_names.name(selector.unit)]
    if isinstance(selector, UnitMember):
        matches = [row for row in matches if isinstance(parent, RequiredParent) and row.parent == staff.ReportsTo(parent.row.id)]
    if len(matches) > 1:
        raise staff.StaffUnreadable(f"{selector.unit}: no unique staff entry")
    return MemberFound(matches[0]) if matches else NoSuchMember(selector)


def excluded_unit_ids(members: tuple[staff.StaffRow, ...], values: Iterable[str], production: str) -> set[staff.StaffId]:
    """Resolve exclusions within the supplied production members."""
    record = staff.StaffSnapshot(members)
    excluded: set[staff.StaffId] = set()
    for value in values:
        match = unit_match(ObservedUnit(value), record, UnitExclusion(value))
        if not isinstance(match, MemberFound):
            raise staff.StaffRefused(f"{value}: no unit of production {production}")
        excluded.add(match.row.id)
    return excluded


@dataclass(frozen=True)
class MemberSources:
    launches: tuple[PaneLaunch, ...]

    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        if isinstance(selector, FolderMember):
            return RowResolver(record).member_by_folder(selector.machine, selector.path)
        if isinstance(selector, ObservedUnit):
            if isinstance(selector.staff_id, NoSession):
                raise staff.StaffUnreadable("observed session has no native runtime identity")
            return unit_match(selector, record, selector.staff_id)
        if isinstance(selector, UnitMember):
            parent = self.member(ShowrunnerMember(selector.name), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            return unit_match(selector, record, parent=RequiredParent(parent.row))
        row = next((row for row in record.find(kind="showrunner") if row.name == selector.name), None)
        return MemberFound(row) if row is not None else NoSuchMember(selector)


def member_sources(*, dependencies: Callable[[Path], None] | None = None) -> MemberSources:
    from scripts.herdr import herdr
    from scripts.agents import pane_record

    session = herdr.shared_session(dependencies=dependencies)
    try:
        launches = pane_record.read_strict(session, dependencies=dependencies)
    except OSError as error:
        raise staff.StaffUnreadable(f"{pane_record.record_path(session)}: {error}") from error
    except ValueError as error:
        raise staff.StaffUnreadable(str(error)) from error
    return MemberSources(tuple(launches.values()))


class LiveClaude(NamedTuple):
    name: str
    session_id: str
    socket: str
    pid: int


class ClaudeNotRunning(NamedTuple):
    pass


class ClaudeUnknown(NamedTuple):
    """The session records could not all be read, which says nothing about whether Claude runs."""

    reason: str


Claude = LiveClaude | ClaudeNotRunning | ClaudeUnknown


Place = HerdrPlace


@dataclass(frozen=True)
class ObservedProcess:
    pid: int
    command: str


@dataclass(frozen=True)
class ProcessUnobserved:
    """No process observation is available for this pane."""


@dataclass(frozen=True)
class LabeledSession:
    unit: str
    claude: Claude
    place: Place
    process: ObservedProcess | ProcessUnobserved = ProcessUnobserved()
    identity: LiveSession | NoSession = NO_SESSION


def place_text(place: Place) -> str:
    return f"herdr:{place.session}:{place.pane}"


def process_pid(unit: LabeledSession) -> int:
    """Return the observed PID, or zero when no process was observed."""
    if isinstance(unit.process, ObservedProcess):
        return unit.process.pid
    return unit.claude.pid if isinstance(unit.claude, LiveClaude) else 0


def holder_text(unit: LabeledSession) -> str:
    """Identify the observed process, including uncertain holders."""
    if isinstance(unit.process, ObservedProcess):
        return f"pid {unit.process.pid} ({unit.process.command}) in {place_text(unit.place)}"
    if isinstance(unit.claude, LiveClaude):
        return f"pid {unit.claude.pid} (claude) in {place_text(unit.place)}"
    return f"unobserved process in {place_text(unit.place)}"


@dataclass(frozen=True)
class LabeledProductions:
    # Session labels grouped by production and unit.
    units: dict[str, dict[str, LabeledSession]]
    members: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)

    def status_units(self, production_name: str) -> dict[str, LabeledSession]:
        """Return the selected process for each unit in the production."""
        return dict(self.units.get(production_name, {}))



def production_members(record: staff.StaffSnapshot) -> dict[str, tuple[staff.StaffRow, ...]]:
    """All recorded unit children, including those with no pane or local worktree."""
    units = tuple(staff.active(record, "unit"))
    return {row.name: tuple(unit for unit in units if unit.parent == staff.ReportsTo(row.id))
            for row in staff.active(record, "showrunner")}


def group_labeled_sessions(marked: Iterable[LabeledSession], *,
                       record: staff.StaffSnapshot | None = None,
                       sources: MemberSources | None = None) -> LabeledProductions:
    """Join native runtime identities to their recorded parents."""
    record = record if record is not None else staff.snapshot()
    sources = sources if sources is not None else member_sources()
    return _group_labeled_sessions(marked, record, production_members(record), sources.launches)


def retained_session_labels(marked: Iterable[LabeledSession], row: staff.StaffRow, production_name: str, *,
                        record: staff.StaffSnapshot, sources: MemberSources) -> LabeledProductions:
    """Keep pane evidence for a retained identity during stop and cleanup checks."""
    membership = production_members(record)
    if row not in membership.get(production_name, ()):
        membership[production_name] = (*membership.get(production_name, ()), row)
    return _group_labeled_sessions(marked, record, membership, sources.launches)


def _group_labeled_sessions(marked: Iterable[LabeledSession], record: staff.StaffSnapshot,
                        membership: dict[str, tuple[staff.StaffRow, ...]], launches: tuple[PaneLaunch, ...]) -> LabeledProductions:
    from scripts.herdr import herdr

    shared_session = herdr.shared_session()
    parents = {row.id: production_name for production_name, rows in membership.items() for row in rows}
    found: dict[str, dict[staff.StaffId, list[LabeledSession]]] = {}
    observed: list[LabeledSession] = []
    for unit in marked:
        identity = report_process(process_pid(unit), unit.identity.id if isinstance(unit.identity, LiveSession) else "")
        if identity.is_morty:
            continue
        if identity.native_id and not isinstance(unit.identity, LiveSession):
            unit = replace(unit, identity=LiveSession(identity.native_id))
        observed.append(unit)
    pane_rows: dict[Place, set[staff.StaffId]] = {}
    for unit in observed:
        row = record.by_current_session(unit.identity.id) if isinstance(unit.identity, LiveSession) else None
        if row is not None:
            pane_rows.setdefault(unit.place, set()).add(row.id)
    for unit in observed:
        runtime_row = record.by_current_session(unit.identity.id) if isinstance(unit.identity, LiveSession) else None
        if runtime_row is None:
            if isinstance(unit.identity, LiveSession):
                continue
            if (pid := process_pid(unit)) and pid not in process_table.snapshot():
                continue
            reason = "process identity is unavailable"
            candidates = set(pane_rows.get(unit.place, ()))
            for launch in launches:
                if unit.place.session == shared_session and launch["pane_id"] == unit.place.pane:
                    owner = record.by_member_reference(pane_launch.reference_id(launch))
                    if owner is not None:
                        candidates.add(owner.id)
            if candidates:
                report_anomaly(f"{holder_text(unit)}: cannot read its thread; staff status may be incomplete",
                               pid=process_pid(unit))
            if len(candidates) != 1:
                continue
            runtime_row = record.by_id(next(iter(candidates)))
            assert runtime_row is not None
            unit = replace(unit, claude=ClaudeUnknown(reason))
        if runtime_row.kind != "unit":
            continue
        row = runtime_row
        production_name = parents.get(row.id)
        if production_name is None:
            parent = record.parent_of(row)
            if (isinstance(row.worktree, staff.RemovedWorktree)
                    or (parent is not None and parent.kind == "showrunner"
                        and isinstance(parent.doc, staff.WrappedDoc))):
                continue
            report_anomaly(f"{row.name}: no showrunner staff parent",
                           pid=process_pid(unit))
            continue
        found.setdefault(production_name, {}).setdefault(row.id, []).append(unit)
    units: dict[str, dict[str, LabeledSession]] = {}
    for production_name, production in found.items():
        units[production_name] = {}
        for identity, places in production.items():
            row = record.by_id(identity)
            assert row is not None
            name = row.name
            selected = next((unit for unit in places if isinstance(unit.process, ObservedProcess)
                             and (herdr.is_claude_process([unit.process.command])
                                  or herdr.is_codex_process([unit.process.command]))), places[0])
            units[production_name][name] = selected if selected.unit == name else replace(selected, unit=name)
    return LabeledProductions(units, {production_name: tuple(row.name for row in rows)
                                     for production_name, rows in membership.items()})


def session_staff_and_directors(
    records: list[SessionRecord], environment: Mapping[str, str], *, record: staff.StaffSnapshot | None = None,
) -> tuple[LabeledProductions, dict[str, str]]:
    """Match known session PIDs to staff and directors, without enumerating processes."""
    labeled: list[LabeledSession] = []
    directors: dict[str, str] = {}
    known = {session["sessionId"] for session in records}
    proc = Path(environment.get("HERDR_PROC", "/proc"))
    record = record if record is not None else staff.snapshot()
    sources = member_sources()
    for session in records:
        try:
            # Herdr's Linux environment reader is private; keep this PID-only IO here.
            raw = (proc / str(session["pid"]) / "environ").read_bytes()
            values = dict(item.split("=", 1) for item in raw.decode(errors="replace").split("\0")
                          if "=" in item)
        except OSError as error:
            raise staff.StaffUnreadable(f"pid {session['pid']}: process environment is unavailable: {error}") from error
        owner = values.get("CLAUDE_CODE_SESSION_ID", "")
        if owner in known and owner != session["sessionId"]:
            directors[session["sessionId"]] = owner
            continue
        session_id = session["sessionId"]
        if session_id:
            labels = LabeledSession(
                "", LiveClaude(session["name"], session_id,
                                 session["messagingSocketPath"], session["pid"]),
                HerdrPlace(values.get("HERDR_SESSION", ""), values.get("HERDR_PANE_ID", "")),
                process=ObservedProcess(session["pid"], "claude"), identity=LiveSession(session_id),
            )
            labeled.append(labels)
    return group_labeled_sessions(labeled, record=record, sources=sources), directors


@dataclass(frozen=True)
class LaunchMembership:
    """The staff owner's identities and the production documents that name their parents."""

    record: staff.StaffSnapshot
    sources: MemberSources

    @classmethod
    def read(cls) -> LaunchMembership:
        record = staff.snapshot()
        return cls(record, member_sources())

    def environment(self, launch: PaneLaunch) -> dict[str, str]:
        if agent_kind(launch, record=self.record) == "codex":
            return legacy_identity.without_legacy_identity(launch["environment"])
        labels = StaffLabels(self.record)
        owner = self.record.by_member_reference(pane_launch.reference_id(launch))
        session_id = owner.current_session if owner is not None else pane_launch.reference_id(launch)
        return labels.environment(launch["environment"], session_id)


def record_membership(launch: PaneLaunch) -> PaneLaunch:
    """Keep pane settings from the conversation history without staff labels."""
    from scripts.agents import pane_record

    result = launch.copy()
    result["environment"] = pane_record.retained_environment(result, pane_record.launches())
    return without_staff_labels(result)


@dataclass(frozen=True)
class RepairedSessionLabels:
    identity: str
    assigned: tuple[tuple[str, str], ...]
    removed: tuple[str, ...]


def repair_marks() -> list[RepairedSessionLabels]:
    """Remove saved staff labels and recover missing pane observations."""
    from scripts.message import sessions
    from scripts.agents import pane_record

    record = staff.snapshot()
    history = pane_record.launches()
    newest = {pane_launch.reference_id(entry): entry["written_at"]
              for entry in sorted(history, key=lambda item: item["written_at"])}
    live = sessions.running_sessions()
    repaired: list[RepairedSessionLabels] = []
    if live:
        from scripts.herdr import herdr

        by_pid = {entry["pid"]: entry for entry in live}
        seen: set[str] = set()
        for process in herdr.herdr_processes():
            session = by_pid.get(process.pid)
            if session is None or session["sessionId"] in seen:
                continue
            seen.add(session["sessionId"])
            if pane_launch.matching_launch(pane_record.read(process.place.session), process.place, session["sessionId"]) is not None:
                continue
            try:
                entry = pane_record.running_launch(session, process.place, process.cwd)
            except (OSError, ValueError):
                continue
            if record.by_current_session(entry["session_id"]) is None:
                continue
            pane_record.write(process.place.session, entry)
            newest[pane_launch.reference_id(entry)] = entry["written_at"]
            repaired.append(RepairedSessionLabels(
                f"{process.place.session}:{process.place.pane}:{pane_launch.reference_id(entry)}",
                (), ()))
    for path in sorted(pane_record.record_directory().glob("*.json")):
        with pane_record.locked(path.stem):
            entries = pane_record.read_strict(path.stem)
            changed = False
            for pane, entry in entries.items():
                if entry["written_at"] != newest.get(pane_launch.reference_id(entry)):
                    continue
                restored = entry.copy()
                restored["environment"] = pane_record.retained_environment(entry, history)
                restored = without_staff_labels(restored)
                if restored != entry:
                    entries[pane] = restored
                    before, after = entry["environment"], restored["environment"]
                    repaired.append(RepairedSessionLabels(
                        f"{path.stem}:{pane}:{pane_launch.reference_id(entry)}",
                        tuple((key, value) for key, value in sorted(after.items()) if before.get(key) != value),
                        tuple(sorted(key for key in before if key not in after))))
                    changed = True
            if changed:
                pane_record.save(path.stem, entries)
    return repaired


def main(arguments: list[str] | None = None) -> int:
    """Repair saved session labels without starting or stopping a process."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    _ = parser.add_argument("command", choices=["repair"])
    _ = parser.parse_args(arguments)
    try:
        repaired = repair_marks()
    except (staff.StaffUnreadable, OSError, ValueError) as error:
        print(f"session labels: {error}", file=sys.stderr)
        return 1
    for repair in repaired:
        changes = [f"set {key}={value}" for key, value in repair.assigned]
        changes.extend(f"removed {key}" for key in repair.removed)
        print(f"{repair.identity}: " + "; ".join(changes))
    if not repaired:
        print("nothing to repair")
    return 0



def inbox_path(identity: staff.StaffId) -> Path:
    return staff.record_path().parent / "inbox" / f"{identity}.pending.json"


def worker_inbox_path(session_dir: str | Path, name: str) -> Path:
    """Select the fixed staff inbox or the unpublished admission inbox."""
    folder = Path(session_dir)
    find_member = cast(Callable[[Path, str], staff.StaffRow | None],
                       import_module("scripts.production.worker_admission").member)
    member = find_member(folder, name)
    return inbox_path(member.id) if member is not None else folder / f"{name}.pending.json"


if __name__ == "__main__":
    sys.exit(main())
