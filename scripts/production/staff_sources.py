#!/usr/bin/env python3
"""Match production members and import missing staff once; remove this importer on 2026-11-13."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stderr
import io
import json
import os
import shlex
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
import sys
from pathlib import Path
from typing import Literal, cast, override

# These source modules still use sibling imports; keep their existing module identity.

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production import showrunners  # noqa: E402
from scripts.production.production_doc import Production, Refusal, WRAPPED, production_field  # noqa: E402
# The public batch reader drops failures; share its decoder within this repair's
# two-file boundary instead of copying its timer parser.
from scripts.production.showrunners import (  # noqa: E402
    _fields as _timer_fields,  # pyright: ignore[reportPrivateUsage]
    _source as _source,  # pyright: ignore[reportPrivateUsage]
)
import unit_names  # noqa: E402
from scripts.production import staff, unit_config, session_staff  # noqa: E402
from scripts.direct import workers  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.shutdown import remote  # noqa: E402


# Matchers live in unit_lookup, so lower readers use the same join without an import cycle.
from unit_lookup import (  # noqa: E402
    ShowrunnerMember as ShowrunnerMember, UnitMember as UnitMember, ObservedUnit as ObservedUnit,
    MemberSelector as MemberSelector, MemberFound as MemberFound, NoSuchMember as NoSuchMember,
    UnitProduction as UnitProduction, MemberSources as _MemberSources,
    member_sources as _member_sources,
)
import unit_lookup  # noqa: E402


def member(selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
    """Resolve pane identity or current production membership through the shared join."""
    return _member_sources().member(selector, record)


def ensure_staff_showrunner(child: Production, caller: str = "", *, runtime_id: str = "") -> staff.StaffRow:
    record = staff.initial_snapshot()
    sources = _member_sources()
    found = sources.member(ShowrunnerMember(child.slug), record)
    location = staff.LiveDoc(remote.local_machine(), child.doc.resolve())
    if isinstance(found, MemberFound):
        if runtime_id and found.row.current_session != staff.native_id(runtime_id):
            raise staff.StaffRefused(f"{child.slug}: native session conflicts with retained staff identity")
        if found.row.doc == location:
            return found.row
        return staff.ensure("showrunner", name=found.row.name, parent=found.row.parent, doc=location)
    parent = sources.member(ShowrunnerMember(caller), record)
    parent_doc = showrunners.timer_docs().get(caller)
    if parent_doc is not None:
        existing = parent.row if isinstance(parent, MemberFound) else None
        parent_location = staff.LiveDoc(remote.local_machine(), parent_doc.resolve())
        if existing is None or existing.doc != parent_location:
            source = sources.productions.get(caller)
            if existing is None and source is None:
                raise staff.StaffRefused(f"{caller}: native showrunner identity is not proved by its timer")
            parent = MemberFound(staff.ensure(
                "showrunner", name=existing.name if existing else showrunners.herdr_space(caller, str(parent_doc)),
                parent=existing.parent if existing else staff.StaffRoot(), doc=parent_location,
                runtime_id=existing.current_session if existing else source.timer.target.removeprefix("session:") if source else ""))
    if isinstance(parent, NoSuchMember):
        if caller:
            raise staff.StaffRefused(f"{child.slug}: no staff parent or timer document for {caller}")
        return staff.ensure("showrunner", name=showrunners.herdr_space(child.slug, str(child.doc)), doc=location, runtime_id=runtime_id)
    # Admission records the live doc; attach_parent records the edge and its
    # routing notices together after the showrunner appears.
    return staff.ensure("showrunner", name=showrunners.herdr_space(child.slug, str(child.doc)), doc=location, runtime_id=runtime_id)


def showrunner_members(record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
    """Resolve local production slugs to rows with the same launch-first matching rule."""
    return _member_sources().showrunner_members(record)


def unit_parent(unit: str, session_id: str = "") -> UnitProduction:
    """Resolve the current staff edge; old launch marks supply identity only."""
    reference = session_staff.StoredReference(session_id) if session_id else session_staff.BY_NAME
    return unit_lookup.unit_parent(unit, reference, record=staff.snapshot(), sources=_member_sources())


def _showrunner_key(slug: str) -> str:
    return f"showrunner:{slug}"


def _seed_member(key: str, kind: Literal["showrunner", "unit"], name: str, found: MemberFound | NoSuchMember,
                 parent: staff.SeedParent | staff.KeepSeedParent | staff.RootSeedParent, *, runtime_id: str = "") -> staff.SeedMember:
    return staff.SeedMember(key, kind, name if isinstance(found, NoSuchMember) else found.row.name,
                           staff.NewSeedMember() if isinstance(found, NoSuchMember) else found.row.id, parent, runtime_id)


def _collect(record: staff.StaffSnapshot, sources: _MemberSources) -> staff.SeedInput:
    members: list[staff.SeedMember] = []
    for slug, source in sources.productions.items():
        runtime_id = source.timer.target.removeprefix("session:")
        owner = record.by_member_reference(runtime_id)
        found = (MemberFound(owner) if owner is not None and owner.kind == "showrunner"
                 else sources.member(ShowrunnerMember(slug), record))
        members.append(_seed_member(_showrunner_key(slug), "showrunner", showrunners.space_from_text(slug, source.document),
                                    found, staff.KeepSeedParent(), runtime_id=runtime_id))
    return staff.SeedInput(tuple(members), ())


def _print_plan(seed_input: staff.SeedInput) -> bool:
    changed = False
    for entry in seed_input.members:
        if isinstance(entry.match, staff.NewSeedMember):
            action, identity = "add", entry.key
            changed = True
        else:
            action, identity = "keep", entry.match
        print(f"{action} {identity} {entry.kind} {entry.name}")
    return changed


def seed(*, dry_run: bool = False) -> None:
    """Import missing local members without moving or deleting any recorded member."""
    with staff.operation():
        record = staff.initial_snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        sources = _member_sources(dependencies=dependencies)
        seed_input = _collect(record, sources)
        changed = _print_plan(seed_input)
        if dry_run:
            if not changed:
                print("unchanged", file=sys.stderr)
        else:
            _ = staff.apply_seed(seed_input)


# The source scan is shared by seed and the live handler. It never writes source records.
LiveRole = str


@dataclass(frozen=True)
class WorkerOwner:
    key: str
    match: staff.StaffRow


BackendUnavailable = staff.BackendUnavailable


@dataclass(frozen=True)
class ClaudeSession:
    session_id: str


@dataclass(frozen=True)
class CodexThread:
    thread_id: str


@dataclass(frozen=True)
class WorkerObservation:
    row: staff.StaffRow
    owner: WorkerOwner
    identity: ClaudeSession | CodexThread | BackendUnavailable

    @property
    def name(self) -> str:
        return self.row.name





def _observation_session(value: ClaudeSession | CodexThread) -> str:
    return value.session_id if isinstance(value, ClaudeSession) else value.thread_id


def _problem(path: Path | str, error: object) -> None:
    message = f"staff sources: {path}: {error}"
    print(message.replace("\n", " ").replace("\r", " "), file=sys.stderr)


def is_non_staff_note(message: str) -> bool:
    """Identify a diagnostic that excludes an agent from staff."""
    return message.endswith("; not staff")


def codex_thread_host(arguments: list[str]) -> bool:
    """An app-server hosts threads; its inherited staff mark is not a session."""
    index = 2 if arguments and Path(arguments[0]).name in ("node", "nodejs") else 1
    while index < len(arguments):
        option = arguments[index]
        if option in ("-c", "--config", "--enable", "--disable", "--remote",
                      "--remote-auth-token-env", "--local-provider", "-m", "--model",
                      "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval",
                      "-C", "--cd", "--add-dir", "-i", "--image"):
            index += 2
        elif option.startswith("-") and option != "--":
            index += 1
        else:
            return option == "app-server"
    return False



def pane_holds_staff_member(record: staff.StaffSnapshot, sources: _MemberSources, pane: str) -> bool:
    """Join the pane's retained identity or unit labels to the supplied staff snapshot."""
    for launch in sources.launches:
        if launch.get("pane_id") != pane or launch.get("ended"):
            continue
        if record.by_member_reference(launch["session_id"]) is not None:
            return True
        environment = launch["environment"]
        production = environment.get("SHOWRUNNER_UNIT", "")
        unit = environment.get("SHOWRUNNER_UNIT_ID", "")
        if production and unit:
            found = sources.member(UnitMember(production, unit), record)
            if isinstance(found, MemberFound):
                return True
    return False


