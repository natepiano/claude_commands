#!/usr/bin/env python3
"""Load and validate global and repository style rule metadata."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import re
import threading
import fnmatch
import json
import posixpath
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, TypedDict, cast, final, override

import style_rules
from candidate_generators import CandidatesSpec

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


# The ranked families, highest first. On each file a family's work waits while a family ranked above it has open work
# there. modules has no rank: structure blocking orders it.
FAMILY_RANKS: tuple[Family, ...] = ("types", "values", "names")


def ranked_above(family: Family) -> tuple[Family, ...]:
    """The families ranked above family, highest first; none for the top family or an unranked one."""
    return FAMILY_RANKS[:FAMILY_RANKS.index(family)] if family in FAMILY_RANKS else ()


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
class UserApproves:
    """The user answers each decision under the rule."""


@dataclass(frozen=True)
class AlwaysApproved:
    """The rule says `approval: always`: filing approves each new decision under it for the unit of its family."""
    family: Family


RuleApproval = UserApproves | AlwaysApproved


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
    body_identity: str
    judgment_identity: str
    origin: RuleOrigin
    approval: RuleApproval


@dataclass(frozen=True)
class RuleText:
    """One immutable rule snapshot, shared by the existing file-oriented metadata readers."""

    path: Path
    text: str

    def read_text(self) -> str:
        return self.text

    @override
    def __str__(self) -> str:
        return str(self.path)


def _body_identity(text: bytes) -> str:
    """Hash the exact bytes after a complete frontmatter block; unreadable text has no identity."""
    try:
        _ = text.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == b"---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == b"---":
                return hashlib.sha256(b"".join(lines[index + 1:])).hexdigest()
        return ""
    return hashlib.sha256(text).hexdigest()


# These are deletion patterns, not a metadata parser. Unmatched lines stay exact.
_ADMIN_COMMENT = rb"(?: #[^\r\n]*)?"
_ADMIN_ITEM = rb"[a-z0-9_-]+"
_ADMIN_LINE = re.compile(
    rb"(?:approval: always|tags: \[" + _ADMIN_ITEM + rb"(?:, " + _ADMIN_ITEM + rb")*\]"
    + rb"|date_(?:created|modified): (?P<quote>['\"]?)\[\[[0-9]{4}-[0-9]{2}-[0-9]{2}\]\](?P=quote))"
    + _ADMIN_COMMENT
)
_ADMIN_TAGS_BLOCK = re.compile(rb"tags:" + _ADMIN_COMMENT)
_ADMIN_TAG_ITEM = re.compile(rb"(?:  )?- " + _ADMIN_ITEM + _ADMIN_COMMENT)


def _judgment_identity(text: bytes) -> str:
    """Fingerprint validated body and frontmatter bytes after canonical admin deletion."""
    lines = text.splitlines(keepends=True)
    if text.decode("utf-8").splitlines(keepends=True) != [line.decode("utf-8") for line in lines]:
        # A separator only in an unchanged body also makes judgments stale.
        return ""
    frontmatter: list[bytes] = []
    body = text
    if lines and lines[0].rstrip(b"\r\n") == b"---":
        for index, line in enumerate(lines[1:], 1):
            if line.rstrip(b"\r\n") == b"---":
                frontmatter = lines[1:index]
                body = b"".join(lines[index + 1:])
                break
        else:
            raise ValueError("unterminated frontmatter")
    retained: list[bytes] = []
    index = 0
    while index < len(frontmatter):
        line = frontmatter[index]
        content = line.removesuffix(b"\n").removesuffix(b"\r")
        if _ADMIN_TAGS_BLOCK.fullmatch(content):
            end = index + 1
            trivia: list[bytes] = []
            removable = True
            while end < len(frontmatter):
                item = frontmatter[end]
                stripped = item.decode("utf-8").strip()
                if not stripped or stripped.startswith("#"):
                    trivia.append(item)
                elif stripped.startswith("- "):
                    if not _ADMIN_TAG_ITEM.fullmatch(item.removesuffix(b"\n").removesuffix(b"\r")):
                        removable = False
                else:
                    break
                end += 1
            # Validated list ownership crosses trivia and ends at the next key.
            retained.extend(trivia if removable else frontmatter[index:end])
            index = end
            continue
        if not _ADMIN_LINE.fullmatch(content):
            retained.append(line)
        index += 1
    header = b"".join(retained)
    # Length-prefix the header so bytes cannot move between header and body.
    return hashlib.sha256(len(header).to_bytes(8, "big") + header + body).hexdigest()


@final
class ValidatedRuleHistory:
    """Validated judgment identities, read through one batch process for this rule repository."""

    def __init__(self, repo: Path) -> None:
        self.identities: dict[tuple[str, RuleOrigin], str] = {}
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        _ = atexit.register(self.close)

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()
        _ = self.process.wait()

    def identity(self, blob: str, rule: Rule) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", blob):
            return ""
        key = (blob, rule.origin)
        with self.lock:
            if key not in self.identities:
                identity = ""
                try:
                    assert self.process.stdin is not None and self.process.stdout is not None
                    requests = cast(BinaryIO, self.process.stdin)
                    responses = cast(BinaryIO, self.process.stdout)
                    _ = requests.write(blob.encode() + b"\n")
                    requests.flush()
                    header = responses.readline().split()
                    if len(header) == 3:
                        content = responses.read(int(header[2]))
                        separator = responses.read(1)
                        if header[1] == b"blob" and len(content) == int(header[2]) and separator == b"\n":
                            identity = _rule_from_text(
                                rule.path, rule.origin, rule.path.parent, rule.reach, content, blob,
                            ).judgment_identity
                except (OSError, ValueError):
                    pass
                self.identities[key] = identity
            return self.identities[key]


@dataclass(frozen=True)
class RuleRepositoryIdentity:
    """The Git directory and object store currently installed at a rule repository."""

    common_dir: Path
    object_store_identity: tuple[int, int]

    @classmethod
    def at(cls, repo: Path) -> RuleRepositoryIdentity:
        git_dir = repo / ".git"
        if git_dir.is_file():
            marker = git_dir.read_text().strip()
            if not marker.startswith("gitdir: "):
                raise OSError("unreadable Git directory marker")
            git_dir = repo / marker.removeprefix("gitdir: ")
        git_dir = git_dir.resolve(strict=True)
        common = git_dir / "commondir"
        common_dir = (git_dir / common.read_text().strip()).resolve(strict=True) if common.is_file() else git_dir
        objects = (common_dir / "objects").resolve(strict=True)
        object_stat = objects.stat()
        return cls(common_dir, (object_stat.st_dev, object_stat.st_ino))


_RULE_BODIES: dict[RuleRepositoryIdentity, ValidatedRuleHistory] = {}
_RULE_BODIES_LOCK = threading.Lock()


def judged_text_unchanged(judged_blob: str, rule: Rule, *, compared_blob: str = "") -> bool:
    """Whether validated snapshots differ only in canonical administrative lines.

    compared_blob compares two historical pins without changing either pin's provenance.
    Only approval, tags, date_created and date_modified are administrative.
    A batch reader and validated identities are shared for the life of this run.
    """
    current = compared_blob or rule.blob
    if judged_blob == current == rule.blob:
        return bool(rule.judgment_identity)
    repo = next((parent for parent in rule.path.parents if (parent / ".git").exists()), None)
    if repo is None:
        return False
    try:
        repository = RuleRepositoryIdentity.at(repo)
        with _RULE_BODIES_LOCK:
            if repository not in _RULE_BODIES:
                _RULE_BODIES[repository] = ValidatedRuleHistory(repo)
            history = _RULE_BODIES[repository]
        old = rule.judgment_identity if judged_blob == rule.blob else history.identity(judged_blob, rule)
        body = rule.judgment_identity if current == rule.blob else history.identity(current, rule)
        return bool(old and body and old == body and repository == RuleRepositoryIdentity.at(repo))
    except OSError:
        return False


class RuleValidationError(ValueError):
    """A named rule does not meet the metadata contract."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")


