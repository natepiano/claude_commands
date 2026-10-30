#!/usr/bin/env python3
"""Write and inspect the current, user-visible feature log."""

from __future__ import annotations

import argparse
import fcntl
import json
import hashlib
import os
import re
import shlex
import socket as socket
import subprocess as subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal, NamedTuple, NoReturn, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402
from atomic_file import write_text_atomic  # noqa: E402


class ProjectSpec(NamedTuple):
    repo: str
    crates: frozenset[str]
    published: frozenset[str]
    checkouts: tuple[Path, ...]


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
        (Path.home() / "rust/hana", Path.home() / "rust/hana_catalyst"),
    ),
    "natedev": ProjectSpec(
        "natepiano/nixos", frozenset(), frozenset(), (Path("/etc/nixos"),)
    ),
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
        (Path.home() / "rust/cargo-liner",),
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
        (Path.home() / "rust/bevy_brp",),
    ),
    "obsidian_knife": ProjectSpec(
        "natepiano/obsidian_knife",
        frozenset({"obsidian_knife"}),
        frozenset(),
        (Path.home() / "rust/obsidian_knife",),
    ),
    "nateroids": ProjectSpec(
        "natepiano/nateroids",
        frozenset({"nateroids"}),
        frozenset(),
        (Path.home() / "rust/nateroids",),
    ),
    "claude": ProjectSpec(
        "natepiano/claude_commands", frozenset(), frozenset(), (Path.home() / ".claude",)
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


class AddEvent(TypedDict):
    at: str
    verb: Literal["add"]
    project: str
    title: str
    kind: str
    summary: str
    release: list[str]
    backfill: bool


class SupersedeEvent(TypedDict):
    at: str
    verb: Literal["supersede"]
    project: str
    title: str
    kind: str
    summary: str
    release: list[str]
    backfill: bool
    old: list[str]


class LegacyAmendEvent(TypedDict):
    """An amendment journaled before provenance snapshots were supported."""

    at: str
    verb: Literal["amend"]
    project: str
    title: str
    fields: list[str]
    backfill: bool


class AmendEvent(TypedDict):
    at: str
    verb: Literal["amend"]
    project: str
    title: str
    fields: list[str]
    backfill: bool
    old_link: str
    new_link: str
    old_completed: str
    new_completed: str
    releases_added: list[str]
    content: NotRequired[str]


class ReleaseEvent(TypedDict):
    at: str
    verb: Literal["release"]
    project: str
    crate: str
    version: str
    titles: list[str]


FeatureLogEvent = AddEvent | SupersedeEvent | LegacyAmendEvent | AmendEvent | ReleaseEvent


class ChangelogSelection(NamedTuple):
    section: str
    titles: list[str]


class ForwardFailed(NamedTuple):
    exit: int
    stderr: str


class ChangelogSelectionJson(TypedDict):
    section: str
    titles: list[str]


class ReviewedReleaseContents(TypedDict):
    crate: str
    titles: list[str]


class NoSavedContents(NamedTuple):
    """Compare without keeping a release snapshot."""


class SaveReviewedContents(NamedTuple):
    path: Path


class RenderSection(NamedTuple):
    crate: str
    version: str
    selection_json: bool


class CompareSection(NamedTuple):
    crate: str
    version: str
    path: Path
    saving: NoSavedContents | SaveReviewedContents


ForwardedReader = Callable[[list[str]], ChangelogSelection | ForwardFailed]
ReleaseForwarder = Callable[[list[str], ReviewedReleaseContents], int]


class KeepAcceptingProvenance(NamedTuple):
    """Preserve the note's accepting commit and completion date."""


class CorrectAcceptingProvenance(NamedTuple):
    """Replace the accepting commit together with its completion date."""

    link: str
    completed: str


AcceptingProvenanceAmendment = KeepAcceptingProvenance | CorrectAcceptingProvenance


class ReplaceLiveNotes(NamedTuple):
    """Replace live notes, allowing repeats of the same replacement."""


class ExtendReplacementSet(NamedTuple):
    """Add a replacement even when an old note is already superseded."""


ReplacementPolicy = ReplaceLiveNotes | ExtendReplacementSet


class ProposalArgs(TypedDict):
    project: str
    title: str
    kind: str
    summary: str
    behavior: str
    see: str
    link: str
    evidence: list[str]
    crate: list[str]
    area: str | None
    completed: str
    production: str | None
    unit: list[str]
    release: list[str]
    backfill: bool
    separate_result: bool
    old: list[str]


class OpenProposal(TypedDict):
    row: str
    verb: Literal["add", "supersede"]
    args: ProposalArgs
    status: Literal["open"]
    at: str


class LandedProposal(TypedDict):
    row: str
    verb: Literal["add", "supersede"]
    args: ProposalArgs
    status: Literal["landed"]
    at: str
    note: str


class DroppedProposal(TypedDict):
    row: str
    verb: Literal["add", "supersede"]
    args: ProposalArgs
    status: Literal["dropped"]
    at: str
    verdict: str


FeatureLogProposal = OpenProposal | LandedProposal | DroppedProposal


class DigestCursor(TypedDict):
    line: int
    at: str


SshRunner = Callable[[list[str]], int]


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


def _run_ssh(command: list[str]) -> int:
    """Run a feature-log command on the vault host with inherited output."""
    return subprocess.run(command, check=False).returncode


def _ssh_command(argv: list[str]) -> list[str]:
    remote = "~/.claude/scripts/lib/py ~/.claude/scripts/feature_log/feature_log.py "
    return ["ssh", "natedev", remote + shlex.join(argv)]


def _read_forwarded_selection(command: list[str]) -> ChangelogSelection | ForwardFailed:
    """Read a selection from the vault host without forwarding local paths."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except (OSError, UnicodeError) as error:
        return ForwardFailed(2, str(error))
    if result.returncode != 0:
        return ForwardFailed(result.returncode, result.stderr)
    try:
        decoded = cast(object, json.loads(result.stdout))
        value = _json_object(decoded, "changelog selection")
        _require_json_keys(value, frozenset({"section", "titles"}), "changelog selection")
        return ChangelogSelection(
            _json_string(value.get("section"), "selection section"),
            _reviewed_titles(value.get("titles")),
        )
    except (json.JSONDecodeError, FeatureLogError) as error:
        return ForwardFailed(2, f"invalid forwarded changelog selection: {error}")


def _forward_release(command: list[str], contents: ReviewedReleaseContents) -> int:
    """Send caller-validated reviewed contents on stdin to the vault host."""
    try:
        return subprocess.run(
            command, input=json.dumps(contents), text=True, check=False
        ).returncode
    except (OSError, UnicodeError) as error:
        raise FeatureLogError(f"cannot forward release: {error}") from error


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
VERSION_RE = re.compile(
    r"^(?:Unreleased|\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)
# A bare row is a row of hana's list; a row of another showrunner's list names it: natedev/C3.
REVIEW_ROW_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_-]*/)?[AC]\d+$")
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
    if not spec.crates:
        if unique:
            raise FeatureLogError(f"{project} does not accept crates")
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
    sorted_releases = sorted(validated)
    for release in sorted_releases:
        crate, _, version = release.partition("@")
        if version != "Unreleased" and f"{crate}@Unreleased" in validated:
            raise FeatureLogError(
                f"release cannot hold both {crate}@Unreleased and {release}"
            )
    return sorted_releases


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


def _commit_date(project: ProjectSpec, sha: str) -> str:
    """Read the commit's committer date from the first checkout that holds it."""
    for checkout in project.checkouts:
        try:
            result = subprocess.run(
                ["git", "-C", str(checkout), "show", "-s", "--format=%cI", sha],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if result.returncode != 0:
            continue
        try:
            committed = datetime.fromisoformat(result.stdout.strip())
        except ValueError as error:
            raise FeatureLogError(
                f"cannot date commit {sha}: invalid committer date in {checkout}; pass --completed"
            ) from error
        return committed.astimezone(user_zone()).date().isoformat()
    raise FeatureLogError(
        f"cannot date commit {sha}: no checkout of {project.repo} holds it; pass --completed"
    )


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
    spec = _validate_project(project)
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
    if production is not None and not production.strip():
        raise FeatureLogError("production is empty")
    if any(not unit.strip() for unit in units):
        raise FeatureLogError("unit is empty")
    area = cast(str | None, arguments.area)
    if area is not None and not area.strip():
        raise FeatureLogError("area is empty")
    link = _canonical_link(project, raw_link)
    completed = cast(str | None, arguments.completed)
    if completed is None:
        completed = _commit_date(spec, link.rsplit("/", 1)[1])
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
        "link": link,
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
    write_text_atomic(path, render_note(note))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")


def _event_time() -> str:
    return datetime.now(user_zone()).replace(microsecond=0).isoformat()


def _append_event(event: FeatureLogEvent) -> None:
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "events.jsonl").open("a", encoding="utf-8") as output:
        _ = output.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")


def _add_event(note: Note, backfill: bool) -> AddEvent:
    return {
        "at": _event_time(),
        "verb": "add",
        "project": note["project"],
        "title": note["title"],
        "kind": note["kind"],
        "summary": note["summary"],
        "release": note["release"],
        "backfill": backfill,
    }


def _supersede_event(note: Note, backfill: bool, old: list[str]) -> SupersedeEvent:
    return {
        "at": _event_time(),
        "verb": "supersede",
        "project": note["project"],
        "title": note["title"],
        "kind": note["kind"],
        "summary": note["summary"],
        "release": note["release"],
        "backfill": backfill,
        "old": old,
    }


def _amend_event(
    old: Note, updated: Note, backfill: bool, fields: list[str]
) -> AmendEvent:
    return {
        "at": _event_time(),
        "verb": "amend",
        "project": updated["project"],
        "title": updated["title"],
        "fields": fields,
        "backfill": backfill,
        "old_link": old["link"],
        "new_link": updated["link"],
        "old_completed": old["completed_date"],
        "new_completed": updated["completed_date"],
        "releases_added": sorted(set(updated["release"]) - set(old["release"])),
        "content": hashlib.sha256(render_note(updated).encode("utf-8")).hexdigest(),
    }


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FeatureLogError(f"{label} must be a JSON object")
    object_value = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in object_value):
        raise FeatureLogError(f"{label} must be a JSON object")
    return cast(dict[str, object], object_value)