def codex_non_staff_reason(record: staff.StaffSnapshot, sources: _MemberSources,
                           environment: Mapping[str, str]) -> str:
    """Exclude a process without a thread only when its pane has no staff owner."""
    if environment.get("CODEX_THREAD_ID", ""):
        return ""
    pane = environment.get("HERDR_PANE_ID", "")
    if not pane:
        return "not in a herdr pane; not staff"
    if not pane_holds_staff_member(record, sources, pane):
        return "pane holds no staff member; not staff"
    return ""


class _WorkerSources:
    """Read worker membership from staff and session activity from live processes."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 *,
                 dependencies: staff.RecordInputs | None = None) -> None:
        self.record: staff.StaffSnapshot = record
        self.dependencies: staff.RecordInputs = dependencies if dependencies is not None else staff.RecordInputs(set(record.inputs))
        self.inputs: set[str] = self.dependencies.paths
        self.identities: dict[staff.StaffId, dict[str, str]] = {}
        registry = sessions.read_registry(dependencies=self.dependencies)
        self.claude_registry_incomplete: bool = bool(registry.unreadable)
        self.codex_scan_incomplete: bool = False
        for path in registry.unreadable:
            if path.exists():
                _problem(path, "session registry is unreadable")
        self.running: dict[str, sessions.SessionRecord] = {
            session["sessionId"]: session for session in workers.running_director_sessions(registry)
        }
        self.native_running: dict[str, dict[str, str]] = {
            identity: {"claude": identity} for identity in self.running}
        from scripts.message import process_table
        import herdr
        try:
            for process in process_table.snapshot().values():
                try:
                    arguments = shlex.split(process.arguments)
                except ValueError as error:
                    if not herdr.is_codex_process(process.arguments.split()):
                        continue
                    self.codex_scan_incomplete = True
                    _problem(f"Codex process scan: pid {process.pid}", error)
                    continue
                if not herdr.is_codex_process(arguments) or codex_thread_host(arguments):
                    continue
                environment = herdr.process_environment(process.pid)
                if native := environment.get("CODEX_THREAD_ID", ""):
                    self.native_running[native] = {"codex": native}
                elif reason := codex_non_staff_reason(record, sources, environment):
                    _problem(f"Codex process {process.pid}", reason)
                else:
                    self.codex_scan_incomplete = True
                    _problem(f"Codex process {process.pid}", "native thread identity is unavailable")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.codex_scan_incomplete = True
            _problem("Codex processes", error)
        for row in record.rows:
            identity = self.native_running.get(row.current_session)
            if identity is not None:
                self.identities[row.id] = identity

    def collect(self, *, current_only: bool = False) -> tuple[WorkerObservation, ...]:
        observations: list[WorkerObservation] = []
        for row in self.record.find(kind="worker"):
            parent = self.record.parent_of(row)
            if parent is None:
                _problem(row.id, "worker parent is unavailable")
                continue
            if current_only and row.current_session not in self.native_running:
                continue
            backend = self.record.backends.get(row.id)
            identity = (ClaudeSession(row.current_session) if isinstance(backend, staff.ClaudeBackend) else
                        CodexThread(row.current_session) if isinstance(backend, staff.CodexBackend) else BackendUnavailable())
            observations.append(WorkerObservation(row, WorkerOwner(f"staff:{parent.id}", parent), identity))
        return tuple(observations)


@dataclass(frozen=True)
class WorkerEvidence:
    """Raw ownership observations, including workers not yet represented in staff."""

    workers: tuple[WorkerObservation, ...]
    identities: dict[staff.StaffId, dict[str, str]]
    diagnostics: tuple[str, ...]


def observe_workers(record: staff.StaffSnapshot, sources: _MemberSources, *,
                    current_only: bool = False) -> WorkerEvidence:
    """Expose the shared scanner without dropping missing rows or acquiring a lock."""
    errors = io.StringIO()
    with redirect_stderr(errors):
        scan = _WorkerSources(record, sources)
        observed = scan.collect(current_only=current_only)
    if scan.claude_registry_incomplete:
        _ = errors.write("Claude session registry is incomplete\n")
    diagnostics = tuple(errors.getvalue().splitlines())
    return WorkerEvidence(observed, scan.identities, diagnostics)


def _live_member(row: staff.StaffRow, role: LiveRole, identity: Mapping[str, str]) -> dict[str, object]:
    """Keep the schema-3 member fields independent of stored location fields."""
    return {"id": row.id, "kind": row.kind, "name": row.name, "parent": row.parent.id if isinstance(row.parent, staff.ReportsTo) else None,
            "current_session": row.current_session, "role": role, "identity": dict(identity)}


def live() -> dict[str, object]:
    """Read one owner snapshot and project identities without changing any source."""
    record = staff.snapshot()
    dependencies = staff.RecordInputs(set(record.inputs))
    sources = _member_sources(dependencies=dependencies)
    scan = _WorkerSources(record, sources, dependencies=dependencies)
    observed = scan.collect()
    members: dict[staff.StaffId, dict[str, object]] = {}
    unregistered = sorted(identity for identity in scan.native_running if record.by_current_session(identity) is None)
    for identity in unregistered:
        _problem(identity, "current native session has no staff row; identity publication is incomplete")
    incomplete = bool(unregistered) or scan.claude_registry_incomplete or scan.codex_scan_incomplete
    active_ids = {row.id for kind in ("unit", "showrunner", "agent") for row in staff.active(record, kind)}
    resolver = session_staff.RowResolver(record, dependencies=dependencies)
    for row in record.rows:
        identity = scan.identities.get(row.id) if row.id in active_ids else None
        if row.kind != "worker" and identity is not None and row.current_session in identity.values():
            problems: list[str] = []
            document = resolver.production_doc(row)
            if row.kind != "agent" and not isinstance(document, session_staff.ProductionDoc):
                problems.append(document.reason)
            if row.kind == "unit":
                branch = resolver.unit_branch(row)
                if not isinstance(branch, session_staff.Branch):
                    problems.append(branch.reason)
                try:
                    config = resolver.unit_config(row)
                except unit_config.ConfigRefused as error:
                    problems.append(str(error))
                else:
                    if not isinstance(config, unit_config.UnitConfig):
                        problems.append(config.reason if not isinstance(config, session_staff.NoUnitConfig)
                                        else "no unit configuration")
            if problems:
                incomplete = True
                _problem(row.name, "; ".join(dict.fromkeys(problems)))
            if row.kind == "showrunner" and isinstance(document, session_staff.DocumentUnavailable):
                continue
            member = _live_member(row, row.kind, identity)
            parent = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
            if row.kind == "showrunner" and parent is not None and isinstance(parent.doc, staff.WrappedDoc):
                member["parent"] = None
            members[row.id] = member
    for entry in observed:
        row = entry.row
        if entry.owner.match.id not in active_ids:
            continue
        worker_identity = ({"claude": entry.identity.session_id} if isinstance(entry.identity, ClaudeSession)
                           else {"codex": entry.identity.thread_id} if isinstance(entry.identity, CodexThread) else {})
        if isinstance(entry.identity, BackendUnavailable):
            incomplete = True
            _problem(row.name, "worker backend is unavailable")
        member = _live_member(row, row.name, worker_identity)
        members[row.id] = member
    # Every emitted edge ends inside this document, even if source evidence is incomplete.
    while missing := [key for key, member in members.items()
                      if member["parent"] is not None and member["parent"] not in members]:
        for key in missing:
            incomplete = True
            member = members[key]
            parent_id = cast(staff.StaffId, member["parent"])
            parent = record.by_id(parent_id)
            if parent is None:
                _problem(key, "parent staff row is unavailable")
                del members[key]
                continue
            _problem(parent.id, "parent has no current session evidence")
            members[parent.id] = _live_member(parent, "unit" if parent.kind == "unit" else "showrunner", {})
    return {"schema": 3, "inputs": sorted(scan.inputs), "members": [members[key] for key in sorted(members)],
            "complete": not incomplete, "unregistered": sorted(set(unregistered))}


def cli_live() -> int:
    """Keep source failures on the same staff CLI exception boundary when run as a script."""
    def run() -> int:
        result = live()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["complete"] else 1

    return staff.cli_result(run)


@dataclass(frozen=True)
class FillDocument:
    path: Path
    text: str
    source: str

    @property
    def slug(self) -> str:
        return showrunners.production_slug(str(self.path))


def _fill_git(folder: Path, *arguments: str) -> str:
    done = subprocess.run(["git", "-C", str(folder), *arguments], text=True,
                          capture_output=True, check=False)
    if done.returncode:
        raise ValueError(done.stderr.strip() or f"git {' '.join(arguments)} failed in {folder}")
    return done.stdout.strip()


def source_allowed(path: Path) -> bool:
    """Scratch checkouts never provide production ownership evidence."""
    return not path.expanduser().resolve().is_relative_to(showrunners.SCRATCH_ROOT.expanduser().resolve())


class _FillSources:
    """The fill's manifest and document evidence, collected before any write."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 dependencies: staff.RecordInputs) -> None:
        self.record: staff.StaffSnapshot = record
        self.sources: _MemberSources = sources
        self.dependencies: staff.RecordInputs = dependencies
        self.manifest: set[str] = set()
        self.problems: list[str] = []
        self.excluded_documents: set[Path] = set()

    def problem(self, source: str | Path, unit: str, reason: object) -> None:
        self.problems.append(f"{source} {unit}: {str(reason).replace(chr(10), ' ')}")

    def source_allowed(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        if source_allowed(resolved):
            return True
        if resolved not in self.excluded_documents:
            self.excluded_documents.add(resolved)
            for row in self.record.find(kind="showrunner"):
                location = row.doc
                if (isinstance(location, (staff.LiveDoc, staff.WrappedDoc))
                        and location.machine == _fill_machine() and location.path.resolve() == resolved):
                    self.problem(row.id, row.name, f"production document is under scratch root: {path}")
        return False



def _fill_machine() -> str:
    # Use the same machine names as shutdown records, without a second host rule.
    return remote.local_machine()


def _fill_worktree(row: staff.StaffRow) -> staff.PresentWorktree:
    if isinstance(row.worktree, staff.PresentWorktree):
        return row.worktree
    raise ValueError(f"{row.name}: the staff worktree is not present")



def _fill_doc_location(document: FillDocument, machine: str) -> staff.LiveDoc:
    lines = document.text.splitlines()
    checkout = Path(production_field(lines, "Showrunner checkout")).expanduser()
    if not checkout.is_absolute():
        raise ValueError("Showrunner checkout must be absolute")
    root = Path(_fill_git(document.path.parent, "rev-parse", "--show-toplevel"))
    checkout_root = Path(_fill_git(checkout, "rev-parse", "--show-toplevel"))
    if checkout_root.resolve() != checkout.resolve():
        raise ValueError("Showrunner checkout is not a git worktree root")
    path = checkout / document.path.relative_to(root)
    branch = production_field(lines, "Merge branch")
    actual = _fill_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if actual != branch:
        raise ValueError(f"showrunner checkout is on branch {actual}, expected {branch}")
    if not path.is_file():
        raise ValueError(f"live production doc is missing: {path}")
    return staff.LiveDoc(machine, path)


class BoundMigrationSources(_MemberSources):
    """Join legacy owners through their stored documents before their native ids exist."""

    @override
    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        if isinstance(selector, UnitMember):
            parent = self.member(ShowrunnerMember(selector.slug), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            matches = [row for row in record.find(kind="unit", parent=parent.row.id)
                       if row.name == unit_names.name(selector.unit)]
            if len(matches) > 1:
                raise staff.StaffUnreadable(f"{selector}: conflicting retained unit rows")
            return MemberFound(matches[0]) if matches else NoSuchMember(selector)
        if not isinstance(selector, ShowrunnerMember):
            return super().member(selector, record)
        source = self.productions.get(selector.slug)
        if source is None:
            return NoSuchMember(selector)
        matches = [row for row in record.find(kind="showrunner")
                   if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
                   and row.doc.machine == _fill_machine()
                   and row.doc.path.resolve() == source.timer.doc.resolve()]
        if len(matches) > 1:
            raise staff.StaffUnreadable(f"{source.timer.doc}: no unique stored showrunner owner")
        return MemberFound(matches[0]) if matches else NoSuchMember(selector)


def migration_peer_identities(record: staff.StaffSnapshot) -> tuple[dict[staff.StaffId, set[str]], tuple[str, ...]]:
    """Read the peer roster once and join runtime evidence to stored member locations."""
    from scripts.message import roster

    local = remote.local_machine()
    remote_rows = [row for row in record.rows
                   if ((isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.machine != local)
                       or (isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree))
                           and row.worktree.machine != local))]
    if not remote_rows:
        return {}, ()
    peer = roster.peer_snapshot(os.environ)
    if isinstance(peer, roster.PeerNotReached):
        return {}, (f"{peer.host}: native identity evidence uncertain: {peer.reason}",)
    problems = list(peer.problems)
    identities: dict[staff.StaffId, set[str]] = {}
    entries: dict[staff.StaffId, list[roster.RosterEntry]] = {}
    for row in remote_rows:
        candidates: list[roster.RosterEntry] = []
        if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            location = row.doc
            if not source_allowed(location.path):
                problems.append(f"{row.id} {row.name}: scratch production source refused: {location.path}")
                continue
            try:
                result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                         location.machine, "cat -- " + shlex.quote(str(location.path))],
                                        capture_output=True, text=True, check=True, timeout=20,
                                        stdin=subprocess.DEVNULL)
                slug = FillDocument(location.path, result.stdout, f"{location.machine}:{location.path}").slug
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                problems.append(f"{row.id} {row.name}: stored peer document unavailable: {error}")
                continue
            candidates = [entry for entry in peer.entries
                          if entry.machine == location.machine and entry.role == "showrunner"
                          and isinstance(entry.showrunner, roster.RunsProduction)
                          and entry.showrunner.production == slug]
        elif row.kind == "unit" and isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
            location = row.worktree
            candidates = [entry for entry in peer.entries
                          if entry.machine == location.machine and entry.role == "unit director"
                          and ((isinstance(entry.session, roster.ClaudeSession)
                                and entry.session.session_id == row.current_session)
                               or (isinstance(entry.thread, roster.CodexThread)
                                   and entry.thread.thread_id == row.current_session))]
        if candidates:
            entries[row.id] = candidates
    # A worker is proved only by the address of its proved parent, never a global name.
    for row in record.find(kind="worker"):
        parents = entries.get(row.parent.id, []) if isinstance(row.parent, staff.ReportsTo) else []
        addresses = {"session:" + parent.session.session_id for parent in parents
                     if isinstance(parent.session, roster.ClaudeSession)}
        addresses.update("codex:" + parent.thread.thread_id for parent in parents
                         if isinstance(parent.thread, roster.CodexThread))
        if addresses:
            entries[row.id] = [entry for entry in peer.entries if entry.name == row.name
                               and ((isinstance(entry.director, roster.UnderDirector)
                                     and entry.director.address in addresses)
                                    or (isinstance(entry.showrunner, roster.UnderShowrunner)
                                        and entry.showrunner.address in addresses))]
    for identity, candidates in entries.items():
        observed = set[str]()
        for entry in candidates:
            native = (entry.session.session_id if isinstance(entry.session, roster.ClaudeSession)
                      else entry.thread.thread_id if isinstance(entry.thread, roster.CodexThread) else "")
            try:
                observed.add(staff.native_id(native))
            except staff.StaffRefused as error:
                problems.append(f"{identity}: invalid peer native identity: {error}")
        if observed:
            identities[identity] = observed
    return identities, tuple(problems)


