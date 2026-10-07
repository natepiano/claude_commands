"""Read screenshot and related tool calls from Claude and Codex transcripts."""

from __future__ import annotations

import json
import ast
import math
import os
import pickle
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast, override

Agent = Literal["Claude", "Codex"]
CallKind = Literal["shot", "other"]
ProjectAttribution = Literal["repository", "scratchpad", "last_path", "removed_worktree", "scratchpad_target_missing"]


@dataclass(frozen=True)
class NamedCaptureView:
    name: str


@dataclass(frozen=True)
class AdHocCaptureView:
    pass


CaptureView = NamedCaptureView | AdHocCaptureView


@dataclass(frozen=True)
class SelectedCaptureTarget:
    selector: str


@dataclass(frozen=True)
class UnspecifiedCaptureTarget:
    pass


CaptureTarget = SelectedCaptureTarget | UnspecifiedCaptureTarget


@dataclass(frozen=True)
class MeasuredFrameTime:
    milliseconds: float


@dataclass(frozen=True)
class UnavailableFrameTime:
    pass


CaptureFrameTime = MeasuredFrameTime | UnavailableFrameTime


@dataclass(frozen=True)
class SuccessfulCapture:
    image_paths: tuple[str, ...]
    view: CaptureView = AdHocCaptureView()
    target: CaptureTarget = UnspecifiedCaptureTarget()
    frame_time: CaptureFrameTime = UnavailableFrameTime()


@dataclass(frozen=True)
class FailedCapture:
    image_paths: tuple[str, ...]
    reason: str
    view: CaptureView = AdHocCaptureView()


CaptureAttempt = SuccessfulCapture | FailedCapture


@dataclass(frozen=True)
class AttemptCountInferredFromImages:
    pass


@dataclass(frozen=True)
class ExactOrderedCaptureAttempts:
    captures: tuple[CaptureAttempt, ...]


AttemptEvidence = AttemptCountInferredFromImages | ExactOrderedCaptureAttempts


@dataclass(frozen=True)
class IdentifiedSession:
    value: str


@dataclass(frozen=True)
class UnidentifiedSession:
    pass


SessionEvidence = IdentifiedSession | UnidentifiedSession


@dataclass(frozen=True)
class SuccessfulInvocation:
    time: datetime
    session: SessionEvidence
    attempts: tuple[CaptureAttempt, ...]
    identity: TimingRecordId
    source_host: str
    invocation_kind: Literal["shot", "views_check", "unknown"] = "unknown"
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FailedInvocation:
    time: datetime
    session: SessionEvidence
    attempts: tuple[CaptureAttempt, ...]
    exit_code: int
    reason: str
    identity: TimingRecordId
    source_host: str
    invocation_kind: Literal["shot", "views_check", "unknown"] = "unknown"
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OldSuccessInvocation:
    time: datetime
    identity: TimingRecordId
    source_host: str
    invocation_kind: Literal["shot", "views_check", "unknown"] = "unknown"
    raw: dict[str, object] = field(default_factory=dict)


InvocationRecord = SuccessfulInvocation | FailedInvocation | OldSuccessInvocation


@dataclass(frozen=True)
class TimingRecordId:
    source_host: str
    source_path: str
    byte_offset: int


@dataclass(frozen=True)
class AvailableTimingSource:
    path: Path
    source_host: str


@dataclass(frozen=True)
class UnavailableTimingSource:
    pass


TimingSource = AvailableTimingSource | UnavailableTimingSource


@dataclass(frozen=True)
class PersistentScanCache:
    path: Path


@dataclass(frozen=True)
class UncachedScan:
    pass


ScanCache = PersistentScanCache | UncachedScan
NO_TIMING_SOURCE = UnavailableTimingSource()
NO_SCAN_CACHE = UncachedScan()


@dataclass(frozen=True)
class ToolCall:
    start: datetime
    end: datetime
    session_id: str
    transcript_path: str
    agent: Agent
    project: str
    kind: CallKind
    source: str
    command: str = ""
    image_count: int = 0
    project_attribution: ProjectAttribution = "last_path"
    script_path: str = ""
    script_paths: tuple[str, ...] = ()
    cwd: str = ""
    image_paths: tuple[str, ...] = ()
    cited_image_paths: tuple[str, ...] = ()
    result_position: int = 0
    attempt_evidence: AttemptEvidence = AttemptCountInferredFromImages()
    source_host: str = "local"


PREFILTER = (
    "mcp__brp__", "brp_extras/screenshot", "brp_extras_screenshot", "hana_shot.py shot",
    "grim ", "spectacle ", "screencapture ", "import -window",
    "import -display", "import -screen", "playwright", "browser_take_screenshot",
    "browser_screenshot", "take_screenshot", "world.", "bevy/", "hana/",
)
CODEX_PREFILTER = PREFILTER[:-3]
CLAUDE_CANDIDATE_BYTES = tuple(pattern.encode() for pattern in PREFILTER)
CODEX_CANDIDATE_BYTES = tuple(pattern.encode() for pattern in CODEX_PREFILTER)
PARALLEL_SCAN_BYTES = 1_000_000_000
RELATED_WRITE = (
    r"(?:\*\*\* (?:Add|Update) File:|cat\s*>{1,2}|tee\s+|\"name\"\s*:\s*\"Write\")" +
    r"[^\n]*(?:rpc|call|invoke|send_request|method|brp\.(?:sh|py)).{0,48}"
    + r"(?:world\.|bevy/|hana/|brp_extras/)"
)
RELATED_WRITE_BYTES = re.compile(RELATED_WRITE.encode())
BRP_SKIPS = (
    "brp_launch", "brp_shutdown", "brp_status", "brp_list", "read_log",
    "delete_logs", "list_logs", "type_guide", "registry_schema", "rpc_discover",
)
SHELL_NAMES = {"Bash", "exec_command", "functions.exec_command", "exec", "functions.exec", "shell_command"}
SHELL_SEPARATORS = {";", "&", "&&", "|", "||", "\n"}
TEXT_PROGRAMS = {"rg", "grep", "cat", "sed", "head", "tail", "find", "echo", "printf", "tee"}
SHELL_HINTS = (
    "brp_extras/", "brp_extras_", "world.", "bevy/", "hana/", "hana_shot.py",
    "playwright", "grim ", "spectacle ", "screencapture ", "import -",
)
NON_SCRIPT_SUFFIXES = {".md", ".rst", ".txt", ".rs", ".ron", ".json", ".toml", ".yaml", ".yml", ".log"}
CLAUDE_LINE_HINTS = (*SHELL_HINTS, "mcp__brp__", "browser", "take_screenshot")
CLAUDE_HINT = re.compile(b"|".join(re.escape(hint.encode()) for hint in CLAUDE_LINE_HINTS))
CODEX_HINT = re.compile(b"|".join(re.escape(hint.encode()) for hint in (*CLAUDE_LINE_HINTS, "apply_patch", "Write", "Edit")))
JS_COMMAND = re.compile(r"\bcmd\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)")
TOOL_RESULT_ID = re.compile(r'"tool_use_id"\s*:\s*"([^\"]+)"')
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1")
PNG_PATH = re.compile(r"(?<![A-Za-z0-9_./~:-])[A-Za-z0-9_./~:-]{1,512}\.png\b", re.IGNORECASE)
CITATION_SPACED_PATH = re.compile(r"(?:/|~/)[^\"'<>()[\]{}\r\n]*?\.png\b", re.IGNORECASE)
CODEX_THREAD_SUFFIX = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", re.IGNORECASE)
TEXT_WRITE = re.compile(r"\.write_text\s*\(\s*(['\"])(.*?)\1", re.DOTALL)
BRP_METHOD = re.compile(r"[\"']?method[\"']?\s*:\s*[\"']([^\"']+)[\"']")
CODE_CALL = re.compile(r"\b(?:rpc|call|invoke|send_request)\s*\(\s*(['\"])([^'\"]+)\1")
SCRIPT_WRITE = re.compile(r"\b(?:cat\s*>{1,2}\s*|tee\s+)([^\s;]+)")
SCRIPT_HEREDOC_WRITE = re.compile(r"\b(?:cat\s*(>>?)\s*|tee\s+(-a\s+)?)([^\s;]+)")
SCREENSHOT_WORD = re.compile(
    r"brp_extras[/_]screenshot|hana_shot\.py\s+shot|\b(?:grim|spectacle|screencapture)\b|" +
    r"\bimport\s+-(?:window|display|screen)\b|\bplaywright\b[^\n;]*\bscreenshot\b",
    re.IGNORECASE,
)
INLINE_IMAGE_DATA = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+|[A-Za-z0-9+/]{1024,}={0,2}")


def _without_inline_image_data(value: str) -> str:
    return INLINE_IMAGE_DATA.sub("<image data omitted>", value)


def _compact_tool_input(value: object) -> object:
    if isinstance(value, str):
        return _without_inline_image_data(value)
    if isinstance(value, dict):
        return {key: _compact_tool_input(item) for key, item in cast(dict[object, object], value).items()}
    if isinstance(value, list):
        return [_compact_tool_input(item) for item in cast(list[object], value)]
    return value


