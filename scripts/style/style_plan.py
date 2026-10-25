#!/usr/bin/env python3
"""Select available style work and refill a unit's standing plan."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from itertools import accumulate
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import assert_never, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(SCRIPTS / "production"))

import berth_board
import coverage_report
from approved_renames import (AppliedCheck, ApprovedRename, Declarations, FileRename, Folder, InlineModule,
                              MissingFile, RenameNotApplied, UncertainScan, applied_check,
                              approved_rename, declaration_sites, enclosing, file_rename, inline_modules, item_starts)
from add_style import StandingStyleProduction, standing_style_production
import rules
import style_ledger
import style_footprint
import style_proposals
from issues import Issue, frontmatter
from production_units import Refusal, cell_value, production_field, unit_table
from proposal_issue import ApprovedDecision, decision_answer
from unit_files import changed_files, git, unit_file_holders, unit_worktrees

FAMILIES: tuple[rules.Family, ...] = ("modules", "names", "values", "types")


@dataclass(frozen=True)
class AllCrates:
    pass


@dataclass(frozen=True)
class PilotCrates:
    members: frozenset[str]


PilotScope = AllCrates | PilotCrates
_ALL_CRATES = AllCrates()
_NO_FIRST_FILES: frozenset[str] = frozenset()
_ALL_FAMILY_SCOPES: Mapping[rules.Family, PilotScope] = MappingProxyType(
    {family: _ALL_CRATES for family in FAMILIES})


def crate_in_scope(scope: PilotScope, crate: str) -> bool:
    return isinstance(scope, AllCrates) or crate in scope.members


def parse_pilot_scope(text: str, repo: Path) -> PilotScope:
    """Resolve the supplied plan's pilot package names to member directories."""
    lines: list[str] = [match.group(1) for match in re.finditer(
        r"(?m)^\s*(?:- )?\*\*Pilot scope:\*\*[^\S\n]*(.*)$", delegation_context(text))]
    if not lines:
        return AllCrates()
    if len(lines) > 1:
        raise style_ledger.LedgerError("pilot scope has more than one scope line")
    manifests = {path.relative_to(repo).as_posix(): path.read_text()
                 for path in repo.rglob("Cargo.toml") if path.is_file()}
    members = {member.package_name: member.directory for member in rules.workspace_members(manifests)}
    names = tuple(name.strip() for name in lines[0].split(","))
    for name in names:
        if name not in members:
            raise style_ledger.LedgerError(f"pilot scope names unknown crate {name}")
    return PilotCrates(frozenset(members[name] for name in names))


def parse_first_files(text: str) -> frozenset[str]:
    """Read the optional file priority line in the plan's Delegation Context."""
    lines = [match.group(1) for match in re.finditer(
        r"(?m)^\s*(?:- )?\*\*First files:\*\*[^\S\n]*(.*)$", delegation_context(text))]
    if not lines:
        return frozenset()
    if len(lines) > 1:
        raise style_ledger.LedgerError("first files has more than one line")
    paths: set[str] = set()
    for entry in lines[0].split(","):
        path = entry.strip()
        if not path:
            raise style_ledger.LedgerError("first files has empty entry")
        if PurePosixPath(path).is_absolute():
            raise style_ledger.LedgerError(f"first files has absolute path {path}")
        if path in paths:
            raise style_ledger.LedgerError(f"first files has duplicate path {path}")
        paths.add(path)
    return frozenset(paths)


@dataclass(frozen=True)
class UnitPlanSettings:
    pilot_scope: PilotScope
    first_files: frozenset[str]


def unit_plan_settings(text: str, repo: Path) -> UnitPlanSettings:
    return UnitPlanSettings(parse_pilot_scope(text, repo), parse_first_files(text))


@dataclass(frozen=True)
class MissingUnitPlan:
    pass


def _unit_plan(doc: Path, unit: str, worktree: Path) -> Path:
    for row in unit_table(doc.read_text().splitlines()):
        if cell_value(row["Unit"]) == unit:
            return worktree / Path(cell_value(row["Plan"]))
    raise style_ledger.LedgerError(f"{unit}: no Units row in {doc}")


def family_plan_texts(doc: Path) -> Mapping[rules.Family, str | MissingUnitPlan]:
    """Read each style unit's own standing plan copy."""
    texts: dict[rules.Family, str | MissingUnitPlan] = {}
    for row in unit_worktrees(doc):
        family = rules.style_family(row.unit)
        if isinstance(family, rules.NotStyleUnit):
            continue
        plan = _unit_plan(doc, row.unit, row.worktree)
        try:
            texts[family] = plan.read_text()
        except FileNotFoundError:
            texts[family] = MissingUnitPlan()
    return texts


def family_plan_settings(doc: Path, repo: Path, *, unit: str = "") -> Mapping[rules.Family, UnitPlanSettings]:
    """Parse selection settings only for the units whose queue is requested."""
    return {family: unit_plan_settings(text, repo) if isinstance(text, str)
            else UnitPlanSettings(AllCrates(), frozenset())
            for family, text in family_plan_texts(doc).items()
            if not unit or rules.style_family(unit) == family}


def family_pilot_scopes(doc: Path, repo: Path) -> Mapping[rules.Family, PilotScope]:
    return {family: parse_pilot_scope(text, repo) if isinstance(text, str) else AllCrates()
            for family, text in family_plan_texts(doc).items()}


@dataclass(frozen=True)
class PhaseWritten:
    ticket_id: str


@dataclass(frozen=True)
class Resumed:
    ticket_id: str


@dataclass(frozen=True)
class RepairFirst:
    pass


@dataclass(frozen=True)
class WaitingForMerge:
    ticket_id: str
    checkpoint: str


@dataclass(frozen=True)
class WaitingForSync:
    unclassified: int


@dataclass(frozen=True)
class WaitingForStructure:
    crates: tuple[str, ...]
    unclassified: int


@dataclass(frozen=True)
class WaitingForBranchPointSync:
    branch_point: str
    branch_tip: str
    synced_tip: str


@dataclass(frozen=True)
class MergeStyleBranchFirst:
    """No ticket is left, and a judgment in the unit tip saw the inputs of each of these cells that the tip has.

    The pushed style tip holds the unit tip and changes those inputs, so a fast-forward to it lets refill issue them.
    """

    cell_count: int
    unit_tip: str
    style_branch: str


@dataclass(frozen=True)
class UnitBranchTip:
    """The commit where the unit's next ticket starts."""

    commit: str


@dataclass(frozen=True)
class NoUnitBranch:
    """The unit has no branch yet, so no cell was judged at its tip."""


UnitBase = UnitBranchTip | NoUnitBranch


def unit_base(repo: Path, branch: str) -> UnitBase:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch.removeprefix('refs/heads/')}^{{commit}}"],
        capture_output=True, text=True, check=False,
    )
    return UnitBranchTip(result.stdout.strip()) if result.returncode == 0 else NoUnitBranch()


@dataclass(frozen=True)
class TicketSelection:
    kind: style_ledger.TicketKind
    family: rules.Family
    crate: str
    files: tuple[str, ...]
    rules: tuple[str, ...]
    rule: str = ""
    sites: tuple[style_ledger.ListedSite, ...] = ()
    cells: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BusyFiles:
    board: berth_board.Board
    paths: frozenset[str]

    def contains(self, path: str) -> bool:
        return path in self.paths or any(reservation.blocks(path) for reservation in self.board.reservations())


NextPhase = (PhaseWritten | Resumed | RepairFirst | WaitingForMerge | WaitingForSync | WaitingForStructure
             | WaitingForBranchPointSync | MergeStyleBranchFirst | berth_board.BoardUnavailable)


def style_document(repo: Path) -> Path:
    standing = standing_style_production(repo)
    if isinstance(standing, StandingStyleProduction):
        return standing.doc
    raise style_ledger.LedgerError(f"no standing style production: {standing.slug}")


def busy_files(repo: Path, doc: Path, ledger: style_ledger.Ledger, unit: str, board: berth_board.Board) -> BusyFiles:
    holders = unit_file_holders(repo, doc, ledger, unit)
    # The unit's existing open ticket is handled before selection, but its dirty paths still block recovery.
    busy = frozenset(path for paths in holders.values() for path in paths)
    return BusyFiles(board, busy)


def approved_rename_pairs(findings: tuple[style_ledger.HeldFinding, ...]) -> tuple[ApprovedRename, ...]:
    pairs: list[ApprovedRename] = []
    for finding in findings:
        # A file rename's edits in other files are judged by approved_module_file.
        if isinstance(file_rename(finding.kind, finding.path, finding.after), FileRename):
            continue
        pair = approved_rename(finding.kind, finding.before, finding.after)
        if isinstance(pair, ApprovedRename):
            pairs.append(pair)
        elif finding.kind != "rename":
            # A type or restructure finding can change code that no pair proves, so no reference edit is approved.
            return ()
        # A rename with no qualified pair (a bare name, a field or prose) approves no reference edit of its own.
    return tuple(pairs)


def modified_reference_files(repo: Path) -> frozenset[str]:
    """Accept only in-place edits in both the index and the worktree."""
    entries = iter(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0"))
    modified: set[str] = set()
    rejected: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            rejected.update((path, next(entries)))
        elif status in {" M", "M ", "MM"}:
            modified.add(path)
        else:
            rejected.add(path)
    return frozenset(modified - rejected)


def use_reference_parents(words: tuple[str, ...]) -> dict[int, str]:
    """Associate a use leaf with the path that owns its brace group."""
    parents: dict[int, str] = {}
    cursor = 0
    while cursor < len(words):
        if words[cursor] != "use":
            cursor += 1
            continue
        cursor += 1
        groups: list[str] = []
        while cursor < len(words) and words[cursor] != ";":
            word = words[cursor]
            if word == "{":
                parent = words[cursor - 2] if cursor >= 2 and words[cursor - 1] == "::" else ""
                groups.append(parent)
            elif word == "}":
                if groups:
                    _ = groups.pop()
            elif style_footprint.IDENTIFIER.fullmatch(word):
                previous = words[cursor - 1] if cursor else ""
                following = words[cursor + 1] if cursor + 1 < len(words) else ""
                if previous != "as" and following != "::":
                    if previous == "::" and cursor >= 2:
                        parents[cursor] = words[cursor - 2]
                    elif groups and previous in {"{", ","}:
                        parents[cursor] = groups[-1]
            cursor += 1
    return parents


def approved_reference_qualifier(
    words: tuple[str, ...], index: int, use_parents: Mapping[int, str], pair: ApprovedRename,
) -> bool:
    parent = words[index - 2] if index >= 2 and words[index - 1] == "::" else use_parents.get(index, "")
    return parent != "Self" and parent == pair.parent


def unique_rename_mentions(
    repo: Path, pairs: tuple[ApprovedRename, ...], finding_files: set[str],
) -> frozenset[ApprovedRename]:
    """Prove which bare names refer only to the approved item at HEAD."""
    unique = set(pairs)
    matching = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", "-z", "-w",
         *(argument for pair in pairs for argument in ("-e", pair.old)), "HEAD", "--", "*.rs"],
        capture_output=True, text=True, check=False,
    )
    if matching.returncode == 1:
        return frozenset(unique)
    if matching.returncode:
        return frozenset()
    for entry in matching.stdout.split("\0"):
        if not entry:
            continue
        path = entry.removeprefix("HEAD:")
        source = style_footprint.tokens(git(repo, "show", f"HEAD:{path}"))
        if any(token.kind == "unknown" for token in source):
            return frozenset()
        words = tuple(token.value for token in source)
        use_parents = use_reference_parents(words)
        for pair in tuple(unique):
            found = declaration_sites(words, path, pair, pair.old) if path in finding_files else UncertainScan()
            declarations = found.sites if isinstance(found, Declarations) else frozenset[int]()
            for index, token in enumerate(source):
                if token.kind != "identifier" or token.value != pair.old:
                    continue
                if approved_reference_qualifier(words, index, use_parents, pair):
                    continue
                if path in finding_files:
                    if index >= 2 and words[index - 2:index] == ("Self", "::"):
                        continue
                    if index in declarations:
                        continue
                unique.discard(pair)
                break
    return frozenset(unique)


