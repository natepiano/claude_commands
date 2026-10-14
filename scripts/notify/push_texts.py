"""Fixed phone texts, their examples, and the argv used to deliver them."""
from __future__ import annotations

import hashlib
import importlib
import json
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from user_action import ActionRequired, NoActionRequired, UserAction

Need = Literal["note", "decision", "blocked"]
REVIEWED_FILE = Path(__file__).with_name("push_texts_reviewed.json")
SEND_SCRIPT = Path(__file__).resolve().parents[1] / "message/send.py"


@dataclass(frozen=True)
class ActionTemplate:
    text: str


@dataclass(frozen=True)
class NoActionNeeded:
    pass


@dataclass(frozen=True)
class PushExample:
    name: str
    values: dict[str, str]


@dataclass(frozen=True)
class PushText:
    id: str
    sender: str
    need: Need
    title: str
    action: ActionTemplate | NoActionNeeded
    body: str
    examples: tuple[PushExample, ...]


@dataclass(frozen=True)
class RenderedPush:
    id: str
    sender: str
    need: Need
    title: str
    action: UserAction
    body: str
    values: dict[str, str]


def catalog() -> tuple[PushText, ...]:
    module = importlib.import_module("push_catalog")
    entries = cast(tuple[PushText, ...], module.ENTRIES)
    if len({entry.id for entry in entries}) != len(entries):
        raise ValueError("duplicate push text id")
    for entry in entries:
        if not entry.examples or len({example.name for example in entry.examples}) != len(entry.examples):
            raise ValueError(f"{entry.id}: examples need distinct names and must not be empty")
    return entries


def render_entry(entry: PushText, values: dict[str, str]) -> RenderedPush:
    templates = [entry.sender, entry.title, entry.body]
    if isinstance(entry.action, ActionTemplate):
        templates.append(entry.action.text)
    fields: set[str] = set()
    for template in templates:
        for _, field, _, _ in string.Formatter().parse(template):
            if field is not None:
                if not field.isidentifier():
                    raise ValueError(f"{entry.id}: placeholder must be a name: {field}")
                fields.add(field)
    if fields != set(values):
        raise ValueError(f"{entry.id}: missing values {sorted(fields - set(values))}; extra values {sorted(set(values) - fields)}")
    action = ActionRequired(entry.action.text.format(**values)) if isinstance(entry.action, ActionTemplate) else NoActionRequired()
    return RenderedPush(entry.id, entry.sender.format(**values), entry.need,
                        entry.title.format(**values), action, entry.body.format(**values), dict(values))


def render(id: str, **values: str) -> RenderedPush:
    for entry in catalog():
        if entry.id == id:
            return render_entry(entry, values)
    raise ValueError(f"unknown push text id: {id}")


def user_send_argv(rendered: RenderedPush, *, sender: str = "", machine: str = "",
                   priority: str = "", timeout: float = 0) -> list[str]:
    command = [sys.executable, str(SEND_SCRIPT), "--to", "user", "--from", sender or rendered.sender,
               "--need", rendered.need, "--text-id", rendered.id]
    for key, value in sorted(rendered.values.items()):
        command.extend(["--value", f"{key}={value}"])
    if machine:
        command.extend(["--machine", machine])
    if priority:
        command.extend(["--priority", priority])
    if timeout:
        command.extend(["--timeout", str(timeout)])
    return command


class FilledExampleRecord(TypedDict):
    name: str
    values: dict[str, str]
    sender: str
    need: Need
    title: str
    action: str | None
    body: str


class FingerprintRecord(TypedDict):
    sender: str
    need: Need
    title: str
    action: dict[str, str | bool]
    body: str
    examples: list[FilledExampleRecord]


def fingerprint(entry: PushText) -> str:
    examples: list[FilledExampleRecord] = []
    for example in entry.examples:
        rendered = render_entry(entry, example.values)
        examples.append({"name": example.name, "values": example.values, "sender": rendered.sender,
                         "need": rendered.need, "title": rendered.title,
                         "action": rendered.action.text if isinstance(rendered.action, ActionRequired) else None,
                         "body": rendered.body})
    value: FingerprintRecord = {"sender": entry.sender, "need": entry.need, "title": entry.title,
             "action": {"required": entry.action.text} if isinstance(entry.action, ActionTemplate) else {"none": True},
             "body": entry.body, "examples": examples}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
