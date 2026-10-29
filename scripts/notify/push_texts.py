"""Fixed phone texts, their examples, and the argv used to deliver them."""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from user_action import (ActionRequired, NoActionRequired, UserAction, NotificationSession,
                         NoSessionLine, NO_SESSION_LINE, SessionLine, first_line)

Need = Literal["note", "decision", "blocked"]
REVIEWED_FILE = Path(__file__).with_name("push_texts_reviewed.json")
SEND_SCRIPT = Path(__file__).resolve().parents[1] / "message/send.py"
# A unit id such as "types-unit": the whole value is one session name. The user knows it as "types".
_UNIT_ID = re.compile(r"[A-Za-z0-9][\w-]*-unit")


@dataclass(frozen=True)
class ActionTemplate:
    text: str


@dataclass(frozen=True)
class NoActionNeeded:
    pass


@dataclass(frozen=True)
class SessionTemplate:
    text: str


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
    # One line for /nate help: when the push goes out. Not part of the approval fingerprint.
    purpose: str
    session: SessionTemplate | NoSessionLine = NO_SESSION_LINE
    # False: the phone shows the title and body exactly as rendered, with no
    # "No action needed." line. Only an entry with no action and no session can opt out.
    header: bool = True
    # An agent writes the text at send time; no person approved it, so check it like free text.
    agent_written: bool = False


@dataclass(frozen=True)
class RenderedPush:
    id: str
    sender: str
    need: Need
    title: str
    action: UserAction
    body: str
    values: dict[str, str]
    session: NotificationSession = NO_SESSION_LINE
    header: bool = True

    @property
    def message(self) -> str:
        return first_line(self.action, self.session) + "\n" + self.body if self.header else self.body


def catalog() -> tuple[PushText, ...]:
    module = importlib.import_module("push_catalog")
    entries = cast(tuple[PushText, ...], module.ENTRIES)
    if len({entry.id for entry in entries}) != len(entries):
        raise ValueError("duplicate push text id")
    for entry in entries:
        if not entry.examples or len({example.name for example in entry.examples}) != len(entry.examples):
            raise ValueError(f"{entry.id}: examples need distinct names and must not be empty")
        if not entry.header and (isinstance(entry.action, ActionTemplate) or isinstance(entry.session, SessionTemplate)):
            raise ValueError(f"{entry.id}: an entry without a header cannot state an action or a session")
    return entries


def shown_value(key: str, value: str) -> str:
    """A session name loses its "-unit" end: the {sender} and {session} values, and any value that is
    a whole unit id. Free text keeps every word, "per-unit" included (reviewer, 2026-10-21)."""
    return value.removesuffix("-unit") if key in ("sender", "session") or _UNIT_ID.fullmatch(value) else value


def render_entry(entry: PushText, given: dict[str, str]) -> RenderedPush:
    templates = [entry.sender, entry.title, entry.body]
    if isinstance(entry.action, ActionTemplate):
        templates.append(entry.action.text)
    if isinstance(entry.session, SessionTemplate):
        templates.append(entry.session.text)
    fields: set[str] = set()
    for template in templates:
        for _, field, _, _ in string.Formatter().parse(template):
            if field is not None:
                if not field.isidentifier():
                    raise ValueError(f"{entry.id}: placeholder must be a name: {field}")
                fields.add(field)
    values = {key: shown_value(key, value) for key, value in given.items()}
    if fields != set(values):
        raise ValueError(f"{entry.id}: missing values {sorted(fields - set(values))}; extra values {sorted(set(values) - fields)}")
    action = ActionRequired(entry.action.text.format(**values)) if isinstance(entry.action, ActionTemplate) else NoActionRequired()
    return RenderedPush(entry.id, entry.sender.format(**values), entry.need,
                        entry.title.format(**values), action, entry.body.format(**values), dict(values),
                        SessionLine(entry.session.text.format(**values)) if isinstance(entry.session, SessionTemplate) else NO_SESSION_LINE,
                        entry.header)


def render(id: str, **values: str) -> RenderedPush:
    for entry in catalog():
        if entry.id == id:
            return render_entry(entry, values)
    raise ValueError(f"unknown push text id: {id}")


def listing(entries: tuple[PushText, ...]) -> str:
    """One line per entry, sorted by id: the id in one column, then its purpose."""
    ordered = sorted(entries, key=lambda entry: entry.id)
    width = max(len(entry.id) for entry in ordered)
    return "\n".join(f"{entry.id:<{width}}  {entry.purpose}" for entry in ordered)


