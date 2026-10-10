#!/usr/bin/env python3
"""Write and inspect the current, user-visible feature log."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Literal, NamedTuple, NoReturn, TypedDict, cast
from zoneinfo import ZoneInfo


class ProjectSpec(NamedTuple):
    repo: str
    crates: frozenset[str]
    published: frozenset[str]


PROJECTS: dict[str, ProjectSpec] = {
    "hana": ProjectSpec(
        "natepiano/hana",
        frozenset(
            {
                "fairy_dust",
                "hana",
                "hana_animation",
                "hana_catalyst",
                "hana_clerestory",
                "hana_conduit",
                "hana_diegetic",
                "hana_effects",
                "hana_kana",
                "hana_lading",
                "hana_lagrange",
                "hana_liminal",
                "hana_metrics",
                "hana_mimesis_tools",
                "hana_organon",
                "hana_origami",
                "hana_prosody",
                "hana_rigging",
                "hana_rigging_scripted",
                "hana_rubric",
                "hana_sdf",
                "hana_tool_macros",
                "hana_tool_schema",
                "hana_tools",
                "hana_valence",
                "hana_video",
                "hana_works",
            }
        ),
        frozenset(
            {
                "hana_clerestory",
                "hana_kana",
                "hana_lagrange",
                "hana_liminal",
                "hana_rigging",
                "hana_rubric",
            }
        ),
    ),
    "natedev": ProjectSpec("natepiano/nixos", frozenset(), frozenset()),
    "cargo-liner": ProjectSpec(
        "natepiano/cargo-liner",
        frozenset(
            {
                "cargo-berth",
                "cargo-berth-test-support",
                "cargo-handler",
                "cargo-mend",
                "cargo-port",
                "cargo-tile",
                "tui_pane",
            }
        ),
        frozenset({"cargo-mend", "cargo-port", "cargo-tile", "tui_pane"}),
    ),
    "bevy_brp": ProjectSpec(
        "natepiano/bevy_brp",
        frozenset(
            {
                "bevy_brp_extras",
                "bevy_brp_mcp",
                "bevy_brp_mcp_macros",
                "bevy_brp_test_apps",
                "test-app-a",
                "test-app-b",
            }
        ),
        frozenset({"bevy_brp_extras", "bevy_brp_mcp", "bevy_brp_mcp_macros"}),
    ),
    "obsidian_knife": ProjectSpec(
        "natepiano/obsidian_knife", frozenset({"obsidian_knife"}), frozenset()
    ),
    "nateroids": ProjectSpec(
        "natepiano/nateroids", frozenset({"nateroids"}), frozenset()
    ),
}

KINDS = ("feature", "fix", "change", "perf", "deprecation", "removal", "security")


class Note(TypedDict):
    title: str
    project: str
    kind: str
    summary: str
    area: str | None
    completed_date: str
    crate: list[str]
    link: str
    production: str | None
    unit: list[str]
    release: list[str]
    replaced_by: list[str]
    state: Literal["live", "superseded"]
    date_created: str
    date_modified: str
    behavior: str
    see: str
    evidence: list[str]


class FeatureLogEvent(TypedDict):
    at: str
    verb: Literal["add", "supersede", "amend"]
    project: str
    title: str
    kind: str
    summary: str
    release: list[str]
    backfill: bool
    old: list[str]
    fields: list[str]


class FeatureLogError(ValueError):
    """A rejected command or invalid feature note."""


def vault_dir() -> Path:
    """Return the feature-log vault selected by the current environment."""
    configured = os.environ.get("FEATURE_LOG_VAULT")
    return Path(configured) if configured else Path.home() / "rust/hanadocs"


def state_dir() -> Path:
    """Return the feature-log state directory selected by the current environment."""
    configured = os.environ.get("XDG_STATE_HOME")
    root = Path(configured) if configured else Path.home() / ".local/state"
    return root / "claude/feature-log"


def today() -> str:
    """Return the machine-local date used by the Obsidian Linter."""
    return date.today().isoformat()


FRONTMATTER_KEYS = frozenset(
    {
        "area",
        "completed_date",
        "crate",
        "date_created",
        "date_modified",
        "kind",
        "link",
        "production",
        "project",
        "release",
        "replaced_by",
        "return",
        "state",
        "summary",
        "tags",
        "unit",
    }
)
REQUIRED_FRONTMATTER_KEYS = frozenset(
    {
        "completed_date",
        "crate",
        "date_created",
        "date_modified",
        "kind",
        "link",
        "project",
        "release",
        "replaced_by",
        "return",
        "state",
        "summary",
        "tags",
    }
)
FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?: (.*))?$")
SAFE_SCALAR_RE = re.compile(r"^[A-Za-z0-9][^:#\"'\n]*$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_RE = re.compile(r"^(?:Unreleased|\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$")
BODY_SECTION_HEADINGS = frozenset({"## See it", "## Evidence"})
UNSAFE_BARE_SCALAR_PREFIXES = frozenset("-?:,[]{}#&*!|>'\"%@`")

YamlValue = str | list[str]


def normalize_title(value: str) -> str:
    title = value.lower().replace("-", " ")
    title = re.sub(r"[^a-z0-9 _]", "", title)
    title = re.sub(r" +", " ", title).strip()
    if not title:
        raise FeatureLogError("title normalizes to empty")
    if len(title) > 50:
        raise FeatureLogError("title is longer than 50 characters after normalization")
    return title


def _validate_date(value: str, field: str) -> None:
    if not DATE_RE.fullmatch(value):
        raise FeatureLogError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise FeatureLogError(f"{field} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise FeatureLogError(f"{field} must be YYYY-MM-DD")


def _yaml_scalar(value: str) -> str:
    if SAFE_SCALAR_RE.fullmatch(value) and not value.endswith(" "):
        return value
    return json.dumps(value, ensure_ascii=False)


def _yaml_link(value: str) -> str:
    return json.dumps(f"[[{value}]]", ensure_ascii=False)


def _render_list(key: str, values: list[str], links: bool = False) -> list[str]:
    if not values:
        return [f"{key}: []"]
    rendered = [f"{key}:"]
    for value in sorted(values):
        item = _yaml_link(value) if links else _yaml_scalar(value)
        rendered.append(f"  - {item}")
    return rendered


def render_note(note: Note) -> str:
    lines = ["---"]
    if note["area"] is not None:
        lines.append(f"area: {_yaml_link(note['area'])}")
    lines.append(f"completed_date: {_yaml_link(note['completed_date'])}")
    lines.extend(_render_list("crate", note["crate"]))
    lines.append(f"date_created: {_yaml_link(note['date_created'])}")
    lines.append(f"date_modified: {_yaml_link(note['date_modified'])}")
    lines.append(f"kind: {_yaml_scalar(note['kind'])}")
    lines.append(f"link: {note['link']}")
    if note["production"] is not None:
        lines.append(f"production: {_yaml_scalar(note['production'])}")
    lines.append(f"project: {_yaml_link(note['project'])}")
    lines.extend(_render_list("release", note["release"]))
    lines.extend(_render_list("replaced_by", note["replaced_by"], links=True))
    lines.extend(
        _render_list(
            "return",
            [f"feature-log - {note['project']}.base", "feature-log.base"],
            links=True,
        )
    )
    lines.append(f"state: {_yaml_scalar(note['state'])}")
    lines.append(f"summary: {_yaml_scalar(note['summary'])}")
    lines.append("tags: [feature-log]")
    if note["unit"]:
        lines.extend(_render_list("unit", note["unit"]))
    lines.append("---")
    lines.append(note["behavior"])
    lines.extend(["", "## See it", "", note["see"], "", "## Evidence", ""])
    lines.extend(f"- {item}" for item in note["evidence"])
    return "\n".join(lines) + "\n"


def _split_inline_list(value: str) -> list[str]:
    inside = value[1:-1].strip()
    if not inside:
        return []
    items: list[str] = []
    start = 0
    quote = ""
    index = 0
    while index < len(inside):
        character = inside[index]
        if quote == "\"" and character == "\\":
            index += 2
            continue
        if character in {"\"", "'"}:
            if not quote:
                quote = character
            elif quote == character:
                if quote == "'" and index + 1 < len(inside) and inside[index + 1] == "'":
                    index += 2
                    continue
                quote = ""
        elif character == "," and not quote:
            items.append(inside[start:index].strip())
            start = index + 1
        index += 1
    if quote:
        raise FeatureLogError("malformed quoted list item")
    items.append(inside[start:].strip())
    if any(not item for item in items):
        raise FeatureLogError("empty inline list item")
    return [_parse_scalar(item) for item in items]


def _parse_scalar(value: str) -> str:
    if not value:
        raise FeatureLogError("empty scalar")
    if value.startswith("\""):
        try:
            decoded = cast(object, json.loads(value))
        except json.JSONDecodeError as error:
            raise FeatureLogError("malformed double-quoted scalar") from error
        if not isinstance(decoded, str):
            raise FeatureLogError("expected a string scalar")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise FeatureLogError("malformed single-quoted scalar")
        return value[1:-1].replace("''", "'")
    if value.startswith("["):
        raise FeatureLogError("nested list is not a scalar")
    if (
        value != value.strip()
        or "\n" in value
        or ": " in value
        or " #" in value
        or value.endswith(":")
        or value[0] in UNSAFE_BARE_SCALAR_PREFIXES
    ):
        raise FeatureLogError("malformed bare scalar")
    return value


def _parse_value(value: str) -> YamlValue:
    if value.startswith("["):
        if not value.endswith("]"):
            raise FeatureLogError("malformed inline list")
        return _split_inline_list(value)
    return _parse_scalar(value)


def _string_field(values: dict[str, YamlValue], key: str) -> str:
    value = values[key]
    if not isinstance(value, str):
        raise FeatureLogError(f"{key} must be a string")
    return value


def _list_field(values: dict[str, YamlValue], key: str) -> list[str]:
    value = values[key]
    if isinstance(value, str):
        raise FeatureLogError(f"{key} must be a list")
    return value


def _linked_value(value: str, field: str) -> str:
    if len(value) < 5 or value[:2] != "[[" or value[-2:] != "]]":
        raise FeatureLogError(f"{field} must be a wikilink")
    target = value[2:-2]
    if not target:
        raise FeatureLogError(f"{field} must be a non-empty wikilink")
    return target


def _parse_body(lines: list[str]) -> tuple[str, str, list[str]]:
    body = "\n".join(lines)
    see_marker = "\n\n## See it\n\n"
    evidence_marker = "\n\n## Evidence\n\n"
    if see_marker not in body:
        raise FeatureLogError("body is missing the See it section")
    behavior, remainder = body.split(see_marker, 1)
    if evidence_marker not in remainder:
        raise FeatureLogError("body is missing the Evidence section")
    see, evidence_text = remainder.split(evidence_marker, 1)
    evidence_lines = evidence_text.split("\n")
    if evidence_lines and evidence_lines[-1] == "":
        _ = evidence_lines.pop()
    if not behavior.strip():
        raise FeatureLogError("behavior is empty")
    if not see.strip():
        raise FeatureLogError("see is empty")
    if not evidence_lines:
        raise FeatureLogError("evidence is empty")
    evidence: list[str] = []
    for line in evidence_lines:
        if not line.startswith("- ") or not line[2:].strip():
            raise FeatureLogError("evidence must contain one non-empty Markdown item per line")
        evidence.append(line[2:])
    return behavior, see, evidence


def parse_note(path: Path) -> Note:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise FeatureLogError(str(error)) from error
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise FeatureLogError("missing opening frontmatter delimiter")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise FeatureLogError("missing closing frontmatter delimiter") from error

    values: dict[str, YamlValue] = {}
    index = 1
    while index < closing:
        line = lines[index]
        match = FIELD_RE.fullmatch(line)
        if match is None:
            raise FeatureLogError(f"invalid frontmatter line {index + 1}")
        key = match.group(1)
        if key not in FRONTMATTER_KEYS:
            raise FeatureLogError(f"unknown frontmatter key {key}")
        if key in values:
            raise FeatureLogError(f"duplicate frontmatter key {key}")
        raw = match.group(2)
        if raw is None or raw == "":
            items: list[str] = []
            index += 1
            while index < closing and lines[index].startswith("  - "):
                items.append(_parse_scalar(lines[index][4:]))
                index += 1
            if not items:
                raise FeatureLogError(f"{key} has an empty block list")
            values[key] = items
            continue
        values[key] = _parse_value(raw)
        index += 1

    missing = sorted(REQUIRED_FRONTMATTER_KEYS - values.keys())
    if missing:
        raise FeatureLogError(f"missing frontmatter key {missing[0]}")

    behavior, see, evidence = _parse_body(lines[closing + 1 :])
    state_value = _string_field(values, "state")
    if state_value not in {"live", "superseded"}:
        raise FeatureLogError("state must be live or superseded")
    state = cast(Literal["live", "superseded"], state_value)
    project = _linked_value(_string_field(values, "project"), "project")
    note: Note = {
        "title": path.stem,
        "project": project,
        "kind": _string_field(values, "kind"),
        "summary": _string_field(values, "summary"),
        "area": (
            _linked_value(_string_field(values, "area"), "area")
            if "area" in values
            else None
        ),
        "completed_date": _linked_value(
            _string_field(values, "completed_date"), "completed_date"
        ),
        "crate": _list_field(values, "crate"),
        "link": _string_field(values, "link"),
        "production": _string_field(values, "production") if "production" in values else None,
        "unit": _list_field(values, "unit") if "unit" in values else [],
        "release": _list_field(values, "release"),
        "replaced_by": [
            _linked_value(item, "replaced_by") for item in _list_field(values, "replaced_by")
        ],
        "state": state,
        "date_created": _linked_value(_string_field(values, "date_created"), "date_created"),
        "date_modified": _linked_value(
            _string_field(values, "date_modified"), "date_modified"
        ),
        "behavior": behavior,
        "see": see,
        "evidence": evidence,
    }
    expected_return = [f"[[feature-log - {project}.base]]", "[[feature-log.base]]"]
    if _list_field(values, "return") != expected_return:
        raise FeatureLogError("return must name the project Base and global Base")
    if _list_field(values, "tags") != ["feature-log"]:
        raise FeatureLogError("tags must be [feature-log]")
    return note


def _validate_project(project: str) -> ProjectSpec:
    try:
        return PROJECTS[project]
    except KeyError as error:
        raise FeatureLogError(f"unknown project {project}") from error


def _validate_crates(project: str, crates: list[str]) -> list[str]:
    spec = _validate_project(project)
    unique = sorted(set(crates))
    if project == "natedev":
        if unique:
            raise FeatureLogError("natedev does not accept crates")
        return []
    if not unique:
        raise FeatureLogError(f"project {project} requires at least one crate")
    invalid = [crate for crate in unique if crate not in spec.crates]
    if invalid:
        raise FeatureLogError(f"crate {invalid[0]} does not belong to project {project}")
    return unique


def _validate_release_values(project: str, crates: list[str], releases: list[str]) -> list[str]:
    spec = _validate_project(project)
    validated: set[str] = set()
    for release in releases:
        crate, separator, version = release.partition("@")
        if not separator or "@" in version:
            raise FeatureLogError(f"invalid release {release}")
        if crate not in crates:
            raise FeatureLogError(f"release crate {crate} is not in --crate")
        if crate not in spec.published:
            raise FeatureLogError(f"release crate {crate} is not published")
        if not VERSION_RE.fullmatch(version):
            raise FeatureLogError(f"invalid release version {version}")
        validated.add(release)
    return sorted(validated)


def _canonical_link(project: str, value: str) -> str:
    if not value.strip():
        raise FeatureLogError("link is empty")
    spec = _validate_project(project)
    if SHA_RE.fullmatch(value):
        return f"https://github.com/{spec.repo}/commit/{value.lower()}"
    prefix = f"https://github.com/{spec.repo}/commit/"
    if not value.startswith(prefix) or not SHA_RE.fullmatch(value[len(prefix) :]):
        raise FeatureLogError(f"link must be a 40-hex commit for {spec.repo}")
    return prefix + value[len(prefix) :].lower()


def _validate_sorted_unique(values: list[str], field: str) -> None:
    if values != sorted(set(values)):
        raise FeatureLogError(f"{field} must be sorted with no duplicates")


def _validate_body_section(value: str, field: str) -> None:
    if not value.strip():
        raise FeatureLogError(f"{field} is empty")
    if any(line.rstrip() in BODY_SECTION_HEADINGS for line in value.splitlines()):
        raise FeatureLogError(
            f"{field} must not contain a See it or Evidence section heading"
        )


def validate_note(note: Note, expected_project: str | None = None) -> None:
    spec = _validate_project(note["project"])
    if expected_project is not None and note["project"] != expected_project:
        raise FeatureLogError(f"project must be {expected_project}")
    if normalize_title(note["title"]) != note["title"]:
        raise FeatureLogError("file name is not a normalized title")
    if note["kind"] not in KINDS:
        raise FeatureLogError(f"unknown kind {note['kind']}")
    if not note["summary"].strip():
        raise FeatureLogError("summary is empty")
    if note["summary"].startswith("- "):
        raise FeatureLogError("summary must not start with '- '")
    if "\n" in note["summary"]:
        raise FeatureLogError("summary must not contain a newline")
    _validate_body_section(note["behavior"], "behavior")
    _validate_body_section(note["see"], "see")
    if not note["evidence"] or any(
        not item.strip() or "\n" in item for item in note["evidence"]
    ):
        raise FeatureLogError("evidence is empty")
    _validate_date(note["completed_date"], "completed_date")
    _validate_date(note["date_created"], "date_created")
    _validate_date(note["date_modified"], "date_modified")
    if _canonical_link(note["project"], note["link"]) != note["link"]:
        raise FeatureLogError("link is not canonical")
    validated_crates = _validate_crates(note["project"], note["crate"])
    if validated_crates != note["crate"]:
        raise FeatureLogError("crate must be sorted with no duplicates")
    if _validate_release_values(note["project"], note["crate"], note["release"]) != note["release"]:
        raise FeatureLogError("release must be sorted with no duplicates")
    _validate_sorted_unique(note["replaced_by"], "replaced_by")
    _validate_sorted_unique(note["unit"], "unit")
    if note["state"] == "live" and note["replaced_by"]:
        raise FeatureLogError("a live note must have replaced_by: []")
    if note["state"] == "superseded" and not note["replaced_by"]:
        raise FeatureLogError("a superseded note must name a replacement")
    if note["project"] != "hana" and note["production"] is not None:
        raise FeatureLogError("production is only valid for hana")
    if note["project"] != "hana" and note["unit"]:
        raise FeatureLogError("unit is only valid for hana")
    if note["production"] is not None and not note["production"].strip():
        raise FeatureLogError("production is empty")
    if any(not unit.strip() for unit in note["unit"]):
        raise FeatureLogError("unit is empty")
    if note["area"] is not None and not note["area"].strip():
        raise FeatureLogError("area is empty")
    _ = spec


def _flatten(values: list[list[str]] | None) -> list[str]:
    if values is None:
        return []
    return [item for group in values for item in group]


def _new_note(arguments: argparse.Namespace) -> Note:
    project = cast(str, arguments.project)
    _ = _validate_project(project)
    title = normalize_title(cast(str, arguments.title))
    kind = cast(str, arguments.kind)
    if kind not in KINDS:
        raise FeatureLogError(f"unknown kind {kind}")
    summary = cast(str, arguments.summary)
    behavior = cast(str, arguments.behavior)
    see = cast(str, arguments.see)
    raw_link = cast(str, arguments.link)
    if not summary.strip():
        raise FeatureLogError("summary is empty")
    if summary.startswith("- "):
        raise FeatureLogError("summary must not start with '- '")
    if "\n" in summary:
        raise FeatureLogError("summary must not contain a newline")
    if not behavior.strip():
        raise FeatureLogError("behavior is empty")
    if not see.strip():
        raise FeatureLogError("see is empty")
    if not raw_link.strip():
        raise FeatureLogError("link is empty")
    evidence = [item.strip() for item in _flatten(cast(list[list[str]] | None, arguments.evidence))]
    if not evidence or any(not item or "\n" in item for item in evidence):
        raise FeatureLogError("at least one non-empty --evidence is required")
    crates = _validate_crates(
        project, _flatten(cast(list[list[str]] | None, arguments.crate))
    )
    production = cast(str | None, arguments.production)
    units = sorted(set(_flatten(cast(list[list[str]] | None, arguments.unit))))
    if project != "hana" and production is not None:
        raise FeatureLogError("--production is only valid for hana")
    if project != "hana" and units:
        raise FeatureLogError("--unit is only valid for hana")
    if production is not None and not production.strip():
        raise FeatureLogError("production is empty")
    if any(not unit.strip() for unit in units):
        raise FeatureLogError("unit is empty")
    area = cast(str | None, arguments.area)
    if area is not None and not area.strip():
        raise FeatureLogError("area is empty")
    completed = cast(str | None, arguments.completed) or today()
    _validate_date(completed, "completed")
    backfill = cast(bool, arguments.backfill)
    raw_releases = _flatten(cast(list[list[str]] | None, arguments.release))
    if not backfill:
        raw_releases.extend(
            f"{crate}@Unreleased" for crate in crates if crate in PROJECTS[project].published
        )
    releases = _validate_release_values(project, crates, raw_releases)
    stamp = today()
    note: Note = {
        "title": title,
        "project": project,
        "kind": kind,
        "summary": summary,
        "area": area,
        "completed_date": completed,
        "crate": crates,
        "link": _canonical_link(project, raw_link),
        "production": production,
        "unit": units,
        "release": releases,
        "replaced_by": [],
        "state": "live",
        "date_created": stamp,
        "date_modified": stamp,
        "behavior": behavior.strip(),
        "see": see.strip(),
        "evidence": evidence,
    }
    validate_note(note, project)
    return note


def _note_path(note: Note) -> Path:
    return vault_dir() / "feature-log" / note["project"] / f"{note['title']}.md"


def _relative(path: Path) -> str:
    try:
        return path.relative_to(vault_dir()).as_posix()
    except ValueError:
        return str(path)


@contextmanager
def locked() -> Generator[None, None, None]:
    """Hold the exclusive feature-log writer lock for this context."""
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_note(path: Path, note: Note) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        _ = temporary.write_text(render_note(note), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _event_time() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).replace(microsecond=0).isoformat()


def _append_event(event: FeatureLogEvent) -> None:
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "events.jsonl").open("a", encoding="utf-8") as output:
        _ = output.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")


def _event(
    verb: Literal["add", "supersede", "amend"],
    note: Note,
    *,
    backfill: bool,
    old: list[str],
    fields: list[str],
) -> FeatureLogEvent:
    return {
        "at": _event_time(),
        "verb": verb,
        "project": note["project"],
        "title": note["title"],
        "kind": note["kind"],
        "summary": note["summary"],
        "release": note["release"],
        "backfill": backfill,
        "old": old,
        "fields": fields,
    }


def _project_notes(project: str) -> list[tuple[Path, Note]]:
    folder = vault_dir() / "feature-log" / project
    notes: list[tuple[Path, Note]] = []
    if not folder.exists():
        return notes
    for path in sorted(folder.glob("*.md")):
        note = parse_note(path)
        validate_note(note, project)
        notes.append((path, note))
    return notes


def _same_repeat(
    existing: Note,
    candidate: Note,
    *,
    completed_date_was_defaulted: bool,
) -> bool:
    comparable = candidate.copy()
    comparable["date_created"] = existing["date_created"]
    comparable["date_modified"] = existing["date_modified"]
    if completed_date_was_defaulted:
        comparable["completed_date"] = existing["completed_date"]
    return comparable == existing


def _reject_duplicate_link(
    note: Note,
    path: Path,
    separate_result: bool,
    replacing_titles: frozenset[str],
) -> None:
    if separate_result:
        return
    for other_path, other in _project_notes(note["project"]):
        if (
            other_path != path
            and other["title"] not in replacing_titles
            and other["state"] == "live"
            and other["link"] == note["link"]
        ):
            raise FeatureLogError(
                f"link already belongs to live note {other['title']}; use --separate-result"
            )


def _prepare_new(
    note: Note,
    separate_result: bool,
    *,
    completed_date_was_defaulted: bool,
    replacing_titles: frozenset[str],
) -> tuple[bool, Note]:
    path = _note_path(note)
    if path.exists():
        existing = parse_note(path)
        validate_note(existing, note["project"])
        if existing["link"] != note["link"]:
            raise FeatureLogError("title already belongs to a different link; make the title more specific")
        if not _same_repeat(
            existing,
            note,
            completed_date_was_defaulted=completed_date_was_defaulted,
        ):
            raise FeatureLogError("note already exists with different fields; use amend")
        return False, existing
    _reject_duplicate_link(note, path, separate_result, replacing_titles)
    return True, note


def _run_add(arguments: argparse.Namespace) -> int:
    note = _new_note(arguments)
    path = _note_path(note)
    with locked():
        should_write, stored = _prepare_new(
            note,
            cast(bool, arguments.separate_result),
            completed_date_was_defaulted=cast(str | None, arguments.completed) is None,
            replacing_titles=frozenset(),
        )
        if not should_write:
            print(f"unchanged: {_relative(path)}")
            return 0
        _write_note(path, stored)
        _append_event(
            _event(
                "add",
                stored,
                backfill=cast(bool, arguments.backfill),
                old=[],
                fields=[],
            )
        )
    print(f"added: {_relative(path)}")
    return 0


def _updated_replacement(note: Note, replacement: str) -> Note:
    updated = note.copy()
    updated["state"] = "superseded"
    updated["replaced_by"] = sorted(set([*note["replaced_by"], replacement]))
    updated["date_modified"] = today()
    return updated


def _run_supersede(arguments: argparse.Namespace) -> int:
    note = _new_note(arguments)
    old_titles = [normalize_title(item) for item in cast(list[str], arguments.old)]
    old_titles = list(dict.fromkeys(old_titles))
    if note["title"] in old_titles:
        raise FeatureLogError("--old must not equal the new title")
    new_path = _note_path(note)
    outputs: list[str] = []
    with locked():
        write_new, stored = _prepare_new(
            note,
            cast(bool, arguments.separate_result),
            completed_date_was_defaulted=cast(str | None, arguments.completed) is None,
            replacing_titles=frozenset(old_titles),
        )
        replacements: list[tuple[Path, Note, str]] = []
        for old_title in old_titles:
            old_path = vault_dir() / "feature-log" / note["project"] / f"{old_title}.md"
            if not old_path.exists():
                raise FeatureLogError(f"old note {old_title} is not a live note of {note['project']}")
            old_note = parse_note(old_path)
            validate_note(old_note, note["project"])
            if old_note["state"] == "live":
                replacements.append(
                    (old_path, _updated_replacement(old_note, note["title"]), old_title)
                )
            elif note["title"] not in old_note["replaced_by"]:
                raise FeatureLogError(f"old note {old_title} is not live")

        if write_new:
            _write_note(new_path, stored)
            outputs.append(f"added: {_relative(new_path)}")
        else:
            outputs.append(f"unchanged: {_relative(new_path)}")
        for old_path, replacement, old_title in replacements:
            _write_note(old_path, replacement)
            outputs.append(f"superseded: {old_title} → {note['title']}")
        if write_new or replacements:
            _append_event(
                _event(
                    "supersede",
                    stored,
                    backfill=cast(bool, arguments.backfill),
                    old=old_titles,
                    fields=[],
                )
            )
    for output in outputs:
        print(output)
    return 0


def _run_amend(arguments: argparse.Namespace) -> int:
    project = cast(str, arguments.project)
    _ = _validate_project(project)
    title = normalize_title(cast(str, arguments.title))
    summary_argument = cast(str | None, arguments.summary)
    behavior_argument = cast(str | None, arguments.behavior)
    see_argument = cast(str | None, arguments.see)
    area_argument = cast(str | None, arguments.area)
    kind_argument = cast(str | None, arguments.kind)
    completed_argument = cast(str | None, arguments.completed)
    crate_arguments = cast(list[list[str]] | None, arguments.crate)
    evidence_arguments = cast(list[list[str]] | None, arguments.evidence)
    evidence_additions = [
        item.strip() for item in _flatten(evidence_arguments)
    ]
    if any(not item or "\n" in item for item in evidence_additions):
        raise FeatureLogError("evidence is empty")
    requested = any(
        value is not None
        for value in (
            summary_argument,
            behavior_argument,
            see_argument,
            area_argument,
            kind_argument,
            completed_argument,
            crate_arguments,
            evidence_arguments,
        )
    )
    if not requested:
        raise FeatureLogError("amend requires at least one field")
    path = vault_dir() / "feature-log" / project / f"{title}.md"
    with locked():
        if not path.exists():
            raise FeatureLogError(f"note {title} does not exist in {project}")
        note = parse_note(path)
        validate_note(note, project)
        updated = note.copy()
        changed: list[str] = []

        if summary_argument is not None and updated["summary"] != summary_argument:
            updated["summary"] = summary_argument
            changed.append("summary")
        if behavior_argument is not None and updated["behavior"] != behavior_argument:
            updated["behavior"] = behavior_argument
            changed.append("behavior")
        if see_argument is not None and updated["see"] != see_argument:
            updated["see"] = see_argument
            changed.append("see")
        if area_argument is not None and updated["area"] != area_argument:
            updated["area"] = area_argument
            changed.append("area")
        if kind_argument is not None and updated["kind"] != kind_argument:
            updated["kind"] = kind_argument
            changed.append("kind")
        if (
            completed_argument is not None
            and updated["completed_date"] != completed_argument
        ):
            updated["completed_date"] = completed_argument
            changed.append("completed_date")

        if crate_arguments is not None:
            crates = _validate_crates(project, _flatten(crate_arguments))
            if crates != updated["crate"]:
                updated["crate"] = crates
                updated["release"] = [
                    release
                    for release in updated["release"]
                    if release.partition("@")[0] in crates
                ]
                changed.append("crate")
        if evidence_additions:
            updated["evidence"] = [*updated["evidence"], *evidence_additions]
            changed.append("evidence")

        if not changed:
            print(f"unchanged: {_relative(path)}")
            return 0
        updated["behavior"] = updated["behavior"].strip()
        updated["see"] = updated["see"].strip()
        updated["date_modified"] = today()
        validate_note(updated, project)
        _write_note(path, updated)
        _append_event(
            _event(
                "amend",
                updated,
                backfill=False,
                old=[],
                fields=changed,
            )
        )
    print(f"amended: {_relative(path)} ({', '.join(changed)})")
    return 0


def _run_live(arguments: argparse.Namespace) -> int:
    project = cast(str, arguments.project)
    _ = _validate_project(project)
    crate = cast(str | None, arguments.crate)
    if crate is not None and crate not in PROJECTS[project].crates:
        raise FeatureLogError(f"crate {crate} does not belong to project {project}")
    area = cast(str | None, arguments.area)
    kind = cast(str | None, arguments.kind)
    if kind is not None and kind not in KINDS:
        raise FeatureLogError(f"unknown kind {kind}")
    grep = cast(str | None, arguments.grep)
    matches: list[Note] = []
    for _path, note in _project_notes(project):
        if note["state"] != "live":
            continue
        if crate is not None and crate not in note["crate"]:
            continue
        if area is not None and area != note["area"]:
            continue
        if kind is not None and kind != note["kind"]:
            continue
        if grep is not None:
            needle = grep.casefold()
            haystack = "\n".join((note["title"], note["summary"], note["behavior"])).casefold()
            if needle not in haystack:
                continue
        matches.append(note)
    matches.sort(key=lambda item: (item["completed_date"], item["title"]), reverse=True)
    for note in matches:
        crates = ", ".join(note["crate"])
        area_text = f" ({note['area']})" if note["area"] is not None else ""
        output = f"{note['completed_date']} {note['kind']} {note['title']} — {note['summary']} [{crates}]{area_text}"
        print(output)
    return 0


def _run_check() -> int:
    errors: list[tuple[Path, str]] = []
    notes: dict[tuple[str, str], Note] = {}
    for project in PROJECTS:
        folder = vault_dir() / "feature-log" / project
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                note = parse_note(path)
                validate_note(note, project)
                notes[(project, note["title"])] = note
            except (FeatureLogError, OSError) as error:
                errors.append((path, str(error)))
    for (project, _title), note in notes.items():
        for replacement in note["replaced_by"]:
            if (project, replacement) not in notes:
                path = vault_dir() / "feature-log" / project / f"{note['title']}.md"
                errors.append((path, f"replaced_by target {replacement} does not exist in {project}"))
    if errors:
        for path, reason in sorted(errors, key=lambda item: str(item[0])):
            print(f"{_relative(path)}: {reason}")
        return 1
    print(f"ok: {len(notes)} notes")
    return 0


class FeatureLogArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # pyright: ignore[reportImplicitOverride]
        raise FeatureLogError(message)


def _add_new_note_arguments(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--project", required=True)
    _ = parser.add_argument("--title", required=True)
    _ = parser.add_argument("--kind", required=True)
    _ = parser.add_argument("--summary", required=True)
    _ = parser.add_argument("--behavior", required=True)
    _ = parser.add_argument("--see", required=True)
    _ = parser.add_argument("--link", required=True)
    _ = parser.add_argument("--evidence", action="append", nargs="+", required=True)
    _ = parser.add_argument("--crate", action="append", nargs="+")
    _ = parser.add_argument("--area")
    _ = parser.add_argument("--completed")
    _ = parser.add_argument("--production")
    _ = parser.add_argument("--unit", action="append", nargs="+")
    _ = parser.add_argument("--release", action="append", nargs="+")
    _ = parser.add_argument("--backfill", action="store_true")
    _ = parser.add_argument("--separate-result", action="store_true")


def _parser() -> FeatureLogArgumentParser:
    parser = FeatureLogArgumentParser(prog="feature_log")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add")
    _add_new_note_arguments(add)

    supersede = subparsers.add_parser("supersede")
    _ = supersede.add_argument("--old", action="append", required=True)
    _add_new_note_arguments(supersede)

    amend = subparsers.add_parser("amend")
    _ = amend.add_argument("--project", required=True)
    _ = amend.add_argument("--title", required=True)
    _ = amend.add_argument("--summary")
    _ = amend.add_argument("--behavior")
    _ = amend.add_argument("--see")
    _ = amend.add_argument("--area")
    _ = amend.add_argument("--kind")
    _ = amend.add_argument("--completed")
    _ = amend.add_argument("--crate", action="append", nargs="+")
    _ = amend.add_argument("--evidence", action="append", nargs="+")

    live = subparsers.add_parser("live")
    _ = live.add_argument("--project", required=True)
    _ = live.add_argument("--crate")
    _ = live.add_argument("--area")
    _ = live.add_argument("--kind")
    _ = live.add_argument("--grep")

    _ = subparsers.add_parser("check")
    return parser


def main(argv: list[str]) -> int:
    try:
        arguments = _parser().parse_args(argv)
        command = cast(str, arguments.command)
        if command == "add":
            return _run_add(arguments)
        if command == "supersede":
            return _run_supersede(arguments)
        if command == "amend":
            return _run_amend(arguments)
        if command == "live":
            return _run_live(arguments)
        if command == "check":
            return _run_check()
        raise FeatureLogError(f"unknown command {command}")
    except FeatureLogError as error:
        print(f"feature_log: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
