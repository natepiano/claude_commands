#!/usr/bin/env python3
"""Bump idle unit directors and tell their running showrunners once per stretch.

A unit that herdr reports `blocked` waits at a permission, trust or question prompt: no bump can
move it, so after PROMPT_ALERT_SECONDS the user gets one phone alert per prompt instead. The alert's
request and wait start come from the session's transcript when it holds the pending tool call.
Once that alert is sent, rca.py opens a case so the cause of the prompt is found and fixed, and each
pass reminds the owner of a case still open after 30 minutes (`rca.remind`). The alert is an emergency
push that repeats until the user acknowledges it, so when its prompt is gone `send.py retire` cancels it
by its per-unit key.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from socket import gethostname
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

import production_units
import showrunners
import unit_lookup
from session_pane import HerdrFailure, agent_status, read_place

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
import push_texts  # noqa: E402
import rca  # noqa: E402
import sessions  # noqa: E402

import herdr  # noqa: E402  session_pane puts scripts/herdr on the path

if TYPE_CHECKING:
    from collections.abc import Iterator

STATE_DIR = Path(os.environ.get("STALL_WATCH_STATE_DIR") or Path.home() / ".local/state/stall-watch")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
PROJECTS_DIR = Path(os.environ.get("STALL_WATCH_PROJECTS_DIR") or Path.home() / ".claude/projects")
SESSIONS = Path(os.environ.get("STALL_WATCH_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SEND = Path(os.environ.get("STALL_WATCH_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
PS = os.environ.get("STALL_WATCH_PS") or "ps"
WAITING_KINDS = ("done", "blocked", "gate", "decision")
HOLDING_KIND = "holding"
TURN_END = re.compile(rf"^\s*(?:[●⏺]\s+)?(?P<status>(?:— )?(?P<kind>{'|'.join((*WAITING_KINDS, HOLDING_KIND))}):.*)$",
                      re.MULTILINE)
# A time a holding line names: a date, with or without its year, is optional, and UTC or GMT marks a time
# not in the production's zone.
HOLD_TIME = re.compile(r"\b(?:(?:(?P<year>\d{4})-)?(?P<month>\d{2})-(?P<day>\d{2})[ T])?"
                       + r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\b(?:\s+(?P<utc>UTC|GMT)\b)?")
WORK = {"zsh", "bash", "sh", "implement.sh", "review.sh", "verify.sh"}
BLOCKED = "blocked"
PROMPT_ALERT_SECONDS = 60
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


class PromptWait(TypedDict):
    since: float
    alerted: bool
    # The pending tool call the prompt holds, or empty when the transcript names none.
    tool: str
    # The prompt is gone after its alert went out: the alert is retired before the unit gets a new one.
    retiring: bool


class Delivery(NamedTuple):
    state_path: Path
    kind: str
    command: list[str]
    # The prompt alert's facts: once the alert is sent, rca.py opens a case to fix why the prompt came up.
    report: rca.Report | None = None


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


def command_output(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OSError(f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def processes() -> list[Process]:
    rows: list[Process] = []
    for line in command_output([PS, "-eo", "pid=,ppid=,args="]).splitlines():
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


def unit_marks(pid: int) -> tuple[str, str] | None:
    """The production and unit a process's environment marks it with, inherited from its unit's session."""
    if sys.platform == "darwin":
        # The Mac has no /proc; herdr reads the environment from ps.
        try:
            values = herdr.darwin_process_environment(pid)
        except (HerdrFailure, subprocess.SubprocessError, OSError):
            return None
    else:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return None
        values = dict(item.decode(errors="replace").split("=", 1) for item in environment if b"=" in item)
    slug, unit = values.get(unit_lookup.PRODUCTION_MARK, ""), values.get(unit_lookup.UNIT_MARK, "")
    return (slug, unit) if slug and unit else None


def work_running(pid: int, rows: list[Process], marks: tuple[str, str] | None = None) -> bool:
    """A shell or launcher under the unit's Claude, or any process the unit started that left its tree.

    A process carrying the unit's marks counts wherever it sits, except the Claude, the pane around
    it, and the long-lived helpers the Claude starts itself (its MCP servers).
    """
    for row in descendants(pid, rows):
        if row.command.endswith(" <defunct>"):
            continue
        word = row.command.split(" ", 1)[0]
        if Path(word).name in WORK:
            return True
    if marks is None:
        return False
    parents = {row.pid: row.parent for row in rows}
    ancestors: set[int] = set()
    current = pid
    while current in parents and current not in ancestors:
        ancestors.add(current)
        current = parents[current]
    helpers = {row.pid for row in rows if row.parent == pid}
    helpers.update(child.pid for helper in tuple(helpers) for child in descendants(helper, rows))
    current_slugs: dict[str, str] = {}
    for row in rows:
        if row.pid in ancestors or row.pid in helpers or row.command.endswith(" <defunct>"):
            continue
        inherited = unit_marks(row.pid)
        if inherited is None or inherited[1] != marks[1]:
            continue
        slug = inherited[0]
        if slug not in current_slugs:
            current_slugs[slug] = showrunners.canonical_slug(slug)
        if (current_slugs[slug], inherited[1]) == marks:
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

    Compaction clears the pane, so a unit that ended its turn and then compacted while idle shows no
    turn-end line on screen; its transcript still ends with that text. A tool call after the text
    means the turn went on, so there is no turn-end line.
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


def stretch_path(slug: str, unit: str) -> Path:
    name = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
    return STATE_DIR / f"{name}.json"


def prompt_path(slug: str, unit: str) -> Path:
    return stretch_path(slug, unit).with_suffix(".prompt.json")


def prompt_key(slug: str, unit: str) -> str:
    """The key of the unit's prompt alert, which `send.py retire` cancels it by."""
    return f"stall-watch:prompt:{slug}:{unit}"


