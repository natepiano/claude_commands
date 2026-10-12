"""Store a production's current pins, the user's answers and notes."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from pins import Pin, Pins

if TYPE_CHECKING:
    from add_unit import Production


class Answer(TypedDict):
    answer: Literal["approved", "declined"]
    at: str  # The answer's date in the production's zone.
    words: str


class Entry(TypedDict):
    pin: Pin
    bevy: str
    via: list[str]
    patched_bevy: bool
    answer: Answer | None  # None means the user has given no current answer for this crate.
    note: str


class Record(TypedDict):
    slug: str
    production: str
    checkout: str
    target: str
    started: str
    closed: str | None  # None means this upgrade watch is open.
    bevy: list[str]
    pins_read: str
    dependencies: dict[str, Entry]


class RecordFailure(Exception):
    """An unreadable record or an answer that cannot be recorded."""


def state_dir() -> Path:
    return Path(os.environ.get("BEVY_UPGRADE_STATE_DIR") or Path.home() / ".local/state/bevy-upgrade")


def record_path(slug: str) -> Path:
    return state_dir() / f"{slug}.json"


@contextmanager
def locked(slug: str) -> Generator[None, None, None]:
    """Hold one exclusive lock for the caller's entire read-modify-write."""
    path = state_dir() / f"{slug}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a")
    except OSError as error:
        raise RecordFailure(f"{path}: {error}") from error
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise RecordFailure(f"{path}: {error}") from error
        yield


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    data = cast(dict[str, object], value)
    return data


def _strings(data: dict[str, object], *keys: str) -> None:
    for key in keys:
        if not isinstance(data.get(key), str):
            raise ValueError(f"{key} must be a string")


def _string_list(value: object, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError(f"{label} must be an array of strings")


def _validate(value: object) -> Record:
    data = _object(value, "record")
    _strings(data, "slug", "production", "checkout", "target", "started", "pins_read")
    if "closed" not in data or (data["closed"] is not None and not isinstance(data["closed"], str)):
        raise ValueError("closed must be a timestamp or null")
    for key in ("started", "pins_read", "closed"):
        if data[key] is not None:
            _ = datetime.fromisoformat(cast(str, data[key]))
    _string_list(data.get("bevy"), "bevy")
    for name, raw in _object(data.get("dependencies"), "dependencies").items():
        entry = _object(raw, name)
        _strings(entry, "bevy", "note")
        _string_list(entry.get("via"), "via")
        if not isinstance(entry.get("patched_bevy"), bool):
            raise ValueError("patched_bevy must be a boolean")
        pin = _object(entry.get("pin"), "pin")
        _strings(pin, "source", "version")
        if pin["source"] == "git":
            _strings(pin, "url", "ref", "rev")
        elif pin["source"] == "path":
            _strings(pin, "path")
        elif pin["source"] != "registry":
            raise ValueError("pin source must be registry, git or path")
        if "answer" not in entry:
            raise ValueError("answer is missing")
        if entry["answer"] is not None:
            answer = _object(entry["answer"], "answer")
            _strings(answer, "answer", "at", "words")
            if answer["answer"] not in ("approved", "declined"):
                raise ValueError("answer must be approved or declined")
    return cast(Record, value)


def load(slug: str) -> Record | None:
    """None means this production has never started a watch."""
    path = record_path(slug)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        return _validate(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as error:
        raise RecordFailure(f"{path}: {error}") from error


def save(record: Record) -> None:
    """Atomically replace the file; the caller holds locked for any preceding read."""
    path = record_path(record["slug"])
    temporary: Path | None = None  # No temporary file exists until NamedTemporaryFile opens it.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.stem}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, indent=2, ensure_ascii=False)
            _ = stream.write("\n")
        _ = temporary.replace(path)
    except (OSError, ValueError, TypeError) as error:
        raise RecordFailure(f"{path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def start(production: Production, target: str, pins: Pins, now: datetime) -> Record:
    """Begin or refresh a watch; the caller holds locked through start and save."""
    if re.fullmatch(r"\d+\.\d+", target) is None:
        raise RecordFailure("target must be major.minor, such as 0.20")
    current = load(production.slug)
    if current is not None and current["closed"] is None:
        if current["target"] != target:
            raise RecordFailure(f"a watch for {current['target']} is open; stop it first")
        return apply_pins(current, pins, now)
    if current is not None:
        path = record_path(production.slug)
        archive = path.with_name(f"{production.slug}.{current['target']}.closed.json")
        try:
            _ = path.replace(archive)
        except OSError as error:
            raise RecordFailure(f"{path}: {error}") from error
    return apply_pins(Record(slug=production.slug, production=str(production.doc),
                             checkout=str(production.checkout), target=target, started=_utc(now),
                             closed=None, bevy=[], pins_read=_utc(now), dependencies={}), pins, now)


def apply_pins(record: Record, pins: Pins, now: datetime) -> Record:
    dependencies: dict[str, Entry] = {}
    for dependent in pins.dependents:
        entry = record["dependencies"].get(dependent.name)
        if entry is None:
            entry = Entry(pin=dependent.pin, bevy=dependent.bevy, via=list(dependent.via),
                          patched_bevy=dependent.patched_bevy, answer=None, note="")
        else:
            entry["pin"] = dependent.pin
            entry["bevy"] = dependent.bevy
            entry["via"] = list(dependent.via)
            entry["patched_bevy"] = dependent.patched_bevy
        dependencies[dependent.name] = entry
    record["dependencies"] = dependencies
    record["bevy"] = list(pins.bevy)
    record["pins_read"] = _utc(now)
    return record


def _entry(record: Record, crate: str) -> Entry:
    entry = record["dependencies"].get(crate)
    if entry is None:
        tracked = ", ".join(sorted(record["dependencies"]))
        raise RecordFailure(f"{crate} is not tracked; tracked: {tracked}")
    return entry


def set_answer(record: Record, crate: str, answer: Literal["approved", "declined"],
               words: str, today: str) -> Record:
    entry = _entry(record, crate)
    if not words.strip():
        raise RecordFailure("answer words must not be empty")
    entry["answer"] = Answer(answer=answer, at=today, words=words)
    return record


def forget_answer(record: Record, crate: str) -> Record:
    _entry(record, crate)["answer"] = None
    return record


def set_note(record: Record, crate: str, text: str) -> Record:
    _entry(record, crate)["note"] = text
    return record


def on_target(version: str, target: str) -> bool:
    parts = version.split(".", 2)
    return len(parts) >= 2 and ".".join(parts[:2]) == target