def _scalar(metadata: style_rules.RuleFrontmatter, key: str, path: Path, default: str = "") -> str:
    if key not in metadata.fields:
        return default
    value = metadata.fields[key]
    if not isinstance(value, str) or not value.strip():
        raise RuleValidationError(path, f"{key} must be a nonempty scalar")
    return value


def _candidate_spec(metadata: style_rules.RuleFrontmatter, path: Path) -> CandidatesSpec | None:
    if "candidates" not in metadata.fields:
        return None
    block = metadata.fields["candidates"]
    if not isinstance(block, dict):
        raise RuleValidationError(path, "candidates must be a mapping")
    candidates = style_rules.RuleFrontmatter(dict(block))
    kind = _scalar(candidates, "kind", path)
    if not kind:
        raise RuleValidationError(path, "candidates.kind is required")
    surface: Literal["whole", "partial", "unclassified"] = "unclassified"
    declared = _scalar(candidates, "surface", path)
    if "surface" in block and declared not in ("whole", "partial"):
        raise RuleValidationError(path, "candidates.surface must be whole or partial")
    if declared in ("whole", "partial"):
        surface = declared
    uncovered = _scalar(candidates, "uncovered", path)
    if surface == "partial" and not uncovered:
        raise RuleValidationError(path, "partial candidates.surface requires uncovered")
    return CandidatesSpec(
        kind=kind, origin=str(path), surface=surface, uncovered=uncovered,
        pattern=_scalar(candidates, "pattern", path),
        exclude_pattern=_scalar(candidates, "exclude_pattern", path),
        field_type_pattern=_scalar(candidates, "field_type_pattern", path),
        globs=tuple(candidates["globs"]), paths=tuple(candidates["paths"]),
        paths_exempt=tuple(candidates["paths_exempt"]), structs=tuple(candidates["structs"]),
    )