def retire_alert(slug: str, unit: str) -> Delivery:
    """Cancel the repeats of the unit's prompt alert; once that succeeds, its prompt state is removed."""
    command = push_texts.user_retire_argv(SENDER, prompt_key(slug, unit))
    return Delivery(prompt_path(slug, unit), "retire", [sys.executable, str(SEND), *command[2:]])


def end_prompt(slug: str, unit: str) -> Delivery | None:
    """The prompt is gone: retire its alert when one went out, else remove its state."""
    path = prompt_path(slug, unit)
    saved = saved_prompt(path)
    if saved is None or not saved["alerted"]:
        path.unlink(missing_ok=True)
        return None
    saved["retiring"] = True
    save_prompt(path, saved)
    return retire_alert(slug, unit)


def forget(slug: str, unit: str) -> Delivery | None:
    stretch_path(slug, unit).unlink(missing_ok=True)
    return end_prompt(slug, unit)


def saved_prompt(path: Path) -> PromptWait | None:
    try:
        old = cast(PromptWait, json.loads(path.read_text(encoding="utf-8")))
        return PromptWait(since=float(old["since"]), alerted=old["alerted"] is True, tool=str(old.get("tool", "")),
                          retiring=old.get("retiring") is True)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def read_prompt(path: Path, now: float, pending: PendingTool | None = None) -> PromptWait:
    """The wait at the prompt on screen now: the saved one while the unit stays blocked, else a new one.

    A pending tool call in the transcript starts the wait when the session made the call, which can be
    long before herdr first reports the pane blocked. A different pending call is a new prompt; when the
    old one alerted, that alert is retired first, since both alerts have the unit's one key.
    """
    saved = saved_prompt(path)
    if saved is not None and saved["retiring"]:
        return saved
    if pending is None:
        return saved or PromptWait(since=now, alerted=False, tool="", retiring=False)
    if saved is not None and saved["tool"] in ("", pending.id):
        return PromptWait(since=min(saved["since"], pending.started), alerted=saved["alerted"], tool=pending.id,
                          retiring=False)
    if saved is not None and saved["alerted"]:
        saved["retiring"] = True
        return saved
    return PromptWait(since=min(now, pending.started), alerted=False, tool=pending.id, retiring=False)


def save_prompt(path: Path, prompt: PromptWait) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(prompt) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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


def prompt_report(slug: str, unit: str, session_id: str, pending: PendingTool | None, since: float) -> rca.Report:
    """The facts rca.py opens its case with: the pending Bash call's command, else the pending tool's name,
    when the wait started, and the alert's sender and key."""
    command = pending.input.get("command") if pending is not None else None
    if pending is None:
        commands: tuple[str, ...] = ()
    else:
        commands = (command,) if pending.name == "Bash" and isinstance(command, str) else (pending.name,)
    try:
        transcript = sessions.latest_transcript(session_id, PROJECTS_DIR)
    except OSError:
        transcript = None
    return rca.Report(unit, machine_name(), "prompt", commands, str(transcript or ""), slug,
                      stopped=datetime.fromtimestamp(since, ZoneInfo("UTC")).isoformat(), source=SENDER,
                      key=prompt_key(slug, unit))


