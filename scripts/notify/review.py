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
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
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
NOTIFY_CONFIG = ROOT / "config/notify.conf"
HOLD_SECONDS = 300
# Minimum age; the configured review and post budgets may require more time.
CLAIM_STALE_SECONDS = 120
DELIVERED_KEEP_SECONDS = 3600
EXPIRY_KEYS_FILE = Path.home() / ".config/pushover/env"


def review_timeout_seconds() -> float:
    """Read the reviewer limit once; configuration faults use the 60 s default."""
    try:
        contents = NOTIFY_CONFIG.read_text(encoding="utf-8")
        for line in contents.splitlines():
            key, separator, value = line.partition("#")[0].partition("=")
            if separator and key.strip() == "REVIEW_TIMEOUT_SECONDS":
                seconds = float(value.strip())
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError("REVIEW_TIMEOUT_SECONDS must be positive and finite")
                return seconds
        raise ValueError("missing REVIEW_TIMEOUT_SECONDS")
    except (OSError, ValueError) as error:
        print(f"review: {NOTIFY_CONFIG}: {error}; using REVIEW_TIMEOUT_SECONDS=60", file=sys.stderr)
        return 60.0


@dataclass(frozen=True)
class DeliveryWaits:
    revision: float
    user_send: float
    claim_stale: float
    posting: float


def delivery_waits() -> DeliveryWaits:
    revision = review_timeout_seconds() + push_channel().TIMEOUT_S
    return DeliveryWaits(revision, revision + 10, max(CLAIM_STALE_SECONDS, revision + 10),
                         min(110, revision + 5))


def revision_seconds() -> float:
    return delivery_waits().revision


def user_send_seconds() -> float:
    return delivery_waits().user_send


def claim_stale_seconds() -> float:
    return delivery_waits().claim_stale


def posting_wait_seconds() -> float:
    return delivery_waits().posting


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
class RuleFailure:
    rule: str
    quote: str


@dataclass(frozen=True)
class Rewritten:
    version: Draft
    failed_rules: list[str]
    metadata: ReviewMetadata = ReviewMetadata()
    failures: tuple[RuleFailure, ...] = ()


@dataclass(frozen=True)
class Questions:
    best_version: Draft
    questions: list[str]
    failed_rules: list[str]
    metadata: ReviewMetadata = ReviewMetadata()
    failures: tuple[RuleFailure, ...] = ()


@dataclass(frozen=True)
class Unreviewed:
    why: str
    metadata: ReviewMetadata = ReviewMetadata()
    failures: tuple[RuleFailure, ...] = ()


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


class ReleaseOutcome(Enum):
    DELIVERED = "delivered"
    RELEASED = "released"
    POSTING = "posting"
    NONE = "none"


@dataclass(frozen=True)
class ClaimSent:
    pass


@dataclass(frozen=True)
class ClaimCancelled:
    pass


@dataclass(frozen=True)
class ClaimFailed:
    code: int


ClaimDelivery = ClaimSent | ClaimCancelled | ClaimFailed


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


class RuleFailureRecord(TypedDict):
    rule: str
    quote: str


class VerdictRecord(MessageRecord):
    verdict: Literal["send", "rewrite", "ask"]
    questions: list[str]
    failed_rules: list[RuleFailureRecord]


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
    failures: NotRequired[list[RuleFailureRecord]]


class ItemSchema(TypedDict, total=False):
    type: str
    properties: dict[str, dict[str, str]]
    required: list[str]
    additionalProperties: bool


