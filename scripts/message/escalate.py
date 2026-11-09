#!/usr/bin/env python3
"""Hold news for the user, and send it when they have not typed in a terminal in time.

  escalate.py hold KEY --text-id ID --value KEY=VALUE ...
                   [--minutes 15] [--question PATH]
  escalate.py close KEY    the news is over; the next hold of KEY starts a new wait, and a sent
                           --need blocked push stops repeating (send.py retire --to user)
  escalate.py typed        the user typed in a terminal: the prompt hook's call
  escalate.py due          settle each waiting message: the notifier job's call
  escalate.py list         each held message and where it stands

`hold` is for news a terminal already shows. It waits `--minutes`: if the user
types in any terminal in that time it is never sent, and if not, `due` sends it
through `push_texts.user_send_argv()` with its registered template. Holding a KEY already held changes nothing, so a caller
may hold on every check and `close` once the news stops being true.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, TypedDict, cast

from scripts.lib.process_failure import failure_detail
from scripts.production.showrunners import load_settings
from scripts.production import staff
from scripts.message import send_budget, session_send
from scripts.notify import push_texts
from scripts.notify import push_reference
from scripts.whoami.agent_accounts import (KnownCodexAccount, UnknownCodexAccount, StoredCodexAccount,
                                    codex_account, workers_home)
from scripts.notify.user_action import ActionRequired, NoActionRequired, UserAction

if TYPE_CHECKING:
    from scripts.message.send import Need

STATE = Path(os.environ.get("ESCALATE_STATE_DIR")
             or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "escalate")
SEND = Path(os.environ.get("ESCALATE_SEND") or session_send.SEND_SCRIPT)
NOTIFIER = Path(os.environ.get("ESCALATE_NOTIFIER") or Path(__file__).with_name("notifier.sh"))
PY = Path(__file__).resolve().parent.parent / "lib" / "py"
JOB = "escalate"
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
USAGE = ("usage: escalate.py hold KEY --text-id ID --value KEY=VALUE ... "
         + "[--minutes N] [--question PATH] "
         + " | close KEY | typed | due | list")

Outcome = Literal["waiting", "sent", "answered", "review_held"]


class _StoredActionRequired(TypedDict):
    kind: Literal["required"]
    text: str


class _StoredNoActionRequired(TypedDict):
    kind: Literal["none"]


_StoredUserAction = _StoredActionRequired | _StoredNoActionRequired


@dataclass(frozen=True)
class LegacyHeld:
    summary: str
    text: str
    need: Need
    held_at: float
    minutes: int
    outcome: Outcome


@dataclass(frozen=True)
class UnownedHold:
    """A hold for another tool, or a record predating Codex account ownership."""


HoldOwner = UnownedHold | KnownCodexAccount
UNOWNED_HOLD = UnownedHold()


class HeldRelay(TypedDict):
    recipient: str
    key: str


@dataclass(frozen=True)
class SessionQuestionOrigin:
    session_id: str


@dataclass(frozen=True)
class UnknownQuestionOrigin:
    """An old hold or a caller outside a recorded Claude session."""


QuestionOrigin = SessionQuestionOrigin | UnknownQuestionOrigin
UNKNOWN_QUESTION_ORIGIN = UnknownQuestionOrigin()


def current_question_origin() -> QuestionOrigin:
    pid = os.environ.get("CLAUDE_PID", "")
    if not pid.isdigit():
        return UNKNOWN_QUESTION_ORIGIN
    try:
        value = cast(object, json.loads((Path.home() / ".claude/sessions" / f"{pid}.json").read_text()))
        if isinstance(value, dict):
            session_id = cast(dict[str, object], value).get("sessionId")
            if isinstance(session_id, str) and session_id and Path(session_id).name == session_id:
                return SessionQuestionOrigin(session_id)
    except (OSError, ValueError):
        pass
    return UNKNOWN_QUESTION_ORIGIN


class StoredHeldMessage(TypedDict):
    summary: str
    text: str
    need: Need
    held_at: float
    minutes: int
    outcome: Outcome
    action: _StoredUserAction
    origin_session: NotRequired[str]
    codex_owner: NotRequired[StoredCodexAccount]
    relays: NotRequired[list[HeldRelay]]
    review_reason: NotRequired[str]
    text_id: NotRequired[str]
    values: NotRequired[dict[str, str]]
    reference: NotRequired[push_reference.PushReference]


@dataclass(frozen=True)
class SessionPush:
    summary: str
    text: str
    action: UserAction
    need: Need = "decision"


@dataclass(frozen=True)
class HeldMessage:
    push: SessionPush
    held_at: float
    minutes: int
    outcome: Outcome
    owner: HoldOwner = field(default_factory=UnownedHold)
    relays: tuple[HeldRelay, ...] = ()
    origin: QuestionOrigin = UNKNOWN_QUESTION_ORIGIN
    # Non-empty until the held review result has reached the question's sender.
    review_reason: str = ""

    @property
    def summary(self) -> str:
        return self.push.summary

    @property
    def text(self) -> str:
        return self.push.text

    @property
    def need(self) -> Need:
        return self.push.need

    @property
    def action(self) -> UserAction:
        return self.push.action


@dataclass(frozen=True, kw_only=True)
class RegisteredHold(HeldMessage):
    text_id: str
    values: tuple[tuple[str, str], ...]
    reference: push_reference.PushReference = field(default_factory=lambda: push_reference.NO_REFERENCE)


@dataclass(frozen=True)
class UnreadableHeld:
    pass


@dataclass(frozen=True)
class RetiredScriptHold:
    """An on-disk script hold from before script warnings left this queue."""
    text_id: str


class RetiredHoldRecord(TypedDict):
    time: float
    key: str
    text_id: str
    reason: str


@dataclass(frozen=True)
class NoHeld:
    pass


ReadHeld = HeldMessage | LegacyHeld | UnreadableHeld | RetiredScriptHold | NoHeld


class Settled(NamedTuple):
    line: str
    failed: bool


@contextmanager
def locked() -> Generator[None]:
    """One writer at a time, so a close never loses to a `due` settling the same key."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def held_path(key: str) -> Path:
    return STATE / "held" / f"{key}.json"


