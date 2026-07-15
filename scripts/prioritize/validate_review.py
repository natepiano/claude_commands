#!/usr/bin/env python3
"""Validate and normalize evidence-backed prioritization review JSONL."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

import renumber  # pyright: ignore[reportImplicitRelativeImport]
import review_hash  # pyright: ignore[reportImplicitRelativeImport]
import review_manifest  # pyright: ignore[reportImplicitRelativeImport]


VAULT_PATH = Path("/Users/natemccoy/rust/hanadocs")
ISSUES_PATH = VAULT_PATH / "issues"
GOALS_PATH = VAULT_PATH / "prioritization goals.md"
PRODUCTION_SCOPE = review_manifest.Scope(VAULT_PATH, ISSUES_PATH, GOALS_PATH)

Mode = Literal["reviewer", "calibrator"]
JUDGMENT_FIELDS = ("strategic_goal", *renumber.RUBRIC_FIELDS)
FINDING_FIELDS = frozenset(
    {
        "path",
        "review_hash",
        "goals_hash",
        "current",
        "verdict",
        "proposed",
        "evidence",
        "reason",
    }
)
MANIFEST_FIELDS = (
    "path",
    "review_hash",
    "goals_hash",
    "goals",
    "project",
    "category",
    "linked_evidence",
    "current",
    "status",
    "title",
    "body",
    "body_bytes",
    "body_words",
    "note_bytes",
    "review_weight",
)


class ValidationError(RuntimeError):
    """Review output does not match its immutable source manifest."""


class ProposedRubric(TypedDict):
    strategic_goal: str
    alignment: str
    impact: str
    urgency: str
    leverage: str
    confidence: str
    effort: str


class Evidence(TypedDict):
    path: str
    detail: str


class NormalizedFinding(TypedDict):
    path: str
    review_hash: str
    goals_hash: str
    current: review_manifest.CurrentRubric
    verdict: str
    proposed: ProposedRubric | dict[str, object]
    evidence: list[Evidence]
    evidence_hashes: dict[str, str]
    reason: str


@dataclass(frozen=True)
class ManifestContext:
    records: tuple[review_manifest.ManifestRecord, ...]
    live_by_path: dict[str, review_manifest.ManifestRecord]
    goals: tuple[str, ...]
    goals_hash: str


def _without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_jsonl(path: Path, *, label: str) -> tuple[dict[str, object], ...]:
    try:
        source = renumber._read_source(path)  # pyright: ignore[reportPrivateUsage]
        text = source.content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{label} is not valid UTF-8: {path}") from error
    except (OSError, renumber.PlanningError) as error:
        raise ValidationError(f"cannot read {label} {path}: {error}") from error

    records: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ValidationError(f"{label} contains a blank line at {line_number}")
        try:
            parsed: object = json.loads(  # pyright: ignore[reportAny]
                line,
                object_pairs_hook=_without_duplicate_keys,
            )
        except (json.JSONDecodeError, ValidationError) as error:
            raise ValidationError(
                f"{label} line {line_number} is not one plain JSON object: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise ValidationError(
                f"{label} line {line_number} is not one plain JSON object"
            )
        row = cast(dict[object, object], parsed)
        if not all(isinstance(key, str) for key in row):
            raise ValidationError(
                f"{label} line {line_number} contains a non-string object key"
            )
        records.append(cast(dict[str, object], parsed))
    return tuple(records)


def _manifest_context(
    manifest_path: Path,
    scope: review_manifest.Scope,
) -> ManifestContext:
    raw_records = _read_jsonl(manifest_path, label="manifest")
    try:
        live_inventory = review_manifest.build_inventory(scope)
        goals_source = renumber._read_source(  # pyright: ignore[reportPrivateUsage]
            scope.goals
        )
        goals = tuple(goal.value for goal in renumber.parse_goals(goals_source))
    except (review_manifest.ManifestError, renumber.PlanningError) as error:
        raise ValidationError(f"cannot validate the live inventory: {error}") from error

    live_by_path = {record["path"]: record for record in live_inventory}
    selected: list[review_manifest.ManifestRecord] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_records, start=1):
        manifest_fields: set[str] = set(MANIFEST_FIELDS)
        raw_fields: set[str] = set(raw)
        if raw_fields != manifest_fields:
            missing = sorted(manifest_fields - raw_fields)
            extra = sorted(raw_fields - manifest_fields)
            details: list[str] = []
            if missing:
                details.append(f"missing {missing}")
            if extra:
                details.append(f"unexpected {extra}")
            raise ValidationError(
                f"manifest row {index} has the wrong fields: {'; '.join(details)}"
            )
        path_value = raw.get("path")
        if not isinstance(path_value, str):
            raise ValidationError(f"manifest row {index} has no string path")
        if path_value in seen:
            raise ValidationError(f"manifest repeats issue path: {path_value}")
        seen.add(path_value)
        live = live_by_path.get(path_value)
        if live is None:
            raise ValidationError(
                f"manifest issue is missing, closed, or out of scope: {path_value}"
            )
        for field in MANIFEST_FIELDS:
            if raw.get(field) != live[field]:
                raise ValidationError(
                    f"manifest {field} changed for {path_value}"
                )
        if live["goals_hash"] != goals_source.digest or tuple(live["goals"]) != goals:
            raise ValidationError(f"manifest goals changed for {path_value}")
        selected.append(live)

    return ManifestContext(
        records=tuple(selected),
        live_by_path=live_by_path,
        goals=goals,
        goals_hash=goals_source.digest,
    )


def _object(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context} must be an object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValidationError(f"{context} contains a non-string key")
    return cast(dict[str, object], value)


def _optional_string(
    mapping: dict[str, object],
    field: str,
    *,
    context: str,
) -> str | None:
    value = mapping[field]
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{context}.{field} must be a string or null")
    return value


def _current_rubric(value: object, *, context: str) -> review_manifest.CurrentRubric:
    mapping = _object(value, context=context)
    if set(mapping) != set(JUDGMENT_FIELDS):
        raise ValidationError(
            f"{context} must contain exactly: {', '.join(JUDGMENT_FIELDS)}"
        )
    raw_goal = _optional_string(mapping, "strategic_goal", context=context)
    strategic_goal = (
        None if raw_goal is None else renumber.normalize_obsidian_links(raw_goal)
    )
    return review_manifest.CurrentRubric(
        strategic_goal=strategic_goal,
        alignment=_optional_string(
            mapping, "alignment", context=context
        ),
        impact=_optional_string(mapping, "impact", context=context),
        urgency=_optional_string(mapping, "urgency", context=context),
        leverage=_optional_string(mapping, "leverage", context=context),
        confidence=_optional_string(mapping, "confidence", context=context),
        effort=_optional_string(mapping, "effort", context=context),
    )


def _valid_current(
    current: review_manifest.CurrentRubric,
    goals: Sequence[str],
) -> bool:
    if current["strategic_goal"] not in goals:
        return False
    return all(current[field] in renumber.RUBRIC_DOMAINS[field] for field in renumber.RUBRIC_FIELDS)


def _proposed_rubric(
    value: object,
    *,
    context: str,
    goals: Sequence[str],
) -> ProposedRubric:
    mapping = _object(value, context=context)
    if set(mapping) != set(JUDGMENT_FIELDS):
        raise ValidationError(
            f"{context} must contain exactly: {', '.join(JUDGMENT_FIELDS)}"
        )
    for field in JUDGMENT_FIELDS:
        if not isinstance(mapping[field], str):
            raise ValidationError(f"{context}.{field} must be a string")
    strategic_goal = renumber.normalize_obsidian_links(
        cast(str, mapping["strategic_goal"])
    )
    if strategic_goal not in goals:
        raise ValidationError(
            f"{context}.strategic_goal is not an exact current goal: {strategic_goal!r}"
        )
    for field in renumber.RUBRIC_FIELDS:
        field_value = cast(str, mapping[field])
        if field_value not in renumber.RUBRIC_DOMAINS[field]:
            raise ValidationError(
                f"{context}.{field} is not an exact rubric value: {field_value!r}"
            )
    return ProposedRubric(
        strategic_goal=strategic_goal,
        alignment=cast(str, mapping["alignment"]),
        impact=cast(str, mapping["impact"]),
        urgency=cast(str, mapping["urgency"]),
        leverage=cast(str, mapping["leverage"]),
        confidence=cast(str, mapping["confidence"]),
        effort=cast(str, mapping["effort"]),
    )


def _canonical_evidence_source(
    raw_path: str,
    scope: review_manifest.Scope,
) -> renumber.SourceFile:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValidationError(f"evidence path must be absolute: {raw_path}")
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ValidationError(f"evidence is missing or unreadable: {raw_path}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValidationError(f"evidence must not be a symlink: {raw_path}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValidationError(f"evidence is missing or unreadable: {raw_path}") from error
    if candidate != resolved:
        raise ValidationError(f"evidence path must be canonical: {raw_path}")
    vault = scope.vault.resolve()
    if resolved == vault or not resolved.is_relative_to(vault):
        raise ValidationError(f"evidence is outside the vault: {raw_path}")
    if resolved.suffix != ".md":
        raise ValidationError(f"evidence is not a Markdown note: {raw_path}")
    try:
        return renumber._read_source(  # pyright: ignore[reportPrivateUsage]
            resolved
        )
    except (OSError, renumber.PlanningError) as error:
        raise ValidationError(f"cannot read evidence {raw_path}: {error}") from error


def _evidence(
    value: object,
    *,
    context: str,
    mode: Mode,
    issue: review_manifest.ManifestRecord,
    manifest: ManifestContext,
    scope: review_manifest.Scope,
) -> tuple[list[Evidence], dict[str, str]]:
    if not isinstance(value, list):
        raise ValidationError(f"{context} must be a list")
    raw_entries = cast(list[object], value)
    if not raw_entries:
        raise ValidationError(f"{context} must cite at least one vault note")
    entries: list[Evidence] = []
    hashes: dict[str, str] = {}
    issues = scope.issues.resolve()
    goals = scope.goals.resolve()
    if mode == "reviewer":
        allowed_paths = {
            issue["path"],
            str(goals),
            *issue["linked_evidence"],
        }
    else:
        allowed_paths = {str(goals), *manifest.live_by_path}
    for index, raw_entry in enumerate(raw_entries, start=1):
        entry_context = f"{context}[{index}]"
        mapping = _object(raw_entry, context=entry_context)
        if set(mapping) != {"path", "detail"}:
            raise ValidationError(
                f"{entry_context} must contain exactly path and detail"
            )
        raw_path = mapping["path"]
        detail = mapping["detail"]
        if not isinstance(raw_path, str):
            raise ValidationError(f"{entry_context}.path must be a string")
        if not isinstance(detail, str) or not detail.strip():
            raise ValidationError(f"{entry_context}.detail must be a non-empty string")
        source = _canonical_evidence_source(raw_path, scope)
        path = str(source.path)
        if path in hashes:
            raise ValidationError(f"{context} repeats evidence path: {path}")
        if path not in allowed_paths:
            raise ValidationError(
                f"{context} cites evidence outside the {mode} allowlist: {path}"
            )

        if source.path == goals:
            digest = source.digest
            if digest != manifest.goals_hash:
                raise ValidationError(f"goals evidence changed: {path}")
        elif source.path.parent == issues:
            try:
                strict_digest = review_hash.review_hash_for_source(source)
                digest = review_hash.evidence_hash_for_source(source)
            except renumber.PlanningError as error:
                raise ValidationError(
                    f"cannot hash issue evidence {path}: {error}"
                ) from error
            known = manifest.live_by_path.get(path)
            if known is not None and strict_digest != known["review_hash"]:
                raise ValidationError(f"issue evidence changed: {path}")
            if path == issue["path"] and strict_digest != issue["review_hash"]:
                raise ValidationError(f"reviewed issue evidence changed: {path}")
        else:
            digest = source.digest

        if mode == "reviewer":
            expected_digest = issue["linked_evidence"].get(path)
            if expected_digest is not None and digest != expected_digest:
                raise ValidationError(f"linked evidence changed after discovery: {path}")

        hashes[path] = digest
        entries.append(Evidence(path=path, detail=detail.strip()))
    if mode == "reviewer" and issue["path"] not in hashes:
        raise ValidationError(
            f"{context} must cite the reviewed issue itself"
        )
    return entries, hashes


def _finding(
    raw: dict[str, object],
    *,
    index: int,
    mode: Mode,
    issue: review_manifest.ManifestRecord,
    manifest: ManifestContext,
    scope: review_manifest.Scope,
) -> NormalizedFinding:
    context = f"{mode} finding {index}"
    finding_fields = set(FINDING_FIELDS)
    if set(raw) != finding_fields:
        missing = sorted(finding_fields - set(raw))
        extra = sorted(set(raw) - finding_fields)
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ValidationError(f"{context} has the wrong fields: {'; '.join(details)}")

    path = raw["path"]
    review_digest = raw["review_hash"]
    goals_digest = raw["goals_hash"]
    verdict = raw["verdict"]
    reason = raw["reason"]
    if not isinstance(path, str):
        raise ValidationError(f"{context}.path must be a string")
    if path != issue["path"]:
        raise ValidationError(
            f"{context}.path does not match manifest path {issue['path']}"
        )
    if review_digest != issue["review_hash"]:
        raise ValidationError(f"{context}.review_hash changed for {path}")
    if goals_digest != issue["goals_hash"] or goals_digest != manifest.goals_hash:
        raise ValidationError(f"{context}.goals_hash changed for {path}")
    if verdict not in {"unchanged", "proposed"}:
        raise ValidationError(f"{context}.verdict must be unchanged or proposed")
    if mode == "calibrator" and verdict != "proposed":
        raise ValidationError(f"{context}: calibrators may emit only proposed amendments")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError(f"{context}.reason must be a non-empty string")

    current = _current_rubric(raw["current"], context=f"{context}.current")
    if current != issue["current"]:
        raise ValidationError(f"{context}.current does not match the manifest")

    if verdict == "unchanged":
        proposed_object = _object(raw["proposed"], context=f"{context}.proposed")
        if proposed_object:
            raise ValidationError(f"{context}.proposed must be empty for unchanged")
        if not _valid_current(current, manifest.goals):
            raise ValidationError(
                f"{context} must propose all seven values because current is missing or invalid"
            )
        proposed: ProposedRubric | dict[str, object] = {}
    else:
        proposed = _proposed_rubric(
            raw["proposed"],
            context=f"{context}.proposed",
            goals=manifest.goals,
        )

    evidence, evidence_hashes = _evidence(
        raw["evidence"],
        context=f"{context}.evidence",
        mode=mode,
        issue=issue,
        manifest=manifest,
        scope=scope,
    )
    return NormalizedFinding(
        path=path,
        review_hash=cast(str, review_digest),
        goals_hash=cast(str, goals_digest),
        current=current,
        verdict=cast(str, verdict),
        proposed=proposed,
        evidence=evidence,
        evidence_hashes=evidence_hashes,
        reason=reason.strip(),
    )


def validate(
    mode: Mode,
    manifest_path: Path,
    findings_path: Path,
    scope: review_manifest.Scope = PRODUCTION_SCOPE,
) -> tuple[NormalizedFinding, ...]:
    """Validate findings against a live manifest and return deterministic rows."""

    manifest = _manifest_context(manifest_path, scope)
    raw_findings = _read_jsonl(findings_path, label=f"{mode} findings")
    manifest_paths = [record["path"] for record in manifest.records]

    if mode == "calibrator":
        live_paths = list(manifest.live_by_path)
        if manifest_paths != live_paths:
            raise ValidationError(
                "calibrator validation requires the complete live inventory manifest"
            )
        completion = {"status": "complete", "amendments": 0}
        if len(raw_findings) == 1 and raw_findings[0] == completion:
            return ()
        if not raw_findings:
            raise ValidationError(
                "calibrator findings must contain amendments or the explicit "
                + "no-amendments completion object"
            )

    finding_paths: list[str] = []
    raw_by_path: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(raw_findings, start=1):
        path = raw.get("path")
        if not isinstance(path, str):
            raise ValidationError(f"{mode} finding {index} has no string path")
        if path in raw_by_path:
            raise ValidationError(f"{mode} findings repeat issue path: {path}")
        finding_paths.append(path)
        raw_by_path[path] = raw

    if mode == "reviewer":
        if finding_paths != manifest_paths:
            raise ValidationError(
                "reviewer findings must cover every manifest path exactly once in manifest order"
            )
        ordered_paths = manifest_paths
    else:
        unexpected = sorted(set(finding_paths) - set(manifest_paths))
        if unexpected:
            raise ValidationError(
                f"calibrator findings contain paths outside the manifest: {unexpected}"
            )
        ordered_paths = [path for path in manifest_paths if path in raw_by_path]

    manifest_by_path = {record["path"]: record for record in manifest.records}
    normalized: list[NormalizedFinding] = []
    for index, path in enumerate(ordered_paths, start=1):
        normalized.append(
            _finding(
                raw_by_path[path],
                index=index,
                mode=mode,
                issue=manifest_by_path[path],
                manifest=manifest,
                scope=scope,
            )
        )
    return tuple(normalized)


def _jsonl(rows: Sequence[NormalizedFinding]) -> bytes:
    content = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    )
    return content.encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.prioritize-",
            dir=path.parent,
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
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def write_output(
    output_path: Path,
    rows: Sequence[NormalizedFinding],
    scope: review_manifest.Scope = PRODUCTION_SCOPE,
) -> Path:
    """Atomically write normalized rows outside the protected vault."""

    expanded = output_path.expanduser()
    if expanded.is_symlink():
        raise ValidationError(f"refusing to replace output symlink: {expanded}")
    resolved = expanded.resolve()
    vault = scope.vault.resolve()
    if resolved == vault or resolved.is_relative_to(vault):
        raise ValidationError(f"refusing to write validator output inside the vault: {resolved}")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        parent_metadata = resolved.parent.lstat()
    except OSError as error:
        raise ValidationError(f"cannot prepare output directory {resolved.parent}: {error}") from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValidationError(f"expected a real output directory: {resolved.parent}")
    if resolved.exists():
        try:
            metadata = resolved.lstat()
        except OSError as error:
            raise ValidationError(f"cannot inspect output {resolved}: {error}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"expected a regular output file: {resolved}")
    try:
        _atomic_write(resolved, _jsonl(rows))
    except OSError as error:
        raise ValidationError(f"cannot write output {resolved}: {error}") from error
    return resolved


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("reviewer", "calibrator"):
        subparser = subparsers.add_parser(mode)
        _ = subparser.add_argument("--manifest", required=True, type=Path)
        _ = subparser.add_argument("--findings", required=True, type=Path)
        _ = subparser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    raw_mode = cast(str, arguments.mode)
    if raw_mode not in {"reviewer", "calibrator"}:
        raise AssertionError(f"unsupported parser mode: {raw_mode}")
    mode = cast(Mode, raw_mode)
    manifest_path = cast(Path, arguments.manifest)
    findings_path = cast(Path, arguments.findings)
    output_path = cast(Path, arguments.output)
    try:
        rows = validate(mode, manifest_path, findings_path, PRODUCTION_SCOPE)
        destination = write_output(output_path, rows, PRODUCTION_SCOPE)
    except ValidationError as error:
        print(f"review validation error: {error}", file=sys.stderr)
        return 2
    print(f"validated {len(rows)} {mode} findings to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