class PropertySchema(TypedDict, total=False):
    type: str | list[str]
    enum: list[str]
    items: ItemSchema
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
        "failed_rules": {"type": "array", "items": {"type": "object",
            "properties": {"rule": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["rule", "quote"], "additionalProperties": False}},
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


def length_failures(draft: Draft) -> list[str]:
    failures: list[str] = []
    if len(draft.title) >= 45:
        failures.append("push-title-under-45")
    if len(first_line(draft.action) + "\n" + draft.body) >= 500:
        failures.append("push-body-under-500")
    return failures


def supported_failures(value: object, draft: Draft) -> list[RuleFailure]:
    if not isinstance(value, list):
        raise ValueError("failed_rules")
    original = [draft.title, first_line(draft.action) + "\n" + draft.body]
    if isinstance(draft.action, ActionRequired):
        original.append(draft.action.text)
    failures: list[RuleFailure] = []
    for item in cast(list[object], value):
        record = _object(item)
        rule, quote = _text(record, "rule"), _text(record, "quote")
        if rule in ("push-title-under-45", "push-body-under-500"):
            continue  # Length rules are decided by the code count.
        if rule and quote and any(quote in text for text in original) and all(item.rule != rule for item in failures):
            failures.append(RuleFailure(rule, quote))
    for rule in length_failures(draft):
        quote = draft.title if rule == "push-title-under-45" else first_line(draft.action) + "\n" + draft.body
        failures.append(RuleFailure(rule, quote))
    return failures


def validate(value: object, draft: Draft, revision: bool,
             metadata: ReviewMetadata = DEFAULT_METADATA) -> ReviewOutcome:
    failures: list[RuleFailure] = []
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
        failures = supported_failures(record.get("failed_rules"), draft)
        failed = [failure.rule for failure in failures]
        if len(questions) > 3:
            raise ValueError("questions exceeds 3")
        if verdict != "ask" and questions:
            raise ValueError("questions without ask")
        version = replace(draft, title=title, action=action, body=body)
        if verdict == "send":
            if message_record(version) != message_record(draft):
                raise ValueError("send changed draft")
            if not failed:
                return Approved(draft, metadata)
            return Unreviewed("send failed rules: " + ", ".join(failed), metadata, tuple(failures))
        if not failed:
            return Approved(draft, metadata)
        proposed_lengths = length_failures(version)
        if proposed_lengths:
            raise ValueError("proposed text fails: " + ", ".join(proposed_lengths))
        if verdict == "ask" and not revision:
            if not questions or any(not question.strip() for question in questions):
                raise ValueError("ask needs questions")
            return Questions(version, questions, failed, metadata, tuple(failures))
        return Rewritten(version, failed, metadata, tuple(failures))
    except ValueError as error:
        return Unreviewed(str(error), metadata, tuple(failures))


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
    timeout = 60.0

    def details() -> ReviewMetadata:
        return ReviewMetadata(time.monotonic() - started, account, model)

    def remaining() -> float:
        return max(0.001, timeout - (time.monotonic() - started))

    try:
        timeout = review_timeout_seconds()
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
                        f"Priority: {draft.priority}\nRevision: {str(revision).lower()}\n"
                        f"Code-counted length failures: {', '.join(length_failures(draft)) or 'none'}")
        command = ["claude", "-p", "--model", model, "--effort", effort,
                   "--system-prompt", guide.stdout + "\n" + prompt,
                   "--json-schema", json.dumps(SCHEMA), "--output-format", "json",
                   "--tools", "", "--no-session-persistence", "--setting-sources", "", draft_prompt]
        if time.monotonic() - started >= timeout:
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
        failed = [failure.rule for failure in outcome.failures]
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
    if isinstance(outcome, (Rewritten, Questions, Unreviewed)) and outcome.failures:
        entry["failures"] = [{"rule": failure.rule, "quote": failure.quote} for failure in outcome.failures]
    if replay:
        entry["replay"] = True
    if dry:
        entry["dry"] = True
    if detail:
        entry["detail"] = detail
    _append(entry)


_STATE_MUTEX = threading.RLock()
_LOCK_DEPTH = threading.local()


@contextmanager
def locked(*, timeout: float = math.inf, clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> Generator[None]:
    deadline = clock() + timeout

    def wait() -> None:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("review lock deadline expired")
        sleep(min(0.05, remaining))

    if math.isinf(timeout):
        _ = _STATE_MUTEX.acquire()
    else:
        while True:
            if clock() >= deadline:
                raise TimeoutError("review lock deadline expired")
            if _STATE_MUTEX.acquire(blocking=False):
                break
            wait()
    try:
        depth = cast(int, getattr(_LOCK_DEPTH, "value", 0))
        _LOCK_DEPTH.value = depth + 1
        try:
            if depth:
                yield
            else:
                state_path().mkdir(parents=True, exist_ok=True)
                with (state_path() / "review.lock").open("a") as lock:
                    if math.isinf(timeout):
                        fcntl.flock(lock, fcntl.LOCK_EX)
                    else:
                        while True:
                            if clock() >= deadline:
                                raise TimeoutError("review lock deadline expired")
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                wait()
                    try:
                        yield
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            _LOCK_DEPTH.value = depth
    finally:
        _STATE_MUTEX.release()


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
        if (isinstance(held.draft.action, ActionRequired)
                != isinstance(held.best_version.action, ActionRequired)):
            raise ValueError("hold action form changed")
        if len(held.best_version.title) > 250 or len(first_line(held.best_version.action) + "\n" + held.best_version.body) > 1024:
            raise ValueError("hold transport limits")
        if not 1 <= len(held.questions) <= 3 or any(not question.strip() for question in held.questions):
            raise ValueError("hold questions")
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
        if now - sending.stat().st_mtime < claim_stale_seconds():
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


def hold_identity(held: Held) -> str:
    record: HeldRecord = {"source": held.source, "priority": held.draft.priority,
                          "created": float(held.created), "draft": draft_record(held.draft),
                          "best_version": draft_record(held.best_version), "questions": held.questions}
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def delivered_path(identity: str) -> Path:
    if len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity):
        raise ValueError("invalid hold identity")
    return state_path() / "held/delivered" / identity