def approved_word_swaps(
    before: str, after: str, pairs: tuple[ApprovedRename, ...], unique: frozenset[ApprovedRename],
) -> bool:
    """Keep each text part exact except for approved whole identifier words."""
    parts = r"[A-Za-z0-9_]+|[^A-Za-z0-9_]+"
    old, new = tuple(re.finditer(parts, before)), tuple(re.finditer(parts, after))
    if len(old) != len(new):
        return False
    for left, right in zip(old, new, strict=True):
        if left[0] == right[0]:
            continue
        qualifier = re.search(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*::\s*$", before[:left.start()])
        if not any(left[0] == pair.old and right[0] == pair.new
                   and (qualifier[1] == pair.parent if qualifier else pair in unique) for pair in pairs):
            return False
    return True


def approved_reference_tokens(
    before: str, after: str, pairs: tuple[ApprovedRename, ...], unique: frozenset[ApprovedRename],
) -> bool:
    old, new = style_footprint.token_spans(before), style_footprint.token_spans(after)
    if (before == after or len(old) != len(new)
            or len(old) != len(style_footprint.tokens(before))
            or len(new) != len(style_footprint.tokens(after))):
        return False
    words = tuple(span.token.value for span in old)
    use_parents = use_reference_parents(words)
    old_cursor = new_cursor = 0
    for index, (span, target) in enumerate(zip(old, new, strict=True)):
        # Gaps contain only comments and whitespace; literals stay code tokens.
        if not approved_word_swaps(before[old_cursor:span.start], after[new_cursor:target.start], pairs, unique):
            return False
        old_cursor, new_cursor = span.end, target.end
        if span.token == target.token:
            continue
        if span.token.kind != "identifier" or target.token.kind != "identifier":
            return False
        if not any(pair.old == span.token.value and pair.new == target.token.value
                   and approved_reference_qualifier(words, index, use_parents, pair) for pair in pairs):
            return False
    return approved_word_swaps(before[old_cursor:], after[new_cursor:], pairs, unique)


@dataclass(frozen=True)
class UnreadableText:
    """The file is not at HEAD, or the HEAD file or the worktree file is not UTF-8 text the gate can read."""


def head_and_worktree(repo: Path, head_path: str, target: Path) -> tuple[str, str] | UnreadableText:
    """The text of the file at head_path in HEAD, and the text of the worktree file at target."""
    result = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{head_path}"], capture_output=True, check=False)
    if result.returncode:
        return UnreadableText()
    try:
        return result.stdout.decode("utf-8"), target.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        return UnreadableText()


def approved_reference_file(
    repo: Path, path: str, pairs: tuple[ApprovedRename, ...], unique: frozenset[ApprovedRename],
) -> bool:
    target = repo / path
    if not pairs or target.is_symlink() or not target.is_file():
        return False
    texts = head_and_worktree(repo, path, target)
    if isinstance(texts, UnreadableText):
        return False
    before, after = texts
    if target.suffix == ".md":
        return before != after and approved_word_swaps(before, after, pairs, unique)
    return target.suffix == ".rs" and approved_reference_tokens(before, after, pairs, unique)


def rename_pair_text(pair: ApprovedRename) -> str:
    parent = "::".join((*pair.qualifier, pair.parent))
    return f"`{parent}::{pair.old}` -> `{parent}::{pair.new}`"


def approved_file_renames(findings: tuple[style_ledger.HeldFinding, ...]) -> tuple[FileRename, ...]:
    return tuple(moved for finding in findings
                 if isinstance(moved := file_rename(finding.kind, finding.path, finding.after), FileRename))


@dataclass(frozen=True)
class NoCrateRoot:
    """No folder above the renamed file holds a `lib.rs` or `main.rs`."""


@dataclass(frozen=True)
class Library:
    """The library target of the renamed file's crate, by the name that paths in other crates start with."""

    name: str


@dataclass(frozen=True)
class NoLibrary:
    """The renamed file's crate has no library target the gate can name, so no path outside the crate names its modules."""


@dataclass(frozen=True)
class OutsideCrate:
    """The file is not a module file of the renamed file's crate, so its `crate`, `self` and `super` paths do not resolve."""


CrateModule = tuple[str, ...] | OutsideCrate


@dataclass(frozen=True)
class UnresolvedPath:
    """The path does not start at `crate`, `self` or `super`, or its file is outside the crate, so it names no known module."""


@dataclass(frozen=True)
class UseTrees:
    """The path before each segment and glob of the file's `use` items, and the `use` keyword of each segment and alias."""

    prefixes: Mapping[int, tuple[str, ...]]
    keywords: Mapping[int, int]


@dataclass(frozen=True)
class PathAttributes:
    """The `mod` keywords whose attributes hold a `path` word, and the string of each one whose path is the renamed file."""

    keywords: frozenset[int]
    literals: Mapping[int, int]


@dataclass(frozen=True)
class ModuleBinding:
    """An item, import or alias that binds the renamed module's name in one inline module scope of a file."""

    scope: tuple[InlineModule, ...]
    renamed: bool


@dataclass(frozen=True)
class GlobImport:
    """A `*` import in one inline module scope, and the module path it imports from."""

    scope: tuple[InlineModule, ...]
    source: tuple[str, ...] | UnresolvedPath


# The anchors that start a path the gate resolves from the file's own module.
PATH_ANCHORS = frozenset({"crate", "self", "super"})
# The item keywords whose name is in the type namespace, where a module name lives.
TYPE_ITEM_KEYWORDS = frozenset({"mod", "struct", "enum", "union", "trait", "type"})
# The words after a `use` leaf.
USE_LEAF_ENDS = frozenset({";", ",", "}", "as"})
# A path in a comment that starts at `crate`, as in an intra-doc link `[`crate::ledger::lock`]`.
CRATE_PATH = re.compile(r"(?<![\w:$])crate(?:::[A-Za-z_][A-Za-z0-9_]*)+")


def crate_root(repo: Path, moved: FileRename) -> PurePosixPath | NoCrateRoot:
    """The nearest folder above the renamed file that holds a crate root file."""
    for folder in PurePosixPath(moved.old).parents:
        if any((repo / folder / name).is_file() for name in ("lib.rs", "main.rs")):
            return folder
    return NoCrateRoot()


def crate_library(repo: Path, root: PurePosixPath) -> Library | NoLibrary:
    """The library of the crate whose root files are in root: its `[lib]` name, else its package name in snake case."""
    manifest = repo / root.parent / "Cargo.toml"
    if not (repo / root / "lib.rs").is_file() or not manifest.is_file():
        return NoLibrary()
    try:
        data: dict[str, object] = tomllib.loads(manifest.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return NoLibrary()
    for table in (data.get("lib"), data.get("package")):
        if isinstance(table, dict) and isinstance(name := cast(dict[str, object], table).get("name"), str):
            return Library(name.replace("-", "_"))
    return NoLibrary()


def crate_module(path: str, root: PurePosixPath) -> CrateModule:
    """The module path of a Rust file under the crate root folder: `()` for the crate root file."""
    file = PurePosixPath(path)
    if file.suffix != ".rs" or not file.is_relative_to(root):
        return OutsideCrate()
    folders = file.relative_to(root).parent.parts
    if folders[:1] == ("bin",):
        # Each file in `bin` is the root of a crate of its own.
        return OutsideCrate()
    if not folders and file.stem in {"lib", "main"}:
        return ()
    return folders if file.stem == "mod" else (*folders, file.stem)


def resolved_module(prefix: tuple[str, ...], module: CrateModule) -> tuple[str, ...] | UnresolvedPath:
    """The module that a path starting at `crate`, `self` or `super` names from module."""
    if isinstance(module, OutsideCrate) or not prefix or prefix[0] not in PATH_ANCHORS:
        return UnresolvedPath()
    rest = prefix[1:]
    base = () if prefix[0] == "crate" else module
    if prefix[0] == "super":
        rest = prefix
        while rest and rest[0] == "super":
            if not base:
                return UnresolvedPath()
            base, rest = base[:-1], rest[1:]
    return UnresolvedPath() if PATH_ANCHORS & set(rest) else base + rest


def code_item_starts(words: tuple[str, ...]) -> frozenset[int]:
    """The item starts outside every macro token tree: a macro reads the words in its tree as tokens, not as items."""
    in_macros = {index for start, end in style_footprint.macro_spans(words, style_footprint.delimiter_ends(words))
                 for index in range(start + 1, end)}
    return item_starts(words) - in_macros


def use_trees(words: tuple[str, ...], starts: frozenset[int]) -> UseTrees:
    prefixes: dict[int, tuple[str, ...]] = {}
    keywords: dict[int, int] = {}
    for keyword, word in enumerate(words):
        if word != "use" or keyword not in starts:
            continue
        groups: list[tuple[str, ...]] = [()]
        path: list[str] = []
        cursor = keyword + 1
        while cursor < len(words) and words[cursor] != ";":
            part = words[cursor]
            if part == "{":
                groups.append(groups[-1] + tuple(path))
                path = []
            elif part in {"}", ","}:
                if part == "}" and len(groups) > 1:
                    _ = groups.pop()
                path = []
            elif part == "as":
                cursor += 1
                keywords[cursor] = keyword
            elif part != "::":
                prefixes[cursor] = groups[-1] + tuple(path)
                keywords[cursor] = keyword
                path.append(part)
            cursor += 1
    return UseTrees(prefixes, keywords)


@dataclass(frozen=True)
class RenamedModuleScan:
    """One Rust file at path read at HEAD, and the module file rename whose references the scan finds in it."""

    path: str
    words: tuple[str, ...]
    module: CrateModule
    parent: tuple[str, ...]
    moved: FileRename
    library: Library | NoLibrary

    def renames(self) -> Mapping[int, str]:
        """The words that name the renamed module, each with its new word: its `mod` line, the path segments that name
        it, and the `#[path]` string that names its file.

        A word is a site only when the scan is certain; the gate keeps every other word unchanged.
        """
        words, stem = self.words, self.moved.old_stem
        starts = code_item_starts(words)
        modules = inline_modules(words, starts)
        trees = use_trees(words, starts)
        paths = self.path_attributes(starts, modules)
        depths = (0, *accumulate((word == "{") - (word == "}") for word in words))
        bindings: list[ModuleBinding] = []
        globs: list[GlobImport] = []
        block_bindings = False
        for index, word in enumerate(words[:-1]):
            if index == 0 or word not in {"*", stem}:
                continue
            # A binding is in the scope of its `use` keyword, or of the keyword just before its name.
            anchor = trees.keywords.get(index, index - 1)
            scope = enclosing(modules, anchor)
            if word == "*":
                if index in trees.prefixes:
                    globs.append(GlobImport(scope, resolved_module(trees.prefixes[index], self.scope_module(scope))))
                continue
            renamed = self.mod_line(index, starts, modules, paths)
            if index in trees.prefixes and words[index + 1] in {";", ",", "}"}:
                renamed = self.names_parent(trees.prefixes[index], self.scope_module(scope))
            elif not (renamed or words[index - 1] == "as"
                      or (words[index - 1] in TYPE_ITEM_KEYWORDS and index - 1 in starts)
                      or words[index - 2:index] == ("extern", "crate")):
                continue
            if depths[anchor] == len(scope):
                bindings.append(ModuleBinding(scope, renamed))
            else:
                block_bindings = True
        found: set[int] = set()
        for index, word in enumerate(words[:-1]):
            if index == 0 or word != stem or words[index - 1] == "." or words[index + 1:index + 3] == ("::", "<"):
                continue
            scope = enclosing(modules, index)
            if self.mod_line(index, starts, modules, paths):
                found.add(index)
            elif index in trees.prefixes and (trees.prefixes[index] or words[index - 1] == "::"):
                if (words[index + 1] == "::" or words[index + 1] in USE_LEAF_ENDS) and self.names_parent(
                        trees.prefixes[index], self.scope_module(enclosing(modules, trees.keywords[index]))):
                    found.add(index)
            elif words[index + 1] == "::":
                prefix = self.qualifier(index)
                if ((prefix and self.names_parent(prefix, self.scope_module(scope)))
                        or (not prefix and words[index - 1] != "::" and not block_bindings
                            and self.bound_in(scope, tuple(bindings), tuple(globs)))):
                    found.add(index)
        old_name, new_name = PurePosixPath(self.moved.old).name, PurePosixPath(self.moved.new).name
        return ({index: self.moved.new_stem for index in found}
                | {index: words[index].removesuffix(old_name + '"') + new_name + '"' for index in paths.literals.values()})

    def scope_module(self, scope: tuple[InlineModule, ...]) -> CrateModule:
        if isinstance(self.module, OutsideCrate):
            return self.module
        return self.module + tuple(module.name for module in scope)

    def mod_line(self, index: int, starts: frozenset[int], modules: tuple[InlineModule, ...], paths: PathAttributes) -> bool:
        """Whether the word at index is the name in the renamed module's `mod <name>;` line.

        A line with a `path` attribute declares the renamed module only when the attribute's string names its file.
        """
        if not (self.words[index - 1:index] == ("mod",) and index - 1 in starts
                and self.words[index + 1:index + 2] == (";",)):
            return False
        if index - 1 in paths.keywords:
            return index - 1 in paths.literals
        return self.scope_module(enclosing(modules, index)) == self.parent

    def path_attributes(self, starts: frozenset[int], modules: tuple[InlineModule, ...]) -> PathAttributes:
        """The `path` attributes of the file's `mod` items.

        A `#[path = "<string>"]` names the renamed file when it is the only `path` word of a `mod` at an item start
        outside every inline module, and its string, read from the file's own folder, is the old file and ends with the
        old file's name. A string with an escape, or a `path` word in any other form, names no file the gate can read.
        """
        words, old_name = self.words, PurePosixPath(self.moved.old).name
        spans = dict(style_footprint.attribute_spans(words))
        attributes = style_footprint.decorated_items(words)
        keywords = frozenset(owner for owner, words_of in attributes.items() if words[owner] == "mod" and "path" in words_of)
        literals: dict[int, int] = {}
        for start, owner in style_footprint.attribute_owners(words).items():
            named = words[start + 4][1:-1] if spans[start] == start + 6 else ""
            if (owner in keywords and owner in starts and attributes[owner].count("path") == 1
                    and not enclosing(modules, owner)
                    and words[start + 1:start + 5] == ("[", "path", "=", f'"{named}"') and "\\" not in named
                    and named.rsplit("/", 1)[-1] == old_name
                    and posixpath.normpath(str(PurePosixPath(self.path).parent / named)) == self.moved.old):
                literals[owner] = start + 4
        return PathAttributes(keywords, MappingProxyType(literals))

    def names_parent(self, prefix: tuple[str, ...], module: CrateModule) -> bool:
        """Whether a path prefix read in module names the renamed module's parent.

        A plain prefix such as `ledger` names it from a child of module inside the crate, and from the library's name in
        a file outside the crate.
        """
        if prefix and prefix[0] in PATH_ANCHORS:
            return resolved_module(prefix, module) == self.parent
        if not prefix or PATH_ANCHORS & set(prefix):
            return False
        if isinstance(module, OutsideCrate):
            return isinstance(self.library, Library) and prefix == (self.library.name, *self.parent)
        return module + prefix == self.parent

    def qualifier(self, index: int) -> tuple[str, ...]:
        """The path segments joined by `::` before the word at index."""
        segments: list[str] = []
        while index >= 2 and self.words[index - 1] == "::" and style_footprint.IDENTIFIER.fullmatch(self.words[index - 2]):
            segments.insert(0, self.words[index - 2])
            index -= 2
        return () if index >= 1 and self.words[index - 1] == "::" else tuple(segments)

    def bound_in(self, scope: tuple[InlineModule, ...], bindings: tuple[ModuleBinding, ...],
                 globs: tuple[GlobImport, ...]) -> bool:
        """Whether the stem names the renamed module in scope: every binding there does, or with none, a glob brings it.

        A glob from an outer inline module scope of the file brings what that scope binds.
        """
        bound = tuple(binding.renamed for binding in bindings if binding.scope == scope)
        if bound:
            return all(bound)
        for glob in globs:
            if glob.scope != scope:
                continue
            if glob.source == self.parent or any(
                    glob.source == self.scope_module(scope[:depth]) and self.bound_in(scope[:depth], bindings, globs)
                    for depth in range(len(scope))):
                return True
        return False


def sorted_use_groups(words: tuple[str, ...]) -> tuple[str, ...]:
    """The words of a use tree with the entries of each brace group in sorted order, no word dropped."""
    result: list[str] = []
    cursor = 0
    while cursor < len(words):
        if words[cursor] != "{":
            result.append(words[cursor])
            cursor += 1
            continue
        end = style_footprint.closing(words, cursor)
        entries: list[tuple[str, ...]] = []
        start = inner = cursor + 1
        while inner < end:
            if words[inner] == "{":
                inner = style_footprint.closing(words, inner)
            elif words[inner] == ",":
                entries.append(sorted_use_groups(words[start:inner]))
                start = inner + 1
            inner += 1
        entries.append(sorted_use_groups(words[start:end]))
        result.append("{")
        for position, entry in enumerate(sorted(entries)):
            result.extend((",", *entry) if position else entry)
        result.append("}")
        cursor = end + 1
    return tuple(result)


@dataclass(frozen=True)
class ModuleItem:
    """A `use` item or `mod <name>;` item: its first word (attributes and visibility included), its keyword and its `;`."""

    start: int
    keyword: int
    end: int


def module_items(words: tuple[str, ...]) -> tuple[ModuleItem, ...]:
    """Each `use` item and `mod <name>;` item, in order.

    An item with a `macro_use` attribute is left out: its place decides where its macros can be used.
    """
    starts = code_item_starts(words)
    attributes = {end: start for start, end in style_footprint.attribute_spans(words)}
    visibilities = {style_footprint.closing(words, index + 1) + 1: index for index, word in enumerate(words[:-1])
                    if word == "pub" and words[index + 1] == "("}
    items: list[ModuleItem] = []
    for keyword, word in enumerate(words):
        if keyword not in starts:
            continue
        if word == "use":
            end = next((index for index in range(keyword, len(words)) if words[index] == ";"), len(words))
        elif word == "mod" and words[keyword + 2:keyword + 3] == (";",):
            end = keyword + 2
        else:
            continue
        start = visibilities.get(keyword, keyword - 1 if words[keyword - 1:keyword] == ("pub",) else keyword)
        while start in attributes:
            start = attributes[start]
        if end < len(words) and "macro_use" not in words[start:keyword]:
            items.append(ModuleItem(start, keyword, end))
    return tuple(items)


def module_runs(words: tuple[str, ...]) -> tuple[tuple[ModuleItem, ...], ...]:
    """The runs of adjacent module items, in order."""
    runs: list[list[ModuleItem]] = []
    for item in module_items(words):
        if runs and runs[-1][-1].end + 1 == item.start:
            runs[-1].append(item)
        else:
            runs.append([item])
    return tuple(tuple(run) for run in runs)


def comment_gaps(text: str, spans: tuple[style_footprint.TokenSpan, ...],
                 renamed: Callable[[str], str]) -> tuple[tuple[str, ...], ...]:
    """The stripped lines of the comments before each code token, then after the last one, each passed through renamed."""
    ends = (0, *(span.end for span in spans))
    starts = (*(span.start for span in spans), len(text))
    return tuple(tuple(renamed(line.strip()) for line in text[end:start].splitlines() if line.strip())
                 for end, start in zip(ends, starts, strict=True))


def outer_doc(line: str) -> bool:
    """Whether the stripped comment line starts an outer doc comment, an attribute of the item after it."""
    return ((line.startswith("///") and not line.startswith("////"))
            or (line.startswith("/**") and not line.startswith(("/***", "/**/"))))


# A comment line, keyed by the count of the words of its piece before it.
CommentLine = tuple[int, str]
# The code words of one place in the code, and the comment lines before them and inside them.
CodePiece = tuple[tuple[str, ...], tuple[CommentLine, ...]]


def run_items(words: tuple[str, ...], gaps: tuple[tuple[str, ...], ...], run: tuple[ModuleItem, ...],
              dropped: frozenset[int]) -> tuple[tuple[str, ...], tuple[CodePiece, ...]]:
    """The comment lines before a run of module items that stay in place, and each item of the run with its comments.

    An item has its words without the dropped commas, the entries of a `use` tree sorted, and the lines of the gap
    before its first word and of the gaps inside it, each keyed by the count of the item's words before it. Only the
    use tree sorts: an attribute, the visibility and a `mod` item keep their words. The gap before the run stays in
    place up to its first outer doc comment, as rustfmt leaves it there.
    """
    first = gaps[run[0].start]
    doc = next((index for index, line in enumerate(first) if outer_doc(line)), len(first))

    def kept(start: int, end: int) -> tuple[str, ...]:
        return tuple(words[index] for index in range(start, end) if index not in dropped)

    items: list[CodePiece] = []
    for item in run:
        tree = kept(item.keyword + 1, item.end)
        code = (*kept(item.start, item.keyword + 1), *(sorted_use_groups(tree) if words[item.keyword] == "use" else tree),
                words[item.end])
        lines: list[CommentLine] = []
        key = 0
        for gap in range(item.start, item.end + 1):
            lines.extend((key, line) for line in (first[doc:] if gap == run[0].start else gaps[gap]))
            key += gap not in dropped
        items.append((code, tuple(lines)))
    return first[:doc], tuple(items)


def canonical_code(words: tuple[str, ...], gaps: tuple[tuple[str, ...], ...]) -> tuple[CodePiece, ...]:
    """The code in canonical order: each word with the comment lines before it, and each item of a run of adjacent
    module items with its comment lines, the run's items sorted.

    A trailing comma drops, and its comment lines go to the next piece; no other word drops. Use tree entries and the
    items of a run sort, and a comment moves with its item. None of these changes the code, and rustfmt makes them when
    a rename changes a name's sort order or line length. A comment line keeps its place among the words of its piece, so
    a comment that moves to another place in the code changes the pieces.
    """
    dropped = style_footprint.trailing_commas(words)
    pieces: list[CodePiece] = []
    lines: list[str] = []

    def place(index: int) -> None:
        """Add word index, or after the last word the comment lines left, as the next piece."""
        lines.extend(gaps[index])
        if (index not in dropped and index < len(words)) or (index == len(words) and lines):
            pieces.append((words[index:index + 1], tuple((0, line) for line in lines)))
            lines.clear()

    cursor = 0
    for run in module_runs(words):
        for index in range(cursor, run[0].start):
            place(index)
        before, items = run_items(words, gaps, run, dropped)
        if lines or before:
            pieces.append(((), tuple((0, line) for line in (*lines, *before))))
            lines.clear()
        pieces.extend(sorted(items))
        cursor = run[-1].end + 1
    for index in range(cursor, len(gaps)):
        place(index)
    return tuple(pieces)


def renamed_comment(line: str, scans: tuple[RenamedModuleScan, ...]) -> str:
    """The comment line with each `crate` path segment that names a renamed module changed to the module's new name.

    A segment names a renamed module when it is the old name and the segments before it name the module's parent. A
    comment has no certain scope, so a `self` or `super` path stays unchanged.
    """
    def renamed_path(path: re.Match[str]) -> str:
        segments = path[0].split("::")
        return "::".join(next((scan.moved.new_stem for scan in scans if segment == scan.moved.old_stem
                               and scan.names_parent(tuple(segments[:index]), scan.module)), segment)
                         for index, segment in enumerate(segments))
    return CRATE_PATH.sub(renamed_path, line)


@dataclass(frozen=True)
class RenamedText:
    """A Rust file's HEAD code and comment lines with the edits of the approved renames made, in canonical order.

    Module sites tells whether the file names a module of an approved file rename, in its code or in a comment.
    """

    code: tuple[CodePiece, ...]
    module_sites: bool

    def matches(self, after: str) -> bool:
        """Whether the worktree text has this code and these comment lines in canonical order."""
        spans = style_footprint.token_spans(after)
        return (len(spans) == len(style_footprint.tokens(after))
                and self.code == canonical_code(tuple(span.token.value for span in spans),
                                                comment_gaps(after, spans, lambda line: line)))


@dataclass(frozen=True)
class NoRenamedText:
    """The HEAD text has a token the gate cannot read, or a renamed file is not in a crate whose modules it can name."""


def renamed_text(repo: Path, path: str, before: str, moves: tuple[FileRename, ...],
                 pairs: tuple[ApprovedRename, ...]) -> RenamedText | NoRenamedText:
    """The HEAD text of the Rust file at path with the edits of the approved renames made.

    Each module site of an approved file rename takes its new word, then each qualified reference of an approved rename,
    which reads its parent from the HEAD words. In comments, each `crate` path to a renamed module is renamed.
    """
    old = style_footprint.token_spans(before)
    if len(old) != len(style_footprint.tokens(before)):
        return NoRenamedText()
    words = tuple(span.token.value for span in old)
    renamed = list(words)
    scans: list[RenamedModuleScan] = []
    for moved in moves:
        root = crate_root(repo, moved)
        if isinstance(root, NoCrateRoot):
            return NoRenamedText()
        module = crate_module(moved.old, root)
        if isinstance(module, OutsideCrate):
            return NoRenamedText()
        scans.append(RenamedModuleScan(path, words, crate_module(path, root), module[:-1], moved, crate_library(repo, root)))
        for index, word in scans[-1].renames().items():
            renamed[index] = word
    comments = comment_gaps(before, old, lambda line: renamed_comment(line, tuple(scans)))
    module_sites = tuple(renamed) != words or comments != comment_gaps(before, old, lambda line: line)
    use_parents = use_reference_parents(words)
    for index, word in enumerate(words):
        for pair in pairs:
            if word == pair.old and approved_reference_qualifier(words, index, use_parents, pair):
                renamed[index] = pair.new
    return RenamedText(canonical_code(tuple(renamed), comments), module_sites)


def approved_module_file(repo: Path, path: str, moves: tuple[FileRename, ...], pairs: tuple[ApprovedRename, ...]) -> bool:
    """Whether a Rust file outside the findings changes only the `mod` lines, `#[path]` strings and module paths of
    approved file renames, and the qualified references of approved renames.

    The file must name a renamed module, and its worktree text must be its renamed_text. A file that names no renamed
    module is judged by approved_reference_file alone.
    """
    target = repo / path
    if not moves or target.suffix != ".rs" or target.is_symlink() or not target.is_file():
        return False
    texts = head_and_worktree(repo, path, target)
    if isinstance(texts, UnreadableText):
        return False
    expected = renamed_text(repo, path, texts[0], moves, pairs)
    return isinstance(expected, RenamedText) and expected.module_sites and expected.matches(texts[1])


def approved_renamed_file(repo: Path, moved: FileRename, moves: tuple[FileRename, ...],
                          pairs: tuple[ApprovedRename, ...]) -> bool:
    """Whether the new file of an approved file rename is the old file at HEAD with only the edits approved_module_file
    allows, so no definition moves into or out of it.

    The old file must be gone and no file may be at the new path in HEAD. Self references such as `crate::ledger::lock`
    take the new name. The file's `use` and `mod` lines may take another order, which moves no definition between files.
    """
    target = repo / moved.new
    in_head = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"HEAD:{moved.new}"],
                             capture_output=True, check=False).returncode == 0
    if target.is_symlink() or not target.is_file() or os.path.lexists(repo / moved.old) or in_head:
        return False
    texts = head_and_worktree(repo, moved.old, target)
    if isinstance(texts, UnreadableText):
        return False
    # An unchanged file needs no scan, so a file outside every crate root, as in `tests/`, can move too.
    if texts[0] == texts[1]:
        return True
    expected = renamed_text(repo, moved.old, texts[0], moves, pairs)
    return isinstance(expected, RenamedText) and expected.matches(texts[1])


