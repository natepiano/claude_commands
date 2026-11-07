"""Transport names, historical senders and verified live Claude relay identities."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.message.process_identity import process_start

from scripts.lib.boot_identity import boot_identity

RELAY_PREFIX = "relay/"


def relay_session_name(sender: str) -> str:
    """Keep the transport name separate from the real sender's name."""
    return RELAY_PREFIX + sender


@dataclass(frozen=True)
class SavedRelaySender:
    sender: str


@dataclass(frozen=True)
class NoSavedRelaySender:
    """No readable historical sender record exists for this socket's PID."""


@dataclass(frozen=True)
class RelayIdentity:
    """The saved sender belongs to this PID's current process lifetime."""

    sender: str


@dataclass(frozen=True)
class NoRelayIdentity:
    """The available evidence does not identify this process as a live relay."""


@dataclass(frozen=True)
class ProcessLifetime:
    started: str
    boot: str


@dataclass(frozen=True)
class ProcessLifetimeUnknown:
    """The process start identity could not be established."""


def _process_lifetime(pid: int) -> ProcessLifetime | ProcessLifetimeUnknown:
    started = process_start(pid)
    if not started:
        return ProcessLifetimeUnknown()
    try:
        boot = boot_identity() if sys.platform == "linux" else ""
    except (OSError, UnicodeError, ValueError):
        return ProcessLifetimeUnknown()
    if sys.platform == "linux" and not boot:
        return ProcessLifetimeUnknown()
    return ProcessLifetime(started, boot)


def relay_senders_directory() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "message/relay_senders"


def read_relay_sender(pid: int, directory: Path) -> SavedRelaySender | NoSavedRelaySender:
    """Keep historical sender lookup independent of whether that process still lives."""
    try:
        sender = (directory / str(pid)).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return NoSavedRelaySender()
    return SavedRelaySender(sender) if sender else NoSavedRelaySender()


def publish_relay_identity(pid: int, sender: str, directory: Path) -> None:
    """Publish the lifetime before its sender, before Claude can receive a reply."""
    # This module also runs in the small pre-exec launcher.
    from scripts.lib.atomic_file import write_text_atomic

    lifetime = _process_lifetime(pid)
    fields = {"sender": sender}
    if isinstance(lifetime, ProcessLifetime):
        fields.update(started=lifetime.started, boot=lifetime.boot)
    write_text_atomic(directory / f"{pid}.identity", json.dumps(fields))
    write_text_atomic(directory / str(pid), sender)


def live_relay_identity(pid: int, directory: Path) -> RelayIdentity | NoRelayIdentity:
    """Only a matching live process lifetime proves that a saved PID belongs to a relay."""
    sender = read_relay_sender(pid, directory)
    if not isinstance(sender, SavedRelaySender):
        return NoRelayIdentity()
    try:
        raw = cast(object, json.loads((directory / f"{pid}.identity").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return NoRelayIdentity()
    if not isinstance(raw, dict):
        return NoRelayIdentity()
    fields = cast(dict[str, object], raw)
    if fields.get("sender") != sender.sender:
        return NoRelayIdentity()
    lifetime = _process_lifetime(pid)
    if (isinstance(lifetime, ProcessLifetime)
            and fields.get("started") == lifetime.started and fields.get("boot") == lifetime.boot):
        return RelayIdentity(sender.sender)
    return NoRelayIdentity()


def message_sender_name(name: str) -> str:
    """Read attribution from a wrapper name; this does not verify its origin."""
    return name.removeprefix(RELAY_PREFIX)
