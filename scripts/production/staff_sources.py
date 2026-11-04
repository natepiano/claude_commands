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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from importlib import import_module
import sys
from pathlib import Path
from typing import Literal, cast, override

# These source modules still use sibling imports; keep their existing module identity.

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_units  # noqa: E402
from scripts.production import showrunners  # noqa: E402
# The public batch reader drops failures; share its decoder within this repair's
# two-file boundary instead of copying its timer parser.
from scripts.production.showrunners import (  # noqa: E402
    _fields as _timer_fields,  # pyright: ignore[reportPrivateUsage]
    _source as _source,  # pyright: ignore[reportPrivateUsage]
)
import unit_names  # noqa: E402
from scripts.production import staff, unit_config, session_staff  # noqa: E402
from scripts.production.session_labels import showrunner_owner  # noqa: E402
from scripts.direct import workers  # noqa: E402
from scripts.lib import git_exclude  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.shutdown import remote  # noqa: E402


# Matchers live below production_units, so lower readers use the same join without an import cycle.
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


def ensure_staff_showrunner(child: production_units.Production, caller: str = "", *, runtime_id: str = "") -> staff.StaffRow:
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
    imported_unit_names = {row.name for row in record.find(kind="unit")}
    for slug, source in sources.productions.items():
        try:
            cells = production_units.unit_table(source.document.splitlines())
            runtime_id = source.timer.target.removeprefix("session:")
            owner = record.by_member_reference(runtime_id)
            found = (MemberFound(owner) if owner is not None and owner.kind == "showrunner"
                     else sources.member(ShowrunnerMember(slug), record))
            production_members = [_seed_member(_showrunner_key(slug), "showrunner",
                                               showrunners.space_from_text(slug, source.document), found,
                                               staff.KeepSeedParent(), runtime_id=source.timer.target.removeprefix("session:"))]
            for unit_cells in cells:
                unit = production_units.cell_value(unit_cells.get("Unit", ""))
                name = unit_names.name(unit)
                identities = {entry["session_id"] for entry in sources.launches
                              if entry["environment"].get("SHOWRUNNER_UNIT") == slug
                              and entry["environment"].get("SHOWRUNNER_UNIT_ID") == unit}
                existing: dict[staff.StaffId, staff.StaffRow] = {}
                if isinstance(found, MemberFound):
                    for identity in identities:
                        matched = session_staff.unit_match(
                            UnitMember(slug, unit), record, session_staff.StoredReference(identity),
                            parent=session_staff.RequiredParent(found.row))
                        if isinstance(matched, MemberFound):
                            existing[matched.row.id] = matched.row
                if len(existing) > 1:
                    raise staff.StaffUnreadable(f"{slug}/{unit}: conflicting staff identities")
                selected = (MemberFound(next(iter(existing.values()))) if existing
                            else sources.member(UnitMember(slug, unit), record))
                if isinstance(selected, MemberFound):
                    production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                           staff.KeepSeedParent()))
                    continue
                # A moved unit can remain in its old Units table. Import never recreates it there.
                if name in imported_unit_names:
                    continue
                path_text = production_units.cell_value(unit_cells.get("Worktree", ""))
                if (not path_text or not Path(path_text).expanduser().is_dir()
                        or production_units.plan_cell_is_retired(unit_cells.get("Plan", ""))):
                    continue
                if len(identities) != 1:
                    _problem(unit, "no unique native session identity for seed")
                    continue
                production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                       staff.SeedParent(_showrunner_key(slug)),
                                                       runtime_id=next(iter(identities))))
        except (OSError, UnicodeError, production_units.Refusal, unit_names.BadName) as error:
            print(f"seed: {slug} ({source.timer.doc}): {error}", file=sys.stderr)
            continue
        members.extend(production_members)
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


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], value)







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
        self.documents: dict[tuple[Path, str], FillDocument] = {}
        self.repositories: set[Path] = set()
        self.manifest: set[str] = set()
        self.problems: list[str] = []
        self.missing_owners: set[str] = set()
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

    def repository(self, path: Path) -> None:
        if not self.source_allowed(path):
            return
        folder = path if path.is_dir() else path.parent
        try:
            common = Path(_fill_git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            if not self.source_allowed(common):
                return
            # The common directory names the repository once, even for linked worktrees.
            self.repositories.add(common)
            self.manifest.add(f"repository {common}")
        except (OSError, ValueError) as error:
            self.problem(path, "repository", error)

    def document(self, path: Path) -> None:
        path = path.expanduser().absolute()
        if not self.source_allowed(path):
            return
        if any(document.path == path for document in self.documents.values()):
            return
        slug = showrunners.production_slug(str(path))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            self.problem(path, "document", error)
            return
        # Share the document's session-name join, including owners without timers.
        owner = showrunner_owner(self.record, slug, text)
        if owner is None:
            # An unowned old copy cannot make an already-bound production missing.
            if any(document.slug == slug for document in self.documents.values()):
                return
            if slug not in self.missing_owners:
                self.missing_owners.add(slug)
                self.problem(path, slug, "no current showrunner staff row")
            return
        location = owner.doc
        if not isinstance(location, (staff.LiveDoc, staff.WrappedDoc)):
            self.problem(path, slug, "showrunner document is unfilled")
            return
        if location.machine != _fill_machine():
            self.problem(path, slug, f"showrunner document is on {location.machine}")
            return
        bound_path = location.path.expanduser().absolute()
        if not self.source_allowed(bound_path):
            return
        if bound_path != path:
            path = bound_path
            if any(document.path == path for document in self.documents.values()):
                return
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                self.problem(path, "document", error)
                return
        self.dependencies(path)
        self.documents[(path, text)] = FillDocument(path, text, str(path))

    def _repository_documents(self, common: Path) -> None:
        # --git-dir works for both a main checkout and its linked worktrees.
        def git(*arguments: str) -> str:
            done = subprocess.run(["git", "--git-dir", str(common), *arguments],
                                  capture_output=True, text=True, check=False)
            if done.returncode:
                raise ValueError(done.stderr.strip() or f"cannot read repository {common}")
            return done.stdout.strip()

        try:
            listing = git("worktree", "list", "--porcelain")
            worktrees = [Path(line.removeprefix("worktree ")) for line in listing.splitlines()
                         if line.startswith("worktree ")]
            for worktree in worktrees:
                if not self.source_allowed(worktree):
                    continue
                self.manifest.add(f"worktree {worktree}")
                for path in sorted((worktree / "docs").rglob("*-production.md")):
                    self.document(path)
        except (OSError, UnicodeError, ValueError) as error:
            self.problem(common, "repository", error)

    def collect(self) -> tuple[FillDocument, ...]:
        for row in self.record.find(kind="showrunner"):
            location = row.doc
            if (isinstance(location, (staff.LiveDoc, staff.WrappedDoc))
                    and location.machine == _fill_machine()):
                self.document(location.path)
        for source in self.sources.productions.values():
            path = source.timer.doc.expanduser().absolute()
            self.document(path)
        for path_text in tuple(self.dependencies.paths):
            path = Path(path_text)
            if path.name.endswith("-production.md") and not any(doc.path == path for doc in self.documents.values()):
                self.document(path)
                self.repository(path)
        for row in self.record.rows:
            location = row.worktree if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else row.doc
            if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc)):
                if location.machine == _fill_machine():
                    if location.path.exists():
                        self.repository(location.path)
        state = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or showrunners.SHOWRUNNER_STATE_DIR)
        slugs: set[str] = set()
        for folder in sorted(state.glob("*")):
            if not folder.is_dir():
                continue
            slugs.add(folder.name)
            self.manifest.add(f"showrunner {folder}")
            # A state folder may itself live inside a checkout or link to one.
            if (folder.resolve() / ".git").exists():
                self.repository(folder.resolve())
        config = Path(os.environ.get("SHOWRUNNERS_CONFIG") or showrunners.CONFIG)
        self.dependencies(config)
        if config.exists():
            try:
                settings = _object(cast(object, json.loads(config.read_text(encoding="utf-8"))))
                always = settings.get("always", [])
                if not isinstance(always, list) or any(not isinstance(item, str) for item in cast(list[object], always)):
                    raise ValueError("always must list production slugs")
                slugs.update(cast(list[str], always))
            except (OSError, UnicodeError, ValueError) as error:
                self.problem(config, "showrunners", error)
        for common in sorted(self.repositories):
            self._repository_documents(common)
        found_slugs = {document.slug for document in self.documents.values()}
        found_slugs.update(showrunners.production_slug(str(path)) for path in self.excluded_documents
                           if path.name.endswith("-production.md"))
        for slug in sorted(slugs - found_slugs):
            if slug not in self.missing_owners:
                owner = self.sources.member(ShowrunnerMember(slug), self.record)
                reason = ("no current showrunner staff row" if isinstance(owner, NoSuchMember)
                          else "no source holds its production doc")
                self.problem(state / slug, slug, reason)
        # Timer parsing may have read a stale copy. It is not retirement evidence.
        authoritative = {str(document.path) for document in self.documents.values()}
        self.dependencies.paths.difference_update(
            path for path in tuple(self.dependencies.paths)
            if Path(path).name.endswith("-production.md") and path not in authoritative
        )
        return tuple(self.documents.values())