def outside_refusal(path: str, moves: tuple[FileRename, ...]) -> str:
    """Why the gate refuses a changed file outside the findings: a person moves a renamed module's folder of children."""
    for moved in moves:
        for folder in moved.module_folders:
            if path.startswith(folder + "/"):
                return f"{path} (in {folder}/, the folder of a renamed module: a person moves it)"
    return f"{path} (change is not an approved rename reference)"


def worktree_file(repo: Path, path: str) -> bytes | Folder | MissingFile:
    target = repo / path
    if target.is_dir():
        return Folder()
    return target.read_bytes() if target.is_file() else MissingFile()


def worktree_check(repo: Path, finding: style_ledger.HeldFinding) -> AppliedCheck:
    """Whether the finding's change is in the worktree."""
    return applied_check(finding.kind, finding.before, finding.after, finding.path, lambda path: worktree_file(repo, path))


def gate(repo: Path, ticket_id: str) -> int:
    """Exit 0 when the change may commit, 1 for a review result, 2 when the gate could not run."""
    try:
        with style_ledger.open_ledger(repo) as ledger:
            origin = ledger.ticket_origin(ticket_id)
            if isinstance(origin, style_ledger.SelectedByCoverage):
                missing = ledger.unjudged_ticket_cells(ticket_id)
                if missing:
                    for cell in missing:
                        print(f"unjudged: {cell.rule} {cell.path}")
                    return 1
                base = coverage_footprint_base(repo, ledger, origin.base)
                result = subprocess.run([sys.executable, str(SCRIPTS / "style/style_footprint.py"), "check",
                                         "--repo", str(repo), "--base", base, "--worktree"], check=False)
                return result.returncode if result.returncode in (0, 2) else 1
            fields = frontmatter(origin.issue)
            decision = Issue(origin.issue, fields)
            answer = decision_answer(decision)
            if not origin.finding_ids:
                raise style_ledger.LedgerError("proposal ticket " + made_before_decisions(ticket_id))
            by_id = {finding.finding_id: finding for finding in ledger.proposal_findings(origin.issue)}
            if any(identity not in by_id for identity in origin.finding_ids):
                raise style_ledger.LedgerError("proposal ticket findings changed")
            findings = tuple(by_id[identity] for identity in origin.finding_ids)
            if (decision.value("status") != "open" or not style_proposals.is_decision(decision)
                    or not isinstance(answer, ApprovedDecision) or answer.unit != origin.unit
                    or any(not isinstance(finding.state, style_ledger.Filed) or finding.state.proposal != origin.issue
                           or finding.build_ticket != style_ledger.BuildTicket(ticket_id) for finding in findings)):
                raise style_ledger.LedgerError("proposal approval or filed findings changed")
            moves = approved_file_renames(findings)
            allowed = {finding.path for finding in findings} | {moved.new for moved in moves}
            outside = changed_files(repo) - allowed
            pairs = approved_rename_pairs(findings)
            if outside:
                unique = unique_rename_mentions(repo, pairs, allowed) if pairs else frozenset[ApprovedRename]()
                modified = modified_reference_files(repo)
                refused = [path for path in sorted(outside)
                           if path not in modified or not (approved_reference_file(repo, path, pairs, unique)
                                                           or approved_module_file(repo, path, moves, pairs))]
                if refused:
                    raise style_ledger.LedgerError("files outside approved findings: " + ", ".join(
                        outside_refusal(path, moves) for path in refused))
            # A finding path is free, and a renamed file that another finding names moves under that approval.
            free = {finding.path for finding in findings
                    if not isinstance(file_rename(finding.kind, finding.path, finding.after), FileRename)}
            # A rename with its old file still there and no new file is not applied: the check below reports it.
            changed = [moved for moved in moves if not {moved.old, moved.new} & free
                       and (os.path.lexists(repo / moved.new) or not os.path.lexists(repo / moved.old))
                       and not approved_renamed_file(repo, moved, moves, pairs)]
            if changed:
                raise style_ledger.LedgerError("renamed files with other changes: " + ", ".join(
                    f"{moved.new} (renamed from {moved.old}: change is not only the rename)" for moved in changed))
            unapplied = tuple(finding for finding in findings if isinstance(worktree_check(repo, finding), RenameNotApplied))
            if unapplied:
                for finding in unapplied:
                    print(f"review: {finding.finding_id}: {finding.before} -> {finding.after} not applied in {finding.path}")
                return 1
            print("approved proposal")
            return 0
    except (OSError, sqlite3.Error, style_ledger.CheckCouldNotRun) as error:
        print(f"error: {error}")
        return 2
    except ValueError as error:
        print(f"review: {error}")
        return 1