def migration_sources(record: staff.StaffSnapshot) -> tuple[_MemberSources, tuple[str, ...]]:
    """Read only stored production documents and non-scratch launch evidence."""
    problems: list[str] = []
    import herdr
    from scripts.shutdown import pane_record
    launches = pane_record.read_strict(herdr.shared_session())
    permitted: list[pane_record.PaneLaunch] = []
    for launch in launches.values():
        owner = record.by_member_reference(launch["session_id"])
        if owner is None:
            owner = record.by_id(staff.StaffId(launch["environment"].get("STAFF_ID", "")))
        if owner is not None and owner.kind == "worker":
            owner = record.parent_of(owner)
        if owner is not None:
            location = owner.worktree
            if not isinstance(location, staff.WorktreeUnfilled) and source_allowed(location.path):
                permitted.append(launch)
            elif isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) and source_allowed(owner.doc.path):
                permitted.append(launch)
    productions: dict[str, showrunners.ShowrunnerSource] = {}
    for row in record.find(kind="showrunner"):
        location = row.doc
        if not isinstance(location, (staff.LiveDoc, staff.WrappedDoc)):
            problems.append(f"{row.id} {row.name}: missing stored production document")
            continue
        if location.machine != _fill_machine():
            problems.append(f"{row.id} {row.name}: remote production evidence requires peer read")
            continue
        if not source_allowed(location.path):
            problems.append(f"{row.id} {row.name}: scratch production source refused: {location.path}")
            continue
        try:
            document = location.path.read_text(encoding="utf-8")
            slug = FillDocument(location.path, document, str(location.path)).slug
            timer = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}"
            if timer.exists():
                source = showrunners.read_bound_source(timer, location.path)
            else:
                source = showrunners.ShowrunnerSource(showrunners.Timer(slug, "", location.path, ""), document)
            productions[slug] = source
        except (OSError, ValueError) as error:
            problems.append(f"{row.id} {row.name}: {error}")
    return BoundMigrationSources(productions, tuple(permitted)), tuple(problems)