@dataclass
class RememberedScript:
    content: str
    classification_state: Literal["unread", "not_related", "shot", "other"] = "unread"
    source: str = ""
    screenshot_calls: int = 0
    selected_content: dict[tuple[str, ...], str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptRun:
    kind: CallKind
    source: str
    path: str
    screenshot_calls: int
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptInvocation:
    path: str
    arguments: tuple[str, ...]
    cwd: str
    mode: Literal["execute", "source"] = "execute"


@dataclass(frozen=True)
class ShellFunction:
    name: str
    content: str
    start: int
    end: int


class TranscriptScan(list[ToolCall]):
    candidate_file_count: int
    bytes_read: int

    invocations: list[InvocationRecord]

    def __init__(self, calls: list[ToolCall], candidate_file_count: int, bytes_read: int,
                 invocations: list[InvocationRecord] | None = None) -> None:
        super().__init__(calls)
        self.candidate_file_count = candidate_file_count
        self.bytes_read = bytes_read
        self.invocations = invocations if invocations is not None else []


@dataclass(frozen=True)
class TranscriptEvidence:
    calls: list[ToolCall]
    citations: list[ImageCitation]


@dataclass(frozen=True)
class ImageCitation:
    position: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ClaudeToolUseAwaitingResult:
    start: datetime
    session: str
    project: str
    attribution: ProjectAttribution
    kind: CallKind
    source: str
    command: str
    script_path: str
    script_calls: int
    script_paths: tuple[str, ...]
    cwd: str


@dataclass(frozen=True)
class CodexToolUseAwaitingResult:
    start: datetime
    kind: CallKind
    source: str
    command: str
    script_path: str
    script_calls: int
    script_paths: tuple[str, ...]
    cwd: str


@dataclass(frozen=True)
class ScriptWriteAwaitingResult:
    name: str
    value: object
    cwd: str


@dataclass
class ClaudeTranscriptProgress:
    pending_uses: dict[str, ClaudeToolUseAwaitingResult] = field(default_factory=dict)
    pending_writes: dict[str, ScriptWriteAwaitingResult] = field(default_factory=dict)
    pending_citations: dict[str, tuple[str, ...]] = field(default_factory=dict)
    remembered_scripts: dict[str, RememberedScript] = field(default_factory=dict)
    known_cwds: set[str] = field(default_factory=set)
    next_position: int = 0


@dataclass
class CodexTranscriptProgress:
    session: str
    project: str
    attribution: ProjectAttribution = "last_path"
    cwd: str = ""
    pending_uses: dict[str, CodexToolUseAwaitingResult] = field(default_factory=dict)
    pending_citations: dict[str, tuple[str, ...]] = field(default_factory=dict)
    remembered_scripts: dict[str, RememberedScript] = field(default_factory=dict)
    next_position: int = 0


TranscriptProgress = ClaudeTranscriptProgress | CodexTranscriptProgress


@dataclass
class CachedTranscriptEvidence:
    agent: Agent
    source_host: str
    device: int
    inode: int
    cursor: int
    observed_size: int
    modified_ns: int
    evidence: TranscriptEvidence
    progress: TranscriptProgress
    candidate: bool


@dataclass
class CachedTimingEvidence:
    device: int
    inode: int
    cursor: int
    observed_size: int
    modified_ns: int
    records: list[InvocationRecord]


@dataclass
class CachedScanEvidence:
    format_version: int
    roots: tuple[str, str]
    files: dict[str, CachedTranscriptEvidence]
    timings: dict[tuple[str, str], CachedTimingEvidence]


CACHE_FORMAT_VERSION = 3


def _load_cache(cache: PersistentScanCache, roots: tuple[str, str]) -> CachedScanEvidence:
    try:
        with cache.path.open("rb") as source:
            value = cast(object, pickle.load(source))
        if (isinstance(value, CachedScanEvidence) and value.format_version == CACHE_FORMAT_VERSION
                and value.roots == roots):
            return value
    except Exception:
        pass
    return CachedScanEvidence(CACHE_FORMAT_VERSION, roots, {}, {})


def _save_cache(cache: PersistentScanCache, evidence: CachedScanEvidence) -> None:
    for transcript in evidence.files.values():
        for script in transcript.progress.remembered_scripts.values():
            script.selected_content.clear()
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{cache.path.name}.", dir=cache.path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            pickle.dump(evidence, target, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(name, cache.path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _items(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _stamp(value: object) -> datetime | None:
    stamp = _string(value)
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _invocation_record(value: object, identity: TimingRecordId) -> InvocationRecord | None:
    record = _object(value)
    stamp = _stamp(record.get("time"))
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    status = record.get("status")
    source_host = identity.source_host
    kind_value = record.get("invocation_kind")
    kind: Literal["shot", "views_check", "unknown"] = (
        kind_value if kind_value in ("shot", "views_check") else "unknown"
    )
    if status is None:
        return OldSuccessInvocation(stamp, identity, source_host, kind, record)
    raw_session = _object(record.get("session"))
    if raw_session.get("state") == "present" and isinstance(raw_session.get("value"), str):
        session: SessionEvidence = IdentifiedSession(_string(raw_session.get("value")))
    elif raw_session.get("state") == "absent":
        session = UnidentifiedSession()
    else:
        return None
    raw_attempts = record.get("attempts")
    if not isinstance(raw_attempts, list):
        return None
    attempts: list[CaptureAttempt] = []
    for raw_attempt in cast(list[object], raw_attempts):
        attempt = _object(raw_attempt)
        view_value = attempt.get("view")
        view: CaptureView = NamedCaptureView(view_value) if isinstance(view_value, str) else AdHocCaptureView()
        raw_paths = attempt.get("image_paths")
        if not isinstance(raw_paths, list):
            return None
        path_items = cast(list[object], raw_paths)
        if not all(isinstance(path, str) for path in path_items):
            return None
        paths = tuple(cast(list[str], path_items))
        if attempt.get("status") == "success":
            target_value = attempt.get("target")
            target: CaptureTarget = (SelectedCaptureTarget(target_value)
                                     if isinstance(target_value, str) else UnspecifiedCaptureTarget())
            frame_value = attempt.get("frame_ms")
            frame: CaptureFrameTime = (MeasuredFrameTime(float(frame_value))
                                       if isinstance(frame_value, (int, float)) and not isinstance(frame_value, bool)
                                       else UnavailableFrameTime())
            attempts.append(SuccessfulCapture(paths, view, target, frame))
        elif attempt.get("status") == "failure" and isinstance(attempt.get("failure_reason"), str):
            attempts.append(FailedCapture(paths, _string(attempt.get("failure_reason")), view))
        else:
            return None
    if status == "success" and record.get("exit_code") == 0:
        return SuccessfulInvocation(stamp, session, tuple(attempts), identity, source_host, kind, record)
    exit_code = record.get("exit_code")
    reason = record.get("failure_reason")
    if status == "failure" and isinstance(exit_code, int) and not isinstance(exit_code, bool) and (
        isinstance(reason, str)
    ):
        return FailedInvocation(stamp, session, tuple(attempts), exit_code, reason, identity, source_host, kind, record)
    return None


def _timing_invocations(path: Path, source_host: str = "local", start: int = 0) -> tuple[list[InvocationRecord], int, int]:
    if not path.exists():
        return [], start, 0
    records: list[InvocationRecord] = []
    with path.open("rb") as source:
        _ = source.seek(start)
        while line := source.readline():
            if not line.endswith(b"\n"):
                _ = source.seek(-len(line), os.SEEK_CUR)
                break
            position = source.tell() - len(line)
            try:
                raw = cast(object, json.loads(line))
            except json.JSONDecodeError:
                continue
            record = _invocation_record(raw, TimingRecordId(source_host, str(path), position))
            if record is not None:
                records.append(record)
        cursor = source.tell()
    return records, cursor, path.stat().st_size - start


def _recorded_calls(calls: list[ToolCall], records: list[InvocationRecord]) -> list[ToolCall]:
    updated = calls.copy()
    shot_calls = {index for index, call in enumerate(calls) if call.source == "hana_shot" and call.kind == "shot"}
    by_session: dict[str, set[int]] = {}
    for index in shot_calls:
        by_session.setdefault(calls[index].session_id, set()).add(index)
    for record in sorted(records, key=lambda item: item.time):
        if isinstance(record, OldSuccessInvocation) or record.invocation_kind == "views_check":
            continue
        pool = ({index for index in shot_calls if calls[index].source_host == record.source_host}
                if isinstance(record.session, UnidentifiedSession)
                else by_session.get(record.session.value, set()))
        candidates = [index for index in pool if (
            calls[index].source_host == record.source_host and
            calls[index].start - timedelta(seconds=10) <= record.time <= calls[index].end + timedelta(seconds=10)
        )]
        if not candidates or isinstance(record.session, UnidentifiedSession) and len(candidates) != 1:
            continue
        index = min(candidates, key=lambda candidate: abs((calls[candidate].end - record.time).total_seconds()))
        call = updated[index]
        paths = tuple(dict.fromkeys((*call.image_paths, *(path for attempt in record.attempts
                                                         for path in attempt.image_paths))))
        earlier = call.attempt_evidence.captures if isinstance(call.attempt_evidence, ExactOrderedCaptureAttempts) else ()
        updated[index] = replace(call, image_paths=paths,
                                 attempt_evidence=ExactOrderedCaptureAttempts((*earlier, *record.attempts)))
    return updated


def _candidate_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    args = ["rg", "-l", "-0", "--no-ignore", "--hidden", "--glob", "*.jsonl"]
    for pattern in PREFILTER:
        args.extend(("-F", "-e", pattern))
    result = subprocess.run([*args, str(root)], capture_output=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return [Path(name.decode(errors="replace")) for name in result.stdout.split(b"\0") if name]


def _codex_candidate_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    args = ["rg", "-l", "-0", "--no-ignore", "--hidden", "--glob", "*.jsonl"]
    for pattern in CODEX_PREFILTER:
        args.extend(("-F", "-e", pattern))
    direct = subprocess.run([*args, str(root)], capture_output=True, check=False)
    related = subprocess.run(
        ["rg", "-l", "-0", "--pcre2", "--no-ignore", "--hidden", "--glob", "*.jsonl", "-e", RELATED_WRITE, str(root)],
        capture_output=True, check=False,
    )
    for result in (direct, related):
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.decode(errors="replace"))
    names = set(direct.stdout.split(b"\0")) | set(related.stdout.split(b"\0"))
    return [Path(name.decode(errors="replace")) for name in names if name]


def _claude_encoded(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _scratchpad_project(cwd: str, known_cwds: set[str]) -> tuple[str, ProjectAttribution]:
    parts = Path(cwd).parts
    for index, part in enumerate(parts):
        if not part.startswith("claude-") or index + 1 >= len(parts):
            continue
        encoded = parts[index + 1]
        if not encoded.startswith("-") or "scratchpad" not in parts[index + 2:]:
            continue
        same_session = [name for name in known_cwds if _claude_encoded(Path(name)) == encoded]
        if same_session:
            project, _ = _project(max(same_session, key=len), {})
            return project, "scratchpad"
        frontier = [Path("/")]
        best = Path("/")
        while frontier:
            path = frontier.pop()
            prefix = _claude_encoded(path)
            if len(prefix) > len(_claude_encoded(best)):
                best = path
            if prefix == encoded:
                project, _ = _project(str(path), {})
                return project, "scratchpad"
            if not encoded.startswith(("" if path == Path("/") else prefix) + "-"):
                continue
            try:
                children = path.iterdir()
                frontier.extend(child for child in children if child.is_dir()
                                and encoded.startswith(_claude_encoded(child)))
            except OSError:
                continue
        remainder = encoded[len(_claude_encoded(best)):].strip("-")
        if remainder and best != Path("/"):
            parent_project, parent_attribution = _project(str(best), {})
            if parent_attribution == "repository":
                return (parent_project + "-" + remainder, "scratchpad_target_missing")
        return (remainder or best.name, "scratchpad_target_missing")
    return "", "last_path"


def _project(cwd: str, cache: dict[str, tuple[str, ProjectAttribution]],
             known_cwds: set[str] | None = None) -> tuple[str, ProjectAttribution]:
    if cwd in cache:
        return cache[cwd]
    scratchpad = _scratchpad_project(cwd, known_cwds or set()) if "scratchpad" in cwd and "claude-" in cwd else ("", "last_path")
    if scratchpad[0]:
        cache[cwd] = scratchpad
        return cache[cwd]
    original = Path(cwd).expanduser()
    current = original
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.exists():
        result = subprocess.run(
            ["git", "-C", str(current), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip()).resolve()
            repo = common_dir.parent if common_dir.name == ".git" else current
            cache[cwd] = (repo.name, "repository")
            return cache[cwd]
    cache[cwd] = (original.name or "unknown", "last_path")
    return cache[cwd]


def _exec_shell_commands(value: str) -> str:
    if "tools.exec_command" not in value:
        return ""
    commands: list[str] = []
    for match in JS_COMMAND.finditer(value):
        literal = match.group(1)
        if literal.startswith('"'):
            try:
                commands.append(_string(cast(object, json.loads(literal))))
            except json.JSONDecodeError:
                continue
        elif literal.startswith("'"):
            commands.append(literal[1:-1].replace("\\'", "'"))
        else:
            commands.append(literal[1:-1].replace("\\`", "`"))
    return "\n".join(commands)


def _shell_command(name: str, value: object) -> str:
    if name not in SHELL_NAMES:
        return ""
    if isinstance(value, str):
        return _exec_shell_commands(value) if name in {"exec", "functions.exec"} else value
    data = _object(value)
    command = _string(data.get("command")) or _string(data.get("cmd"))
    if command:
        return command
    # Codex's exec wrapper places shell commands inside its JavaScript input.
    nested = _string(data.get("input"))
    return _exec_shell_commands(nested) if name in {"exec", "functions.exec"} else nested


def _without_heredoc_bodies(command: str) -> str:
    if "<<" not in command:
        return command
    lines = command.splitlines(keepends=True)
    kept: list[str] = []
    delimiter = ""
    for line in lines:
        if delimiter:
            if line.strip() == delimiter:
                delimiter = ""
            else:
                continue
            continue
        kept.append(line)
        match = HEREDOC.search(line)
        if match:
            delimiter = match.group(2)
    return "".join(kept)


def _heredoc_executions(command: str) -> list[tuple[str, str]]:
    if "<<" not in command:
        return []
    lines = command.splitlines(keepends=True)
    executions: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        marker = HEREDOC.search(line)
        if marker is None:
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() != marker.group(2):
            end += 1
        if end == len(lines):
            continue
        opener = line[:marker.start()]
        body = "".join(lines[index + 1:end])
        opener_segments = _shell_segments(opener)
        program, _ = _program(opener_segments[-1]) if opener_segments else ("", [])
        if program in {"python", "python3", "python3.13"} or program.endswith((".py", "/python", "/python3")) or program.startswith("$") and "-" in opener_segments[-1]:
            executions.append(("python", body))
        elif program in {"bash", "sh", "zsh", "ssh"}:
            executions.append(("shell", body))
        elif program in {"cat", "tee"}:
            written = SCRIPT_WRITE.search(opener)
            if written is not None:
                path = written.group(1)
                rest = "".join(lines[end + 1:])
                script_name = Path(path).name
                run = re.search(
                    r"(?:^|[;&\n])\s*(?:(?:uv\s+run\s+(?:--with\s+\S+\s+)*)?"
                    + r"(?:[^\s;]*python(?:3(?:\.\d+)?)?|bash|sh|zsh|[^\s;]*py\d*\.sh)\s+(?:-[A-Za-z]+\s+)*)?"
                    + r"(?:[^\s;]*/)?" + re.escape(script_name) + r"(?:\s|$)", rest,
                )
                if run is not None:
                    executions.append(("python" if script_name.endswith(".py") else "shell", body))
                elif script_name.endswith(".py"):
                    stem = re.escape(Path(script_name).stem)
                    for imported in re.finditer(r"\bfrom\s+" + stem + r"\s+import\s+([A-Za-z_][A-Za-z_0-9]*)", rest):
                        function = imported.group(1)
                        if re.search(r"\bdef\s+" + function + r"\s*\(", body) and re.search(r"\b" + function + r"\s*\(", rest[imported.end():]):
                            executions.append(("python", body))
                            break
    return executions


def _skipped_brp_method(method: str) -> bool:
    normalized = method.replace("/", "_").replace(".", "_")
    if normalized.startswith("brp_extras_"):
        normalized = "brp_" + normalized.removeprefix("brp_extras_")
    return any(skip in normalized for skip in BRP_SKIPS)


def _shell_segments(command: str) -> list[list[str]]:
    shell_text = _without_heredoc_bodies(command).replace("\\\n", " ")
    lexer = shlex.shlex(shell_text, posix=True, punctuation_chars=";&|\n{}()")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = []
    segment: list[str] = []
    try:
        for token in lexer:
            if token in SHELL_SEPARATORS or all(character in ";&|{}()" for character in token):
                if segment:
                    segments.append(segment)
                segment = []
            else:
                segment.append(token)
    except ValueError:
        return []
    if segment:
        segments.append(segment)
    return segments


def _backtick_commands(command: str) -> list[str]:
    nested: list[str] = []
    start = -1
    single_quoted = False
    escaped = False
    shell_text = _without_heredoc_bodies(command)
    for index, character in enumerate(shell_text):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "'":
            single_quoted = not single_quoted
        elif character == "`" and not single_quoted:
            if start < 0:
                start = index + 1
            else:
                nested.append(shell_text[start:index])
                start = -1
    return nested


def _program(tokens: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"do", "then", "else", "elif", "!", "time", "command", "exec", "env"}:
            index += 1
        elif token == "nice":
            index += 1
            if index < len(tokens) and tokens[index] in {"-n", "--adjustment"}:
                index += 2
            elif index < len(tokens) and re.fullmatch(r"-[0-9]+", tokens[index]):
                index += 1
        elif token == "timeout":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            if index < len(tokens):
                index += 1
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", token):
            index += 1
        else:
            break
    if index >= len(tokens):
        return "", []
    name = Path(tokens[index]).name
    if name == "uv" and index + 2 < len(tokens) and tokens[index + 1] == "run":
        index += 2
        name = Path(tokens[index]).name
    return name, tokens[index:]


def _method_result(method: str, command: str) -> tuple[CallKind, str, str] | None:
    if method in {"brp_extras/screenshot", "brp_extras_screenshot"}:
        return ("shot", "bash_brp", command)
    if method and not _skipped_brp_method(method):
        return ("other", "bash_brp", command)
    return None


def _code_result(code: str, command: str) -> tuple[CallKind, str, str] | None:
    methods = [match.group(1) for match in BRP_METHOD.finditer(code)]
    methods.extend(match.group(2) for match in CODE_CALL.finditer(code))
    if "brp_extras/screenshot" in methods or "brp_extras_screenshot" in methods:
        return ("shot", "bash_brp", command)
    return next((found for method in methods if (found := _method_result(method, command)) is not None), None)


def _assigned_request_results(command: str) -> list[tuple[CallKind, str, str]]:
    results: list[tuple[CallKind, str, str]] = []
    for assigned in re.finditer(r"\b([A-Za-z_][A-Za-z_0-9]*)=(['\"])(.*?)\2", command, re.DOTALL):
        name = assigned.group(1)
        sent = re.search(r"\bcurl\b[\s\S]*?(?:-d|--data(?:-raw)?)\s+[\"']?\$" + name + r"\b", command[assigned.end():])
        if sent is None:
            continue
        for method in BRP_METHOD.finditer(assigned.group(3)):
            found = _method_result(method.group(1), command)
            if found is not None:
                results.append(found)
    return results


def _shell_classification(command: str) -> tuple[CallKind, str, str] | None:
    candidates = _assigned_request_results(command) if "curl" in command and "method" in command else []
    for language, body in _heredoc_executions(command):
        found = _code_result(body, command) if language == "python" else _shell_classification(body)
        if found is not None:
            candidates.append((found[0], found[1], command))
    if "`" in command:
        for inner in _backtick_commands(command):
            found = _shell_classification(inner)
            if found is not None:
                candidates.append((found[0], found[1], command))
    for tokens in _shell_segments(command):
        program, arguments = _program(tokens)
        if not program or program in TEXT_PROGRAMS or program == "cd":
            continue
        runner = program in {"python", "python3", "curl", "bash", "sh", "zsh", "ssh"} or program.endswith((".py", ".sh"))
        joined = " ".join(arguments)
        if program in {"bash", "sh", "zsh"} and "-c" in arguments:
            code_index = arguments.index("-c") + 1
            if code_index < len(arguments):
                found = _shell_classification(arguments[code_index])
                if found is not None:
                    candidates.append((found[0], found[1], command))
        if program in {"bash", "sh", "zsh"} and len(arguments) > 2:
            for argument in arguments[2:]:
                if re.match(r"(?:rpc|curl|python\d*|bash|\$[A-Za-z_])\s", argument):
                    found = _shell_classification(argument)
                    if found is not None:
                        candidates.append((found[0], found[1], command))
        if program == "ssh" and len(arguments) > 2:
            found = _shell_classification(arguments[-1])
            if found is not None:
                candidates.append((found[0], found[1], command))
        if runner and any("hana_shot.py" in token and index + 1 < len(arguments) and arguments[index + 1] == "shot"
                          for index, token in enumerate(arguments)):
            candidates.append(("shot", "hana_shot", command))
        if runner:
            if "method" in joined:
                for method in BRP_METHOD.finditer(joined):
                    found = _method_result(method.group(1), command)
                    if found is not None:
                        candidates.append(found)
            if program in {"python", "python3"} or program.endswith(".py") or "-c" in arguments:
                code = arguments[arguments.index("-c") + 1] if "-c" in arguments and arguments.index("-c") + 1 < len(arguments) else ""
                found = _code_result(code, command)
                if found is not None:
                    candidates.append(found)
        for token in arguments[1:]:
            if re.fullmatch(r"(?:brp_extras[/_][A-Za-z_]+|world\.[A-Za-z_]+|bevy/[A-Za-z_]+|hana/[A-Za-z_]+)", token):
                found = _method_result(token, command)
                if found is not None:
                    candidates.append(found)
        if program in {"grim", "spectacle", "screencapture"}:
            candidates.append(("shot", program, command))
        if program == "import" and any(flag in arguments for flag in ("-window", "-display", "-screen")):
            candidates.append(("shot", "import", command))
        if program in {"playwright", "npx", "python", "python3"} and "playwright" in joined.lower():
            if "screenshot" in joined.lower():
                candidates.append(("shot", "browser", command))
            if any(action in joined.lower() for action in ("navigate", "click", "fill", "hover", "press", "scroll", "snapshot")):
                candidates.append(("other", "browser", command))
    return max(candidates, key=lambda found: (found[0] == "shot", found[1] == "hana_shot"), default=None)


def _classify(name: str, value: object) -> tuple[CallKind, str, str] | None:
    command = _shell_command(name, value)
    if command and any(hint in command for hint in SHELL_HINTS):
        classified = _shell_classification(command)
        if classified is not None:
            return classified
    if name == "mcp__brp__brp_extras_screenshot":
        return ("shot", "mcp_brp", command)
    if name.startswith("mcp__brp__"):
        if any(skip in name for skip in BRP_SKIPS):
            return None
        return ("other", "brp", command)
    lower_name = name.lower()
    if "playwright" in lower_name or "browser" in lower_name:
        if "screenshot" in lower_name:
            return ("shot", "browser", command)
        if any(part in lower_name for part in ("navigate", "click", "type", "fill", "hover", "press", "scroll", "snapshot")):
            return ("other", "browser", command)
    return None


def _script_path(path: str, cwd: str) -> str:
    expanded = os.path.expanduser(path.strip("\"'"))
    return os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded))


def _remember(path: str, content: str, scripts: dict[str, RememberedScript]) -> None:
    content = _without_inline_image_data(content)
    if Path(path).suffix.lower() in NON_SCRIPT_SUFFIXES:
        _ = scripts.pop(path, None)
        return
    old = scripts.get(path)
    if old is not None and old.source:
        previous_calls = tuple(line for line in old.content.splitlines() if any(hint in line for hint in SHELL_HINTS))
        current_calls = tuple(line for line in content.splitlines() if any(hint in line for hint in SHELL_HINTS))
        if previous_calls and previous_calls == current_calls:
            scripts[path] = RememberedScript(content, source=old.source)
            return
    if path.endswith(".py"):
        direct = _code_result(content, content)
    else:
        direct = _shell_classification(content) if any(hint in content for hint in SHELL_HINTS) else None
    names = (Path(name).name for name in scripts if name != path)
    if direct is not None or any(name in content for name in names):
        scripts[path] = RememberedScript(content, source=direct[1] if direct is not None else "")
    else:
        _ = scripts.pop(path, None)


def _heredoc_writes(command: str, cwd: str) -> list[tuple[str, str, bool]]:
    lines = command.splitlines(keepends=True)
    writes: list[tuple[str, str, bool]] = []
    current = cwd
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = HEREDOC.search(line)
        prefix = line[:marker.start()] if marker else line
        for tokens in _shell_segments(prefix):
            program, arguments = _program(tokens)
            if program == "cd" and len(arguments) > 1:
                current = _script_path(arguments[1], current)
        if marker is None:
            index += 1
            continue
        target = SCRIPT_HEREDOC_WRITE.search(line[:marker.start()])
        if target is None:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() != marker.group(2):
            end += 1
        if end == len(lines):
            index += 1
            continue
        path = _script_path(target.group(3), current)
        writes.append((path, "".join(lines[index + 1:end]), target.group(1) == ">>" or bool(target.group(2))))
        index = end + 1
    return writes


def _patch_changes(patch: str, cwd: str, scripts: dict[str, RememberedScript]) -> None:
    lines = patch.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
            index += 1
            continue
        action, raw_path = line[4:].split(": ", 1)
        path = _script_path(raw_path, cwd)
        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("*** "):
            body.append(lines[index])
            index += 1
        if action == "Delete File":
            _ = scripts.pop(path, None)
        elif action == "Add File":
            _remember(path, "".join(part[1:] for part in body if part.startswith("+")), scripts)
        elif action == "Update File":
            old = scripts.get(path)
            content = old.content if old is not None else ""
            before: list[str] = []
            after: list[str] = []
            for part in [*body, "@@\n"]:
                if part.startswith("@@"):
                    if before:
                        previous = "".join(before)
                        replacement = "".join(after)
                        content = content.replace(previous, replacement, 1) if previous in content else content + replacement
                    before, after = [], []
                elif part.startswith("-"):
                    before.append(part[1:])
                elif part.startswith("+"):
                    after.append(part[1:])
                elif part.startswith(" "):
                    before.append(part[1:])
                    after.append(part[1:])
            _remember(path, content, scripts)


def _script_write_may_matter(name: str, value: object, cwd: str,
                             scripts: dict[str, RememberedScript]) -> bool:
    data = _object(value)
    path = _script_path(_string(data.get("file_path")), cwd)
    if name == "Edit":
        return path in scripts
    if name != "Write" or Path(path).suffix.lower() in NON_SCRIPT_SUFFIXES:
        return False
    if path in scripts:
        return True
    content = _string(data.get("content"))
    return any(hint in content for hint in SHELL_HINTS) or any(Path(saved).name in content for saved in scripts)


def _remember_tool(name: str, value: object, cwd: str, scripts: dict[str, RememberedScript]) -> None:
    data = _object(value)
    if name == "Write":
        path = _string(data.get("file_path"))
        if path:
            _remember(_script_path(path, cwd), _string(data.get("content")), scripts)
    elif name == "Edit":
        path = _script_path(_string(data.get("file_path")), cwd)
        previous = scripts.get(path)
        if previous is not None:
            old = _string(data.get("old_string"))
            new = _string(data.get("new_string"))
            content = previous.content.replace(old, new) if data.get("replace_all") else previous.content.replace(old, new, 1)
            _remember(path, content, scripts)
    elif name.endswith("apply_patch"):
        patch = _string(data.get("input")) if data else _string(value)
        if patch:
            _patch_changes(patch, cwd, scripts)
    else:
        command = _shell_command(name, value)
        if not command or "<<" not in command and "rm" not in command:
            return
        if "<<" in command:
            for path, body, append in _heredoc_writes(command, cwd):
                prior = scripts.get(path)
                _remember(path, (prior.content if prior is not None else "") + body if append else body, scripts)
        if "rm" not in command:
            return
        current = cwd
        for tokens in _shell_segments(command):
            program, arguments = _program(tokens)
            if program == "cd" and len(arguments) > 1:
                current = _script_path(arguments[1], current)
            elif program == "rm":
                for target in arguments[1:]:
                    if not target.startswith("-"):
                        _ = scripts.pop(_script_path(target, current), None)


def _running_invocations(command: str, cwd: str, scripts: dict[str, RememberedScript],
                         fallback_cwd: str = "") -> list[ScriptInvocation]:
    found: list[ScriptInvocation] = []
    current = cwd
    fallback = fallback_cwd
    removed: set[str] = set()
    for tokens in _shell_segments(command):
        program, arguments = _program(tokens)
        if program == "cd" and len(arguments) > 1:
            current = _script_path(arguments[1], current)
            if fallback:
                fallback = _script_path(arguments[1], fallback)
            continue
        if program == "rm":
            removed.update(_script_path(target, current) for target in arguments[1:] if not target.startswith("-"))
            continue
        if program in {"bash", "sh", "zsh"} and "-c" in arguments:
            index = arguments.index("-c") + 1
            if index < len(arguments):
                found.extend(_running_invocations(arguments[index], current, scripts, fallback))
        candidate = ""
        run_arguments: list[str] = []
        if program in {"python", "python3", "python3.13", "bash", "sh", "zsh", "source", "."}:
            for index, argument in enumerate(arguments[1:], 1):
                if argument.startswith("-"):
                    continue
                candidate = argument
                run_arguments = arguments[index + 1:]
                break
        elif arguments and (arguments[0].startswith(("./", "/", "../")) or _script_path(arguments[0], current) in scripts):
            candidate = arguments[0]
            run_arguments = arguments[1:]
        if candidate:
            path = _script_path(candidate, current)
            if path not in scripts and fallback:
                path = _script_path(candidate, fallback)
            if path in scripts and path not in removed:
                found.append(ScriptInvocation(path, tuple(run_arguments), current,
                                              "source" if program in {"source", "."} else "execute"))
    return found


def _python_invocations(content: str, cwd: str, script_dir: str,
                        scripts: dict[str, RememberedScript]) -> list[ScriptInvocation]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    found: list[ScriptInvocation] = []
    vector_exec = {"os.execv", "os.execve", "os.execvp", "os.execvpe"}
    list_exec = {"os.execl", "os.execle", "os.execlp", "os.execlpe"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = ast.unparse(node.func)
        if name not in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
                        "subprocess.Popen", "Popen", "os.system", *vector_exec, *list_exec}:
            continue
        try:
            if name in vector_exec and len(node.args) > 1:
                raw = cast(object, ast.literal_eval(node.args[1]))
            elif name in list_exec and len(node.args) > 1:
                raw = cast(object, [ast.literal_eval(arg) for arg in node.args[1:]
                                    if not isinstance(arg, ast.Dict)])
            else:
                raw = cast(object, ast.literal_eval(node.args[0]))
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
        commands: list[str] = []
        if isinstance(raw, str):
            commands.append(raw)
        elif isinstance(raw, (list, tuple)):
            parts = list(cast(list[object] | tuple[object, ...], raw))
            if all(isinstance(item, str) for item in parts):
                commands.append(shlex.join([cast(str, item) for item in parts]))
        for command in commands:
            found.extend(_running_invocations(command, cwd, scripts, script_dir))
    return found


def _shell_functions(content: str) -> list[ShellFunction]:
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    functions: list[ShellFunction] = []
    index = 0
    while index < len(lines):
        opening = re.match(r"\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{", lines[index])
        if opening is None:
            index += 1
            continue
        end = index + 1
        if not lines[index].rstrip().endswith("}"):
            while end < len(lines) and lines[end].strip() != "}":
                end += 1
            end = min(end + 1, len(lines))
        functions.append(ShellFunction(opening.group(1), "".join(lines[index:end]), offsets[index], offsets[end]))
        index = end
    return functions


def _selected_content(content: str, path: str, arguments: tuple[str, ...], source_context: str = "") -> str:
    selected = arguments[0] if arguments else ""
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return content
        option_values: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
                continue
            flags = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            if not flags or not flags[0].startswith("-"):
                continue
            destination = next((keyword.value.value for keyword in node.keywords
                                if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant)
                                and isinstance(keyword.value.value, str)),
                               max(flags, key=len).lstrip("-").replace("-", "_"))
            for index, argument in enumerate(arguments):
                for flag in flags:
                    if argument == flag and index + 1 < len(arguments):
                        option_values[destination] = arguments[index + 1]
                    elif argument.startswith(flag + "="):
                        option_values[destination] = argument[len(flag) + 1:]
        command_aliases: set[str] = set()
        argument_lists: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if ast.unparse(node.value) == "sys.argv[1:]":
                argument_lists.update(target.id for target in node.targets if isinstance(target, ast.Name))
            if ast.unparse(node.value) == "sys.argv[1]":
                command_aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
            if isinstance(node.value, ast.Tuple):
                for target in node.targets:
                    if isinstance(target, ast.Tuple):
                        command_aliases.update(name.id for name, value in zip(target.elts, node.value.elts)
                                               if isinstance(name, ast.Name) and ast.unparse(value) == "sys.argv[1]")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(ast.unparse(node.value) == name + "[0]" for name in argument_lists):
                command_aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        changed = False

        class ChooseBranch(ast.NodeTransformer):
            @override
            def visit_Expr(self, node: ast.Expr) -> ast.AST | list[ast.stmt]:
                nonlocal changed
                value = node.value
                if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                        and value.func.id in {"exec", "eval"} and value.args
                        and ast.unparse(value.args[0]) == "sys.argv[1]" and arguments):
                    try:
                        parsed = ast.parse(arguments[0])
                    except SyntaxError:
                        return node
                    changed = True
                    return parsed.body
                return self.generic_visit(node)

            @override
            def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
                nonlocal changed
                test = node.test
                if not isinstance(test, ast.Compare) or len(test.comparators) != 1 or len(test.ops) != 1:
                    return self.generic_visit(node)
                operand = ast.unparse(test.left)
                if ("sys.argv[" not in operand and operand not in command_aliases
                        and not re.fullmatch(r"(?:args|parsed|namespace)\.[A-Za-z_]\w*", operand)):
                    return self.generic_visit(node)
                try:
                    wanted = cast(object, ast.literal_eval(test.comparators[0]))
                except (ValueError, TypeError):
                    return self.generic_visit(node)
                choice = selected
                position = re.fullmatch(r"sys\.argv\[(\d+)\]", operand)
                if position is not None:
                    offset = int(position.group(1)) - 1
                    choice = arguments[offset] if 0 <= offset < len(arguments) else ""
                elif operand.startswith(("args.", "parsed.", "namespace.")):
                    choice = option_values.get(operand.split(".", 1)[1], selected)
                matches = choice == wanted
                if isinstance(test.ops[0], ast.NotEq):
                    matches = not matches
                elif not isinstance(test.ops[0], ast.Eq):
                    return self.generic_visit(node)
                changed = True
                body = node.body if matches else node.orelse
                chosen: list[ast.stmt] = []
                for statement in body:
                    visited = cast(object, self.visit(statement))
                    if isinstance(visited, list):
                        chosen.extend(cast(list[ast.stmt], visited))
                    elif isinstance(visited, ast.stmt):
                        chosen.append(visited)
                return chosen

        transformed = cast(ast.Module | None, ChooseBranch().visit(tree))
        if transformed is None:
            return content
        functions = {node.name: node for node in transformed.body if isinstance(node, ast.FunctionDef)}
        runtime = [node for node in transformed.body if not isinstance(node, ast.FunctionDef)]
        decorated = [function for function in functions.values() if function.decorator_list]
        reachable: set[str] = set()
        pending = [node for statement in [*runtime, *decorated] for node in ast.walk(statement)]
        while pending:
            node = pending.pop()
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            name = node.id
            if name in functions and name not in reachable:
                reachable.add(name)
                pending.extend(ast.walk(functions[name]))
        reachable.update(function.name for function in decorated)
        selected_tree = ast.Module(body=[*runtime, *(functions[name] for name in functions if name in reachable)],
                                   type_ignores=[])
        return ast.unparse(selected_tree) if changed or len(selected_tree.body) != len(tree.body) else content
    case = re.search(r"\bcase\s+['\"]?\$(\d+)['\"]?\s+in\s*(.*?)\besac\b", content, re.DOTALL)
    branch: re.Match[str] | None = None
    if case is not None:
        options = cast(list[tuple[str, str]], re.findall(r"(?:^|;;)\s*([^;\n)]+)\)\s*(.*?)(?=;;|$)", case.group(2), re.DOTALL))
        offset = int(case.group(1)) - 1
        choice = arguments[offset] if 0 <= offset < len(arguments) else ""
        body = next((body for label, body in options if choice in label.split("|")), "")
        content = content[:case.start()] + body + content[case.end():]
    else:
        branch = re.search(r"\bif\s+\[\[?\s*['\"]?\$(\d+)['\"]?\s*(?:=|==)\s*['\"]?([^\s'\"]+)['\"]?\s*\]\]?\s*;?\s*then\s*(.*?)\s*(?:else\s*(.*?))?\s*fi\b", content, re.DOTALL)
        if branch is not None:
            offset = int(branch.group(1)) - 1
            choice = arguments[offset] if 0 <= offset < len(arguments) else ""
            body = branch.group(3) if choice == branch.group(2) else branch.group(4) or ""
            content = content[:branch.start()] + body + content[branch.end():]
    if case is None and branch is None and not source_context:
        return content
    functions = _shell_functions(content)
    if not functions:
        return content
    outside = content
    for function in reversed(functions):
        outside = outside[:function.start] + "\n" + outside[function.end:]
    outside = "\n".join(line for line in outside.splitlines() if not line.lstrip().startswith("#"))
    active = outside + "\n" + source_context
    included: set[str] = set()
    while True:
        commands = {_program(tokens)[0] for tokens in _shell_segments(active)}
        newly_called = [function for function in functions if function.name not in included
                        and function.name in commands]
        if not newly_called:
            break
        for function in newly_called:
            included.add(function.name)
            active += "\n" + function.content
    return outside + "\n" + "\n".join(function.content for function in functions if function.name in included)