def _fill_machine() -> str:
    # Use the same machine names as shutdown records, without a second host rule.
    return remote.local_machine()


def _fill_config(cells: dict[str, str]) -> unit_config.UnitConfig:
    nightly_unit = cast(str, import_module("scripts.production.add_unit").NIGHTLY_REVIEW_UNIT)

    plan_text = production_units.cell_value(cells.get("Plan", ""))
    if not plan_text or plan_text in ("—", "-"):
        raise ValueError("missing Plan cell")
    plan = (unit_config.StandbyPlan() if plan_text == "standby" else
            unit_config.PermanentPlan() if plan_text == "permanent" else unit_config.PlanFile(Path(plan_text)))
    if "Port" not in cells:
        raise ValueError("Units row has no Port column")
    port = unit_config.parse_port_cell(cells["Port"])
    unit = production_units.cell_value(cells.get("Unit", ""))
    duties = ("nightly-review",) if unit_names.unit_id(unit) == nightly_unit else ()
    return unit_config.UnitConfig(plan, port, duties)


def _fill_worktree(row: staff.StaffRow, cells: dict[str, str], machine: str) -> staff.PresentWorktree:
    if isinstance(row.worktree, staff.PresentWorktree):
        return row.worktree
    if isinstance(row.worktree, staff.RemovedWorktree):
        raise ValueError(f"{row.name}: staff worktree is removed")
    path_text = production_units.cell_value(cells.get("Worktree", ""))
    branch = production_units.cell_value(cells.get("Branch", ""))
    if not path_text or path_text in ("—", "-"):
        raise ValueError("missing Worktree cell")
    worktree = Path(path_text).expanduser()
    if not worktree.is_absolute():
        raise ValueError("Worktree cell must be absolute")
    if not branch:
        raise ValueError("missing Branch cell")
    actual = _fill_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    root = Path(_fill_git(worktree, "rev-parse", "--show-toplevel"))
    if root.resolve() != worktree.resolve():
        raise ValueError("Worktree cell is not a git worktree root")
    if actual != branch:
        raise ValueError(f"worktree is on branch {actual}, expected {branch}")
    return staff.PresentWorktree(machine, worktree)



