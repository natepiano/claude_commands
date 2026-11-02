"""Check launch permission, then publish the backend runtime identity."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
from pathlib import Path
from typing import cast

from scripts.agents import worker_lifecycle
from scripts.lib import unit_runs
from scripts.production import staff, session_staff, showrunners


class AdmissionRefused(ValueError):
    """Permission or native publication failed; no work may be dispatched."""


@dataclass(frozen=True)
class WorkerStartAllowed:
    """Parent, worker name and role passed the read-only start checks."""

    name: str
    parent: staff.StaffId
    run: Path
    role: str


@dataclass(frozen=True)
class PublishedWorker:
    """The backend's native identity and membership are durably published."""

    row: staff.StaffRow


@dataclass(frozen=True)
class UnpublishedWorker:
    """A returned native backend retained in its existing inbox, before success."""

    inbox: Path
    identity: staff.StaffId
    parent: staff.StaffId


def pending_publications(run: Path) -> tuple[UnpublishedWorker, ...]:
    pending: list[UnpublishedWorker] = []
    for path in sorted(run.glob("*.pending.json")):
        record = worker_lifecycle.validate_worker_state(path, worker_lifecycle.read_json_object(path))
        raw = record.get("unpublished_thread")
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise staff.StaffUnreadable(f"{path}: invalid unfinished backend evidence")
        fields = cast(dict[str, object], raw)
        native, parent_id = fields.get("id"), fields.get("parent")
        if not isinstance(native, str) or not isinstance(parent_id, str):
            raise staff.StaffUnreadable(f"{path}: unfinished backend needs native identity and parent")
        pending.append(UnpublishedWorker(path, staff.native_id(native), staff.native_id(parent_id)))
    return tuple(pending)


def showrunner_member(record: staff.StaffSnapshot, sources: session_staff.MemberSources,
                      slug: str, *, dependencies: Callable[[Path], None] | None = None
                      ) -> session_staff.MemberFound | session_staff.NoSuchMember:
    """Resolve a holder's former slug only through its recorded rename journal."""
    selector = session_staff.ShowrunnerMember(slug)
    found = sources.member(selector, record)
    if isinstance(found, session_staff.MemberFound):
        return found
    matches: dict[staff.StaffId, staff.StaffRow] = {}
    for path in sorted(showrunners.SHOWRUNNER_STATE_DIR.glob("*/rename.json")):
        if dependencies is not None:
            dependencies(path)
        try:
            journal = worker_lifecycle.read_json_object(path)
            old, new = journal.get("old"), journal.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str) or not new:
                raise ValueError("rename journal needs old and new slugs")
        except (OSError, ValueError) as error:
            raise staff.StaffUnreadable(f"{path}: {error}") from error
        source = sources.productions.get(new)
        former = showrunners.former_slugs(source.document) if source is not None else ()
        # Formerly is supporting evidence, never a replacement for the journal.
        if slug != old or (former and old not in former):
            continue
        current = sources.member(session_staff.ShowrunnerMember(new), record)
        if isinstance(current, session_staff.MemberFound):
            matches[current.row.id] = current.row
    if len(matches) > 1:
        raise staff.StaffUnreadable(f"{slug}: no unique renamed showrunner staff entry")
    return session_staff.MemberFound(next(iter(matches.values()))) if matches else found


def parent(record: staff.StaffSnapshot, sources: session_staff.MemberSources | None = None, *,
           staff_id: str = "", production: str = "", unit: str = "",
           pending: tuple[staff.SeedMember, ...] = (), run: Path | None = None) -> staff.StaffRow | staff.SeedMember:
    """Resolve the caller or recorded director before using production names."""
    row = record.by_current_session(staff_id) if staff_id else None
    if row is not None and row.kind == "worker" and row.parent is not None:
        row = record.by_id(row.parent)
    if row is not None and row.kind in ("unit", "showrunner"):
        return row
    if run is not None:
        director = unit_runs.director(run)
        row = record.by_member_reference(director) if director else None
        if row is not None and row.kind in ("unit", "showrunner"):
            return row
        state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
        production = value if isinstance(value := state.get("production"), str) else ""
        unit = value if isinstance(value := state.get("unit"), str) else ""
    if unit:
        found = session_staff.unit_match(session_staff.ObservedUnit(unit), record)
    else:
        sources = sources if sources is not None else session_staff.member_sources()
        found = showrunner_member(record, sources, production)
    if isinstance(found, session_staff.NoSuchMember):
        if unit:
            from scripts.production import unit_names
            candidates = [entry for entry in pending if entry.kind == "unit"
                          and entry.name == unit_names.name(unit)]
            if len(candidates) > 1:
                raise staff.StaffUnreadable(f"{unit}: no unique staff entry")
            if candidates:
                return candidates[0]
        raise ValueError(f"no unit staff row for {unit}" if unit else f"no showrunner staff row for {production}")
    return found.row


def admit(name: str, run: Path, *, role: str, production: str = "", unit: str = "") -> WorkerStartAllowed:
    """Refuse before starting a backend or taking lifecycle, inbox or roster locks."""
    try:
        if role in ("designer", "reviewer"):
            role = "astra-" + role
        if role not in ("impl", "test", "review", "fix", "astra-designer", "astra-reviewer"):
            raise ValueError(f"invalid worker role: {role!r}")
        record = staff.snapshot()
        explicit = bool(production or unit)
        owner = parent(record, staff_id="" if explicit else staff.runtime_id(),
                       production=production, unit=unit, run=None if explicit else run)
        assert isinstance(owner, staff.StaffRow)
        if isinstance(owner.worktree, staff.RemovedWorktree) or isinstance(owner.doc, staff.WrappedDoc):
            raise ValueError(f"parent {owner.id} is retired")
        _ = staff.WorkerName.parse(name)
        return WorkerStartAllowed(name, owner.id, run, role)
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {name}: not opened: {error}".replace("\n", " ")) from error


def publish(allowed: WorkerStartAllowed, runtime_id: str, *, previous_id: str = "") -> PublishedWorker:
    """Publish a returned backend id before dispatch or roster success."""
    try:
        identity = staff.native_id(runtime_id)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if previous_id and previous_id != runtime_id:
                old = staff.by_current_session(previous_id)
                if old is None:
                    replay = staff.by_current_session(identity)
                    if replay is None:
                        # An interrupted backend may never have published its first row.
                        return PublishedWorker(staff.ensure_worker(allowed.name, allowed.parent,
                                                                   runtime_id=runtime_id))
                    if (replay.kind, replay.name, replay.parent) != ("worker", allowed.name, allowed.parent):
                        raise staff.StaffRefused(f"{previous_id}: replacement owner is not proved")
                    return PublishedWorker(replay)
                if (old.kind, old.name, old.parent) != ("worker", allowed.name, allowed.parent):
                    raise staff.StaffRefused(f"{previous_id}: replacement owner is not proved")
                row = staff.update_current_session(old.current_session, identity)
            else:
                row = staff.ensure_worker(allowed.name, allowed.parent, runtime_id=runtime_id)
        return PublishedWorker(row)
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {allowed.name}: {runtime_id}: publication refused: {error}".replace("\n", " ")) from error