def synced_tip(ledger: style_ledger.Ledger) -> str:
    with closing(sqlite3.connect(ledger.database, timeout=style_ledger.BUSY_TIMEOUT_SECONDS)) as connection:
        rows = cast(list[tuple[str]], connection.execute("SELECT tip FROM sync WHERE id=1").fetchall())
    if not rows:
        raise style_ledger.LedgerError("no synced tip; run set-tip first")
    return rows[0][0]


def synced_style_branch(ledger: style_ledger.Ledger) -> str:
    with closing(sqlite3.connect(ledger.database, timeout=style_ledger.BUSY_TIMEOUT_SECONDS)) as connection:
        rows = cast(list[tuple[str]], connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
    return rows[0][0] if rows else ""


def coverage_footprint_base(repo: Path, ledger: style_ledger.Ledger, recorded_base: str) -> str:
    """Exclude style-branch work taken in since the ticket's recorded start."""
    branch = synced_style_branch(ledger)
    if not branch:
        return recorded_base
    resolved = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
                              capture_output=True, text=True, check=False)
    if resolved.returncode == 1:
        return recorded_base
    if resolved.returncode:
        raise style_ledger.CheckCouldNotRun(resolved.stderr.strip() or "git rev-parse failed")
    merged = subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD", resolved.stdout.strip()],
                            capture_output=True, text=True, check=False)
    if merged.returncode == 1:
        return recorded_base
    if merged.returncode:
        raise style_ledger.CheckCouldNotRun(merged.stderr.strip() or "git merge-base failed")
    base = merged.stdout.strip()
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", recorded_base, base],
                              capture_output=True, text=True, check=False)
    if ancestry.returncode == 1:
        return recorded_base
    if ancestry.returncode:
        raise style_ledger.CheckCouldNotRun(ancestry.stderr.strip() or "git merge-base --is-ancestor failed")
    return base


@dataclass(frozen=True)
class BatchLimits:
    files: int
    lines: int
    floor: int


_READ_LIMITS = BatchLimits(25, 5000, 10)
_STRUCTURE_LIMITS = BatchLimits(6, 3000, 5)


def read_slices(repo: Path, tip: str, files: tuple[str, ...], limits: BatchLimits = _READ_LIMITS) -> tuple[tuple[str, ...], ...]:
    """Keep directories together, splitting only an oversized directory."""
    directories: dict[str, list[str]] = defaultdict(list)
    lengths = {path: len(git(repo, "show", f"{tip}:{path}").splitlines()) for path in files}
    for path in sorted(files):
        directories[str(PurePosixPath(path).parent)].append(path)
    groups: list[tuple[str, ...]] = []
    for directory in sorted(directories, key=lambda path: PurePosixPath(path).parts):
        paths = tuple(directories[directory])
        if len(paths) > limits.files or sum(lengths[path] for path in paths) > limits.lines:
            groups.extend((path,) for path in paths)
        else:
            groups.append(paths)
    return balanced_slices(tuple(groups), lengths, limits)


