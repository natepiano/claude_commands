"""Check launch permission, then publish the backend runtime identity."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import io
import hashlib
import ast
from importlib import import_module
import json
import re
import sys
from pathlib import Path
from typing import cast

from scripts.agents import worker_lifecycle
from scripts.lib import unit_runs
from scripts.production import staff, session_staff
from scripts.shutdown.stop_results import Stopped, NotRunning, StopFailed, SessionNotFound


class AdmissionRefused(ValueError):
    """Permission or native publication failed; no work may be dispatched."""


@dataclass(frozen=True)
class NewWorkerSlot:
    """The slot has no staff row yet."""


@dataclass(frozen=True)
class ExistingWorkerSlot:
    row: staff.StaffRow


@dataclass(frozen=True)
class NoFamilyHandoff:
    """The destination does not need a transcript handoff."""


@dataclass(frozen=True)
class TranscriptHandoff:
    extract: Path
    source: Path

    def opening(self, assignment: str = "") -> str:
        return (f"Read the transcript extract: {self.extract}\n"
                + f"Full source transcript: {self.source}\n"
                + "Read the full transcript only when the extract does not answer your question.\n" + assignment)


@dataclass(frozen=True)
class TranscriptUnavailable:
    reason: str


FamilyHandoff = NoFamilyHandoff | TranscriptHandoff | TranscriptUnavailable


@dataclass(frozen=True)
class WorkerStartAllowed:
    """Parent, worker name and role passed the read-only start checks."""

    name: str
    parent: staff.StaffId
    run: Path
    slot: NewWorkerSlot | ExistingWorkerSlot

    @property
    def lock_reference(self) -> staff.StaffId:
        return self.slot.row.id if isinstance(self.slot, ExistingWorkerSlot) else self.parent


@dataclass(frozen=True)
class SourceReleased:
    """Every applicable source server released the native session."""


@dataclass(frozen=True)
class SourceUnresolved:
    reason: str


@dataclass(frozen=True)
class SourceReleaseUnknown:
    reason: str


SourceRelease = SourceReleased | SourceUnresolved | SourceReleaseUnknown


class SwitchState(StrEnum):
    BACKEND_PUBLISHED = "prepared"
    DISPATCH_DEFERRED = "sending"
    ASSIGNMENT_ACCEPTED = "accepted"


@dataclass(frozen=True)
class DispatchRecord:
    assignment_id: str
    state: SwitchState


@dataclass(frozen=True)
class PublishedWorker:
    """The backend's native identity and membership are durably published."""

    row: staff.StaffRow
    handoff: FamilyHandoff = field(default_factory=NoFamilyHandoff)
    state: SwitchState = SwitchState.BACKEND_PUBLISHED


def parent(record: staff.StaffSnapshot, sources: session_staff.MemberSources | None = None, *,
           staff_id: str = "", production: str = "", unit: str = "",
           run: Path | None = None, roster: Mapping[str, object] | None = None) -> staff.StaffRow:
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
        entries = roster if roster is not None else session_staff.read_worker_roster(run / "mesh_roster.json")
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
    found = sources.member(session_staff.ShowrunnerMember(production), record)
    if isinstance(found, session_staff.NoSuchMember):
        raise ValueError(f"no unit staff row for {unit}" if unit else f"no showrunner staff row for {production}")
    return found.row


def find_worker(record: staff.StaffSnapshot, owner: staff.StaffId, name: str) -> staff.StaffRow | None:
    """Find the named worker under its staff parent."""
    return next((row for row in record.find(kind="worker", parent=owner) if row.name == name), None)


def resolve_roster(run: Path, entries: dict[str, object]) -> dict[str, object]:
    """Add staff references to old entries through the run's unit and worker name."""
    missing = [(name, cast(dict[str, object], raw)) for name, raw in entries.items()
               if isinstance(raw, dict) and "staff_id" not in raw]
    if not missing:
        return entries
    record = staff.local_snapshot()
    try:
        owner = parent(record, staff_id=staff.runtime_id(), run=run, roster=entries)
    except ValueError:
        return entries
    for name, entry in missing:
        row = find_worker(record, owner.id, name)
        if row is not None:
            entry["staff_id"] = row.id
    return entries