def _legacy_held(value: object) -> LegacyHeld | None:
    if not isinstance(value, dict):
        return None
    record = cast(dict[str, object], value)
    if not isinstance(record.get("summary"), str) or not isinstance(record.get("text"), str):
        return None
    if record.get("need") not in ("note", "decision", "blocked"):
        return None
    if not isinstance(record.get("held_at"), (int, float)) or not isinstance(record.get("minutes"), int):
        return None
    if record.get("outcome") not in ("waiting", "sent", "answered", "review_held"):
        return None
    return LegacyHeld(
        summary=cast(str, record["summary"]),
        text=cast(str, record["text"]),
        need=cast("Need", record["need"]),
        held_at=float(cast(float, record["held_at"])),
        minutes=cast(int, record["minutes"]),
        outcome=cast(Outcome, record["outcome"]),
    )


def _read_user_action(value: object) -> UserAction | UnreadableHeld:
    if not isinstance(value, dict):
        return UnreadableHeld()
    action = cast(dict[str, object], value)
    if action == {"kind": "none"}:
        return NoActionRequired()
    if set(action) == {"kind", "text"} and action.get("kind") == "required" \
            and isinstance(action.get("text"), str):
        return ActionRequired(cast(str, action["text"]))
    return UnreadableHeld()


def read(path: Path) -> ReadHeld:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoHeld()
    except (OSError, ValueError):
        return UnreadableHeld()
    legacy = _legacy_held(value)
    record = cast(dict[str, object], value) if isinstance(value, dict) else {}
    text_id = record.get("text_id", "")
    values = record.get("values", {})
    registered_values: dict[str, str] = {}
    if "text_id" in record or "values" in record:
        if not isinstance(text_id, str) or not text_id or not isinstance(values, dict):
            return UnreadableHeld()
        raw_values = cast(dict[str, object], values)
        if not all(isinstance(item, str) for item in raw_values.values()):
            return UnreadableHeld()
        registered_values = cast(dict[str, str], raw_values)
        if text_id not in {entry.id for entry in push_texts.catalog()}:
            return RetiredScriptHold(text_id)
        try:
            _ = push_texts.render(text_id, **registered_values)
        except ValueError:
            return UnreadableHeld()
    if legacy is None:
        return UnreadableHeld()
    if "action" not in record:
        return legacy
    action = _read_user_action(record["action"])
    if isinstance(action, UnreadableHeld):
        return action
    owner = _read_owner(record.get("codex_owner"))
    if isinstance(owner, UnreadableHeld):
        return owner
    origin = record.get("origin_session")
    origin = SessionQuestionOrigin(origin) if isinstance(origin, str) and origin else UNKNOWN_QUESTION_ORIGIN
    review_reason = record.get("review_reason", "")
    if not isinstance(review_reason, str):
        return UnreadableHeld()
    push = SessionPush(legacy.summary, legacy.text, action, legacy.need)
    if text_id:
        try:
            reference = push_reference.read_reference(record.get("reference"))
        except ValueError:
            return UnreadableHeld()
        return RegisteredHold(push, legacy.held_at, legacy.minutes, legacy.outcome,
            owner, _read_relays(record.get("relays")), origin, review_reason,
            text_id=cast(str, text_id), values=tuple(registered_values.items()), reference=reference)
    return HeldMessage(push, legacy.held_at, legacy.minutes, legacy.outcome,
                       owner, _read_relays(record.get("relays")), origin, review_reason)