def balanced_slices(groups: tuple[tuple[str, ...], ...], lengths: dict[str, int],
                    limits: BatchLimits = _READ_LIMITS) -> tuple[tuple[str, ...], ...]:
    """Prefer feasible lower bounds before minimizing and balancing slice sizes."""
    costs = {len(groups): (0, 0, 0)}
    ends: dict[int, int] = {}
    for start in reversed(range(len(groups))):
        files, lines = 0, 0
        options: list[tuple[tuple[int, int, int], int]] = []
        for end in range(start + 1, len(groups) + 1):
            files += len(groups[end - 1])
            lines += sum(lengths[path] for path in groups[end - 1])
            if end > start + 1 and (files > limits.files or lines > limits.lines):
                break
            tail = costs[end]
            options.append(((tail[0] + int(files < limits.floor), tail[1] + 1, tail[2] + files * files), end))
        costs[start], ends[start] = min(options)
    result: list[tuple[str, ...]] = []
    start = 0
    while start < len(groups):
        end = ends[start]
        result.append(tuple(path for group in groups[start:end] for path in group))
        start = end
    return tuple(result)


def blocked_by_structure(cell: style_ledger.Cell, structure: tuple[style_ledger.Cell, ...]) -> bool:
    """A file cell waits for the structure cells at its own file; a crate cell waits for every one in its crate."""
    return any(blocker.crate == cell.crate and cell.path in (cell.crate, blocker.path)
               for blocker in structure)


def _is_name_bindings_rule(rule_id: str) -> bool:
    return PurePosixPath(rule_id.removesuffix("#uncovered")).name == "name-bindings-to-match-parameters.md"


def _is_when_to_split_rule(rule_id: str) -> bool:
    return PurePosixPath(rule_id.removesuffix("#uncovered")).name == "when-to-split-a-module.md"


@dataclass(frozen=True)
class RefillCells:
    """The cells one refill weighs.

    Ranking and structure blocking read every cell in cells. No ticket takes a cell in judged_at_base: it is one of
    the unit's own stale cells in its pilot scope, and a ticket at the unit tip cannot change it.
    """

    cells: tuple[style_ledger.Cell, ...]
    judged_at_base: frozenset[style_ledger.Cell]


def _unit_tickets(family: rules.Family, cell: style_ledger.Cell) -> bool:
    return cell.family == family or (family == "modules" and cell.tier == "D")


def _refill_cells(ledger: style_ledger.Ledger, base: UnitBase, family: rules.Family, scope: PilotScope) -> RefillCells:
    """Leave out unchanged held sites with open proposals, and find the unit's cells a ticket at its tip cannot change."""
    held = ledger.latest_held_judgments()
    findings: dict[tuple[str, str], list[style_ledger.HeldFinding]] = defaultdict(list)
    for finding in ledger.held_findings(style_ledger.AllHeldFindings()):
        # A waived, built or withdrawn finding needs no more work at its site.
        if not isinstance(finding.state, style_ledger.Waived | style_ledger.Built | style_ledger.Withdrawn):
            findings[(finding.rule, finding.path)].append(finding)
    tip = synced_tip(ledger)
    result: list[style_ledger.Cell] = []
    for cell in ledger.cells():
        key = (cell.rule, cell.path)
        if cell.state in ("open", "stale") and key in held and findings[key]:
            if all(isinstance(finding.state, style_ledger.Filed)
                   and frontmatter(finding.state.proposal).get("status") == ["open"]
                   for finding in findings[key]):
                try:
                    previous = git(ledger.repo, "rev-parse", "--verify", f"{held[key]}:{cell.path}")
                    current = git(ledger.repo, "rev-parse", "--verify", f"{tip}:{cell.path}")
                except style_ledger.LedgerError:
                    pass
                else:
                    if previous == current:
                        continue
        result.append(cell)
    own = (cell for cell in result if _unit_tickets(family, cell) and crate_in_scope(scope, cell.crate))
    judged = ledger.judged_at_base(own, base.commit) if isinstance(base, UnitBranchTip) else ()
    return RefillCells(tuple(result), frozenset(judged))


def candidate_tickets(repo: Path, ledger: style_ledger.Ledger, busy: BusyFiles,
                      family: rules.Family, scope: PilotScope = _ALL_CRATES,
                      family_scopes: Mapping[rules.Family, PilotScope] = _ALL_FAMILY_SCOPES,
                      *, base: UnitBase, first_files: frozenset[str] = _NO_FIRST_FILES) -> tuple[TicketSelection, ...]:
    return _tickets_from(repo, ledger, busy, family, scope, family_scopes, _refill_cells(ledger, base, family, scope),
                         first_files)


def _tickets_from(repo: Path, ledger: style_ledger.Ledger, busy: BusyFiles, family: rules.Family, scope: PilotScope,
                  family_scopes: Mapping[rules.Family, PilotScope], refill: RefillCells,
                  first_files: frozenset[str]) -> tuple[TicketSelection, ...]:
    pending: list[style_ledger.Cell] = []
    stale: dict[str, int] = defaultdict(int)
    blocked: dict[str, int] = defaultdict(int)
    structure_crates: set[str] = set()
    for cell in refill.cells:
        stale[cell.crate] += cell.state == "stale"
        if cell.state in ("open", "stale"):
            pending.append(cell)
            if cell.tier == "D":
                structure_crates.add(cell.crate)
            elif (cell.family is not None and cell.family != "modules" and cell.family in family_scopes
                  and crate_in_scope(family_scopes[cell.family], cell.crate)):
                blocked[cell.crate] += 1
    crates = sorted(stale, key=lambda crate: (-stale[crate], crate))
    if family == "modules":
        crates = sorted(structure_crates, key=lambda crate: (-blocked[crate], -stale[crate], crate)) + [
            crate for crate in crates if crate not in structure_crates
        ]
    tip = synced_tip(ledger)
    result: list[TicketSelection] = []
    for crate in crates:
        if not crate_in_scope(scope, crate):
            continue
        structure = tuple(cell for cell in pending if cell.crate == crate and cell.tier == "D")
        if structure:
            if family == "modules":
                manifest = str(PurePosixPath(crate) / "Cargo.toml")
                ticketed = tuple(cell for cell in structure if cell not in refill.judged_at_base)
                manifest_cells = tuple(cell for cell in ticketed if cell.path == crate)
                file_cells = tuple(cell for cell in ticketed if cell.path != crate and not busy.contains(cell.path))
                available = {cell.path for cell in file_cells}
                if manifest_cells and not busy.contains(manifest):
                    available.add(manifest)
                sites_by_rule = {rule_id: ledger.listed_sites(rule_id, crate)
                                 for rule_id in sorted({cell.rule for cell in structure})}
                for files in sorted(read_slices(repo, tip, tuple(sorted(available)), _STRUCTURE_LIMITS)):
                    batch_cells = tuple(cell for cell in file_cells if cell.path in files)
                    if manifest in files:
                        batch_cells += manifest_cells
                    rule_ids = tuple(sorted({cell.rule for cell in batch_cells}))
                    sites = tuple(site for rule_id in rule_ids for site in sites_by_rule[rule_id]
                                  if site.path in files)
                    result.append(TicketSelection("structure", family, crate, files, rule_ids, sites=sites,
                                                  cells=tuple(sorted((cell.rule, cell.path) for cell in batch_cells))))
                continue
        own = tuple(cell for cell in pending if cell.crate == crate and cell.family == family
                    and cell not in refill.judged_at_base
                    and not busy.contains(cell.path) and not blocked_by_structure(cell, structure))
        for rule_id in sorted({cell.rule for cell in own if cell.tier == "B"}):
            files = {cell.path for cell in own if cell.rule == rule_id}
            sites_by_file: dict[str, list[style_ledger.ListedSite]] = defaultdict(list)
            for site in ledger.listed_sites(rule_id, crate):
                if site.path in files:
                    sites_by_file[site.path].append(site)
            packed: list[style_ledger.ListedSite] = []
            for path in sorted(sites_by_file):
                sites = sites_by_file[path]
                if packed and len(packed) + len(sites) > 150:
                    result.append(TicketSelection("listed", family, crate, tuple(dict.fromkeys(site.path for site in packed)),
                                                  (rule_id,), rule_id, tuple(packed)))
                    packed = []
                packed.extend(sites)
            if packed:
                result.append(TicketSelection("listed", family, crate, tuple(dict.fromkeys(site.path for site in packed)),
                                              (rule_id,), rule_id, tuple(packed)))
        read_rules: dict[str, set[str]] = defaultdict(set)
        for cell in own:
            if cell.tier == "C":
                read_rules[cell.path].add(cell.rule)
        by_rules: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for path, rule_ids in sorted(read_rules.items()):
            name_bindings = tuple(sorted(rule_id for rule_id in rule_ids if _is_name_bindings_rule(rule_id)))
            other_rules = tuple(sorted(rule_id for rule_id in rule_ids if not _is_name_bindings_rule(rule_id)))
            for group in (name_bindings, other_rules):
                if group:
                    by_rules[group].append(path)
        for rule_ids, paths in sorted(by_rules.items()):
            priority = tuple(path for path in paths if path in first_files)
            remaining = tuple(path for path in paths if path not in first_files)
            for group in (priority, remaining):
                if group:
                    for files in read_slices(repo, tip, group):
                        result.append(TicketSelection("read", family, crate, files, rule_ids))
    return tuple(sorted(result, key=lambda ticket: not first_files.intersection(ticket.files)))


def delegation_context(text: str) -> str:
    match = re.search(r"(?ms)^## Delegation Context\n(.*?)(?=^## |\Z)", text)
    if match is None:
        raise style_ledger.LedgerError("plan lacks Delegation Context")
    return match.group(1).strip()


@dataclass(frozen=True)
class ReadWindow:
    start: int
    end: int


_NO_READ_WINDOWS: Mapping[str, tuple[ReadWindow, ...]] = MappingProxyType({})


def read_windows(text: str) -> tuple[ReadWindow, ...]:
    """Balance line ranges, keeping a file's top-level test module apart."""
    lines = len(text.splitlines())
    cuts = [0, lines]
    for match in re.finditer(r"(?m)^[ \t]*#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]", text):
        prefix = style_footprint.tokens(text[:match.start()])
        depth = sum(token.value in ("{", "(", "[") for token in prefix) - sum(
            token.value in ("}", ")", "]") for token in prefix)
        if depth or any(token.kind == "unknown" for token in prefix):
            continue
        following = [token.value for token in style_footprint.tokens(text[match.end():])]
        cursor = 0
        while following[cursor:cursor + 2] == ["#", "["]:
            cursor += 2
            brackets = 1
            while cursor < len(following) and brackets:
                brackets += int(following[cursor] == "[") - int(following[cursor] == "]")
                cursor += 1
        if following[cursor:cursor + 1] == ["pub"]:
            cursor += 1
            if following[cursor:cursor + 1] == ["("]:
                cursor += 1
                parentheses = 1
                while cursor < len(following) and parentheses:
                    parentheses += int(following[cursor] == "(") - int(following[cursor] == ")")
                    cursor += 1
        if following[cursor:cursor + 1] != ["mod"]:
            continue
        cut = text.count("\n", 0, match.start())
        if cut:
            cuts.insert(1, cut)
        break
    windows: list[ReadWindow] = []
    for start, end in zip(cuts, cuts[1:]):
        length = end - start
        count = (length + 299) // 300
        if not count:
            continue
        size, extra = divmod(length, count)
        for index in range(count):
            finish = start + size + int(index < extra)
            windows.append(ReadWindow(start + 1, finish))
            start = finish
    return tuple(windows)


class CrateTargets(Enum):
    NO_PACKAGE = "no-package"
    LIBRARY = "library"
    BINARY_ONLY = "binary-only"


