"""Check launch permission, then publish the backend runtime identity."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import sys
from pathlib import Path
from typing import cast

from scripts.agents import worker_lifecycle
from scripts.lib import unit_runs
from scripts.production import staff, session_staff


class AdmissionRefused(ValueError):
    """Permission or native publication failed; no work may be dispatched."""


@dataclass(frozen=True)
class WorkerStartAllowed:
    """Parent, worker name and role passed the read-only start checks."""

    name: str
    parent: staff.StaffId
    run: Path
    member: staff.StaffRow | None


@dataclass(frozen=True)
class PublishedWorker:
    """The backend's native identity and membership are durably published."""

    row: staff.StaffRow


def showrunner_member(record: staff.StaffSnapshot, sources: session_staff.MemberSources,
                      slug: str
                      ) -> session_staff.MemberFound | session_staff.NoSuchMember:
    """Find the showrunner in staff."""
    selector = session_staff.ShowrunnerMember(slug)
    return sources.member(selector, record)


def parent(record: staff.StaffSnapshot, sources: session_staff.MemberSources | None = None, *,
           staff_id: str = "", production: str = "", unit: str = "",
           run: Path | None = None) -> staff.StaffRow:
    """Resolve the run unit before using the caller or worker parents."""
    if run is not None:
        try:
            state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
        except (OSError, ValueError) as error:
            print(f"anomaly: {error}".replace("\n", " "), file=sys.stderr)
            state = {}
        production = value if isinstance(value := state.get("production"), str) else ""
        unit = value if isinstance(value := state.get("unit"), str) else ""
    if unit:
        found = session_staff.unit_match(session_staff.ObservedUnit(unit), record)
        if isinstance(found, session_staff.MemberFound):
            return found.row
        if production:
            sources = sources if sources is not None else session_staff.member_sources()
            found = sources.member(session_staff.UnitMember(production, unit), record)
            if isinstance(found, session_staff.MemberFound):
                return found.row
    if run is not None:
        entries = session_staff.read_worker_roster(run / "mesh_roster.json")
        for raw in entries.values():
            if not isinstance(raw, dict):
                continue
            child = record.by_id(staff.StaffId(str(cast(dict[str, object], raw).get("staff_id", ""))))
            owner = record.parent_of(child) if child is not None else None
            if owner is not None:
                return owner
    row = record.by_current_session(staff_id)
    if row is not None and row.kind == "worker":
        row = record.parent_of(row)
    if row is not None and row.kind in ("unit", "showrunner"):
        return row
    sources = sources if sources is not None else session_staff.member_sources()
    found = showrunner_member(record, sources, production)
    if isinstance(found, session_staff.NoSuchMember):
        raise ValueError(f"no unit staff row for {unit}" if unit else f"no showrunner staff row for {production}")
    return found.row


def find_worker(record: staff.StaffSnapshot, owner: staff.StaffId, name: str) -> staff.StaffRow | None:
    """Find the named worker under its staff parent."""
    return next((row for row in record.find(kind="worker", parent=owner) if row.name == name), None)


def admit(name: str, run: Path, *, role: str, production: str = "", unit: str = "") -> WorkerStartAllowed:
    """Refuse before starting a backend or taking lifecycle, inbox or roster locks."""
    try:
        del role
        record = staff.snapshot()
        explicit = bool(production or unit)
        owner = parent(record, staff_id="" if explicit else staff.runtime_id(),
                       production=production, unit=unit, run=None if explicit else run)
        _ = staff.WorkerName.parse(name)
        if isinstance(owner.worktree, staff.RemovedWorktree):
            print(f"staff: {name}: parent {owner.id} is retired; admission continues", file=sys.stderr)
        existing = find_worker(record, owner.id, name)
        return WorkerStartAllowed(name, owner.id, run, existing)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {name}: not opened: {error}".replace("\n", " ")) from error


def member(run: Path, name: str) -> staff.StaffRow | None:
    record = staff.local_snapshot()
    owner = parent(record, staff_id=staff.runtime_id(), run=run)
    return find_worker(record, owner.id, name)


def family_switch_context(allowed: WorkerStartAllowed, family: str) -> str:
    """Read the prior conversation when the new session uses another family."""
    row = allowed.member
    if row is None:
        return ""
    backend = staff.local_snapshot().backends.get(row.id)
    paths: list[Path] = []
    if isinstance(backend, staff.CodexBackend) and family == "claude":
        for folder in ("sessions", "archived_sessions"):
            paths.extend((backend.codex_home / folder).rglob(f"*{row.current_session}.jsonl"))
    elif isinstance(backend, staff.ClaudeBackend) and family == "codex":
        from scripts.message import sessions
        paths = [path for home in sessions.config_dirs()
                 if (path := sessions.latest_transcript(row.current_session, home / "projects")) is not None]
    messages: list[str] = []
    try:
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    continue
                fields = cast(dict[str, object], value)
                raw = fields.get("message", fields.get("payload", {}))
                if not isinstance(raw, dict):
                    continue
                message = cast(dict[str, object], raw)
                if message.get("role", fields.get("type")) in ("user", "assistant"):
                    messages.append(json.dumps(message.get("content", message.get("text", "")), ensure_ascii=False))
    except (OSError, ValueError) as error:
        print(f"anomaly: {row.name}: transcript extract: {error}", file=sys.stderr)
    return "Prior conversation:\n" + "\n".join(messages)[-32000:] if messages else ""


def publish(allowed: WorkerStartAllowed, runtime_id: str) -> PublishedWorker:
    """Publish the session before dispatch."""
    try:
        identity = staff.native_id(runtime_id)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if allowed.member is not None:
                row = staff.update_current_session(allowed.member.current_session, identity)
            else:
                row = staff.ensure_worker(identity, allowed.name, allowed.parent)
        return PublishedWorker(row)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {allowed.name}: {runtime_id}: publication refused: {error}".replace("\n", " ")) from error