def _json_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise FeatureLogError(f"{field} must be a string")
    return value


def _json_optional_string(value: object, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise FeatureLogError(f"{field} must be a string or null")


def _json_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise FeatureLogError(f"{field} must be a boolean")
    return value


def _json_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FeatureLogError(f"{field} must be an integer")
    return value


def _json_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise FeatureLogError(f"{field} must be a list of strings")
    object_items = cast(list[object], value)
    if not all(isinstance(item, str) for item in object_items):
        raise FeatureLogError(f"{field} must be a list of strings")
    return cast(list[str], object_items)


def _require_json_keys(
    value: dict[str, object], expected: frozenset[str], label: str
) -> None:
    if value.keys() != expected:
        raise FeatureLogError(f"{label} has invalid fields")


def _parse_event(line: str, line_number: int) -> FeatureLogEvent:
    try:
        decoded = cast(object, json.loads(line))
    except json.JSONDecodeError as error:
        raise FeatureLogError(f"events.jsonl line {line_number} is invalid JSON") from error
    value = _json_object(decoded, f"events.jsonl line {line_number}")
    verb = _json_string(value.get("verb"), "event verb")
    if verb == "release":
        _require_json_keys(
            value, frozenset({"at", "verb", "project", "crate", "version", "titles"}),
            "release event",
        )
        return {
            "at": _json_string(value.get("at"), "event at"),
            "verb": "release",
            "project": _json_string(value.get("project"), "event project"),
            "crate": _json_string(value.get("crate"), "event crate"),
            "version": _json_string(value.get("version"), "event version"),
            "titles": _json_string_list(value.get("titles"), "event titles"),
        }
    if verb == "add":
        _require_json_keys(
            value,
            frozenset(
                {"at", "verb", "project", "title", "kind", "summary", "release", "backfill"}
            ),
            "add event",
        )
        return {
            "at": _json_string(value.get("at"), "event at"),
            "verb": "add",
            "project": _json_string(value.get("project"), "event project"),
            "title": _json_string(value.get("title"), "event title"),
            "kind": _json_string(value.get("kind"), "event kind"),
            "summary": _json_string(value.get("summary"), "event summary"),
            "release": _json_string_list(value.get("release"), "event release"),
            "backfill": _json_bool(value.get("backfill"), "event backfill"),
        }
    if verb == "supersede":
        _require_json_keys(
            value,
            frozenset(
                {
                    "at",
                    "verb",
                    "project",
                    "title",
                    "kind",
                    "summary",
                    "release",
                    "backfill",
                    "old",
                }
            ),
            "supersede event",
        )
        return {
            "at": _json_string(value.get("at"), "event at"),
            "verb": "supersede",
            "project": _json_string(value.get("project"), "event project"),
            "title": _json_string(value.get("title"), "event title"),
            "kind": _json_string(value.get("kind"), "event kind"),
            "summary": _json_string(value.get("summary"), "event summary"),
            "release": _json_string_list(value.get("release"), "event release"),
            "backfill": _json_bool(value.get("backfill"), "event backfill"),
            "old": _json_string_list(value.get("old"), "event old"),
        }
    if verb == "amend":
        legacy_keys = frozenset({"at", "verb", "project", "title", "fields", "backfill"})
        if value.keys() == legacy_keys:
            return {
                "at": _json_string(value.get("at"), "event at"),
                "verb": "amend",
                "project": _json_string(value.get("project"), "event project"),
                "title": _json_string(value.get("title"), "event title"),
                "fields": _json_string_list(value.get("fields"), "event fields"),
                "backfill": _json_bool(value.get("backfill"), "event backfill"),
            }
        _require_json_keys(
            value,
            legacy_keys | {"old_link", "new_link", "old_completed", "new_completed", "releases_added"}
            | (frozenset({"content"}) if "content" in value else frozenset[str]()),
            "amend event",
        )
        event: AmendEvent = {
            "at": _json_string(value.get("at"), "event at"),
            "verb": "amend",
            "project": _json_string(value.get("project"), "event project"),
            "title": _json_string(value.get("title"), "event title"),
            "fields": _json_string_list(value.get("fields"), "event fields"),
            "backfill": _json_bool(value.get("backfill"), "event backfill"),
            "old_link": _json_string(value.get("old_link"), "event old_link"),
            "new_link": _json_string(value.get("new_link"), "event new_link"),
            "old_completed": _json_string(value.get("old_completed"), "event old_completed"),
            "new_completed": _json_string(value.get("new_completed"), "event new_completed"),
            "releases_added": _json_string_list(value.get("releases_added"), "event releases_added"),
        }
        if "content" in value:
            event["content"] = _json_string(value["content"], "amend content")
        return event
    raise FeatureLogError(f"events.jsonl line {line_number} has unknown verb {verb}")


def _read_events() -> list[FeatureLogEvent]:
    path = state_dir() / "events.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise FeatureLogError(str(error)) from error
    return [_parse_event(line, index) for index, line in enumerate(lines, start=1)]


def _event_recorded(verb: str, project: str, title: str) -> bool:
    return any(
        event["verb"] == verb
        and event["project"] == project
        and event["verb"] != "release"
        and event["title"] == title
        for event in _read_events()
    )


def _journal_accepting_correction(event: AmendEvent) -> None:
    """Keep one journal entry when retrying an unwritten accepting correction."""
    for previous in reversed(_read_events()):
        if (
            previous["verb"] == "amend"
            and previous["project"] == event["project"]
            and previous["title"] == event["title"]
        ):
            if previous == {**event, "at": previous["at"]}:
                return
            break
    _append_event(event)


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


def _apply_add(note: Note, arguments: argparse.Namespace) -> list[str]:
    path = _note_path(note)
    backfill = cast(bool, arguments.backfill)
    should_write, stored = _prepare_new(
        note,
        cast(bool, arguments.separate_result),
        completed_date_was_defaulted=cast(str | None, arguments.completed) is None,
        replacing_titles=frozenset(),
    )
    if not should_write:
        if not _event_recorded("add", stored["project"], stored["title"]):
            _append_event(_add_event(stored, backfill))
        return [f"unchanged: {_relative(path)}"]
    _write_note(path, stored)
    _append_event(_add_event(stored, backfill))
    return [f"added: {_relative(path)}"]


def _run_add(arguments: argparse.Namespace) -> int:
    note = _new_note(arguments)
    with locked():
        outputs = _apply_add(note, arguments)
    for output in outputs:
        print(output)
    return 0


def _updated_replacement(note: Note, replacement: str) -> Note:
    updated = note.copy()
    updated["state"] = "superseded"
    updated["replaced_by"] = sorted(set([*note["replaced_by"], replacement]))
    updated["date_modified"] = today()
    return updated


def _apply_supersede(
    note: Note, arguments: argparse.Namespace, policy: ReplacementPolicy
) -> list[str]:
    old_titles = [normalize_title(item) for item in cast(list[str], arguments.old)]
    old_titles = list(dict.fromkeys(old_titles))
    if note["title"] in old_titles:
        raise FeatureLogError("--old must not equal the new title")
    new_path = _note_path(note)
    outputs: list[str] = []
    backfill = cast(bool, arguments.backfill)
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
            if isinstance(policy, ReplaceLiveNotes):
                raise FeatureLogError(f"old note {old_title} is not live")
            replacements.append(
                (old_path, _updated_replacement(old_note, note["title"]), old_title)
            )

    if write_new:
        _write_note(new_path, stored)
        outputs.append(f"added: {_relative(new_path)}")
    else:
        outputs.append(f"unchanged: {_relative(new_path)}")
    for old_path, replacement, old_title in replacements:
        _write_note(old_path, replacement)
        outputs.append(f"superseded: {old_title} → {note['title']}")
    if write_new or replacements or not _event_recorded(
        "supersede", stored["project"], stored["title"]
    ):
        _append_event(_supersede_event(stored, backfill, old_titles))
    return outputs


def _run_supersede(arguments: argparse.Namespace, policy: ReplacementPolicy) -> int:
    note = _new_note(arguments)
    with locked():
        outputs = _apply_supersede(note, arguments, policy)
    for output in outputs:
        print(output)
    return 0


def _review_row(value: str) -> str:
    if not REVIEW_ROW_RE.fullmatch(value):
        raise FeatureLogError("review row must match A<number>, C<number> or <list>/C<number>")
    return value


def _proposal_path(row: str) -> Path:
    return state_dir() / "proposals" / f"{row}.json"


def _proposal_args(note: Note, arguments: argparse.Namespace, old: list[str]) -> ProposalArgs:
    return {
        "project": note["project"],
        "title": cast(str, arguments.title),
        "kind": note["kind"],
        "summary": note["summary"],
        "behavior": note["behavior"],
        "see": note["see"],
        "link": note["link"],
        "evidence": note["evidence"],
        "crate": note["crate"],
        "area": note["area"],
        "completed": note["completed_date"],
        "production": note["production"],
        "unit": note["unit"],
        "release": note["release"],
        "backfill": cast(bool, arguments.backfill),
        "separate_result": cast(bool, arguments.separate_result),
        "old": old,
    }


def _parse_proposal_args(value: object) -> ProposalArgs:
    arguments = _json_object(value, "proposal args")
    _require_json_keys(
        arguments,
        frozenset(
            {
                "project",
                "title",
                "kind",
                "summary",
                "behavior",
                "see",
                "link",
                "evidence",
                "crate",
                "area",
                "completed",
                "production",
                "unit",
                "release",
                "backfill",
                "separate_result",
                "old",
            }
        ),
        "proposal args",
    )
    return {
        "project": _json_string(arguments.get("project"), "proposal project"),
        "title": _json_string(arguments.get("title"), "proposal title"),
        "kind": _json_string(arguments.get("kind"), "proposal kind"),
        "summary": _json_string(arguments.get("summary"), "proposal summary"),
        "behavior": _json_string(arguments.get("behavior"), "proposal behavior"),
        "see": _json_string(arguments.get("see"), "proposal see"),
        "link": _json_string(arguments.get("link"), "proposal link"),
        "evidence": _json_string_list(arguments.get("evidence"), "proposal evidence"),
        "crate": _json_string_list(arguments.get("crate"), "proposal crate"),
        "area": _json_optional_string(arguments.get("area"), "proposal area"),
        "completed": _json_string(arguments.get("completed"), "proposal completed"),
        "production": _json_optional_string(
            arguments.get("production"), "proposal production"
        ),
        "unit": _json_string_list(arguments.get("unit"), "proposal unit"),
        "release": _json_string_list(arguments.get("release"), "proposal release"),
        "backfill": _json_bool(arguments.get("backfill"), "proposal backfill"),
        "separate_result": _json_bool(
            arguments.get("separate_result"), "proposal separate_result"
        ),
        "old": _json_string_list(arguments.get("old"), "proposal old"),
    }


def _parse_proposal(value: object) -> FeatureLogProposal:
    proposal = _json_object(value, "proposal")
    status = _json_string(proposal.get("status"), "proposal status")
    verb_value = _json_string(proposal.get("verb"), "proposal verb")
    if verb_value not in {"add", "supersede"}:
        raise FeatureLogError(f"proposal has unknown verb {verb_value}")
    verb = cast(Literal["add", "supersede"], verb_value)
    row = _review_row(_json_string(proposal.get("row"), "proposal row"))
    arguments = _parse_proposal_args(proposal.get("args"))
    at = _json_string(proposal.get("at"), "proposal at")
    if status == "open":
        _require_json_keys(
            proposal,
            frozenset({"row", "verb", "args", "status", "at"}),
            "open proposal",
        )
        return {"row": row, "verb": verb, "args": arguments, "status": "open", "at": at}
    if status == "landed":
        _require_json_keys(
            proposal,
            frozenset({"row", "verb", "args", "status", "at", "note"}),
            "landed proposal",
        )
        return {
            "row": row,
            "verb": verb,
            "args": arguments,
            "status": "landed",
            "at": at,
            "note": _json_string(proposal.get("note"), "proposal note"),
        }
    if status == "dropped":
        _require_json_keys(
            proposal,
            frozenset({"row", "verb", "args", "status", "at", "verdict"}),
            "dropped proposal",
        )
        return {
            "row": row,
            "verb": verb,
            "args": arguments,
            "status": "dropped",
            "at": at,
            "verdict": _json_string(proposal.get("verdict"), "proposal verdict"),
        }
    raise FeatureLogError(f"proposal has unknown status {status}")


def _read_proposal(row: str) -> FeatureLogProposal | None:
    path = _proposal_path(row)
    if not path.exists():
        return None
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FeatureLogError(f"cannot read proposal {row}: {error}") from error
    proposal = _parse_proposal(decoded)
    if proposal["row"] != row:
        raise FeatureLogError(f"proposal {row} names row {proposal['row']}")
    return proposal


def _proposal_namespace(arguments: ProposalArgs) -> argparse.Namespace:
    return argparse.Namespace(
        project=arguments["project"],
        title=arguments["title"],
        kind=arguments["kind"],
        summary=arguments["summary"],
        behavior=arguments["behavior"],
        see=arguments["see"],
        link=arguments["link"],
        evidence=[arguments["evidence"]],
        crate=[arguments["crate"]] if arguments["crate"] else None,
        area=arguments["area"],
        completed=arguments["completed"],
        production=arguments["production"],
        unit=[arguments["unit"]] if arguments["unit"] else None,
        release=[arguments["release"]] if arguments["release"] else None,
        backfill=arguments["backfill"],
        separate_result=arguments["separate_result"],
        old=arguments["old"],
    )


def _require_new_note_arguments(arguments: argparse.Namespace) -> None:
    missing = [
        option
        for attribute, option in (
            ("project", "--project"),
            ("title", "--title"),
            ("kind", "--kind"),
            ("summary", "--summary"),
            ("behavior", "--behavior"),
            ("see", "--see"),
            ("link", "--link"),
            ("evidence", "--evidence"),
        )
        if getattr(arguments, attribute, None) is None
    ]
    if missing:
        raise FeatureLogError(f"the following arguments are required: {', '.join(missing)}")


def _proposal_old_titles(arguments: argparse.Namespace) -> list[str]:
    values = cast(list[str] | None, arguments.old)
    return list(dict.fromkeys(normalize_title(value) for value in values or []))


def _validate_proposal_old_notes(note: Note, old_titles: list[str]) -> None:
    if note["title"] in old_titles:
        raise FeatureLogError("--old must not equal the new title")
    for old_title in old_titles:
        path = vault_dir() / "feature-log" / note["project"] / f"{old_title}.md"
        if not path.exists():
            raise FeatureLogError(
                f"old note {old_title} is not a live note of {note['project']}"
            )
        old_note = parse_note(path)
        validate_note(old_note, note["project"])
        if old_note["state"] != "live":
            raise FeatureLogError(f"old note {old_title} is not live")


def _run_propose(arguments: argparse.Namespace) -> int:
    row = _review_row(cast(str, arguments.review))
    _require_new_note_arguments(arguments)
    project = cast(str, arguments.project)
    if "/" not in row and project != "hana":
        raise FeatureLogError(f"a {project} review row names its list: <list>/{row}")
    old_titles = _proposal_old_titles(arguments)
    verb: Literal["add", "supersede"] = "supersede" if old_titles else "add"
    with locked():
        existing = _read_proposal(row)
        if existing is not None and existing["status"] != "open":
            raise FeatureLogError(f"proposal {row} is already {existing['status']}")
        completed_date_was_defaulted = cast(str | None, arguments.completed) is None
        if existing is not None and completed_date_was_defaulted:
            arguments.completed = existing["args"]["completed"]
        note = _new_note(arguments)
        _write_new, stored = _prepare_new(
            note,
            cast(bool, arguments.separate_result),
            completed_date_was_defaulted=(
                completed_date_was_defaulted and existing is None
            ),
            replacing_titles=frozenset(old_titles),
        )
        _validate_proposal_old_notes(stored, old_titles)
        proposal: OpenProposal = {
            "row": row,
            "verb": verb,
            "args": _proposal_args(stored, arguments, old_titles),
            "status": "open",
            "at": _event_time(),
        }
        _write_json(_proposal_path(row), proposal)
    print(f"proposed: {row} ({verb} {stored['title']})")
    return 0


def _run_review_land(verb: Literal["add", "supersede"], row_value: str) -> int:
    row = _review_row(row_value)
    with locked():
        proposal = _read_proposal(row)
        if proposal is None:
            raise FeatureLogError(f"no proposal for {row}; run propose first")
        if proposal["verb"] != verb:
            raise FeatureLogError(
                f"proposal {row} uses {proposal['verb']}; run {proposal['verb']} --review {row}"
            )
        if proposal["status"] == "landed":
            print(f"unchanged: [[{proposal['note']}]]")
            return 0
        if proposal["status"] == "dropped":
            raise FeatureLogError(f"proposal {row} is already dropped")
        saved_arguments = _proposal_namespace(proposal["args"])
        note = _new_note(saved_arguments)
        outputs = (
            _apply_add(note, saved_arguments)
            if verb == "add"
            else _apply_supersede(note, saved_arguments, ReplaceLiveNotes())
        )
        landed: LandedProposal = {
            "row": row,
            "verb": verb,
            "args": proposal["args"],
            "status": "landed",
            "at": _event_time(),
            "note": note["title"],
        }
        _write_json(_proposal_path(row), landed)
    for output in outputs:
        print(output)
    print(f"[[{note['title']}]]")
    return 0


def _run_drop(arguments: argparse.Namespace) -> int:
    row = _review_row(cast(str, arguments.review))
    verdict = cast(str, arguments.verdict).strip()
    if not verdict or "\n" in verdict:
        raise FeatureLogError("verdict must be one non-empty line")
    with locked():
        proposal = _read_proposal(row)
        if proposal is None:
            raise FeatureLogError(f"no proposal for {row}; run propose first")
        if proposal["status"] == "dropped":
            print(f"unchanged: {row}")
            return 0
        if proposal["status"] == "landed":
            raise FeatureLogError(f"proposal {row} is already landed")
        dropped: DroppedProposal = {
            "row": row,
            "verb": proposal["verb"],
            "args": proposal["args"],
            "status": "dropped",
            "at": _event_time(),
            "verdict": verdict,
        }
        _write_json(_proposal_path(row), dropped)
    print(f"dropped: {row}")
    return 0


def _amend_provenance(
    arguments: argparse.Namespace, project: str, backfill: bool
) -> AcceptingProvenanceAmendment:
    link = cast(str | None, arguments.link)
    completed = cast(str | None, arguments.completed)
    if cast(bool, arguments.separate_result) and link is None:
        raise FeatureLogError("--separate-result requires --link")
    if link is None and completed is None:
        return KeepAcceptingProvenance()
    if not backfill:
        raise FeatureLogError("--link and --completed require --backfill")
    if link is None or completed is None:
        raise FeatureLogError("--link and --completed must be provided together")
    _validate_date(completed, "completed")
    return CorrectAcceptingProvenance(_canonical_link(project, link), completed)


def _run_amend(arguments: argparse.Namespace) -> int:
    project = cast(str, arguments.project)
    _ = _validate_project(project)
    title = normalize_title(cast(str, arguments.title))
    summary_argument = cast(str | None, arguments.summary)
    behavior_argument = cast(str | None, arguments.behavior)
    see_argument = cast(str | None, arguments.see)
    area_argument = cast(str | None, arguments.area)
    kind_argument = cast(str | None, arguments.kind)
    crate_arguments = cast(list[list[str]] | None, arguments.crate)
    evidence_arguments = cast(list[list[str]] | None, arguments.evidence)
    evidence_additions = [
        item.strip() for item in _flatten(evidence_arguments)
    ]
    backfill = cast(bool, arguments.backfill)
    provenance = _amend_provenance(arguments, project, backfill)
    release_additions = _flatten(cast(list[list[str]] | None, arguments.release))
    separate_result = cast(bool, arguments.separate_result)
    if release_additions and not backfill:
        raise FeatureLogError("--release requires --backfill")
    if any(not item or "\n" in item for item in evidence_additions):
        raise FeatureLogError("evidence is empty")
    requested_fields = [
        name
        for name, value in (
            ("summary", summary_argument),
            ("behavior", behavior_argument),
            ("see", see_argument),
            ("area", area_argument),
            ("kind", kind_argument),
            ("crate", crate_arguments),
            ("evidence", evidence_arguments),
        )
        if value is not None
    ]
    if isinstance(provenance, CorrectAcceptingProvenance):
        requested_fields.extend(("link", "completed_date"))
    if release_additions:
        requested_fields.append("release")
    if not requested_fields:
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
        if isinstance(provenance, CorrectAcceptingProvenance):
            if updated["link"] != provenance.link:
                updated["link"] = provenance.link
                changed.append("link")
            if updated["completed_date"] != provenance.completed:
                updated["completed_date"] = provenance.completed
                changed.append("completed_date")
            _reject_duplicate_link(updated, path, separate_result, frozenset())

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
        validated_releases = _validate_release_values(project, updated["crate"], release_additions)
        for release in validated_releases:
            crate, _, version = release.partition("@")
            if version != "Unreleased":
                pending_release = f"{crate}@Unreleased"
                if pending_release in note["release"]:
                    raise FeatureLogError(f"{title} already holds {pending_release}")
                continue
            if note["state"] != "live":
                raise FeatureLogError("a superseded note cannot be pending")
            for existing_release in note["release"]:
                existing_crate, _, existing_version = existing_release.partition("@")
                if existing_crate == crate and existing_version != "Unreleased":
                    raise FeatureLogError(f"{title} already holds {existing_release}")
        releases = sorted(set(updated["release"]) | set(validated_releases))
        if releases != updated["release"]:
            updated["release"] = releases
            changed.append("release")
        known_evidence = set(updated["evidence"])
        new_evidence: list[str] = []
        for item in evidence_additions:
            if item not in known_evidence:
                known_evidence.add(item)
                new_evidence.append(item)
        if new_evidence:
            updated["evidence"] = [*updated["evidence"], *new_evidence]
            changed.append("evidence")

        if not changed:
            content = hashlib.sha256(render_note(note).encode("utf-8")).hexdigest()
            previous = next((event for event in reversed(_read_events())
                             if event["verb"] == "amend" and event["project"] == note["project"]
                             and event["title"] == note["title"]), None)
            if previous is None or previous.get("content") != content:
                _append_event(_amend_event(note, note, backfill, requested_fields))
            print(f"unchanged: {_relative(path)}")
            return 0
        updated["behavior"] = updated["behavior"].strip()
        updated["see"] = updated["see"].strip()
        updated["date_modified"] = today()
        validate_note(updated, project)
        event = _amend_event(note, updated, backfill, changed)
        if (
            event["old_link"] != event["new_link"]
            or event["old_completed"] != event["new_completed"]
            or event["releases_added"]
        ):
            _journal_accepting_correction(event)
            _write_note(path, updated)
        else:
            _write_note(path, updated)
            _append_event(event)
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
        output = f"{note['completed_date']} {note['kind']} {note['title']} — {note['summary']} [{crates}]{area_text} {note['link']}"
        print(output)
    return 0


CHANGELOG_HEADINGS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
KIND_HEADING = {
    "feature": "Added",
    "fix": "Fixed",
    "change": "Changed",
    "perf": "Changed",
    "deprecation": "Deprecated",
    "removal": "Removed",
    "security": "Security",
}


def _published_project(crate: str) -> str:
    for project, spec in PROJECTS.items():
        if crate in spec.published:
            return project
    raise FeatureLogError(f"crate {crate} is not published")


def _changelog_request(arguments: argparse.Namespace) -> RenderSection | CompareSection:
    crate = cast(str, arguments.crate)
    version = cast(str, arguments.version)
    _ = _published_project(crate)
    if not VERSION_RE.fullmatch(version):
        raise FeatureLogError(f"invalid release version {version}")
    compare = cast(str | None, arguments.compare)
    save = cast(str | None, arguments.save)
    selection_json = cast(bool, arguments.selection)
    if compare is None:
        if save is not None:
            raise FeatureLogError("--save requires --compare")
        return RenderSection(crate, version, selection_json)
    if version != "Unreleased":
        raise FeatureLogError("--compare requires --version Unreleased")
    if selection_json:
        raise FeatureLogError("--selection cannot be combined with --compare")
    saving = NoSavedContents() if save is None else SaveReviewedContents(Path(save))
    return CompareSection(crate, version, Path(compare), saving)


def _select_changelog(crate: str, version: str) -> ChangelogSelection:
    project = _published_project(crate)
    with locked():
        notes = [
            note for _, note in _project_notes(project)
            if crate in note["crate"]
            and f"{crate}@{version}" in note["release"]
            and (version != "Unreleased" or note["state"] == "live")
        ]
    lines = [f"## [{version}]"]
    for heading in CHANGELOG_HEADINGS:
        summaries = sorted(
            note["summary"] for note in notes if KIND_HEADING[note["kind"]] == heading
        )
        if summaries:
            lines.extend(["", f"### {heading}", ""])
            lines.extend(f"- {summary}" for summary in summaries)
    return ChangelogSelection("\n".join(lines) + "\n", sorted(note["title"] for note in notes))


def _section_bullets(text: str, version: str) -> dict[str, list[str]]:
    """Read the requested section's bullets, including repeats and wrapped lines."""
    headings: dict[str, list[str]] = {}
    in_section = False
    found = False
    heading = ""
    bullet_parts: list[str] = []

    def finish_bullet() -> None:
        if bullet_parts:
            headings.setdefault(heading, []).append(" ".join(bullet_parts))
            bullet_parts.clear()

    for line in text.splitlines():
        section = re.match(r"^##\s+\[([^]]+)\]", line)
        if section is not None:
            if in_section:
                break
            in_section = section.group(1) == version
            found = found or in_section
            continue
        if not in_section:
            continue
        if line.startswith("## "):
            break
        category = re.match(r"^###\s+(.+?)\s*$", line)
        if category is not None:
            finish_bullet()
            heading = category.group(1)
            continue
        bullet = re.match(r"^[-*+]\s+(.+)$", line)
        if bullet is not None:
            finish_bullet()
            bullet_parts.append(bullet.group(1).strip())
        elif bullet_parts and line[:1].isspace() and line.strip():
            bullet_parts.append(line.strip())
    finish_bullet()
    if not found:
        raise FeatureLogError(f"changelog has no ## [{version}] section")
    return headings


def _run_changelog(
    request: RenderSection | CompareSection, off_host: bool, forwarded_reader: ForwardedReader
) -> int:
    if isinstance(request, CompareSection) and isinstance(request.saving, SaveReviewedContents):
        try:
            request.saving.path.unlink(missing_ok=True)
        except OSError as error:
            raise FeatureLogError(f"cannot remove saved release contents: {error}") from error
    if off_host:
        result = forwarded_reader(_ssh_command([
            "changelog", "--crate", request.crate, "--version", request.version, "--selection",
        ]))
        if isinstance(result, ForwardFailed):
            raise FeatureLogError(result.stderr or f"forwarded read failed (exit {result.exit})")
        selection = result
    else:
        selection = _select_changelog(request.crate, request.version)
    if isinstance(request, RenderSection):
        if request.selection_json:
            selection_json: ChangelogSelectionJson = {
                "section": selection.section, "titles": selection.titles,
            }
            print(json.dumps(selection_json, ensure_ascii=False))
        else:
            print(selection.section, end="")
        return 0
    try:
        changelog = request.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise FeatureLogError(f"cannot read changelog: {error}") from error
    expected = _section_bullets(selection.section, request.version)
    actual = _section_bullets(changelog, request.version)
    different = False
    for heading in (*CHANGELOG_HEADINGS, ""):
        expected_bullets = set(expected.get(heading, []))
        actual_bullets = set(actual.get(heading, []))
        missing = expected_bullets - actual_bullets
        extra = actual_bullets - expected_bullets
        if not missing and not extra:
            continue
        different = True
        print(f"### {heading}" if heading else "Uncategorized")
        if missing:
            print("Only in feature log:")
            for summary in sorted(missing):
                print(f"- {summary}")
        if extra:
            print("Only in changelog:")
            for summary in sorted(extra):
                print(f"- {summary}")
    if not different:
        if isinstance(request.saving, SaveReviewedContents):
            contents: ReviewedReleaseContents = {"crate": request.crate, "titles": selection.titles}
            try:
                _write_json(request.saving.path, contents)
            except (OSError, UnicodeError) as error:
                raise FeatureLogError(f"cannot save reviewed release contents: {error}") from error
        print("match")
    not_compared: list[str] = []
    for heading in sorted(actual.keys() - set(CHANGELOG_HEADINGS) - {""}):
        count = len(actual[heading])
        noun = "bullet" if count == 1 else "bullets"
        not_compared.append(f"{heading} ({count} {noun})")
    if not_compared:
        print(f"not compared: {', '.join(not_compared)}")
    return 1 if different else 0


def _reviewed_titles(value: object) -> list[str]:
    titles = _json_string_list(value, "reviewed titles")
    _validate_sorted_unique(titles, "reviewed titles")
    if any(normalize_title(title) != title for title in titles):
        raise FeatureLogError("reviewed titles must be normalized note titles")
    return titles


def _parse_reviewed_contents(text: str, crate: str) -> ReviewedReleaseContents:
    try:
        decoded = cast(object, json.loads(text))
    except json.JSONDecodeError as error:
        raise FeatureLogError(f"invalid reviewed release contents JSON: {error}") from error
    contents = _json_object(decoded, "reviewed release contents")
    _require_json_keys(contents, frozenset({"crate", "titles"}), "reviewed release contents")
    saved_crate = _json_string(contents.get("crate"), "reviewed crate")
    if saved_crate != crate:
        raise FeatureLogError(f"reviewed contents are for {saved_crate}, not {crate}")
    return {"crate": crate, "titles": _reviewed_titles(contents.get("titles"))}


def _release_contents(arguments: argparse.Namespace) -> ReviewedReleaseContents:
    crate = cast(str, arguments.crate)
    _ = _published_project(crate)
    version = cast(str, arguments.version)
    if version == "Unreleased" or not VERSION_RE.fullmatch(version):
        raise FeatureLogError(f"release requires an exact version, not {version}")
    try:
        if cast(bool, arguments.contents_stdin):
            text = sys.stdin.read()
        else:
            text = Path(cast(str, arguments.contents)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise FeatureLogError(f"cannot read reviewed release contents: {error}") from error
    return _parse_reviewed_contents(text, crate)


def _run_release(crate: str, version: str, contents: ReviewedReleaseContents) -> int:
    project = _published_project(crate)
    unreleased = f"{crate}@Unreleased"
    released = f"{crate}@{version}"
    reviewed = set(contents["titles"])
    with locked():
        notes = _project_notes(project)
        by_title = {note["title"]: note for _, note in notes}
        for title in contents["titles"]:
            if title not in by_title:
                raise FeatureLogError(f"reviewed note {title} does not exist")
            note = by_title[title]
            if crate not in note["crate"] or not {unreleased, released}.intersection(note["release"]):
                raise FeatureLogError(f"reviewed note {title} has no pending or stamped {crate} release")
        # A publish retry must leave all work logged after the original stamp pending.
        previous = any(
            event["verb"] == "release" and event["crate"] == crate and event["version"] == version
            for event in _read_events()
        )
        if previous:
            if any(released not in by_title[title]["release"] for title in reviewed):
                raise FeatureLogError(f"{released} was already stamped with different contents")
            print("unchanged")
            return 0
        changes: list[tuple[Path, Note]] = []
        for path, note in notes:
            if crate not in note["crate"] or unreleased not in note["release"]:
                continue
            # A superseded note absent from the reviewed contents never shipped; remove its pending marker.
            if note["title"] not in reviewed and note["state"] == "live":
                continue
            updated = note.copy()
            releases = set(note["release"]) - {unreleased}
            if note["title"] in reviewed:
                releases.add(released)
            updated["release"] = sorted(releases)
            updated["date_modified"] = today()
            validate_note(updated, project)
            changes.append((path, updated))
        for path, note in changes:
            _write_note(path, note)
        event: ReleaseEvent = {
            "at": _event_time(), "verb": "release", "project": project,
            "crate": crate, "version": version,
            "titles": sorted(note["title"] for _, note in changes),
        }
        _append_event(event)
    print(f"released {released} ({len(changes)} notes)")
    return 0


def _read_digest_cursor() -> DigestCursor | None:
    path = state_dir() / "digest.json"
    if not path.exists():
        return None
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FeatureLogError(f"cannot read digest cursor: {error}") from error
    value = _json_object(decoded, "digest cursor")
    _require_json_keys(value, frozenset({"line", "at"}), "digest cursor")
    line = _json_int(value.get("line"), "digest cursor line")
    if line < 0:
        raise FeatureLogError("digest cursor line must not be negative")
    return {
        "line": line,
        "at": _json_string(value.get("at"), "digest cursor at"),
    }


def _event_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FeatureLogError(f"event time is invalid: {value}") from error
    if parsed.tzinfo is None:
        raise FeatureLogError(f"event time has no timezone: {value}")
    return parsed.astimezone(user_zone())


def _digest_time_label(value: str) -> str:
    return _event_datetime(value).strftime("%Y-%m-%d %H:%M %Z")


def _print_digest(selected: list[FeatureLogEvent], since_label: str) -> int:
    visible = [event for event in selected if event["verb"] == "release" or not event["backfill"]]
    for project in PROJECTS:
        project_events = [event for event in visible if event["project"] == project]
        if not project_events:
            continue
        print(f"{project}:")
        for event in project_events:
            if event["verb"] == "add":
                print(f"+ {event['title']} — {event['summary']}")
            elif event["verb"] == "supersede":
                for old in event["old"]:
                    print(f"→ {old} ⇒ {event['title']}")
            elif event["verb"] == "release":
                print(f"= {event['crate']}@{event['version']} ({len(event['titles'])} notes)")
            else:
                print(f"~ {event['title']} ({', '.join(event['fields'])})")

    if not visible:
        print(f"feature log: nothing new since {since_label}")
        return 0
    print(f"{len(visible)} changes since {since_label}")
    return 0


def _run_digest(arguments: argparse.Namespace) -> int:
    since = cast(str | None, arguments.since)
    if since is not None:
        _validate_date(since, "since")
        events = _read_events()
        boundary = datetime.fromisoformat(f"{since}T00:00:00").replace(tzinfo=user_zone())
        selected = [event for event in events if _event_datetime(event["at"]) >= boundary]
        return _print_digest(selected, boundary.strftime("%Y-%m-%d %H:%M %Z"))

    with locked():
        events = _read_events()
        cursor = _read_digest_cursor()
        start = min(cursor["line"], len(events)) if cursor is not None else 0
        selected = events[start:]
        if cursor is not None:
            since_label = _digest_time_label(cursor["at"])
        elif events:
            since_label = _digest_time_label(events[0]["at"])
        else:
            since_label = _digest_time_label(_event_time())
        cursor_value: DigestCursor = {"line": len(events), "at": _event_time()}
        result = _print_digest(selected, since_label)
        _ = sys.stdout.flush()
        _write_json(state_dir() / "digest.json", cursor_value)
    return result


def _github_slug(remote: str) -> str | None:
    value = remote.strip().rstrip("/")
    for prefix in ("git@github.com:", "https://github.com/"):
        if value.startswith(prefix):
            slug = value[len(prefix) :]
            if slug.endswith(".git"):
                slug = slug[:-4]
            return slug if slug.count("/") == 1 else None
    return None


def _project_of(path: str) -> str | None:
    """Return the configured project for a checkout's origin, when known."""
    result = subprocess.run(
        ["git", "-C", path, "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    slug = _github_slug(result.stdout)
    if slug is None:
        return None
    return next((project for project, spec in PROJECTS.items() if spec.repo == slug), None)


def _run_project_of(arguments: argparse.Namespace) -> int:
    project = _project_of(cast(str, arguments.path))
    print(project or "none")
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


def _add_new_note_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    _ = parser.add_argument("--project", required=required)
    _ = parser.add_argument("--title", required=required)
    _ = parser.add_argument("--kind", required=required)
    _ = parser.add_argument("--summary", required=required)
    _ = parser.add_argument("--behavior", required=required)
    _ = parser.add_argument("--see", required=required)
    _ = parser.add_argument("--link", required=required)
    _ = parser.add_argument("--evidence", action="append", nargs="+", required=required)
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
    _ = add.add_argument("--review")
    _add_new_note_arguments(add, required=False)

    supersede = subparsers.add_parser("supersede")
    _ = supersede.add_argument("--review")
    _ = supersede.add_argument("--old", action="append")
    _ = supersede.add_argument(
        "--split", dest="replacement_policy", action="store_const",
        const=ExtendReplacementSet(), default=ReplaceLiveNotes(),
    )
    _add_new_note_arguments(supersede, required=False)

    propose = subparsers.add_parser("propose")
    _ = propose.add_argument("--review", required=True)
    _ = propose.add_argument("--old", action="append")
    _add_new_note_arguments(propose, required=True)

    drop = subparsers.add_parser("drop")
    _ = drop.add_argument("--review", required=True)
    _ = drop.add_argument("--verdict", required=True)

    amend = subparsers.add_parser("amend")
    _ = amend.add_argument("--project", required=True)
    _ = amend.add_argument("--title", required=True)
    _ = amend.add_argument("--summary")
    _ = amend.add_argument("--behavior")
    _ = amend.add_argument("--see")
    _ = amend.add_argument("--area")
    _ = amend.add_argument("--kind")
    _ = amend.add_argument("--link")
    _ = amend.add_argument("--completed")
    _ = amend.add_argument("--release", action="append", nargs="+")
    _ = amend.add_argument("--crate", action="append", nargs="+")
    _ = amend.add_argument("--evidence", action="append", nargs="+")
    _ = amend.add_argument("--backfill", action="store_true")
    _ = amend.add_argument("--separate-result", action="store_true")

    live = subparsers.add_parser("live")
    _ = live.add_argument("--project", required=True)
    _ = live.add_argument("--crate")
    _ = live.add_argument("--area")
    _ = live.add_argument("--kind")
    _ = live.add_argument("--grep")

    digest = subparsers.add_parser("digest")
    _ = digest.add_argument("--since")

    changelog = subparsers.add_parser("changelog")
    _ = changelog.add_argument("--crate", required=True)
    _ = changelog.add_argument("--version", required=True)
    _ = changelog.add_argument("--compare")
    _ = changelog.add_argument("--save")
    _ = changelog.add_argument("--selection", action="store_true", help=argparse.SUPPRESS)

    release = subparsers.add_parser("release")
    _ = release.add_argument("--crate", required=True)
    _ = release.add_argument("--version", required=True)
    contents = release.add_mutually_exclusive_group(required=True)
    _ = contents.add_argument("--contents")
    _ = contents.add_argument("--contents-stdin", action="store_true", help=argparse.SUPPRESS)

    project_of_parser = subparsers.add_parser("project-of")
    _ = project_of_parser.add_argument("path", nargs="?", default=".")

    _ = subparsers.add_parser("check")
    return parser


def main(
    argv: list[str],
    ssh_runner: SshRunner = _run_ssh,
    forwarded_reader: ForwardedReader = _read_forwarded_selection,
    release_forwarder: ReleaseForwarder = _forward_release,
) -> int:
    off_host = (
        (not argv or argv[0] != "project-of")
        and socket.gethostname().split(".", 1)[0] != "natedev"
        and "FEATURE_LOG_VAULT" not in os.environ
    )
    if off_host and (not argv or argv[0] not in {"changelog", "release"}):
        return ssh_runner(_ssh_command(argv))
    try:
        arguments = _parser().parse_args(argv)
        command = cast(str, arguments.command)
        if command == "changelog":
            return _run_changelog(_changelog_request(arguments), off_host, forwarded_reader)
        if command == "release":
            contents = _release_contents(arguments)
            crate = cast(str, arguments.crate)
            version = cast(str, arguments.version)
            if off_host:
                return release_forwarder(_ssh_command([
                    "release", "--crate", crate, "--version", version, "--contents-stdin",
                ]), contents)
            return _run_release(crate, version, contents)
        if command == "add":
            review = cast(str | None, arguments.review)
            if review is not None:
                return _run_review_land("add", review)
            _require_new_note_arguments(arguments)
            return _run_add(arguments)
        if command == "supersede":
            review = cast(str | None, arguments.review)
            policy = cast(ReplacementPolicy, arguments.replacement_policy)
            if review is not None:
                if isinstance(policy, ExtendReplacementSet):
                    raise FeatureLogError("--split cannot be combined with --review")
                return _run_review_land("supersede", review)
            _require_new_note_arguments(arguments)
            if cast(list[str] | None, arguments.old) is None:
                raise FeatureLogError("the following arguments are required: --old")
            return _run_supersede(arguments, policy)
        if command == "propose":
            return _run_propose(arguments)
        if command == "drop":
            return _run_drop(arguments)
        if command == "amend":
            return _run_amend(arguments)
        if command == "live":
            return _run_live(arguments)
        if command == "digest":
            return _run_digest(arguments)
        if command == "project-of":
            return _run_project_of(arguments)
        if command == "check":
            return _run_check()
        raise FeatureLogError(f"unknown command {command}")
    except FeatureLogError as error:
        print(f"feature_log: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