def admit(name: str, run: Path, *, role: str, production: str = "", unit: str = "", reference: str = "") -> WorkerStartAllowed:
    """Refuse before starting a backend or taking lifecycle, inbox or roster locks."""
    try:
        del role
        record = staff.snapshot()
        if reference:
            existing = record.by_id(staff.StaffId(reference))
            if existing is None or existing.kind != "worker" or not isinstance(existing.parent, staff.ReportsTo):
                raise AdmissionRefused(f"{reference}: no worker staff row")
            return WorkerStartAllowed(existing.name, existing.parent.id, run, ExistingWorkerSlot(existing))
        explicit = bool(production or unit)
        owner = parent(record, staff_id="" if explicit else staff.runtime_id(),
                       production=production, unit=unit, run=None if explicit else run)
        _ = staff.WorkerName.parse(name)
        if isinstance(owner.worktree, staff.RemovedWorktree):
            print(f"staff: {name}: parent {owner.id} is retired; admission continues", file=sys.stderr)
        existing = find_worker(record, owner.id, name)
        return WorkerStartAllowed(name, owner.id, run, ExistingWorkerSlot(existing) if existing else NewWorkerSlot())
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {name}: not opened: {error}".replace("\n", " ")) from error


def member(run: Path, name: str) -> staff.StaffRow | None:
    record = staff.local_snapshot()
    owner = parent(record, staff_id=staff.runtime_id(), run=run)
    return find_worker(record, owner.id, name)


def transcript_source(row: staff.StaffRow, backend: staff.ClaudeBackend | staff.CodexBackend) -> Path:
    if isinstance(backend, staff.CodexBackend):
        paths = [path for folder in ("sessions", "archived_sessions")
                 for path in (backend.codex_home / folder).rglob(f"*{row.current_session}.jsonl")]
    else:
        from scripts.message import sessions
        paths = sessions.transcript_locations(row.current_session)
    if not paths:
        raise ValueError("no source transcript found")
    return max(paths, key=lambda path: path.stat().st_mtime)


def transcript_entry(fields: dict[str, object], changed: set[str]) -> str:
    raw = fields.get("message", fields.get("payload", {}))
    if not isinstance(raw, dict):
        return ""
    message = cast(dict[str, object], raw)
    role = message.get("role", fields.get("type"))
    if message.get("type") in ("user_message", "agent_message"):
        role = "user" if message["type"] == "user_message" else "assistant"
    content = message.get("content", message.get("message", message.get("text", "")))
    parts = cast(list[object], content) if isinstance(content, list) else [content]
    text: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text.append(part)
        elif isinstance(part, dict):
            block = cast(dict[str, object], part)
            if block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
                text.append(str(block["text"]))
            _changed_files(block, changed)
    _changed_files(message, changed)
    final = message.get("stop_reason") not in ("tool_use", "max_tokens") and message.get("phase") != "commentary"
    return f"{role}:\n" + "\n".join(text) if role in ("user", "assistant") and text and (role == "user" or final) else ""


def extract_transcript(row: staff.StaffRow, run: Path, backend: staff.ClaudeBackend | staff.CodexBackend) -> FamilyHandoff:
    """Extract assignments, final replies, and changed paths without tool bodies."""
    messages: list[str] = []
    changed: set[str] = set()
    malformed = False
    try:
        source = transcript_source(row, backend)
        with source.open(encoding="utf-8") as transcript:
            for line in transcript:
                try:
                    fields = cast(object, json.loads(line))
                except ValueError:
                    malformed = True
                    continue
                if isinstance(fields, dict):
                    entry = transcript_entry(cast(dict[str, object], fields), changed)
                    if entry and (not messages or messages[-1] != entry):
                        messages.append(entry)
        if not messages:
            raise ValueError("source transcript has no assignments or final replies")
        run.mkdir(parents=True, exist_ok=True)
        extract = run / f"{row.id}.handoff.md"
        from scripts.lib.atomic_file import write_text_atomic
        write_text_atomic(extract, f"Full source transcript: {source}\n\n" + "\n\n".join(messages) + "\n\nChanged files:\n" + "\n".join(sorted(changed)) + "\n")
        if malformed:
            print(f"anomaly: {row.name}: malformed transcript record", file=sys.stderr)
        return TranscriptHandoff(extract, source)
    except (OSError, ValueError) as error:
        reason = str(error).replace("\n", " ")
        print(f"anomaly: {row.name}: transcript extract: {reason}", file=sys.stderr)
        return TranscriptUnavailable(reason)


