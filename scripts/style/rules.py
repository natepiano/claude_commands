#!/usr/bin/env python3
"""Load and validate global and repository style rule metadata."""

from __future__ import annotations

import argparse
import fnmatch
import json
import posixpath
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

import style_rules
from candidate_generators import CandidatesSpec, CandidatesSpecError, read_candidates_spec

Tier = Literal["A", "B", "C", "D", "E"]
Family = Literal["names", "values", "types", "modules"]
InputScope = Literal["file", "crate", "manifest"]
RuleOrigin = Literal["global", "local"]


@dataclass(frozen=True)
class NotStyleUnit:
    """The unit id does not name a style family."""


def style_family(unit: str) -> Family | NotStyleUnit:
    families: tuple[Family, ...] = ("names", "values", "types", "modules")
    for family in families:
        suffix = f"{family}-unit"
        if unit == suffix or unit.endswith("-" + suffix):
            return family
    return NotStyleUnit()


@dataclass(frozen=True)
class RepoReach:
    """The rule governs the entire repository."""


@dataclass(frozen=True)
class MemberReach:
    member: str
    crate_dir: Path


@dataclass(frozen=True)
class WorkspaceMember:
    directory: str
    package_name: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    reach: RepoReach | MemberReach
    name: str
    path: Path
    tier: Tier | Literal["unclassified"]
    family: Family | None
    input_scope: InputScope
    mode_propose: bool
    candidates: CandidatesSpec | None
    pre_filter: str | None
    blob: str
    origin: RuleOrigin