class _LiveFillSources(_FillSources):
    """One live checkout per staff parent; other document copies are not evidence."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 dependencies: staff.RecordInputs) -> None:
        super().__init__(record, sources, dependencies)
        registry = sessions.read_registry(dependencies=dependencies)
        self.running: dict[str, sessions.RegistryObservation] = {
            item.record["sessionId"]: item for item in registry.entries
            if sessions.running_session(item.record)
        }
        self.row_sessions: dict[staff.StaffId, list[sessions.RegistryObservation]] = {}
        # Use the same id-first join as every other membership reader.
        for launch in sources.launches:
            owner = record.by_member_reference(launch["session_id"])
            if launch.get("ended") or owner is None or owner.current_session not in self.running:
                continue
            environment = launch["environment"]
            identity = owner.current_session
            if owner.kind == "showrunner":
                self.row_sessions.setdefault(owner.id, []).append(self.running[identity])
                continue
            try:
                found = sources.member(ObservedUnit(environment.get("SHOWRUNNER_UNIT_ID", ""),
                                                   session_staff.LiveSession(identity)), record)
            except (ValueError, staff.StaffUnreadable):
                continue
            if isinstance(found, MemberFound):
                self.row_sessions.setdefault(found.row.id, []).append(self.running[identity])
        self.timers: dict[staff.StaffId, list[showrunners.ShowrunnerSource]] = {}
        self.timer_errors: dict[staff.StaffId, str] = {}

        def timer_owner(slug: str, source: showrunners.ShowrunnerSource) -> MemberFound | NoSuchMember:
            owner = record.by_member_reference(source.timer.target.removeprefix("session:"))
            if owner is not None and owner.kind == "showrunner" and isinstance(owner.doc, staff.DocUnfilled):
                return MemberFound(owner)
            return _MemberSources({slug: source}, sources.launches).member(ShowrunnerMember(slug), record)

        # The shared membership reader omits unreadable docs. Reuse its timer
        # decoder for omitted instances so failed evidence still identifies a row.
        for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
            slug = instance.name.removeprefix("showrunner-")
            if not instance.is_dir() or slug in sources.productions:
                continue
            # Keep the timer target available to the canonical launch-id join,
            # even when its document cannot be read. Empty document text supplies
            # no location evidence; only document_for may admit a live document.
            conf = instance / "conf"
            try:
                target = _timer_fields(conf, dependencies=dependencies).get("TARGET", "")
            except (OSError, UnicodeError):
                target = ""
            timer = showrunners.Timer(slug, target, conf, "")
            owner = record.by_member_reference(target.removeprefix("session:"))
            found = (MemberFound(owner) if owner is not None and owner.kind == "showrunner"
                     else timer_owner(slug, showrunners.ShowrunnerSource(timer, "")))
            if not isinstance(found, MemberFound):
                continue
            attempted: list[Path] = []

            def record_input(path: Path) -> None:
                dependencies(path)
                attempted.append(path)

            try:
                recovered = _source(instance, dependencies=record_input)
            except (OSError, UnicodeError, KeyError, ValueError) as error:
                self.timer_errors[found.row.id] = f"{attempted[-1]}: {error}"
            else:
                document_owner = timer_owner(slug, recovered)
                if isinstance(document_owner, MemberFound):
                    self.timers.setdefault(document_owner.row.id, []).append(recovered)
        for slug, source in sources.productions.items():
            found = timer_owner(slug, source)
            if isinstance(found, MemberFound):
                self.timers.setdefault(found.row.id, []).append(source)
                session = self.running.get(found.row.current_session)
                if session is not None:
                    self.row_sessions.setdefault(found.row.id, []).append(session)

    def load_document(self, path: Path) -> str:
        if not self.source_allowed(path):
            raise ValueError("scratch production document is not evidence")
        self.dependencies(path)
        return path.read_text(encoding="utf-8")

    def document_for(self, row: staff.StaffRow) -> FillDocument:
        candidates = self.timers.get(row.id, [])
        if row.id in self.timer_errors:
            raise ValueError(self.timer_errors[row.id])
        if not candidates:
            raise ValueError("no timer names its live production document")
        documents: dict[Path, FillDocument] = {}
        for source in candidates:
            pointer = FillDocument(source.timer.doc, source.document, str(source.timer.doc))
            location = _fill_doc_location(pointer, _fill_machine())
            document = FillDocument(location.path, self.load_document(location.path), str(location.path))
            # Validate the actual checkout's fields, rather than an older copy's.
            if _fill_doc_location(document, _fill_machine()).path != location.path:
                raise ValueError(f"{location.path}: live document points to another checkout")
            documents[location.path.resolve()] = document
        if len(documents) != 1:
            raise ValueError("timers disagree on the live document: " + ", ".join(map(str, documents)))
        document = next(iter(documents.values()))
        common = _fill_git(document.path.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.manifest.add(f"repository {common}")
        if WRAPPED in document.text or re.search(r"(?im)^- \*\*Status:\*\*\s*wrapped\b", document.text):
            raise ValueError(f"{document.path}: production is wrapped")
        return document

def _fill_live(record: staff.StaffSnapshot, sources: _MemberSources,
               dependencies: staff.RecordInputs, *, dry_run: bool) -> int:
    scan = _LiveFillSources(record, sources, dependencies)
    machine = _fill_machine()
    documents: dict[staff.StaffId, FillDocument] = {}
    doc_errors: dict[staff.StaffId, str] = {}
    changed = False
    recovery_errors: list[str] = []
    for row in record.find(kind="showrunner"):
        if isinstance(row.doc, staff.WrappedDoc):
            continue
        if row.id not in scan.timers and row.id not in scan.timer_errors and row.id not in scan.row_sessions:
            continue
        try:
            document = scan.document_for(row)
            documents[row.id] = document
            location = staff.LiveDoc(machine, document.path)
            if isinstance(row.doc, staff.DocUnfilled):
                if not dry_run:
                    _ = staff.set_doc(row.id, location)
                changed = True
        except (OSError, UnicodeError, ValueError, Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
            doc_errors[row.id] = str(error)
            if isinstance(row.doc, staff.DocUnfilled):
                scan.problem(row.id, row.name, error)
    for row in record.find(kind="unit"):
        if isinstance(row.worktree, staff.RemovedWorktree):
            continue
        unfilled = isinstance(row.worktree, staff.WorktreeUnfilled)
        if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.machine != machine:
            continue
        if (isinstance(row.parent, staff.ReportsTo) and row.parent.id not in documents
                and row.parent.id not in doc_errors and row.id not in scan.row_sessions):
            continue
        # Existing files supply their values. The writer still checks their formatted bytes.
        try:
            location = _fill_worktree(row)
            dependencies(location.path / ".unit.toml")
            current = unit_config.read(location.path, cleanup=False)
            if isinstance(current, unit_config.ConfigMissing):
                raise ValueError(current.reason)
            if dry_run:
                changed |= unit_config.needs_write(location.path, current)
            else:
                changed |= isinstance(unit_config.write(location.path, current), unit_config.Published)
        except (OSError, UnicodeError, ValueError, Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
            if unfilled:
                scan.problem(row.id, row.name, error)
            else:
                recovery_errors.append(f"recovery {row.id} {row.name}: {str(error).replace(chr(10), ' ')}")
    if changed or dry_run or scan.problems or recovery_errors:
        for source in sorted(dependencies.paths | scan.manifest):
            print(f"source {source}")
    for problem in (*scan.problems, *recovery_errors):
        print(problem, file=sys.stderr)
    if not scan.problems and not recovery_errors:
        print("changed" if changed else "unchanged")
    return 1 if scan.problems or recovery_errors else 0


def require_worker_stopped(record: staff.StaffSnapshot, identity: staff.StaffId) -> None:
    """Refuse removal while worker or peer evidence still names the native id."""
    evidence = observe_workers(record, session_staff.member_sources(), current_only=True)
    member = record.by_id(identity)
    current_session = member.current_session if member is not None else identity
    if identity in evidence.identities or any(
        not isinstance(worker.identity, BackendUnavailable)
        and current_session == _observation_session(worker.identity) for worker in evidence.workers
    ):
        raise staff.StaffRefused(f"{identity}: native worker is current; stop it before removing its row")
    # Remote process evidence belongs to the roster's one peer reader.
    row = record.by_id(identity)
    while row is not None:
        location = row.worktree if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else row.doc
        if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc)):
            if location.machine != remote.local_machine():
                from scripts.message import roster
                peer = roster.peer_snapshot(os.environ)
                if isinstance(peer, roster.PeerNotReached):
                    raise staff.StaffRefused(f"{identity}: remote native session state is uncertain: {peer.reason}")
                if peer.problems:
                    raise staff.StaffRefused(f"{identity}: remote native session state is uncertain: {peer.problems[0]}")
                for entry in peer.entries:
                    native = (entry.session.session_id if isinstance(entry.session, roster.ClaudeSession)
                              else entry.thread.thread_id if isinstance(entry.thread, roster.CodexThread) else "")
                    if native == current_session:
                        raise staff.StaffRefused(f"{identity}: native session is live on {entry.machine}; stop it before removing its row")
            break
        row = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None


def fill(*, dry_run: bool = False) -> int:
    """Fill showrunner docs and rewrite unit configuration from current staff rows."""
    with nullcontext() if dry_run else staff.operation():
        record = staff.snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        return _fill_live(record, _member_sources(dependencies=dependencies), dependencies, dry_run=dry_run)


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=["seed", "fill"])
    _ = parser.add_argument("--dry-run", action="store_true")
    arguments = _Arguments()
    _ = parser.parse_args(argv, namespace=arguments)

    def run() -> int:
        if arguments.command == "fill":
            return fill(dry_run=arguments.dry_run)
        seed(dry_run=arguments.dry_run)
        return 0

    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main())
