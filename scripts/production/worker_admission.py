"""Check launch permission, then publish the backend runtime identity."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator
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
from scripts.shutdown.stop_results import ReleaseSource, SourceReleased


class AdmissionRefused(ValueError):
    """Permission or native publication failed; no work may be dispatched."""


class RunOwnerUnavailable(ValueError):
    """The run records identify no staff owner."""


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
                + f"{HANDOFF_SOURCE_PREFIX}{self.source}\n"
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


class SwitchState(StrEnum):
    NO_RECORD = ""
    CREATION_INTENT_RECORDED = "prepared"
    DISPATCH_IN_FLIGHT = "sending"
    ASSIGNMENT_ACCEPTED = "accepted"


class DispatchOwner(StrEnum):
    TURN_SUBMISSION = "turn_submission"


@dataclass(frozen=True)
class DispatchRecord:
    assignment_id: str
    state: SwitchState


@dataclass(frozen=True)
class PublishedWorker:
    """The backend's native identity and membership are durably published."""

    row: staff.StaffRow
    handoff: FamilyHandoff = field(default_factory=NoFamilyHandoff)


def parent(record: staff.StaffSnapshot, sources: session_staff.MemberSources | None = None, *,
           staff_id: str = "", production: str = "", unit: str = "",
           run: Path | None = None) -> staff.StaffRow:
    """Resolve the recorded run owner before the caller."""
    if run is not None:
        try:
            state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
        except (OSError, ValueError) as error:
            unit_runs.report_anomaly(f"anomaly: {error}".replace("\n", " "))
            state = {}
        owner = unit_runs.run_owner(state, record)
        if isinstance(owner, staff.StaffRow) and owner.kind in ("unit", "showrunner"):
            return owner
    if unit:
        found = session_staff.unit_match(session_staff.ObservedUnit(unit), record)
        if isinstance(found, session_staff.MemberFound):
            return found.row
        if production:
            sources = sources if sources is not None else session_staff.member_sources()
            found = sources.member(session_staff.UnitMember(production, unit), record)
            if isinstance(found, session_staff.MemberFound):
                return found.row
    row = record.owner_of(staff_id) if staff_id else None
    if row is not None and row.kind in ("unit", "showrunner"):
        return row
    if production:
        sources = sources if sources is not None else session_staff.member_sources()
        found = sources.member(session_staff.ShowrunnerMember(production), record)
        if isinstance(found, session_staff.MemberFound):
            return found.row
    error = RunOwnerUnavailable if run is not None else ValueError
    if unit or production:
        raise error(f"no unit staff row for {unit}" if unit else f"no showrunner staff row for {production}")
    raise error(f"this run has no owner because {unit_runs.STATE_FILE} names no staff row"
                if run is not None else "the caller has no staff row and names no unit or production")


def find_worker(record: staff.StaffSnapshot, owner: staff.StaffId, name: str) -> staff.StaffRow | None:
    """Find the named worker under its staff parent."""
    return next((row for row in record.find(kind="worker", parent=owner) if row.name == name), None)


def admit(name: str, run: Path, *, reference: str = "") -> WorkerStartAllowed:
    """Refuse before starting a backend or taking lifecycle, inbox or roster locks."""
    try:
        record = staff.snapshot()
        if reference:
            existing = record.by_id(staff.StaffId(reference))
            if existing is None or existing.kind != "worker" or not isinstance(existing.parent, staff.ReportsTo):
                raise AdmissionRefused(f"{reference}: no worker staff row")
            return WorkerStartAllowed(existing.name, existing.parent.id, run, ExistingWorkerSlot(existing))
        owner = parent(record, staff_id=staff.runtime_id(), run=run)
        _ = staff.WorkerName.parse(name)
        if isinstance(owner.worktree, staff.RemovedWorktree):
            print(f"staff: {name}: parent {owner.id} is retired; admission continues", file=sys.stderr)
        existing = find_worker(record, owner.id, name)
        return WorkerStartAllowed(name, owner.id, run, ExistingWorkerSlot(existing) if existing else NewWorkerSlot())
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {name}: not opened: {error}".replace("\n", " ")) from error


