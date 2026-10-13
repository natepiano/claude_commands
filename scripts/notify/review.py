#!/usr/bin/env python3
"""Review phone drafts; keep one owner for delivery of a held push."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import math
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast

from user_action import ActionRequired, NoActionRequired, UserAction, first_line, parse_user_action

if TYPE_CHECKING:
    from ..accounts import accounts
    from ..whoami.account import named_account_environment

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path.home() / ".local/state/notify"
GUIDE = Path.home() / "rust/nate_style/message"
LOADER = Path.home() / ".claude/scripts/ux_style/load-ux-style.sh"
PROMPT_FILE = Path(__file__).with_name("reviewer_prompt.md")
TIMEOUT_S = 20
HOLD_SECONDS = 300
# A claim gets 120 s, beyond a revision's 20 s review plus 15 s post budget.
CLAIM_STALE_SECONDS = 120
EXPIRY_KEYS_FILE = Path.home() / ".config/pushover/env"


@dataclass(frozen=True)
class Draft:
    source: str
    priority: str
    title: str
    action: UserAction
    body: str
    answerable: bool


@dataclass(frozen=True)
class ReviewMetadata:
    seconds: float = 0.0
    account: str = "default"
    model: str = "opus"


DEFAULT_METADATA = ReviewMetadata()


@dataclass(frozen=True)
class Approved:
    draft: Draft
    metadata: ReviewMetadata = ReviewMetadata()


@dataclass(frozen=True)
class Rewritten:
    version: Draft
    failed_rules: list[str]
    metadata: ReviewMetadata = ReviewMetadata()


@dataclass(frozen=True)
class Questions:
    best_version: Draft
    questions: list[str]
    failed_rules: list[str]
    metadata: ReviewMetadata = ReviewMetadata()


@dataclass(frozen=True)
class Unreviewed:
    why: str
    metadata: ReviewMetadata = ReviewMetadata()


ReviewOutcome = Approved | Rewritten | Questions | Unreviewed
Verdict = Approved | Rewritten | Questions


@dataclass(frozen=True)
class Sent:
    draft: Draft


@dataclass(frozen=True)
class NotSent:
    pass


Delivery = Sent | NotSent
NOT_SENT = NotSent()


@dataclass(frozen=True)
class Held:
    source: str
    created: float
    draft: Draft
    best_version: Draft
    questions: list[str]


@dataclass(frozen=True)
class NoHold:
    pass


@dataclass(frozen=True)
class LiveHold:
    hold: Held


@dataclass(frozen=True)
class UnreadableHold:
    path: Path
    why: str


HoldLookup = NoHold | LiveHold | UnreadableHold


class MessageRecord(TypedDict):
    title: str
    action: str | None
    body: str


class DraftRecord(MessageRecord):
    source: str
    priority: str
    answerable: bool


class HeldRecord(TypedDict):
    source: str
    priority: str
    created: float
    draft: DraftRecord
    best_version: DraftRecord
    questions: list[str]


class VerdictRecord(MessageRecord):
    verdict: Literal["send", "rewrite", "ask"]
    questions: list[str]
    failed_rules: list[str]


class ReviewRecord(TypedDict):
    time: str
    source: str
    priority: str
    draft: MessageRecord
    verdict: str
    failed_rules: list[str]
    questions: list[str]
    sent: MessageRecord | None
    seconds: float
    account: str
    model: str
    replay: NotRequired[bool]
    dry: NotRequired[bool]
    detail: NotRequired[str]


class StringSchema(TypedDict):
    type: str


class PropertySchema(TypedDict, total=False):
    type: str | list[str]
    enum: list[str]
    items: StringSchema
    maxItems: int


class VerdictSchema(TypedDict):
    type: str
    properties: dict[str, PropertySchema]
    required: list[str]
    additionalProperties: bool


SCHEMA: VerdictSchema = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["send", "rewrite", "ask"]},
        "title": {"type": "string"}, "action": {"type": ["string", "null"]},
        "body": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "failed_rules": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "title", "action", "body", "questions", "failed_rules"],
    "additionalProperties": False,
}


class ProcessRunner(Protocol):
    def __call__(self, argv: list[str], *, timeout: float,
                 env: dict[str, str]) -> subprocess.CompletedProcess[str]: ...


def run_process(argv: list[str], *, timeout: float,
                env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          timeout=timeout, env=env)


def state_path() -> Path:
    return Path(os.environ.get("NOTIFY_STATE_DIR", str(STATE_DIR)))


def switched_off() -> bool:
    return (state_path() / "review_off").exists()


def message_record(draft: Draft) -> MessageRecord:
    action = draft.action.text if isinstance(draft.action, ActionRequired) else None
    return {"title": draft.title, "action": action, "body": draft.body}


def draft_record(draft: Draft) -> DraftRecord:
    return DraftRecord(**message_record(draft), source=draft.source,
                       priority=draft.priority, answerable=draft.answerable)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return cast(dict[str, object], value)


def _strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(field)
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(field)
    return cast(list[str], items)


def _text(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(field)
    return value


def _action(record: dict[str, object]) -> UserAction:
    if "action" not in record:
        raise ValueError("action missing")
    text = record["action"]
    if text is None:
        return NoActionRequired()
    if not isinstance(text, str):
        raise ValueError("action")
    action = parse_user_action(text, False)
    if not isinstance(action, ActionRequired):
        raise ValueError("action empty or invalid")
    return action


def _draft(value: object) -> Draft:
    record = _object(value)
    answerable = record.get("answerable")
    if not isinstance(answerable, bool):
        raise ValueError("answerable")
    priority = _text(record, "priority")
    if priority not in ("0", "1", "2"):
        raise ValueError("priority")
    return Draft(_text(record, "source"), priority, _text(record, "title"),
                 _action(record), _text(record, "body"), answerable)


def validate(value: object, draft: Draft, revision: bool,
             metadata: ReviewMetadata = DEFAULT_METADATA) -> ReviewOutcome:
    try:
        record = _object(value)
        verdict = _text(record, "verdict")
        if verdict not in ("send", "rewrite", "ask"):
            raise ValueError("verdict")
        title, body = _text(record, "title"), _text(record, "body")
        action_text = record.get("action")
        if isinstance(action_text, str) and action_text.startswith("Action: "):
            record = record | {"action": action_text.removeprefix("Action: ")}
        action = _action(record)
        if isinstance(action, ActionRequired) != isinstance(draft.action, ActionRequired):
            raise ValueError("action form changed")
        body_first_line, _, remaining_body = body.partition("\n")
        if body_first_line == first_line(action):
            body = remaining_body.removeprefix("\n")
        if len(title) > 250:
            raise ValueError("title exceeds 250")
        if len(first_line(action) + "\n" + body) > 1024:
            raise ValueError("message exceeds 1024")
        questions = _strings(record.get("questions"), "questions")
        failed = _strings(record.get("failed_rules"), "failed_rules")
        if len(questions) > 3:
            raise ValueError("questions exceeds 3")
        if verdict != "ask" and questions:
            raise ValueError("questions without ask")
        version = replace(draft, title=title, action=action, body=body)
        if verdict == "send":
            if message_record(version) != message_record(draft):
                raise ValueError("send changed draft")
            return Approved(draft, metadata)
        if verdict == "ask" and not revision:
            if not questions or any(not question.strip() for question in questions):
                raise ValueError("ask needs questions")
            return Questions(version, questions, failed, metadata)
        return Rewritten(version, failed, metadata)
    except ValueError as error:
        return Unreviewed(str(error), metadata)


def reviewer_environment() -> tuple[dict[str, str], str]:
    # Import the shared account modules from this checkout, not the writer's login.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if not TYPE_CHECKING:
        from scripts.accounts import accounts
        from scripts.whoami.account import named_account_environment
    environment = dict(os.environ)
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"):
        _ = environment.pop(key, None)
    selected = accounts.role_account("unit")
    if isinstance(selected, accounts.NamedAccount):
        environment.update(named_account_environment(selected.dir))
        return environment, selected.label
    return environment, "default"


def reviewer_assignment(runner: ProcessRunner, environment: dict[str, str],
                        timeout: float) -> tuple[str, str]:
    try:
        result = runner(["bash", "-c", "source ~/.claude/scripts/agents/agents_config.sh; " +
                         "agents_resolve_print message_review.reviewer"],
                        timeout=timeout, env=environment)
        fields = dict(item.split("=", 1) for item in result.stdout.split() if "=" in item)
        if result.returncode == 0 and fields.get("agent") and fields.get("effort"):
            return fields["agent"], fields["effort"]
    except (OSError, subprocess.SubprocessError):
        pass
    return "opus", "medium"


def _review(draft: Draft, revision: bool, runner: ProcessRunner) -> ReviewOutcome:
    started = time.monotonic()
    account, model = "default", "opus"

    def details() -> ReviewMetadata:
        return ReviewMetadata(time.monotonic() - started, account, model)

    def remaining() -> float:
        return max(0.001, TIMEOUT_S - (time.monotonic() - started))

    try:
        if not GUIDE.is_dir():
            return Unreviewed("no guide", details())
        environment, account = reviewer_environment()
        guide = runner(["zsh", str(LOADER), "--guide", str(GUIDE), "--tags", "push,all"],
                       timeout=remaining(), env=environment)
        if guide.returncode != 0 or not guide.stdout.strip():
            return Unreviewed("no guide", details())
        model, effort = reviewer_assignment(runner, environment, min(2.0, remaining()))
        prompt = PROMPT_FILE.read_text(encoding="utf-8")
        action_line = first_line(draft.action) if isinstance(draft.action, ActionRequired) else "no action"
        draft_prompt = (f"Title: {draft.title}\n{action_line}\n"
                        f"Body (text after the action line; never repeat it): {draft.body}\n"
                        f"Priority: {draft.priority}\nRevision: {str(revision).lower()}")
        command = ["claude", "-p", "--model", model, "--effort", effort,
                   "--system-prompt", guide.stdout + "\n" + prompt,
                   "--json-schema", json.dumps(SCHEMA), "--output-format", "json",
                   "--tools", "", "--no-session-persistence", "--setting-sources", "", draft_prompt]
        if time.monotonic() - started >= TIMEOUT_S:
            return Unreviewed("timeout", details())
        result = runner(command, timeout=remaining(), env=environment)
        if result.returncode != 0:
            return Unreviewed(f"reviewer exit {result.returncode}", details())
        value = cast(object, json.loads(result.stdout))
        envelope = _object(value)
        if envelope.get("is_error") is True:
            return Unreviewed("reviewer error", details())
        if "structured_output" in envelope:
            value = envelope["structured_output"]
        elif "result" in envelope and isinstance(envelope["result"], str):
            value = cast(object, json.loads(envelope["result"]))
        return validate(value, draft, revision, details())
    except subprocess.TimeoutExpired:
        return Unreviewed("timeout", details())
    except json.JSONDecodeError:
        return Unreviewed("bad JSON", details())
    except (OSError, ValueError) as error:
        return Unreviewed(f"review unavailable: {error}", details())
    except Exception as error:
        # A review must never block a push, including unexpected setup or runner faults.
        return Unreviewed(f"review unavailable: {type(error).__name__}: {error}", details())


def review(draft: Draft, revision: bool, *, runner: ProcessRunner = run_process) -> ReviewOutcome:
    return _review(draft, revision, runner)


def _append(entry: ReviewRecord) -> None:
    try:
        state_path().mkdir(parents=True, exist_ok=True)
        with (state_path() / "review.jsonl").open("a", encoding="utf-8") as output:
            _ = output.write(json.dumps(entry) + "\n")
    except OSError as error:
        print(f"review: could not write log: {error}", file=sys.stderr)


def log_review(draft: Draft, outcome: ReviewOutcome, sent: Delivery = NOT_SENT, *,
               replay: bool = False, dry: bool = False, detail: str = "") -> None:
    verdict, failed, questions = "send", [], []
    if isinstance(outcome, Unreviewed):
        verdict = "review: off" if detail == "review: off" else "unreviewed: " + outcome.why
    elif isinstance(outcome, Rewritten):
        verdict, failed = "rewrite", outcome.failed_rules
    elif isinstance(outcome, Questions):
        verdict, failed, questions = "ask", outcome.failed_rules, outcome.questions
    metadata = outcome.metadata
    entry: ReviewRecord = {
        "time": datetime.now().astimezone().isoformat(), "source": draft.source,
        "priority": draft.priority, "draft": message_record(draft), "verdict": verdict,
        "failed_rules": failed, "questions": questions,
        "sent": message_record(sent.draft) if isinstance(sent, Sent) else None,
        "seconds": metadata.seconds, "account": metadata.account, "model": metadata.model,
    }
    if replay:
        entry["replay"] = True
    if dry:
        entry["dry"] = True
    if detail:
        entry["detail"] = detail
    _append(entry)


@contextmanager
def locked() -> Generator[None]:
    state_path().mkdir(parents=True, exist_ok=True)
    with (state_path() / "review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def held_path(source: str) -> Path:
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    return state_path() / "held" / f"{digest}.json"


def _read_hold(path: Path) -> HoldLookup:
    try:
        record = _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))
        created = record.get("created")
        if (isinstance(created, bool) or not isinstance(created, (int, float))
                or not math.isfinite(created)):
            raise ValueError("created")
        held = Held(_text(record, "source"), float(created), _draft(record.get("draft")),
                    _draft(record.get("best_version")), _strings(record.get("questions"), "questions"))
        if held.source != held.draft.source or held.source != held.best_version.source:
            raise ValueError("hold source mismatch")
        if held.draft.priority != held.best_version.priority:
            raise ValueError("hold priority mismatch")
        if record.get("priority") != held.draft.priority:
            raise ValueError("stored priority mismatch")
        if path.stem != held_path(held.source).stem:
            raise ValueError("hold filename mismatch")
        checked = validate(VerdictRecord(**message_record(held.best_version), verdict="ask",
                                        questions=held.questions, failed_rules=[]), held.draft, False)
        if isinstance(checked, Unreviewed):
            raise ValueError(checked.why)
        return LiveHold(held)
    except FileNotFoundError:
        return NoHold()
    except (OSError, ValueError) as error:
        bad = path.parent / "bad"
        bad.mkdir(parents=True, exist_ok=True)
        target = bad / f"{path.name}.{time.time_ns()}"
        _ = path.replace(target)
        draft = Draft("review.hold", "0", "Unreadable held push", NoActionRequired(), "", False)
        log_review(draft, Unreviewed(f"unreadable hold: {error}"), detail=str(target))
        return UnreadableHold(target, str(error))


def held_for(source: str) -> HoldLookup:
    with locked():
        path = held_path(source)
        if path.with_suffix(".sending").exists():
            path = path.with_suffix(".sending")
        return _read_hold(path)


def schedule() -> None:
    python = Path.home() / ".claude/scripts/lib/py"
    notifier = Path.home() / ".claude/scripts/message/notifier.sh"
    command = f"{shlex.quote(str(python))} {shlex.quote(str(Path(__file__).resolve()))} due"
    _ = subprocess.run(["zsh", str(notifier), "new", "notify-review", "--every", "1",
                        "--run", command], capture_output=True, text=True, check=True, timeout=2)


def hold(held: Held) -> None:
    with locked():
        path = held_path(held.source)
        if path.exists() or path.with_suffix(".sending").exists():
            raise OSError("source already held")
        path.parent.mkdir(parents=True, exist_ok=True)
        record: HeldRecord = {"source": held.source, "priority": held.draft.priority,
                              "created": held.created, "draft": draft_record(held.draft),
                              "best_version": draft_record(held.best_version), "questions": held.questions}
        scratch = path.with_suffix(".tmp")
        try:
            _ = scratch.write_text(json.dumps(record), encoding="utf-8")
            _ = scratch.replace(path)
            schedule()
        except (OSError, subprocess.SubprocessError):
            path.unlink(missing_ok=True)
            scratch.unlink(missing_ok=True)
            raise


def _claim(path: Path) -> bool:
    """Take or recover a claim while holding the state lock."""
    sending = path.with_suffix(".sending")
    now = time.time()
    if sending.exists():
        if now - sending.stat().st_mtime < CLAIM_STALE_SECONDS:
            return False
        os.utime(sending, (now, now))
        return True
    if not path.exists():
        return False
    # Stamp before renaming so an interrupted rename cannot leave an unstamped claim.
    os.utime(path, (now, now))
    _ = path.replace(sending)
    return True


def claim(source: str) -> bool:
    with locked():
        return _claim(held_path(source))


def release(source: str) -> None:
    with locked():
        held_path(source).with_suffix(".sending").unlink(missing_ok=True)
        held_path(source).unlink(missing_ok=True)


def restore(source: str) -> None:
    with locked():
        path = held_path(source)
        sending = path.with_suffix(".sending")
        if sending.exists():
            _ = sending.replace(path)


class PushChannel(Protocol):
    LOG_FILE: Path

    def http_post(self, url: str, data: bytes) -> tuple[int, bytes]: ...

    def deliver(self, draft: Draft, keys_file: Path,
                post: Callable[[str, bytes], tuple[int, bytes]], *, detail: str) -> int: ...


def push_channel() -> PushChannel:
    return cast(PushChannel, cast(object, importlib.import_module("pushover")))


def expiry_post(url: str, data: bytes) -> tuple[int, bytes]:
    return push_channel().http_post(url, data)


def due(*, post: Callable[[str, bytes], tuple[int, bytes]] = expiry_post,
        keys_file: Path = EXPIRY_KEYS_FILE) -> int:
    channel = push_channel()
    directory = state_path() / "held"
    with locked():
        paths = sorted({path.with_suffix(".json") for pattern in ("*.json", "*.sending")
                        for path in directory.glob(pattern)})
    failed = False
    for path in paths:
        with locked():
            sending = path.with_suffix(".sending")
            recovering = sending.exists()
            if recovering and time.time() - sending.stat().st_mtime < CLAIM_STALE_SECONDS:
                continue
            found = _read_hold(sending if recovering else path)
            if not isinstance(found, LiveHold):
                continue
            held = found.hold
            if not recovering and time.time() - held.created < HOLD_SECONDS:
                continue
            if not _claim(path):
                continue
        try:
            code = channel.deliver(held.best_version, keys_file, post,
                                    detail="sent after hold expired")
        except Exception as error:
            code = 1
            why = f"{type(error).__name__}: {error}"
        else:
            why = f"pushover exit {code}"
        if code == 0:
            release(held.source)
        else:
            restore(held.source)
            failed = True
            log_review(held.draft, Unreviewed(f"expired send failed: {why}"))
    return int(failed)


def best_version(draft: Draft, outcome: ReviewOutcome) -> Draft:
    if isinstance(outcome, Approved):
        return outcome.draft
    if isinstance(outcome, Rewritten):
        return outcome.version
    if isinstance(outcome, Questions):
        return outcome.best_version
    return draft


def print_review(draft: Draft, outcome: ReviewOutcome, stamp: str = "") -> None:
    verdict = "send"
    failed: list[str] = []
    if isinstance(outcome, Unreviewed):
        verdict = "unreviewed: " + outcome.why
    elif isinstance(outcome, Rewritten):
        verdict, failed = "rewrite", outcome.failed_rules
    elif isinstance(outcome, Questions):
        verdict, failed = "ask", outcome.failed_rules
    version = best_version(draft, outcome)
    print(f"{stamp + ' ' if stamp else ''}{verdict}; failed rules: {', '.join(failed) or 'none'}")
    print(version.title)
    print(first_line(version.action))
    print(version.body)
    if isinstance(outcome, Questions):
        for index, question in enumerate(outcome.questions, 1):
            print(f"{index}. {question}")


def replay(limit: int = 0) -> int:
    channel = push_channel()
    try:
        lines = channel.LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        print(f"review: cannot read push log: {error}", file=sys.stderr)
        return 1
    count = 0
    for line in reversed(lines):
        try:
            record = _object(cast(object, json.loads(line)))
            if not _text(record, "outcome").startswith("sent"):
                continue
            message = _text(record, "message")
            action_line, _, body = message.partition("\n")
            if action_line.startswith("Action: "):
                action: UserAction = ActionRequired(action_line.removeprefix("Action: "))
            elif action_line == "No action needed.":
                action = NoActionRequired()
            else:
                action, body = NoActionRequired(), message
            draft = Draft(_text(record, "source"), _text(record, "priority"),
                          _text(record, "title"), action, body, False)
            stamp = _text(record, "time")
        except ValueError:
            continue
        outcome = review(draft, False)
        print_review(draft, outcome, stamp)
        log_review(draft, outcome, replay=True)
        count += 1
        if limit and count >= limit:
            break
    return 0


def switch(direction: str) -> int:
    path = state_path() / "review_off"
    if direction == "off":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    elif direction == "on":
        path.unlink(missing_ok=True)
    print("review: " + ("off" if path.exists() else "on"))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser("due")
    replay_parser = commands.add_parser("replay")
    _ = replay_parser.add_argument("--limit", type=int, default=0)
    switch_parser = commands.add_parser("switch")
    _ = switch_parser.add_argument("direction", choices=("on", "off", "status"))
    check = commands.add_parser("check")
    _ = check.add_argument("--title", required=True)
    action = check.add_mutually_exclusive_group(required=True)
    _ = action.add_argument("--action")
    _ = action.add_argument("--no-action", action="store_true")
    _ = check.add_argument("--text", required=True)
    _ = check.add_argument("--priority", choices=("0", "1", "2"), default="0")
    arguments = parser.parse_args(argv)
    command = cast(str, arguments.command)
    if command == "due":
        return due()
    if command == "replay":
        limit = cast(int, arguments.limit)
        if limit < 0:
            parser.error("--limit must be non-negative")
        return replay(limit)
    if command == "switch":
        return switch(cast(str, arguments.direction))
    chosen = parse_user_action(cast(str | None, arguments.action), cast(bool, arguments.no_action))
    if not isinstance(chosen, (ActionRequired, NoActionRequired)):
        parser.error("state a non-empty action")
    draft = Draft("review.check", cast(str, arguments.priority), cast(str, arguments.title),
                  chosen, cast(str, arguments.text), False)
    outcome = review(draft, False)
    print_review(draft, outcome)
    log_review(draft, outcome, dry=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
