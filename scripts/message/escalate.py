#!/usr/bin/env python3
"""Hold news for the user, and send it when they have not typed in a terminal in time.

  escalate.py hold KEY --summary TITLE --text TEXT (--action TEXT | --no-action)
                   [--minutes 15] [--need note|decision|blocked]
  escalate.py close KEY    the news is over; the next hold of KEY starts a new wait
  escalate.py typed        the user typed in a terminal: the prompt hook's call
  escalate.py due          settle each waiting message: the notifier job's call
  escalate.py list         each held message and where it stands

`hold` is for news a terminal already shows. It waits `--minutes`: if the user
types in any terminal in that time it is never sent, and if not, `due` sends it
with `send.py --to user`. Holding a KEY already held changes nothing, so a caller
may hold on every check and `close` once the news stops being true.
"""
from __future__ import annotations

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "production"))
from showrunners import load_settings, registered_showrunners
if TYPE_CHECKING:
    from ..whoami.agent_accounts import (KnownCodexAccount, UnknownCodexAccount, StoredCodexAccount,
                                        codex_account, seats_home)
else:
    from scripts.whoami.agent_accounts import (KnownCodexAccount, UnknownCodexAccount, StoredCodexAccount,
                                codex_account, seats_home)
from user_action import (FIX, ActionRefused, ActionRequired, ActionUnstated, NoActionRequired, UserAction,
                         parse_user_action)

if TYPE_CHECKING:
    from send import Need

STATE = Path(os.environ.get("ESCALATE_STATE_DIR")
             or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "escalate")
SEND = Path(os.environ.get("ESCALATE_SEND") or Path(__file__).with_name("send.py"))
NOTIFIER = Path(os.environ.get("ESCALATE_NOTIFIER") or Path(__file__).with_name("notifier.sh"))
PY = Path(__file__).resolve().parent.parent / "lib" / "py"
JOB = "escalate"
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
USAGE = ("usage: escalate.py hold KEY --summary TITLE --text TEXT (--action TEXT | --no-action) "
         + "[--minutes N] [--need note|decision|blocked]"
         + " | close KEY | typed | due | list")

Outcome = Literal["waiting", "sent", "answered"]


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


class StoredHeldMessage(TypedDict):
    summary: str
    text: str
    need: Need
    held_at: float
    minutes: int
    outcome: Outcome
    action: _StoredUserAction
    codex_owner: NotRequired[StoredCodexAccount]
    relays: NotRequired[list[HeldRelay]]


@dataclass(frozen=True)
class HeldMessage:
    summary: str
    text: str
    need: Need
    held_at: float
    minutes: int
    outcome: Outcome
    action: UserAction
    owner: HoldOwner = field(default_factory=UnownedHold)
    relays: tuple[HeldRelay, ...] = ()


@dataclass(frozen=True)
class UnreadableHeld:
    pass


@dataclass(frozen=True)
class NoHeld:
    pass


ReadHeld = HeldMessage | LegacyHeld | UnreadableHeld | NoHeld


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
    if record.get("outcome") not in ("waiting", "sent", "answered"):
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
    if legacy is None:
        return UnreadableHeld()
    record = cast(dict[str, object], value)
    if "action" not in record:
        return legacy
    action = _read_user_action(record["action"])
    if isinstance(action, UnreadableHeld):
        return action
    owner = _read_owner(record.get("codex_owner"))
    if isinstance(owner, UnreadableHeld):
        return owner
    return HeldMessage(
        summary=legacy.summary,
        text=legacy.text,
        need=legacy.need,
        held_at=legacy.held_at,
        minutes=legacy.minutes,
        outcome=legacy.outcome,
        action=action,
        owner=owner,
        relays=_read_relays(record.get("relays")),
    )


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
    if held.relays:
        record["relays"] = list(held.relays)
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


def hold(key: str, summary: str, text: str, action: UserAction, minutes: int = 15,
         need: Need = "decision", *, owner: HoldOwner = UNOWNED_HOLD,
         relays: tuple[HeldRelay, ...] = ()) -> bool:
    """Start KEY's wait; false when KEY is already held."""
    if isinstance(action, NoActionRequired) and need != "note":
        raise ValueError(f"need {need} requires an action")
    with locked():
        path = held_path(key)
        existing = read(path)
        if isinstance(existing, (HeldMessage, UnreadableHeld)):
            return False
        schedule()
        write(path, HeldMessage(summary, text, need, time.time(), minutes, "waiting", action, owner, relays))
    return True


def close(key: str) -> bool:
    """Forget KEY; false when it was not held."""
    with locked():
        try:
            held_path(key).unlink()
        except FileNotFoundError:
            return False
    return True


