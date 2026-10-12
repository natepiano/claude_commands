"""Parse Codex rollout records using only the standard library."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TypedDict, cast


class TokenUsageJson(TypedDict, total=False):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class TokenInfoJson(TypedDict, total=False):
    last_token_usage: TokenUsageJson


class RolloutItemJson(TypedDict, total=False):
    type: str


class RolloutPayloadJson(TypedDict, total=False):
    type: str
    name: str
    timestamp: str
    input: object
    info: TokenInfoJson
    item: RolloutItemJson


class RolloutRecordJson(TypedDict, total=False):
    timestamp: str
    type: str
    payload: RolloutPayloadJson


@dataclass(frozen=True)
class TokenUsage:
    uncached_input: int = 0
    cached_input: int = 0
    output: int = 0


@dataclass(frozen=True)
class KnownThreadOpening:
    at: datetime


@dataclass(frozen=True)
class UnknownThreadOpening:
    pass


ThreadOpening = KnownThreadOpening | UnknownThreadOpening


@dataclass(frozen=True)
class UsageSample:
    at: datetime
    usage: TokenUsage
    before_first_edit: bool


@dataclass
class ThreadRolloutBuilder:
    opening: ThreadOpening = field(default_factory=UnknownThreadOpening)
    usage: list[UsageSample] = field(default_factory=list)
    compactions: list[datetime] = field(default_factory=list)
    edits: list[datetime] = field(default_factory=list)
    edit_seen: bool = False

    def consume(self, record: RolloutRecordJson) -> None:
        payload = payload_of(record)
        at = timestamp_of(record)
        if record.get("type") == "session_meta":
            opening = parse_timestamp(payload.get("timestamp"))
            if opening is None:
                opening = at
            if opening is not None:
                self.opening = KnownThreadOpening(opening)
        if is_edit(record):
            self.edit_seen = True
            if at is not None:
                self.edits.append(at)
        usage = token_usage(record)
        if usage is not None and at is not None:
            self.usage.append(UsageSample(at, usage, not self.edit_seen))
        if is_compaction(record) and at is not None:
            self.compactions.append(at)


def payload_of(record: RolloutRecordJson) -> RolloutPayloadJson:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def decode_rollout(line: str) -> RolloutRecordJson | None:
    try:
        value = cast(object, json.loads(line))
    except (json.JSONDecodeError, UnicodeError):
        return None
    return cast(RolloutRecordJson, cast(object, value)) if isinstance(value, dict) else None


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_of(record: RolloutRecordJson) -> datetime | None:
    return parse_timestamp(record.get("timestamp"))


def token_usage(record: RolloutRecordJson) -> TokenUsage | None:
    payload = payload_of(record)
    if record.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    info_value = payload.get("info")
    if not isinstance(info_value, dict):
        return None
    info = info_value
    last_value = info.get("last_token_usage")
    if not isinstance(last_value, dict):
        return None
    last = last_value
    input_tokens = last.get("input_tokens")
    cached_tokens = last.get("cached_input_tokens")
    output_tokens = last.get("output_tokens")
    if not all(isinstance(value, int) and not isinstance(value, bool)
               for value in (input_tokens, cached_tokens, output_tokens)):
        return None
    assert isinstance(input_tokens, int)
    assert isinstance(cached_tokens, int)
    assert isinstance(output_tokens, int)
    if min(input_tokens, cached_tokens, output_tokens) < 0 or cached_tokens > input_tokens:
        return None
    return TokenUsage(input_tokens - cached_tokens, cached_tokens, output_tokens)


def javascript_without_string_literals(source: str) -> str:
    visible: list[str] = []
    string_delimiter = ""
    inside_template_text = False
    template_expression_depths: list[int] = []
    escaped = False
    position = 0
    while position < len(source):
        character = source[position]
        if string_delimiter:
            visible.append(" ")
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == string_delimiter:
                string_delimiter = ""
        elif inside_template_text:
            visible.append(" ")
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "`":
                inside_template_text = False
            elif character == "$" and source[position:position + 2] == "${":
                visible.append(" ")
                template_expression_depths.append(0)
                inside_template_text = False
                position += 1
        elif character in ("'", '"'):
            string_delimiter = character
            visible.append(" ")
        elif character == "`":
            inside_template_text = True
            visible.append(" ")
        elif character == "{" and template_expression_depths:
            template_expression_depths[-1] += 1
            visible.append(character)
        elif character == "}" and template_expression_depths:
            if template_expression_depths[-1] == 0:
                _ = template_expression_depths.pop()
                inside_template_text = True
                visible.append(" ")
            else:
                template_expression_depths[-1] -= 1
                visible.append(character)
        else:
            visible.append(character)
        position += 1
    return "".join(visible)


def is_edit(record: RolloutRecordJson) -> bool:
    payload = payload_of(record)
    if record.get("type") != "response_item" or payload.get("type") != "custom_tool_call":
        return False
    if payload.get("name") == "apply_patch":
        return True
    tool_input = payload.get("input")
    if not isinstance(tool_input, str):
        return False
    try:
        decoded = cast(object, json.loads(tool_input))
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        fields = cast(dict[str, object], cast(object, decoded))
        if any(fields.get(key) == "apply_patch" for key in ("command", "name", "tool")):
            return True
    stripped = tool_input.lstrip()
    executable_source = javascript_without_string_literals(tool_input)
    return stripped.startswith("apply_patch ") or re.search(
        r"\b(?:tools\.)?apply_patch\s*\(", executable_source,
    ) is not None


def is_compaction(record: RolloutRecordJson) -> bool:
    payload = payload_of(record)
    item_value = payload.get("item")
    item: RolloutItemJson = item_value if isinstance(item_value, dict) else {}
    return (
        record.get("type") == "compacted"
        or payload.get("type") in ("compacted", "contextCompaction")
        or item.get("type") == "contextCompaction"
    )

