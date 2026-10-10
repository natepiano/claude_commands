#!/usr/bin/env python3
"""Pause scheduled updates while a session has an active conversation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import cast


QUIET_SECONDS = 900
ANSWER_SECONDS = 300
UNANSWERED_SECONDS = 1800
TOMBSTONE_SECONDS = 300
SCHEDULED_PROMPT_RETENTION_SECONDS = 8 * 24 * 60 * 60
WATCHER = "conversation-pause"
JOB_SENDERS = frozenset({WATCHER, "stall-watch", "tmux-names", "quota_alert", "mac-test",
                         "disk_floor", "shutdown"})
QUESTION = (
    f"conversation-pause: the user has been quiet here for {QUIET_SECONDS // 60} minutes. "
    "Ask them this, word for word, and nothing else: Return to automatic updates? (yes / no) "
    f"They return on their own in {ANSWER_SECONDS // 60} minutes. "
    "Their yes or no is handled when they type it."
)
USAGE = "usage: conversation_pause.py status|resume|keep|tick"
RESUME_COMMAND = (
    '"$HOME/.claude/scripts/lib/py" '
    '"$HOME/.claude/scripts/hooks/conversation_pause.py" resume'
)
KEEP_COMMAND = (
    '"$HOME/.claude/scripts/lib/py" '
    '"$HOME/.claude/scripts/hooks/conversation_pause.py" keep'
)
NOTICE_TAG = re.compile(r"^<(?:task-notification|system-reminder|agent-message)(?:\s|>)")
CROSS_SESSION_TAG = re.compile(r"^<cross-session-message\b[^>]*>")
FROM_NAME = re.compile(r'\bfrom-name="([^"]*)"')
CLIPPED_PROMPT_MARKER = re.compile(r"… \[\+\d+ chars\]")

type ScheduledSenderLookup = Callable[[], frozenset[str]]
type ScheduledPromptLookup = Callable[[], tuple[str, ...]]


class PromptSource(Enum):
    TYPED = "typed"
    PEER = "peer"
    SCHEDULED = "scheduled"
    NOTICE = "notice"


@dataclass(frozen=True)
class Replying:
    user_wrote_at: int


@dataclass(frozen=True)
class Quiet:
    user_wrote_at: int
    reply_ended_at: int


@dataclass(frozen=True)
class QuestionPending:
    due_at: int


@dataclass(frozen=True)
class QuestionNotRead:
    pass


@dataclass(frozen=True)
class QuestionRead:
    replies_ended: int


QuestionReading = QuestionNotRead | QuestionRead


@dataclass(frozen=True)
class Asked:
    asked_at: int
    reading: QuestionReading


@dataclass(frozen=True)
class KeptOff:
    pass


@dataclass(frozen=True)
class Returned:
    returned_at: int


PausePhase = Replying | Quiet | QuestionPending | Asked | KeptOff | Returned


@dataclass(frozen=True)
class PauseRecord:
    session_id: str
    instances: tuple[str, ...]
    footers: tuple[str, ...]
    phase: PausePhase


@dataclass(frozen=True)
class Report:
    name: str
    enabled: bool


@dataclass(frozen=True)
class Paused:
    newly: tuple[str, ...]


@dataclass(frozen=True)
class NothingToPause:
    pass


PauseResult = Paused | NothingToPause


@dataclass(frozen=True)
class ShownToUser:
    system_message: str
    context: str


@dataclass(frozen=True)
class ContextOnly:
    context: str


class NoReply(Enum):
    NOTHING = "no reply"


HookReply = ShownToUser | ContextOnly | NoReply


@dataclass(frozen=True)
class Yes:
    pass


@dataclass(frozen=True)
class No:
    pass


@dataclass(frozen=True)
class NotAnAnswer:
    pass


PromptAnswer = Yes | No | NotAnAnswer


class NoPauseRecord(Enum):
    ABSENT = "no pause record"


RecordLookup = PauseRecord | NoPauseRecord


@dataclass(frozen=True)
class Running:
    socket: str


class NotRunning(Enum):
    GONE = "session gone"
    UNKNOWN = "session lookup failed"


SessionLookup = Running | NotRunning


@dataclass(frozen=True)
class QuestionDelivery:
    session_id: str
    socket: str
    due_at: int


def state_root() -> Path:
    return Path(os.environ.get("CONVERSATION_PAUSE_STATE_DIR")
                or Path.home() / ".local/state/conversation-pause")


def notifier_root() -> Path:
    return Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")


def record_path(session_id: str) -> Path:
    return state_root() / f"{session_id}.json"


def scheduled_prompts_path(session_id: str) -> Path:
    return state_root() / "scheduled-prompts" / f"{session_id}.json"


def now_epoch() -> int:
    override = os.environ.get("CONVERSATION_PAUSE_NOW_EPOCH")
    return int(override) if override is not None else int(time.time())


def notifier_command() -> list[str]:
    override = os.environ.get("CONVERSATION_PAUSE_NOTIFIER")
    if override:
        return [override]
    return ["zsh", str(Path(__file__).resolve().parent.parent / "message" / "notifier.sh")]


def sessions_command() -> list[str]:
    override = os.environ.get("CONVERSATION_PAUSE_SESSIONS")
    if override:
        return [override]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "message" / "sessions.py")]


def send_command() -> list[str]:
    override = os.environ.get("CONVERSATION_PAUSE_SEND")
    if override:
        return [override]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "message" / "send.py")]


def first_error_line(message: str) -> str:
    lines = message.splitlines()
    return lines[0] if lines else ""


def run_notifier(*arguments: str) -> str:
    result = subprocess.run([*notifier_command(), *arguments], capture_output=True, text=True,
                            check=False, timeout=5)
    if result.returncode != 0:
        message = first_error_line(result.stderr) or first_error_line(result.stdout)
        raise RuntimeError(message or f"notifier {' '.join(arguments)} exited {result.returncode}")
    return result.stdout.strip()


def _key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def scheduled_senders() -> frozenset[str]:
    senders: set[str] = set()
    try:
        entries = list(notifier_root().iterdir())
    except OSError:
        return frozenset()
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            conf = _key_values(entry / "conf")
            if "RUN" not in conf and conf.get("FROM"):
                senders.add(conf["FROM"])
        except (OSError, UnicodeError):
            continue
    return frozenset(senders)


def record_scheduled_prompts(session_id: str, prompts: tuple[str, ...]) -> None:
    destination = scheduled_prompts_path(session_id)
    if not prompts:
        destination.unlink(missing_ok=True)
        return
    root = destination.parent
    root.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=root,
            prefix=".scheduled-prompts-",
            delete=False,
            encoding="utf-8",
        ) as temporary:
            json.dump(list(prompts), temporary)
            _ = temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_scheduled_prompts(session_id: str) -> tuple[str, ...]:
    try:
        raw = cast(object, json.loads(
            scheduled_prompts_path(session_id).read_text(encoding="utf-8")
        ))
    except (OSError, UnicodeError, ValueError):
        return ()
    if not isinstance(raw, list):
        return ()
    items = cast(list[object], raw)
    if not all(isinstance(item, str) for item in items):
        return ()
    return tuple(cast(list[str], items))


def _is_recorded_scheduled_prompt(prompt: str, recorded: tuple[str, ...]) -> bool:
    prompt = prompt.strip()
    for scheduled in recorded:
        scheduled = scheduled.strip()
        if prompt == scheduled:
            return True
        marker = CLIPPED_PROMPT_MARKER.search(scheduled)
        if marker is not None:
            prefix = scheduled[:marker.start()]
            if prefix and prompt.startswith(prefix):
                return True
    return False


def is_return_question(prompt: str) -> bool:
    text = prompt.lstrip()
    if CROSS_SESSION_TAG.match(text) is None:
        return False
    opening_tag = text.partition(">")[0]
    match = FROM_NAME.search(opening_tag)
    return match is not None and match.group(1) == WATCHER


def prompt_source(
    prompt: str,
    senders: ScheduledSenderLookup,
    scheduled_prompts: ScheduledPromptLookup,
) -> PromptSource:
    text = prompt.lstrip()
    if not text:
        return PromptSource.NOTICE
    if CROSS_SESSION_TAG.match(text) is not None:
        opening_tag = text.partition(">")[0]
        match = FROM_NAME.search(opening_tag)
        name = match.group(1) if match is not None else ""
        return (PromptSource.SCHEDULED if name in JOB_SENDERS or name in senders()
                else PromptSource.PEER)
    if (NOTICE_TAG.match(text) is not None
            or text.startswith("Another Claude session sent a message")
            or text.startswith("[SYSTEM NOTIFICATION")):
        return PromptSource.NOTICE
    if _is_recorded_scheduled_prompt(text, scheduled_prompts()):
        return PromptSource.SCHEDULED
    return PromptSource.TYPED


def pauses(source: PromptSource, showrunner_session: bool) -> bool:
    if source is PromptSource.TYPED:
        return True
    return source is PromptSource.PEER and not showrunner_session


def is_showrunner_session(session_id: str) -> bool:
    import showrunner_footer

    return bool(showrunner_footer.targeted_instances(session_id))


def session_reports(session_id: str) -> list[Report]:
    reports: list[Report] = []
    try:
        entries = list(notifier_root().iterdir())
    except OSError:
        return reports
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            conf_lines = (entry / "conf").read_text(encoding="utf-8").splitlines()
            if f"TARGET=session:{session_id}" not in conf_lines:
                continue
            if any(line.startswith("RUN=") for line in conf_lines):
                continue
            state_lines = (entry / "state").read_text(encoding="utf-8").splitlines()
            reports.append(Report(entry.name, "ENABLED=1" in state_lines))
        except (OSError, UnicodeError):
            continue
    return sorted(reports, key=lambda report: report.name)


def _phase_json(phase: PausePhase) -> dict[str, object]:
    if isinstance(phase, Replying):
        return {"kind": "replying", "user_wrote_at": phase.user_wrote_at}
    if isinstance(phase, Quiet):
        return {"kind": "quiet", "user_wrote_at": phase.user_wrote_at,
                "reply_ended_at": phase.reply_ended_at}
    if isinstance(phase, QuestionPending):
        return {"kind": "question_pending", "due_at": phase.due_at}
    if isinstance(phase, Asked):
        reading: dict[str, object]
        if isinstance(phase.reading, QuestionNotRead):
            reading = {"kind": "not_read"}
        else:
            reading = {"kind": "read", "replies_ended": phase.reading.replies_ended}
        return {
            "kind": "asked",
            "asked_at": phase.asked_at,
            "reading": reading,
        }
    if isinstance(phase, KeptOff):
        return {"kind": "kept_off"}
    return {"kind": "returned", "returned_at": phase.returned_at}


def _phase_kind(phase: PausePhase) -> str:
    return cast(str, _phase_json(phase)["kind"])


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {field}")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {field}")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"invalid {field}")
    return tuple(cast(list[str], items))


def _question_reading_from_json(value: object) -> QuestionReading:
    if not isinstance(value, dict):
        raise ValueError("invalid reading")
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind == "not_read":
        return QuestionNotRead()
    if kind == "read":
        return QuestionRead(_integer(fields.get("replies_ended"), "replies_ended"))
    raise ValueError("invalid reading kind")


def _phase_from_json(value: object) -> PausePhase:
    if not isinstance(value, dict):
        raise ValueError("invalid phase")
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind == "replying":
        return Replying(_integer(fields.get("user_wrote_at"), "user_wrote_at"))
    if kind == "quiet":
        return Quiet(_integer(fields.get("user_wrote_at"), "user_wrote_at"),
                     _integer(fields.get("reply_ended_at"), "reply_ended_at"))
    if kind == "question_pending":
        return QuestionPending(_integer(fields.get("due_at"), "due_at"))
    if kind == "asked":
        reading = (
            _question_reading_from_json(fields.get("reading"))
            if "reading" in fields
            else QuestionRead(_integer(fields.get("replies_ended_since_question", 1),
                                       "replies_ended_since_question"))
        )
        return Asked(
            _integer(fields.get("asked_at"), "asked_at"),
            reading,
        )
    if kind == "kept_off":
        return KeptOff()
    if kind == "returned":
        return Returned(_integer(fields.get("returned_at"), "returned_at"))
    raise ValueError("invalid phase kind")


def read_record(path: Path) -> PauseRecord:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid conversation pause record: {path}")
    fields = cast(dict[str, object], raw)
    session_id = fields.get("session_id")
    if not isinstance(session_id, str):
        raise ValueError(f"invalid conversation pause record: {path}")
    try:
        return PauseRecord(session_id, _string_tuple(fields["instances"], "instances"),
                           _string_tuple(fields["footers"], "footers"),
                           _phase_from_json(fields["phase"]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid conversation pause record: {path}") from error


def write_record(record: PauseRecord) -> None:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = record_path(record.session_id)
    payload: dict[str, object] = {
        "session_id": record.session_id,
        "instances": list(record.instances),
        "footers": list(record.footers),
        "phase": _phase_json(record.phase),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=root, prefix=".conversation-pause-", delete=False,
                                         encoding="utf-8") as temporary:
            json.dump(payload, temporary)
            _ = temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def record_lock() -> Generator[None, None, None]:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _record(session_id: str) -> RecordLookup:
    path = record_path(session_id)
    return read_record(path) if path.exists() else NoPauseRecord.ABSENT


def user_words(instances: tuple[str, ...] | list[str],
               footers: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    instance_set = set(instances)
    words: list[str] = []
    if any(name.startswith("showrunner-") for name in instance_set):
        words.append("dailies")
    if footers:
        words.append("footer")
    if any(name.startswith("delegate-") for name in instance_set):
        words.append("status reports")
    if "report-builds" in instance_set:
        words.append("build report")
    known = {name for name in instance_set
             if name.startswith("showrunner-") or name.startswith("delegate-")
             or name == "report-builds"}
    words.extend(sorted(instance_set - known))
    return tuple(words)


def _watcher_conf() -> Path:
    return notifier_root() / WATCHER / "conf"


def _watcher_state() -> Path:
    return notifier_root() / WATCHER / "state"


def ensure_watcher() -> None:
    conf_exists = _watcher_conf().exists()
    state_exists = _watcher_state().exists()
    if conf_exists and state_exists:
        if _key_values(_watcher_state()).get("ENABLED") == "1":
            return
        _ = run_notifier("resume", WATCHER)
        return
    if conf_exists or state_exists:
        _ = run_notifier("remove", WATCHER)
    home = Path.home()
    command = f"{home}/.claude/scripts/lib/py {home}/.claude/scripts/hooks/conversation_pause.py tick"
    _ = run_notifier("new", WATCHER, "--every", "1", "--run", command)


def _pause_locked(session_id: str, now: int) -> PauseResult:
    import showrunner_footer

    current = _record(session_id)
    reports = session_reports(session_id)
    footer_slugs = tuple(dict.fromkeys(
        report.name.removeprefix("showrunner-")
        for report in reports if report.name.startswith("showrunner-")
    ))
    footers_on = {
        slug for slug in footer_slugs
        if showrunner_footer.footer_state(slug) is showrunner_footer.FooterState.ON
    }
    if (isinstance(current, NoPauseRecord)
            and not any(report.enabled for report in reports) and not footers_on):
        return NothingToPause()
    ensure_watcher()

    record = (current if isinstance(current, PauseRecord)
              else PauseRecord(session_id, (), (), Replying(now)))
    record = replace(record, phase=Replying(now))
    new_instances: list[str] = []
    new_footers: list[str] = []
    failed: list[str] = []
    for report in reports:
        if not report.enabled:
            continue
        newly_recorded = report.name not in record.instances
        if newly_recorded:
            record = replace(record, instances=(*record.instances, report.name))
            write_record(record)
        try:
            _ = run_notifier("stop", report.name)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            record = replace(record, instances=tuple(
                name for name in record.instances if name != report.name
            ))
            write_record(record)
            failed.append(report.name)
            continue
        if newly_recorded:
            new_instances.append(report.name)
    for slug in footer_slugs:
        if slug not in footers_on:
            continue
        newly_recorded = slug not in record.footers
        if newly_recorded:
            record = replace(record, footers=(*record.footers, slug))
            write_record(record)
        try:
            showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.OFF)
        except OSError:
            record = replace(record, footers=tuple(
                name for name in record.footers if name != slug
            ))
            write_record(record)
            failed.append(f"footer {slug}")
            continue
        if newly_recorded:
            new_footers.append(slug)
    if not record.instances and not record.footers:
        record_path(session_id).unlink(missing_ok=True)
        result: PauseResult = NothingToPause()
    else:
        write_record(record)
        result = Paused(user_words(new_instances, new_footers))
    if failed:
        raise RuntimeError(f"failed to pause: {', '.join(failed)}")
    return result


def pause(session_id: str, now: int) -> PauseResult:
    with record_lock():
        return _pause_locked(session_id, now)


def _resume_items(record: PauseRecord) -> tuple[str, ...]:
    import showrunner_footer

    resumed_instances: list[str] = []
    failed_instances: list[str] = []
    resumed_footers: list[str] = []
    failed_footers: list[str] = []
    for name in record.instances:
        if not (notifier_root() / name).is_dir():
            continue
        try:
            _ = run_notifier("resume", name)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            failed_instances.append(name)
        else:
            resumed_instances.append(name)
    for slug in record.footers:
        try:
            showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.ON)
        except OSError:
            failed_footers.append(slug)
        else:
            resumed_footers.append(slug)
    if failed_instances or failed_footers:
        write_record(replace(
            record,
            instances=tuple(failed_instances),
            footers=tuple(failed_footers),
        ))
        failed = [*failed_instances, *(f"footer {slug}" for slug in failed_footers)]
        raise RuntimeError(f"failed to resume: {', '.join(failed)}")
    return user_words(resumed_instances, resumed_footers)


def _resume_locked(session_id: str) -> tuple[str, ...]:
    current = _record(session_id)
    if isinstance(current, NoPauseRecord):
        return ()
    words = _resume_items(current)
    record_path(session_id).unlink(missing_ok=True)
    return words


def resume(session_id: str) -> tuple[str, ...]:
    with record_lock():
        return _resume_locked(session_id)


def release(session_id: str) -> RecordLookup:
    """Remove and return a pause record without resuming what it paused."""
    with record_lock():
        current = _record(session_id)
        if isinstance(current, PauseRecord):
            record_path(session_id).unlink(missing_ok=True)
        return current


def mark_reply_ended(session_id: str, now: int) -> None:
    with record_lock():
        current = _record(session_id)
        if not isinstance(current, PauseRecord):
            return
        phase = current.phase
        if isinstance(phase, Replying):
            write_record(replace(
                current,
                phase=Quiet(phase.user_wrote_at, now),
            ))
        elif isinstance(phase, Asked) and isinstance(phase.reading, QuestionRead):
            write_record(replace(
                current,
                phase=replace(
                    phase,
                    reading=replace(
                        phase.reading,
                        replies_ended=phase.reading.replies_ended + 1,
                    ),
                ),
            ))


def answer(prompt: str) -> PromptAnswer:
    text = prompt.strip().rstrip(".! ").lower()
    if text in {"yes", "y"}:
        return Yes()
    if text in {"no", "n"}:
        return No()
    return NotAnAnswer()


def _with_list(sentence: str, words: tuple[str, ...]) -> str:
    return f"{sentence}: {', '.join(words)}." if words else f"{sentence}."


def _return_schedule() -> str:
    ask = QUIET_SECONDS // 60
    back = (QUIET_SECONDS + ANSWER_SECONDS) // 60
    return f"They return {back} minutes after my last reply; I ask you first at {ask}."


def _ask_again() -> str:
    return f"If you write again, I ask about them {QUIET_SECONDS // 60} minutes after my reply."


def _pause_reply(result: PauseResult) -> HookReply:
    if not isinstance(result, Paused) or not result.newly:
        return NoReply.NOTHING
    names = ", ".join(result.newly)
    return ShownToUser(
        f"Automatic updates paused while we talk: {names}. {_return_schedule()}",
        "Automatic updates for this session are paused while the user talks to you: "
        +
        f"{names}. Leave them off; the session asks the user before they return. If the user asks "
        +
        f"for them back sooner, run: {RESUME_COMMAND}",
    )


def _keep_context() -> str:
    return (
        'The user answered no to "Return to automatic updates?". They stay off until the user asks '
        f"for them; then run: {RESUME_COMMAND}. Confirm it in one line. That no answers only this question."
    )


def message_arrived(session_id: str, source: PromptSource, prompt: str, now: int) -> HookReply:
    response = answer(prompt) if source is PromptSource.TYPED else NotAnAnswer()
    return_question = is_return_question(prompt)
    with record_lock():
        current = _record(session_id)
        if return_question:
            if isinstance(current, PauseRecord):
                phase = current.phase
                if isinstance(phase, QuestionPending):
                    write_record(replace(
                        current,
                        phase=Asked(now, QuestionRead(0)),
                    ))
                elif (isinstance(phase, Asked)
                      and isinstance(phase.reading, QuestionNotRead)):
                    write_record(replace(
                        current,
                        phase=replace(phase, reading=QuestionRead(0)),
                    ))
            return NoReply.NOTHING
        if isinstance(current, PauseRecord) and isinstance(current.phase, Asked):
            phase = current.phase
            if (isinstance(phase.reading, QuestionRead)
                    and phase.reading.replies_ended > 1
                    and isinstance(response, (Yes, No))):
                return NoReply.NOTHING
            if isinstance(response, Yes):
                words = _resume_locked(session_id)
                return ShownToUser(
                    _with_list("Automatic updates are back on", words),
                    'The user answered yes to "Return to automatic updates?". '
                    + _with_list("They are back on", words)
                    + " Confirm it in one line. That yes answers only this question.",
                )
            if isinstance(response, No):
                write_record(replace(current, phase=KeptOff()))
                return ShownToUser(
                    f"Automatic updates stay off. {_ask_again()}",
                    _keep_context(),
                )
            _ = _pause_locked(session_id, now)
            if source is not PromptSource.TYPED:
                return NoReply.NOTHING
            return ContextOnly(
                'You asked the user "Return to automatic updates? (yes / no)" and they wrote something '
                + "else. If their message answers that question, run "
                +
                f"{RESUME_COMMAND} for yes or {KEEP_COMMAND} for no. Otherwise answer them and say nothing "
                + "of the question; it comes back when they go quiet.",
            )
        if isinstance(current, PauseRecord) and isinstance(current.phase, Returned):
            late_answer = now - current.phase.returned_at < TOMBSTONE_SECONDS
            if late_answer and isinstance(response, Yes):
                record_path(session_id).unlink(missing_ok=True)
                return ContextOnly(
                    "Automatic updates already returned on their own. Tell the user that in one line."
                )
            if late_answer and isinstance(response, No):
                result = _pause_locked(session_id, now)
                refreshed = _record(session_id)
                if isinstance(refreshed, PauseRecord):
                    write_record(replace(refreshed, phase=KeptOff()))
                words = result.newly if isinstance(result, Paused) else ()
                return ShownToUser(
                    _with_list("Automatic updates are off again", words)
                    + f" {_ask_again()}",
                    _keep_context(),
                )
            record_path(session_id).unlink(missing_ok=True)
            return _pause_reply(_pause_locked(session_id, now))
        return _pause_reply(_pause_locked(session_id, now))


def _keep(session_id: str) -> bool:
    with record_lock():
        current = _record(session_id)
        if isinstance(current, NoPauseRecord):
            return False
        write_record(replace(current, phase=KeptOff()))
        return True


def _session_lookup(session_id: str) -> SessionLookup:
    try:
        result = subprocess.run([*sessions_command(), "socket", f"session:{session_id}"],
                                capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return NotRunning.UNKNOWN
    socket = result.stdout.strip()
    if result.returncode == 0 and socket:
        return Running(socket)
    return NotRunning.GONE if result.returncode in {0, 1} else NotRunning.UNKNOWN


def _send_question(delivery: QuestionDelivery) -> bool:
    try:
        result = subprocess.run(
            [*send_command(), "--to", f"uds:{delivery.socket}", "--from", WATCHER,
             "--key", f"conversation-pause-{delivery.session_id}", "--text", QUESTION],
            capture_output=True, text=True, check=False, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"conversation-pause: send for {delivery.session_id}: {error}", file=sys.stderr)
        return False
    first_output = first_error_line(result.stdout)
    if result.returncode == 0 and first_output.startswith("SENT:"):
        return True
    message = first_error_line(result.stderr) or first_output or str(result.returncode)
    print(f"conversation-pause: send for {delivery.session_id}: {message}", file=sys.stderr)
    return False


def _return_after_timeout(record: PauseRecord, now: int) -> tuple[str, ...]:
    words = _resume_items(record)
    write_record(PauseRecord(record.session_id, (), (), Returned(now)))
    return words


def _review_slugs(record: PauseRecord) -> frozenset[str]:
    slugs = set(record.footers)
    slugs.update(
        name.removeprefix("showrunner-")
        for name in record.instances if name.startswith("showrunner-")
    )
    return frozenset(slugs)


def _advance(path: Path, record: PauseRecord, now: int, session: SessionLookup, review_open: bool,
             questions: list[QuestionDelivery], actions: list[str]) -> None:
    if session is NotRunning.GONE:
        words = _resume_items(record)
        path.unlink(missing_ok=True)
        actions.append(f"session ended: {record.session_id}: {', '.join(words)}")
        return
    if review_open:
        return
    phase = record.phase
    if isinstance(phase, Replying):
        if now - phase.user_wrote_at >= UNANSWERED_SECONDS:
            phase = QuestionPending(now)
            record = replace(record, phase=phase)
            write_record(record)
            actions.append(f"question due: {record.session_id}")
    elif isinstance(phase, Quiet):
        if now - phase.reply_ended_at >= QUIET_SECONDS:
            phase = QuestionPending(now)
            record = replace(record, phase=phase)
            write_record(record)
            actions.append(f"question due: {record.session_id}")
    if isinstance(phase, QuestionPending):
        if now - phase.due_at >= ANSWER_SECONDS:
            words = _return_after_timeout(record, now)
            actions.append(f"automatic updates on: {record.session_id}: {', '.join(words)}")
        elif isinstance(session, Running):
            questions.append(QuestionDelivery(record.session_id, session.socket, phase.due_at))
        return
    if isinstance(phase, Asked):
        if now - phase.asked_at < ANSWER_SECONDS:
            return
        words = _return_after_timeout(record, now)
        actions.append(f"automatic updates on: {record.session_id}: {', '.join(words)}")
        return
    if isinstance(phase, Returned) and now - phase.returned_at >= TOMBSTONE_SECONDS:
        path.unlink(missing_ok=True)
        actions.append(f"late-answer window ended: {record.session_id}")


def tick(now: int) -> tuple[str, ...]:
    import showrunner_footer

    scheduled_root = state_root() / "scheduled-prompts"
    try:
        scheduled_paths = list(scheduled_root.glob("*.json"))
    except OSError:
        scheduled_paths = []
    for path in scheduled_paths:
        try:
            if now - path.stat().st_mtime > SCHEDULED_PROMPT_RETENTION_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue

    paths = sorted(state_root().glob("*.json"))
    lookups = [(path, _session_lookup(path.stem)) for path in paths]
    questions: list[QuestionDelivery] = []
    actions: list[str] = []
    with record_lock():
        for path, session in lookups:
            try:
                record = read_record(path)
                review_open = any(
                    showrunner_footer.review_pause_path(slug).exists()
                    for slug in _review_slugs(record)
                )
                _advance(path, record, now, session, review_open, questions, actions)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                print(f"conversation-pause: {path.name}: {first_error_line(str(error))}",
                      file=sys.stderr)
        if not any(state_root().glob("*.json")) and _watcher_conf().exists():
            _ = run_notifier("remove", WATCHER)
            actions.append("watcher removed")
    for delivery in questions:
        with record_lock():
            current = _record(delivery.session_id)
            if (not isinstance(current, PauseRecord)
                    or not isinstance(current.phase, QuestionPending)
                    or current.phase.due_at != delivery.due_at):
                continue
        if not _send_question(delivery):
            continue
        delivered_at = now_epoch()
        with record_lock():
            current = _record(delivery.session_id)
            if (isinstance(current, PauseRecord)
                    and isinstance(current.phase, QuestionPending)
                    and current.phase.due_at == delivery.due_at):
                write_record(replace(
                    current,
                    phase=Asked(delivered_at, QuestionNotRead()),
                ))
    return tuple(actions)


def _status(session_id: str) -> str:
    with record_lock():
        current = _record(session_id)
        if isinstance(current, NoPauseRecord):
            return "not paused"
        names = ", ".join(user_words(current.instances, current.footers))
        kind = _phase_kind(current.phase)
        if (isinstance(current.phase, Asked)
                and isinstance(current.phase.reading, QuestionRead)
                and current.phase.reading.replies_ended > 1):
            kind += "; a newer reply ended; bare yes/no belongs to the session"
        return f"paused: {names} ({kind})" if names else f"not paused ({kind})"


def main(arguments: list[str]) -> int:
    if len(arguments) != 1 or arguments[0] not in {"status", "resume", "keep", "tick"}:
        print(USAGE, file=sys.stderr)
        return 2
    action = arguments[0]
    if action == "tick":
        for line in tick(now_epoch()):
            print(line)
        return 0
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        print("no session", file=sys.stderr)
        return 1
    if action == "status":
        print(_status(session_id))
    elif action == "resume":
        words = resume(session_id)
        print(f"automatic updates on: {', '.join(words)}" if words else "nothing was paused")
    elif _keep(session_id):
        print("automatic updates stay off")
    else:
        print("nothing was paused")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"conversation-pause: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