def _cancel_outgoing_codex_holds(home: Path, login: str) -> None:
    for path in (STATE / "held").glob("*.json"):
        held = read(path)
        if path.stem == "five-hour-codex" or (isinstance(held, HeldMessage)
                and isinstance(held.owner, KnownCodexAccount)
                and (held.owner.home != home or held.owner.login.casefold() != login.casefold())):
            relays = held.relays if isinstance(held, HeldMessage) else ()
            if not relays:
                try:
                    names = [*load_settings()["always"], *(runner["session"] for runner in registered_showrunners())]
                except (OSError, ValueError):
                    names = []
                label = held.owner.label if isinstance(held, HeldMessage) and isinstance(held.owner, KnownCodexAccount) else "Codex"
                relays = tuple(HeldRelay(recipient=name, key=f"5-hour {label}") for name in dict.fromkeys(names))
            for relay in relays:
                _ = subprocess.run([sys.executable, str(SEND), "retire", "--to", relay["recipient"],
                                    "--from", "quota_alert", "--key", relay["key"]],
                                   capture_output=True, text=True, check=False)
            path.unlink(missing_ok=True)


def reconcile_codex_holds(home: Path, login: str) -> None:
    """Cancel outgoing and legacy Codex holds after a known selection, without a recovery notice."""
    with locked():
        _cancel_outgoing_codex_holds(home, login)


def deliver(held: HeldMessage) -> str:
    """Send one held message to the user; the empty string when sent, else send.py's own line."""
    action_args = ["--action", held.action.text] if isinstance(held.action, ActionRequired) else ["--no-action"]
    done = subprocess.run([sys.executable, str(SEND), "--to", "user", "--from", JOB, "--summary", held.summary,
                           "--need", held.need, *action_args, "--text", held.text],
                          capture_output=True, text=True, check=False)
    return "" if done.returncode == 0 else " ".join((done.stdout or done.stderr).split()) or f"exit {done.returncode}"


def due(now: float) -> list[Settled]:
    """Settle each waiting message: answered when the user typed since it was held, sent once its time is up."""
    settled: list[Settled] = []
    with locked():
        try:
            identity = codex_account(seats_home())
        except (OSError, ValueError):
            identity = UnknownCodexAccount(Path.home() / ".codex", "seats identity cannot be read")
        if isinstance(identity, KnownCodexAccount):
            _cancel_outgoing_codex_holds(identity.home, identity.login)
        seen = typed_at()
        for path in sorted((STATE / "held").glob("*.json")):
            held = read(path)
            if not isinstance(held, HeldMessage) or held.outcome != "waiting":
                continue
            if (path.stem == "five-hour-codex" or isinstance(held.owner, KnownCodexAccount)) and isinstance(identity, UnknownCodexAccount):
                continue
            if seen >= held.held_at:
                held = replace(held, outcome="answered")
                line = "the user typed in a terminal; not sent"
            elif now < held.held_at + held.minutes * 60:
                continue
            elif problem := deliver(held):
                settled.append(Settled(f"{path.stem}: NOT sent: {problem}", True))
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
        if isinstance(held, LegacyHeld) and not isinstance(held, HeldMessage):
            lines.append(f"{path.stem} - waiting for a hold that states its action")
            continue
        if not isinstance(held, HeldMessage):
            continue
        left = max(math.ceil((held.held_at + held.minutes * 60 - now) / 60), 0)
        lines.append(f"{path.stem} - " + (f"waiting, sent in {left} min" if held.outcome == "waiting"
                                          else held.outcome) + f" - {held.summary}")
    return lines or ["nothing held"]


def parse_hold_options(arguments: list[str]) -> tuple[dict[str, str], bool] | None:
    options = {"--minutes": "15", "--need": "decision"}
    no_action = False
    seen: set[str] = set()
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option == "--no-action":
            if option in seen:
                return None
            seen.add(option)
            no_action = True
            index += 1
            continue
        if option not in ("--summary", "--text", "--minutes", "--need", "--action") \
                or option in seen or index + 1 >= len(arguments):
            return None
        seen.add(option)
        options[option] = arguments[index + 1]
        index += 2
    return options, no_action


def main(arguments: list[str]) -> int:
    match arguments:
        case ["hold", key, *pairs] if KEY.fullmatch(key):
            from send import CHANNEL_PRIORITY

            parsed = parse_hold_options(pairs)
            if parsed is None:
                print(USAGE, file=sys.stderr)
                return 2
            options, no_action = parsed
            if options.keys() != {"--summary", "--text", "--minutes", "--need", "--action"} \
                    and options.keys() != {"--summary", "--text", "--minutes", "--need"}:
                print(USAGE, file=sys.stderr)
                return 2
            action = parse_user_action(options.get("--action"), no_action)
            if isinstance(action, (ActionUnstated, ActionRefused)):
                reason = FIX if isinstance(action, ActionUnstated) else f"{action.reason}. {FIX}"
                print(f"escalate.py: {reason}", file=sys.stderr)
                return 2
            if options["--need"] not in CHANNEL_PRIORITY or not options["--minutes"].isdigit():
                print(USAGE, file=sys.stderr)
                return 2
            try:
                started = hold(key, options["--summary"], options["--text"], action, int(options["--minutes"]),
                               options["--need"])
            except ValueError as error:
                print(f"escalate.py: {error}. {FIX}", file=sys.stderr)
                return 2
            print(f"{key}: held; sent to the user in {options['--minutes']} min unless they type in a terminal"
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