def _fill_doc_location(document: FillDocument, machine: str) -> staff.LiveDoc:
    lines = document.text.splitlines()
    checkout = Path(production_units.production_field(lines, "Showrunner checkout")).expanduser()
    if not checkout.is_absolute():
        raise ValueError("Showrunner checkout must be absolute")
    root = Path(_fill_git(document.path.parent, "rev-parse", "--show-toplevel"))
    checkout_root = Path(_fill_git(checkout, "rev-parse", "--show-toplevel"))
    if checkout_root.resolve() != checkout.resolve():
        raise ValueError("Showrunner checkout is not a git worktree root")
    path = checkout / document.path.relative_to(root)
    branch = production_units.production_field(lines, "Merge branch")
    actual = _fill_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if actual != branch:
        raise ValueError(f"showrunner checkout is on branch {actual}, expected {branch}")
    if not path.is_file():
        raise ValueError(f"live production doc is missing: {path}")
    return staff.LiveDoc(machine, path)


def prepare_retirement(cells: dict[str, str], sources: _MemberSources,
                       selected: MemberFound | NoSuchMember, slug: str,
                       *, date: str = "", dependencies: Callable[[Path], None] | None = None) -> unit_config.UnitConfig:
    """Prepare complete facts using the existing note and transcript readers.

    A missing session uses scoped launch evidence, then the interactive transcripts
    in the recorded worktree's project folder. A same-name session in another production is not evidence.
    """
    # Reuse the transitional note parser and transcript reader without a module cycle.
    resume = import_module("scripts.production.resume_unit")
    retirement = import_module("scripts.production.retire_unit")
    retired_note = cast(re.Pattern[str], resume.RETIRED)
    session_note = cast(re.Pattern[str], resume.SESSION)
    resume_folder = cast(Callable[..., Path], retirement.director_resume_folder)
    refused = cast(type[Exception], retirement.Refused)

    plan = cells.get("Plan", "")
    note = retired_note.match(plan)
    session = session_note.match(plan)
    if note is not None:
        date = note[0].strip().removeprefix("(retired ").partition(",")[0].removesuffix(")")
    config = _fill_config({**cells, "Plan": retired_note.sub("", plan)})
    session_id = session[1] if session is not None else ""
    if not session_id:
        name = production_units.cell_value(cells.get("Unit", ""))
        evidence = [launch for launch in sources.launches
                    if launch["session_id"] and isinstance(selected, MemberFound)
                    and launch["session_id"] == selected.row.current_session]
        if not evidence:
            evidence = [launch for launch in sources.launches
                        if launch["session_id"]
                        and launch["environment"].get("SHOWRUNNER_UNIT") == slug
                        and launch["environment"].get("SHOWRUNNER_UNIT_ID") == name]
        identities = {launch["session_id"] for launch in evidence}
        if len(identities) > 1:
            raise ValueError("no unique recorded director session for retirement")
        if identities:
            session_id = next(iter(identities))
        else:
            if isinstance(selected, MemberFound) and not isinstance(selected.row.worktree, staff.WorktreeUnfilled):
                location = selected.row.worktree
                if location.machine != remote.local_machine():
                    raise ValueError(f"staff worktree is on {location.machine}")
                worktree = location.path
            else:
                path_text = production_units.cell_value(cells.get("Worktree", ""))
                if not path_text or path_text in ("—", "-"):
                    raise ValueError("missing legacy Worktree cell")
                worktree = Path(path_text).expanduser()
            recover = cast(Callable[..., str], retirement.last_interactive_session)
            session_id = recover(worktree, dependencies=dependencies)
            if not session_id:
                raise ValueError("no unique recorded director session for retirement")
    if not date:
        raise ValueError("missing retirement date")
    branch = production_units.cell_value(cells.get("Branch", ""))
    if not branch or branch in ("—", "-"):
        raise ValueError("missing Branch cell")
    try:
        if session is not None and session[2]:
            folder = Path(session[2]).expanduser()
        else:
            folder = resume_folder(session_id, dependencies=dependencies)
    except refused as error:
        raise ValueError(str(error)) from error
    return replace(config, retired=unit_config.RetiredSession(
        date, session_id, unit_config.RecordedStartFolder(folder), unit_config.Branch(branch)))