def _changed_files(message: dict[str, object], changed: set[str]) -> None:
    from scripts.direct.phase_reservation import patch_paths
    from scripts.usage import rollouts
    arguments = message.get("input", message.get("arguments", {}))
    if message.get("name") in ("Edit", "Write", "MultiEdit") and isinstance(arguments, dict):
        path = cast(dict[str, object], arguments).get("file_path")
        if isinstance(path, str):
            changed.add(path)
    if not isinstance(arguments, str):
        return
    if message.get("name") in ("apply_patch", "functions.apply_patch"):
        changed.update(patch_paths(arguments))
    elif rollouts.is_edit({"type": "response_item", "payload": {"type": "custom_tool_call", "input": arguments}}):
        visible = rollouts.javascript_without_string_literals(arguments)
        for call in re.finditer(r"\b(?:tools\.)?apply_patch\s*\(", visible):
            literal = re.match(r'''\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)''', arguments[call.end():], re.DOTALL)
            if literal:
                value = literal.group(1)
                patch = value[1:-1].replace("\\n", "\n") if value.startswith("`") else ast.literal_eval(value)
                changed.update(patch_paths(patch))


def assignment_id(prompt: Path) -> str:
    """Identify an existing assignment artifact across launcher retries."""
    return hashlib.sha256(str(prompt.resolve()).encode() + str(prompt.stat().st_mtime_ns).encode() + prompt.read_bytes()).hexdigest()


def dispatch_record(allowed: WorkerStartAllowed) -> DispatchRecord:
    entry = session_staff.read_worker_roster(allowed.run / "mesh_roster.json").get(allowed.name)
    fields = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
    try:
        return DispatchRecord(str(fields.get("assignment_id", "")),
                              SwitchState(fields.get("dispatch_state", "prepared")))
    except ValueError as error:
        raise AdmissionRefused(f"{allowed.name}: invalid dispatch state") from error


def record_dispatch(allowed: WorkerStartAllowed, request: str, state: SwitchState) -> None:
    with worker_lifecycle.locked_state(allowed.run / "mesh_roster.json") as handle:
        entries = handle.read_object()
        previous = entries.get(allowed.name, {})
        entry = dict(cast(dict[str, object], previous)) if isinstance(previous, dict) else {}
        entry.update(assignment_id=request, dispatch_state=state)
        if isinstance(allowed.slot, ExistingWorkerSlot):
            entry["staff_id"] = allowed.slot.row.id
        entries[allowed.name] = entry
        session_staff.publish_worker_roster(handle, entries)


def release_source(row: staff.StaffRow, runs: Callable[[], tuple[Path, ...]], record: staff.StaffSnapshot) -> SourceRelease:
    """Resolve native stop evidence before creating the destination."""
    stop = cast(Callable[[str], Stopped | NotRunning | StopFailed | SessionNotFound],
                import_module("scripts.shutdown.stop_resume").stop)
    result = stop(row.id)
    if isinstance(result, SessionNotFound):
        return SourceUnresolved(f"{row.id}: lifecycle could not resolve the staff reference")
    if isinstance(result, StopFailed):
        return SourceReleaseUnknown(result.reason)
    if isinstance(result, NotRunning) and isinstance(record.backends.get(row.id), staff.CodexBackend):
        stop_mesh = cast(Callable[[staff.StaffRow, tuple[Path, ...]], SourceRelease],
                         import_module("scripts.agents.codex_mesh").stop_worker_for_switch)
        return stop_mesh(row, runs())
    return SourceReleased()


@contextmanager
def worker_operation(allowed: WorkerStartAllowed) -> Generator[None]:
    """Lock the existing row or the new slot before checking membership."""
    try:
        with ExitStack() as locks:
            if isinstance(allowed.slot, NewWorkerSlot):
                key = hashlib.sha256(f"{allowed.parent}:{allowed.name}".encode()).hexdigest()
                _ = locks.enter_context(staff.slot_operation(key, allowed.name))
                row = find_worker(staff.local_snapshot(), allowed.parent, allowed.name)
            else:
                row = allowed.slot.row
            if row is not None:
                _ = locks.enter_context(staff.row_operation(row.id))
            yield
    except staff.StaffRefused as error:
        raise AdmissionRefused(str(error)) from error