def shown(entry: PushText) -> str:
    """The entry's template with its placeholders, then its first example as the phone shows it."""
    template = RenderedPush(entry.id, entry.sender, entry.need, entry.title,
                            ActionRequired(entry.action.text) if isinstance(entry.action, ActionTemplate) else NoActionRequired(),
                            entry.body, {},
                            SessionLine(entry.session.text) if isinstance(entry.session, SessionTemplate) else NO_SESSION_LINE,
                            entry.header)
    example = entry.examples[0]
    filled = render_entry(entry, example.values)
    return (f"{entry.id}  {entry.purpose}\n\n"
            + f"Template:\nTitle: {template.title}\n{template.message}\n\n"
            + f"Example ({example.name}):\nTitle: {filled.title}\n{filled.message}")


def user_send_argv(rendered: RenderedPush, *, sender: str = "", machine: str = "",
                   priority: str = "", timeout: float = 0, key: str = "", self_clearing: bool = False) -> list[str]:
    """The send.py argv; a `key` lets `send.py retire --to user --key KEY` stop a blocked push's repeats.
    `self_clearing`: the caller retires its own blocked push, so send.py needs no --clears-when."""
    command = [sys.executable, str(SEND_SCRIPT), "--to", "user", "--from", sender or rendered.sender,
               "--need", rendered.need, "--text-id", rendered.id]
    if key:
        command.extend(["--key", key])
    if self_clearing:
        command.append("--self-clearing")
    for name, value in sorted(rendered.values.items()):
        command.extend(["--value", f"{name}={value}"])
    if machine:
        command.extend(["--machine", machine])
    if priority:
        command.extend(["--priority", priority])
    if timeout:
        command.extend(["--timeout", str(timeout)])
    return command


def user_retire_argv(sender: str, key: str) -> list[str]:
    """The send.py argv that stops the repeats of the emergency push `user_send_argv` sent with `key`."""
    return [sys.executable, str(SEND_SCRIPT), "retire", "--to", "user", "--from", sender, "--key", key]


class FilledExampleRecord(TypedDict):
    session: NotRequired[str]
    name: str
    values: dict[str, str]
    sender: str
    need: Need
    title: str
    action: str | None
    body: str


class FingerprintRecord(TypedDict):
    session: NotRequired[str]
    header: NotRequired[bool]
    sender: str
    need: Need
    title: str
    action: dict[str, str | bool]
    body: str
    examples: list[FilledExampleRecord]


def agent_written(text_id: str) -> bool:
    return any(entry.id == text_id and entry.agent_written for entry in catalog())


def user_specified(text_id: str) -> bool:
    """Whether the user specified this text: its values are written per send, so a failed review is
    about one send's words, not the template, and no template fix is due."""
    try:
        approvals = cast(object, json.loads(REVIEWED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    entry = cast(dict[str, object], approvals).get(text_id) if isinstance(approvals, dict) else None
    if not isinstance(entry, dict) or not entry:
        return False
    return all(isinstance(approval, dict) and str(cast(dict[str, object], approval).get("reviewer", "")).startswith("user-spec:")
               for approval in cast(dict[str, object], entry).values())


def approval_date(text_id: str) -> str:
    """The date (YYYY-MM-DD) of the newest approval when every example of the entry has a current
    approval in REVIEWED_FILE; "" when one is missing or its template changed since."""
    entry = next((item for item in catalog() if item.id == text_id), None)
    try:
        approvals = cast(object, json.loads(REVIEWED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""
    examples = cast(dict[str, object], approvals).get(text_id) if isinstance(approvals, dict) else None
    if entry is None or not isinstance(examples, dict):
        return ""
    digest, times = fingerprint(entry), list[str]()
    for example in entry.examples:
        approval = cast(dict[str, object], examples).get(example.name)
        if not isinstance(approval, dict):
            return ""
        record = cast(dict[str, object], approval)
        time = record.get("time")
        if record.get("sha256") != digest or not isinstance(time, str):
            return ""
        times.append(time)
    return max(times)[:10]


def fingerprint(entry: PushText) -> str:
    examples: list[FilledExampleRecord] = []
    for example in entry.examples:
        rendered = render_entry(entry, example.values)
        examples.append({"name": example.name, "values": example.values, "sender": rendered.sender,
                         "need": rendered.need, "title": rendered.title,
                         "action": rendered.action.text if isinstance(rendered.action, ActionRequired) else None,
                         "body": rendered.body})
        if isinstance(rendered.session, SessionLine):
            examples[-1]["session"] = rendered.session.text
    value: FingerprintRecord = {"sender": entry.sender, "need": entry.need, "title": entry.title,
             "action": {"required": entry.action.text} if isinstance(entry.action, ActionTemplate) else {"none": True},
             "body": entry.body, "examples": examples}
    if isinstance(entry.session, SessionTemplate):
        value["session"] = entry.session.text
    if not entry.header:
        value["header"] = False
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