class RuleValidationError(ValueError):
    """A named rule does not meet the metadata contract."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")


def _scalar(metadata: dict[str, list[str]], key: str, path: Path, default: str = "") -> str:
    values = metadata.get(key)
    if values is None:
        return default
    if len(values) != 1 or not values[0].strip():
        raise RuleValidationError(path, f"{key} must be a nonempty scalar")
    return values[0]


def _load_rule(path: Path, origin: RuleOrigin, rule_root: Path, reach: RepoReach | MemberReach) -> Rule:
    metadata = style_rules.parse_frontmatter(path)
    try:
        candidates = read_candidates_spec(path)
    except CandidatesSpecError as error:
        raise RuleValidationError(path, str(error)) from error

    tier: Tier | Literal["unclassified"] = "unclassified"
    family: Family | None = None
    input_scope: InputScope = "crate"
    has_classification = any(key in metadata for key in ("tier", "family", "input_scope"))
    if origin == "local" or has_classification:
        declared_tier = _scalar(metadata, "tier", path)
        if declared_tier not in ("A", "B", "C", "D", "E"):
            raise RuleValidationError(path, "tier must be A, B, C, D or E")
        tier = declared_tier
        mechanism = _scalar(metadata, "mechanism", path)
        if (tier == "A") != (mechanism in ("clippy", "mend", "rustfmt")):
            raise RuleValidationError(path, "tier A requires a clippy, mend or rustfmt mechanism, and vice versa")

        declared_family = _scalar(metadata, "family", path)
        if tier in ("B", "C", "D") and not declared_family:
            raise RuleValidationError(path, "family is required for tier B, C and D")
        if tier in ("A", "E") and declared_family:
            raise RuleValidationError(path, "family is forbidden for tier A and E")
        if declared_family:
            if declared_family not in ("names", "values", "types", "modules"):
                raise RuleValidationError(path, "family must be names, values, types or modules")
            family = declared_family
        if tier == "D" and family != "modules":
            raise RuleValidationError(path, "tier D requires the modules family")

        declared_scope = _scalar(metadata, "input_scope", path, "crate")
        if declared_scope not in ("file", "crate", "manifest"):
            raise RuleValidationError(path, "input_scope must be file, crate or manifest")
        input_scope = declared_scope

        if candidates is not None and candidates.surface == "unclassified":
            raise RuleValidationError(path, "classified rule's candidates.surface must be whole or partial")

    pre_filter = style_rules.read_pre_filter(path) or None
    if tier == "B" and candidates is None and pre_filter is None:
        raise RuleValidationError(path, "tier B requires candidates or pre_filter")

    blob = subprocess.run(
        ["git", "hash-object", "--", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Rule(
        rule_id=f"{origin}:{path.relative_to(rule_root).as_posix()}",
        reach=reach,
        name=path.stem,
        path=path,
        tier=tier,
        family=family,
        input_scope=input_scope,
        mode_propose=_scalar(metadata, "mode", path) == "propose",
        candidates=candidates,
        pre_filter=pre_filter,
        blob=blob,
        origin=origin,
    )


def _table(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _workspace_match(directory: str, pattern: str) -> bool:
    def matches(parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
        if not patterns:
            return not parts
        if patterns[0] == "**":
            return matches(parts, patterns[1:]) or bool(parts) and matches(parts[1:], patterns)
        return bool(parts) and fnmatch.fnmatchcase(parts[0], patterns[0]) and matches(parts[1:], patterns[1:])

    return matches(PurePosixPath(directory).parts, PurePosixPath(pattern).parts)


def workspace_members(manifests: dict[str, str]) -> tuple[WorkspaceMember, ...]:
    """Resolve explicit members and their recursive in-tree path dependencies.

    Manifest keys are repository-relative paths. Supplying text keeps working
    tree rule discovery and committed ledger snapshots on the same resolver.
    """
    root = _table(tomllib.loads(manifests.get("Cargo.toml", "")))
    workspace = _table(root.get("workspace"))
    patterns = tuple(value for value in cast(list[object], workspace.get("members", [])) if isinstance(value, str))
    exclusions = tuple(value for value in cast(list[object], workspace.get("exclude", [])) if isinstance(value, str))
    available = {str(PurePosixPath(path).parent): path for path in manifests}

    def excluded(directory: str) -> bool:
        parents = (directory, *(str(parent) for parent in PurePosixPath(directory).parents))
        return any(_workspace_match(parent, pattern) for parent in parents for pattern in exclusions)

    pending = [directory for directory in available if directory == "." or any(
        _workspace_match(directory, pattern) for pattern in patterns
    )]
    visited: set[str] = set()
    members: list[WorkspaceMember] = []
    inherited = _table(workspace.get("dependencies"))
    while pending:
        directory = pending.pop()
        if directory in visited or excluded(directory):
            continue
        visited.add(directory)
        manifest = _table(tomllib.loads(manifests[available[directory]]))
        name = _table(manifest.get("package")).get("name")
        if not isinstance(name, str):
            continue
        members.append(WorkspaceMember(directory, name))
        dependency_tables = [manifest, *(_table(target) for target in _table(manifest.get("target")).values())]
        for table in dependency_tables:
            for kind in ("dependencies", "dev-dependencies", "build-dependencies"):
                for dependency_name, value in _table(table.get(kind)).items():
                    dependency = _table(value)
                    base = directory
                    if dependency.get("workspace") is True:
                        dependency = _table(inherited.get(dependency_name))
                        base = "."
                    path = dependency.get("path")
                    if not isinstance(path, str) or PurePosixPath(path).is_absolute():
                        continue
                    dependency_dir = posixpath.normpath(posixpath.join(base, path))
                    if dependency_dir == ".." or dependency_dir.startswith("../"):
                        continue
                    if dependency_dir in available:
                        pending.append(dependency_dir)
    return tuple(sorted(members, key=lambda member: (-len(PurePosixPath(member.directory).parts), member.directory)))


def _member_reach(repo_root: Path, member: str, rule_path: Path) -> MemberReach:
    manifests = {path.relative_to(repo_root).as_posix(): path.read_text() for path in repo_root.rglob("Cargo.toml") if path.is_file()}
    for candidate in workspace_members(manifests):
        if (repo_root / candidate.directory).resolve().name == member or candidate.package_name == member:
            return MemberReach(member, Path(candidate.directory))
    raise RuleValidationError(rule_path, f"member-local rule names no workspace member {member}")


def load_rules(repo_root: Path | None) -> tuple[Rule, ...]:
    """Read every global rule, then root and member rules in a repository.

    Global rules predating classification metadata remain unclassified.
    Local rules and any declared classification metadata are validated.
    Use the loader's directory layout without its context-loading filters:
    this registry includes tool-enforced and Bevy rules for every repository.
    """
    global_root = style_rules.NATE_STYLE_DIR.resolve()
    global_dir = global_root / "rust"
    rules = [
        _load_rule(path.resolve(), "global", global_root, RepoReach())
        for path in sorted(global_dir.glob("*.md")) if path.is_file()
    ]
    if repo_root is not None:
        repo_root = repo_root.expanduser().resolve()
        docs = repo_root / "docs"
        directories = [docs / "style", *sorted(docs.glob("*/style"))]
        for directory in directories:
            rules.extend(
                _load_rule(
                    path.resolve(), "local", repo_root,
                    RepoReach() if directory == docs / "style"
                    else _member_reach(repo_root, directory.parent.name, path),
                )
                for path in sorted(directory.glob("*.md")) if path.is_file()
            )
    return tuple(rules)


class RuleListing(TypedDict):
    rule_id: str
    name: str
    tier: Tier | Literal["unclassified"]
    family: Family | None
    input_scope: InputScope
    surface: Literal["whole", "partial", "unclassified"] | None


class ListArguments(argparse.Namespace):
    repo: str = ""
    json: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list", help="List rule tiers, families, scopes and generator surfaces.")
    _ = listing.add_argument("--repo", default="", help="Include this repository's local rules.")
    _ = listing.add_argument("--json", action="store_true", help="Emit a JSON array.")
    args = parser.parse_args(namespace=ListArguments())
    try:
        rules = load_rules(Path(args.repo) if args.repo else None)
    except (RuleValidationError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"rules: {error}\n")
    rows: list[RuleListing] = [
        {
            "rule_id": rule.rule_id,
            "name": rule.name,
            "tier": rule.tier,
            "family": rule.family,
            "input_scope": rule.input_scope,
            "surface": rule.candidates.surface if rule.candidates else None,
        }
        for rule in rules
    ]
    if args.json:
        print(json.dumps(rows, sort_keys=True))
    else:
        print("rule_id\tname\ttier\tfamily\tinput_scope\tsurface")
        for row in rows:
            print(f"{row['rule_id']}\t{row['name']}\t{row['tier']}\t{row['family'] or '-'}\t{row['input_scope']}\t{row['surface'] or '-'}")


if __name__ == "__main__":
    main()
