"""Matching a running session to its staff row."""
from __future__ import annotations

import sys
import fcntl
import json
import shlex
import subprocess
import tomllib
from importlib import import_module
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from scripts.agents.worker_lifecycle import WorkerStateFile

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in (SCRIPTS / "message", SCRIPTS / "production", SCRIPTS / "herdr", SCRIPTS.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from scripts.production import staff, showrunners
from scripts.production.session_labels import StaffLabels
from scripts.production import unit_config as configuration
from scripts.shutdown import remote
from scripts.production.showrunners import canonical_slug
from sessions import SessionRecord
from place import HerdrPlace
from pane_launch import PaneLaunch
import unit_names


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
    """Read records and marker associations once, with one anomaly for each failed read."""
    from scripts.lib import unit_runs
    rows = tuple(rows)
    owners = {owner.current_session: owner for row in rows if (owner := record.parent_of(row)) is not None}
    marked: dict[Path, set[staff.StaffId]] = {}
    for owner in owners.values():
        try:
            marker = unit_runs.marker(owner.current_session)
            if marker.is_file():
                marked.setdefault(Path(marker.read_text().strip()), set()).update(row.id for row in rows if row.parent == staff.ReportsTo(owner.id))
        except (OSError, ValueError) as error:
            print(f"anomaly: {owner.name}: {error}".replace("\n", " "), file=sys.stderr)
    paths = [*marked, *unit_runs.run_folders()]
    if current := unit_runs.environ("RUN_DIR"):
        paths.append(Path(current))
    found: dict[staff.StaffId, WorkerRunEvidence] = {}
    for run in sorted(dict.fromkeys(paths), key=lambda run: run.stat().st_mtime if run.exists() else 0):
        records: dict[staff.StaffId, dict[str, object]] = {}
        try:
            entries = [cast(object, json.loads(path.read_text())) for path in run.glob("impl_worker_*.json")]
            entries.extend(read_worker_roster(run / "mesh_roster.json").values())
            records = {staff.StaffId(value): cast(dict[str, object], entry) for entry in entries if isinstance(entry, dict)
                       and isinstance(value := cast(dict[str, object], entry).get("staff_id"), str)}
        except (OSError, ValueError) as error:
            print(f"anomaly: {run}: {error}".replace("\n", " "), file=sys.stderr)
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


def publish_worker_roster(handle: WorkerStateFile, roster: Mapping[str, object]) -> None:
    handle.publish(json.dumps(stored_worker_records(roster), indent=2, sort_keys=True))


def read_worker_roster(path: Path) -> dict[str, object]:
    from scripts.agents import worker_lifecycle
    with worker_lifecycle.locked_state(path, mode=fcntl.LOCK_SH) as handle:
        return handle.read_object()


def worker_directory(record: staff.StaffSnapshot, reference: staff.StaffId) -> Path:
    row = record.by_id(reference)
    if row is not None:
        owner = record.parent_of(row) if row.kind == "worker" else row
        if owner is not None and owner.kind == "showrunner" and isinstance(owner.doc, staff.LiveDoc):
            from scripts.production.production_doc import read_production
            return read_production(owner.doc.path).checkout
        tree = RowResolver(record).unit_worktree(row)
        if isinstance(tree, PresentWorktree):
            return tree.path
    print(f"anomaly: {reference}: staff worktree is unavailable", file=sys.stderr)
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
                "import json,sys; from pathlib import Path; "
                "sys.path.insert(0,str(Path.home()/'.claude')); "
                "from scripts.production.session_staff import _read_facts; "
                "requests=json.load(sys.stdin); "
                "print(json.dumps(_read_facts(requests)))"
            )
            try:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", machine,
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
            home = Path.home() if tree.machine == remote.local_machine() else Path("~")
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
            config = self.unit_config(holder)
            if isinstance(config, UnitConfig) and isinstance(config.retired, configuration.RetiredSession):
                return Branch(config.retired.branch.value)
            return RetiredFactsUnavailable(config.reason if isinstance(config, (ConfigMissing, RetiredFactsUnavailable))
                                          else "parked retirement facts are missing")
        state, text = self._fact(tree.machine, "git", tree.path)
        return Branch(text) if state == "value" else BranchUnreadable(text)

    def production_doc(self, row: staff.StaffRow) -> ProductionDoc | DocumentUnavailable | LocationUnfilled:
        owner = self._holder(row)
        if owner.kind == "unit":
            parent = self.record.by_id(owner.parent.id) if isinstance(owner.parent, staff.ReportsTo) else None
            if parent is None or parent.kind != "showrunner":
                return DocumentUnavailable(f"{owner.name}: no showrunner staff parent")
            owner = parent
        doc = owner.doc
        if isinstance(doc, staff.DocUnfilled):
            return LocationUnfilled("the production document is not recorded")
        state, text = self._fact(doc.machine, "file", doc.path)
        return ProductionDoc(doc.machine, doc.path) if state == "value" else DocumentUnavailable(text)

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
    slug: str