def _screenshot_calls(content: str, python: bool = False) -> int:
    if python:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return 1
        count = 0
        functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

        def visit(node: ast.AST, multiplier: int, active: frozenset[str]) -> None:
            nonlocal count
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    visit(decorator, multiplier, active)
                if node.decorator_list:
                    for child in node.body:
                        visit(child, multiplier, active | {node.name})
                return
            if isinstance(node, ast.For):
                try:
                    values = cast(object, ast.literal_eval(node.iter))
                except (ValueError, TypeError):
                    values = None
                repeats = len(cast(list[object] | tuple[object, ...], values)) if isinstance(values, (list, tuple)) else 1
                if isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range":
                    try:
                        bounds = [cast(int, ast.literal_eval(arg)) for arg in node.iter.args]
                        if 1 <= len(bounds) <= 3 and all(type(bound) is int for bound in bounds):
                            repeats = len(range(*bounds))
                    except (ValueError, TypeError):
                        pass
                for child in node.body:
                    visit(child, multiplier * repeats, active)
                for child in node.orelse:
                    visit(child, multiplier, active)
                return
            if isinstance(node, ast.Call):
                code = ast.get_source_segment(content, node) or ""
                found = _code_result(code, code)
                if found is not None and found[0] == "shot":
                    count += multiplier
                    return
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in functions and node.id not in active):
                for child in functions[node.id].body:
                    visit(child, multiplier, active | {node.id})
                return
            for child in ast.iter_child_nodes(node):
                visit(child, multiplier, active)

        visit(tree, 1, frozenset())
        return count
    count = sum(_screenshot_calls(body, language == "python")
                for language, body in _heredoc_executions(content))
    loops: list[int] = []
    pending = 1
    shell_content = "\n".join(line.split(" #", 1)[0] for line in content.splitlines()
                              if not line.lstrip().startswith("#"))
    for tokens in _shell_segments(shell_content):
        if tokens[0] == "for" and "in" in tokens:
            values = tokens[tokens.index("in") + 1:]
            pending = len(values) if values and all(not value.startswith("$") for value in values) else 1
            continue
        if tokens[0] == "do":
            loops.append(pending)
            pending = 1
            tokens = tokens[1:]
        if tokens and tokens[0] == "done":
            if loops:
                _ = loops.pop()
            tokens = tokens[1:]
        if tokens and (found := _shell_classification(shlex.join(tokens))) is not None and found[0] == "shot":
            count += math.prod(loops)
    return max(1, count)