def prompt_alert(slug: str, unit: str, pane: str, since: float, zone: ZoneInfo,
                 pending: PendingTool | None = None, report: rca.Report | None = None) -> Delivery:
    rendered = push_texts.render(PROMPT_TEXT_ID, unit=unit.removesuffix("-unit"), machine=machine_name(),
                                 since=datetime.fromtimestamp(since, zone).strftime("%H:%M %Z"),
                                 request=prompt_request(pane, pending))
    # end_prompt() retires the alert when its prompt is gone.
    command = push_texts.user_send_argv(rendered, sender=SENDER, key=prompt_key(slug, unit), self_clearing=True)
    # The send script is the one this watcher is set to use, so a test's stands in for it.
    return Delivery(prompt_path(slug, unit), "alert", [sys.executable, str(SEND), *command[2:]], report)


def read_stretch(path: Path, pane_hash: str, now: float) -> Stretch:
    try:
        old = cast(Stretch, json.loads(path.read_text(encoding="utf-8")))
        _ = old.setdefault("reported_status", "")
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


def send_all(pending: list[Delivery], now: float) -> None:
    """Start every relay and alert retire, then give the entire batch one 90-second deadline.

    After the batch, each prompt alert that went out opens its rca.py case. A retire that fails keeps
    its prompt state, so the next tick tries it again and sends no new alert.
    """
    started: list[tuple[Delivery, subprocess.Popen[str]]] = []
    reports: list[rca.Report] = []
    for item in pending:
        try:
            started.append((item, subprocess.Popen(item.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   text=True)))
        except OSError as error:
            print(f"stall-watch: {item.kind}: {error}", file=sys.stderr)
    deadline = time.monotonic() + 90
    for item, process in started:
        try:
            out, err = process.communicate(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            print(f"stall-watch: {item.kind}: send timeout", file=sys.stderr)
            continue
        if process.returncode != 0:
            print(f"stall-watch: {item.kind}: {err.strip() or out.strip() or process.returncode}", file=sys.stderr)
            continue
        if item.kind == "retire":
            item.state_path.unlink(missing_ok=True)
            continue
        if item.kind == "alert":
            prompt = read_prompt(item.state_path, 0)
            prompt["alerted"] = True
            save_prompt(item.state_path, prompt)
            if item.report is not None:
                reports.append(item.report)
            continue
        stretch = cast(Stretch, json.loads(item.state_path.read_text(encoding="utf-8")))
        if item.kind == "bump":
            stretch["bump_sent"] = True
        else:
            stretch["tell_sent"] = True
        save_stretch(item.state_path, stretch)
    for report in reports:
        print(rca.open_case(report, datetime.fromtimestamp(now, ZoneInfo("UTC"))))


def tick(now: float) -> None:
    settings = showrunners.load_settings()
    rows = processes()
    pending: list[Delivery] = []
    try:
        runs = unit_lookup.read_run_states()
    except (OSError, UnicodeError) as error:
        print(f"stall-watch: units: their run state is unknown: {error}", file=sys.stderr)
        return
    for configured in showrunners.registered_showrunners():
        # A showrunner that is not running has no one to tell: its units are not watched.
        showrunner_socket = configured["socket"]
        if not showrunner_socket:
            continue
        try:
            zone = ZoneInfo(configured["zone"])
        except (KeyError, ValueError):
            print(f"stall-watch: invalid zone for {configured['slug']}: {configured['zone']}", file=sys.stderr)
            continue
        # The units are the doc's live rows; each is found by the mark on its herdr pane.
        try:
            slug = showrunners.production_slug(configured["doc"])
            lines = Path(configured["doc"]).read_text(encoding="utf-8").splitlines()
            live = {production_units.cell_value(cells.get("Unit", "")): production_units.cell_value(cells.get("Worktree", ""))
                    for cells in production_units.live_unit_table(lines, slug)}
            marked = unit_lookup.marked_units(slug)
        except (OSError, UnicodeError, ValueError, production_units.Refusal) as error:
            print(f"stall-watch: {configured['slug']}: its units are not watched: {error}", file=sys.stderr)
            continue
        for name, unit in marked.items():
            # A unit is watched while the newest /unit:direct run in its worktree is unfinished.
            running = name in live and runs.state(Path(live[name])) is unit_lookup.UnitState.RUNNING
            if not running:
                if (retire := forget(slug, name)) is not None:
                    pending.append(retire)
                continue
            claude = unit.claude
            if not isinstance(claude, unit_lookup.LiveClaude):
                if isinstance(claude, unit_lookup.ClaudeUnknown):
                    print(f"stall-watch: {name}: {claude.reason}", file=sys.stderr)
                # A prompt is not on screen without its Claude.
                elif (retire := end_prompt(slug, name)) is not None:
                    pending.append(retire)
                continue
            try:
                pane = read_place(unit.place, 400, joined=True).strip()
            except (OSError, HerdrFailure) as error:
                print(f"stall-watch: {name}: {error}", file=sys.stderr)
                continue
            pid, session_id, socket = claude.pid, claude.session_id, claude.socket
            path = stretch_path(slug, name)
            waiting = prompt_path(slug, name)
            # None: herdr cannot say, so the unit is watched as not blocked, and its prompt is not ended.
            blocked: bool | None
            try:
                blocked = agent_status(unit.place) == BLOCKED
            except (OSError, HerdrFailure) as error:
                print(f"stall-watch: {name}: its herdr state is unknown: {error}", file=sys.stderr)
                blocked = None
            if blocked:
                # It waits on the user, not on itself: the idle clock starts again after the prompt.
                save_stretch(path, Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                           bump_sent=False, tell_sent=False, reported_status=""))
                # Claude Code denies a prompt with the auto-deny banner by itself: the user gets no alert.
                if auto_denied(pane):
                    if (retire := end_prompt(slug, name)) is not None:
                        pending.append(retire)
                    continue
                call = pending_tool(session_id)
                prompt = read_prompt(waiting, now, call)
                save_prompt(waiting, prompt)
                if prompt["retiring"]:
                    pending.append(retire_alert(slug, name))
                elif not prompt["alerted"] and now - prompt["since"] >= PROMPT_ALERT_SECONDS:
                    pending.append(prompt_alert(slug, name, pane, prompt["since"], zone, call,
                                                prompt_report(slug, name, session_id, call, prompt["since"])))
                continue
            if blocked is not None and (retire := end_prompt(slug, name)) is not None:
                pending.append(retire)
            stretch = read_stretch(path, hashlib.sha256(pane.encode()).hexdigest(), now)
            turns = list(TURN_END.finditer(pane))
            turn = turns[-1] if turns else transcript_turn_end(session_id)
            last = turn.group("status").strip() if turn else "none on screen"
            # A hold that states when it ends waits like a gate until then; the idle clock starts after it.
            held_until = (stated_hold_end(turn.group(0), now, zone)
                          if turn and turn.group("kind") == HOLDING_KIND else None)
            if turn and (turn.group("kind") in WAITING_KINDS or (held_until is not None and held_until > now)):
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
                save_stretch(path, stretch)
                continue
            running_work = work_running(pid, rows, (slug, unit.unit))
            if running_work or (stretch["reported_status"] and last != stretch["reported_status"]):
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
            elif not stretch["reported_status"]:
                stretch["since"] = max(stretch["since"], min(latest_transcript_activity(session_id), now))
            save_stretch(path, stretch)
            since = stretch["since"]
            if now - since < settings["stall_minutes"] * 60 or running_work:
                continue
            since_text = datetime.fromtimestamp(since, zone).strftime("%H:%M %Z")
            key = f"stall-watch:{name}:{int(since)}"
            if not stretch["reported_status"]:
                stretch["reported_status"] = last
                save_stretch(path, stretch)
            if not stretch["bump_sent"]:
                text = (f"stall-watch: you have been idle since {since_text} with nothing running. "
                        "Continue your run; if you are waiting on someone, say on whom in one line.")
                pending.append(delivery(path, "bump", socket, f"{key}:bump", text))
            if not stretch["tell_sent"] and showrunner_socket:
                text = f"{name} idle since {since_text}, nothing running; bumped. Last status: {last}"
                pending.append(delivery(path, "tell", showrunner_socket, f"{key}:tell", text))
    send_all(pending, now)


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = float(os.environ.get("STALL_WATCH_NOW_EPOCH") or time.time())
        tick(now)
        # The pass also reminds the owner of each permission case still open after 30 minutes.
        rca.remind(datetime.fromtimestamp(now, ZoneInfo("UTC")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"stall-watch: {error}", file=sys.stderr)
        raise SystemExit(1) from error