@dataclass(frozen=True)
class RetiredFillUnit:
    name: str
    member: MemberFound | NoSuchMember
    worktree: staff.RemovedWorktree
    config: unit_config.UnitConfig
    source: str
    needs_publication: bool


@dataclass(frozen=True)
class WrappedFillDoc:
    member: MemberFound
    location: staff.WrappedDoc


@dataclass(frozen=True)
class RetirementPlan:
    owners: dict[str, WrappedFillDoc]
    units: dict[tuple[str, str], RetiredFillUnit]


def retirement_plan(scan: _FillSources, documents: tuple[FillDocument, ...],
                    joined: _MemberSources, machine: str, *, check_format: bool = True) -> RetirementPlan:
    """Prove retired facts once for fill and the native-id migration."""
    from scripts.production.production_units import WRAPPED

    owners: dict[str, WrappedFillDoc] = {}
    units: dict[tuple[str, str], RetiredFillUnit] = {}
    rejected_owners: set[str] = set()
    rejected_units: set[tuple[str, str]] = set()
    texts: dict[str, FillDocument] = {}
    for document in documents:
        slug = document.slug
        try:
            previous = texts.get(slug)
            if previous is not None and (previous.text.strip() != document.text.strip()
                                         or previous.path != document.path):
                rejected_owners.add(slug)
                raise ValueError(f"conflicting production documents: {previous.source} and {document.source}")
            texts[slug] = document
            wrapped = WRAPPED in document.text or bool(re.search(
                r"(?im)^(?:>\s*)?(?:-\s*)?\*\*Status:\*\*\s*wrapped\b", document.text))
            owner = joined.member(ShowrunnerMember(slug), scan.record)
            if isinstance(owner, NoSuchMember):
                raise ValueError("no current showrunner staff row")
            if wrapped:
                if not document.path.is_file():
                    raise ValueError(f"wrapped production doc is missing: {document.path}")
                if document.path.read_text(encoding="utf-8").strip() != document.text.strip():
                    raise ValueError(f"wrapped document evidence differs from {document.path}")
                location = staff.WrappedDoc(machine, document.path)
                if not isinstance(owner.row.doc, staff.DocUnfilled) and owner.row.doc != location:
                    raise ValueError(f"existing doc {owner.row.doc!r} conflicts with wrapped location {location!r}")
                owners[slug] = WrappedFillDoc(owner, location)
            lines = document.text.splitlines()
            cells_list = production_units.unit_table(lines)
            _, raw_rows = production_units.unit_rows(lines)
            search_from = lines.index("## Units")
            for cells, raw in zip(cells_list, raw_rows, strict=True):
                line = lines.index(raw, search_from) + 1
                search_from = line
                name = production_units.cell_value(cells.get("Unit", ""))
                key = (slug, name)
                if not production_units.plan_cell_is_retired(cells.get("Plan", "")):
                    if wrapped:
                        scan.problem(f"{document.path}:{line}", name, "wrapped unit has no retired note")
                    continue
                try:
                    selected = joined.member(UnitMember(slug, name), scan.record)
                    if isinstance(selected, MemberFound) and isinstance(selected.row.worktree, staff.RemovedWorktree):
                        location = selected.row.worktree
                    else:
                        path_text = production_units.cell_value(cells.get("Worktree", ""))
                        if not path_text or path_text in ("—", "-"):
                            raise ValueError("missing Worktree cell")
                        location = staff.RemovedWorktree(machine, Path(path_text).expanduser())
                        if (isinstance(selected, MemberFound)
                                and isinstance(selected.row.worktree, staff.PresentWorktree)
                                and (selected.row.worktree.machine, selected.row.worktree.path)
                                != (location.machine, location.path)):
                            raise ValueError(f"existing worktree {selected.row.worktree!r} conflicts with removed location {location!r}")
                    if location.machine != machine:
                        raise ValueError(f"{name}: staff worktree is on {location.machine}")
                    if location.path.exists():
                        raise ValueError(f"retired worktree still exists: {location.path}")
                    config = prepare_retirement(cells, joined, selected, slug, dependencies=scan.dependencies)
                    repository = Path(_fill_git(document.path.parent, "rev-parse", "--show-toplevel"))
                    config = replace(config, formatting=unit_config.repository_formatting(repository))
                    needs_publication = True
                    if isinstance(selected, MemberFound):
                        parked = unit_config.parked_path(selected.row.id)
                        scan.dependencies(parked)
                        current = unit_config.read_parked(selected.row.id, cleanup=False)
                        if isinstance(current, unit_config.UnitConfig) and current != config:
                            raise ValueError(f"{parked}: existing parked configuration conflicts with document facts")
                        if isinstance(current, unit_config.UnitConfig) and isinstance(current.formatting, unit_config.RepositoryTaploConfig):
                            config = replace(config, formatting=current.formatting)
                        needs_publication = (selected.row.worktree != location
                                             or isinstance(current, unit_config.ConfigMissing)
                                             or (check_format and unit_config.parked_needs_write(selected.row.id, config)))
                    candidate = RetiredFillUnit(name, selected, location, config, f"{document.path}:{line}",
                                               needs_publication)
                    previous_unit = units.get(key)
                    if previous_unit is not None and (previous_unit.worktree, previous_unit.config) != (location, config):
                        raise ValueError(f"conflicting unit facts with {previous_unit.source}")
                    units[key] = candidate
                except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused) as error:
                    rejected_units.add(key)
                    scan.problem(f"{document.path}:{line}", name, error)
        except (OSError, ValueError, production_units.Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
            rejected_owners.add(slug)
            scan.problem(document.source, slug, error)
    return RetirementPlan({slug: owner for slug, owner in owners.items() if slug not in rejected_owners},
                          {key: value for key, value in units.items()
                           if key[0] not in rejected_owners and key not in rejected_units})


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
                          and entry.cwd == str(location.path)]
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
    permitted = tuple(launch for launch in launches.values() if source_allowed(Path(launch["cwd"])))
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
    return BoundMigrationSources(productions, permitted), tuple(problems)