def _script_run(path: str, scripts: dict[str, RememberedScript], seen: set[str],
                arguments: tuple[str, ...] = (), cwd: str = "", source_context: str = "") -> ScriptRun | None:
    if path in seen:
        return None
    script = scripts.get(path)
    if script is None:
        return None
    seen.add(path)
    if script.classification_state == "unread":
        direct = None
        if any(hint in script.content for hint in SHELL_HINTS):
            direct = _code_result(script.content, script.content) if path.endswith(".py") else _shell_classification(script.content)
        if direct is None:
            script.classification_state = "not_related"
        else:
            script.classification_state = direct[0]
            script.source = direct[1]
            script.screenshot_calls = _screenshot_calls(script.content, path.endswith(".py")) if direct[0] == "shot" else 0
    key = arguments
    if source_context:
        selected = _selected_content(script.content, path, arguments, source_context)
    else:
        if key not in script.selected_content:
            script.selected_content[key] = _selected_content(script.content, path, arguments)
        selected = script.selected_content[key]
    if selected == script.content:
        direct = ((cast(CallKind, script.classification_state), script.source, selected)
                  if script.classification_state in {"shot", "other"} else None)
    else:
        direct = _code_result(selected, selected) if path.endswith(".py") else _shell_classification(selected)
    nested_invocations = (_python_invocations(selected, cwd or str(Path(path).parent), str(Path(path).parent), scripts)
                          if path.endswith(".py") else
                          _running_invocations(selected, cwd or str(Path(path).parent), scripts, str(Path(path).parent)))
    nested = [run for child in nested_invocations
              if (run := _script_run(child.path, scripts, seen.copy(), child.arguments, child.cwd,
                                     selected if child.mode == "source" else "")) is not None]
    candidates: list[tuple[CallKind, str]] = []
    if direct is not None:
        candidates.append((direct[0], direct[1]))
    candidates.extend((run.kind, run.source) for run in nested)
    if not candidates:
        return None
    kind, source = max(candidates, key=lambda item: (item[0] == "shot", item[1] == "hana_shot"))
    count = (script.screenshot_calls if selected == script.content else _screenshot_calls(selected, path.endswith(".py"))) if direct is not None and direct[0] == "shot" else 0
    count += sum(run.screenshot_calls for run in nested)
    return ScriptRun(kind, source, path, count, (path, *(child.path for child in nested)))