def _read_relays(value: object) -> tuple[HeldRelay, ...]:
    if not isinstance(value, list):
        return ()
    relays: list[HeldRelay] = []
    for item in cast(list[object], value):
        if isinstance(item, dict):
            fields = cast(dict[str, object], item)
            recipient, key = fields.get("recipient"), fields.get("key")
            if isinstance(recipient, str) and isinstance(key, str):
                relays.append(HeldRelay(recipient=recipient, key=key))
    return tuple(relays)


def _read_owner(value: object) -> HoldOwner | UnreadableHeld:
    if value is None:
        return UnownedHold()
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        home, login, label = fields.get("home"), fields.get("login"), fields.get("label")
        if isinstance(home, str) and isinstance(login, str) and isinstance(label, str):
            return KnownCodexAccount(Path(home), login, label)
    return UnreadableHeld()


def write(path: Path, held: HeldMessage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    record: StoredHeldMessage = {
        "summary": held.summary,
        "text": held.text,
        "need": held.need,
        "held_at": held.held_at,
        "minutes": held.minutes,
        "outcome": held.outcome,
        "action": _stored_action(held.action),
    }
    if isinstance(held, RegisteredHold):
        record["text_id"] = held.text_id
        record["values"] = dict(held.values)
        record["reference"] = held.reference
    if isinstance(held.origin, SessionQuestionOrigin):
        record["origin_session"] = held.origin.session_id
    if held.relays:
        record["relays"] = list(held.relays)
    if held.review_reason:
        record["review_reason"] = held.review_reason
    if isinstance(held.owner, KnownCodexAccount):
        record["codex_owner"] = StoredCodexAccount(home=str(held.owner.home),
            login=held.owner.login, label=held.owner.label)
    _ = scratch.write_text(json.dumps(record), encoding="utf-8")
    _ = scratch.replace(path)


def typed() -> None:
    """Record that the user typed in a terminal just now."""
    STATE.mkdir(parents=True, exist_ok=True)
    _ = (STATE / "typed").write_text(repr(time.time()), encoding="utf-8")


def typed_at() -> float:
    try:
        return float((STATE / "typed").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0


def schedule() -> None:
    """Have the notifier run `due` every minute; a repeated `new` changes nothing."""
    _ = subprocess.run(["zsh", str(NOTIFIER), "new", JOB, "--every", "1",
                        "--run", f"{PY} {Path(__file__).resolve()} due"], capture_output=True, text=True, check=True)


def _stored_action(action: UserAction) -> _StoredUserAction:
    if isinstance(action, ActionRequired):
        return {"kind": "required", "text": action.text}
    return {"kind": "none"}


def hold(key: str, push: SessionPush | push_texts.RenderedPush, minutes: int = 15,
         *, question: str = "", owner: HoldOwner = UNOWNED_HOLD,
         relays: tuple[HeldRelay, ...] = (), origin: QuestionOrigin = UNKNOWN_QUESTION_ORIGIN) -> bool:
    """Start KEY's wait; false when KEY is already held."""
    rendered = push if isinstance(push, push_texts.RenderedPush) else None
    reference = push_reference.NO_REFERENCE if rendered is None else push_reference.admission_reference(rendered.id, question)
    if rendered is not None:
        push = SessionPush(rendered.title, rendered.body, rendered.action, rendered.need)
    if isinstance(push.action, NoActionRequired) and push.need != "note":
        raise ValueError(f"need {push.need} requires an action")
    with locked():
        path = held_path(key)
        existing = read(path)
        if isinstance(existing, RetiredScriptHold):
            _ = discard_script_hold(path, existing)
        if isinstance(existing, (HeldMessage, LegacyHeld, UnreadableHeld)):
            return False
        schedule()
        held = HeldMessage(cast(SessionPush, push), time.time(), minutes, "waiting", owner, relays, origin)
        if rendered is not None:
            held = RegisteredHold(held.push, held.held_at, minutes, "waiting", owner, relays, origin,
                                  "", text_id=rendered.id, values=tuple(rendered.values.items()), reference=reference)
        write(path, held)
    return True


def discard_script_hold(path: Path, held: RetiredScriptHold | RegisteredHold,
                        reason: str = "script warnings no longer escalate to the phone") -> Settled:
    """Under the state lock, log an obsolete script hold once and close it."""
    record: RetiredHoldRecord = {"time": time.time(), "key": path.stem,
                                "text_id": held.text_id, "reason": reason}
    with (STATE / "log.jsonl").open("a", encoding="utf-8") as output:
        _ = output.write(json.dumps(record) + "\n")
    path.unlink()
    return Settled(f"{path.stem}: closed script hold {held.text_id}: {reason}", False)


def user_key(key: str) -> str:
    """The send.py --key of KEY's user push."""
    return f"{JOB}-{key}"


def close(key: str) -> bool:
    """Forget KEY, and stop its emergency push if one was sent; false when it was not held."""
    with locked():
        path = held_path(key)
        held = read(path)
        if isinstance(held, RetiredScriptHold):
            _ = discard_script_hold(path, held)
            return True
        if isinstance(held, NoHeld):
            return False
        if isinstance(held, HeldMessage) and held.outcome == "sent" and held.need == "blocked":
            try:
                done = subprocess.run(session_send.send_argv("retire", "--to", "user", "--from", JOB,
                                       "--key", user_key(key), send_script=SEND), capture_output=True, text=True, check=False, timeout=60)
                problem = "" if done.returncode == 0 else (
                    failure_detail(done, lines=1)).replace("\n", "; ")
            except (OSError, subprocess.TimeoutExpired) as error:
                problem = str(error)
            if problem:
                print(f"escalate.py: {key}: {problem}", file=sys.stderr)
                return True
        path.unlink()
    return True


def _outgoing_codex_hold(key: str, held: ReadHeld, home: Path, login: str) -> bool:
    """Registered holds for an old Codex key or another account must retire."""
    return key == "five-hour-codex" or (isinstance(held, HeldMessage)
            and isinstance(held.owner, KnownCodexAccount)
            and (held.owner.home != home or held.owner.login.casefold() != login.casefold()))


def _cancel_outgoing_codex_holds(home: Path, login: str) -> None:
    for path in (STATE / "held").glob("*.json"):
        held = read(path)
        if isinstance(held, RetiredScriptHold):
            _ = discard_script_hold(path, held)
            continue
        if isinstance(held, (HeldMessage, LegacyHeld)) and not isinstance(held, RegisteredHold):
            continue
        if _outgoing_codex_hold(path.stem, held, home, login):
            relays = held.relays if isinstance(held, HeldMessage) else ()
            if not relays:
                try:
                    names = [*load_settings()["always"], *(f"session:{row.current_session}" for row in staff.active(staff.snapshot(), "showrunner"))]
                except (OSError, ValueError):
                    continue
                label = held.owner.label if isinstance(held, HeldMessage) and isinstance(held.owner, KnownCodexAccount) else "Codex"
                relays = tuple(HeldRelay(recipient=name, key=f"5-hour {label}") for name in dict.fromkeys(names))
            for relay in relays:
                _ = subprocess.run(session_send.send_argv("retire", "--to", relay["recipient"],
                                    "--from", "quota_alert", "--key", relay["key"], send_script=SEND),
                                   capture_output=True, text=True, check=False)
            path.unlink(missing_ok=True)


def reconcile_codex_holds(home: Path, login: str) -> None:
    """Cancel registered Codex holds after an account selection. Keep legacy free-text holds."""
    with locked():
        _cancel_outgoing_codex_holds(home, login)


def deliver(held: HeldMessage, key: str) -> str:
    """Send one held message to the user; the empty string when sent, else send.py's own line."""
    from scripts.notify import review

    if not isinstance(held, RegisteredHold):
        return "template revision required"
    rendered = push_texts.render(held.text_id, **dict(held.values))
    command = push_texts.user_send_argv(rendered, sender=JOB, key=user_key(key),
                                      self_clearing=held.need == "blocked", send_script=SEND,
                                      question=held.reference["path"] if held.reference["kind"] == "question" else "")
    done = subprocess.run(command, capture_output=True, text=True, check=False,
                          timeout=send_budget.user_seconds(review.user_send_seconds()))
    return "" if done.returncode == 0 else " ".join((done.stdout or done.stderr).split()) or f"exit {done.returncode}"


def forward_review_hold(held: HeldMessage, reason: str, entry_key: str) -> list[str]:
    """Tell the sessions whose question was relayed; the user push is terminal."""
    errors: list[str] = []
    relays = list(held.relays)
    if isinstance(held.origin, SessionQuestionOrigin):
        recipient = "session:" + held.origin.session_id
        if all(relay["recipient"] != recipient for relay in relays):
            relays.append(HeldRelay(recipient=recipient, key="review-held-" + entry_key))
    for relay in relays:
        try:
            done = subprocess.run(session_send.send_argv("--to", relay["recipient"], "--from", JOB,
                                   "--key", relay["key"], "--summary", "Relayed push needs revision",
                                   "--text", f"Your relayed push {held.summary!r} was not sent. {reason} Revise and resend it.", send_script=SEND),
                                  capture_output=True, text=True, check=False, timeout=send_budget.seconds())
            if done.returncode not in (0, 1):
                errors.append(f"{relay['recipient']}: {failure_detail(done)}")
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{relay['recipient']}: {error}")
    return errors


def due(now: float) -> list[Settled]:
    """Settle each waiting message: answered when the user typed since it was held, sent once its time is up."""
    settled: list[Settled] = []
    with locked():
        for path in sorted((STATE / "held").glob("*.json")):
            held = read(path)
            if isinstance(held, RetiredScriptHold):
                settled.append(discard_script_hold(path, held))
        try:
            identity = codex_account(workers_home())
        except (OSError, ValueError):
            identity = UnknownCodexAccount(Path.home() / ".codex", "workers identity cannot be read")
        if isinstance(identity, KnownCodexAccount):
            _cancel_outgoing_codex_holds(identity.home, identity.login)
        seen = typed_at()
        for path in sorted((STATE / "held").glob("*.json")):
            held = read(path)
            if isinstance(held, (HeldMessage, LegacyHeld)) and not isinstance(held, RegisteredHold):
                owner = (held.origin.session_id if isinstance(held, HeldMessage)
                         and isinstance(held.origin, SessionQuestionOrigin) else
                         held.owner.label if isinstance(held, HeldMessage)
                         and isinstance(held.owner, KnownCodexAccount) else path.stem)
                print(f"escalate.py: {owner}: legacy hold needs a template revision; not delivered", file=sys.stderr)
                continue
            if not isinstance(held, RegisteredHold):
                continue
            if held.outcome == "review_held" and held.review_reason:
                errors = forward_review_hold(held, held.review_reason, path.stem)
                if not errors:
                    write(path, replace(held, review_reason=""))
                settled.extend(Settled(f"{path.stem}: held result relay failed: {error}", True) for error in errors)
                continue
            if held.outcome != "waiting":
                continue
            if held.reference["kind"] == "question":
                try:
                    opening = push_reference.read_question(Path(held.reference["path"]))
                except (OSError, ValueError) as error:
                    settled.append(Settled(f"{path.stem}: NOT sent: {error}", True))
                    continue
                reason = ("question closed" if opening.status == "closed" else
                          "question reopened" if opening.since != held.reference["since"] else "")
                if reason:
                    settled.append(discard_script_hold(path, held, reason))
                    continue
            if (path.stem == "five-hour-codex" or isinstance(held.owner, KnownCodexAccount)) and isinstance(identity, UnknownCodexAccount):
                continue
            if (isinstance(identity, KnownCodexAccount)
                    and _outgoing_codex_hold(path.stem, held, identity.home, identity.login)):
                # Cancellation may need a later inventory; the outgoing account must never alert meanwhile.
                continue
            if seen >= held.held_at:
                held = replace(held, outcome="answered")
                line = "the user typed in a terminal; not sent"
            elif now < held.held_at + held.minutes * 60:
                continue
            elif problem := deliver(held, path.stem):
                if not problem.startswith("HELD:"):
                    settled.append(Settled(f"{path.stem}: NOT sent: {problem}", True))
                    continue
                held = replace(held, outcome="review_held", review_reason=problem)
                write(path, held)
                errors = forward_review_hold(held, problem, path.stem)
                if not errors:
                    write(path, replace(held, review_reason=""))
                settled.append(Settled(f"{path.stem}: {problem}", False))
                settled.extend(Settled(f"{path.stem}: held result relay failed: {error}", True) for error in errors)
                continue
            else:
                held = replace(held, outcome="sent")
                line = "sent to the user"
            write(path, held)
            settled.append(Settled(f"{path.stem}: {line}", False))
    return settled


def listing(now: float) -> list[str]:
    lines: list[str] = []
    for path in sorted((STATE / "held").glob("*.json")):
        held = read(path)
        if isinstance(held, RetiredScriptHold):
            lines.append(f"{path.stem} - obsolete script hold {held.text_id}; closes on the next tick")
            continue
        if isinstance(held, LegacyHeld) and not isinstance(held, HeldMessage):
            lines.append(f"{path.stem} - waiting for a hold that states its action")
            continue
        if not isinstance(held, HeldMessage):
            continue
        left = max(math.ceil((held.held_at + held.minutes * 60 - now) / 60), 0)
        lines.append(f"{path.stem} - " + (f"waiting, sent in {left} min" if held.outcome == "waiting"
                                          else held.outcome) + f" - {held.summary}")
    return lines or ["nothing held"]


def registered_hold_options(arguments: list[str]) -> tuple[push_texts.RenderedPush, int, str]:
    text_id = ""
    question = ""
    minutes = "15"
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        if index + 1 >= len(arguments):
            raise ValueError(USAGE)
        option, value = arguments[index:index + 2]
        if option == "--text-id" and not text_id:
            text_id = value
        elif option == "--question":
            question = value
        elif option == "--minutes":
            minutes = value
        elif option == "--value":
            key, separator, item = value.partition("=")
            if not separator or key in values:
                raise ValueError("--value needs distinct key=value pairs")
            values[key] = item
        else:
            raise ValueError(USAGE + "; " + push_texts.TEMPLATE_REQUIRED)
        index += 2
    if not text_id or not minutes.isdigit():
        raise ValueError(USAGE + "; " + push_texts.TEMPLATE_REQUIRED)
    return push_texts.render(text_id, **values), int(minutes), question


def main(arguments: list[str]) -> int:
    match arguments:
        case ["hold", key, *pairs] if KEY.fullmatch(key):
            try:
                rendered, minutes, question = registered_hold_options(pairs)
                started = hold(key, rendered, minutes, question=question, origin=current_question_origin())
            except ValueError as error:
                print(f"escalate.py: {error}", file=sys.stderr)
                return 2
            print(f"{key}: held; sent to the user in {minutes} min unless they type in a terminal"
                  if started else f"{key}: already held")
        case ["close", key] if KEY.fullmatch(key):
            print(f"{key}: closed" if close(key) else f"{key}: not held")
        case ["typed"]:
            typed()
        case ["due"]:
            settled = due(time.time())
            for one in settled:
                print(one.line)
            return 1 if any(one.failed for one in settled) else 0
        case ["list"]:
            print("\n".join(listing(time.time())))
        case _:
            print(USAGE, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
