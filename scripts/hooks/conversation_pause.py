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


QUIET_SECONDS = 300
ANSWER_SECONDS = 300
UNANSWERED_SECONDS = 1800
TOMBSTONE_SECONDS = 300
WATCHER = "conversation-pause"
JOB_SENDERS = frozenset({WATCHER, "stall-watch", "tmux-names", "quota_alert", "mac-test",
                         "disk_floor"})
QUESTION = (
    "conversation-pause: the user has been quiet here for 5 minutes. Ask them this, word for word, "
    "and nothing else: Return to automatic updates? (yes / no) They return on their own in 5 minutes. "
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
TAG_WRAPPER = re.compile(r"^<[A-Za-z][A-Za-z0-9-]*[\s>]")
CROSS_SESSION_TAG = re.compile(r"^<cross-session-message\b[^>]*>")
FROM_NAME = re.compile(r'\bfrom-name="([^"]*)"')


class PromptSource(Enum):
    TYPED = "typed"
    PEER = "peer"
    SCHEDULED = "scheduled"
    NOTICE = "notice"


@dataclass(frozen=True)
class Talking:
    user_wrote_at: int
    answered_at: int | None


@dataclass(frozen=True)
class Asked:
    asked_at: int


@dataclass(frozen=True)
class KeptOff:
    pass


@dataclass(frozen=True)
class Returned:
    returned_at: int


PausePhase = Talking | Asked | KeptOff | Returned


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
class HookReply:
    system_message: str | None
    context: str


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


def state_root() -> Path:
    return Path(os.environ.get("CONVERSATION_PAUSE_STATE_DIR")
                or Path.home() / ".local/state/conversation-pause")


def notifier_root() -> Path:
    return Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")


def record_path(session_id: str) -> Path:
    return state_root() / f"{session_id}.json"


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
                            check=False, timeout=10)
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


def prompt_source(prompt: str, senders: Callable[[], frozenset[str]]) -> PromptSource:
    text = prompt.lstrip()
    if not text:
        return PromptSource.NOTICE
    if CROSS_SESSION_TAG.match(text) is not None:
        opening_tag = text.partition(">")[0]
        match = FROM_NAME.search(opening_tag)
        name = match.group(1) if match is not None else ""
        return (PromptSource.SCHEDULED if name in JOB_SENDERS or name in senders()
                else PromptSource.PEER)
    if (TAG_WRAPPER.match(text) is not None
            or text.startswith("Another Claude session sent a message")
            or text.startswith("[SYSTEM NOTIFICATION")):
        return PromptSource.NOTICE
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
    if isinstance(phase, Talking):
        return {"kind": "talking", "user_wrote_at": phase.user_wrote_at,
                "answered_at": phase.answered_at}
    if isinstance(phase, Asked):
        return {"kind": "asked", "asked_at": phase.asked_at}
    if isinstance(phase, KeptOff):
        return {"kind": "kept_off"}
    return {"kind": "returned", "returned_at": phase.returned_at}


def _phase_kind(phase: PausePhase) -> str:
    return cast(str, _phase_json(phase)["kind"])


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {field}")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {field}")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"invalid {field}")
    return tuple(cast(list[str], items))


def _phase_from_json(value: object) -> PausePhase:
    if not isinstance(value, dict):
        raise ValueError("invalid phase")
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind == "talking":
        return Talking(_integer(fields.get("user_wrote_at"), "user_wrote_at"),
                       _optional_integer(fields.get("answered_at"), "answered_at"))
    if kind == "asked":
        return Asked(_integer(fields.get("asked_at"), "asked_at"))
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


def ensure_watcher() -> None:
    if _watcher_conf().exists():
        return
    home = Path.home()
    command = f"{home}/.claude/scripts/lib/py {home}/.claude/scripts/hooks/conversation_pause.py tick"
    _ = run_notifier("new", WATCHER, "--every", "1", "--run", command)


def _pause_locked(session_id: str, now: int) -> PauseResult:
    import showrunner_footer

    current = _record(session_id)
    record = (current if isinstance(current, PauseRecord)
              else PauseRecord(session_id, (), (), Talking(now, None)))
    record = replace(record, phase=Talking(now, None))
    new_instances: list[str] = []
    new_footers: list[str] = []
    reports = session_reports(session_id)
    for report in reports:
        if report.enabled and report.name not in record.instances:
            record = replace(record, instances=(*record.instances, report.name))
            write_record(record)
            _ = run_notifier("stop", report.name)
            new_instances.append(report.name)
    for report in reports:
        if not report.name.startswith("showrunner-"):
            continue
        slug = report.name.removeprefix("showrunner-")
        if (slug not in record.footers
                and showrunner_footer.footer_state(slug) is showrunner_footer.FooterState.ON):
            record = replace(record, footers=(*record.footers, slug))
            write_record(record)
            showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.OFF)
            new_footers.append(slug)
    if not record.instances and not record.footers:
        record_path(session_id).unlink(missing_ok=True)
        return NothingToPause()
    write_record(record)
    ensure_watcher()
    return Paused(user_words(new_instances, new_footers))


def pause(session_id: str, now: int) -> PauseResult:
    with record_lock():
        return _pause_locked(session_id, now)


def _resume_items(record: PauseRecord) -> tuple[str, ...]:
    import showrunner_footer

    resumed_instances: list[str] = []
    for name in record.instances:
        if (notifier_root() / name).is_dir():
            _ = run_notifier("resume", name)
            resumed_instances.append(name)
    for slug in record.footers:
        showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.ON)
    return user_words(resumed_instances, record.footers)


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