def _rule_from_text(path: Path, origin: RuleOrigin, rule_root: Path, reach: RepoReach | MemberReach,
                    text: bytes, blob: str) -> Rule:
    decoded = text.decode("utf-8")
    # These existing readers only use read_text and str(path). Adapt the immutable
    # snapshot at their Path API boundary, so history and loading cannot diverge.
    source = cast(Path, cast(object, RuleText(path, decoded)))
    try:
        metadata = style_rules.parse_frontmatter(source)
    except ValueError as error:
        raise RuleValidationError(path, str(error)) from error
    candidates = _candidate_spec(metadata, path)

    tier: Tier | Literal["unclassified"] = "unclassified"
    family: Family | None = None
    input_scope: InputScope = "crate"
    has_classification = any(key in metadata.fields for key in ("tier", "family", "input_scope"))
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

    approval: RuleApproval = UserApproves()
    declared_approval = _scalar(metadata, "approval", path)
    if declared_approval:
        if declared_approval != "always":
            raise RuleValidationError(path, "approval must be always")
        if family is None:
            raise RuleValidationError(path, "approval always requires a family")
        approval = AlwaysApproved(family)

    pre_filter_value = metadata.fields.get("pre_filter", "")
    if not isinstance(pre_filter_value, str):
        raise RuleValidationError(path, "pre_filter must be a scalar")
    pre_filter = pre_filter_value or None
    if tier == "B" and candidates is None and pre_filter is None:
        raise RuleValidationError(path, "tier B requires candidates or pre_filter")

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
        body_identity=_body_identity(text),
        judgment_identity=_judgment_identity(text),
        origin=origin,
        approval=approval,
    )


def _load_rule(path: Path, origin: RuleOrigin, rule_root: Path, reach: RepoReach | MemberReach) -> Rule:
    text = path.read_bytes()
    blob = subprocess.run(
        ["git", "hash-object", "--stdin"], input=text, check=True, capture_output=True,
    ).stdout.decode().strip()
    return _rule_from_text(path, origin, rule_root, reach, text, blob)


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


def global_rule_paths() -> tuple[Path, ...]:
    """The file of each global rule, in name order. A rule's name is its file stem."""
    global_dir = style_rules.NATE_STYLE_DIR.resolve() / "rust"
    return tuple(path.resolve() for path in sorted(global_dir.glob("*.md")) if path.is_file())


def load_rules(repo_root: Path | None) -> tuple[Rule, ...]:
    """Read every global rule, then root and member rules in a repository.

    Global rules predating classification metadata remain unclassified.
    Local rules and any declared classification metadata are validated.
    Use the loader's directory layout without its context-loading filters:
    this registry includes tool-enforced and Bevy rules for every repository.
    """
    global_root = style_rules.NATE_STYLE_DIR.resolve()
    rules = [_load_rule(path, "global", global_root, RepoReach()) for path in global_rule_paths()]
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
