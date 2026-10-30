"""Refused work outranks a last good usage reading until allowance returns."""
from __future__ import annotations

import fcntl
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami import usage_store  # noqa: E402

@dataclass(frozen=True)
class KnownReset:
    at: datetime


@dataclass(frozen=True)
class NoResetKnown:
    pass


@dataclass(frozen=True)
class NoWindow:
    pass


@dataclass(frozen=True)
class ReadingWindow:
    left: float
    reset: KnownReset | NoResetKnown
    at: datetime


@dataclass(frozen=True)
class FreshReading:
    weekly: ReadingWindow
    five_hour: ReadingWindow | NoWindow
    credits: str


@dataclass(frozen=True)
class StaleReading:
    weekly: ReadingWindow | NoWindow = field(default_factory=NoWindow)
    five_hour: ReadingWindow | NoWindow = field(default_factory=NoWindow)


@dataclass(frozen=True)
class NoReading:
    pass


def _reading_window(window: usage_store.StoredWindow) -> ReadingWindow:
    at = datetime.fromisoformat(window["at"])
    if at.tzinfo is None or not math.isfinite(window["left"]):
        raise ValueError("invalid usage reading")
    reset = NoResetKnown() if not window["resets"] else KnownReset(datetime.fromisoformat(window["resets"]))
    if isinstance(reset, KnownReset) and reset.at.tzinfo is None:
        raise ValueError("reset time needs an offset")
    return ReadingWindow(window["left"], reset, at)


def stored_reading(tool: Literal["claude", "codex"], login: str, now: datetime) -> FreshReading | StaleReading | NoReading:
    """Convert optional saved windows at the boundary into named reading states."""
    held = usage_store.load().get(usage_store.key(tool.capitalize(), login))
    if held is None or "weekly" not in held:
        return NoReading()
    try:
        weekly = _reading_window(held["weekly"])
        five = _reading_window(held["five_hour"]) if "five_hour" in held else NoWindow()
        if now - weekly.at > timedelta(minutes=10) or weekly.at > now:
            return StaleReading(weekly, five)
        credits = held["credits"]["balance"] if "credits" in held else ""
        return FreshReading(weekly, five, credits)
    except (ValueError, TypeError):
        return NoReading()


@dataclass(frozen=True)
class KnownWindow:
    name: Literal["weekly", "five_hour"]


@dataclass(frozen=True)
class UnknownWindow:
    pass


@dataclass(frozen=True)
class AccountRefusal:
    tool: Literal["claude", "codex"]
    login: str
    window: KnownWindow | UnknownWindow
    reset: KnownReset | NoResetKnown
    at: datetime


class RefusalWire(TypedDict):
    tool: str
    login: str
    window: str
    reset: str
    at: str


class RefusalsError(ValueError):
    pass


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "accounts" / "refusals.json"


def key(tool: str, login: str) -> str:
    return f"{tool.casefold()}:{login.casefold()}"


def _time(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError as error:
        raise RefusalsError("invalid refusal time") from error
    if value.tzinfo is None:
        raise RefusalsError("refusal time needs an offset")
    return value


def load() -> dict[str, AccountRefusal]:
    path = store_file()
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise RefusalsError(f"refusal store unreadable: {error}") from error
    if not isinstance(raw, dict):
        raise RefusalsError("refusal store must be an object")
    result: dict[str, AccountRefusal] = {}
    for value in cast(dict[str, object], raw).values():
        if not isinstance(value, dict):
            raise RefusalsError("invalid account refusal")
        held = cast(dict[str, object], value)
        if not all(isinstance(held.get(name), str) for name in ("tool", "login", "window", "reset", "at")):
            raise RefusalsError("invalid account refusal fields")
        wire = cast(RefusalWire, cast(object, held))
        if wire["tool"] not in {"claude", "codex"} or not wire["login"] or wire["window"] not in {"weekly", "five_hour", "unknown"}:
            raise RefusalsError("invalid account refusal identity or window")
        window = (UnknownWindow() if wire["window"] == "unknown" else KnownWindow(cast(Literal["weekly", "five_hour"], wire["window"])))
        reset = KnownReset(_time(wire["reset"])) if wire["reset"] else NoResetKnown()
        refusal = AccountRefusal(cast(Literal["claude", "codex"], wire["tool"]), wire["login"], window, reset, _time(wire["at"]))
        result[key(refusal.tool, refusal.login)] = refusal
    return result


def write_refusal(refusal: AccountRefusal) -> None:
    """Merge one account under a lock and replace the store atomically."""
    if refusal.at.tzinfo is None or isinstance(refusal.reset, KnownReset) and refusal.reset.at.tzinfo is None:
        raise RefusalsError("refusal time needs an offset")
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        held = load()
        identity = key(refusal.tool, refusal.login)
        previous = held.get(identity)
        if previous is not None and previous.at > refusal.at:
            return
        held[identity] = refusal
        wire: dict[str, RefusalWire] = {
            identity: RefusalWire(tool=item.tool, login=item.login,
                window=item.window.name if isinstance(item.window, KnownWindow) else "unknown",
                reset=item.reset.at.isoformat() if isinstance(item.reset, KnownReset) else "", at=item.at.isoformat())
            for identity, item in held.items()
        }
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".refusals-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                _ = output.write(json.dumps(wire, indent=2, sort_keys=True) + "\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def resolved(refusal: AccountRefusal, reading: FreshReading | StaleReading | NoReading, now: datetime) -> bool:
    if isinstance(refusal.window, KnownWindow) and isinstance(refusal.reset, KnownReset) and refusal.reset.at <= now:
        return True
    if not isinstance(reading, FreshReading):
        return False
    if isinstance(refusal.window, KnownWindow):
        window = reading.weekly if refusal.window.name == "weekly" else reading.five_hour
        return (isinstance(window, ReadingWindow) and window.at > refusal.at
                and (window.left > 0 and (isinstance(window.reset, NoResetKnown) or window.reset != refusal.reset)
                     or isinstance(window.reset, KnownReset)
                     and isinstance(refusal.reset, KnownReset) and window.reset.at > refusal.reset.at))
    windows = ((reading.weekly,) if isinstance(reading.five_hour, NoWindow)
               else (reading.weekly, reading.five_hour))
    return all(window.at > refusal.at and window.left > 0 for window in windows)