def _linked_run(command: str, cwd: str, scripts: dict[str, RememberedScript]) -> ScriptRun | None:
    matches = [
        run for invocation in _running_invocations(command, cwd, scripts)
        if (run := _script_run(invocation.path, scripts, set(), invocation.arguments, invocation.cwd,
                              command if invocation.mode == "source" else "")) is not None
    ]
    if not matches:
        return None
    preferred = max(matches, key=lambda run: (run.kind == "shot", run.source == "hana_shot"))
    return ScriptRun(preferred.kind, preferred.source, preferred.path,
                     sum(run.screenshot_calls for run in matches),
                     tuple(path for run in matches for path in run.paths))


def _result_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_result_text(item) for item in cast(list[object], value))
    if not isinstance(value, dict):
        return ""
    data = _object(cast(object, value))
    return " ".join(_result_text(data.get(key)) for key in (
        "text", "content", "output", "path", "file_path", "image_path", "image_paths", "saved_to", "images",
    ))


def _paths(value: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(PNG_PATH.findall(_result_text(value))))


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_message_text(item) for item in cast(list[object], value))
    data = _object(value)
    if not data:
        return ""
    return " ".join(_message_text(data.get(key)) for key in ("message", "summary", "content", "text"))


def _outgoing_tool_message(name: str, value: object, command: str) -> str:
    if name == "SendMessage" or name.endswith(".SendMessage"):
        return _message_text(value)
    if "codex_mesh.py" not in command or "--message" not in command:
        return ""
    messages: list[str] = []
    for tokens in _shell_segments(command):
        program, arguments = _program(tokens)
        if program == "codex_mesh.py":
            options = arguments[1:]
        elif program.startswith("python") and len(arguments) > 1 and Path(arguments[1]).name == "codex_mesh.py":
            options = arguments[2:]
        else:
            continue
        for index, option in enumerate(options):
            if option == "--message" and index + 1 < len(options):
                messages.append(options[index + 1])
            elif option.startswith("--message="):
                messages.append(option.partition("=")[2])
    return " ".join(messages)


