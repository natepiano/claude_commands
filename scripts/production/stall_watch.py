#!/usr/bin/env python3
"""Bump idle unit directors and notify their showrunners once per stretch.

Notify the owner after PROMPT_ALERT_SECONDS at a permission prompt.
Send a phone alert after the grace period and open an RCA case.
Retire the alert when the prompt ends. Staff ids identify files and alert keys.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from socket import gethostname
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))

from scripts.lib import conf as tunables
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.lib.process_group import send_process_group

from scripts.production import showrunners, staff, session_staff
from scripts.production import unit_lookup
from scripts.production.session_pane import HerdrFailure, agent_status, read_place

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from scripts.hooks import context_usage  # noqa: E402
from scripts.notify import push_texts  # noqa: E402
from scripts.message import rca  # noqa: E402
from scripts.message import sessions  # noqa: E402

from scripts.herdr import herdr  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator

STATE_DIR = Path(os.environ.get("STALL_WATCH_STATE_DIR") or Path.home() / ".local/state/stall-watch")
PROJECTS_DIR = Path(os.environ.get("STALL_WATCH_PROJECTS_DIR") or Path.home() / ".claude/projects")
SESSIONS = Path(os.environ.get("STALL_WATCH_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SEND = Path(os.environ.get("STALL_WATCH_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
PS = os.environ.get("STALL_WATCH_PS") or "ps"
WAITING_KINDS = ("done", "blocked", "gate", "decision")
COMPACTION_WAKE_KIND = "blocked"
HOLDING_KIND = "holding"
TURN_END = re.compile(rf"^\s*(?:[●⏺]\s+)?(?P<status>(?:— )?(?P<kind>{'|'.join((*WAITING_KINDS, HOLDING_KIND))}):.*)$",
                      re.MULTILINE)
# A time a holding line names: a date, with or without its year, is optional, and UTC or GMT marks a time
# not in the production's zone.
HOLD_TIME = re.compile(r"\b(?:(?:(?P<year>\d{4})-)?(?P<month>\d{2})-(?P<day>\d{2})[ T])?"
                       + r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\b(?:\s+(?P<utc>UTC|GMT)\b)?")
WORK = {"zsh", "bash", "sh", "review.sh", "verify.sh"}
BLOCKED = herdr.AgentStatus.BLOCKED
PROMPT_TEXT_ID = "stall_watch.permission_prompt"
# The prompt alert's sender, which its retire names too.
SENDER = "stall-watch"
# The question of a Claude Code permission prompt: "Do you want to make this edit to panes.py?"
PROMPT_QUESTION = re.compile(r"Do you want to (?P<ask>[^?\n]{1,160})\?")
# "make this edit to panes.py" reads as "edit panes.py".
EDIT_ASK = re.compile(r"make (?:this|these) edits? to (?P<file>.+)")
BASH_PROMPT_TITLE = "Bash command"
# Claude Code's auto-mode banner on a prompt it denies by itself when a countdown ends, so no one has to answer:
# "⚠ Claude Code will automatically deny this request in 0:41, to avoid blocking progress on an unattended session."
AUTO_DENY_BANNER = re.compile(r"^⚠ Claude Code will automatically deny this request in \d+:\d{2}\b")
# A permission prompt fits in the pane's bottom 40 lines.
PROMPT_LINES = 40
# A request that reads "{unit} asks to go on with its work" when neither the transcript nor the screen names one.
UNKNOWN_REQUEST = "go on with its work"
# A Bash request shows its command cut to 89 characters and "…", as send.py and rca.py cut a refused command.
COMMAND_LENGTH = 90
FRAME = " \t│┃|╭╮╰╯─"
# Claude Code's warning under a command that reaches a path outside the session's folders:
# "<path>, which is outside the allowed working directories".
OUTSIDE_WARNING = ", which is outside the allowed working directories"
OUTSIDE_TARGET = re.compile(r"(\S+)" + re.escape(OUTSIDE_WARNING))
# A path with a folder, shown on the phone by its file name: "/tmp/claude/saved.txt" reads as "saved.txt".
LONG_PATH = re.compile(r"(?<![\w.~/])(?:~|\.{1,2})?/[^\s,;:'\"()]*")
# The first line of a Bash prompt's description is a sentence: "Read lines 5 to the end of a saved output".
SENTENCE_START = re.compile(r"[A-Z][a-z]+ ")
# A wrapped description goes on in lowercase words, with none of a command's shell characters.
SENTENCE_REST = re.compile(r"[a-z0-9][^|;&<>$'\"=/`]*")
COMMAND_BREAK = re.compile(r"\n|;|&&|\|\||\|")
# What a command does to the file outside the session's folders; any other program is "use".
OUTSIDE_VERBS = {"rm": "delete", "cp": "copy", "mv": "move"}
# Claude Code keeps a large tool output in the session's tool-results folder.
SAVED_OUTPUT_FOLDER = "tool-results"
# A file tool's request: its verb and the input that names its file.
FILE_TOOLS = {"Edit": ("edit", "file_path"), "MultiEdit": ("edit", "file_path"),
              "NotebookEdit": ("edit", "notebook_path"), "Write": ("write", "file_path")}


class Process(NamedTuple):
    pid: int
    parent: int
    command: str


class Stretch(TypedDict):
    pane_hash: str
    since: float
    bump_sent: bool
    tell_sent: bool
    reported_status: str


class PaneInspection(NamedTuple):
    production_name: str
    configured: showrunners.Showrunner
    zone: ZoneInfo
    members: tuple[str, ...]
    name: str
    unit: unit_lookup.LabeledSession
    row: staff.StaffRow


class PromptWait(TypedDict):
    session_id: str
    created: float
    key: str
    since: float
    alerted: bool
    # The pending tool call the prompt holds, or empty when the transcript names none.
    tool: str
    # The prompt is gone after its alert went out: the alert is retired before the unit gets a new one.
    retiring: bool
    # Successful owner delivery stops repeats; escalation uses the prompt start.
    told: NotRequired[float]
    # Delivered alert whose case must still open, even after the prompt and alert are gone.
    case_pending: NotRequired[rca.Report]


class Delivery(NamedTuple):
    state_path: Path
    kind: str
    command: list[str]
    # The prompt alert's facts: once the alert is sent, rca.py opens a case to fix why the prompt came up.
    report: rca.Report | None = None


class PromptCancellation(NamedTuple):
    request: Path
    session_id: str
    at: float


class CancellationBatch(NamedTuple):
    deliveries: list[Delivery]
    requests: list[PromptCancellation]


class CurrentPrompt(NamedTuple):
    value: PromptWait


class LegacyPrompt(NamedTuple):
    value: PromptWait


class InvalidPrompt(NamedTuple):
    reason: str
    key: str
    # Unreadable content might be a current record on the next pass.
    unreadable: bool


class MissingPrompt(NamedTuple):
    pass


# A pass is single-threaded; every consumer shares its diagnostic ledger.
PROMPT_WARNINGS: set[Path] = set()


def prompt_warning(path: Path, reason: str) -> None:
    if path not in PROMPT_WARNINGS:
        PROMPT_WARNINGS.add(path)
        print(f"stall-watch: prompt {path}: {reason}", file=sys.stderr)


class ContentBlock(TypedDict, total=False):
    type: str
    text: str
    id: str
    name: str
    input: dict[str, object]
    tool_use_id: str


class TranscriptMessage(TypedDict, total=False):
    content: list[ContentBlock] | str


class TranscriptEntry(TypedDict, total=False):
    type: str
    timestamp: str
    message: TranscriptMessage


class PendingTool(NamedTuple):
    """A tool call in the session's transcript with no result yet, and the time the session made it."""
    id: str
    name: str
    input: dict[str, object]
    started: float