def migration_retirements(record: staff.StaffSnapshot) -> tuple[RetirementPlan, tuple[str, ...]]:
    """Migration production evidence comes only from each stored document."""
    dependencies = staff.RecordInputs(set(record.inputs))
    current, source_problems = migration_sources(record)
    scan = _FillSources(record, current, dependencies)
    documents: list[FillDocument] = []
    bound: dict[str, showrunners.ShowrunnerSource] = {}
    for row in record.find(kind="showrunner"):
        location = row.doc
        if not isinstance(location, (staff.LiveDoc, staff.WrappedDoc)):
            scan.problem(row.id, row.name, "missing stored production document")
            continue
        if location.machine != _fill_machine():
            scan.problem(row.id, row.name, "remote production document requires peer evidence")
            continue
        if not scan.source_allowed(location.path):
            continue
        try:
            dependencies(location.path)
            text = location.path.read_text(encoding="utf-8")
            document = FillDocument(location.path, text, str(location.path))
            documents.append(document)
            bound[document.slug] = showrunners.ShowrunnerSource(
                showrunners.Timer(document.slug, "session:" + row.id, location.path, ""), text)
        except (OSError, ValueError) as error:
            scan.problem(location.path, row.name, error)
    joined = _MemberSources(bound, current.launches)
    return retirement_plan(scan, tuple(documents), joined, _fill_machine()), (*source_problems, *scan.problems)