def switch_worker(allowed: WorkerStartAllowed, family: str, *,
                  start: Callable[[FamilyHandoff], str],
                  dispatch: Callable[[PublishedWorker], SwitchState] | SwitchState | None,
                  cleanup: Callable[[str], None], request: str = "") -> PublishedWorker:
    """Hold the row through publication and dispatch acceptance, then release it."""
    with worker_operation(allowed):
        handoff: FamilyHandoff = NoFamilyHandoff()
        row = allowed.slot.row if isinstance(allowed.slot, ExistingWorkerSlot) else None
        record = staff.local_snapshot()
        row = record.by_id(row.id) if row is not None else find_worker(record, allowed.parent, allowed.name)
        if row is not None:
            allowed = WorkerStartAllowed(row.name, allowed.parent, allowed.run, ExistingWorkerSlot(row))
        if isinstance(allowed.slot, ExistingWorkerSlot) and row is None:
            raise AdmissionRefused(f"{allowed.slot.row.id}: source staff row is missing")
        backend = record.backends.get(row.id) if row is not None else None
        same = row is not None and session_staff.worker_family(record, row.id) == family
        prior = dispatch_record(allowed)
        request = request or (hashlib.sha256(f"{allowed.slot.row.id}:{family}".encode()).hexdigest()
                              if isinstance(allowed.slot, ExistingWorkerSlot) else "")
        retry = bool(request and prior.assignment_id == request)
        if same and retry:
            assert row is not None
            published = PublishedWorker(row)
            if prior.state is SwitchState.ASSIGNMENT_ACCEPTED:
                print("unchanged")
                return PublishedWorker(row, state=prior.state)
            extract = allowed.run / f"{row.id}.handoff.md"
            try:
                with extract.open(encoding="utf-8") as saved:
                    source = Path(saved.readline().removeprefix("Full source transcript: ").strip())
                published = PublishedWorker(row, TranscriptHandoff(extract, source))
            except OSError as error:
                print(f"anomaly: {row.name}: transcript extract: {error}".replace("\n", " "), file=sys.stderr)
                published = PublishedWorker(row, TranscriptUnavailable(str(error)))
        else:
            if row is not None and not same:
                allowed = WorkerStartAllowed(row.name, allowed.parent, allowed.run, ExistingWorkerSlot(row))
                handoff = extract_transcript(row, allowed.run, backend) if backend is not None else TranscriptUnavailable("backend unavailable")
                if backend is None:
                    print(f"anomaly: {row.name}: transcript extract: backend unavailable", file=sys.stderr)
                released = release_source(row, lambda: (allowed.run, *session_staff.worker_run_index(record).get(allowed.lock_reference, ())), record)
                if not isinstance(released, SourceReleased):
                    raise AdmissionRefused(released.reason)
                retry = True
            if request and retry and prior.assignment_id != request:
                record_dispatch(allowed, request, SwitchState.BACKEND_PUBLISHED)
                prior = DispatchRecord(request, SwitchState.BACKEND_PUBLISHED)
            identity = start(handoff)
            try:
                published = publish(allowed, identity)
            except BaseException:
                cleanup(identity)
                raise
            published = PublishedWorker(published.row, handoff)
        state = prior.state if dispatch is None else dispatch(published) if callable(dispatch) else dispatch
        if request and retry:
            record_dispatch(allowed, request, state)
        return PublishedWorker(published.row, published.handoff, state)


def publish(allowed: WorkerStartAllowed, runtime_id: str) -> PublishedWorker:
    """Publish the session before dispatch."""
    try:
        identity = staff.native_id(runtime_id)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if isinstance(allowed.slot, ExistingWorkerSlot):
                row = staff.update_current_session(allowed.slot.row.current_session, identity)
            else:
                row = staff.ensure_worker(identity, allowed.name, allowed.parent)
        return PublishedWorker(row)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {allowed.name}: {runtime_id}: publication refused: {error}".replace("\n", " ")) from error
