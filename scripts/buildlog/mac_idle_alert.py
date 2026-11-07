"""The saved Mac idle reading and the one line shown by build views."""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from . import store
    from ..lib import conf
else:
    from scripts.buildlog import store
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    from scripts.lib import conf


@dataclass(frozen=True)
class MacIdleConfig:
    """Validated timing policy shared by the idle watch and its views."""

    alert_seconds: float
    push_seconds: float
    stale_seconds: float
    repeat_minutes: float
    send_timeout_s: float


# Keep the public error name for callers that report idle configuration failures.
MacIdleConfigError = conf.Unusable


def idle_config(config: conf.ConfigRead) -> MacIdleConfig:
    """Read the idle policy from one validated configuration file."""
    return MacIdleConfig(config.number("alert_seconds"), config.number("push_seconds"),
                         config.number("stale_seconds"), config.number("repeat_minutes"),
                         config.number("send_timeout_s"))


def read_idle_config() -> MacIdleConfig:
    return idle_config(conf.read("mac_test"))


def nonnegative_time(value: object) -> bool:
    """JSON numbers used as times must also fit a finite float."""
    try:
        return type(value) in (int, float) and math.isfinite(cast(float, value)) and cast(float, value) >= 0
    except OverflowError:
        return False


def state_dir() -> Path:
    return Path(os.environ.get("MAC_IDLE_STATE_DIR", str(store.root().parent / "mac-idle"))).expanduser()


class SavedAlert(TypedDict):
    version: int
    written: float
    state: Literal["busy", "idle", "unreachable", "unreadable"]
    since: float
    waiting: int
    cause: Literal["blocked", "claim_idle", "free", "disk", ""]
    holder: str
    detail: str
    until: float
    messaged: bool
    pushed: bool
    admission_error: NotRequired[str]


@dataclass(frozen=True)
class NoSavedAlert:
    """The watch has not written a reading."""


@dataclass(frozen=True)
class UnreadableAlert:
    reason: str


def read_alert(path: Path) -> SavedAlert | NoSavedAlert | UnreadableAlert:
    try:
        raw = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return NoSavedAlert()
    except (OSError, ValueError, UnicodeError) as error:
        return UnreadableAlert(str(error))
    if not isinstance(raw, dict):
        return UnreadableAlert("saved reading is not an object")
    row = cast(dict[str, object], raw)
    if type(row.get("version")) is not int or row["version"] != 1:
        return UnreadableAlert("invalid version")
    for field in ("written", "since", "until"):
        if not nonnegative_time(row.get(field)):
            return UnreadableAlert(f"invalid {field}")
    if row.get("state") not in ("busy", "idle", "unreachable", "unreadable"):
        return UnreadableAlert("invalid state")
    if row.get("cause") not in ("blocked", "claim_idle", "free", "disk", ""):
        return UnreadableAlert("invalid cause")
    waiting = row.get("waiting")
    if type(waiting) is not int or waiting < 0:
        return UnreadableAlert("invalid waiting")
    for field in ("holder", "detail"):
        if not isinstance(row.get(field), str):
            return UnreadableAlert(f"invalid {field}")
    if "admission_error" in row and not isinstance(row["admission_error"], str):
        return UnreadableAlert("invalid admission_error")
    for field in ("messaged", "pushed"):
        if type(row.get(field)) is not bool:
            return UnreadableAlert(f"invalid {field}")
    return cast(SavedAlert, cast(object, row))


def display_field(value: str) -> str:
    return " ".join("".join(" " if character.isspace() else character
                            for character in value if character.isprintable() or character.isspace()).split())


def clock_text(at: float, zone: ZoneInfo, *, include_zone: bool = False) -> str:
    try:
        return datetime.fromtimestamp(at, zone).strftime("%H:%M %Z" if include_zone else "%H:%M")
    except (OSError, OverflowError, ValueError):
        return "unknown time"


def alert_line(path: Path, now: float, zone: ZoneInfo) -> str | None:
    saved = read_alert(path)
    if isinstance(saved, NoSavedAlert):
        return None
    if isinstance(saved, UnreadableAlert):
        return f"MAC IDLE WATCH: Mac state unreadable: {display_field(saved.reason)}."
    try:
        settings = read_idle_config()
    except MacIdleConfigError as error:
        return f"MAC IDLE WATCH: config unreadable: {display_field(str(error))}."
    if now - saved["written"] > settings.stale_seconds:
        return f"MAC IDLE WATCH: no reading since {clock_text(saved['written'], zone)}; check buildlog-sample.timer."
    if saved["state"] == "unreadable":
        return f"MAC IDLE WATCH: Mac state unreadable: {display_field(saved['detail'])}."
    admission_error = saved.get("admission_error", "")
    query_reason = f"admission query failed: {display_field(admission_error)}" if admission_error else ""
    if saved["state"] == "busy" or saved["waiting"] == 0:
        return f"MAC IDLE WATCH: {query_reason}." if query_reason else None
    waiting = saved["waiting"]
    if saved["state"] == "unreachable":
        return f"MAC UNREACHABLE until {clock_text(saved['until'], zone)}: {waiting} steps wait for the Mac."
    elapsed = now - saved["since"]
    if elapsed < settings.alert_seconds:
        return f"MAC IDLE WATCH: {query_reason}." if query_reason else None
    holder, detail = display_field(saved["holder"]), display_field(saved["detail"])
    if saved["cause"] == "blocked":
        cause = f"Blocked by {holder} since {clock_text(saved['since'], zone)} ({detail}); /build:mac unblock clears it"
    elif saved["cause"] == "claim_idle":
        cause = f"Claim of {holder} ({detail}) runs no build or test on the Mac"
    elif saved["cause"] == "disk":
        cause = "Mac disk exclusion keeps these steps on natedev"
    else:
        cause = "No block and no claim; the waiting steps do not take the Mac"
    suffix = f"; {query_reason}" if query_reason else ""
    return f"MAC IDLE {int(elapsed // 60)} min: {waiting} steps wait for the Mac. {cause}{suffix}."
