"""Publish worker membership before any process, thread or lifecycle lock."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import os
from pathlib import Path

from scripts.agents import worker_lifecycle
from scripts.lib import unit_runs
from scripts.production import staff, session_staff, showrunners


class AdmissionRefused(ValueError):
    """No worker may start until its staff membership is durable."""


@dataclass(frozen=True)
class WorkerAdmitted:
    """Staff membership was published before any worker process can start."""

    row: staff.StaffRow


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
    """Use a surviving owner id, otherwise the unique unit across productions."""
    row = record.by_id(staff.StaffId(staff_id)) if staff_id else None
    if row is not None and row.kind in ("unit", "showrunner"):
        return row
    if run is not None:
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


def admit(name: str, run: Path, *, production: str = "", unit: str = "") -> WorkerAdmitted:
    """Write the row before acquiring lifecycle, inbox or roster locks."""
    try:
        record = staff.snapshot()
        explicit = bool(production or unit)
        owner = parent(record, staff_id="" if explicit else os.environ.get("STAFF_ID", ""),
                       production=production, unit=unit, run=None if explicit else run)
        assert isinstance(owner, staff.StaffRow)
        if isinstance(owner.worktree, staff.RemovedWorktree) or isinstance(owner.doc, staff.WrappedDoc):
            raise ValueError(f"parent {owner.id} is retired")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            row = staff.ensure_worker(name, owner.id)
        return WorkerAdmitted(row)
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {name}: not opened: {error}".replace("\n", " ")) from error