def _is_two_judge_ticket(ticket: style_ledger.TicketRecord,
                         texts: tuple[style_ledger.PinnedRuleText, ...]) -> bool:
    return (ticket["kind"] == "read" and ticket["family"] == "names"
            and not any(_is_name_bindings_rule(pinned.rule_id) for pinned in texts))


def phase_text(ticket: style_ledger.TicketRecord, texts: tuple[style_ledger.PinnedRuleText, ...], plan: Path,
               context: str, held_findings: tuple[style_ledger.HeldFinding, ...] = (),
               file_windows: Mapping[str, tuple[ReadWindow, ...]] = _NO_READ_WINDOWS,
               crate_targets: CrateTargets = CrateTargets.LIBRARY,
               proposal_cells: tuple[style_ledger.ProposalCell, ...] = (),
               waived_findings: tuple[style_ledger.HeldFinding, ...] = ()) -> str:
    ticket_id = ticket["id"]
    number = ticket_id.rsplit("-", 1)[1]
    rule_lines = [f"- `{text.rule_id}`: `{plan.parent.parent / 'rules' / (text.blob + '.md')}` (blob `{text.blob}`)" for text in texts]
    sites = [f"- {json.dumps(site, sort_keys=True, ensure_ascii=False)}" for site in ticket["sites"]]
    uncovered = [f"- `{pinned.rule_id}` uncovered: {match.group(1).strip()}"
                 for pinned in texts
                 if ticket["kind"] == "structure" or pinned.rule_id.endswith("#uncovered")
                 for match in re.finditer(r"(?m)^\s+uncovered:\s*(.+)$", pinned.text.decode())]
    two_judges = _is_two_judge_ticket(ticket, texts)
    workers = ("2 judges, blind to each other; each judges every window of every file, "
               + "then the first judge applies the mechanical fixes.") if two_judges else (
        "2 writers; split files by subtree." if len(ticket["files"]) > 12 else (
            "1 writer + 1 tester; the tester opens as impl and re-reads the diff against the pinned rules."
        )
    )
    per_file_structure = ticket["kind"] == "structure" and bool(ticket["cells"])
    manifest_rules = [rule for rule, path in ticket["cells"] if path == ticket["crate"]]
    owned_lines = (["Owned pending verdict cells (record only these rule/path pairs):"]
                   + [f"- `{rule}` · `{path}`" for rule, path in ticket["cells"]]) if per_file_structure else []
    held_lines = (["Already-held findings: do not hold these again; record held for a file whose finding still stands."]
                  + [f"- {finding.finding_id} · {finding.rule} · {finding.path}:{finding.line} · {finding.text}"
                     for finding in held_findings]) if per_file_structure and held_findings else []
    waived_lines = (["Waived by the user: do not hold these again. Record waived for a cell whose only findings are these."]
                    + [f"- {finding.finding_id} · {finding.rule} · {finding.path}:{finding.line} · {finding.text}"
                       for finding in waived_findings]) if per_file_structure and waived_findings else []
    approved = ticket["kind"] == "proposal"
    # The gate takes reference edits in other files only for these pairs, so the text names them.
    pairs = approved_rename_pairs(held_findings)
    own_file_renames = any(finding.kind == "rename" and not isinstance(file_rename(finding.kind, finding.path, finding.after), FileRename)
                           and approved_rename(finding.kind, finding.before, finding.after) not in pairs
                           for finding in held_findings)
    # A proposal ticket names its exact cells, because the ledger refuses a tier B cell at a file with no listed site.
    record_lines = ([f"Record exactly these cells, each with `style_ledger.py record --ticket {ticket_id} --rule <rule> --path <file>`; "
                     + "the ledger takes its pinned rule blob:"] + [f"- `{cell.rule}` · `{cell.path}`" for cell in proposal_cells]
                    if proposal_cells else ["Record no cell: the ledger takes no cell of this ticket."]) if approved else [
        f"Record every judged cell with `style_ledger.py record --ticket {ticket_id}`; the ledger takes its pinned rule blob."]
    files: list[str] = []
    for path in ticket["files"]:
        line = f"- `{path}`"
        if ticket["kind"] == "read":
            windows = file_windows[path]
            line += " — " + ("lines " + ", ".join(f"{window.start}-{window.end}" for window in windows)
                              if windows else "empty file")
        files.append(line)
    authorization = (
        f"This ticket builds the approved proposal `{ticket['proposal']}`: its approved findings are this ticket's work. "
        + "Read the issue for its sites and before/after changes. Hold every other non-mechanical finding. Package verification remains required."
        if approved else "Every structure finding is held as a proposal. Apply only mechanical findings from other tickets."
    )
    if approved:
        goal = (f"Apply the approved findings of `{ticket['proposal']}` in {len(ticket['files'])} "
                + f"{'file' if len(ticket['files']) == 1 else 'files'} of `{ticket['crate']}`.")
    elif per_file_structure:
        goal = f"Judge these {len(ticket['files'])} files of `{ticket['crate']}` against every pinned rule below."
    else:
        goal = f"Judge the {ticket['kind']} ticket for {ticket['family']} in `{ticket['crate']}` against every pinned rule below."
    return "\n".join([
        f"### Phase {number} — {ticket_id}  · status: todo", "", "#### Work Order", "", context, "",
        "**Goal:**", goal,
        "", "**Spec:**", *rule_lines, *sites, *uncovered, *owned_lines,
        authorization,
        "Read only these pinned rule texts. A #uncovered rule is the parent rule's uncovered read.",
        *([f"`{ticket['crate']}` is binary-only (no library target): none of its items, `pub` or not, is public outside the crate."]
          if crate_targets is CrateTargets.BINARY_ONLY else []),
        *(["A conversion between the binding and the call passes through: `map_or`, `From::from`, `.into()`, "
           + "an accessor such as `.path()` or `.as_str()`, and a method that returns the same value in another form, "
           + "such as `.to_local_def_id()`; name the binding after the parameter the converted value fills. "
           + "A conversion's own parameter, such as the `value` of `From::from` or the `name` "
           + "of a newtype constructor such as `ThemeId::new(name)`, is never the target and never a second target: "
           + "follow the converted value to the parameter it fills. "
           + "A method that takes arguments or looks up another value (`target.local_upstream(root)`) "
           + "is no accessor: its result is another value. "
           + "A binding fills several parameters only when that same value fills them: "
           + "a value derived from it (`dir.join(\"config\")`) is another value, "
           + "and a later binding of the same name, as in a second loop, is another binding. "
           + "A call inside a macro's arguments counts, and an enum tuple variant wrapping the binding "
           + "(`Pane::App(id)`) passes through like a conversion. A binding of the same name declared "
           + "later in an inner block, such as a loop or closure body, would only shadow the renamed "
           + "binding; it is no collision unless that inner block also reads the renamed binding. "
           + "A parameter with a single-letter name, such as `x` and `y` in `Position::new(x, y)`, "
           + "gives no name to match: report no finding for it. "
           + "A binding placed as one element of an array or slice literal (`&[item]`) "
           + "does not fill the collection parameter: its name names one element. "
           + "The rule renames a binding that already exists: report no finding whose fix only adds a binding, "
           + "whether it names an inline expression, literal, array or field projection, "
           + "or adds a scoped alias that keeps the old name."]
          if any(_is_name_bindings_rule(pinned.rule_id) for pinned in texts) else []),
        *([
            "Judge file by file: take each file under **Files** in turn and judge it against every rule above, with that rule's listed sites "
            + "in it and its uncovered surface; read the uncovered instructions in the pinned text.",
            "Before judging a file, read its sibling files in the same module and its direct callers "
            + "(search the crate for its types and functions), so placement and ownership are judged against the code around it.",
            "Hold every distinct defect as its own finding. Never fold several defects into one crate-level finding, "
            + "and never stop at a file's first finding.",
            *(["For every file, name its type clusters (each type with the impls and functions that serve it) "
               + "and its test domains (groups of tests that exercise separate behaviour), then decide against "
               + "`when-to-split-a-module` whether the file should split, and hold a finding when it should. "
               + "Ask this of every file, also one where other rules already hold findings: moving or reordering "
               + "code under another rule does not answer whether the file should split. "
               + "A split finding names the split alone: each test, type or function that belongs with an owner "
               + "in another file is its own finding under the rule that places it, even when the split moves it."]
              if any(_is_when_to_split_rule(pinned.rule_id) for pinned in texts) else []),
            "A defect that spans files is held once, under the file that should change. A defect several rules name is held once, "
            + "under the rule it breaks most directly, naming the others in its text.",
            ("Record each file's verdict for every rule above as soon as that file has been judged against it, "
             + "with `record --path <file>`; the gate refuses this ticket while any file lacks one."
             if per_file_structure else "Record each rule's crate verdict only after every file has been judged against it."),
            *[f"Record `{rule}` once with `--path {ticket['crate']}` after reading the manifests." for rule in manifest_rules],
            *held_lines,
            *waived_lines,
        ] if ticket["kind"] == "structure" else []),
        *([
            "Judge window by window: take the files under **Files** in order, and each file's windows in order.",
            "For each window, read exactly that line range in full and judge it against every rule above, "
            + "line by line, giving its last line the same attention as its first.",
            "Start a file's first window with its module docs (the `//!` block and any comment before the first item); "
            + "judge them against every rule before any code.",
            *([
                "Two judges read this ticket, blind to each other: each judges every window of every file against every rule, "
                + "and neither reads the other's findings, board posts or edits until both have finished. Neither judge edits code while judging.",
                "Note each window's findings before opening the next window, each distinct defect as its own finding "
                + "(rule, path, line, text, kind, before and after); never stop at a window's first finding. "
                + "A judge holds no finding and records no cell: it lists its findings in its summary.",
            ] if two_judges else [
                "Hold that window's findings before opening the next window. Hold each distinct defect as its own finding; "
                + "never stop at a window's first finding.",
            ]),
            "A site that breaks more than one rule above is held under each rule it breaks, once per rule, "
            + "even when one change fixes both: each rule's cell records its own verdict.",
            *([
                "The unit director holds the union of both judges' findings, one finding per defect: a defect both judges hold is held once, "
                + "and a site that breaks more than one rule is held under each rule it breaks.",
                "A cell is recorded only after both judges have judged it: held when either judge holds a finding in it, "
                + "clean only when both judges call it clean.",
                "Once both judges have finished, the first judge applies the union's mechanical fixes.",
            ] if two_judges else [
                "Record a file's verdict for every rule only after its last window has been judged, "
                + "with `record --rule <rule> --path <file>` for each rule/file cell.",
            ]),
        ] if ticket["kind"] == "read" else []),
        "Product work wins. Skip reserved, unmerged or dirty files; follow the yielding procedure in ~/.claude/docs/style_production.md.",
        (("Apply the approved findings. "
          + ("For each of these renames, find every use through the compiler, not a text search, and update each one "
             + "in other files: " + ", ".join(rename_pair_text(pair) for pair in dict.fromkeys(pairs)) + ". "
             + "Also update every comment and Markdown doc in the repo that means the "
             + "renamed item, and leave mentions of other items with the same name. "
             + "Change nothing else in those files. "
             if pairs else "")
          + ("Apply any other rename only in its own file; when other files use its old name, hold it. "
             if own_file_renames else "")
          + ("For a file rename, rename the file, then update its `mod` line and each path that names the module "
             + "in other files; change nothing else in those files, comments included. "
             if approved_file_renames(held_findings) else "")
          + "Hold other findings: ") if approved else "Apply only mechanical fixes under ~/.claude/docs/style_production.md. Hold every other finding: ")
        + f"`style_ledger.py held add --ticket {ticket_id}` with the pinned rule blob, rule, site path, line and text, kind, before and after; "
        + "record that cell with `record --verdict held`.",
        "Hold a finding only when the cited rule's text requires the change.",
        f'Record other ideas with `style_ledger.py candidate --ticket {ticket_id} --site <path>:<line> --idea "<one line>"`.',
        *record_lines,
        "On a crate or module cell of a Rust source file, a `clean`, `exception` or `held` verdict must have `--input-scope`.",
        "Use `--input-scope file` when the verdict rests on that file alone. It stays current when other files change.",
        "Use `--input-scope rule --reads <files>` when the verdict rests on other files; name each file you read for it. "
        + "Without `--reads`, the cell comes back after any change in its crate or module.",
        "", "**Files:**", *files, "", "**Workers:**", workers, "",
        "**Acceptance gate:**", "For each touched package run:",
        "`bash ~/.claude/scripts/direct/verify.sh check <package>`",
        "`bash ~/.claude/scripts/direct/verify.sh test <package>`",
        "`bash ~/.claude/scripts/direct/verify.sh lint <package>`",
        f"`~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py gate --repo . --ticket {ticket_id}`",
        "Every ticket cell has a recorded verdict.", "",
    ])


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            _ = temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _waived_ticket_findings(ledger: style_ledger.Ledger, ticket: style_ledger.TicketRecord,
                            rule_paths: Mapping[str, Path]) -> tuple[style_ledger.HeldFinding, ...]:
    """Map waived sites with the ticket's pinned rules and repository layout."""
    if ticket["kind"] != "structure":
        return ()
    metadata = {rule: frontmatter(path) for rule, path in rule_paths.items()}
    manifest_rules = {rule for rule, fields in metadata.items()
                      if fields.get("tier") == ["D"] and fields.get("input_scope") == ["manifest"]}
    directories = (tuple(member.directory for member in coverage_report.workspace(ledger, ticket["base"]))
                   if manifest_rules else ())
    cells = {(rule, path) for rule, path in ticket["cells"]}
    return tuple(finding for finding in ledger.held_findings(style_ledger.HeldFindingsInState("waived"))
                 if (finding.rule, style_proposals.crate_for(directories, finding.path)
                     if finding.rule in manifest_rules else finding.path) in cells)