def _path_in_message(path: str, message: str) -> bool:
    start = message.find(path)
    while start >= 0:
        end = start + len(path)
        before = message[start - 1] if start else ""
        after = message[end] if end < len(message) else ""
        if (not before or before not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./~:-") and (
            not after or after not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./~:-"
        ):
            return True
        start = message.find(path, start + 1)
    return False


def _citation_paths(message: str) -> tuple[str, ...]:
    candidates = (*cast(list[str], CITATION_SPACED_PATH.findall(message)),
                  *cast(list[str], PNG_PATH.findall(message)))
    return tuple(dict.fromkeys(path for path in candidates if _path_in_message(path, message)))


def _cite(citations: list[ImageCitation], position: int, message: str) -> None:
    paths = _citation_paths(message)
    if paths:
        citations.append(ImageCitation(position, paths))


def _write_text(name: str, value: object) -> str:
    data = _object(value)
    if name in {"Write", "Edit", "MultiEdit"}:
        edits = " ".join(_string(_object(item).get("new_string")) for item in _items(data.get("edits")))
        return " ".join((_string(data.get("content")), _string(data.get("new_string")), edits))
    if "apply_patch" in name:
        patch = _string(value) or _string(data.get("input"))
        return "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    command = _shell_command(name, value)
    if "*** Begin Patch" in command:
        return "\n".join(line[1:] for line in command.splitlines() if line.startswith("+") and not line.startswith("+++"))
    heredocs = _heredoc_writes(command, "") if "<<" in command else []
    if heredocs:
        return "\n".join(content for _, content, _ in heredocs)
    literal = TEXT_WRITE.search(command)
    if literal:
        return literal.group(2)
    if re.search(r"\b(?:echo|printf)\s+", command) and ">" in command:
        return command.split(">", 1)[0]
    if re.search(r"\b(?:echo|printf)\s+", command) and "| tee " in command:
        return command.split("| tee ", 1)[0]
    return ""


def _write_succeeded(output: object, is_error: object) -> bool:
    if is_error is True:
        return False
    result = _result_text(output).strip()
    if result.startswith(("Failed", "Error:")):
        return False
    return re.search(r"(?:exit code:|exited with code)\s*[1-9]\d*", result, re.IGNORECASE) is None


def _kept_calls(calls: list[ToolCall], citations: list[ImageCitation]) -> list[ToolCall]:
    ordered = sorted(enumerate(calls), key=lambda item: item[1].result_position)
    cited: dict[int, set[str]] = {}
    latest: dict[str, int] = {}
    next_call = 0
    for citation in sorted(citations, key=lambda item: item.position):
        while next_call < len(ordered) and ordered[next_call][1].result_position < citation.position:
            index, call = ordered[next_call]
            if call.kind == "shot":
                for path in call.image_paths:
                    latest[path] = index
            next_call += 1
        for path, index in latest.items():
            if path in citation.paths:
                cited.setdefault(index, set()).add(path)
    return [replace(call, cited_image_paths=tuple(path for path in call.image_paths
                                                   if path in cited.get(index, set())))
            for index, call in enumerate(calls)]


def _image_count(kind: CallKind, source: str, result: object, command: str, scripted_calls: int = 0) -> int:
    if kind != "shot":
        return 0
    if command or source == "hana_shot":
        paths = set(PNG_PATH.findall(_result_text(result)))
        if paths:
            return len(paths)
        return max(1, scripted_calls or _screenshot_calls(command))
    return 1


def _read_json(line: str) -> dict[str, object]:
    try:
        return _object(cast(object, json.loads(line)))
    except json.JSONDecodeError:
        return {}


