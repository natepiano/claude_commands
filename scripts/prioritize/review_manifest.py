#!/usr/bin/env python3
"""Build deterministic review manifests for the open Hanadocs backlog."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import unquote, urlsplit

import renumber  # pyright: ignore[reportImplicitRelativeImport]
import review_hash  # pyright: ignore[reportImplicitRelativeImport]


VAULT_PATH = Path("/Users/natemccoy/rust/hanadocs")
ISSUES_PATH = VAULT_PATH / "issues"
GOALS_PATH = VAULT_PATH / "prioritization goals.md"

RUBRIC_FIELDS = (
    "strategic_goal",
    "alignment",
    "impact",
    "urgency",
    "effort",
)
TOP_LEVEL_FIELD_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<raw>.*?)(?:\r?\n)?$"
)
LIST_ITEM_RE = re.compile(r"^\s+-\s*(?P<raw>.*?)\s*$")
GOAL_LINE_RE = re.compile(r"^(?P<ordinal>[1-9]\d*)\.\s+`(?P<value>[^`]+)`\s*$")
GOAL_VALUE_RE = re.compile(r"^(?P<prefix>[1-9]\d*) - \S(?:.*\S)?$")
TITLE_RE = re.compile(r"^\s*#\s+(?P<title>.+?)\s*$")
SHARD_FILE_RE = re.compile(r"^shard_[1-9]\d*\.jsonl$")
WIKILINK_RE = re.compile(r"!?\[\[(?P<target>[^\]\n]+)\]\]")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\((?P<target>[^)\n]+)\)")
MARKDOWN_TITLE_RE = re.compile(
    r'''\s+(?:"[^"]*"|'[^']*'|\([^)]*\))\s*$'''
)


class ManifestError(RuntimeError):
    """The review manifest cannot be generated safely."""


@dataclass(frozen=True)
class Scope:
    vault: Path
    issues: Path
    goals: Path


PRODUCTION_SCOPE = Scope(VAULT_PATH, ISSUES_PATH, GOALS_PATH)


@dataclass(frozen=True)
class FieldOccurrence:
    index: int
    raw: str
    continuation: tuple[str, ...]


@dataclass(frozen=True)
class ParsedNote:
    lines: tuple[str, ...]
    closing_index: int
    fields: dict[str, tuple[FieldOccurrence, ...]]
    body: str


class CurrentRubric(TypedDict):
    strategic_goal: str | None
    alignment: str | None
    impact: str | None
    urgency: str | None
    effort: str | None


class ManifestRecord(TypedDict):
    path: str
    review_hash: str
    goals_hash: str
    goals: list[str]
    project: list[str]
    category: list[str]
    linked_evidence: dict[str, str]
    current: CurrentRubric
    status: str
    title: str
    body: str
    body_bytes: int
    body_words: int
    note_bytes: int
    review_weight: int


def _source_text(source: renumber.SourceFile) -> str:
    try:
        return source.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ManifestError(f"{source.path} is not valid UTF-8: {error}") from error


def _safe_markdown_note(path: Path, scope: Scope) -> Path | None:
    vault_input = Path(os.path.abspath(scope.vault))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(vault_input)
    except ValueError:
        return None
    if not relative.parts:
        return None

    cursor = scope.vault
    metadata: os.stat_result | None = None
    for part in relative.parts:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(metadata.st_mode):
            return None
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        return None

    try:
        vault = scope.vault.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(vault) or resolved.suffix != ".md":
        return None
    return resolved


def _note_index(scope: Scope) -> dict[str, tuple[Path, ...]]:
    try:
        candidates = tuple(sorted(scope.vault.rglob("*.md"), key=lambda path: str(path)))
    except OSError as error:
        raise ManifestError(f"cannot enumerate Markdown notes in {scope.vault}: {error}") from error

    buckets: dict[str, list[Path]] = {}
    for candidate in candidates:
        resolved = _safe_markdown_note(candidate, scope)
        if resolved is None:
            continue
        for key in (resolved.name, resolved.stem):
            paths = buckets.setdefault(key, [])
            if resolved not in paths:
                paths.append(resolved)
    return {
        key: tuple(sorted(paths, key=lambda path: str(path)))
        for key, paths in buckets.items()
    }


def _resolve_note_target(
    target: str,
    *,
    source: renumber.SourceFile,
    scope: Scope,
    note_index: dict[str, tuple[Path, ...]],
    require_md_suffix: bool,
) -> Path | None:
    value = target.strip()
    if not value or "\x00" in value:
        return None
    target_path = Path(value)
    if target_path.is_absolute():
        return None
    if require_md_suffix:
        if target_path.suffix != ".md":
            return None
    elif target_path.suffix != ".md":
        target_path = Path(f"{value}.md")

    matches: set[Path] = set()
    for candidate in (
        source.path.parent / target_path,
        scope.vault / target_path,
    ):
        resolved = _safe_markdown_note(candidate, scope)
        if resolved is not None:
            matches.add(resolved)
    if len(target_path.parts) == 1:
        matches.update(note_index.get(target_path.name, ()))
    return next(iter(matches)) if len(matches) == 1 else None


def _wikilink_target(raw: str) -> str | None:
    target = raw.split("|", maxsplit=1)[0].strip()
    target = re.split(r"[#^]", target, maxsplit=1)[0].strip()
    return unquote(target) if target else None


def _markdown_target(raw: str) -> str | None:
    destination = raw.strip()
    if destination.startswith("<"):
        closing = destination.find(">")
        if closing < 0:
            return None
        destination = destination[1:closing]
    else:
        destination = MARKDOWN_TITLE_RE.sub("", destination).strip()
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc:
        return None
    target = unquote(parsed.path)
    return target if target else None


def _evidence_digest(path: Path, scope: Scope) -> str:
    try:
        source = renumber._read_source(  # pyright: ignore[reportPrivateUsage]
            path
        )
        if path.parent == scope.issues.resolve():
            return review_hash.evidence_hash_for_source(source)
        return source.digest
    except renumber.PlanningError as error:
        raise ManifestError(f"cannot hash linked evidence {path}: {error}") from error


def _linked_evidence(
    source: renumber.SourceFile,
    content: str,
    scope: Scope,
    note_index: dict[str, tuple[Path, ...]],
    digest_cache: dict[Path, str],
) -> dict[str, str]:
    resolved: set[Path] = set()
    for match in WIKILINK_RE.finditer(content):
        target = _wikilink_target(match.group("target"))
        if target is None:
            continue
        note = _resolve_note_target(
            target,
            source=source,
            scope=scope,
            note_index=note_index,
            require_md_suffix=False,
        )
        if note is not None:
            resolved.add(note)
    for match in MARKDOWN_LINK_RE.finditer(content):
        target = _markdown_target(match.group("target"))
        if target is None:
            continue
        note = _resolve_note_target(
            target,
            source=source,
            scope=scope,
            note_index=note_index,
            require_md_suffix=True,
        )
        if note is not None:
            resolved.add(note)
    result: dict[str, str] = {}
    for path in sorted(resolved, key=lambda candidate: str(candidate)):
        digest = digest_cache.get(path)
        if digest is None:
            digest = _evidence_digest(path, scope)
            digest_cache[path] = digest
        result[str(path)] = digest
    return result


def parse_note(content: str, path: Path) -> ParsedNote:
    lines = tuple(content.splitlines(keepends=True))
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ManifestError(f"{path} does not begin with YAML frontmatter")

    closing_index = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            closing_index = index
            break
    if closing_index < 0:
        raise ManifestError(f"{path} has no closing frontmatter delimiter")

    top_level: list[tuple[int, str, str]] = []
    for index in range(1, closing_index):
        line = lines[index]
        if line[:1].isspace():
            continue
        match = TOP_LEVEL_FIELD_RE.fullmatch(line)
        if match is not None:
            top_level.append((index, match.group("key"), match.group("raw").strip()))

    fields: dict[str, list[FieldOccurrence]] = {}
    for position, (index, key, raw) in enumerate(top_level):
        next_index = (
            top_level[position + 1][0]
            if position + 1 < len(top_level)
            else closing_index
        )
        fields.setdefault(key, []).append(
            FieldOccurrence(
                index=index,
                raw=raw,
                continuation=lines[index + 1 : next_index],
            )
        )

    return ParsedNote(
        lines=lines,
        closing_index=closing_index,
        fields={key: tuple(values) for key, values in fields.items()},
        body="".join(lines[closing_index + 1 :]),
    )


def _one_field(note: ParsedNote, key: str, path: Path) -> FieldOccurrence | None:
    occurrences = note.fields.get(key, ())
    if len(occurrences) > 1:
        raise ManifestError(f"{path}: duplicate {key} properties")
    return occurrences[0] if occurrences else None


def _strip_unquoted_comment(value: str) -> str:
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _parse_scalar(raw: str, *, context: str) -> str | None:
    value = raw.strip()
    if not value or value in {"~", "null", "Null", "NULL"}:
        return None
    if value.startswith('"'):
        try:
            decoded: object = json.loads(value)  # pyright: ignore[reportAny]
        except json.JSONDecodeError as error:
            raise ManifestError(f"{context}: malformed double-quoted scalar") from error
        if not isinstance(decoded, str):
            raise ManifestError(f"{context}: expected a string scalar")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ManifestError(f"{context}: malformed single-quoted scalar")
        return value[1:-1].replace("''", "'")
    return _strip_unquoted_comment(value)


def _parse_inline_list(raw: str, *, context: str) -> list[str]:
    inner = raw[1:-1].strip()
    if not inner:
        return []
    try:
        parsed: object = json.loads(raw)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        try:
            values = next(csv.reader([inner], skipinitialspace=True))
        except (csv.Error, StopIteration) as error:
            raise ManifestError(f"{context}: malformed inline list") from error
        result: list[str] = []
        for value in values:
            parsed_value = _parse_scalar(value, context=context)
            if parsed_value is None:
                raise ManifestError(f"{context}: list entries must be strings")
            result.append(parsed_value)
        return result
    if not isinstance(parsed, list):
        raise ManifestError(f"{context}: expected a string list")
    parsed_items = cast(list[object], parsed)
    if not all(isinstance(item, str) for item in parsed_items):
        raise ManifestError(f"{context}: expected a string list")
    return [item for item in parsed_items if isinstance(item, str)]


def _parse_string_list(
    occurrence: FieldOccurrence | None,
    *,
    context: str,
) -> list[str]:
    if occurrence is None:
        return []
    if occurrence.raw:
        if occurrence.raw.startswith("[") and occurrence.raw.endswith("]"):
            return _parse_inline_list(occurrence.raw, context=context)
        value = _parse_scalar(occurrence.raw, context=context)
        return [] if value is None else [value]

    values: list[str] = []
    for line in occurrence.continuation:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LIST_ITEM_RE.fullmatch(line.rstrip("\r\n"))
        if match is None:
            raise ManifestError(f"{context}: expected a block string list")
        value = _parse_scalar(match.group("raw"), context=context)
        if value is None:
            raise ManifestError(f"{context}: list entries must be strings")
        values.append(value)
    return values


def parse_goals(content: str, path: Path) -> tuple[str, ...]:
    in_goals = False
    saw_heading = False
    goals: list[str] = []
    for line in content.splitlines():
        if line.strip() == "## Current goals":
            if saw_heading:
                raise ManifestError(f"{path} repeats the Current goals heading")
            saw_heading = True
            in_goals = True
            continue
        if in_goals and line.startswith("##"):
            break
        if not in_goals:
            continue
        match = GOAL_LINE_RE.fullmatch(line.strip())
        if match is None:
            if re.match(r"^[1-9]\d*\.\s+", line.strip()):
                raise ManifestError(f"{path}: malformed ordered goal {line.strip()!r}")
            continue
        ordinal = int(match.group("ordinal"))
        expected = len(goals) + 1
        if ordinal != expected:
            raise ManifestError(f"{path}: goal ordinals must be contiguous from 1")
        value = renumber.normalize_obsidian_links(match.group("value"))
        value_match = GOAL_VALUE_RE.fullmatch(value)
        if value_match is None or int(value_match.group("prefix")) != ordinal:
            raise ManifestError(f"{path}: goal {ordinal} must begin with {ordinal} - ")
        goals.append(value)
    if not saw_heading:
        raise ManifestError(f"{path} has no '## Current goals' heading")
    if not goals:
        raise ManifestError(f"{path} defines no current goals")
    if len(set(goals)) != len(goals):
        raise ManifestError(f"{path} contains duplicate current goals")
    return tuple(goals)


def _optional_scalar(note: ParsedNote, key: str, path: Path) -> str | None:
    occurrence = _one_field(note, key, path)
    if occurrence is None:
        return None
    for line in occurrence.continuation:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            raise ManifestError(f"{path}: {key} must be a scalar")
    return _parse_scalar(occurrence.raw, context=f"{path}: {key}")


def _optional_domain(note: ParsedNote, key: str, path: Path) -> str | None:
    occurrences = note.fields.get(key, ())
    if len(occurrences) != 1:
        return None
    occurrence = occurrences[0]
    if any(
        line.strip() and not line.strip().startswith("#")
        for line in occurrence.continuation
    ):
        return None
    try:
        return _parse_scalar(occurrence.raw, context=f"{path}: {key}")
    except ManifestError:
        return None


def _optional_goal(note: ParsedNote, path: Path) -> str | None:
    value = _optional_domain(note, "strategic_goal", path)
    return None if value is None else renumber.normalize_obsidian_links(value)


def _title(body: str, path: Path) -> str:
    for line in body.splitlines():
        match = TITLE_RE.fullmatch(line)
        if match is not None:
            return match.group("title")
    return path.stem


def _record(
    source: renumber.SourceFile,
    goals: tuple[str, ...],
    goals_hash: str,
    scope: Scope,
    note_index: dict[str, tuple[Path, ...]],
    evidence_digest_cache: dict[Path, str],
) -> ManifestRecord | None:
    path = source.path
    content = _source_text(source)
    note = parse_note(content, path)
    status = _optional_scalar(note, "status", path)
    if status not in {"open", "closed"}:
        raise ManifestError(f"{path}: unsupported or missing status {status!r}")
    if status == "closed":
        return None

    current = CurrentRubric(
        strategic_goal=_optional_goal(note, path),
        alignment=_optional_domain(note, "alignment", path),
        impact=_optional_domain(note, "impact", path),
        urgency=_optional_domain(note, "urgency", path),
        effort=_optional_domain(note, "effort", path),
    )
    body_bytes = len(note.body.encode("utf-8"))
    note_bytes = len(review_hash.canonical_review_bytes(source))
    return ManifestRecord(
        path=str(path.resolve()),
        review_hash=review_hash.review_hash_for_source(source),
        goals_hash=goals_hash,
        goals=list(goals),
        project=_parse_string_list(
            _one_field(note, "project", path), context=f"{path}: project"
        ),
        category=_parse_string_list(
            _one_field(note, "category", path), context=f"{path}: category"
        ),
        linked_evidence=_linked_evidence(
            source,
            content,
            scope,
            note_index,
            evidence_digest_cache,
        ),
        current=current,
        status="open",
        title=_title(note.body, path),
        body=note.body,
        body_bytes=body_bytes,
        body_words=len(re.findall(r"\S+", note.body)),
        note_bytes=note_bytes,
        review_weight=max(1, note_bytes + body_bytes),
    )


def _issue_paths(scope: Scope) -> tuple[Path, ...]:
    try:
        metadata = scope.issues.lstat()
    except OSError as error:
        raise ManifestError(f"cannot inspect {scope.issues}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ManifestError(f"refusing to follow symlink: {scope.issues}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError(f"expected an issue directory: {scope.issues}")
    try:
        return tuple(sorted(scope.issues.glob("*.md"), key=lambda path: str(path)))
    except OSError as error:
        raise ManifestError(f"cannot enumerate {scope.issues}: {error}") from error


def build_inventory(scope: Scope = PRODUCTION_SCOPE) -> tuple[ManifestRecord, ...]:
    goals_source = renumber._read_source(  # pyright: ignore[reportPrivateUsage]
        scope.goals
    )
    goals = parse_goals(_source_text(goals_source), scope.goals)
    issue_paths = _issue_paths(scope)
    note_index = _note_index(scope)
    evidence_digest_cache: dict[Path, str] = {}
    records: list[ManifestRecord] = []
    for path in issue_paths:
        source = renumber._read_source(path)  # pyright: ignore[reportPrivateUsage]
        record = _record(
            source,
            goals,
            goals_source.digest,
            scope,
            note_index,
            evidence_digest_cache,
        )
        if record is not None:
            records.append(record)
    if _issue_paths(scope) != issue_paths:
        raise ManifestError("issue files changed while inventory was being built")
    current_goals = renumber._read_source(  # pyright: ignore[reportPrivateUsage]
        scope.goals
    )
    if (
        current_goals.digest != goals_source.digest
        or current_goals.signature != goals_source.signature
    ):
        raise ManifestError("strategic goals changed while inventory was being built")
    for path, expected_digest in evidence_digest_cache.items():
        if _evidence_digest(path, scope) != expected_digest:
            raise ManifestError(f"linked evidence changed while inventory was built: {path}")
    records.sort(key=lambda record: record["path"])
    paths = [record["path"] for record in records]
    if len(paths) != len(set(paths)):
        raise ManifestError("inventory contains duplicate issue paths")
    return tuple(records)


def shard_inventory(
    records: Sequence[ManifestRecord], shard_count: int
) -> tuple[tuple[ManifestRecord, ...], ...]:
    if shard_count < 1:
        raise ManifestError("shard count must be at least 1")
    shards: list[list[ManifestRecord]] = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    ordered = sorted(
        records,
        key=lambda record: (-record["review_weight"], record["path"]),
    )
    for record in ordered:
        index = min(
            range(shard_count),
            key=lambda candidate: (loads[candidate], len(shards[candidate]), candidate),
        )
        shards[index].append(record)
        loads[index] += record["review_weight"]
    for shard in shards:
        shard.sort(key=lambda record: record["path"])
    result = tuple(tuple(shard) for shard in shards)
    verify_shard_union(records, result)
    return result


def verify_shard_union(
    inventory: Sequence[ManifestRecord],
    shards: Sequence[Sequence[ManifestRecord]],
) -> None:
    expected = {record["path"]: record for record in inventory}
    if len(expected) != len(inventory):
        raise ManifestError("inventory paths are not unique")
    actual: dict[str, ManifestRecord] = {}
    for shard in shards:
        for record in shard:
            path = record["path"]
            if path in actual:
                raise ManifestError(f"issue appears in more than one shard: {path}")
            actual[path] = record
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        unexpected = sorted(actual.keys() - expected.keys())
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if unexpected:
            details.append(f"unexpected {unexpected}")
        if not details:
            details.append("record contents differ")
        raise ManifestError("shard union differs from inventory: " + "; ".join(details))


def _jsonl(records: Sequence[ManifestRecord]) -> bytes:
    text = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    )
    return text.encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.prioritize-", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as temporary:
            descriptor = None
            _ = temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if descriptor is not None:
            _ = os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _prepare_session_dir(session_dir: Path, scope: Scope) -> Path:
    resolved = session_dir.expanduser().resolve()
    vault = scope.vault.resolve()
    if resolved == vault or resolved.is_relative_to(vault):
        raise ManifestError(f"refusing to write session output inside the vault: {resolved}")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise ManifestError(f"cannot prepare session directory {resolved}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError(f"expected a real session directory: {resolved}")
    return resolved


def write_outputs(
    session_dir: Path,
    inventory: Sequence[ManifestRecord],
    shards: Sequence[Sequence[ManifestRecord]],
    scope: Scope = PRODUCTION_SCOPE,
) -> tuple[Path, ...]:
    verify_shard_union(inventory, shards)
    directory = _prepare_session_dir(session_dir, scope)
    outputs = [(directory / "inventory.jsonl", _jsonl(inventory))]
    outputs.extend(
        (directory / f"shard_{index}.jsonl", _jsonl(shard))
        for index, shard in enumerate(shards, start=1)
    )
    expected_names = {path.name for path, _content in outputs}
    for path, content in outputs:
        _atomic_write(path, content)
    for path in directory.iterdir():
        if SHARD_FILE_RE.fullmatch(path.name) and path.name not in expected_names:
            _ = path.unlink()
    return tuple(path for path, _content in outputs)


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--session-dir",
        required=True,
        type=Path,
        help="write inventory.jsonl and shard JSONL files in this directory",
    )
    _ = parser.add_argument(
        "--shards",
        type=_positive_integer,
        default=4,
        help="number of deterministically balanced shards (default: 4)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    session_dir = cast(Path, arguments.session_dir)
    shard_count = cast(int, arguments.shards)
    try:
        inventory = build_inventory(PRODUCTION_SCOPE)
        effective_shard_count = min(shard_count, len(inventory))
        shards = (
            shard_inventory(inventory, effective_shard_count)
            if effective_shard_count
            else ()
        )
        outputs = write_outputs(session_dir, inventory, shards, PRODUCTION_SCOPE)
    except (ManifestError, OSError) as error:
        print(f"review manifest error: {error}", file=sys.stderr)
        return 2
    destination = outputs[0].parent
    print(
        "wrote {} open issues across {} shards to {}".format(
            len(inventory), len(shards), destination
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