@dataclass(frozen=True)
class UnitMember:
    slug: str
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
                reference: StoredReference | LiveSession | ByName = BY_NAME, *,
                parent: RequiredParent | AnyParent = ANY_PARENT) -> MemberFound | NoSuchMember:
    """One id-first join; production requests also require their current parent."""
    if isinstance(reference, StoredReference):
        row = record.by_member_reference(reference.id)
        matches = [row] if row is not None and row.kind == "unit" else []
    elif isinstance(reference, LiveSession):
        row = record.by_current_session(reference.id)
        matches = [row] if row is not None and row.kind == "unit" else []
    else:
        matches = [row for row in record.find(kind="unit") if row.name == unit_names.name(selector.unit)]
    if isinstance(selector, UnitMember):
        matches = [row for row in matches if isinstance(parent, RequiredParent) and row.parent == staff.ReportsTo(parent.row.id)]
    if len(matches) > 1:
        raise staff.StaffUnreadable(f"{selector.unit}: no unique staff entry")
    return MemberFound(matches[0]) if matches else NoSuchMember(selector)


@dataclass(frozen=True)
class MemberSources:
    productions: dict[str, showrunners.ShowrunnerSource]
    launches: tuple[PaneLaunch, ...]

    def canonical_slug(self, slug: str) -> str:
        """Resolve a former slug from the documents already read for this snapshot."""
        if slug in self.productions:
            return slug
        matches = [name for name, source in self.productions.items()
                   if slug in showrunners.former_slugs(source.document)]
        return matches[0] if len(matches) == 1 else slug

    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        if isinstance(selector, FolderMember):
            return RowResolver(record).member_by_folder(selector.machine, selector.path)
        if isinstance(selector, ObservedUnit):
            if isinstance(selector.staff_id, NoSession):
                raise staff.StaffUnreadable("observed session has no native runtime identity")
            return unit_match(selector, record, selector.staff_id)
        source = self.productions.get(selector.slug)
        if isinstance(selector, UnitMember):
            parent = self.member(ShowrunnerMember(selector.slug), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            return unit_match(selector, record, parent=RequiredParent(parent.row))
        if source is None:
            row = record.by_path(selector.slug)
            return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)
        owner = record.by_doc(remote.local_machine(), source.timer.doc)
        if owner is not None:
            timer = showrunners.timer_for(owner, record, sources=self.productions.values())
            return (MemberFound(owner) if owner.kind == "showrunner" and isinstance(timer, showrunners.Timer)
                    else NoSuchMember(selector))
        return NoSuchMember(selector)

    def showrunner_members(self, record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
        found: dict[str, staff.StaffRow] = {}
        for slug in self.productions:
            selected = self.member(ShowrunnerMember(slug), record)
            if isinstance(selected, MemberFound):
                found[slug] = selected.row
        return found


def member_sources(*, dependencies: Callable[[Path], None] | None = None,
                   unreadable: showrunners.UnreadableTimerPolicy = showrunners.UnreadableTimerPolicy.SKIP) -> MemberSources:
    import herdr
    from scripts.shutdown import pane_record

    session = herdr.shared_session(dependencies=dependencies)
    try:
        launches = pane_record.read_strict(session, dependencies=dependencies)
    except OSError as error:
        raise staff.StaffUnreadable(f"{pane_record.record_path(session)}: {error}") from error
    except ValueError as error:
        raise staff.StaffUnreadable(str(error)) from error
    sources = showrunners.read_sources(unreadable=unreadable,
                                       dependencies=dependencies)
    return MemberSources({source.timer.slug: source for source in sources}, tuple(launches.values()))


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
    # Why a unit was left out, by production slug and then unit id: multiple processes claim its runtime identity.
    faults: dict[str, dict[str, str]]
    members: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)

    def status_units(self, slug: str) -> dict[str, LabeledSession]:
        """Include ambiguous units without selecting a process or pane."""
        result = dict(self.units.get(slug, {}))
        for name, reason in self.faults.get(slug, {}).items():
            result[name] = LabeledSession(name, ClaudeUnknown(reason), Place("", ""))
        return result

    def production_fault(self, slug: str) -> str:
        """Every left-out unit of the production, in one line; empty when none was left out."""
        return "; ".join(detail for _, detail in sorted(self.faults.get(slug, {}).items()))


def lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


def production_members(record: staff.StaffSnapshot, sources: MemberSources) -> dict[str, tuple[staff.StaffRow, ...]]:
    """All recorded unit children, including those with no pane or local worktree."""
    active_runners = {row.id for row in staff.active(record, "showrunner")}
    runners = {slug: row for slug, row in sources.showrunner_members(record).items() if row.id in active_runners}
    # A timer is not required for an offline production whose short name is its slug.
    mapped = {row.id for row in runners.values()}
    runners.update({row.name: row for row in staff.active(record, "showrunner")
                    if row.id not in mapped and row.name not in runners})
    return {slug: tuple(unit for unit in staff.active(record, "unit") if unit.parent == staff.ReportsTo(row.id))
            for slug, row in runners.items()}


def group_labeled_sessions(marked: Iterable[LabeledSession], *,
                       record: staff.StaffSnapshot | None = None,
                       sources: MemberSources | None = None) -> LabeledProductions:
    """Join native runtime identities to their recorded parents and refuse duplicate processes."""
    record = record if record is not None else staff.snapshot()
    sources = sources if sources is not None else member_sources()
    return _group_labeled_sessions(marked, record, production_members(record, sources), sources.launches)


def retained_session_labels(marked: Iterable[LabeledSession], row: staff.StaffRow, slug: str, *,
                        record: staff.StaffSnapshot, sources: MemberSources) -> LabeledProductions:
    """Keep pane evidence for a retained identity during stop and cleanup checks."""
    membership = production_members(record, sources)
    if row not in membership.get(slug, ()):
        membership[slug] = (*membership.get(slug, ()), row)
    return _group_labeled_sessions(marked, record, membership, sources.launches)


def _group_labeled_sessions(marked: Iterable[LabeledSession], record: staff.StaffSnapshot,
                        membership: dict[str, tuple[staff.StaffRow, ...]], launches: tuple[PaneLaunch, ...]) -> LabeledProductions:
    import herdr

    shared_session = herdr.shared_session()
    parents = {row.id: slug for slug, rows in membership.items() for row in rows}
    found: dict[str, dict[staff.StaffId, list[LabeledSession]]] = {}
    faults: dict[str, dict[str, str]] = {}
    observed = tuple(marked)
    pane_rows: dict[Place, set[staff.StaffId]] = {}
    for unit in observed:
        row = record.by_current_session(unit.identity.id) if isinstance(unit.identity, LiveSession) else None
        if row is not None:
            pane_rows.setdefault(unit.place, set()).add(row.id)
    for unit in observed:
        runtime_row = record.by_current_session(unit.identity.id) if isinstance(unit.identity, LiveSession) else None
        if runtime_row is None:
            reason = (f"{unit.identity.id}: no current staff row" if isinstance(unit.identity, LiveSession)
                      else "process identity is unavailable")
            print(f"anomaly: {holder_text(unit)}: {reason}", file=sys.stderr)
            candidates = set(pane_rows.get(unit.place, ()))
            for launch in launches:
                if unit.place.session == shared_session and launch["pane_id"] == unit.place.pane:
                    owner = record.by_current_session(launch["session_id"])
                    if owner is not None:
                        candidates.add(owner.id)
            if len(candidates) != 1:
                continue
            runtime_row = record.by_id(next(iter(candidates)))
            assert runtime_row is not None
            unit = replace(unit, claude=ClaudeUnknown(reason))
        if runtime_row.kind != "unit":
            continue
        row = runtime_row
        slug = parents.get(row.id)
        if slug is None:
            parent = record.parent_of(row)
            if (isinstance(row.worktree, staff.RemovedWorktree)
                    or (parent is not None and parent.kind == "showrunner"
                        and isinstance(parent.doc, staff.WrappedDoc))):
                continue
            print(f"anomaly: {row.name}: no showrunner staff parent", file=sys.stderr)
            continue
        found.setdefault(slug, {}).setdefault(row.id, []).append(unit)
    units: dict[str, dict[str, LabeledSession]] = {}
    for slug, production in found.items():
        units[slug] = {}
        for identity, places in production.items():
            row = record.by_id(identity)
            assert row is not None
            name = row.name
            if len(places) > 1:
                sessions = " and ".join(sorted(holder_text(place) for place in places))
                reason = f"{name}: multiple processes claim its runtime identity: {sessions}"
                faults.setdefault(slug, {})[name] = reason
                print(f"anomaly: {reason}", file=sys.stderr)
            else:
                selected = places[0]
                units[slug][name] = selected if selected.unit == name else replace(selected, unit=name)
    return LabeledProductions(units, faults, {slug: tuple(row.name for row in rows)
                                           for slug, rows in membership.items()})


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
        return cls(staff.snapshot(), member_sources())

    def environment(self, launch: PaneLaunch) -> dict[str, str]:
        labels = StaffLabels(self.record, self.sources.productions, self.sources.showrunner_members(self.record))
        owner = self.record.by_member_reference(launch["session_id"])
        session_id = owner.current_session if owner is not None else launch["session_id"]
        return labels.environment(launch["environment"], session_id)