def write_phase(ledger: style_ledger.Ledger, ticket: style_ledger.TicketRecord, plan: Path, text: str) -> None:
    texts = ledger.pinned_rule_texts(ticket["id"])
    ticket["base"] = ledger.ticket_base(ticket["id"])
    crate = PurePosixPath(ticket["crate"])
    manifest: dict[str, object] = tomllib.loads(git(ledger.repo, "show", f"{ticket['base']}:{crate / 'Cargo.toml'}"))
    if not isinstance(manifest.get("package"), dict):
        crate_targets = CrateTargets.NO_PACKAGE
    elif isinstance(manifest.get("lib"), dict) or git(
            ledger.repo, "ls-tree", "--name-only", ticket["base"], "--", str(crate / "src/lib.rs")).strip():
        crate_targets = CrateTargets.LIBRARY
    else:
        crate_targets = CrateTargets.BINARY_ONLY
    file_windows = ({path: read_windows(git(ledger.repo, "show", f"{ticket['base']}:{path}"))
                     for path in ticket["files"]} if ticket["kind"] == "read" else {})
    held = (tuple(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())
                  if finding.path in ticket["files"] and finding.rule in ticket["rules"]
                  and not isinstance(finding.state, (style_ledger.Waived, style_ledger.Built, style_ledger.Withdrawn)))
            if ticket["kind"] == "structure" and ticket["cells"] else ())
    if ticket["kind"] == "proposal":
        identities = set(ticket["finding_ids"])
        held = tuple(finding for finding in ledger.proposal_findings(Path(ticket["proposal"]))
                     if finding.finding_id in identities)
    cells = ledger.proposal_cells(ticket["id"]) if ticket["kind"] == "proposal" else ()
    rule_paths = {pinned.rule_id: plan.parent.parent / "rules" / (pinned.blob + ".md") for pinned in texts}
    for pinned in texts:
        path = rule_paths[pinned.rule_id]
        if not path.exists() or git(plan.parent, "hash-object", str(path)).strip() != pinned.blob:
            atomic_write(path, pinned.text)
    waived = _waived_ticket_findings(ledger, ticket, rule_paths)
    phase = phase_text(ticket, texts, plan, delegation_context(text), held, file_windows, crate_targets, cells, waived)
    if any(match.group("id") == ticket["id"] for match in PHASE.finditer(text)):
        updated = PHASE.sub(lambda match: phase if match.group("id") == ticket["id"] else match.group(0), text)
        if updated != text:
            atomic_write(plan, updated.encode())
        return
    heading = re.search(r"^## Phases\s*$", text, re.MULTILINE)
    if heading is None:
        raise style_ledger.LedgerError("plan lacks Phases heading")
    following = re.search(r"^## ", text[heading.end():], re.MULTILINE)
    end = heading.end() + following.start() if following else len(text)
    atomic_write(plan, (text[:end].rstrip() + "\n\n" + phase + text[end:]).encode())


PHASE = re.compile(r"(?ms)^### Phase \d+ — (?P<id>[^\s]+)[^\n]*(?:\n|\Z).*?(?=^### Phase |^## |\Z)")


def discard_finished(ledger: style_ledger.Ledger, text: str) -> tuple[str, tuple[str, ...]]:
    with closing(sqlite3.connect(ledger.database, timeout=style_ledger.BUSY_TIMEOUT_SECONDS)) as connection:
        states = dict(cast(list[tuple[str, str]], connection.execute("SELECT id,state FROM tickets").fetchall()))
    changes: list[str] = []
    def keep(match: re.Match[str]) -> str:
        ticket_id = match.group("id")
        state = states.get(ticket_id, "")
        if state in ("merged", "dropped"):
            changes.append(f"discarded {ticket_id}: {state}")
            return ""
        return match.group(0)
    return PHASE.sub(keep, text), tuple(changes)


@dataclass(frozen=True)
class NoAvailableWork:
    pass


def made_before_decisions(ticket_id: str) -> str:
    """Why a proposal ticket with no finding ids cannot build, and what to do."""
    return f"{ticket_id}: made before decisions; drop it with style_ledger.py ticket drop, then run migrate"