def _matching_lines(path: Path, markers: tuple[bytes, ...]) -> Iterator[bytes]:
    args = ["rg", "--no-heading", "--no-line-number", "--text", "-F"]
    for marker in markers:
        args.extend(("-e", marker.decode()))
    with subprocess.Popen([*args, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        if process.stdout is None:
            raise RuntimeError(f"No output stream for {path}")
        for line in cast(Iterable[bytes], process.stdout):
            yield line.rstrip(b"\n")
        if process.wait() not in (0, 1):
            error = cast(bytes, process.stderr.read()).decode(errors="replace") if process.stderr is not None else ""
            raise RuntimeError(error)


def _claude_calls(path: Path, projects: dict[str, tuple[str, ProjectAttribution]],
                  matched_lines: Iterable[bytes] | None = None,
                  progress: ClaudeTranscriptProgress | None = None) -> TranscriptEvidence:
    state = progress if progress is not None else ClaudeTranscriptProgress()
    pending = state.pending_uses
    pending_writes = state.pending_writes
    known_cwds = state.known_cwds
    scripts = state.remembered_scripts
    script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
    script_names = tuple(name.encode() for name in script_names_text)
    script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
    calls: list[ToolCall] = []
    citations: list[ImageCitation] = []
    pending_citation_writes = state.pending_citations
    lines = matched_lines if matched_lines is not None else _matching_lines(path, (b'"tool_use"', b'"tool_result"', b'.png'))
    for position, line in enumerate(lines, state.next_position):
            state.next_position = position + 1
            if b'.png' in line and b'"tool_use"' not in line and b'"tool_result"' not in line:
                row = _read_json(line.decode("utf-8", errors="replace"))
                row_type = _string(row.get("type"))
                message = _object(row.get("message"))
                if row_type == "assistant":
                    content = message.get("content")
                    outgoing = (content if isinstance(content, str) else
                                " ".join(_string(_object(part).get("text")) for part in _items(content)
                                         if _object(part).get("type") == "text"))
                elif "checkpoint" in row_type or "checkpoint" in _string(row.get("subtype")) or (
                    "checkpoint" in _string(_object(row.get("data")).get("type"))
                ):
                    outgoing = _result_text(message) or _result_text(row.get("content"))
                else:
                    outgoing = ""
                if outgoing:
                    _cite(citations, position, outgoing)
                continue
            if b'"tool_use"' in line:
                if not (CLAUDE_HINT.search(line) or b'.png' in line
                        or b'"Write"' in line or b'"Edit"' in line
                        or script_name_pattern is not None and script_name_pattern.search(line)):
                    continue
            elif (pending or pending_writes or pending_citation_writes) and b'"tool_result"' in line:
                if not any(use_id.encode() in line for use_id in (*pending, *pending_writes, *pending_citation_writes)):
                    continue
            else:
                continue
            row = _read_json(line.decode("utf-8", errors="replace"))
            stamp = _stamp(row.get("timestamp"))
            if stamp is None:
                continue
            message = _object(row.get("message"))
            for raw_part in _items(message.get("content")):
                part = _object(raw_part)
                if row.get("type") == "assistant" and part.get("type") == "text":
                    _cite(citations, position, _string(part.get("text")))
                if part.get("type") == "tool_use":
                    name = _string(part.get("name"))
                    value = part.get("input")
                    command = _shell_command(name, value)
                    if b'.png' in line:
                        outgoing = _outgoing_tool_message(name, value, command)
                        if outgoing:
                            _cite(citations, position, outgoing)
                    cwd = _string(row.get("cwd"))
                    if cwd and "scratchpad" not in cwd:
                        known_cwds.add(cwd)
                    linked = _linked_run(command, cwd, scripts) if command and any(name in command for name in script_names_text) else None
                    classified = _classify(name, value)
                    if linked is not None and (classified is None or linked.kind == "shot" and classified[0] != "shot"):
                        classified = (linked.kind, linked.source, command)
                    use_id = _string(part.get("id"))
                    written = _write_text(name, value) if b'.png' in line else ""
                    references = _citation_paths(written) if written else ()
                    if references:
                        pending_citation_writes[use_id] = references
                    if name in {"Write", "Edit"}:
                        if _script_write_may_matter(name, value, cwd, scripts):
                            pending_writes[use_id] = ScriptWriteAwaitingResult(name, _compact_tool_input(value), cwd)
                    else:
                        _remember_tool(name, value, cwd, scripts)
                    if "<<" in command or name not in {"Write", "Edit"} and "rm" in command:
                        script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
                        script_names = tuple(name.encode() for name in script_names_text)
                        script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
                    if classified is None:
                        continue
                    kind, origin, classified_command = classified
                    project, attribution = _project(cwd, projects, known_cwds)
                    pending[use_id] = ClaudeToolUseAwaitingResult(
                        stamp, _string(row.get("sessionId")) or path.stem,
                        project, attribution, kind, origin, _without_inline_image_data(classified_command),
                        linked.path if linked is not None else "",
                        linked.screenshot_calls if linked is not None else 0,
                        linked.paths if linked is not None else (), cwd,
                    )
                elif part.get("type") == "tool_result":
                    use_id = _string(part.get("tool_use_id"))
                    references = pending_citation_writes.pop(use_id, ())
                    if references and _write_succeeded(part.get("content"), part.get("is_error")):
                        citations.append(ImageCitation(position, references))
                    write = pending_writes.pop(use_id, None)
                    if write is not None and part.get("is_error") is not True:
                        _remember_tool(write.name, write.value, write.cwd, scripts)
                        script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
                        script_names = tuple(name.encode() for name in script_names_text)
                        script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
                    use = pending.pop(use_id, None)
                    if use is None:
                        continue
                    calls.append(ToolCall(
                        use.start, stamp, use.session, str(path), "Claude", use.project, use.kind, use.source,
                        use.command, _image_count(use.kind, use.source, part.get("content"), use.command, use.script_calls),
                        use.attribution, use.script_path, use.script_paths, use.cwd,
                        _paths(part.get("content")) if use.kind == "shot" else (), (), position,
                    ))
    return TranscriptEvidence(calls, citations)


def _codex_calls(path: Path, projects: dict[str, tuple[str, ProjectAttribution]],
                 matched_lines: Iterable[bytes] | None = None,
                 progress: CodexTranscriptProgress | None = None) -> TranscriptEvidence:
    thread_suffix = CODEX_THREAD_SUFFIX.search(path.stem)
    fallback_session = thread_suffix.group(1) if thread_suffix is not None else path.stem
    state = progress if progress is not None else CodexTranscriptProgress(fallback_session, path.parent.name)
    pending = state.pending_uses
    scripts = state.remembered_scripts
    script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
    script_names = tuple(name.encode() for name in script_names_text)
    script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
    calls: list[ToolCall] = []
    citations: list[ImageCitation] = []
    pending_citation_writes = state.pending_citations
    session = state.session
    project = state.project
    attribution = state.attribution
    cwd = state.cwd
    lines = matched_lines if matched_lines is not None else _matching_lines(path, (b'"session_meta"', b'"response_item"', b'.png'))
    for position, line in enumerate(lines, state.next_position):
            state.next_position = position + 1
            if b'"session_meta"' not in line and b'"response_item"' not in line:
                if b'.png' in line:
                    row = _read_json(line.decode("utf-8", errors="replace"))
                    payload = _object(row.get("payload"))
                    if row.get("type") == "event_msg" and _string(payload.get("type")) in {"agent_message", "checkpoint", "task_complete"}:
                        _cite(citations, position, _result_text(payload.get("message")))
                continue
            if b'"session_meta"' not in line:
                if b'"function_call_output"' in line or b'"custom_tool_call_output"' in line:
                    if not pending and not pending_citation_writes or not any(
                        use_id.encode() in line for use_id in (*pending, *pending_citation_writes)
                    ):
                        continue
                elif b'"message"' in line and b'.png' in line:
                    row = _read_json(line.decode("utf-8", errors="replace"))
                    payload = _object(row.get("payload"))
                    if payload.get("type") == "message" and payload.get("role") == "assistant":
                        _cite(citations, position, _result_text(payload.get("content")))
                    continue
                elif not (CODEX_HINT.search(line) or b'.png' in line
                          or script_name_pattern is not None and script_name_pattern.search(line)):
                    continue
            row = _read_json(line.decode("utf-8", errors="replace"))
            stamp = _stamp(row.get("timestamp"))
            payload = _object(row.get("payload"))
            if row.get("type") == "session_meta":
                session = _string(payload.get("id")) or session
                cwd = _string(payload.get("cwd"))
                project, attribution = _project(cwd, projects)
                state.session, state.cwd, state.project, state.attribution = session, cwd, project, attribution
                continue
            if stamp is None or row.get("type") != "response_item":
                continue
            part_type = _string(payload.get("type"))
            if part_type in ("function_call", "custom_tool_call"):
                name = _string(payload.get("name"))
                raw_input = payload.get("arguments", payload.get("input"))
                if isinstance(raw_input, str) and raw_input.startswith("{"):
                    parsed = _read_json(raw_input)
                    raw_input = parsed if parsed else raw_input
                call_cwd = _string(_object(raw_input).get("workdir")) or cwd
                command = _shell_command(name, raw_input)
                if b'.png' in line:
                    outgoing = _outgoing_tool_message(name, raw_input, command)
                    if outgoing:
                        _cite(citations, position, outgoing)
                linked = _linked_run(command, call_cwd, scripts) if command and any(name in command for name in script_names_text) else None
                classified = _classify(name, raw_input)
                if linked is not None and (classified is None or linked.kind == "shot" and classified[0] != "shot"):
                    classified = (linked.kind, linked.source, command)
                _remember_tool(name, raw_input, call_cwd, scripts)
                written = _write_text(name, raw_input) if b'.png' in line else ""
                references = _citation_paths(written) if written else ()
                if references:
                    pending_citation_writes[_string(payload.get("call_id"))] = references
                if name.endswith("apply_patch") or "<<" in command:
                    script_names_text = tuple({os.path.basename(script_path) for script_path in scripts})
                    script_names = tuple(name.encode() for name in script_names_text)
                    script_name_pattern = re.compile(b"|".join(re.escape(name) for name in script_names)) if script_names else None
                if classified is None:
                    continue
                kind, origin, classified_command = classified
                pending[_string(payload.get("call_id"))] = CodexToolUseAwaitingResult(
                    stamp, kind, origin, _without_inline_image_data(classified_command),
                    linked.path if linked is not None else "",
                    linked.screenshot_calls if linked is not None else 0,
                    linked.paths if linked is not None else (), call_cwd,
                )
            elif part_type in ("function_call_output", "custom_tool_call_output"):
                use_id = _string(payload.get("call_id"))
                references = pending_citation_writes.pop(use_id, ())
                if references and _write_succeeded(payload.get("output"), payload.get("is_error")):
                    citations.append(ImageCitation(position, references))
                use = pending.pop(use_id, None)
                if use is None:
                    continue
                calls.append(ToolCall(
                    use.start, stamp, session, str(path), "Codex", project, use.kind, use.source,
                    use.command, _image_count(use.kind, use.source, payload.get("output"), use.command, use.script_calls),
                    attribution, use.script_path, use.script_paths, use.cwd,
                    _paths(payload.get("output")) if use.kind == "shot" else (), (), position,
                ))
    return TranscriptEvidence(calls, citations)


def _scan_file(file: tuple[Agent, Path]) -> TranscriptEvidence:
    agent, path = file
    projects: dict[str, tuple[str, ProjectAttribution]] = {}
    return _claude_calls(path, projects) if agent == "Claude" else _codex_calls(path, projects)


def _markers(agent: Agent) -> tuple[bytes, ...]:
    return ((b'"tool_use"', b'"tool_result"', b'.png') if agent == "Claude"
            else (b'"session_meta"', b'"response_item"', b'.png'))


def _has_candidate_hint(agent: Agent, line: bytes) -> bool:
    hints = CLAUDE_CANDIDATE_BYTES if agent == "Claude" else CODEX_CANDIDATE_BYTES
    return any(hint in line for hint in hints) or agent == "Codex" and RELATED_WRITE_BYTES.search(line) is not None


def _whole_file_candidate(path: Path, agent: Agent) -> bool:
    with path.open("rb") as source:
        return any(_has_candidate_hint(agent, line) for line in source)


def _new_progress(agent: Agent, path: Path) -> TranscriptProgress:
    if agent == "Claude":
        return ClaudeTranscriptProgress()
    thread_suffix = CODEX_THREAD_SUFFIX.search(path.stem)
    session = thread_suffix.group(1) if thread_suffix is not None else path.stem
    return CodexTranscriptProgress(session, path.parent.name)


def _parse_lines(path: Path, agent: Agent, lines: Iterable[bytes],
                 progress: TranscriptProgress) -> TranscriptEvidence:
    projects: dict[str, tuple[str, ProjectAttribution]] = {}
    if agent == "Claude" and isinstance(progress, ClaudeTranscriptProgress):
        return _claude_calls(path, projects, lines, progress)
    if agent == "Codex" and isinstance(progress, CodexTranscriptProgress):
        return _codex_calls(path, projects, lines, progress)
    raise ValueError("Transcript agent and parser progress disagree")


@dataclass
class TranscriptContentRead:
    cursor: int
    content_bytes: int = 0
    has_matching_line: bool = False


def _appended_lines(path: Path, agent: Agent, read: TranscriptContentRead) -> Iterator[bytes]:
    markers = _markers(agent)
    with path.open("rb") as source:
        _ = source.seek(read.cursor)
        while line := source.readline():
            read.content_bytes += len(line)
            if not line.endswith(b"\n"):
                break
            read.cursor = source.tell()
            if any(marker in line for marker in markers):
                read.has_matching_line = True
                yield line.rstrip(b"\n")


def _appended_candidate(path: Path, agent: Agent, read: TranscriptContentRead) -> bool:
    with path.open("rb") as source:
        _ = source.seek(read.cursor)
        while line := source.readline():
            read.content_bytes += len(line)
            if line.endswith(b"\n"):
                read.cursor = source.tell()
            if _has_candidate_hint(agent, line):
                return True
    return False


def _complete_cursor(path: Path, size: int) -> int:
    if size == 0:
        return 0
    with path.open("rb") as source:
        _ = source.seek(size - 1)
        if source.read(1) == b"\n":
            return size
        _ = source.seek(max(0, size - 65_536))
        tail_start = source.tell()
        tail = source.read()
    last_newline = tail.rfind(b"\n")
    return tail_start + last_newline + 1 if last_newline >= 0 else 0


def _new_file_evidence(file: tuple[Agent, Path, str], candidate: bool = True) -> CachedTranscriptEvidence:
    agent, path, source_host = file
    stat = path.stat()
    progress = _new_progress(agent, path)
    if not candidate:
        return CachedTranscriptEvidence(agent, source_host, stat.st_dev, stat.st_ino,
                                        _complete_cursor(path, stat.st_size), stat.st_size, stat.st_mtime_ns,
                                        TranscriptEvidence([], []), progress, False)
    complete = _complete_cursor(path, stat.st_size)
    if complete == stat.st_size:
        lines = _matching_lines(path, _markers(agent))
        evidence = _parse_lines(path, agent, lines, progress)
        cursor = stat.st_size
    else:
        read = TranscriptContentRead(0)
        evidence = _parse_lines(path, agent, _appended_lines(path, agent, read), progress)
        cursor = read.cursor
    evidence = TranscriptEvidence([replace(call, source_host=source_host) for call in evidence.calls],
                                  evidence.citations)
    return CachedTranscriptEvidence(agent, source_host, stat.st_dev, stat.st_ino, cursor, stat.st_size,
                                    stat.st_mtime_ns, evidence, progress, True)


def _append_file_evidence(prior: CachedTranscriptEvidence, path: Path, stat: os.stat_result) -> int:
    read = TranscriptContentRead(prior.cursor)
    fresh = _parse_lines(path, prior.agent, _appended_lines(path, prior.agent, read), prior.progress)
    prior.evidence.calls.extend(replace(call, source_host=prior.source_host) for call in fresh.calls)
    prior.evidence.citations.extend(fresh.citations)
    prior.device, prior.inode = stat.st_dev, stat.st_ino
    prior.cursor, prior.observed_size, prior.modified_ns = read.cursor, stat.st_size, stat.st_mtime_ns
    prior.candidate = prior.candidate or read.has_matching_line
    return read.content_bytes


def _file_inventory(claude_root: Path, codex_root: Path) -> dict[str, tuple[Agent, Path, os.stat_result]]:
    inventory: dict[str, tuple[Agent, Path, os.stat_result]] = {}
    for agent, root in (("Claude", claude_root), ("Codex", codex_root)):
        for directory, _, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = Path(directory) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                inventory[str(path)] = (cast(Agent, agent), path, stat)
    return inventory


def _joined_calls(evidence: Iterable[TranscriptEvidence], records: list[InvocationRecord]) -> list[ToolCall]:
    files = list(evidence)
    calls = [call for item in files for call in item.calls]
    calls = _recorded_calls(calls, records)
    offset = 0
    for item in files:
        count = len(item.calls)
        calls[offset:offset + count] = _kept_calls(calls[offset:offset + count], item.citations)
        offset += count
    live_names = {call.project for call in calls if call.project_attribution == "repository"}
    folded: list[ToolCall] = []
    for call in calls:
        if call.project_attribution in {"last_path", "scratchpad_target_missing"} and (
            call.project_attribution == "scratchpad_target_missing" or not Path(call.cwd).exists()
        ):
            matches = [name for name in live_names
                       if call.project == name or call.project.startswith(name + "-")
                       or call.project == re.sub(r"[^A-Za-z0-9]", "-", name)
                       or call.project.startswith(re.sub(r"[^A-Za-z0-9]", "-", name) + "-")]
            if matches:
                call = replace(call, project=max(matches, key=len), project_attribution="removed_worktree")
        folded.append(call)
    return folded


def _cached_scan(claude_root: Path, codex_root: Path, timing_source: TimingSource,
                 cache: PersistentScanCache, source_host: str) -> TranscriptScan:
    roots = (str(claude_root), str(codex_root))
    saved = _load_cache(cache, roots)
    inventory = _file_inventory(claude_root, codex_root)
    initial = not saved.files
    candidates: set[str] = ({str(path) for path in (*_candidate_files(claude_root), *_codex_candidate_files(codex_root))}
                            if initial else set())
    initial_results: dict[str, CachedTranscriptEvidence] = {}
    if candidates:
        tasks: list[tuple[Agent, Path, str]] = [(inventory[name][0], inventory[name][1], source_host)
                                                for name in candidates if name in inventory]
        tasks.sort(key=lambda item: inventory[str(item[1])][2].st_size, reverse=True)
        if sum(inventory[str(path)][2].st_size for _, path, _ in tasks) > PARALLEL_SCAN_BYTES:
            with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as workers:
                initial_results = dict(zip((str(path) for _, path, _ in tasks), workers.map(_new_file_evidence, tasks)))
        else:
            initial_results = {str(task[1]): _new_file_evidence(task) for task in tasks}
    changed = False
    # Count unique source byte offsets inspected in this run. Cold prefilter and parser
    # passes over the same file contribute its size once, even if both touch it.
    bytes_read = sum(stat.st_size for _, _, stat in inventory.values()) if initial else 0
    for name in set(saved.files) - set(inventory):
        del saved.files[name]
        changed = True
    for name, (agent, path, stat) in inventory.items():
        prior = saved.files.get(name)
        if prior is not None and (prior.device, prior.inode, prior.observed_size, prior.modified_ns) == (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        ):
            continue
        if (prior is not None and prior.device == stat.st_dev and prior.inode == stat.st_ino
                and stat.st_size > prior.observed_size):
            if prior.candidate:
                bytes_read += _append_file_evidence(prior, path, stat)
            else:
                read = TranscriptContentRead(prior.cursor)
                if _appended_candidate(path, agent, read):
                    saved.files[name] = _new_file_evidence((agent, path, source_host))
                    bytes_read += stat.st_size
                else:
                    prior.cursor, prior.observed_size, prior.modified_ns = read.cursor, stat.st_size, stat.st_mtime_ns
                    bytes_read += read.content_bytes
        elif name in initial_results:
            saved.files[name] = initial_results[name]
        elif initial:
            saved.files[name] = _new_file_evidence((agent, path, source_host), False)
        else:
            saved.files[name] = _new_file_evidence((agent, path, source_host), _whole_file_candidate(path, agent))
            bytes_read += stat.st_size
        changed = True
    if isinstance(timing_source, AvailableTimingSource):
        key = (timing_source.source_host, str(timing_source.path))
        prior_timing = saved.timings.get(key)
        if timing_source.path.exists():
            stat = timing_source.path.stat()
            if prior_timing is None or (prior_timing.device, prior_timing.inode, prior_timing.observed_size,
                                        prior_timing.modified_ns) != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns):
                append = (prior_timing is not None and prior_timing.device == stat.st_dev
                          and prior_timing.inode == stat.st_ino and stat.st_size > prior_timing.cursor)
                start = prior_timing.cursor if append and prior_timing is not None else 0
                new_records, cursor, count = _timing_invocations(timing_source.path, timing_source.source_host, start)
                records = [*prior_timing.records, *new_records] if append and prior_timing is not None else new_records
                saved.timings[key] = CachedTimingEvidence(stat.st_dev, stat.st_ino, cursor, stat.st_size,
                                                          stat.st_mtime_ns, records)
                bytes_read += count
                changed = True
    records = [record for timing in saved.timings.values() for record in timing.records]
    if changed:
        _save_cache(cache, saved)
    evidence = [item.evidence for item in saved.files.values()]
    calls = _joined_calls(evidence, records)
    candidate_count = sum(item.candidate for item in saved.files.values())
    return TranscriptScan(calls, candidate_count, bytes_read, records)


def scan_calls(claude_root: Path, codex_root: Path, timings_path: Path | TimingSource = NO_TIMING_SOURCE,
               cache: ScanCache = NO_SCAN_CACHE, source_host: str = "local") -> TranscriptScan:
    """Read transcript evidence; a persistent cache reads only appended content."""
    timing_source = (AvailableTimingSource(timings_path, source_host) if isinstance(timings_path, Path)
                     else timings_path)
    if isinstance(cache, PersistentScanCache):
        return _cached_scan(claude_root, codex_root, timing_source, cache, source_host)
    claude_files = _candidate_files(claude_root)
    codex_files = _codex_candidate_files(codex_root)
    files: list[tuple[Agent, Path]] = [*(('Claude', path) for path in claude_files), *(('Codex', path) for path in codex_files)]
    sized: list[tuple[int, Agent, Path]] = sorted(((path.stat().st_size, agent, path) for agent, path in files), reverse=True)
    ordered: list[tuple[Agent, Path]] = [(agent, path) for _, agent, path in sized]
    evidence: list[TranscriptEvidence] = []
    if sum(size for size, _, _ in sized) > PARALLEL_SCAN_BYTES:
        with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as workers:
            evidence.extend(workers.map(_scan_file, ordered))
    else:
        for file in ordered:
            evidence.append(_scan_file(file))
    evidence = [TranscriptEvidence([replace(call, source_host=source_host) for call in item.calls], item.citations)
                for item in evidence]
    records = (_timing_invocations(timing_source.path, timing_source.source_host)[0]
               if isinstance(timing_source, AvailableTimingSource) else [])
    calls = _joined_calls(evidence, records)
    content_bytes = sum(size for size, _, _ in sized)
    if isinstance(timing_source, AvailableTimingSource) and timing_source.path.exists():
        content_bytes += timing_source.path.stat().st_size
    return TranscriptScan(calls, len(files), content_bytes, records)


def survey_counts(calls: Iterable[ToolCall]) -> dict[str, tuple[int, int, int]]:
    groups: dict[str, tuple[int, set[str], set[str]]] = {}
    for call in calls:
        if call.kind != "shot":
            continue
        count, sessions, projects = groups.setdefault(call.source, (0, set(), set()))
        sessions.add(call.session_id)
        projects.add(call.project)
        groups[call.source] = (count + 1, sessions, projects)
    return {source: (count, len(sessions), len(projects)) for source, (count, sessions, projects) in groups.items()}