def member(run: Path, name: str, record: staff.StaffSnapshot | None = None) -> staff.StaffRow | None:
    record = record if record is not None else staff.local_snapshot()
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


HANDOFF_SOURCE_PREFIX = "Full source transcript: "


@dataclass(frozen=True)
class TornTranscriptRecord:
    """A transcript line is not complete JSON."""


def transcript_entries(source: Path, changed: set[str]) -> Generator[str | TornTranscriptRecord]:
    with source.open(encoding="utf-8") as transcript:
        for line in transcript:
            try:
                fields = cast(object, json.loads(line))
            except json.JSONDecodeError:
                yield TornTranscriptRecord()
                continue
            if isinstance(fields, dict):
                yield transcript_entry(cast(dict[str, object], fields), changed)


def conversation_entries(row: staff.StaffRow, backend: staff.ClaudeBackend | staff.CodexBackend,
                         changed: set[str]) -> Generator[str | TornTranscriptRecord]:
    """Read a conversation; Claude has no entries before its first transcript."""
    try:
        source = transcript_source(row, backend)
    except ValueError as error:
        if isinstance(backend, staff.ClaudeBackend) and str(error) == "no source transcript found":
            return
        raise
    yield from transcript_entries(source, changed)


def handoff_path(run: Path, row: staff.StaffRow) -> Path:
    return run / f"{row.id}.handoff.md"


def extract_transcript(row: staff.StaffRow, run: Path, backend: staff.ClaudeBackend | staff.CodexBackend | staff.BackendUnavailable) -> FamilyHandoff:
    """Extract assignments, final replies, and changed paths without tool bodies."""
    messages: list[str] = []
    changed: set[str] = set()
    malformed = False
    try:
        if isinstance(backend, staff.BackendUnavailable):
            raise ValueError("backend unavailable")
        source = transcript_source(row, backend)
        for entry in transcript_entries(source, changed):
            if isinstance(entry, TornTranscriptRecord):
                malformed = True
            elif entry and (not messages or messages[-1] != entry):
                messages.append(entry)
        if not messages:
            raise ValueError("source transcript has no assignments or final replies")
        run.mkdir(parents=True, exist_ok=True)
        extract = handoff_path(run, row)
        from scripts.lib.atomic_file import write_text_atomic
        write_text_atomic(extract, f"{HANDOFF_SOURCE_PREFIX}{source}\n\n" + "\n\n".join(messages) + "\n\nChanged files:\n" + "\n".join(sorted(changed)) + "\n")
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


def switch_record_path(run: Path, name: str) -> Path:
    """Keep native switch state separate from saved Claude launcher records."""
    return run / f"native_switch_{name}.json"


def dispatch_record(run: Path, name: str, *, report_anomaly: bool = True) -> DispatchRecord:
    path = switch_record_path(run, name)
    fields = worker_lifecycle.read_worker_state(path)
    try:
        return DispatchRecord(str(fields.get("assignment_id", "")),
                              SwitchState(fields.get("dispatch_state", SwitchState.NO_RECORD)))
    except ValueError:
        if report_anomaly:
            print(f"anomaly: {name}: unknown dispatch_state {fields.get('dispatch_state')!r}; using NO_RECORD", file=sys.stderr)
        return DispatchRecord(str(fields.get("assignment_id", "")), SwitchState.NO_RECORD)


def dispatch_pending(run: Path, name: str, prompt: Path) -> bool:
    try:
        record = dispatch_record(run, name)
    except (OSError, worker_lifecycle.WorkerStateUnreadable) as error:
        print(f"anomaly: {name}: {error}".replace("\n", " "), file=sys.stderr)
        return False
    return record.assignment_id == assignment_id(prompt) and record.state is not SwitchState.ASSIGNMENT_ACCEPTED