def _unbuilt_findings(ledger: style_ledger.Ledger, issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
    """The decision's filed findings that no build ticket holds."""
    return tuple(finding for finding in ledger.proposal_findings(issue)
                 if isinstance(finding.state, style_ledger.Filed) and isinstance(finding.build_ticket, style_ledger.NoBuildTicket))


def _select_phase(
    ledger: style_ledger.Ledger, unit: str, branch: str, plan: Path, repo: Path,
    doc: Path, text: str, scope: PilotScope, busy: BusyFiles, first_files: frozenset[str],
    definitions: Mapping[str, rules.Rule],
) -> PhaseWritten | NoAvailableWork | WaitingForStructure | MergeStyleBranchFirst:
    queued: list[style_ledger.TicketRecord] = []
    for ticket in ledger.queued_tickets(unit):
        if ticket["kind"] == "proposal" and not ticket["finding_ids"]:
            print("waiting " + made_before_decisions(ticket["id"]))
        else:
            queued.append(ticket)
    for index, ticket in enumerate(queued):
        in_scope = crate_in_scope(scope, ticket["crate"])
        if index == 0 and in_scope and not any(busy.contains(path) for path in ticket["files"]):
            ledger.ticket_assign(ticket["id"], branch)
            for extra in queued[1:]:
                ledger.ticket_drop(extra["id"])
                print(f"dropped {extra['id']}: files busy")
            origin = ledger.ticket_origin(ticket["id"])
            if isinstance(origin, style_ledger.ApprovedProposal):
                style_proposals.proposal_command("start", str(origin.issue))
            write_phase(ledger, ledger.ticket_open(unit)[0], plan, text)
            return PhaseWritten(ticket["id"])
        ledger.ticket_drop(ticket["id"])
        reason = "files busy" if in_scope else "outside pilot scope"
        print(f"dropped {ticket['id']}: {reason}")
    family = rules.style_family(unit)
    if isinstance(family, rules.NotStyleUnit):
        raise style_ledger.LedgerError(f"{unit}: unknown style family")
    try:
        folder = Path(production_field(doc.read_text().splitlines(), "Proposals")).expanduser()
    except Refusal:
        decisions: tuple[Issue, ...] = ()
    else:
        if not folder.is_absolute():
            folder = doc.parent.parent / folder
        decisions = style_proposals.approved_decisions(folder, unit)
    directories = style_proposals.workspace_directories(ledger) if decisions else ()
    tip = synced_tip(ledger)
    decision_phase: PhaseWritten | NoAvailableWork = NoAvailableWork()
    closed = False
    sites = style_proposals.MovedSites(ledger, tip)
    unbuilt = {issue.path: _unbuilt_findings(ledger, issue.path) for issue in decisions if issue.value("style_rule") in definitions}
    # The declaration step runs once for the proposals folder that holds every decision, before the loop, so a finding
    # whose declaration moved tickets at its new file, a rename whose new name is declared is built, and one whose
    # declaration is gone waits for a person or is withdrawn with its file. Each refresh keeps the parent its decision
    # names, and the step renumbers after its own closes.
    follow = (style_proposals.run_follow(style_proposals.decision_writer(ledger, next(iter(unbuilt)).parent, ""),
                                         (finding for findings in unbuilt.values() for finding in findings), dry_run=False)
              if any(unbuilt.values()) else style_proposals.FollowRun(style_ledger.DeclarationReading(tip, ()), (), ()))
    outcomes = {outcome.finding.finding_id: outcome for outcome in follow.reading.outcomes}
    for issue in decisions:
        rule = issue.value("style_rule")
        definition = definitions.get(rule)
        if definition is None:
            print(f"waiting {issue.path}: rule {rule} not in the catalog")
            continue
        findings = unbuilt[issue.path]
        if not findings or issue.path in follow.closes:
            continue
        # The step wrote none of a skipped decision's findings, so the gone-file and moved-site waives must not write them either.
        if issue.path in follow.skipped:
            print(f"waiting {issue.path}: skipped by the declaration step")
            continue
        own = tuple(outcomes[finding.finding_id] for finding in findings)
        # The answer changed after this refill read the decision as approved, so it waits for the next refill.
        if any(isinstance(outcome, style_ledger.RenameAwaitingApproval) for outcome in own):
            print(f"waiting {issue.path}: no longer approved")
            continue
        left: dict[str, style_ledger.DeclarationLeft] = {
            outcome.finding.finding_id: outcome for outcome in own if isinstance(outcome, style_ledger.DeclarationLeft)}
        for finding_id, outcome in left.items():
            match outcome:
                case style_ledger.DeclaredInSeveralFiles():
                    where = f"declared in {len(outcome.paths)} files: {', '.join(sorted(outcome.paths))}"
                case style_ledger.DeclaredInAnotherCrate():
                    where = f"declared in another crate: {outcome.path}"
                case style_ledger.DeclarationGoneFromFile():
                    where = "declaration gone"
                case style_ledger.TraitIdentityUncertain():
                    where = "trait identity uncertain"
                case style_ledger.OutsideRenameUnresolved():
                    where = style_ledger.outside_evidence_line(outcome.evidence)
                case _:
                    assert_never(outcome)
            if (isinstance(outcome, (style_ledger.DeclaredInSeveralFiles, style_ledger.DeclaredInAnotherCrate,
                                     style_ledger.DeclarationGoneFromFile))
                    and isinstance(outcome.evidence, style_ledger.OutsideRenameNotProven)):
                where += "; " + style_ledger.outside_evidence_line(outcome.evidence)
            print(f"waiting {issue.path}: {finding_id} {where}")
        read = {outcome.finding.finding_id for outcome in own if not isinstance(outcome, style_ledger.DeclarationNotRead)}
        withdrew = any(isinstance(outcome, style_ledger.DeclarationRemoved) for outcome in own)
        built = any(isinstance(outcome, style_ledger.DeclarationRenamed) for outcome in own)
        findings = tuple(finding for finding in _unbuilt_findings(ledger, issue.path) if finding.finding_id not in left)
        # The gone-file and moved-site waives read only the findings whose declaration the step could not read.
        unread_findings = tuple(finding for finding in findings if finding.finding_id not in read)
        present = style_proposals.files_at(repo, tip, tuple(sorted({finding.path for finding in unread_findings})))
        moved = sites.destinations(tuple(finding for finding in unread_findings if finding.path in present))
        by_file: dict[str, list[style_ledger.HeldFinding]] = defaultdict(list)
        gone: dict[str, list[str]] = defaultdict(list)
        for finding in findings:
            if finding.finding_id not in read and finding.path not in present:
                gone[f"{finding.path} is not in synced commit {tip}"].append(finding.finding_id)
            elif finding.finding_id in moved:
                gone[f"{finding.path} site moved to {moved[finding.finding_id]} in synced commit {tip}"].append(finding.finding_id)
            else:
                by_file[finding.path].append(finding)
        # A finding in a removed file, or whose site a merge moved to another file, can never build at its
        # site, so it is waived and the decision can close.
        for words, finding_ids in sorted(gone.items()):
            _ = ledger.decline_findings(finding_ids, words, style_ledger.WaiveByLedger())
            print(f"waived {len(finding_ids)} findings in {issue.path}: {words}")
        if (gone or withdrew or built) and style_proposals.close_finished_decision(ledger, issue.path):
            closed = True
            continue
        outside = {path for path in by_file if not crate_in_scope(scope, style_proposals.crate_for(directories, path))}
        if outside:
            print(f"waiting {issue.path}: outside pilot scope")
        held = {path for path in by_file if path not in outside and busy.contains(path)}
        if held:
            print(f"waiting {issue.path}: files busy")
        available = tuple(sorted(set(by_file) - outside - held))
        if not available:
            continue
        limits = (_STRUCTURE_LIMITS if any(style_proposals.proposal_kind(finding, definition) == "restructure"
                                           for finding in findings) else _READ_LIMITS)
        files = read_slices(repo, tip, available, limits)[0]
        selected_findings = tuple(finding for path in files for finding in by_file[path])
        ticket_id = ledger.next_ticket_id(unit)
        ledger.ticket_add(ticket_id, unit, "proposal", family, style_proposals.crate_for(directories, files[0]),
                          list(files), sorted({finding.rule for finding in selected_findings}),
                          proposal=str(issue.path), finding_ids=tuple(finding.finding_id for finding in selected_findings))
        ledger.ticket_assign(ticket_id, branch)
        style_proposals.proposal_command("start", str(issue.path))
        write_phase(ledger, ledger.ticket_open(unit)[0], plan, text)
        decision_phase = PhaseWritten(ticket_id)
        break
    # As complete does, one renumber follows every decision this refill closed.
    if closed:
        style_proposals.renumber()
    if isinstance(decision_phase, PhaseWritten):
        return decision_phase
    base = unit_base(repo, branch)
    refill = _refill_cells(ledger, base, family, scope)
    candidates = _tickets_from(repo, ledger, busy, family, scope, family_pilot_scopes(doc, repo), refill, first_files)
    if not candidates:
        if isinstance(base, UnitBranchTip) and refill.judged_at_base:
            merge = ledger.style_merge_changes(refill.judged_at_base, base.commit)
            if isinstance(merge, style_ledger.StyleMergeChanges):
                return MergeStyleBranchFirst(len(merge.cells), base.commit, merge.style_branch)
        if family != "modules":
            pending = tuple(cell for cell in refill.cells
                            if cell.state in ("open", "stale") and crate_in_scope(scope, cell.crate))
            structure = tuple(cell for cell in pending if cell.tier == "D")
            own = tuple(cell for cell in pending if cell.family == family)
            blocked = tuple(sorted(crate for crate in {cell.crate for cell in own}
                                   if all(blocked_by_structure(cell, structure)
                                          for cell in own if cell.crate == crate)))
            if blocked:
                return WaitingForStructure(blocked, len(ledger.unclassified_rules()))
        return NoAvailableWork()
    selected = candidates[0]
    ticket_id = ledger.next_ticket_id(unit)
    ledger.ticket_add(ticket_id, unit, selected.kind, selected.family, selected.crate, list(selected.files),
                      list(selected.rules), selected.rule, [dict(rule=site.rule, path=site.path, line=site.line, text=site.text) for site in selected.sites],
                      cells=selected.cells)
    ledger.ticket_assign(ticket_id, branch)
    write_phase(ledger, ledger.ticket_open(unit)[0], plan, text)
    return PhaseWritten(ticket_id)


def next_phase(unit: str, plan: Path, repo: Path) -> NextPhase:
    plan, repo = plan.expanduser().resolve(), repo.expanduser().resolve()
    doc = style_document(repo)
    row = next((row for row in unit_worktrees(doc) if row.unit == unit), None)
    if row is None:
        raise style_ledger.LedgerError(f"{unit}: no Units row in {doc}")
    expected = _unit_plan(doc, unit, row.worktree).resolve()
    if plan != expected:
        raise style_ledger.LedgerError(f"--plan must be the unit worktree's own copy: {expected}")
    original = plan.read_text()
    settings = unit_plan_settings(original, repo)
    scope = settings.pilot_scope
    if any("repair" in line.lower() and re.search(r"status:\s*todo", line) for line in original.splitlines() if line.startswith("### Phase ")):
        return RepairFirst()
    with style_ledger.open_ledger(repo) as ledger:
        style_proposals.complete(ledger)
        text, changes = discard_finished(ledger, original)
        if text != original:
            atomic_write(plan, text.encode())
        for change in changes:
            print(change)
        opened = ledger.ticket_open(unit)
        if opened:
            ticket = opened[0]
            if ticket["state"] == "checkpointed":
                return WaitingForMerge(ticket["id"], ticket["checkpoint"])
            origin = ledger.ticket_origin(ticket["id"])
            if isinstance(origin, style_ledger.ApprovedProposal):
                style_proposals.proposal_command("start", str(origin.issue))
            write_phase(ledger, ticket, plan, text)
            ledger.clear_sync_wait(unit)
            return Resumed(ticket["id"])
        tip = synced_tip(ledger)
        standing = standing_style_production(repo)
        if not isinstance(standing, StandingStyleProduction):
            raise style_ledger.LedgerError(f"no standing style production: {standing.slug}")
        branch_tip = style_ledger.pushed_branch_point_tip(repo, standing.branch_point)
        ancestry = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch_tip, tip],
            capture_output=True, text=True, check=False,
        )
        if ancestry.returncode == 1:
            ledger.wait_for_sync(unit)
            return WaitingForBranchPointSync(standing.branch_point, branch_tip, tip)
        if ancestry.returncode:
            raise style_ledger.LedgerError(ancestry.stderr.strip() or "git merge-base --is-ancestor failed")
        # One catalog read serves both refill passes; it reads no shared state, so it runs before the lock.
        definitions = {rule.rule_id: rule for rule in rules.load_rules(repo)}
        with style_ledger.refill_lock(repo):
            board = berth_board.read_board(repo)
            if isinstance(board, berth_board.BoardUnavailable):
                return board
            busy = busy_files(repo, doc, ledger, unit, board)
            selected = _select_phase(ledger, unit, row.branch, plan, repo, doc, text, scope, busy, settings.first_files,
                                     definitions)
            # A unit told to merge the style branch acts at once, so it records no sync wait.
            if isinstance(selected, PhaseWritten | MergeStyleBranchFirst):
                ledger.clear_sync_wait(unit)
                return selected
        ledger.wait_for_sync(unit)
        with style_ledger.refill_lock(repo):
            board = berth_board.read_board(repo)
            if isinstance(board, berth_board.BoardUnavailable):
                return board
            busy = busy_files(repo, doc, ledger, unit, board)
            selected = _select_phase(ledger, unit, row.branch, plan, repo, doc, text, scope, busy, settings.first_files,
                                     definitions)
            if isinstance(selected, PhaseWritten | MergeStyleBranchFirst):
                ledger.clear_sync_wait(unit)
                return selected
            if isinstance(selected, WaitingForStructure):
                return selected
            return WaitingForSync(len(ledger.unclassified_rules()))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    refill = sub.add_parser("next-phase")
    _ = refill.add_argument("--unit", required=True)
    _ = refill.add_argument("--plan", type=Path, required=True)
    _ = refill.add_argument("--repo", type=Path, required=True)
    ticket_gate = sub.add_parser("gate")
    _ = ticket_gate.add_argument("--repo", type=Path, required=True)
    _ = ticket_gate.add_argument("--ticket", required=True)
    queue = sub.add_parser("queue")
    _ = queue.add_argument("--repo", type=Path, required=True)
    _ = queue.add_argument("--unit")
    args = cast(PlanArguments, parser.parse_args(argv))
    try:
        if args.command == "gate":
            return gate(args.repo, args.ticket)
        if args.command == "queue":
            board = berth_board.read_board(args.repo)
            if isinstance(board, berth_board.BoardUnavailable):
                print(f"board unavailable: {board.reason}")
                return 4
            with style_ledger.open_ledger(args.repo) as ledger:
                doc = style_document(args.repo)
                busy = busy_files(args.repo, doc, ledger, args.unit or "", board)
                settings = family_plan_settings(doc, args.repo, unit=args.unit or "")
                scopes = family_pilot_scopes(doc, args.repo)
                for row in unit_worktrees(doc):
                    unit = row.unit
                    family = rules.style_family(unit)
                    if isinstance(family, rules.NotStyleUnit) or (args.unit and args.unit != unit):
                        continue
                    first_files = settings[family].first_files if family in settings else _NO_FIRST_FILES
                    base = unit_base(args.repo, row.branch)
                    for ticket in candidate_tickets(args.repo, ledger, busy, family, scopes.get(family, _ALL_CRATES),
                                                    scopes, base=base, first_files=first_files):
                        print(f"{unit}: {ticket.kind} {ticket.crate} {ticket.rule or ','.join(ticket.rules)} — {len(ticket.files)} files: {', '.join(ticket.files)}")
            return 0
        result = next_phase(args.unit, args.plan, args.repo)
        match result:
            case PhaseWritten(ticket_id):
                print(f"phase written: {ticket_id}")
            case Resumed(ticket_id):
                print(f"resume {ticket_id}")
            case RepairFirst():
                print("repair first")
            case WaitingForMerge(ticket_id, checkpoint):
                print(f"waiting for merge: {ticket_id} checkpointed at {checkpoint[:7]}")
                return 5
            case berth_board.BoardUnavailable(reason):
                print(f"board unavailable: {reason}")
                return 4
            case WaitingForSync(unclassified):
                print("waiting for sync")
                if unclassified:
                    print(f"unclassified: {unclassified} rules — pull nate_style", file=sys.stderr)
                return 3
            case WaitingForStructure(crates, unclassified):
                print(f"waiting for structure: {', '.join(crates)}")
                if unclassified:
                    print(f"unclassified: {unclassified} rules — pull nate_style", file=sys.stderr)
                return 3
            case WaitingForBranchPointSync(branch_point, branch_tip, tip):
                print(f"waiting for sync: {branch_point} {branch_tip[:7]} is not in the synced tip {tip[:7]}")
                return 3
            case MergeStyleBranchFirst(cell_count, unit_tip, style_branch):
                print(f"merge the style branch first: {cell_count} cell{'' if cell_count == 1 else 's'} judged at {unit_tip[:7]} "
                      + f"wait for {style_branch.removeprefix('refs/heads/')}")
                return 6
        return 0
    except (OSError, ValueError, Refusal, sqlite3.Error) as error:
        print(f"style_plan: {error}", file=sys.stderr)
        return 2


class PlanArguments(argparse.Namespace):
    command: str = ""
    ticket: str = ""
    repo: Path = Path()
    unit: str = ""
    plan: Path = Path()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
