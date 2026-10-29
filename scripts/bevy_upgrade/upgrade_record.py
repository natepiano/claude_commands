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
from upstream import Item, TargetPrerelease, TargetReleased, TargetUnpublished

if TYPE_CHECKING:
    from production_units import Production


class Answer(TypedDict):
    answer: Literal["approved", "declined"]
    at: str  # The answer's date in the production's zone.
    words: str


class CratesIoUnread(TypedDict):
    status: Literal["unread"]


class CratesIoRead(TypedDict):
    status: Literal["read"]
    checked: str
    releases: dict[str, str]
    newest: str
    ready: str  # Empty when no walked release is on the target.
    repository: str  # Empty when the crate names no GitHub repository.


class TargetUnread(TypedDict):
    status: Literal["unread"]


class CratesIoUnlisted(TypedDict):
    status: Literal["unlisted"]
    checked: str
    repository: str  # Empty when the git pin names no GitHub repository.


class BranchUnread(TypedDict):
    status: Literal["unread"]


class BranchRead(TypedDict):
    status: Literal["read"]
    repository: str
    name: str
    rev: str
    committed: str
    requirement: str  # Empty when the manifest names no Bevy requirement.


TargetBevy = TargetUnread | TargetUnpublished | TargetPrerelease | TargetReleased
TargetFit = Literal["on target", "off target", "unreadable"]


class Entry(TypedDict):
    pin: Pin
    bevy: str
    via: list[str]
    patched_bevy: bool
    answer: Answer | None  # None means the user has given no current answer for this crate.
    note: str
    crates_io: CratesIoUnread | CratesIoRead | CratesIoUnlisted
    error: str
    branch: BranchUnread | BranchRead
    items: list[Item]


class Announced(TypedDict):
    """What the showrunner was last told about one dependency."""

    state: str
    repository: str  # Empty when no branch has been read.
    items: dict[str, str]


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
    target_bevy: TargetBevy
    target_error: str
    checked: str | None  # None means no check has reached every entry yet.
    announced: dict[str, Announced] | None  # None means no report has reached the showrunner.


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


def _timestamp(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timestamp")
    _ = datetime.fromisoformat(value)


def _upstream_fields(data: dict[str, object]) -> None:
    _ = data.setdefault("target_bevy", {"status": "unread"})
    _ = data.setdefault("target_error", "")
    _ = data.setdefault("checked", None)
    _strings(data, "target_error")
    if data["checked"] is not None:
        _timestamp(data["checked"], "checked")
    target = _object(data["target_bevy"], "target_bevy")
    status = target.get("status")
    if status == "released":
        _strings(target, "version", "date", "since")
    elif status == "pre-release":
        _strings(target, "since")
    elif status not in ("unread", "unpublished"):
        raise ValueError("target_bevy status must be unread, unpublished, pre-release or released")


def _crates_io_fields(entry: dict[str, object]) -> None:
    _ = entry.setdefault("crates_io", {"status": "unread"})
    _ = entry.setdefault("error", "")
    _strings(entry, "error")
    facts = _object(entry["crates_io"], "crates_io")
    if facts.get("status") == "read":
        _strings(facts, "checked", "newest", "ready", "repository")
        _timestamp(facts["checked"], "crates_io checked")
        for requirement in _object(facts.get("releases"), "releases").values():
            if not isinstance(requirement, str):
                raise ValueError("release requirements must be strings")
    elif facts.get("status") == "unlisted":
        _strings(facts, "checked", "repository")
        _timestamp(facts["checked"], "crates_io checked")
    elif facts.get("status") != "unread":
        raise ValueError("crates_io status must be unread, read or unlisted")


def _github_fields(entry: dict[str, object]) -> None:
    _ = entry.setdefault("branch", {"status": "unread"})
    _ = entry.setdefault("items", [])
    branch = _object(entry["branch"], "branch")
    if branch.get("status") == "read":
        _strings(branch, "repository", "name", "rev", "committed", "requirement")
    elif branch.get("status") != "unread":
        raise ValueError("branch status must be unread or read")
    items = entry["items"]
    if not isinstance(items, list):
        raise ValueError("items must be an array")
    for raw in cast(list[object], items):
        item = _object(raw, "item")
        _strings(item, "title", "updated", "head")
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("item number must be an integer")
        if item.get("kind") not in ("pr", "issue"):
            raise ValueError("item kind must be pr or issue")
        if item.get("state") not in ("open", "closed", "merged"):
            raise ValueError("item state must be open, closed or merged")


def _validate(value: object) -> Record:
    data = _object(value, "record")
    _strings(data, "slug", "production", "checkout", "target", "started", "pins_read")
    if "closed" not in data or (data["closed"] is not None and not isinstance(data["closed"], str)):
        raise ValueError("closed must be a timestamp or null")
    for key in ("started", "pins_read", "closed"):
        if data[key] is not None:
            _ = datetime.fromisoformat(cast(str, data[key]))
    _upstream_fields(data)
    _ = data.setdefault("announced", None)
    if data["announced"] is not None:
        for raw in _object(data["announced"], "announced").values():
            announcement = _object(raw, "announcement")
            _strings(announcement, "state", "repository")
            for item_state in _object(announcement.get("items"), "announced items").values():
                if not isinstance(item_state, str):
                    raise ValueError("announced item states must be strings")
    _string_list(data.get("bevy"), "bevy")
    for name, raw in _object(data.get("dependencies"), "dependencies").items():
        entry = _object(raw, name)
        _crates_io_fields(entry)
        _github_fields(entry)
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
                             closed=None, bevy=[], pins_read=_utc(now), dependencies={},
                             target_bevy=TargetUnread(status="unread"), target_error="", checked=None,
                             announced=None), pins, now)


def apply_pins(record: Record, pins: Pins, now: datetime) -> Record:
    dependencies: dict[str, Entry] = {}
    for dependent in pins.dependents:
        entry = record["dependencies"].get(dependent.name)
        if entry is None:
            entry = Entry(pin=dependent.pin, bevy=dependent.bevy, via=list(dependent.via),
                          patched_bevy=dependent.patched_bevy, answer=None, note="",
                          crates_io=CratesIoUnread(status="unread"), error="",
                          branch=BranchUnread(status="unread"), items=[])
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


def target_fit(requirement: str, target: str) -> TargetFit:
    remainder = requirement[1:] if requirement.startswith(("^", "~", "=")) else requirement
    if re.fullmatch(r"\d+\.\d+(\.\d+)?(-[\w.]+)?", remainder) is None:
        return "unreadable"
    return "on target" if on_target(remainder.split("-", 1)[0], target) else "off target"