class ProcessRows(list[Process]):
    """A successful process snapshot, including a proven empty snapshot."""


class ProcessSnapshotFailure(NamedTuple):
    reason: str


class CommandFailure(NamedTuple):
    reason: str


def finish_process(process: subprocess.Popen[str], work_deadline: float,
                   cleanup_deadline: float) -> subprocess.CompletedProcess[str] | CommandFailure:
    """Bound both pipe reads and reaping; kill only the group this watcher started."""
    try:
        out, err = process.communicate(timeout=max(0, work_deadline - time.monotonic()))
        return subprocess.CompletedProcess(process.args, process.returncode, out, err)
    except subprocess.TimeoutExpired:
        try:
            send_process_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            _ = process.communicate(timeout=max(0, cleanup_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # An inherited pipe must never turn cleanup into an unbounded read.
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            try:
                _ = process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        return CommandFailure("timeout")


def processes(*, deadline: float, limits: SenderLimits | None = None) -> ProcessRows | ProcessSnapshotFailure:
    """Unknown liveness is a failure, never an empty process table."""
    try:
        cleanup = (sender_configuration().limits if limits is None else limits).cleanup_seconds
        process = subprocess.Popen([PS, "-eo", "pid=,ppid=,args="], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        result = finish_process(process, deadline - cleanup, deadline)
    except (OSError, ValueError) as error:
        return ProcessSnapshotFailure(str(error))
    if isinstance(result, CommandFailure):
        return ProcessSnapshotFailure(result.reason)
    if result.returncode != 0:
        return ProcessSnapshotFailure(f"ps exited {result.returncode}: {failure_detail(result)}")
    rows = ProcessRows()
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if match:
            rows.append(Process(int(match.group(1)), int(match.group(2)), match.group(3)))
    return rows


def descendants(root: int, rows: list[Process]) -> list[Process]:
    children: dict[int, list[Process]] = {}
    for row in rows:
        children.setdefault(row.parent, []).append(row)
    found: list[Process] = []
    queue = [root]
    for parent in queue:
        for child in children.get(parent, []):
            found.append(child)
            queue.append(child.pid)
    return found


def process_session(pid: int) -> str:
    """Read the native session identity inherited by a process."""
    if sys.platform == "darwin":
        # The Mac has no /proc; herdr reads the environment from ps.
        try:
            values = herdr.darwin_process_environment(pid)
        except (HerdrFailure, subprocess.SubprocessError, OSError):
            return ""
    else:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return ""
        values = dict(item.decode(errors="replace").split("=", 1) for item in environment if b"=" in item)
    return values.get("CODEX_THREAD_ID", "") or values.get("CLAUDE_CODE_SESSION_ID", "")


def work_running(pid: int, rows: list[Process], session_id: str = "") -> bool:
    """Find work through descendants or a shared native session identity."""
    for row in descendants(pid, rows):
        if row.command.endswith(" <defunct>"):
            continue
        word = row.command.split(" ", 1)[0]
        if Path(word).name in WORK:
            return True
    if not session_id:
        return False
    parents = {row.pid: row.parent for row in rows}
    ancestors: set[int] = set()
    current = pid
    while current in parents and current not in ancestors:
        ancestors.add(current)
        current = parents[current]
    helpers = {row.pid for row in rows if row.parent == pid}
    helpers.update(child.pid for helper in tuple(helpers) for child in descendants(helper, rows))
    for row in rows:
        if row.pid in ancestors or row.pid in helpers or row.command.endswith(" <defunct>"):
            continue
        if process_session(row.pid) == session_id:
            return True
    return False


def latest_transcript_activity(session_id: str) -> float:
    latest = 0.0
    for transcript in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        latest = max(latest, transcript.stat().st_mtime)
    for directory in PROJECTS_DIR.glob(f"*/{session_id}/subagents"):
        for path in directory.rglob("*"):
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    return latest


def context_near_compaction(session_id: str) -> bool:
    """Wake at the context hook's shared handoff threshold using its bounded tail reading."""
    try:
        transcript = sessions.latest_transcript(session_id, PROJECTS_DIR)
        if transcript is None:
            return False
        reading = context_usage.latest_reading(transcript)
    except OSError:
        return False
    if reading is None:
        return False
    window = context_usage.auto_compact_window(reading["model"])
    return (window is not None
            and context_usage.estimate_tokens(reading, 0) >= context_usage.handoff_threshold(window))


def lines_from_end(path: Path, block: int = 1 << 20) -> Iterator[bytes]:
    """The file's lines, last first, read a block at a time from its end."""
    with path.open("rb") as file:
        end = file.seek(0, os.SEEK_END)
        rest = b""
        while end > 0:
            start = max(0, end - block)
            _ = file.seek(start)
            lines = (file.read(end - start) + rest).split(b"\n")
            rest = lines.pop(0) if start > 0 else b""
            yield from reversed(lines)
            end = start


def transcript_turn_end(session_id: str) -> re.Match[str] | None:
    """The turn-end line closing the session's last assistant text, read from its transcript.

    Read before the pane, whose text can be stale after resume or compaction. The pane supplies
    the fallback when the transcript has no usable turn end. A tool call after the text means
    the turn went on, so there is no turn-end line.
    """
    try:
        transcript = sessions.latest_transcript(session_id, PROJECTS_DIR)
        if transcript is None:
            return None
        for line in lines_from_end(transcript):
            if b'"assistant"' not in line:
                continue
            try:
                entry = cast(TranscriptEntry, json.loads(line))
            except ValueError:
                continue
            content = entry.get("message", {}).get("content") if entry.get("type") == "assistant" else None
            blocks = content if isinstance(content, list) else []
            if any(block.get("type") == "tool_use" for block in blocks):
                return None
            text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            if text:
                turns = list(TURN_END.finditer(text))
                return turns[-1] if turns else None
    except OSError:
        return None
    return None


def blocks_of(entry: TranscriptEntry) -> list[ContentBlock]:
    content = entry.get("message", {}).get("content")
    return content if isinstance(content, list) else []


def pending_tool(session_id: str) -> PendingTool | None:
    """The newest unanswered tool call in the session's transcript or in one of its helpers' transcripts.

    A helper (subagent) writes to <session id>/subagents/agent-*.jsonl, and its prompt shows in the
    session's pane, so its call is the one the prompt holds when it is the newest.
    """
    try:
        paths = [path for directory in PROJECTS_DIR.glob(f"*/{session_id}/subagents")
                 for path in directory.glob("agent-*.jsonl")]
        main = sessions.latest_transcript(session_id, PROJECTS_DIR)
    except OSError:
        return None
    calls = [call for path in ([main] if main else []) + paths if (call := transcript_pending(path)) is not None]
    return max(calls, key=lambda call: call.started, default=None)


def transcript_pending(transcript: Path) -> PendingTool | None:
    """The tool call that ends the transcript and has no result yet.

    A permission prompt holds the call from the moment the session makes it, so the call's time is
    when the prompt came up, also when herdr sees the prompt later.
    """
    try:
        answered: set[str] = set()
        for line in lines_from_end(transcript):
            if b'"tool_result"' not in line and b'"assistant"' not in line:
                continue
            try:
                entry = cast(TranscriptEntry, json.loads(line))
            except ValueError:
                continue
            if entry.get("type") == "user":
                answered.update(block.get("tool_use_id", "") for block in blocks_of(entry)
                                if block.get("type") == "tool_result")
                continue
            if entry.get("type") != "assistant":
                continue
            calls = [block for block in blocks_of(entry) if block.get("type") == "tool_use"]
            if not calls or calls[-1].get("id", "") in answered:
                return None
            call = calls[-1]
            try:
                started = datetime.fromisoformat(entry.get("timestamp", "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
            arguments = call.get("input")
            return PendingTool(call.get("id", ""), call.get("name", ""),
                               arguments if isinstance(arguments, dict) else {}, started)
    except OSError:
        return None
    return None


def hold_days(match: re.Match[str], today: date) -> list[date]:
    """The days a hold's time can fall on: the one it names, or each near today when it names no year or no date."""
    if not match["day"]:
        return [today + timedelta(days=offset) for offset in (-1, 0, 1)]
    years = [int(match["year"])] if match["year"] else [today.year + offset for offset in (-1, 0, 1)]
    days: list[date] = []
    for year in years:
        try:
            days.append(date(year, int(match["month"]), int(match["day"])))
        except ValueError:
            continue
    return days


def stated_hold_end(line: str, now: float, zone: ZoneInfo) -> float | None:
    """The latest time a holding line names, so a hold that states when it ends is not bumped before then.

    A time without a date, or a date without a year, is the occurrence nearest now: "until 01:00" read at
    23:00 is tomorrow, "since 23:50" read at 00:10 is yesterday, and "10-22 11:47" is the nearest 22 October.
    """
    instants: list[float] = []
    for match in HOLD_TIME.finditer(line):
        hour, minute = int(match["hour"]), int(match["minute"])
        if hour > 23 or minute > 59:
            continue
        place = ZoneInfo("UTC") if match["utc"] else zone
        candidates = [datetime.combine(day, clock(hour, minute), place).timestamp()
                      for day in hold_days(match, datetime.fromtimestamp(now, place).date())]
        if candidates:
            instants.append(min(candidates, key=lambda instant: abs(instant - now)))
    return max(instants, default=None)


class WatchedUnit(NamedTuple):
    production_name: str
    name: str
    stretch_path: Path
    prompt_path: Path
    prompt_key: str


def watched_staff_unit(production_name: str, unit: str, row: staff.StaffRow) -> WatchedUnit:
    path = STATE_DIR / f"{row.id}.json"
    return WatchedUnit(production_name, unit, path, path.with_suffix(".prompt.json"), f"stall-watch:prompt:{row.id}")


def prompt_alert_seconds() -> float:
    return tunables.read("production").number("PROMPT_ALERT_SECONDS")


def watch_config(*, status_defaults: bool = False, packaged_only: bool = False) -> dict[str, str]:
    """Read person-edited settings at use, without an import-time cache."""
    defaults = Path(__file__).resolve().parents[2] / "config/stall_watch.conf"
    path = defaults if packaged_only else Path(os.environ.get("STALL_WATCH_CONFIG") or defaults)
    paths = (defaults, path) if status_defaults and path != defaults else (path,)
    fields: dict[str, str] = {}
    for source in paths:
        try:
            content = source.read_text(encoding="utf-8")
        except FileNotFoundError:
            if status_defaults and source != defaults:
                continue
            raise
        for line in content.splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
    return fields


def name_spans(text: str, name: str) -> list[tuple[int, int]]:
    """Names exclude word and hyphen neighbors; sentence periods remain boundaries."""
    pattern = r"(?<![\w-])(?<!\w\.)" + re.escape(name) + r"(?![\w-]|\.\w)"
    return [match.span() for match in re.finditer(pattern, text, re.IGNORECASE)] if name else []


def names_outside(text: str, pattern: str, peers: Iterable[str], own_names: tuple[str, ...],
                  *, at_start: bool = False) -> bool:
    """An outside occurrence must extend beyond every complete own-name span."""
    own_spans = [span for own in own_names for span in name_spans(text, own)]
    candidates = [match.span() for match in re.finditer(pattern, text, re.IGNORECASE)]
    candidates.extend(span for peer in peers for span in name_spans(text, peer))
    return any((not at_start or start == 0) and not any(
        own_start <= start and end <= own_end for own_start, own_end in own_spans)
        for start, end in candidates)


def status_defers_bump(status: str, peers: Iterable[str], own_names: tuple[str, ...]) -> bool:
    """Explicit pauses and waits on another owner outrank a context-compaction wake."""
    fields = watch_config(status_defaults=True)
    if re.search(fields["paused_status_pattern"].strip(), status, re.IGNORECASE):
        return True
    waiting = re.search(fields["external_wait_pattern"].strip(), status, re.IGNORECASE)
    if waiting is None:
        return False
    owner = waiting.groupdict().get("owner")
    if owner is None or not owner.strip():
        raise ValueError("external_wait_pattern must capture a nonempty owner")
    owner = owner.strip()
    return names_outside(owner, fields["external_owner_pattern"].strip(), peers, own_names, at_start=True)


def holding_names_outside(status: str, peers: Iterable[str], own_names: tuple[str, ...]) -> bool:
    """A holding line must name an outside party, anywhere in the line."""
    pattern = watch_config(status_defaults=True)["holding_party_pattern"].strip()
    return names_outside(status, pattern, peers, own_names)


def prompt_grace_seconds() -> float:
    """Read the person-edited grace each tick; missing or invalid settings are reported by the caller."""
    fields = watch_config()
    try:
        grace = float(fields["prompt_grace_seconds"].strip())
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("expected a finite nonnegative number")
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid prompt_grace_seconds: {error}") from error
    return grace


def delivery_reserve_seconds() -> float:
    """Leave the configured part of the pass for alert delivery."""
    try:
        reserve = float(watch_config(status_defaults=True)["delivery_reserve_seconds"])
        if not math.isfinite(reserve) or reserve < 0:
            raise ValueError("expected a finite nonnegative number")
        return reserve
    except (OSError, KeyError, ValueError) as error:
        print(f"stall-watch: invalid delivery reserve: {error}; using zero reserve", file=sys.stderr)
        return 0.0


class SenderLimits(NamedTuple):
    batch_seconds: float
    cleanup_seconds: float


class SenderConfiguration(NamedTuple):
    limits: SenderLimits
    exit_code: int


def sender_limits(fields: dict[str, str]) -> SenderLimits:
    values: list[float] = []
    for key in ("sender_batch_seconds", "sender_cleanup_seconds"):
        try:
            value = float(fields[key])
        except (KeyError, ValueError) as error:
            raise ValueError(f"invalid {key}: expected a finite positive number") from error
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid {key}: expected a finite positive number")
        values.append(value)
    return SenderLimits(*values)


def sender_configuration() -> SenderConfiguration:
    """A bad override must not suppress pending work; retain failure in the pass result."""
    try:
        return SenderConfiguration(sender_limits(watch_config(status_defaults=True)), 0)
    except (OSError, ValueError) as error:
        print(f"stall-watch: sender config: {error}; using packaged defaults", file=sys.stderr)
        return SenderConfiguration(sender_limits(watch_config(packaged_only=True)), 1)


def inspection_order(work: list[PaneInspection]) -> list[PaneInspection]:
    """Start after the last pass's first attempt, even when that read exhausted its budget."""
    work = sorted(work, key=lambda item: (item.production_name, item.name))
    cursor = ("", "")
    try:
        value = cast(object, json.loads((STATE_DIR / "inspection.cursor").read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            raise ValueError("expected a production and unit")
        fields = cast(dict[str, object], value)
        production_name, unit = fields.get("name"), fields.get("unit")
        if not isinstance(production_name, str) or not production_name or not isinstance(unit, str) or not unit:
            raise ValueError("expected a production and unit")
        cursor = (production_name, unit)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        print(f"stall-watch: inspection cursor: {error}; starting at the first unit", file=sys.stderr)
    first = next((index for index, item in enumerate(work) if (item.production_name, item.name) > cursor), 0)
    return work[first:] + work[:first]


def retire_session_prompt(session_id: str) -> None:
    """Publish a cancellation request without taking the watcher lock or sending a message."""
    request = STATE_DIR / f"cancel-{uuid4().hex}.prompt.cancel.json"
    write_text_atomic(request, json.dumps({"session_id": session_id, "at": time.time()}) + "\n")


def matching_prompts(cancellation: PromptCancellation) -> tuple[list[tuple[Path, PromptWait]], bool]:
    """Return independently identified matches and any unresolved ownership obligation."""
    matches: list[tuple[Path, PromptWait]] = []
    unresolved = False
    for path in sorted(STATE_DIR.glob("*.prompt.json")):
        record = prompt_record(path)
        if isinstance(record, (InvalidPrompt, LegacyPrompt)):
            unresolved = True
        elif isinstance(record, CurrentPrompt):
            prompt = record.value
            if prompt["session_id"] == cancellation.session_id and prompt["created"] <= cancellation.at:
                matches.append((path, prompt))
    return matches, unresolved


def cancellation_requests() -> CancellationBatch:
    """Isolate every request's failures and retain it until all its old prompts are retired."""
    pending: dict[Path, Delivery] = {}
    requests: list[PromptCancellation] = []
    for request in sorted(STATE_DIR.glob("*.prompt.cancel.json")):
        try:
            raw = cast(object, json.loads(request.read_text(encoding="utf-8")))
            if not isinstance(raw, dict):
                raise ValueError("expected an object")
            data = cast(dict[str, object], raw)
            session_id, at = data.get("session_id"), data.get("at")
            if (not isinstance(session_id, str) or not session_id
                    or not isinstance(at, (int, float)) or isinstance(at, bool)):
                raise ValueError("expected a session_id and finite at")
            cancellation = PromptCancellation(request, session_id, float(at))
            if not math.isfinite(cancellation.at):
                raise ValueError("expected a finite at")
            matches, unresolved = matching_prompts(cancellation)
            if not matches and not unresolved:
                request.unlink()
                continue
            for path, prompt in matches:
                prompt["retiring"] = True
                save_prompt(path, prompt)
                pending[path] = retire_key(path, prompt["key"])
            requests.append(cancellation)
        except Exception as error:
            print(f"stall-watch: cancellation {request}: {error}; retained for retry", file=sys.stderr)
    return CancellationBatch(list(pending.values()), requests)


def finish_cancellations(requests: list[PromptCancellation]) -> None:
    """Remove a request only after delivery removed every matching prompt generation."""
    for cancellation in requests:
        try:
            matches, unresolved = matching_prompts(cancellation)
            if not matches and not unresolved:
                cancellation.request.unlink(missing_ok=True)
        except Exception as error:
            print(f"stall-watch: cancellation {cancellation.request}: {error}; retained for retry", file=sys.stderr)


def retire_key(path: Path, key: str) -> Delivery:
    command = push_texts.user_retire_argv(SENDER, key)
    return Delivery(path, "retire", [sys.executable, str(SEND), *command[2:]])


def end_prompt(watch: WatchedUnit) -> Delivery | None:
    """Retire the alert or remove the prompt state when the prompt ends."""
    path = watch.prompt_path
    record = prompt_record(path)
    if isinstance(record, InvalidPrompt):
        return retire_key(path, record.key) if record.key else None
    saved = record.value if isinstance(record, (CurrentPrompt, LegacyPrompt)) else None
    if saved is None or not saved["alerted"]:
        path.unlink(missing_ok=True)
        return None
    saved["retiring"] = True
    save_prompt(path, saved)
    return retire_key(path, saved["key"])


def forget(watch: WatchedUnit) -> Delivery | None:
    watch.stretch_path.unlink(missing_ok=True)
    return end_prompt(watch)


def finite_timestamp(value: object) -> float:
    """Accept JSON numbers only, with no boolean, overflow or non-finite clock values."""
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise ValueError("expected a finite timestamp")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("expected a finite timestamp")
    return result


def prompt_record(path: Path) -> CurrentPrompt | LegacyPrompt | InvalidPrompt | MissingPrompt:
    """Read and classify prompt state for observation, cancellation and case retries alike."""
    key = ""
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return MissingPrompt()
    except OSError as error:
        prompt_warning(path, f"{error}; unreadable state retained for retry")
        return InvalidPrompt(str(error), key, True)
    except (UnicodeError, ValueError) as error:
        prompt_warning(path, f"{error}; removing undecodable state with no recoverable key")
        return InvalidPrompt(str(error), key, False)
    try:
        if not isinstance(raw, dict):
            raise ValueError("expected a JSON object")
        old = cast(dict[str, object], raw)
        report = old.get("case_pending")
        report_fields = cast(dict[str, object], report) if isinstance(report, dict) else {}
        candidate = old.get("key", report_fields.get("key", ""))
        if isinstance(candidate, str):
            key = candidate
        session_id = old.get("session_id", "")
        if not isinstance(session_id, str) or not isinstance(candidate, str):
            raise ValueError("expected string session_id and key")
        since = finite_timestamp(old["since"])
        created = finite_timestamp(old.get("created", path.stat().st_mtime))
        if not isinstance(old["alerted"], bool) or not isinstance(old.get("retiring", False), bool):
            raise ValueError("expected boolean alerted and retiring")
        tool = old.get("tool", "")
        if not isinstance(tool, str):
            raise ValueError("expected string tool")
        prompt = PromptWait(session_id=session_id, created=created, key=key, since=since,
                            alerted=old["alerted"] is True, tool=tool, retiring=old.get("retiring") is True)
        if "told" in old:
            prompt["told"] = finite_timestamp(old["told"])
        if report is not None:
            if not isinstance(report, dict):
                raise ValueError("expected a case_pending object")
            fields = cast(dict[str, object], report)
            names = ("session", "machine", "kind", "transcript", "production", "stopped", "waits", "source", "key")
            if any(not isinstance(fields.get(name), str) for name in names):
                raise ValueError("expected string case_pending fields")
            commands = fields.get("commands")
            if not isinstance(commands, list) or any(not isinstance(item, str) for item in cast(list[object], commands)):
                raise ValueError("expected case_pending commands")
            if fields["kind"] not in ("prompt", "auto_mode"):
                raise ValueError("expected a case_pending kind")
            prompt["case_pending"] = rca.Report(
                session=str(fields["session"]), machine=str(fields["machine"]), kind=fields["kind"],
                commands=tuple(cast(list[str], commands)), transcript=str(fields["transcript"]),
                production=str(fields["production"]), stopped=str(fields["stopped"]), waits=str(fields["waits"]),
                source=str(fields["source"]), key=str(fields["key"]))
        if not session_id or not key or "created" not in old:
            prompt_warning(path, "legacy ownership; migrating identified state or retiring its known alert key")
            return LegacyPrompt(prompt)
        return CurrentPrompt(prompt)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        prompt_warning(path, f"{error}; retiring its known alert key or removing unusable state")
        return InvalidPrompt(str(error), key, False)


def saved_prompt(path: Path) -> PromptWait | None:
    record = prompt_record(path)
    return record.value if isinstance(record, (CurrentPrompt, LegacyPrompt)) else None


def reconcile_prompts() -> list[Delivery]:
    """Migrate recoverable legacy ownership; retire orphan keys even without live unit discovery."""
    pending: list[Delivery] = []
    for path in sorted(STATE_DIR.glob("*.prompt.json")):
        try:
            record = prompt_record(path)
            if isinstance(record, LegacyPrompt):
                prompt = record.value
                if prompt["session_id"] and prompt["key"]:
                    save_prompt(path, prompt)
                elif prompt["key"]:
                    pending.append(retire_key(path, prompt["key"]))
                else:
                    # There is no recoverable address for an external alert.
                    path.unlink(missing_ok=True)
            elif isinstance(record, InvalidPrompt) and not record.unreadable:
                if record.key:
                    pending.append(retire_key(path, record.key))
                else:
                    path.unlink(missing_ok=True)
        except OSError as error:
            prompt_warning(path, f"{error}; reconciliation retained for retry")
    return pending


def read_prompt(path: Path, now: float, session_id: str, key: str, observed_at: float, pending: PendingTool | None = None) -> PromptWait:
    """The wait at the prompt on screen now: the saved one while the unit stays blocked, else a new one.

    A pending tool call in the transcript starts the wait when the session made the call, which can be
    long before herdr first reports the pane blocked. A different pending call is a new prompt; when the
    old one alerted, that alert is retired first, since both alerts have the unit's one key.
    """
    saved = saved_prompt(path)
    if saved is not None:
        # Adopt older state during observation; cancellation itself never resolves missing ownership.
        if not saved["session_id"]:
            saved["session_id"] = session_id
            saved["key"] = key
        if saved["session_id"] != session_id:
            if saved["alerted"]:
                saved["retiring"] = True
            else:
                saved = None
    if saved is not None and saved["retiring"]:
        return saved
    if pending is None:
        return saved or PromptWait(session_id=session_id, created=observed_at, key=key,
                                   since=now, alerted=False, tool="", retiring=False)
    if saved is not None and saved["tool"] in ("", pending.id):
        saved["since"] = min(saved["since"], pending.started)
        saved["tool"] = pending.id
        return saved
    if saved is not None and saved["alerted"]:
        saved["retiring"] = True
        return saved
    return PromptWait(session_id=session_id, created=observed_at, key=key,
                      since=min(now, pending.started), alerted=False, tool=pending.id, retiring=False)


def save_prompt(path: Path, prompt: PromptWait) -> None:
    record: dict[str, object] = {**prompt}
    if "case_pending" in prompt:
        record["case_pending"] = prompt["case_pending"]._asdict()
    write_text_atomic(path, json.dumps(record) + "\n")


def auto_denied(pane: str) -> bool:
    """Whether the permission prompt at the pane's bottom shows the auto-deny banner."""
    return any(AUTO_DENY_BANNER.match(line.strip(FRAME)) for line in pane.splitlines()[-PROMPT_LINES:])


def file_name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def short_path(found: re.Match[str]) -> str:
    path = found.group(0)
    # "/tmp" alone names a top folder; "/tmp/x" and "~/x" read as "x".
    return path if path.count("/") == 1 and path.startswith("/") else file_name(path)


def short_paths(text: str) -> str:
    """The text with each path that has a folder shown by its file name, so it fits a phone screen."""
    return LONG_PATH.sub(short_path, text)


def described(description: str) -> str:
    """A Bash description worded to follow "run a shell command to": "Write X; read Y." reads "write X and read Y"."""
    text = " ".join(description.split()).rstrip(".").replace("; ", " and ")
    if text[:1].isupper() and text[1:2].islower():
        text = text[:1].lower() + text[1:]
    return f"run a shell command to {text}"


def program_words(segment: str) -> list[str]:
    """A command segment's words from its program on, past any leading variable settings."""
    words = segment.split()
    return words[next((index for index, word in enumerate(words) if "=" not in word), len(words)):]


def tool_request(pending: PendingTool) -> str | None:
    """What a pending Bash, edit or write call asks to do, read from its transcript entry."""
    arguments = pending.input
    if pending.name == "Bash":
        # The command itself, never its description: the user decides on what runs.
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return "run a shell command"
        shown = short_paths(" ".join(command.split()))
        return "run " + (shown if len(shown) <= COMMAND_LENGTH else shown[:COMMAND_LENGTH - 1] + "…")
    if pending.name not in FILE_TOOLS:
        return None
    verb, key = FILE_TOOLS[pending.name]
    path = arguments.get(key)
    return f"{verb} {file_name(path)}" if isinstance(path, str) else None


def bash_paragraphs(above: list[str]) -> list[list[str]] | None:
    """The paragraphs of the Bash prompt above the question, without its warning; None for another prompt."""
    above = [line for line in above if not AUTO_DENY_BANNER.match(line)]
    if BASH_PROMPT_TITLE not in above:
        return None
    paragraphs: list[list[str]] = [[]]
    for line in above[len(above) - above[::-1].index(BASH_PROMPT_TITLE):]:
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    return [paragraph for paragraph in paragraphs if paragraph and OUTSIDE_WARNING not in " ".join(paragraph)]


def screen_request(above: list[str], ask: str) -> str:
    """What the prompt on screen asks to do, read from its question, or from the Bash prompt above it."""
    edit = EDIT_ASK.fullmatch(ask)
    if edit is not None:
        return f"edit {edit['file']}"
    if ask != "proceed":
        return ask
    paragraphs = bash_paragraphs(above)
    if paragraphs is None:
        return UNKNOWN_REQUEST
    # The description is the sentence among the command lines; a wrapped one goes on in the lines after it.
    for paragraph in paragraphs:
        start = next((number for number, line in enumerate(paragraph) if SENTENCE_START.match(line)), None)
        if start is None:
            continue
        sentence = [paragraph[start]]
        for line in paragraph[start + 1:]:
            if not SENTENCE_REST.fullmatch(line):
                break
            sentence.append(line)
        return described(" ".join(sentence))
    return "run a shell command"


def outside_request(above: list[str], command: str) -> str | None:
    """A command the prompt's warning flags, named by what it does to the file outside the session's folders.

    That file is why the prompt asks, so it is what the user decides on.
    """
    found = OUTSIDE_TARGET.search(" ".join(above))
    if found is None:
        return None
    path = found.group(1)
    name = file_name(path)
    segment = next((part for part in COMMAND_BREAK.split(command) if name in part), "")
    words = program_words(segment)
    verb = OUTSIDE_VERBS.get(file_name(words[0]), "use") if words else "use"
    kind = "its saved tool output " if file_name(path.rsplit("/", 1)[0]) == SAVED_OUTPUT_FOLDER else ""
    return f"{verb} {kind}{name}, which is outside its allowed folders"


def prompt_request(pane: str, pending: PendingTool | None = None) -> str:
    """What the permission prompt asks to do, worded to follow "asks to".

    The pending tool call in the transcript names the request best; the prompt at the pane's bottom is
    read when there is none. A long path shows as its file name, and a Bash command is cut at COMMAND_LENGTH.
    """
    lines = [line.strip(FRAME) for line in pane.splitlines()[-PROMPT_LINES:]]
    asked = [(index, found) for index, line in enumerate(lines) if (found := PROMPT_QUESTION.search(line))]
    above = lines[:asked[-1][0]] if asked else []
    paragraphs = bash_paragraphs(above)
    if pending is None and paragraphs is not None:
        outside = outside_request(above, " ".join(line for paragraph in paragraphs for line in paragraph))
    else:
        outside = None
    request = outside or (tool_request(pending) if pending is not None else None)
    if request is None:
        if not asked:
            return UNKNOWN_REQUEST
        request = screen_request(above, asked[-1][1]["ask"].strip())
    return short_paths(request)


def machine_name() -> str:
    return gethostname().split(".", 1)[0]


def prompt_report(watch: WatchedUnit, session_id: str, pending: PendingTool | None, since: float) -> rca.Report:
    """Record the prompt request, wait time, sender, and key for the RCA case."""
    command = pending.input.get("command") if pending is not None else None
    if pending is None:
        commands: tuple[str, ...] = ()
    else:
        commands = (command,) if pending.name == "Bash" and isinstance(command, str) else (pending.name,)
    try:
        transcript = sessions.latest_transcript(session_id, PROJECTS_DIR)
    except OSError:
        transcript = None
    return rca.Report(watch.name, machine_name(), "prompt", commands, str(transcript or ""), watch.production_name,
                      stopped=datetime.fromtimestamp(since, ZoneInfo("UTC")).isoformat(), source=SENDER,
                      key=watch.prompt_key)


def prompt_alert(watch: WatchedUnit, pane: str, since: float, zone: ZoneInfo,
                 pending: PendingTool | None = None, report: rca.Report | None = None) -> Delivery:
    rendered = push_texts.render(PROMPT_TEXT_ID, unit=watch.name, machine=machine_name(),
                                 since=datetime.fromtimestamp(since, zone).strftime("%H:%M %Z"),
                                 request=prompt_request(pane, pending))
    # end_prompt() retires the alert when its prompt is gone.
    command = push_texts.user_send_argv(rendered, sender=SENDER, key=watch.prompt_key, self_clearing=True)
    return Delivery(watch.prompt_path, "alert", [sys.executable, str(SEND), *command[2:]], report)


def prompt_owner(watch: WatchedUnit, socket: str, pane: str, prompt: PromptWait,
                 pending: PendingTool | None) -> Delivery:
    """Tell the owner which prompt needs a response."""
    text = f"{watch.name} on {machine_name()} waits at a permission prompt: {prompt_request(pane, pending)}."
    key = f"{watch.prompt_key}:owner:{prompt['tool'] or prompt['since']}"
    return delivery(watch.prompt_path, "prompt-owner", socket, key, text)


def saved_stretch(path: Path) -> Stretch:
    old = cast(Stretch, json.loads(path.read_text(encoding="utf-8")))
    _ = old.setdefault("reported_status", "")
    return old


def read_stretch(path: Path, pane_hash: str, now: float) -> Stretch:
    try:
        old = saved_stretch(path)
        if (old["pane_hash"] == pane_hash or old["bump_sent"] or old["tell_sent"]
                or old.get("reported_status")):
            return old
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return Stretch(pane_hash=pane_hash, since=now, bump_sent=False, tell_sent=False,
                   reported_status="")


def save_stretch(path: Path, stretch: Stretch) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(stretch) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def delivery(path: Path, kind: str, socket: str, key: str, text: str) -> Delivery:
    return Delivery(path, kind, [sys.executable, str(SEND), "--to", f"uds:{socket}", "--from", "stall-watch",
                                 "--key", key, "--text", text])


def send_all(pending: list[Delivery], now: float, *, deadline: float = math.inf,
             limits: SenderLimits | None = None) -> None:
    """Start every relay and alert retire in its own group, with bounded batch and cleanup waits.

    After the batch, each prompt alert that went out opens its rca.py case. A retire that fails keeps
    its prompt state, so the next tick tries it again and sends no new alert.
    """
    if limits is None:
        limits = sender_configuration().limits
    batch_deadline = min(time.monotonic() + limits.batch_seconds, deadline - limits.cleanup_seconds)
    cleanup_deadline = min(deadline, batch_deadline + limits.cleanup_seconds)
    started: list[tuple[Delivery, subprocess.Popen[str]]] = []
    for item in pending:
        try:
            started.append((item, subprocess.Popen(item.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   text=True, start_new_session=True)))
        except OSError as error:
            print(f"stall-watch: {item.kind}: {error}", file=sys.stderr)
    for item, process in started:
        result = finish_process(process, batch_deadline, cleanup_deadline)
        if isinstance(result, CommandFailure):
            print(f"stall-watch: {item.kind}: send {result.reason}", file=sys.stderr)
            continue
        if result.returncode != 0:
            print(f"stall-watch: {item.kind}: {failure_detail(result)}", file=sys.stderr)
            continue
        if item.kind == "retire":
            prompt = saved_prompt(item.state_path)
            if prompt is not None and "case_pending" in prompt:
                # Move the saved report atomically so new prompts never wait for an old case.
                case_id = hashlib.sha256(json.dumps(prompt["case_pending"]._asdict(), sort_keys=True).encode()).hexdigest()
                _ = item.state_path.replace(STATE_DIR / f"{case_id}.case.json")
            else:
                item.state_path.unlink(missing_ok=True)
            continue
        if item.kind == "alert":
            prompt = saved_prompt(item.state_path)
            if prompt is None:
                continue
            prompt["alerted"] = True
            if item.report is not None:
                prompt["case_pending"] = item.report
            save_prompt(item.state_path, prompt)
            continue
        if item.kind == "prompt-owner":
            prompt = saved_prompt(item.state_path)
            if prompt is None:
                continue
            prompt["told"] = now
            save_prompt(item.state_path, prompt)
            continue
        stretch = cast(Stretch, json.loads(item.state_path.read_text(encoding="utf-8")))
        if item.kind == "bump":
            stretch["bump_sent"] = True
        else:
            stretch["tell_sent"] = True
        save_stretch(item.state_path, stretch)
    retry_cases(now, deadline=deadline)


def retry_cases(now: float, *, deadline: float = math.inf) -> None:
    """Finish case creation independently of whether its prompt or sent alert still exists."""
    for path in [*STATE_DIR.glob("*.prompt.json"), *STATE_DIR.glob("*.case.json")]:
        prompt = saved_prompt(path)
        if prompt is None or "case_pending" not in prompt:
            continue
        result = rca.open_case(prompt["case_pending"], datetime.fromtimestamp(now, ZoneInfo("UTC")), defer_owner=True, deadline=deadline)
        print(result)
        if result.startswith(("rca: opened ", "rca: joined ")):
            del prompt["case_pending"]
            if path.name.endswith(".case.json"):
                path.unlink(missing_ok=True)
            else:
                save_prompt(path, prompt)


def pane_budget_left(name: str, deadline: float) -> bool:
    """Expired inspection never changes a unit's saved prompt or idle stretch."""
    if time.monotonic() < deadline:
        return True
    print(f"stall-watch: {name}: pane inspection deadline exhausted; state retained", file=sys.stderr)
    return False


@dataclass
class InspectionReport:
    """The decision printed for one attempted pane inspection."""

    decision: str = "inspection incomplete"


@contextmanager
def inspection_report(production_name: str, name: str) -> Generator[InspectionReport, None, None]:
    """Write one log line without entering a delivery path."""
    report = InspectionReport()
    try:
        yield report
    finally:
        print(f"inspected {production_name}/{name}: {report.decision}")


def tick(now: float, *, deadline: float = math.inf) -> int:
    with herdr.pane_snapshot() as read_panes:
        return watch_pass(now, deadline=deadline, read_panes=read_panes)


def watch_pass(now: float, *, deadline: float, read_panes: Callable[[str], list[herdr.PaneIdentity]]) -> int:
    deadline = min(deadline, time.monotonic() + 110)
    PROMPT_WARNINGS.clear()
    pending = reconcile_prompts()
    cancellations = cancellation_requests()
    pending.extend(cancellations.deliveries)
    settings = showrunners.load_settings()
    alert_seconds = prompt_alert_seconds()
    try:
        grace = prompt_grace_seconds()
    except (OSError, ValueError) as error:
        print(f"stall-watch: using zero prompt grace: {error}", file=sys.stderr)
        grace = 0
    inspection_deadline = deadline - delivery_reserve_seconds()
    sender = sender_configuration()
    rows = processes(deadline=inspection_deadline, limits=sender.limits)
    snapshot_failed = isinstance(rows, ProcessSnapshotFailure)
    if isinstance(rows, ProcessSnapshotFailure):
        print(f"stall-watch: process snapshot: {rows.reason}", file=sys.stderr)
    runners = showrunners.ShowrunnerDiscovery()
    record = staff.StaffSnapshot(())
    work: list[PaneInspection] = []
    members_by_parent: dict[staff.ReportsTo | staff.StaffRoot, tuple[str, ...]] = {}
    try:
        record = staff.snapshot()
        sessions = unit_lookup.every_labeled_session(record=record)
    except (OSError, UnicodeError) as error:
        print(f"stall-watch: units: their run state is unknown: {error}", file=sys.stderr)
    else:
        runners = showrunners.registered_showrunners(deadline=inspection_deadline, membership=record)
        for configured in runners:
            # A showrunner that is not running has no one to tell: its units are not watched.
            showrunner_socket = configured["socket"]
            if not showrunner_socket:
                continue
            try:
                zone = ZoneInfo(configured["zone"])
            except (KeyError, ValueError):
                print(f"stall-watch: invalid zone for {configured['name']}: {configured['zone']}", file=sys.stderr)
                continue
            try:
                production_name = configured["name"]
                marked = sessions.status_units(production_name)
                for name, unit in marked.items():
                    if isinstance(unit.identity, session_staff.NoSession):
                        raise staff.StaffUnreadable(f"{name}: native runtime identity is unavailable")
                    row = unit_lookup.unit_row(name, record, reference=unit.identity)
                    if row.parent not in members_by_parent:
                        members_by_parent[row.parent] = tuple(member.name
                            for member in (record.find(kind="unit", parent=row.parent.id) if isinstance(row.parent, staff.ReportsTo) else ())
                            if not isinstance(member.worktree, staff.RemovedWorktree))
                    work.append(PaneInspection(production_name, configured, zone, members_by_parent[row.parent], name, unit, row))
            except (OSError, UnicodeError, ValueError) as error:
                print(f"stall-watch: {configured['name']}: its units are not watched: {error}", file=sys.stderr)
                continue
    runs = unit_lookup.HeldRunLookup(record=record)
    inspection_started = False
    labels_by_session: dict[str, dict[str, str]] = {}
    for production_name, configured, zone, members, name, unit, row in inspection_order(work):
        if isinstance(rows, ProcessSnapshotFailure):
            continue
        showrunner_socket = configured["socket"]
        watch = watched_staff_unit(production_name, name, row)
        if any(item.state_path == watch.prompt_path for item in pending):
            continue
        if not pane_budget_left(name, inspection_deadline):
            continue
        # The shared lookup follows recorded director changes, not stale worktree paths.
        run = runs.unfinished_held_run(row.current_session)
        if isinstance(run, unit_lookup.HeldRunUnknown):
            print(f"inspected {production_name}/{name}: run unknown: {run.path}: {run.reason}")
            continue
        if not pane_budget_left(name, inspection_deadline):
            continue
        if isinstance(run, unit_lookup.NoUnfinishedRun):
            if (retire := forget(watch)) is not None:
                pending.append(retire)
            continue
        claude = unit.claude
        if not isinstance(claude, unit_lookup.LiveClaude):
            if isinstance(claude, unit_lookup.ClaudeUnknown):
                print(f"stall-watch: {name}: {claude.reason}", file=sys.stderr)
            # A prompt is not on screen without its Claude.
            elif (retire := end_prompt(watch)) is not None:
                pending.append(retire)
            continue
        observed_at = time.time()
        path = watch.stretch_path
        if isinstance(prompt_record(watch.prompt_path), InvalidPrompt):
            continue
        if not pane_budget_left(name, inspection_deadline):
            continue
        if not inspection_started:
            # Progress belongs to the attempted read, independent of its alert state or outcome.
            write_text_atomic(STATE_DIR / "inspection.cursor", json.dumps({"name": production_name, "unit": name}) + "\n")
            inspection_started = True
        with inspection_report(production_name, name) as report:
            if unit.place.session not in labels_by_session:
                # Discovery already read these panes. A missing label must not prevent prompt checks.
                panes = read_panes(unit.place.session)
                labels_by_session[unit.place.session] = {pane["pane_id"]: pane.get("label") or "" for pane in panes}
                if not panes:
                    print(f"stall-watch: {unit.place.session}: pane labels unavailable", file=sys.stderr)
            labels = labels_by_session[unit.place.session]
            try:
                pane = read_place(unit.place, 400, joined=True, deadline=inspection_deadline).strip()
            except (OSError, HerdrFailure) as error:
                print(f"stall-watch: {name}: {error}", file=sys.stderr)
                continue
            pid, session_id, socket = claude.pid, claude.session_id, claude.socket
            waiting = watch.prompt_path
            # None: herdr cannot say, so the unit is watched as not blocked, and its prompt is not ended.
            blocked: bool | None
            if not pane_budget_left(name, inspection_deadline):
                continue
            try:
                state = agent_status(unit.place, deadline=inspection_deadline)
                blocked = None if state == herdr.AgentStatus.UNKNOWN else state == BLOCKED
            except (OSError, HerdrFailure) as error:
                print(f"stall-watch: {name}: its herdr state is unknown: {error}", file=sys.stderr)
                if time.monotonic() >= inspection_deadline:
                    continue
                blocked = None
            if not pane_budget_left(name, inspection_deadline):
                continue
            if blocked:
                report.decision = "permission prompt"
                # It waits on the user, not on itself: the idle clock starts again after the prompt.
                save_stretch(path, Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                           bump_sent=False, tell_sent=False, reported_status=""))
                # Claude Code denies a prompt with the auto-deny banner by itself: the user gets no alert.
                if auto_denied(pane):
                    if (retire := end_prompt(watch)) is not None:
                        pending.append(retire)
                    continue
                call = pending_tool(session_id)
                prompt = read_prompt(waiting, now, session_id, watch.prompt_key, observed_at, call)
                save_prompt(waiting, prompt)
                if prompt["retiring"]:
                    pending.append(retire_key(waiting, prompt["key"]))
                elif not prompt["retiring"] and now - prompt["since"] >= alert_seconds:
                    if "told" not in prompt:
                        pending.append(prompt_owner(watch, showrunner_socket, pane, prompt, call))
                    if not prompt["alerted"] and now - prompt["since"] >= alert_seconds + grace:
                        pending.append(prompt_alert(watch, pane, prompt["since"], zone, call,
                                                    prompt_report(watch, session_id, call, prompt["since"])))
                continue
            if blocked is not None and (retire := end_prompt(watch)) is not None:
                pending.append(retire)
            stretch = read_stretch(path, hashlib.sha256(pane.encode()).hexdigest(), now)
            turns = list(TURN_END.finditer(pane))
            # A resumed or compacted pane can retain an older turn. The session's last
            # assistant text is authoritative when it supplies a turn-end status.
            turn = transcript_turn_end(session_id) or (turns[-1] if turns else None)
            last = turn.group("status").strip() if turn else "none on screen"
            peers = (*(member for member in members if member != row.name),
                     configured["session"], production_name)
            own_names = (name, unit.unit, claude.name, labels.get(unit.place.pane, ""), f"session:{session_id}")
            try:
                deferred = bool(turn) and status_defers_bump(last, peers, own_names)
                holding = turn is not None and turn.group("kind") == HOLDING_KIND and holding_names_outside(last, peers, own_names)
            except (OSError, UnicodeError, KeyError, ValueError, re.error, IndexError) as error:
                print(f"stall-watch: {name}: no bump: invalid status rule: {error}", file=sys.stderr)
                continue
            # A hold that states when it ends waits like a gate until then; the idle clock starts after it.
            held_until = (stated_hold_end(turn.group(0), now, zone)
                          if turn and turn.group("kind") == HOLDING_KIND else None)
            if deferred or (turn and (turn.group("kind") in WAITING_KINDS or (held_until is not None and held_until > now))
                            and not (turn.group("kind") == COMPACTION_WAKE_KIND and context_near_compaction(session_id))):
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
                save_stretch(path, stretch)
                report.decision = "waiting"
                continue
            running_work = work_running(pid, rows, session_id)
            if running_work or (stretch["reported_status"] and last != stretch["reported_status"]):
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
            elif not stretch["reported_status"]:
                stretch["since"] = max(stretch["since"], min(latest_transcript_activity(session_id), now))
            save_stretch(path, stretch)
            since = stretch["since"]
            if now - since < settings["stall_minutes"] * 60 or running_work:
                report.decision = "running" if running_work else "fresh"
                continue
            since_text = datetime.fromtimestamp(since, zone).strftime("%H:%M %Z")
            key = f"stall-watch:{row.id}:{int(since)}"
            if not stretch["reported_status"]:
                stretch["reported_status"] = last
                save_stretch(path, stretch)
            report.decision = "holding" if holding else "idle already bumped"
            if not holding and not stretch["bump_sent"]:
                report.decision = "idle bump requested"
                text = (f"stall-watch: you have been idle since {since_text} with nothing running. "
                        "Continue your run; if you are waiting on someone, say on whom in one line.")
                pending.append(delivery(path, "bump", socket, f"{key}:bump", text))
            if not holding and not stretch["tell_sent"] and showrunner_socket:
                text = f"{name} idle since {since_text}, nothing running; bumped. Last status: {last}"
                pending.append(delivery(path, "tell", showrunner_socket, f"{key}:tell", text))
    send_all(pending, now, deadline=deadline, limits=sender.limits)
    finish_cancellations(cancellations.requests)
    return max(runners.exit_code, int(snapshot_failed), sender.exit_code)


def migrate_staff_ids() -> int:
    """Move known files and replace alert keys without changing their elapsed time."""
    record = staff.snapshot()
    targets: dict[str, tuple[staff.StaffRow, str]] = {}
    for row in record.find(kind="unit"):
        parent = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
        if parent is None:
            continue
        targets[str(row.id)] = (row, parent.name)
        digest = hashlib.sha256(f"{parent.name}\0{row.name}".encode()).hexdigest()
        targets[digest] = (row, parent.name)
    result = 0
    for source in sorted(STATE_DIR.glob("*.json")):
        if source.name.endswith(".case.json"):
            continue
        stem = source.name.removesuffix(".json").removesuffix(".prompt")
        if stem not in targets:
            print(f"stall-watch: unmatched file: {source}", file=sys.stderr)
            result = 1
            continue
        row, production_name = targets[stem]
        suffix = ".prompt.json" if source.name.endswith(".prompt.json") else ".json"
        target = STATE_DIR / f"{row.id}{suffix}"
        if source != target:
            if target.exists():
                raise ValueError(f"{source}: destination exists: {target}")
            _ = source.replace(target)
        if suffix == ".json":
            continue
        prompt = saved_prompt(target)
        if prompt is None:
            raise ValueError(f"{target}: no prompt record")
        key = f"stall-watch:prompt:{row.id}"
        if prompt["key"] == key:
            continue
        if prompt["alerted"]:
            retire = retire_key(target, prompt["key"])
            _ = subprocess.run(retire.command, check=True, timeout=30)
            if not prompt["retiring"]:
                alert = prompt_alert(watched_staff_unit(production_name, row.name, row), "", prompt["since"], ZoneInfo("UTC"))
                _ = subprocess.run(alert.command, check=True, timeout=30)
        prompt["key"] = key
        if "case_pending" in prompt:
            prompt["case_pending"] = prompt["case_pending"]._replace(key=key)
        save_prompt(target, prompt)
    return result


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if sys.argv[1:] == ["migrate-staff-ids"]:
            return migrate_staff_ids()
        began = time.monotonic()
        now = float(os.environ.get("STALL_WATCH_NOW_EPOCH") or time.time())
        exit_code = tick(now, deadline=began + 110)
        # The pass also reminds the owner of each permission case still open after 30 minutes.
        rca.remind(datetime.fromtimestamp(now, ZoneInfo("UTC")), deadline=began + 110)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"stall-watch: {error}", file=sys.stderr)
        raise SystemExit(1) from error