def _fill_retired(scan: _FillSources, documents: tuple[FillDocument, ...],
                  joined: _MemberSources, machine: str, *, dry_run: bool) -> int:
    """Repair retained facts; native additions belong to the one-time migration."""
    plan = retirement_plan(scan, documents, joined, machine, check_format=dry_run)
    owners, units = plan.owners, plan.units
    rejected_owners: set[str] = set()
    rejected_units: set[tuple[str, str]] = set()
    planned_changes = any(candidate.member.row.doc != candidate.location
                          for slug, candidate in owners.items() if slug not in rejected_owners)
    planned_changes |= any(candidate.needs_publication for (slug, name), candidate in units.items()
                           if slug not in rejected_owners and (slug, name) not in rejected_units)
    if planned_changes or dry_run or scan.problems:
        for source in sorted(scan.dependencies.paths | scan.manifest):
            print(f"source {source}")
    changed = False
    for (slug, name), candidate in sorted(units.items()):
        if slug in rejected_owners or (slug, name) in rejected_units:
            continue
        if isinstance(candidate.member, NoSuchMember):
            if dry_run:
                print(f"would add retired unit {slug}/{name}: {candidate.worktree.path}")
            else:
                print(f"deferred: retired unit {slug}/{name} waits for Phase 3h")
        elif dry_run and candidate.needs_publication:
            print(f"would repair retired unit {slug}/{name}: {candidate.worktree.path}")
    if dry_run:
        changed = planned_changes
    else:
        for slug, owner in sorted(owners.items()):
            if slug in rejected_owners:
                continue
            try:
                if owner.member.row.doc != owner.location:
                    _ = staff.set_doc(owner.member.row.id, owner.location)
                    changed = True
            except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused) as error:
                rejected_owners.add(slug)
                scan.problem(owner.location.path, slug, error)
        for (slug, name), candidate in sorted(units.items()):
            if slug in rejected_owners or (slug, name) in rejected_units:
                continue
            try:
                selected = candidate.member
                if isinstance(selected, NoSuchMember):
                    continue
                row = selected.row
                if row.worktree != candidate.worktree:
                    row = staff.set_worktree(row.id, candidate.worktree, expected=row.worktree)
                    changed = True
                publication = unit_config.write_parked(row.id, candidate.config)
                changed |= isinstance(publication, unit_config.Published)
            except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused) as error:
                scan.problem(candidate.source, name, error)
    for problem in scan.problems:
        print(problem, file=sys.stderr)
    print("changed" if changed else "unchanged")
    return 1 if scan.problems else 0


@dataclass(frozen=True)
class _LiveUnitFill:
    row: staff.StaffRow
    worktree: staff.PresentWorktree
    config: unit_config.UnitConfig | unit_config.ConfigMissing
    source: str