def record_membership(launch: PaneLaunch) -> PaneLaunch:
    """Supply launch session labels from its conversation history and staff owner."""
    from scripts.shutdown import pane_record

    result = launch.copy()
    result["environment"] = pane_record.retained_environment(result, pane_record.launches())
    try:
        membership = LaunchMembership.read()
        result["environment"] = membership.environment(result)
    except (staff.StaffUnreadable, OSError, ValueError):
        pass  # An unavailable owner cannot erase already recorded identity.
    return result


@dataclass(frozen=True)
class RepairedSessionLabels:
    identity: str
    assigned: tuple[tuple[str, str], ...]
    removed: tuple[str, ...]


def repair_marks() -> list[RepairedSessionLabels]:
    """Repair retained identities in place; never launch or change a conversation."""
    import sessions
    from scripts.shutdown import pane_record

    membership = LaunchMembership.read()
    history = pane_record.launches()
    newest = {entry["session_id"]: entry["written_at"]
              for entry in sorted(history, key=lambda item: item["written_at"])}
    live = sessions.running_sessions()
    repaired: list[RepairedSessionLabels] = []
    if live:
        import herdr

        by_pid = {entry["pid"]: entry for entry in live}
        seen: set[str] = set()
        for process in herdr.herdr_processes():
            session = by_pid.get(process.pid)
            if session is None or session["sessionId"] in seen:
                continue
            seen.add(session["sessionId"])
            if pane_record.matching_launch(process.place, session["sessionId"]) is not None:
                continue
            try:
                entry = pane_record.running_launch(session, process.place, process.cwd)
            except (OSError, ValueError):
                continue
            entry["environment"] = membership.environment(entry)
            if membership.record.by_current_session(entry["session_id"]) is None:
                continue
            pane_record.write(process.place.session, entry)
            newest[entry["session_id"]] = entry["written_at"]
            repaired.append(RepairedSessionLabels(
                f"{process.place.session}:{process.place.pane}:{entry['session_id']}",
                tuple((key, value) for key, value in sorted(entry["environment"].items())
                      if key in herdr.NATIVE_ID_MARKS), ()))
    for path in sorted(pane_record.record_directory().glob("*.json")):
        with pane_record.locked(path.stem):
            entries = pane_record.read_strict(path.stem)
            changed = False
            for pane, entry in entries.items():
                if entry["written_at"] != newest.get(entry["session_id"]):
                    continue
                restored = entry.copy()
                restored["environment"] = pane_record.retained_environment(entry, history)
                restored["environment"] = membership.environment(restored)
                if restored != entry:
                    entries[pane] = restored
                    before, after = entry["environment"], restored["environment"]
                    repaired.append(RepairedSessionLabels(
                        f"{path.stem}:{pane}:{entry['session_id']}",
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
    from scripts.agents import worker_lifecycle
    folder = Path(session_dir)
    roster = worker_lifecycle.read_worker_state(folder / "mesh_roster.json")
    raw = roster.get(name)
    if isinstance(raw, dict):
        entry = cast(dict[str, object], raw)
        reference = entry.get("staff_id")
        if isinstance(reference, str) and reference:
            return inbox_path(staff.native_id(reference))
    find_member = cast(Callable[[Path, str], staff.StaffRow | None],
                       import_module("scripts.production.worker_admission").member)
    member = find_member(folder, name)
    return inbox_path(member.id) if member is not None else folder / f"{name}.pending.json"


if __name__ == "__main__":
    sys.exit(main())