def mark_answered(session_id: str, now: int) -> None:
    with record_lock():
        current = _record(session_id)
        if (isinstance(current, PauseRecord) and isinstance(current.phase, Talking)
                and current.phase.answered_at is None):
            write_record(replace(current, phase=replace(current.phase, answered_at=now)))


def answer(prompt: str) -> PromptAnswer:
    text = prompt.strip().rstrip(".! ").lower()
    if text in {"yes", "y"}:
        return Yes()
    if text in {"no", "n"}:
        return No()
    return NotAnAnswer()


def _with_list(sentence: str, words: tuple[str, ...]) -> str:
    return f"{sentence}: {', '.join(words)}." if words else f"{sentence}."


def _pause_reply(result: PauseResult) -> HookReply | None:
    if not isinstance(result, Paused) or not result.newly:
        return None
    names = ", ".join(result.newly)
    return HookReply(
        f"Automatic updates paused while we talk: {names}.",
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


def message_arrived(session_id: str, source: PromptSource, prompt: str, now: int) -> HookReply | None:
    response = answer(prompt) if source is PromptSource.TYPED else NotAnAnswer()
    with record_lock():
        current = _record(session_id)
        if isinstance(current, PauseRecord) and isinstance(current.phase, Asked):
            if isinstance(response, Yes):
                words = _resume_locked(session_id)
                return HookReply(
                    _with_list("Automatic updates are back on", words),
                    'The user answered yes to "Return to automatic updates?". '
                    + _with_list("They are back on", words)
                    + " Confirm it in one line. That yes answers only this question.",
                )
            if isinstance(response, No):
                write_record(replace(current, phase=KeptOff()))
                return HookReply("Automatic updates stay off.", _keep_context())
            _ = _pause_locked(session_id, now)
            if source is not PromptSource.TYPED:
                return None
            return HookReply(
                None,
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
                return HookReply(None, "Automatic updates already returned on their own. Tell the user that in one line.")
            if late_answer and isinstance(response, No):
                result = _pause_locked(session_id, now)
                refreshed = _record(session_id)
                if isinstance(refreshed, PauseRecord):
                    write_record(replace(refreshed, phase=KeptOff()))
                words = result.newly if isinstance(result, Paused) else ()
                return HookReply(_with_list("Automatic updates are off again", words), _keep_context())
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


def _send_question(session_id: str, socket: str) -> None:
    try:
        result = subprocess.run(
            [*send_command(), "--to", f"uds:{socket}", "--from", WATCHER,
             "--key", f"conversation-pause-{session_id}", "--text", QUESTION],
            capture_output=True, text=True, check=False, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"conversation-pause: send for {session_id}: {error}", file=sys.stderr)
        return
    if result.returncode != 0:
        message = first_error_line(result.stderr) or first_error_line(result.stdout) or str(result.returncode)
        print(f"conversation-pause: send for {session_id}: {message}", file=sys.stderr)


def _return_after_timeout(record: PauseRecord, now: int) -> tuple[str, ...]:
    words = _resume_items(record)
    write_record(PauseRecord(record.session_id, (), (), Returned(now)))
    return words


def _question_due(phase: Talking, now: int) -> bool:
    if phase.answered_at is not None:
        return now - phase.answered_at >= QUIET_SECONDS
    return now - phase.user_wrote_at >= UNANSWERED_SECONDS


def _advance(path: Path, now: int, questions: list[tuple[str, str]], actions: list[str]) -> bool:
    """Move one record on; True while it still needs the watcher."""
    record = read_record(path)
    session = _session_lookup(record.session_id)
    if session is NotRunning.UNKNOWN:
        return True
    if session is NotRunning.GONE:
        words = _resume_items(record)
        path.unlink(missing_ok=True)
        actions.append(f"session ended: {record.session_id}: {', '.join(words)}")
        return False
    phase = record.phase
    if isinstance(phase, Talking):
        if _question_due(phase, now):
            write_record(replace(record, phase=Asked(now)))
            questions.append((record.session_id, session.socket))
            actions.append(f"question due: {record.session_id}")
        return True
    if isinstance(phase, Asked):
        if now - phase.asked_at < ANSWER_SECONDS:
            return True
        words = _return_after_timeout(record, now)
        actions.append(f"automatic updates on: {record.session_id}: {', '.join(words)}")
        return False
    if isinstance(phase, Returned) and now - phase.returned_at >= TOMBSTONE_SECONDS:
        path.unlink(missing_ok=True)
        actions.append(f"late-answer window ended: {record.session_id}")
    return False


def tick(now: int) -> tuple[str, ...]:
    questions: list[tuple[str, str]] = []
    actions: list[str] = []
    active = False
    with record_lock():
        for path in sorted(state_root().glob("*.json")):
            try:
                active = _advance(path, now, questions, actions) or active
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                active = True
                print(f"conversation-pause: {path.name}: {first_error_line(str(error))}",
                      file=sys.stderr)
        if not active and _watcher_conf().exists():
            _ = run_notifier("remove", WATCHER)
            actions.append("watcher removed")
    for session_id, socket in questions:
        _send_question(session_id, socket)
    return tuple(actions)


def _status(session_id: str) -> str:
    with record_lock():
        current = _record(session_id)
        if isinstance(current, NoPauseRecord):
            return "not paused"
        names = ", ".join(user_words(current.instances, current.footers))
        kind = _phase_kind(current.phase)
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