def record_dispatch(allowed: WorkerStartAllowed, request: str, state: SwitchState, *, expected_request: str = "") -> None:
    path = switch_record_path(allowed.run, allowed.name)
    with worker_lifecycle.locked_state(path) as handle:
        entry = handle.read_object()
        if expected_request and entry.get("assignment_id") != expected_request:
            return
        entry.update(assignment_id=request, dispatch_state=state)
        if isinstance(allowed.slot, ExistingWorkerSlot):
            entry["staff_id"] = allowed.slot.row.id
        handle.publish(json.dumps(entry, indent=2, sort_keys=True))


def publish_created(allowed: WorkerStartAllowed, identity: str,
                    cleanup: Callable[[str], str | None]) -> PublishedWorker:
    try:
        return publish(allowed, identity)
    except BaseException as error:
        detail = cleanup(identity)
        if detail and isinstance(error, AdmissionRefused):
            raise AdmissionRefused(f"{error}{detail}") from error
        raise


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
                  dispatch: Callable[[PublishedWorker], SwitchState] | DispatchOwner,
                  cleanup: Callable[[str], str | None], request: str = "") -> PublishedWorker:
    """Hold the row through publication and dispatch acceptance, then release it."""
    with worker_operation(allowed):
        handoff: FamilyHandoff = NoFamilyHandoff()
        record = staff.local_snapshot()
        row = (record.by_id(allowed.slot.row.id) if isinstance(allowed.slot, ExistingWorkerSlot)
               else find_worker(record, allowed.parent, allowed.name))
        if row is not None:
            allowed = WorkerStartAllowed(row.name, allowed.parent, allowed.run, ExistingWorkerSlot(row))
        if isinstance(allowed.slot, ExistingWorkerSlot) and row is None:
            raise AdmissionRefused(f"{allowed.slot.row.id}: source staff row is missing")
        backend = staff.backend(row) if row is not None else staff.BACKEND_UNAVAILABLE
        same = not isinstance(backend, staff.BackendUnavailable) and backend.family == family
        prior = dispatch_record(allowed.run, allowed.name)
        request = request or (hashlib.sha256(f"{allowed.slot.row.id}:{family}".encode()).hexdigest()
                              if isinstance(allowed.slot, ExistingWorkerSlot) else "")
        retry = bool(request and prior.assignment_id == request)
        if same and retry:
            assert row is not None
            if prior.state is SwitchState.ASSIGNMENT_ACCEPTED:
                print("unchanged")
                return PublishedWorker(row)
            extract = handoff_path(allowed.run, row)
            try:
                with extract.open(encoding="utf-8") as saved:
                    source = Path(saved.readline().removeprefix(HANDOFF_SOURCE_PREFIX).strip())
                handoff = TranscriptHandoff(extract, source)
            except OSError as error:
                print(f"anomaly: {row.name}: transcript extract: {error}".replace("\n", " "), file=sys.stderr)
                handoff = TranscriptUnavailable(str(error))
        else:
            if row is not None and not same:
                handoff = extract_transcript(row, allowed.run, backend)
                release_source = cast(ReleaseSource, import_module("scripts.shutdown.stop_resume").release_source)
                released = release_source(row, backend)
                if not isinstance(released, SourceReleased):
                    raise AdmissionRefused(released.reason)
                retry = True
            if retry and prior.assignment_id != request:
                record_dispatch(allowed, request, SwitchState.CREATION_INTENT_RECORDED)
            row = publish_created(allowed, start(handoff), cleanup).row
        published = PublishedWorker(row, handoff)
        if not isinstance(dispatch, DispatchOwner):
            state = dispatch(published)
            if retry:
                record_dispatch(allowed, request, state, expected_request=request)
        return published


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