@dataclass(frozen=True)
class _RetiredUnitDeferred:
    """Its old Units note belongs to the separate retired fill."""


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
        self.director_folders: dict[staff.StaffId, set[str]] = {}
        self.director_names: dict[staff.StaffId, set[str]] = {}
        self.document_cache: dict[production_units.DocumentSource, str | production_units.DocumentUnavailable] = {}
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
                if launch["cwd"]:
                    self.director_folders.setdefault(found.row.id, set()).add(launch["cwd"])
                previous_name = environment.get("SHOWRUNNER_UNIT_ID", "")
                try:
                    previous_name = unit_names.name(previous_name)
                except unit_names.BadName:
                    continue
                self.director_names.setdefault(found.row.id, set()).add(previous_name)
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

    def load_document(self, source: production_units.DocumentSource) -> str | production_units.DocumentUnavailable:
        if source not in self.document_cache:
            try:
                if source.machine != _fill_machine():
                    raise ValueError("remote production document requires peer evidence")
                if not self.source_allowed(source.path):
                    raise ValueError("scratch production document is not evidence")
                self.dependencies(source.path)
                self.document_cache[source] = source.path.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as error:
                self.document_cache[source] = production_units.DocumentUnavailable(str(error))
        return self.document_cache[source]

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
            text = self.load_document(production_units.DocumentSource(location.machine, location.path))
            if isinstance(text, production_units.DocumentUnavailable):
                raise ValueError(text.reason)
            document = FillDocument(location.path, text, str(location.path))
            # Validate the actual checkout's fields, rather than an older copy's Units table.
            if _fill_doc_location(document, _fill_machine()).path != location.path:
                raise ValueError(f"{location.path}: live document points to another checkout")
            documents[location.path.resolve()] = document
        if len(documents) != 1:
            raise ValueError("timers disagree on the live document: " + ", ".join(map(str, documents)))
        document = next(iter(documents.values()))
        common = _fill_git(document.path.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.manifest.add(f"repository {common}")
        from scripts.production.production_units import WRAPPED

        if WRAPPED in document.text or re.search(r"(?im)^- \*\*Status:\*\*\s*wrapped\b", document.text):
            raise ValueError(f"{document.path}: production is wrapped")
        return document

    def session_worktree(self, row: staff.StaffRow, machine: str) -> staff.PresentWorktree:
        folders = self.director_folders.get(row.id, set()) - {""}
        roots: set[Path] = set()
        for folder in folders:
            path = Path(folder).expanduser()
            if not path.is_absolute():
                continue
            try:
                roots.add(Path(_fill_git(path, "rev-parse", "--show-toplevel")))
            except (OSError, ValueError):
                continue
        if len(roots) > 1:
            raise ValueError("director launches disagree on the working folder: " + ", ".join(map(str, sorted(roots))))
        if roots:
            return staff.PresentWorktree(machine, next(iter(roots)))
        # A native session can recover an existing configuration, but cannot create one from its current folder.
        current = {item.details.get("cwd", "") for item in self.row_sessions.get(row.id, [])} - {""}
        if not current:
            raise ValueError("no verified live session supplies its working folder")
        if len(current) != 1:
            raise ValueError("live sessions disagree on the working folder: " + ", ".join(sorted(current)))
        folder = Path(next(iter(current))).expanduser()
        if not folder.is_absolute():
            raise ValueError("live session working folder is not absolute")
        root = Path(_fill_git(folder, "rev-parse", "--show-toplevel"))
        self.dependencies(root / ".unit.toml")
        if row.kind == "unit" and isinstance(unit_config.read(root, cleanup=False), unit_config.ConfigMissing):
            raise ValueError("no verified director launch supplies its unit worktree")
        return staff.PresentWorktree(machine, root)

    def session_unit(self, row: staff.StaffRow, machine: str, source: str) -> _LiveUnitFill:
        location = self.session_worktree(row, machine)
        self.dependencies(location.path / ".unit.toml")
        return _LiveUnitFill(row, location, unit_config.read(location.path, cleanup=False), source)

    def unit_for(self, row: staff.StaffRow, document: FillDocument, machine: str) -> _LiveUnitFill | _RetiredUnitDeferred:
        parent = production_units.DocumentSource(machine, document.path)
        documents = tuple(production_units.DocumentSource(item.doc.machine, item.doc.path)
                          for item in self.record.find(kind="showrunner")
                          if not isinstance(item.doc, staff.DocUnfilled))
        selected = production_units.lookup_unit(row.name, parent, documents, self.load_document,
                                                aliases=self.director_names.get(row.id, ()))
        if isinstance(selected, production_units.UnitRowMissing):
            return self.session_unit(row, machine, str(document.path))
        if not isinstance(selected, production_units.UnitRowFound):
            raise ValueError(selected.reason)
        cells, line = selected.cells, selected.line
        if selected.note:
            print(f"note {row.id} {row.name}: {selected.note}")
        if production_units.plan_cell_is_retired(cells.get("Plan", "")):
            return _RetiredUnitDeferred()
        try:
            location = _fill_worktree(row, cells, selected.source.machine)
        except (OSError, ValueError) as error:
            try:
                location = self.session_worktree(row, machine)
            except (OSError, ValueError) as session_error:
                raise ValueError(f"{document.path}:{line}: {error}; {session_error}") from session_error
        try:
            config = _fill_config(cells)
        except ValueError as error:
            config = unit_config.ConfigMissing(str(error))
        return _LiveUnitFill(row, location, config, f"{selected.source.path}:{line}")


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
        except (OSError, UnicodeError, ValueError, production_units.Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
            doc_errors[row.id] = str(error)
            if isinstance(row.doc, staff.DocUnfilled):
                scan.problem(row.id, row.name, error)
    for row in record.find(kind="unit"):
        if isinstance(row.worktree, staff.RemovedWorktree):
            continue
        # Existing files supply their values. The writer still checks their formatted bytes.
        unfilled = isinstance(row.worktree, staff.WorktreeUnfilled)
        if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.machine != machine:
            continue
        if (isinstance(row.parent, staff.ReportsTo) and row.parent.id not in documents
                and row.parent.id not in doc_errors and row.id not in scan.row_sessions):
            continue
        try:
            if isinstance(row.worktree, staff.PresentWorktree):
                dependencies(row.worktree.path / ".unit.toml")
                current = unit_config.read(row.worktree.path, cleanup=False)
                if isinstance(current, unit_config.UnitConfig):
                    if dry_run:
                        changed |= unit_config.needs_write(row.worktree.path, current)
                    else:
                        publication = unit_config.write(row.worktree.path, current)
                        changed |= isinstance(publication, unit_config.Published)
                    continue
            document = documents.get(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
            if document is None:
                reason = (doc_errors.get(row.parent.id, "parent production document is unavailable")
                          if isinstance(row.parent, staff.ReportsTo) else "unit has no parent production document")
                raise ValueError(reason)
            candidate = scan.unit_for(row, document, machine)
            if isinstance(candidate, _RetiredUnitDeferred):
                continue
            if not unfilled and row.worktree != candidate.worktree:
                raise ValueError("configuration recovery evidence conflicts with the recorded worktree")
            dependencies(candidate.worktree.path / ".unit.toml")
            git_exclude.require_untracked_ignored(candidate.worktree.path, ".unit.toml",
                                                 planned_pattern="/.unit.toml")
            if isinstance(candidate.config, unit_config.ConfigMissing):
                if unfilled:
                    if not dry_run:
                        _ = staff.set_worktree(row.id, candidate.worktree, expected=row.worktree)
                    changed = True
                    print(f"configuration {row.id} {row.name}: {candidate.config.reason}")
                else:
                    raise ValueError(candidate.config.reason)
                continue
            current = unit_config.read(candidate.worktree.path, cleanup=False)
            if not isinstance(current, unit_config.ConfigMissing) and current != candidate.config:
                raise ValueError("existing .unit.toml conflicts with the owning document")
            if dry_run:
                changed |= unit_config.needs_write(candidate.worktree.path, candidate.config)
            else:
                git_exclude.ensure_excluded(candidate.worktree.path, "/.unit.toml")
                if unfilled:
                    _ = staff.set_worktree(row.id, candidate.worktree, expected=row.worktree)
                publication = unit_config.write(candidate.worktree.path, candidate.config)
                changed |= isinstance(publication, unit_config.Published)
            changed |= unfilled or isinstance(current, unit_config.ConfigMissing)
        except (OSError, UnicodeError, ValueError, production_units.Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
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


def fill(*, dry_run: bool = False, retired: bool = False) -> int:
    """Fill from current staff rows; retirement keeps its historical source scan."""
    with nullcontext() if dry_run else staff.operation():
        record = staff.snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        sources = _member_sources(dependencies=dependencies)
        if not retired:
            return _fill_live(record, sources, dependencies, dry_run=dry_run)
        scan = _FillSources(record, sources, dependencies)
        documents = scan.collect()
        productions = dict(sources.productions)
        for document in documents:
            source = productions.get(document.slug)
            timer = (source.timer._replace(doc=document.path) if source is not None else
                     showrunners.Timer(document.slug, "session:unrecorded", document.path, ""))
            productions[document.slug] = showrunners.ShowrunnerSource(timer, document.text)
        joined = _MemberSources(productions, sources.launches)
        return _fill_retired(scan, documents, joined, _fill_machine(), dry_run=dry_run)


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False
    retired: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=["seed", "fill"])
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--retired", action="store_true")
    arguments = _Arguments()
    _ = parser.parse_args(argv, namespace=arguments)
    if arguments.retired and arguments.command != "fill":
        parser.error("--retired requires fill")

    def run() -> int:
        if arguments.command == "fill":
            return fill(dry_run=arguments.dry_run, retired=arguments.retired)
        seed(dry_run=arguments.dry_run)
        return 0

    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main())