def release_if_matches(source: str, identity: str, *, timeout: float = math.inf,
                       clock: Callable[[], float] = time.monotonic,
                       sleep: Callable[[float], None] = time.sleep) -> ReleaseOutcome:
    """Resolve every state of the exact hold a failed revision replaced."""
    with locked(timeout=timeout, clock=clock, sleep=sleep):
        delivered = delivered_path(identity)
        if delivered.exists() and time.time() - delivered.stat().st_mtime < DELIVERED_KEEP_SECONDS:
            return ReleaseOutcome.DELIVERED
        path = held_path(source)
        sending = path.with_suffix(".sending")
        for candidate in (sending, path):
            found = _read_hold(candidate)
            if not isinstance(found, LiveHold) or hold_identity(found.hold) != identity:
                continue
            if candidate == sending and time.time() - sending.stat().st_mtime < claim_stale_seconds():
                return ReleaseOutcome.POSTING
            _ = path.with_suffix(".cancel").write_text(identity, encoding="utf-8")
            candidate.unlink()
            return ReleaseOutcome.RELEASED
        return ReleaseOutcome.NONE


def post_claim(held: Held, send: Callable[[], int]) -> ClaimDelivery:
    """Check cancellation and complete delivery in one state-lock transaction."""
    identity = hold_identity(held)
    with locked():
        path = held_path(held.source)
        sending = path.with_suffix(".sending")
        found = _read_hold(sending)
        cancel = path.with_suffix(".cancel")
        cancelled = cancel.exists() and cancel.read_text(encoding="utf-8") == identity
        if not isinstance(found, LiveHold) or hold_identity(found.hold) != identity:
            return ClaimCancelled()
        delivered = delivered_path(identity)
        already_sent = delivered.exists() and time.time() - delivered.stat().st_mtime < DELIVERED_KEEP_SECONDS
        if cancelled or already_sent:
            sending.unlink()
            return ClaimCancelled()
        try:
            code = send()
        except Exception:
            _ = sending.replace(path)
            raise
        if code:
            _ = sending.replace(path)
            return ClaimFailed(code)
        delivered = delivered_path(identity)
        delivered.parent.mkdir(parents=True, exist_ok=True)
        delivered.touch()
        stamp = time.time()
        os.utime(delivered, (stamp, stamp))
        sending.unlink()
        return ClaimSent()


def restore(source: str) -> None:
    with locked():
        path = held_path(source)
        sending = path.with_suffix(".sending")
        if sending.exists():
            _ = sending.replace(path)


class PushChannel(Protocol):
    LOG_FILE: Path
    TIMEOUT_S: int

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
        for delivered in (directory / "delivered").glob("*"):
            if time.time() - delivered.stat().st_mtime >= DELIVERED_KEEP_SECONDS:
                delivered.unlink()
        paths = sorted({path.with_suffix(".json") for pattern in ("*.json", "*.sending")
                        for path in directory.glob(pattern)})
    failed = False
    for path in paths:
        with locked():
            sending = path.with_suffix(".sending")
            recovering = sending.exists()
            if recovering and time.time() - sending.stat().st_mtime < claim_stale_seconds():
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
            delivery = post_claim(held, lambda: channel.deliver(held.best_version, keys_file, post,
                                                               detail="sent after hold expired"))
        except Exception as error:
            failed = True
            log_review(held.draft, Unreviewed(f"expired send failed: {type(error).__name__}: {error}"))
        else:
            if isinstance(delivery, ClaimFailed):
                failed = True
                log_review(held.draft, Unreviewed(f"expired send failed: pushover exit {delivery.code}"))
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
        failed = [failure.rule for failure in outcome.failures]
    elif isinstance(outcome, Rewritten):
        verdict, failed = "rewrite", outcome.failed_rules
    elif isinstance(outcome, Questions):
        verdict, failed = "ask", outcome.failed_rules
    version = best_version(draft, outcome)
    print(f"{stamp + ' ' if stamp else ''}{verdict}; failed rules: {', '.join(failed) or 'none'}")
    print(version.title)
    print(first_line(version.action))
    print(version.body)
    if isinstance(outcome, (Rewritten, Questions, Unreviewed)):
        for failure in outcome.failures:
            print("failure: " + json.dumps({"rule": failure.rule, "quote": failure.quote}))
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
    release_parser = commands.add_parser("release")
    _ = release_parser.add_argument("--identity", required=True)
    _ = release_parser.add_argument("source")
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
    if command == "release":
        print(release_if_matches(cast(str, arguments.source), cast(str, arguments.identity)).value)
        return 0
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
