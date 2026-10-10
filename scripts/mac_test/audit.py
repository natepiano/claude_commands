#!/usr/bin/env python3
"""Report operating-system conditions in a Rust workspace."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass
import glob
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Literal, cast


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
import sweep

import offload


ListingStatus = Literal["yes", "no", "unknown"]
Suggestion = Literal["keep on natedev", "needs review", "can go to the Mac"]
PredicateEffect = Literal[
    "linux_only",
    "same_on_linux_and_macos",
    "macos_only",
    "unclassified",
]


@dataclass(frozen=True)
class LinuxOnlySite:
    """A code condition whose contents are omitted on the Mac."""

    path: Path
    line: int


@dataclass(frozen=True)
class MemberAudit:
    """Operating-system conditions found for one workspace member."""

    package: str
    source_sites: int
    test_sites: int
    target_dependencies: tuple[str, ...]
    listing: ListingStatus
    suggestion: Suggestion
    unclassified_predicates: tuple[str, ...]
    linux_only_sites: tuple[LinuxOnlySite, ...]

    def site_count(self) -> int:
        return self.source_sites + self.test_sites + len(self.target_dependencies)


@dataclass(frozen=True)
class PredicateOccurrence:
    """A cfg predicate and its position in a source file."""

    predicate: str
    opening_line: int
    expression_start: int
    expression_end: int


@dataclass(frozen=True)
class CodeConditions:
    """Operating-system conditions found in source-like files."""

    site_count: int
    predicates: tuple[str, ...]
    linux_only_sites: tuple[LinuxOnlySite, ...]


class AuditArguments(argparse.Namespace):
    """Arguments accepted by the workspace audit."""

    def __init__(self) -> None:
        super().__init__()
        self.repo_dir: str = ""
        self.check: bool = False


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    _ = root.add_argument("repo_dir")
    _ = root.add_argument("--check", action="store_true")
    return root


def toml_table(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        decoded = cast(object, tomllib.load(handle))
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} does not contain a TOML table")
    return cast(dict[str, object], decoded)


def nested_table(values: dict[str, object], key: str) -> dict[str, object]:
    value = values.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def string_sequence(values: dict[str, object], key: str) -> tuple[str, ...]:
    value = values.get(key)
    if not isinstance(value, list):
        return ()
    items = cast(list[object], value)
    return tuple(item for item in items if isinstance(item, str))


def within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def expanded_directories(root: Path, patterns: tuple[str, ...]) -> set[Path]:
    directories: set[Path] = set()
    for pattern in patterns:
        for raw_path in glob.glob(str(root / pattern)):
            path = Path(raw_path).resolve()
            if within(root, path) and path.is_dir():
                directories.add(path)
    return directories


def member_directories(root: Path, manifest: dict[str, object]) -> tuple[Path, ...]:
    workspace = nested_table(manifest, "workspace")
    members = expanded_directories(root, string_sequence(workspace, "members"))
    excluded = expanded_directories(root, string_sequence(workspace, "exclude"))
    members.difference_update(excluded)
    if nested_table(manifest, "package"):
        members.add(root)
    return tuple(sorted(members))


def predicate_occurrences(text: str) -> tuple[PredicateOccurrence, ...]:
    line_starts = [0]
    line_starts.extend(
        index + 1 for index, character in enumerate(text) if character == "\n"
    )
    occurrences: list[PredicateOccurrence] = []
    pattern = re.compile(r"\bcfg_attr\s*\(|\bcfg\s*!?\s*\(")
    cursor = 0
    while match := pattern.search(text, cursor):
        start = match.end()
        depth = 1
        quote = ""
        escaped = False
        predicate_end = -1
        for index in range(start, len(text)):
            character = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
                continue
            if character in {'"', "'"}:
                quote = character
            elif character == "(":
                depth += 1
            elif (
                character == ","
                and depth == 1
                and match.group(0).startswith("cfg_attr")
                and predicate_end == -1
            ):
                predicate_end = index
            elif character == ")":
                depth -= 1
                if depth == 0:
                    if predicate_end == -1:
                        predicate_end = index
                    occurrences.append(
                        PredicateOccurrence(
                            normalized_predicate(text[start:predicate_end]),
                            bisect_right(line_starts, match.start()),
                            start,
                            predicate_end,
                        )
                    )
                    cursor = index + 1
                    break
        else:
            cursor = match.end()
    return tuple(occurrences)


def is_operating_system_predicate(predicate: str) -> bool:
    return re.search(
        r"\btarget_(?:os|family)\s*=|\b(?:unix|windows)\b", predicate
    ) is not None


def normalized_predicate(predicate: str) -> str:
    return re.sub(r"\s+", " ", predicate.strip())


def predicate_effect(predicate: str) -> PredicateEffect:
    compact = re.sub(r"\s+", "", predicate)
    if compact in {'target_os="linux"', 'not(target_os="macos")'}:
        return "linux_only"
    if compact in {
        "unix",
        "windows",
        "not(unix)",
        "not(windows)",
        'target_family="unix"',
    }:
        return "same_on_linux_and_macos"
    if compact == 'target_os="macos"':
        return "macos_only"
    return "unclassified"


def source_files(member: Path, directory: str) -> tuple[Path, ...]:
    root = member / directory
    if not root.is_dir():
        return ()
    return tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and within(member, path.resolve())
    )


def scan_files(paths: tuple[Path, ...], repository_root: Path) -> CodeConditions:
    sites = 0
    predicates: list[str] = []
    linux_only_sites: set[LinuxOnlySite] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        lines = text.splitlines()
        line_starts = [0]
        line_starts.extend(
            index + 1 for index, character in enumerate(text) if character == "\n"
        )
        occurrences = predicate_occurrences(text)
        predicate_ranges = tuple(
            (occurrence.expression_start, occurrence.expression_end)
            for occurrence in occurrences
        )
        condition_lines: set[int] = set()
        for occurrence in occurrences:
            if not is_operating_system_predicate(occurrence.predicate):
                continue
            condition_lines.add(occurrence.opening_line)
            predicates.append(occurrence.predicate)
            if predicate_effect(occurrence.predicate) == "linux_only":
                linux_only_sites.add(
                    LinuxOnlySite(
                        path.relative_to(repository_root), occurrence.opening_line
                    )
                )
        unclassified_lines: set[int] = set()
        for target in re.finditer(r"\btarget_(?:os|family)\b", text):
            if any(
                start <= target.start() < end for start, end in predicate_ranges
            ):
                continue
            line_number = bisect_right(line_starts, target.start())
            if line_number in unclassified_lines:
                continue
            condition_lines.add(line_number)
            unclassified_lines.add(line_number)
            predicates.append(lines[line_number - 1].strip())
        sites += len(condition_lines)
    return CodeConditions(
        sites,
        tuple(predicates),
        tuple(
            sorted(
                linux_only_sites,
                key=lambda site: (site.path.as_posix(), site.line),
            )
        ),
    )


def target_conditions(
    manifest: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    dependencies: set[str] = set()
    predicates: list[str] = []
    for target_name, raw_target in nested_table(manifest, "target").items():
        if not isinstance(raw_target, dict):
            continue
        match = re.fullmatch(r"cfg\((.*)\)", target_name.strip())
        if match is None:
            continue
        target = cast(dict[str, object], raw_target)
        has_dependency_table = False
        for table_name in ("dependencies", "dev-dependencies"):
            raw_dependencies = target.get(table_name)
            if not isinstance(raw_dependencies, dict):
                continue
            has_dependency_table = True
            dependency_table = cast(dict[object, object], raw_dependencies)
            dependencies.update(
                key for key in dependency_table if isinstance(key, str)
            )
        if has_dependency_table:
            predicates.append(normalized_predicate(match.group(1)))
    return tuple(sorted(dependencies)), tuple(predicates)


def suggestion(
    code_predicates: tuple[str, ...], dependency_predicates: tuple[str, ...]
) -> tuple[Suggestion, tuple[str, ...]]:
    predicates = code_predicates + dependency_predicates
    effects = tuple(predicate_effect(predicate) for predicate in predicates)
    unclassified = tuple(
        sorted(
            {
                predicate
                for predicate, effect in zip(predicates, effects, strict=True)
                if effect == "unclassified"
            }
        )
    )
    dependency_effects = tuple(
        predicate_effect(predicate) for predicate in dependency_predicates
    )
    if "linux_only" in dependency_effects:
        return "keep on natedev", unclassified
    if unclassified:
        return "needs review", unclassified
    return "can go to the Mac", unclassified


def configured_packages(repository: offload.RepositoryIdentity) -> frozenset[str]:
    if isinstance(repository, offload.UnknownRepository):
        return frozenset()
    config = os.environ.get("MAC_TEST_CONFIG", "~/.claude/config/mac_test.conf")
    values = sweep.config_values(str(Path(config).expanduser()))
    return frozenset(offload.comma_values(values.get(f"linux_only.{repository.name}", "")))


def audit_member(
    root: Path,
    member: Path,
    repository: offload.RepositoryIdentity,
    listed_packages: frozenset[str],
) -> MemberAudit:
    manifest = toml_table(member / "Cargo.toml")
    package = nested_table(manifest, "package").get("name")
    if not isinstance(package, str):
        raise ValueError(f"{member / 'Cargo.toml'} has no package name")
    source_paths = source_files(member, "src")
    build_script = member / "build.rs"
    if build_script.is_file() and within(member, build_script.resolve()):
        source_paths += (build_script,)
    source_conditions = scan_files(source_paths, root)
    test_conditions = scan_files(source_files(member, "tests"), root)
    dependencies, dependency_predicates = target_conditions(manifest)
    member_suggestion, unclassified = suggestion(
        source_conditions.predicates + test_conditions.predicates,
        dependency_predicates,
    )
    listing: ListingStatus = (
        "unknown"
        if isinstance(repository, offload.UnknownRepository)
        else "yes" if package in listed_packages else "no"
    )
    return MemberAudit(
        package,
        source_conditions.site_count,
        test_conditions.site_count,
        dependencies,
        listing,
        member_suggestion,
        unclassified,
        tuple(
            sorted(
                source_conditions.linux_only_sites
                + test_conditions.linux_only_sites,
                key=lambda site: (site.path.as_posix(), site.line),
            )
        ),
    )


def listing_disagrees(member: MemberAudit) -> bool:
    return (
        member.suggestion == "keep on natedev" and member.listing == "no"
    ) or (
        member.suggestion == "can go to the Mac" and member.listing == "yes"
    )


def print_report(
    root: Path,
    repository: offload.RepositoryIdentity,
    members: tuple[MemberAudit, ...],
) -> None:
    print(
        "| Package | Gated sites (src) | Gated sites (tests) | "
        + "Target dependencies | Listed | Suggestion |"
    )
    print("| --- | ---: | ---: | --- | --- | --- |")
    for member in members:
        dependencies = ", ".join(member.target_dependencies) or "none"
        print(
            f"| {member.package} | {member.source_sites} | {member.test_sites} | "
            + f"{dependencies} | {member.listing} | {member.suggestion} |"
        )
    if isinstance(repository, offload.UnknownRepository):
        print(f"\nRepository unknown: {root} is in no git repository.")
    disagreements = sorted(
        member.package for member in members if listing_disagrees(member)
    )
    print(f"\nListing disagreements: {', '.join(disagreements) or 'none'}.")
    reviews = [member for member in members if member.suggestion == "needs review"]
    review_text = "; ".join(
        f"{member.package} ({', '.join(member.unclassified_predicates)})"
        for member in reviews
    )
    print(f"Needs review: {review_text or 'none'}.")
    not_built = sorted(
        (
            member
            for member in members
            if member.suggestion != "keep on natedev" and member.linux_only_sites
        ),
        key=lambda member: member.package,
    )
    not_built_text = "; ".join(
        f"{member.package} ("
        + ", ".join(
            f"{site.path.as_posix()}:{site.line}"
            for site in member.linux_only_sites
        )
        + ")"
        for member in not_built
    )
    print(f"Not built on the Mac: {not_built_text or 'none'}.")


def run(repo_dir: str, check: bool) -> int:
    root = Path(repo_dir).expanduser().resolve()
    manifest_path = root / "Cargo.toml"
    try:
        manifest = toml_table(manifest_path)
        repository = offload.repository_identity(root)
        listed_packages = configured_packages(repository)
        members = tuple(
            sorted(
                (
                    audit_member(root, member, repository, listed_packages)
                    for member in member_directories(root, manifest)
                ),
                key=lambda member: (-member.site_count(), member.package),
            )
        )
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"audit: {error}", file=sys.stderr)
        return 2
    print_report(root, repository, members)
    if check and isinstance(repository, offload.UnknownRepository):
        return 2
    if check and any(
        member.suggestion == "keep on natedev" and member.listing == "no"
        for member in members
    ):
        return 1
    return 0


def main() -> int:
    arguments = parser().parse_args(namespace=AuditArguments())
    return run(arguments.repo_dir, arguments.check)


if __name__ == "__main__":
    raise SystemExit(main())
