#!/usr/bin/env python3
"""Select available style work and refill a unit's standing plan."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import posixpath
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter, defaultdict
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager, redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from itertools import accumulate
from pathlib import Path, PurePosixPath
from types import FrameType, MappingProxyType
from typing import Literal, assert_never, cast, override

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(SCRIPTS / "production"))

import berth_board
import coverage_report
import mechanical_classes
import merge_gate
from approved_renames import (AppliedCheck, ApprovedRename, BindingRename, Declarations, FileRename, Folder, InlineModule,
                              MissingFile, NoAppliedCheck, RenameNotApplied, UncertainScan, applied_check,
                              approved_rename, binding_rename, binding_rename_check, declaration_sites, enclosing,
                              file_rename, inline_modules)
from add_style import StandingStyleProduction, standing_style_production
import rules
import showrunners
import style_ledger
import style_footprint
import style_proposals
from issues import Issue, frontmatter
from scripts.build_hold import build_hold
from production_units import Refusal, cell_value, live_unit_table, production_field
from proposal_issue import ApprovedDecision, decision_answer, require_generated_body
from unit_files import changed_files, git, unit_file_holders, unit_worktrees
from unit_plans import (AllCrates, PilotScope, crate_in_scope, delegation_context, family_plan_texts, family_pilot_scopes,
                        parse_pilot_scope, unit_plan_path)

FAMILIES: tuple[rules.Family, ...] = ("modules", "names", "values", "types")
_ALL_CRATES = AllCrates()
_NO_FIRST_FILES: frozenset[str] = frozenset()
_ALL_FAMILY_SCOPES: Mapping[rules.Family, PilotScope] = MappingProxyType(
    {family: _ALL_CRATES for family in FAMILIES})


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


def family_plan_settings(doc: Path, repo: Path, *, unit: str = "") -> Mapping[rules.Family, UnitPlanSettings]:
    """Parse selection settings only for the units whose queue is requested."""
    return {family: unit_plan_settings(text, repo) if isinstance(text, str)
            else UnitPlanSettings(AllCrates(), frozenset())
            for family, text in family_plan_texts(doc).items()
            if not unit or rules.style_family(unit) == family}


@dataclass(frozen=True)
class FamilyIdleReason:
    family: rules.Family
    reason: str


@dataclass(frozen=True)
class BuildableFindings:
    count: int
    reason: str


class FamilySelection(Enum):
    OWN = "own"
    COUNT = "count"
    BORROW = "borrow"


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
    reason: str = ""
    families: tuple[FamilyIdleReason, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class WaitingForSync:
    unclassified: int
    reason: str = ""
    families: tuple[FamilyIdleReason, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class WaitingForStructure:
    crates: tuple[str, ...]
    unclassified: int
    families: tuple[FamilyIdleReason, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class SyncNeeded:
    reason: str


@dataclass(frozen=True)
class SyncNotNeeded:
    pass


@dataclass(frozen=True)
class MainChanges:
    pushed_tip: str
    paths: frozenset[str] = frozenset()
    crates: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnknownMainChanges:
    reason: str


def main_changes(ledger: style_ledger.Ledger, branch_point: str, tip: str) -> MainChanges | UnknownMainChanges:
    """Read every commit's touched paths once; endpoint diffs would miss edits later reverted on main."""
    repo = ledger.repo
    try:
        main = style_ledger.pushed_branch_point_tip(repo, branch_point)
        # A synced style tip can already contain main, without main containing that style tip.
        for older, newer in ((main, tip), (tip, main)):
            ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
                                      capture_output=True, text=True, check=False)
            if ancestry.returncode == 0:
                if older == main:
                    return MainChanges(main)
                break
            if ancestry.returncode != 1:
                return UnknownMainChanges(ancestry.stderr.strip() or "git ancestry check failed")
        else:
            return UnknownMainChanges("synced tip is not an ancestor of main")
        changes = subprocess.run(
            ["git", "-C", str(repo), "-c", "diff.renameLimit=0", "log", "--format=", "--name-status", "-z",
             "--no-ext-diff", "--no-textconv", "--no-relative", "-M", "-C", "--find-copies-harder",
             "--diff-merges=first-parent", f"{tip}..{main}", "--"],
            capture_output=True, check=False,
        )
        if changes.returncode or changes.stderr:
            return UnknownMainChanges(os.fsdecode(changes.stderr).strip() or "git touched-path read failed")
        fields = changes.stdout.split(b"\0")
        paths: set[str] = set()
        index = 0
        while index < len(fields) - 1:
            status = fields[index].lstrip(b"\n")
            if status.startswith((b"R", b"C")):
                return UnknownMainChanges("rename or copy in main range")
            if status not in (b"A", b"M", b"D", b"T") or index + 1 >= len(fields) - 1 or not fields[index + 1]:
                return UnknownMainChanges("unrecognized git name-status output")
            raw_path = fields[index + 1]
            path = os.fsdecode(raw_path)
            if os.fsencode(path) != raw_path:
                return UnknownMainChanges("unrepresentable git path")
            # Ledger paths cross a UTF-8 text boundary; refuse surrogate escapes there.
            _ = path.encode("utf-8")
            paths.add(path)
            index += 2
        if fields[-1]:
            return UnknownMainChanges("unterminated git name-status output")
        crates = tuple(member.directory for member in coverage_report.workspace(ledger, tip)) if paths else ()
        return MainChanges(main, frozenset(paths), tuple(sorted(crates, key=len, reverse=True)))
    except (OSError, ValueError, style_ledger.LedgerError, subprocess.CalledProcessError) as error:
        return UnknownMainChanges(str(error))


def ticket_sync_check(changes: MainChanges | UnknownMainChanges, files: tuple[str, ...]) -> SyncNeeded | SyncNotNeeded:
    if isinstance(changes, UnknownMainChanges):
        return SyncNeeded(f"touched set unknown: {changes.reason}")
    if not changes.paths:
        return SyncNotNeeded()
    protected = {"Cargo.toml", "Cargo.lock", *files}
    for path in files:
        crate = next((crate for crate in changes.crates
                      if crate == "." or path == crate or path.startswith(crate + "/")), "")
        if not crate:
            return SyncNeeded(f"touched set unknown: unknown crate for {path}")
        protected.update(str(PurePosixPath(crate) / name) for name in ("Cargo.toml", "build.rs"))
    touched = sorted(protected & changes.paths)
    if not touched:
        return SyncNotNeeded()
    paths = ", ".join(touched[:3])
    if len(touched) > 3:
        paths += f" +{len(touched) - 3}"
    return SyncNeeded(f"main touched {paths}")


def ticket_sync_wait(ledger: style_ledger.Ledger, changes: MainChanges | UnknownMainChanges,
                     kind: str, crate: str, rule: str, files: tuple[str, ...],
                     findings: tuple[style_ledger.HeldFinding, ...] = ()) -> WaitingForSync | SyncNotNeeded:
    # Only the selected proposal needs a footprint at main, where new callers may have appeared.
    if isinstance(changes, MainChanges) and changes.paths and findings:
        try:
            main_edits = EditedFiles(ledger.repo, ledger, UnitBranchTip(changes.pushed_tip))
            files = tuple(dict.fromkeys((*files, *(path for finding in findings for path in main_edits.of(finding)))))
        except (OSError, ValueError, style_ledger.LedgerError, subprocess.CalledProcessError) as error:
            changes = UnknownMainChanges(f"cannot read selected footprint at main: {error}")
    check = ticket_sync_check(changes, files)
    if isinstance(check, SyncNeeded):
        label = " ".join(filter(None, (kind, crate, rule)))
        return WaitingForSync(0, f"{label}: {check.reason}")
    return check


@dataclass(frozen=True)
class MergeStyleBranchFirst:
    """No ticket is left, and a judgment in the unit tip saw the inputs of each of these cells that the tip has.

    The pushed style tip holds the unit tip and changes those inputs, so a fast-forward to it lets refill issue them.
    """

    cell_count: int
    unit_tip: str
    style_branch: str
    families: tuple[FamilyIdleReason, ...] = field(default=(), compare=False)


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
class UnsyncedChange:
    """The newest commit on the unit base, not in the synced tip, that changed path."""

    path: str
    commit: str

    def line(self, tip: str) -> str:
        return f"{self.path} changed in {self.commit[:7]}, not in the synced tip {tip[:7]}"


def unsynced_changes(repo: Path, base: UnitBase, tip: str) -> Mapping[str, UnsyncedChange]:
    """The files that commits on the unit base, not in the synced tip, changed, by path.

    The ledger reads a file's sites and cells at the synced tip, so a ticket on such a file would start from other
    lines. A merge counts for a file only where it differs from each of its parents.
    """
    if isinstance(base, NoUnitBranch):
        return {}
    changes: dict[str, UnsyncedChange] = {}
    commit, first = "", False
    # Newest first. `-z` ends the `\x01<commit>` field and each path with NUL, and puts one newline before only the
    # first path of a commit, so only that path loses one leading newline.
    for field in git(repo, "log", "-z", "--format=%x01%H", "--name-only", "--no-renames", "--diff-merges=dense-combined",
                     f"{tip}..{base.commit}").split("\0"):
        if field.startswith("\x01"):
            commit, first = field[1:], True
        elif field:
            path = field.removeprefix("\n") if first else field
            first = False
            _ = changes.setdefault(path, UnsyncedChange(path, commit))
    return changes


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
             | MergeStyleBranchFirst | berth_board.BoardUnavailable)


def style_document(repo: Path) -> Path:
    standing = standing_style_production(repo)
    if isinstance(standing, StandingStyleProduction):
        return standing.doc
    raise style_ledger.LedgerError(f"no standing style production: {standing.slug}")


def busy_files(repo: Path, doc: Path, ledger: style_ledger.Ledger, unit: str, board: berth_board.Board) -> BusyFiles:
    holders = unit_file_holders(repo, doc, ledger, unit)
    # Worktree changes and reservations share this one busy set for own and borrowed work.
    # Expand every other open ticket with the ledger's no-overlap proof, rather than
    # maintaining a selection-specific approximation of widened paths and references.
    busy = {path for paths in holders.values() for path in paths}
    for owner in holders:
        if owner == unit:
            continue
        for ticket in ledger.ticket_open(owner):
            commits: set[str] = set()
            # Coverage footprints contain recorded paths only; they need no source trees.
            if ticket["kind"] == "proposal":
                commits.update((synced_tip(ledger), *(commit for commit in (ticket["base"], ticket["checkpoint"]) if commit)))
                if ticket["branch"]:
                    commits.add(git(repo, "rev-parse", "--verify", f"{ticket['branch']}^{{commit}}").strip())
            # The ledger owns this proof; keep selection and mutation guards on that implementation.
            busy.update(ledger._pipeline_footprint(ticket, frozenset(commits)))  # pyright: ignore[reportPrivateUsage]
    return BusyFiles(board, frozenset(busy))


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
    try:
        old, new = style_footprint.token_spans(before), style_footprint.token_spans(after)
    except style_footprint.UnreadableTokenSpans:
        return False
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
    """No folder above the module file holds a `lib.rs` or `main.rs`."""


@dataclass(frozen=True)
class Library:
    """The library target of the renamed file's crate, by the name that paths in other crates start with."""

    name: str


@dataclass(frozen=True)
class NoLibrary:
    """The renamed file's crate has no library target the gate can name, so no path outside the crate names its modules."""


@dataclass(frozen=True)
class PathAttributes:
    """The `mod` keywords whose attributes hold a `path` word, the ones whose file the scan reads with certainty, and the
    string of each one whose path is the module file."""

    keywords: frozenset[int]
    resolved: frozenset[int]
    literals: Mapping[int, int]

    @property
    def uncertain(self) -> frozenset[int]:
        """The `mod` keywords whose `path` word names a file the scan cannot read with certainty."""
        return self.keywords - self.resolved


@dataclass(frozen=True)
class UncertainModulePath:
    """The file has a `path` word whose file the scan cannot read with certainty, so it may declare a renamed module in
    a form the rename gate cannot rename."""


@dataclass(frozen=True)
class ModuleBinding:
    """An item, import or alias that binds the renamed module's name in one inline module scope of a file."""

    scope: tuple[InlineModule, ...]
    renamed: bool


@dataclass(frozen=True)
class GlobImport:
    """A `*` import in one inline module scope, and the module path it imports from."""

    scope: tuple[InlineModule, ...]
    source: tuple[str, ...] | style_footprint.UnresolvedPath


# The item keywords whose name is in the type namespace, where a module name lives.
TYPE_ITEM_KEYWORDS = frozenset({"mod", "struct", "enum", "union", "trait", "type"})
# The words after a `use` leaf.
USE_LEAF_ENDS = frozenset({";", ",", "}", "as"})
# A path in a comment that starts at `crate`, as in an intra-doc link `[`crate::ledger::lock`]`.
CRATE_PATH = re.compile(r"(?<![\w:$])crate(?:::[A-Za-z_][A-Za-z0-9_]*)+")
# A `path` word, then `=` and a string the scan cannot read with certainty: a raw string, or a string with an escape,
# with whitespace or comments between them. Such a string can name any module file without its name as a word.
UNREAD_PATH_STRING = re.compile(r'(?<![\w])path(?:\s|/\*.*?\*/|//[^\n]*)*=(?:\s|/\*.*?\*/|//[^\n]*)*(?:r#*"|"[^"\\]*\\)',
                                re.DOTALL)


def crate_root(repo: Path, file: str) -> PurePosixPath | NoCrateRoot:
    """The nearest folder above the module file that holds a crate root file."""
    for folder in PurePosixPath(file).parents:
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


def module_name(file: str) -> str:
    """The name a `mod` line declares the module file by: its folder's name for a `mod.rs`, else its stem."""
    path = PurePosixPath(file)
    return path.parent.name if path.name == "mod.rs" else path.stem


@dataclass(frozen=True)
class DeclarationScan:
    """One Rust file at path read at a commit as its `plain_words`, and the module file whose `mod` declarations the
    scan finds in it."""

    path: str
    words: tuple[str, ...]
    module: style_footprint.CrateModule
    parent: tuple[str, ...]
    file: str

    def declares(self) -> bool:
        """Whether a `mod` item of the file declares the module file, or can: a `mod <name>;` line that resolves to it,
        a `#[path]` string that names it, or a `path` word whose file the scan cannot read with certainty (a `path`
        attribute in an inline module, a `cfg_attr` that holds `path`, or a `path` on an inline module, which mounts a
        folder for the modules in it)."""
        starts = style_footprint.code_item_starts(self.words)
        modules = inline_modules(self.words, starts)
        paths = self.path_attributes(starts, modules)
        name = module_name(self.file)
        return bool(paths.literals or paths.uncertain) or any(
            word == name and self.mod_line(index, starts, modules, paths) for index, word in enumerate(self.words))

    def scope_module(self, scope: tuple[InlineModule, ...]) -> style_footprint.CrateModule:
        if isinstance(self.module, style_footprint.OutsideCrate):
            return self.module
        return self.module + tuple(module.name for module in scope)

    def mod_line(self, index: int, starts: frozenset[int], modules: tuple[InlineModule, ...], paths: PathAttributes) -> bool:
        """Whether the word at index is the name in the module file's `mod <name>;` line.

        A line with a `path` attribute declares the module only when the attribute's string names its file.
        """
        if not (self.words[index - 1:index] == ("mod",) and index - 1 in starts
                and self.words[index + 1:index + 2] == (";",)):
            return False
        if index - 1 in paths.keywords:
            return index - 1 in paths.literals
        return self.scope_module(enclosing(modules, index)) == self.parent

    def path_attributes(self, starts: frozenset[int], modules: tuple[InlineModule, ...]) -> PathAttributes:
        """The `path` attributes of the file's `mod` items.

        A `#[path = "<string>"]` names the module file when it is the only `path` word of a `mod` at an item start
        outside every inline module, and its string, read from the file's own folder, is the module file and ends with
        its name. A string with an escape, or a `path` word in any other form, names no file the gate can read. The
        scan reads the file of a `mod <name>;` with a `path` attribute in that form with certainty.
        """
        words, name = self.words, PurePosixPath(self.file).name
        spans = dict(style_footprint.attribute_spans(words))
        attributes = style_footprint.decorated_items(words)
        keywords = frozenset(owner for owner, words_of in attributes.items() if words[owner] == "mod" and "path" in words_of)
        resolved: set[int] = set()
        literals: dict[int, int] = {}
        for start, owner in style_footprint.attribute_owners(words).items():
            named = words[start + 4][1:-1] if spans[start] == start + 6 else ""
            readable = (owner in keywords and owner in starts and attributes[owner].count("path") == 1
                        and not enclosing(modules, owner)
                        and words[start + 1:start + 5] == ("[", "path", "=", f'"{named}"') and "\\" not in named)
            if readable and words[owner + 2:owner + 3] == (";",):
                resolved.add(owner)
            if (readable and named.rsplit("/", 1)[-1] == name
                    and posixpath.normpath(str(PurePosixPath(self.path).parent / named)) == self.file):
                literals[owner] = start + 4
        return PathAttributes(keywords, frozenset(resolved), MappingProxyType(literals))


@dataclass(frozen=True)
class RenamedModuleScan(DeclarationScan):
    """A declaration scan of a module file rename's old file that also finds the module's references in it."""

    new: str
    library: Library | NoLibrary

    @property
    def moved(self) -> FileRename:
        return FileRename(self.file, self.new)

    def renames(self) -> Mapping[int, str] | UncertainModulePath:
        """The words that name the renamed module, each with its new word: its `mod` line, the path segments that name
        it, and the `#[path]` string that names its file.

        A word is a site only when the scan is certain; the gate keeps every other word unchanged. A file with a `path`
        word whose file the scan cannot read with certainty (`PathAttributes.uncertain`) has no sites the gate can prove.
        """
        words, stem = self.words, self.moved.old_stem
        starts = style_footprint.code_item_starts(words)
        modules = inline_modules(words, starts)
        paths = self.path_attributes(starts, modules)
        if paths.uncertain:
            return UncertainModulePath()
        trees = style_footprint.use_trees(words, starts)
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
                    globs.append(GlobImport(scope, style_footprint.resolved_module(trees.prefixes[index],
                                                                                   self.scope_module(scope))))
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

    def names_parent(self, prefix: tuple[str, ...], module: style_footprint.CrateModule) -> bool:
        """Whether a path prefix read in module names the renamed module's parent.

        A plain prefix such as `ledger` names it from a child of module inside the crate, and from the library's name in
        a file outside the crate.
        """
        if prefix and prefix[0] in style_footprint.PATH_ANCHORS:
            return style_footprint.resolved_module(prefix, module) == self.parent
        if not prefix or style_footprint.PATH_ANCHORS & set(prefix):
            return False
        if isinstance(module, style_footprint.OutsideCrate):
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

    code: tuple[style_footprint.CodePiece, ...]
    module_sites: bool

    def matches(self, after: str) -> bool:
        """Whether the worktree text has this code and these comment lines in canonical order."""
        try:
            spans = style_footprint.token_spans(after)
        except style_footprint.UnreadableTokenSpans:
            return False
        return (len(spans) == len(style_footprint.tokens(after))
                and self.code == style_footprint.canonical_code(
                    tuple(span.token.value for span in spans),
                    style_footprint.comment_gaps(after, spans, lambda line: line)))


@dataclass(frozen=True)
class NoRenamedText:
    """The HEAD text has a token the gate cannot read, or a renamed file is not in a crate whose modules it can name."""


def plain_words(spans: tuple[style_footprint.TokenSpan, ...]) -> tuple[str, ...]:
    """The words of the spans with each raw identifier read as its plain word, so `#[r#path = ...]` and `mod r#name;`
    read as `path` and `name`."""
    return tuple(span.token.value.removeprefix("r#") if span.token.kind == "identifier" else span.token.value
                 for span in spans)


def renamed_text(repo: Path, path: str, before: str, moves: tuple[FileRename, ...],
                 pairs: tuple[ApprovedRename, ...]) -> RenamedText | NoRenamedText | UncertainModulePath:
    """The HEAD text of the Rust file at path with the edits of the approved renames made.

    Each module site of an approved file rename takes its new word, then each qualified reference of an approved rename,
    which reads its parent from the HEAD words. The scan reads the file's `plain_words`, and a raw site keeps its `r#`,
    so `mod r#lock;` takes `mod r#mutation_lock;`. In comments, each `crate` path to a renamed module is renamed.
    """
    try:
        old = style_footprint.token_spans(before)
    except style_footprint.UnreadableTokenSpans:
        return NoRenamedText()
    if len(old) != len(style_footprint.tokens(before)):
        return NoRenamedText()
    words, plain = tuple(span.token.value for span in old), plain_words(old)
    renamed = list(words)
    scans: list[RenamedModuleScan] = []
    for moved in moves:
        root = crate_root(repo, moved.old)
        if isinstance(root, NoCrateRoot):
            return NoRenamedText()
        module = style_footprint.crate_module(moved.old, root)
        if isinstance(module, style_footprint.OutsideCrate):
            return NoRenamedText()
        scans.append(RenamedModuleScan(path, plain, style_footprint.crate_module(path, root), module[:-1], moved.old,
                                       moved.new, crate_library(repo, root)))
        sites = scans[-1].renames()
        if isinstance(sites, UncertainModulePath):
            return sites
        for index, word in sites.items():
            renamed[index] = words[index].removesuffix(plain[index]) + word
    comments = style_footprint.comment_gaps(before, old, lambda line: renamed_comment(line, tuple(scans)))
    module_sites = tuple(renamed) != words or comments != style_footprint.comment_gaps(before, old, lambda line: line)
    use_parents = use_reference_parents(words)
    for index, word in enumerate(words):
        for pair in pairs:
            if word == pair.old and approved_reference_qualifier(words, index, use_parents, pair):
                renamed[index] = pair.new
    return RenamedText(style_footprint.canonical_code(tuple(renamed), comments), module_sites)


def declares_module(repo: Path, path: str, text: str, file: str) -> bool:
    """Whether the Rust file at path, read as text, declares the module file, or has a token the scan cannot read, or
    the module file is not in a crate whose modules it can name."""
    try:
        spans = style_footprint.token_spans(text)
    except style_footprint.UnreadableTokenSpans:
        return True
    root = crate_root(repo, file)
    if len(spans) != len(style_footprint.tokens(text)) or isinstance(root, NoCrateRoot):
        return True
    module = style_footprint.crate_module(file, root)
    if isinstance(module, style_footprint.OutsideCrate):
        return True
    return DeclarationScan(path, plain_words(spans), style_footprint.crate_module(path, root), module[:-1], file).declares()


def approved_module_file(repo: Path, path: str, moves: tuple[FileRename, ...], pairs: tuple[ApprovedRename, ...]) -> bool:
    """Whether a Rust file outside the findings changes only the `mod` lines, `#[path]` strings and module paths of
    approved file renames, and the qualified references of approved renames.

    The file must name a renamed module, and its worktree text must be its renamed_text. A file that names no renamed
    module is judged by approved_reference_file alone, and a file with a `path` attribute the scan cannot read with
    certainty (`UncertainModulePath`) is not approved.
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


def binding_pairs(findings: tuple[style_ledger.HeldFinding, ...]) -> Mapping[str, tuple[BindingRename, ...]]:
    """The approved local name pairs grouped by their original file."""
    paths: dict[str, list[BindingRename]] = defaultdict(list)
    for finding in findings:
        rename = binding_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
        if isinstance(rename, BindingRename):
            paths[finding.path].append(rename)
    return {path: tuple(renames) for path, renames in paths.items()}


def binding_worktree_checks(repo: Path, renames: Mapping[str, tuple[BindingRename, ...]],
                            base: str) -> Mapping[str, Mapping[BindingRename, AppliedCheck]]:
    """Check each file once, from the ticket's saved base (or HEAD before its base is saved)."""
    if not renames:
        return {}
    original = style_footprint.RevisionTree(repo, base or "HEAD")
    checks: dict[str, Mapping[BindingRename, AppliedCheck]] = {}
    for path, pairs in renames.items():
        edited = worktree_file(repo, path)
        if path not in original.paths or not isinstance(edited, bytes):
            checks[path] = {pair: NoAppliedCheck("rename") for pair in pairs}
            continue
        checks[path] = binding_rename_check(path, original.text(path), edited.decode(errors="replace"), pairs).checks
    return checks


@dataclass(frozen=True)
class ApprovedTicket:
    """The findings of a proposal ticket whose decision is approved, their approved file renames, and the paths that
    the findings and renames allow."""

    findings: tuple[style_ledger.HeldFinding, ...]
    moves: tuple[FileRename, ...]
    allowed: frozenset[str]


def approved_ticket(
    ledger: style_ledger.Ledger, ticket_id: str, origin: style_ledger.ApprovedProposal,
) -> ApprovedTicket:
    """Read the ticket's decision; raise unless it is open, approved for the ticket's unit, and still holds the ticket's
    filed findings. The gate and `widen` both read approval here."""
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
    allowed = frozenset({finding.path for finding in findings} | {moved.new for moved in moves})
    return ApprovedTicket(findings, moves, allowed)


def widen(repo: Path, ticket_id: str, paths: tuple[str, ...], reason: str, by: str) -> None:
    """Allow paths outside the findings of an approved proposal ticket, and print the ticket's widened paths."""
    with style_ledger.open_ledger(repo) as ledger:

        def allowed() -> frozenset[str]:
            origin = ledger.ticket_origin(ticket_id)
            if isinstance(origin, style_ledger.SelectedByCoverage):
                raise style_ledger.LedgerError(f"{ticket_id}: only a proposal ticket widens")
            return approved_ticket(ledger, ticket_id, origin).allowed

        for widened in ledger.ticket_widen(ticket_id, paths, reason, by, allowed):
            print(style_ledger.widened_line(widened))


def gate(repo: Path, ticket_id: str) -> int:
    """Exit 0 when the change may commit, 1 for a review result, 2 when the gate could not run."""
    try:
        with style_ledger.open_ledger(repo) as ledger:
            ledger.check_ticket_pipeline(ticket_id)
            origin = ledger.ticket_origin(ticket_id)
            if isinstance(origin, style_ledger.SelectedByCoverage):
                # The worktree is what the checkpoint commits: a removed cell's path is looked for there, its removal in HEAD.
                missing = ledger.unjudged_ticket_cells(ticket_id, "HEAD", lambda path: os.path.lexists(repo / path))
                if missing:
                    for cell in missing:
                        print(f"unjudged: {cell.rule} {cell.path}")
                    return 1
                base = coverage_footprint_base(repo, ledger, origin.base)
                result = subprocess.run([sys.executable, str(SCRIPTS / "style/style_footprint.py"), "check",
                                         "--repo", str(repo), "--base", base, "--worktree"], check=False)
                if result.returncode != 0:
                    return 2 if result.returncode == 2 else 1
                return format_check(repo)
            approved = approved_ticket(ledger, ticket_id, origin)
            findings, moves, allowed = approved.findings, approved.moves, set(approved.allowed)
            # A widened path is allowed as it is; the rename and move checks below do not read it.
            outside = changed_files(repo) - allowed - set(origin.widened)
            pairs = approved_rename_pairs(findings)
            bindings = binding_pairs(findings)
            if outside:
                outside -= mechanical_files(repo, frozenset(PurePosixPath(finding.rule.partition(":")[2]).stem
                                                            for finding in findings), bindings)
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
            binding_checks = binding_worktree_checks(repo, bindings, origin.base)
            unapplied = tuple(finding for finding in findings if isinstance(
                binding_checks[finding.path][rename] if isinstance(rename := binding_rename(
                    finding.kind, finding.before, finding.after, finding.line, finding.text), BindingRename)
                else worktree_check(repo, finding), RenameNotApplied))
            if unapplied:
                for finding in unapplied:
                    print(f"review: {finding.finding_id}: {finding.before} -> {finding.after} not applied in {finding.path}")
                return 1
            if format_check(repo):
                return 1
            print("approved proposal")
            return 0
    except (OSError, sqlite3.Error, style_ledger.CheckCouldNotRun, style_footprint.CheckCouldNotRun,
            mechanical_classes.ClassListError) as error:
        print(f"error: {error}")
        return 2
    except ValueError as error:
        print(f"review: {error}")
        return 1


CARGO_FMT_CHECK = ("cargo", "+nightly", "fmt", "--all", "--check", "--verbose", "--message-format", "short",
                   "--", "--verbose")
RUSTFMT_CHECK = ("rustfmt", "+nightly", "--check", "--files-with-diff")
FMT_ERROR = re.compile(r"(?im)^error\b")


@dataclass(frozen=True)
class CargoFormat:
    """What one `cargo +nightly fmt --all --check` run read: every file its module walk visited, and the visited files
    it would change. All paths are resolved."""

    visited: frozenset[Path]
    listed: frozenset[Path]


def format_check(repo: Path) -> int:
    """Print each changed .rs file of the worktree that is not formatted, and return 1 if there is one, else 0.

    `cargo +nightly fmt --all --check` judges each changed file that its module walk visits. Each other changed file
    (an orphan, a file in no workspace package, a file under a `mod` that rustfmt skips) goes to
    `rustfmt +nightly --check` by name with its package's edition. Both sides compare resolved paths, and a refusal
    names each changed path and the command that judged it. The check never writes a file, and a change with no .rs
    file runs neither command."""
    root = repo.resolve()
    changed: defaultdict[Path, list[str]] = defaultdict(list)
    for path in sorted(changed_files(repo)):
        if path.endswith(".rs") and (repo / path).is_file():
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise style_ledger.CheckCouldNotRun(f"{path} resolves to {target}, outside the repo")
            changed[target].append(path)
    if not changed:
        return 0
    cargo = cargo_format(root)
    by_edition: defaultdict[str, list[Path]] = defaultdict(list)
    for target in changed:
        if target not in cargo.visited:
            by_edition[package_edition(root, target.relative_to(root).as_posix())].append(target)
    named = set[Path]().union(*(files_with_diff(root, edition, targets)
                                for edition, targets in sorted(by_edition.items())))
    judges = (("cargo +nightly fmt", cargo.listed), ("rustfmt +nightly", named))
    refusals = sorted((path, judge) for judge, listed in judges for target in listed & changed.keys()
                      for path in changed[target])
    for path, judge in refusals:
        print(f"review: not formatted ({judge}): {path}")
    return 1 if refusals else 0


def format_output(command: list[str], root: Path, label: str) -> tuple[int, list[str]]:
    """The exit code and the stdout lines of a format check run in root.

    Exit 0 passes whatever stderr holds (rustup info lines, config warnings). Exit 1 lists the files with a diff; an
    error line on stderr with it, such as a parse error, is a failure to run, and so is any other exit code."""
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    except OSError as error:
        raise style_ledger.CheckCouldNotRun(f"{label} could not run: {error}") from error
    error = FMT_ERROR.search(result.stderr) if result.returncode == 1 else None
    if result.returncode not in (0, 1) or error:
        detail = result.stderr[error.start():] if error else result.stderr
        raise style_ledger.CheckCouldNotRun(f"{label} failed: "
                                            + (detail.strip() or result.stdout.strip() or f"exit {result.returncode}"))
    return result.returncode, [line for line in result.stdout.splitlines() if line.strip()]


def listed_files(code: int, lines: list[str], label: str) -> frozenset[Path]:
    """The resolved files that a check run lists with a diff: each line that is an absolute .rs path. Every other line
    (verbose notes, config file lines, timings) is not read. Exit 0 lists no file whatever stdout holds; exit 1 must
    list one."""
    if code == 0:
        return frozenset()
    listed = frozenset(Path(line).resolve() for line in lines if line.endswith(".rs") and Path(line).is_absolute())
    if not listed:
        raise style_ledger.CheckCouldNotRun(f"{label} failed: " + ("\n".join(lines) or f"exit {code}"))
    return listed


def cargo_format(root: Path) -> CargoFormat:
    """The files that `cargo +nightly fmt --all --check` visits, from rustfmt's `Formatting <path>` lines, and the
    files it lists with a diff."""
    label = "cargo +nightly fmt --check"
    code, lines = format_output(list(CARGO_FMT_CHECK), root, label)
    visited = frozenset((root / line.removeprefix("Formatting ")).resolve()
                        for line in lines if line.startswith("Formatting "))
    return CargoFormat(visited, listed_files(code, lines, label))


def files_with_diff(root: Path, edition: str, targets: list[Path]) -> frozenset[Path]:
    """The resolved files that `rustfmt +nightly --check` lists for targets, read with edition."""
    label = "rustfmt +nightly --check"
    code, lines = format_output([*RUSTFMT_CHECK, "--edition", edition, *map(str, targets)], root, label)
    return listed_files(code, lines, label)


def package_edition(root: Path, path: str) -> str:
    """The edition cargo gives the package that holds path: the nearest Cargo.toml with a `[package]` table, its
    `edition`, the workspace's `[workspace.package]` edition under `edition.workspace = true`, else 2015."""
    manifests: list[dict[str, object]] = []
    for folder in (root / path).parents:
        if not folder.is_relative_to(root):
            break
        if (folder / "Cargo.toml").is_file():
            try:
                manifests.append(tomllib.loads((folder / "Cargo.toml").read_text()))
            except (UnicodeError, tomllib.TOMLDecodeError) as error:
                raise style_ledger.CheckCouldNotRun(f"{folder / 'Cargo.toml'}: {error}") from error
    for index, manifest in enumerate(manifests):
        package = manifest.get("package")
        if not isinstance(package, dict):
            continue
        edition = cast(dict[str, object], package).get("edition", "2015")
        if isinstance(edition, dict) and cast(dict[str, object], edition).get("workspace") is True:
            edition = workspace_edition(manifests[index:])
        if isinstance(edition, str):
            return edition
        raise style_ledger.CheckCouldNotRun(f"{path}: its package has no edition that rustfmt can take")
    raise style_ledger.CheckCouldNotRun(f"{path} is in no package, so rustfmt has no edition for it")


def workspace_edition(manifests: list[dict[str, object]]) -> object:
    """The `[workspace.package]` edition of the first manifest with a `[workspace]` table, or None."""
    for manifest in manifests:
        workspace = manifest.get("workspace")
        if isinstance(workspace, dict):
            shared = cast(dict[str, object], workspace).get("package")
            return cast(dict[str, object], shared).get("edition") if isinstance(shared, dict) else None
    return None


def mechanical_files(repo: Path, ticket_rules: frozenset[str],
                     renames: Mapping[str, tuple[BindingRename, ...]] = mechanical_classes.NO_BINDING_RENAMES) -> frozenset[str]:
    """The changed files of the worktree that the proof of a class in `mechanical_classes.toml` explains, for a class
    with a rule in ticket_rules (the rule names of the ticket's findings). Such a change needs no approval. A proven
    item move gives the file each item leaves, the file it moves to, the parent file that gains the new `mod` line,
    and each file with a re-pointed path or a use line of the move. A proven lint removal gives each file whose whole
    edit removes stale lint names. A binding proof accounts for every edit at a path using its approved local name
    pairs. A changed file that no proof explains stays outside."""
    changes = style_footprint.changes_between(repo, "HEAD", style_footprint.worktree_commit(repo))
    return mechanical_classes.explained_files(changes, style_footprint.RevisionTree(repo, "HEAD"), ticket_rules, renames)


def synced_tip(ledger: style_ledger.Ledger) -> str:
    return ledger.synced_commit()


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
    # The files where the unit's family waits for a family ranked above it; no ticket takes a cell at one.
    waits: Mapping[str, style_proposals.RankWait]


def _unit_tickets(family: rules.Family, cell: style_ledger.Cell) -> bool:
    return cell.family == family or (family == "modules" and cell.tier == "D")


def _refill_cells(ledger: style_ledger.Ledger, cells: tuple[style_ledger.Cell, ...], base: UnitBase, family: rules.Family,
                  scope: PilotScope, waits: Mapping[str, style_proposals.RankWait]) -> RefillCells:
    """Leave out unchanged held sites with open proposals, and find the unit's cells a ticket at its tip cannot change."""
    held = ledger.latest_held_judgments()
    findings: dict[tuple[str, str], list[style_ledger.HeldFinding]] = defaultdict(list)
    for finding in ledger.held_findings(style_ledger.AllHeldFindings()):
        # A waived, built or withdrawn finding needs no more work at its site.
        if not isinstance(finding.state, style_ledger.Waived | style_ledger.Built | style_ledger.Withdrawn):
            findings[(finding.rule, finding.path)].append(finding)
    tip = synced_tip(ledger)
    result: list[style_ledger.Cell] = []
    for cell in cells:
        key = (cell.rule, cell.path)
        if cell.state in ("open", "stale") and key in held and findings[key]:
            if all(isinstance(finding.state, style_ledger.Filed)
                   and frontmatter(finding.state.proposal).get("status") == ["open"]
                   for finding in findings[key]):
                try:
                    reader = style_proposals.GitProposalReader(ledger)
                    previous = reader.blob(held[key], cell.path)
                    current = reader.blob(tip, cell.path)
                except style_ledger.GitReadFailed:
                    raise
                except style_ledger.LedgerError:
                    pass
                else:
                    if previous and previous == current:
                        continue
        result.append(cell)
    own = (cell for cell in result if _unit_tickets(family, cell) and crate_in_scope(scope, cell.crate))
    judged = ledger.judged_at_base(own, base.commit) if isinstance(base, UnitBranchTip) else ()
    return RefillCells(tuple(result), frozenset(judged), waits)


def candidate_tickets(repo: Path, ledger: style_ledger.Ledger, busy: BusyFiles,
                      family: rules.Family, scope: PilotScope = _ALL_CRATES,
                      family_scopes: Mapping[rules.Family, PilotScope] = _ALL_FAMILY_SCOPES,
                      *, base: UnitBase, first_files: frozenset[str] = _NO_FIRST_FILES) -> tuple[TicketSelection, ...]:
    cells = tuple(ledger.cells())
    waits = style_proposals.ranked_work(ledger, cells, family_scopes).waits(family)
    return _tickets_from(repo, ledger, busy, family, scope, family_scopes,
                         _refill_cells(ledger, cells, base, family, scope, waits), first_files)


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
                    and cell not in refill.judged_at_base and cell.path not in refill.waits
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


def mechanical_class_lines() -> list[str]:
    """The unit's lines for the classes of `mechanical_classes.toml`: the apply rule, then one line per class."""
    return ["Apply a finding of a mechanical class without asking only when `style_footprint.py check` finds the "
            + "whole change mechanical. Hold it when the change adds an allow or changes code that the proof does not "
            + "explain:",
            *(f"- `{entry.name}` ({', '.join(f'`{rule}`' for rule in entry.rules)}): {entry.summary}"
              for entry in mechanical_classes.classes())]


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
    removed_line = "Record a cell whose file this ticket deletes with `--verdict removed`; the ledger finds the commit that deleted it."
    record_lines = ([f"Record exactly these cells, each with `style_ledger.py record --ticket {ticket_id} --rule <rule> --path <file>`; "
                     + "the ledger takes its pinned rule blob:"] + [f"- `{cell.rule}` · `{cell.path}`" for cell in proposal_cells] + [removed_line]
                    if proposal_cells else ["Record no cell: the ledger takes no cell of this ticket."]) if approved else [
        f"Record every judged cell with `style_ledger.py record --ticket {ticket_id}`; the ledger takes its pinned rule blob.",
        removed_line]
    files: list[str] = []
    for path in ticket["files"]:
        line = f"- `{path}`"
        if ticket["kind"] == "read":
            windows = file_windows[path]
            line += " — " + ("lines " + ", ".join(f"{window.start}-{window.end}" for window in windows)
                              if windows else "empty file")
        files.append(line)
    # A path that `widen` added is the ticket's work too; the line names who added it and why.
    files.extend(f"- {style_ledger.widened_line(widened)}" for widened in ticket["widened"])
    authorization = (
        f"This ticket builds the approved proposal `{ticket['proposal']}`: its approved findings are this ticket's work. "
        + "Read the issue for its sites and before/after changes. Hold every other non-mechanical finding. Package verification remains required."
        if approved else "Every structure finding is held as a proposal, except a finding of a mechanical class below. "
        + "Apply only mechanical findings from other tickets."
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
        *([f"When a finding's file, or a file its change edits, is reserved, unmerged or dirty, yield the finding's file with "
           + f"`style_ledger.py ticket yield --repo . --id {ticket_id} --path <finding file>`: its findings stay filed for a "
           + "later ticket, and the gate skips them."] if approved else []),
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
        *mechanical_class_lines(),
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


def write_phase(ledger: style_ledger.Ledger, ticket: style_ledger.TicketRecord, plan: Path, text: str,
                borrow_reason: str = "") -> None:
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
    own_family = rules.style_family(ticket["unit"])
    if ticket["family"] != own_family:
        previous = next((match.group(0) for match in PHASE.finditer(text) if match.group("id") == ticket["id"]), "")
        saved = re.search(r"(?m)^Borrowed from .+$", previous)
        line = (saved.group(0) if saved else
                f"Borrowed from {ticket['family']}: {own_family} has no buildable work ({borrow_reason or 'no available work'}).")
        phase = phase.replace("\n", "\n" + line + "\n", 1)
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
    families: tuple[FamilyIdleReason, ...] = field(default=(), compare=False)


def made_before_decisions(ticket_id: str) -> str:
    """Why a proposal ticket with no finding ids cannot build, and what to do."""
    return f"{ticket_id}: made before decisions; drop it with style_ledger.py ticket drop, then run migrate"


class EditedFiles:
    """The files that each approved finding's change edits, as the gate lets it edit them, read once per refill at the
    synced tip and at the unit base, so a reference that the unit's branch added after the sync counts. The tip is read
    on first use, so a refill that needs no footprint reads no tip.

    A file rename edits its own file, its new file and each Rust file whose module sites `renamed_text` renames at
    either commit; a file that names the old module but that `renamed_text` cannot read, or whose `path` attribute it
    cannot read with certainty, counts as edited, and the second makes the finding wait (`uncertain`). A qualified
    rename edits its own file and each `.rs` and `.md` file that holds its old name as a word at either commit, the
    files `approved_reference_file` can take. A restructure of a `.rs` file edits its own file and each Rust file whose
    `mod` line or `#[path]` string declares it at either commit, as a move of a test file into its owner removes the
    owner's declaration; a file that names the module but that the scan cannot read, or whose `path` attribute it cannot
    read with certainty, counts as edited. Any other finding edits its own file only.
    """

    def __init__(self, repo: Path, ledger: style_ledger.Ledger, base: UnitBase) -> None:
        self.repo: Path = repo
        self.ledger: style_ledger.Ledger = ledger
        self.base: UnitBase = base
        self.tip: str | None = None
        # The blob of each named path at each commit, and the text of each blob.
        self.blobs: dict[tuple[str, str], str] = {}
        self.texts: dict[str, str] = {}
        # The renamed_text of each file rename in each file that names its old module.
        self.readings: dict[FileRename, tuple[tuple[str, RenamedText | NoRenamedText | UncertainModulePath], ...]] = {}

    def of(self, finding: style_ledger.HeldFinding) -> tuple[str, ...]:
        """The finding's own file first, then each other file its change edits."""
        moved = file_rename(finding.kind, finding.path, finding.after)
        if isinstance(moved, FileRename):
            edited = (path for path, renamed in self.module_readings(moved)
                      if not isinstance(renamed, RenamedText) or renamed.module_sites)
            return tuple(dict.fromkeys((finding.path, moved.new, *edited)))
        pair = approved_rename(finding.kind, finding.before, finding.after)
        if isinstance(pair, ApprovedRename):
            named_files = (path for commit in self.commits()
                           for path in self.ledger.files_naming(commit, pair.old, ("*.rs", "*.md")) if path != finding.path)
            return (finding.path, *dict.fromkeys(named_files))
        if finding.kind == "restructure" and finding.path.endswith(".rs"):
            owners = (path for path, text in self.declaring(module_name(finding.path))
                      if path != finding.path and declares_module(self.repo, path, text, finding.path))
            return (finding.path, *dict.fromkeys(owners))
        return (finding.path,)

    def uncertain(self, finding: style_ledger.HeldFinding) -> tuple[str, ...]:
        """For a file rename, each file at either commit that names the old module and has a `path` word the rename gate
        cannot read with certainty (`UncertainModulePath`), so the gate cannot prove the rename's edit to it."""
        moved = file_rename(finding.kind, finding.path, finding.after)
        if not isinstance(moved, FileRename):
            return ()
        return tuple(dict.fromkeys(path for path, renamed in self.module_readings(moved)
                                   if isinstance(renamed, UncertainModulePath)))

    def of_ticket(self, ticket: style_ledger.TicketRecord) -> tuple[str, ...]:
        """The ticket's files, and for a proposal ticket its widened paths and each file its findings' changes edit."""
        origin = self.ledger.ticket_origin(ticket["id"])
        if isinstance(origin, style_ledger.SelectedByCoverage):
            return tuple(ticket["files"])
        return (*ticket["files"], *origin.widened, *(path for finding in self.ticket_findings(origin)
                                                     for path in self.of(finding)))

    def uncertain_of_ticket(self, ticket: style_ledger.TicketRecord) -> tuple[str, ...]:
        """The `uncertain` files of each finding of a proposal ticket; none for a coverage ticket."""
        origin = self.ledger.ticket_origin(ticket["id"])
        if isinstance(origin, style_ledger.SelectedByCoverage):
            return ()
        return tuple(path for finding in self.ticket_findings(origin) for path in self.uncertain(finding))

    def ticket_findings(self, origin: style_ledger.ApprovedProposal) -> tuple[style_ledger.HeldFinding, ...]:
        return tuple(finding for finding in self.ledger.proposal_findings(origin.issue)
                     if finding.finding_id in origin.finding_ids)

    def commits(self) -> tuple[str, ...]:
        """The synced tip, read once, and the unit base when it is another commit."""
        if self.tip is None:
            self.tip = synced_tip(self.ledger)
        if isinstance(self.base, UnitBranchTip) and self.base.commit != self.tip:
            return (self.tip, self.base.commit)
        return (self.tip,)

    def naming(self, name: str) -> tuple[tuple[str, str], ...]:
        """Each Rust file that holds name as a word at either commit, with its text there, read by one batch. A path
        with the same blob at both commits is listed once."""
        named = {commit: self.ledger.files_naming(commit, name) for commit in self.commits()}
        self.read(named)
        versions = dict.fromkeys((path, self.blobs[commit, path]) for commit, paths in named.items() for path in paths)
        return tuple((path, self.texts[blob]) for path, blob in versions)

    def declaring(self, name: str) -> tuple[tuple[str, str], ...]:
        """Each Rust file at either commit that may declare a module named name, with its text there: one that holds
        name as a word, or a `path` string the scan cannot read with certainty (`UNREAD_PATH_STRING`), which no grep
        for the name finds."""
        unread = (entry for entry in self.naming("path") if UNREAD_PATH_STRING.search(entry[1]))
        return tuple(dict.fromkeys((*self.naming(name), *unread)))

    def read(self, named: Mapping[str, tuple[str, ...]]) -> None:
        """Find the blob of each path at its commit by one `git ls-tree` for each commit, then read each blob not read
        yet by one `git cat-file --batch`."""
        unread: dict[str, str] = {}
        for commit, paths in named.items():
            unlisted = [path for path in paths if (commit, path) not in self.blobs]
            if not unlisted:
                continue
            # Each entry reads `<mode> blob <oid>\t<path>`.
            for entry in git(self.repo, "ls-tree", "-z", commit, "--", *unlisted).split("\0"):
                if entry:
                    info, path = entry.split("\t", 1)
                    self.blobs[commit, path] = info.split()[2]
            if missing := sorted(path for path in unlisted if (commit, path) not in self.blobs):
                raise style_ledger.CheckCouldNotRun(f"git ls-tree {commit}: no blob for {', '.join(missing)}")
            for path in unlisted:
                blob = self.blobs[commit, path]
                if blob not in self.texts and blob not in unread.values():
                    unread[f"{commit}:{path}"] = blob
        for label, content in style_ledger.blob_contents(self.repo, unread):
            self.texts[unread[label]] = content.decode(errors="replace")

    def module_readings(self, moved: FileRename) -> tuple[tuple[str, RenamedText | NoRenamedText | UncertainModulePath], ...]:
        """`renamed_text` of moved in each Rust file at either commit that `declaring` gives for its old stem, the renamed
        file included, scanned once for each file version and kept for the refill.

        A file outside the crate's root folder names the module only through the library's name or a `path` attribute
        that names the old file, so a text that holds neither name, and no `path` string the scan cannot read, is not
        scanned.
        """
        if moved not in self.readings:
            root = crate_root(self.repo, moved.old)
            library = crate_library(self.repo, root) if isinstance(root, PurePosixPath) else NoLibrary()
            self.readings[moved] = tuple(
                (path, renamed_text(self.repo, path, text, (moved,), ())) for path, text in self.declaring(moved.old_stem)
                if not (isinstance(root, PurePosixPath)
                        and isinstance(style_footprint.crate_module(path, root), style_footprint.OutsideCrate)
                        and PurePosixPath(moved.old).name not in text
                        and not (isinstance(library, Library) and library.name in text)
                        and not UNREAD_PATH_STRING.search(text)))
        return self.readings[moved]


def _unbuilt_findings(ledger: style_ledger.Ledger, issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
    """The decision's filed findings that no build ticket holds."""
    return tuple(finding for finding in ledger.proposal_findings(issue)
                 if isinstance(finding.state, style_ledger.Filed) and isinstance(finding.build_ticket, style_ledger.NoBuildTicket))


def _rank_waits(ledger: style_ledger.Ledger, ticket: style_ledger.TicketRecord, family: rules.Family,
                waits: Mapping[str, style_proposals.RankWait], scopes: Mapping[rules.Family, PilotScope], tip: str) -> tuple[str, ...]:
    """Why the family rank holds a queued ticket: each file with open higher-rank work, and each finding judged at an
    older file where a family ranked above has a unit."""
    exemptions = style_proposals.RankExemptions(ledger, tip)
    findings = ledger.named_held(ticket["finding_ids"])
    if any(not isinstance(finding.state, style_ledger.Filed) for finding in findings):
        return ("a queued finding is no longer filed",)
    held = tuple(f"{path} has open {waits[path].higher} work" for path in ticket["files"] if path in waits
                 and (not findings or any(f.path == path and not exemptions.allows(f, family) for f in findings)))
    if not ticket["finding_ids"] or not style_proposals.ranked_below_unit(family, ticket["crate"], scopes):
        return held
    return held + tuple(f"{finding.finding_id} {finding.path} changed since it was judged"
                        for finding in findings
                        if not style_proposals.judged_at_current_file(ledger, finding, tip))


@dataclass(frozen=True)
class NoCheckpointBlock:
    """No waiting checkpoint blocks this selection."""


_NO_CHECKPOINT_BLOCK = NoCheckpointBlock()


@dataclass(frozen=True)
class WaitingCheckpoint:
    ticket_id: str
    commit: str
    files: frozenset[str]
    crates: tuple[str, ...]

    def protected(self, files: frozenset[str]) -> frozenset[str]:
        return style_ledger.protected_ticket_files(files, self.crates)

    def check(self, files: tuple[str, ...]) -> WaitingForMerge | NoCheckpointBlock:
        if not files:
            raise style_ledger.LedgerError("empty selected footprint")
        selected = frozenset(files)
        # Build inputs protect source work, but two source tickets do not edit their shared Cargo.lock.
        overlap = (self.files & self.protected(selected)) | (selected & self.protected(self.files))
        if overlap:
            return WaitingForMerge(self.ticket_id, self.commit, "overlap: " + ", ".join(sorted(overlap)))
        return NoCheckpointBlock()


def waiting_checkpoint(ledger: style_ledger.Ledger, ticket: style_ledger.TicketRecord,
                       branch: str) -> WaitingCheckpoint:
    checkpoint = git(ledger.repo, "rev-parse", "--verify", f"{ticket['checkpoint']}^{{commit}}").strip()
    base = unit_base(ledger.repo, branch)
    if not isinstance(base, UnitBranchTip) or base.commit != checkpoint:
        raise style_ledger.LedgerError("unit branch is not at the waiting checkpoint")
    edits = EditedFiles(ledger.repo, ledger, UnitBranchTip(checkpoint))
    origin = ledger.ticket_origin(ticket["id"])
    if isinstance(origin, style_ledger.ApprovedProposal):
        if not origin.finding_ids or len(edits.ticket_findings(origin)) != len(origin.finding_ids):
            raise style_ledger.LedgerError("waiting findings cannot be read")
    if edits.uncertain_of_ticket(ticket):
        raise style_ledger.LedgerError("waiting footprint has an uncertain module route")
    original = EditedFiles(ledger.repo, ledger, UnitBranchTip(ticket["base"]))
    if not ticket["base"] or original.uncertain_of_ticket(ticket):
        raise style_ledger.LedgerError("waiting base footprint cannot be proved")
    files = frozenset((*edits.of_ticket(ticket), *original.of_ticket(ticket)))
    if not files:
        raise style_ledger.LedgerError("empty waiting footprint")
    crates = tuple(sorted({member.directory for commit in edits.commits()
                           for member in coverage_report.workspace(ledger, commit)}, key=len, reverse=True))
    work = WaitingCheckpoint(ticket["id"], checkpoint, files, crates)
    _ = work.protected(files)
    return work


def checkpoint_selection(work: WaitingCheckpoint | NoCheckpointBlock, ledger: style_ledger.Ledger,
                         changes: MainChanges | UnknownMainChanges, files: tuple[str, ...],
                         findings: tuple[style_ledger.HeldFinding, ...] = ()) -> WaitingForMerge | NoCheckpointBlock:
    if isinstance(work, NoCheckpointBlock):
        return work
    if isinstance(changes, MainChanges) and changes.paths and findings:
        edits = EditedFiles(ledger.repo, ledger, UnitBranchTip(changes.pushed_tip))
        if any(edits.uncertain(finding) for finding in findings):
            raise style_ledger.LedgerError("selected footprint at main is uncertain")
        files = (*files, *(path for finding in findings for path in edits.of(finding)))
    return work.check(files)


def projected_follow_findings(reading: style_ledger.DeclarationReading) -> dict[str, style_ledger.HeldFinding]:
    """The kept finding rows a successful follow would write, without changing the ledger."""
    findings: dict[str, style_ledger.HeldFinding] = {}
    for outcome in reading.outcomes:
        finding = outcome.finding
        if isinstance(outcome, (style_ledger.DeclarationRemoved, style_ledger.DeclarationRenamed)):
            continue
        if isinstance(outcome, style_ledger.DeclarationMoved):
            finding = replace(finding, path=outcome.path, line=outcome.line,
                              follow=style_ledger.FollowedDeclaration(finding.path, finding.line, reading.tip))
        elif isinstance(outcome, style_ledger.FindingSite) and isinstance(outcome.evidence, style_ledger.UnchangedSite):
            finding = replace(finding, path=outcome.evidence.path, line=outcome.evidence.line,
                              follow=style_ledger.FollowedDeclaration(finding.path, finding.line, reading.tip))
        findings[finding.finding_id] = finding
    return findings


def _select_phase(
    ledger: style_ledger.Ledger, unit: str, branch: str, plan: Path, repo: Path,
    doc: Path, text: str, scope: PilotScope, busy: BusyFiles, first_files: frozenset[str],
    definitions: Mapping[str, rules.Rule],
    changes: MainChanges | UnknownMainChanges,
    predecessor: WaitingCheckpoint | NoCheckpointBlock = _NO_CHECKPOINT_BLOCK,
    *, family: rules.Family,
    selection: FamilySelection = FamilySelection.OWN, borrow_reason: str = "",
) -> BuildableFindings | PhaseWritten | NoAvailableWork | WaitingForStructure | MergeStyleBranchFirst | WaitingForSync | WaitingForMerge:
    queued: list[style_ledger.TicketRecord] = []
    for ticket in ledger.queued_tickets(unit) if selection == FamilySelection.OWN else ():
        if ticket["kind"] == "proposal" and not ticket["finding_ids"]:
            print("waiting " + made_before_decisions(ticket["id"]))
        else:
            queued.append(ticket)
    buildable = 0
    refusals: Counter[str] = Counter()
    try:
        folder = Path(production_field(doc.read_text().splitlines(), "Proposals")).expanduser()
    except Refusal:
        decisions: tuple[Issue, ...] = ()
    else:
        if not folder.is_absolute():
            folder = doc.parent.parent / folder
        decision_units = ((unit,) if selection == FamilySelection.OWN else
                          tuple(row.unit for row in unit_worktrees(doc) if rules.style_family(row.unit) == family))
        decisions = tuple(issue for owner in decision_units for issue in style_proposals.approved_decisions(folder, owner))
    unbuilt = {issue.path: _unbuilt_findings(ledger, issue.path) for issue in decisions if issue.value("style_rule") in definitions}
    if selection != FamilySelection.OWN:
        for issue in decisions:
            findings = unbuilt.get(issue.path, ())
            current = tuple(finding for finding in findings if not style_ledger.rule_changed_since_judged(finding, definitions))
            refusals["all stale"] += len(findings) - len(current)
            definition = definitions.get(issue.value("style_rule"))
            unbuilt[issue.path] = current if definition is not None and definition.family == family else ()
            refusals["held by build ticket"] += sum(
                isinstance(finding.state, style_ledger.Filed) and isinstance(finding.build_ticket, style_ledger.BuildTicket)
                for finding in ledger.proposal_findings(issue.path))
        # A family with no eligible approved findings needs no coverage, source or follow scan.
        if not any(unbuilt.values()):
            reason = ", ".join(f"{reason} ({count})" for reason, count in sorted(refusals.items()) if count) or "none approved"
            return BuildableFindings(0, reason) if selection == FamilySelection.COUNT else NoAvailableWork()
    base = unit_base(repo, branch)
    edits = EditedFiles(repo, ledger, base)
    tip = synced_tip(ledger)
    cells = tuple(ledger.cells())
    scopes = family_pilot_scopes(doc, repo)
    waits = style_proposals.ranked_work(ledger, cells, scopes).waits(family)
    exemptions = style_proposals.RankExemptions(ledger, tip)
    directories = style_proposals.workspace_directories(ledger) if decisions else ()
    unsynced = unsynced_changes(repo, base, tip)
    decision_phase: PhaseWritten | NoAvailableWork = NoAvailableWork()
    closed = False
    sites = style_proposals.MovedSites(ledger, tip)
    # The declaration step runs once for the proposals folder that holds every decision, before the loop, so a finding
    # whose declaration moved tickets at its new file, a rename whose new name is declared is built, and one whose
    # declaration is gone waits for a person or is withdrawn with its file. Each refresh keeps the parent its decision
    # names, and the step renumbers after its own closes.
    following = {finding.finding_id: finding for findings in unbuilt.values() for finding in findings}
    following.update({finding.finding_id: finding for ticket in queued
                      for finding in ledger.named_held(ticket["finding_ids"])})
    follow = (style_proposals.run_follow(style_proposals.decision_writer(
                  ledger, next(finding.state.proposal.parent for finding in following.values()
                               if isinstance(finding.state, style_ledger.Filed)), ""), following.values(), dry_run=selection == FamilySelection.COUNT)
              if any(isinstance(finding.state, style_ledger.Filed) for finding in following.values())
              else style_proposals.FollowRun(style_ledger.DeclarationReading(tip, ()), (), ()))
    outcomes = {outcome.finding.finding_id: outcome for outcome in follow.reading.outcomes}
    for index, ticket in enumerate(queued):
        in_scope = crate_in_scope(scope, ticket["crate"])
        # A file rename whose edit the gate cannot prove is dropped, and its finding waits on the decision path below.
        uncertain = edits.uncertain_of_ticket(ticket) if index == 0 and in_scope else ()
        # A ticket that the family rank holds is dropped too; its cells and findings wait on the paths below.
        skipped = ticket["proposal"] and Path(ticket["proposal"]) in follow.skipped
        ranked = (("skipped by the declaration step",) if skipped else
                  _rank_waits(ledger, ticket, family, waits, scopes, tip)) if index == 0 and in_scope and not uncertain else ()
        if index == 0 and in_scope and isinstance(predecessor, WaitingCheckpoint):
            if uncertain:
                raise style_ledger.LedgerError("selected footprint is uncertain")
            origin = ledger.ticket_origin(ticket["id"])
            findings = edits.ticket_findings(origin) if isinstance(origin, style_ledger.ApprovedProposal) else ()
            checkpoint = checkpoint_selection(predecessor, ledger, changes, edits.of_ticket(ticket), findings)
            if isinstance(checkpoint, WaitingForMerge):
                return checkpoint
        if (index == 0 and in_scope and not uncertain and not ranked
                and not any(busy.contains(path) for path in edits.of_ticket(ticket))):
            origin = ledger.ticket_origin(ticket["id"])
            findings = edits.ticket_findings(origin) if isinstance(origin, style_ledger.ApprovedProposal) else ()
            checkpoint = checkpoint_selection(predecessor, ledger, changes, edits.of_ticket(ticket), findings)
            if isinstance(checkpoint, WaitingForMerge):
                return checkpoint
            sync = ticket_sync_wait(ledger, changes, ticket["kind"], ticket["crate"], ticket["rule"],
                                    edits.of_ticket(ticket), findings)
            if isinstance(sync, WaitingForSync):
                return sync
            ledger.ticket_assign(ticket["id"], branch)
            for extra in queued[1:]:
                ledger.ticket_drop(extra["id"])
                print(f"dropped {extra['id']}: files busy")
            if isinstance(origin, style_ledger.ApprovedProposal):
                style_proposals.proposal_command("start", str(origin.issue))
            write_phase(ledger, next(ticket for ticket in ledger.ticket_open(unit) if ticket["state"] == "assigned"), plan, text, borrow_reason)
            return PhaseWritten(ticket["id"])
        ledger.ticket_drop(ticket["id"])
        reason = ((f"{uncertain[0]} has a path attribute the rename gate cannot read" if uncertain
                   else ranked[0] if ranked else "files busy")
                  if in_scope else "outside pilot scope")
        print(f"dropped {ticket['id']}: {reason}")
    # Dropping a queued ticket releases its findings for decision selection below.
    if queued:
        unbuilt = {issue.path: _unbuilt_findings(ledger, issue.path) for issue in decisions
                   if issue.value("style_rule") in definitions}
    # One line for each waiting file that holds the unit's own work: a pending cell in scope or an unbuilt finding.
    own = {cell.path for cell in cells if cell.family == family and cell.state in ("open", "stale")
           and crate_in_scope(scope, cell.crate)} | {finding.path for findings in unbuilt.values() for finding in findings}
    for path in sorted(own.intersection(waits)):
        print(waits[path].line())
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
            refusals["source proof or decision refresh refused"] += len(findings)
            print(f"waiting {issue.path}: skipped by the declaration step")
            continue
        own = tuple(outcomes[finding.finding_id] for finding in findings)
        # The answer changed after this refill read the decision as approved, so it waits for the next refill.
        if any(isinstance(outcome, style_ledger.RenameAwaitingApproval) for outcome in own):
            print(f"waiting {issue.path}: no longer approved")
            continue
        left: dict[str, style_ledger.DeclarationLeft] = {
            outcome.finding.finding_id: outcome for outcome in own if isinstance(outcome, style_ledger.DeclarationLeft)}
        left_waits: Counter[tuple[str, str]] = Counter()
        for outcome in left.values():
            match outcome:
                case style_ledger.DeclaredInSeveralFiles():
                    where = f"declared in {len(outcome.paths)} files: {', '.join(sorted(outcome.paths))}"
                case style_ledger.DeclaredInAnotherCrate():
                    where = f"declared in another crate: {outcome.path}"
                case style_ledger.DeclarationGoneFromFile():
                    where = "declaration gone"
                case style_ledger.TraitIdentityUncertain():
                    where = "trait identity uncertain"
                case style_ledger.StatementNotFollowed():
                    where = f"its function moved to {outcome.path}; {outcome.reason}"
                case style_ledger.OutsideRenameUnresolved():
                    where = style_ledger.outside_evidence_line(outcome.evidence)
                case _:
                    assert_never(outcome)
            if (isinstance(outcome, (style_ledger.DeclaredInSeveralFiles, style_ledger.DeclaredInAnotherCrate,
                                     style_ledger.DeclarationGoneFromFile))
                    and isinstance(outcome.evidence, style_ledger.OutsideRenameNotProven)):
                where += "; " + style_ledger.outside_evidence_line(outcome.evidence)
            left_waits[(outcome.finding.path, where)] += 1
        style_proposals.print_finding_waits(issue.path, left_waits)
        read = {outcome.finding.finding_id for outcome in own if not isinstance(outcome, style_ledger.DeclarationNotRead)}
        withdrew = any(isinstance(outcome, style_ledger.DeclarationRemoved) for outcome in own)
        built = any(isinstance(outcome, style_ledger.DeclarationRenamed) for outcome in own)
        # COUNT follows without writing: use the rows that follow proved, including new
        # locations and settled findings. Reloading the ledger here would restore old sites.
        kept = (tuple(projected_follow_findings(style_ledger.DeclarationReading(follow.reading.tip, own)).values())
                if selection == FamilySelection.COUNT else _unbuilt_findings(ledger, issue.path))
        findings = tuple(finding for finding in kept
                         if finding.finding_id not in left and (selection == FamilySelection.OWN
                             or not style_ledger.rule_changed_since_judged(finding, definitions)))
        # The gone-file and moved-site waives read only the findings whose declaration the step could not read.
        unread_findings = tuple(finding for finding in findings if finding.finding_id not in read)
        present = style_proposals.files_at(repo, tip, tuple(sorted({finding.path for finding in unread_findings})))
        moved = sites.destinations(tuple(finding for finding in unread_findings if finding.path in present))
        by_file: dict[str, list[style_ledger.HeldFinding]] = defaultdict(list)
        gone: dict[str, list[str]] = defaultdict(list)
        blocked = {finding.path for finding in findings
                   if not crate_in_scope(scope, style_proposals.crate_for(directories, finding.path))
                   or busy.contains(finding.path) or finding.path in unsynced}
        lower = {finding.finding_id for finding in findings
                 if style_proposals.ranked_below_unit(family, style_proposals.crate_for(directories, finding.path), scopes)
                 and finding.finding_id not in moved and (finding.finding_id in read or finding.path in present)}
        # A cheap wait avoids extra site proof unless a later judgment could waive the finding.
        # Keep that waiver's existing site condition even when its file is blocked.
        superseding = {finding.finding_id: ledger.judged_again_without(finding, definition)
                       for finding in findings if finding.finding_id in lower and finding.path in blocked}
        site_evidence = ledger.finding_sites(
            (outcomes[finding.finding_id].finding if selection == FamilySelection.COUNT else finding
             for finding in findings if finding.finding_id in lower
             and (finding.path not in blocked or isinstance(superseding[finding.finding_id], style_ledger.SupersedingJudgment))), tip)
        site_waits: Counter[tuple[str, str]] = Counter()
        for finding in findings:
            # Dry-run follow proves the original row; only the selection location is
            # projected. Ask the shared site gate for that proof, rather than asking
            # it to reread a projected row that has not been written to the ledger.
            if finding.finding_id not in read and finding.path not in present:
                gone[f"{finding.path} is not in synced commit {tip}"].append(finding.finding_id)
            elif finding.finding_id in moved:
                gone[f"{finding.path} site moved to {moved[finding.finding_id]} in synced commit {tip}"].append(finding.finding_id)
            elif (finding.finding_id in site_evidence
                  and not isinstance(site_evidence[finding.finding_id], style_ledger.UnchangedSite)):
                # A lower-rank finding judged at an older file builds only once the judgment that reads the file again
                # holds it again; a judgment that does not hold it settles it.
                judged_again = (superseding[finding.finding_id] if finding.finding_id in superseding
                                else ledger.judged_again_without(finding, definition))
                match judged_again:
                    case style_ledger.SupersedingJudgment(judgment_id=judgment):
                        gone[f"{finding.path} judged again in judgment {judgment} without it"].append(finding.finding_id)
                    case style_ledger.NotJudgedAgain():
                        site_waits[(finding.path, "changed since it was judged")] += 1
            else:
                by_file[finding.path].append(finding)
        style_proposals.print_finding_waits(issue.path, site_waits)
        # A finding in a removed file, whose site a merge moved to another file, or whose changed file a later judgment
        # read without it, can never build at its site, so it is waived and the decision can close.
        for words, finding_ids in sorted(gone.items()) if selection != FamilySelection.COUNT else ():
            _ = ledger.decline_findings(finding_ids, words, style_ledger.WaiveByLedger())
            print(f"waived {len(finding_ids)} findings in {issue.path}: {words}")
        if selection != FamilySelection.COUNT and (gone or withdrew or built) and style_proposals.close_finished_decision(ledger, issue.path):
            closed = True
            continue
        outside = {path for path in by_file if not crate_in_scope(scope, style_proposals.crate_for(directories, path))}
        if outside:
            print(f"waiting {issue.path}: outside pilot scope")
        for path in by_file.keys() & waits.keys() - blocked:
            previous_count = len(by_file[path])
            by_file[path] = [finding for finding in by_file[path] if exemptions.allows(finding, family)]
            refusals["waiting for rank"] += previous_count - len(by_file[path])
        held = {path for path in by_file if path not in outside and (not by_file[path] or busy.contains(path))}
        for path in sorted(held - set(waits)):
            print(f"waiting {issue.path}: {path} busy")
        # A file that a commit on the unit base changed waits for the sync that reads it.
        changed = sorted(path for path in set(by_file) - outside - held if path in unsynced)
        for path in changed:
            print(f"waiting {issue.path}: {unsynced[path].line(tip)}")
        # A finding whose change edits another busy file waits, and so does a file rename whose edit the gate cannot
        # prove; the other findings of its file stay available.
        footprint_waits: Counter[tuple[str, str]] = Counter()
        for path in sorted(set(by_file) - outside - held - set(changed)):
            free: list[style_ledger.HeldFinding] = []
            for finding in by_file[path]:
                uncertain = edits.uncertain(finding)
                if uncertain:
                    footprint_waits[(finding.path, f"{uncertain[0]} has a path attribute the rename gate cannot read")] += 1
                    continue
                edited = next((edited for edited in edits.of(finding) if busy.contains(edited)), None)
                if edited is None:
                    free.append(finding)
                else:
                    footprint_waits[(finding.path, f"edits {edited}, busy")] += 1
            by_file[path] = free
        style_proposals.print_finding_waits(issue.path, footprint_waits)
        if selection != FamilySelection.OWN:
            refusals["outside pilot scope"] += sum(len(by_file[path]) for path in outside)
            refusals["busy or rank held"] += sum(len(by_file[path]) for path in held)
            refusals["waiting for sync"] += sum(len(by_file[path]) for path in changed)
            for path in sorted(set(by_file) - outside - held - set(changed)):
                members = tuple(by_file[path])
                if not members:
                    continue
                footprint = tuple(dict.fromkeys(edited for finding in members for edited in edits.of(finding)))
                checkpoint = checkpoint_selection(predecessor, ledger, changes, footprint, members)
                sync = ticket_sync_wait(ledger, changes, "proposal", style_proposals.crate_for(directories, path), rule,
                                        footprint, members)
                if isinstance(checkpoint, WaitingForMerge) or isinstance(sync, WaitingForSync):
                    refusals["waiting for merge" if isinstance(checkpoint, WaitingForMerge) else "waiting for sync"] += len(members)
                    by_file[path] = []
                else:
                    buildable += len(members)
        available = tuple(sorted(path for path in set(by_file) - outside - held - set(changed) if by_file[path]))
        if selection == FamilySelection.COUNT:
            continue
        if not available:
            continue
        limits = (_STRUCTURE_LIMITS if any(style_proposals.proposal_kind(finding, definition) == "restructure"
                                           for finding in findings) else _READ_LIMITS)
        files = ((available[0],) if PurePosixPath(rule.partition(":")[2]).stem == "no-magic-values"
                 else read_slices(repo, tip, available, limits)[0])
        selected_findings = tuple(finding for path in files for finding in by_file[path])
        checkpoint = checkpoint_selection(predecessor, ledger, changes,
                                          tuple(path for finding in selected_findings for path in edits.of(finding)),
                                          selected_findings)
        if isinstance(checkpoint, WaitingForMerge):
            return checkpoint
        sync = ticket_sync_wait(ledger, changes, "proposal", style_proposals.crate_for(directories, files[0]), rule,
                                tuple(dict.fromkeys(path for finding in selected_findings for path in edits.of(finding))),
                                selected_findings)
        if isinstance(sync, WaitingForSync):
            if closed:
                style_proposals.renumber()
            return sync
        ticket_id = ledger.next_ticket_id(unit)
        ledger.ticket_add(ticket_id, unit, "proposal", family, style_proposals.crate_for(directories, files[0]),
                          list(files), sorted({finding.rule for finding in selected_findings}),
                          proposal=str(issue.path), finding_ids=tuple(finding.finding_id for finding in selected_findings))
        ledger.ticket_assign(ticket_id, branch)
        style_proposals.proposal_command("start", str(issue.path))
        write_phase(ledger, next(ticket for ticket in ledger.ticket_open(unit) if ticket["state"] == "assigned"), plan, text, borrow_reason)
        decision_phase = PhaseWritten(ticket_id)
        break
    # As complete does, one renumber follows every decision this refill closed.
    if closed:
        style_proposals.renumber()
    if isinstance(decision_phase, PhaseWritten):
        return decision_phase
    if selection == FamilySelection.COUNT:
        reason = ", ".join(f"{reason} ({count})" for reason, count in sorted(refusals.items()) if count) or "none approved"
        return BuildableFindings(buildable, reason)
    if selection == FamilySelection.BORROW:
        return NoAvailableWork()
    refill = _refill_cells(ledger, cells, base, family, scope, waits)
    candidates = _tickets_from(repo, ledger, busy, family, scope, scopes, refill, first_files)
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
    overlap_wait: WaitingForMerge | NoCheckpointBlock = NoCheckpointBlock()
    for selected in candidates:
        checkpoint = checkpoint_selection(predecessor, ledger, changes, selected.files)
        if isinstance(checkpoint, WaitingForMerge):
            overlap_wait = checkpoint
            continue
        # The ticket's sites and cells were read at the synced tip, so a file a commit on the unit base changed waits.
        changed = [unsynced[path] for path in selected.files if path in unsynced]
        if changed:
            print(f"waiting for sync: {' '.join(filter(None, (selected.kind, selected.crate, selected.rule)))}: "
                  + changed[0].line(tip))
            continue
        sync = ticket_sync_wait(ledger, changes, selected.kind, selected.crate, selected.rule, selected.files)
        if isinstance(sync, WaitingForSync):
            return sync
        ticket_id = ledger.next_ticket_id(unit)
        ledger.ticket_add(ticket_id, unit, selected.kind, selected.family, selected.crate, list(selected.files),
                          list(selected.rules), selected.rule, [dict(rule=site.rule, path=site.path, line=site.line, text=site.text) for site in selected.sites],
                          cells=selected.cells)
        ledger.ticket_assign(ticket_id, branch)
        write_phase(ledger, next(ticket for ticket in ledger.ticket_open(unit) if ticket["state"] == "assigned"), plan, text, borrow_reason)
        return PhaseWritten(ticket_id)
    return overlap_wait if isinstance(overlap_wait, WaitingForMerge) else NoAvailableWork()


def family_idle_reason(family: rules.Family, result: PhaseWritten | NoAvailableWork | WaitingForStructure |
                       MergeStyleBranchFirst | WaitingForSync | WaitingForMerge) -> FamilyIdleReason:
    if isinstance(result, WaitingForSync):
        reason = "waiting for sync" + (f" ({result.reason})" if result.reason else "")
    elif isinstance(result, WaitingForMerge):
        reason = "waiting for merge" + (f" ({result.reason})" if result.reason else "")
    elif isinstance(result, WaitingForStructure):
        reason = "waiting for structure: " + ", ".join(result.crates)
    elif isinstance(result, MergeStyleBranchFirst):
        reason = "merge the style branch first"
    else:
        reason = "no available work"
    return FamilyIdleReason(family, reason)


def select_family_work(ledger: style_ledger.Ledger, unit: str, branch: str, plan: Path, repo: Path,
                       doc: Path, text: str, scope: PilotScope, busy: BusyFiles, first_files: frozenset[str],
                       definitions: Mapping[str, rules.Rule], changes: MainChanges | UnknownMainChanges,
                       predecessor: WaitingCheckpoint | NoCheckpointBlock) -> (PhaseWritten | NoAvailableWork |
                       WaitingForStructure | MergeStyleBranchFirst | WaitingForSync | WaitingForMerge):
    own = rules.style_family(unit)
    if isinstance(own, rules.NotStyleUnit):
        raise style_ledger.LedgerError(f"{unit}: unknown style family")
    result = _select_phase(ledger, unit, branch, plan, repo, doc, text, scope, busy, first_files,
                           definitions, changes, predecessor, family=own)
    assert not isinstance(result, BuildableFindings)
    if isinstance(result, PhaseWritten):
        return result
    idle = [family_idle_reason(own, result)]
    settings = family_plan_settings(doc, repo)
    ranked: tuple[rules.Family, ...] = (*rules.FAMILY_RANKS, *(family for family in FAMILIES if family not in rules.FAMILY_RANKS))
    available: list[tuple[int, int, rules.Family]] = []
    for rank, family in enumerate(ranked):
        if family == own:
            continue
        if family not in settings:
            idle.append(FamilyIdleReason(family, "no family unit"))
            continue
        options = settings[family]
        with redirect_stdout(io.StringIO()):
            count = _select_phase(ledger, unit, branch, plan, repo, doc, text, options.pilot_scope, busy,
                                  options.first_files, definitions, changes, predecessor, family=family,
                                  selection=FamilySelection.COUNT)
        assert isinstance(count, BuildableFindings)
        idle.append(FamilyIdleReason(family, count.reason))
        available.append((-count.count, rank, family))
    for negative_count, _, family in sorted(available):
        if not negative_count:
            continue
        options = settings[family]
        selected = _select_phase(ledger, unit, branch, plan, repo, doc, text, options.pilot_scope, busy,
                                 options.first_files, definitions, changes, predecessor, family=family,
                                 selection=FamilySelection.BORROW, borrow_reason=idle[0].reason)
        assert not isinstance(selected, BuildableFindings)
        if isinstance(selected, PhaseWritten):
            return selected
        idle = [family_idle_reason(family, selected) if item.family == family else item for item in idle]
    return replace(result, families=tuple(idle))


def next_phase(unit: str, plan: Path, repo: Path) -> NextPhase:
    plan, repo = plan.expanduser().resolve(), repo.expanduser().resolve()
    doc = style_document(repo)
    row = next((row for row in unit_worktrees(doc) if row.unit == unit), None)
    if row is None:
        raise style_ledger.LedgerError(f"{unit}: no Units row in {doc}")
    expected = unit_plan_path(doc, unit, row.worktree).resolve()
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
        predecessor: WaitingCheckpoint | NoCheckpointBlock = NoCheckpointBlock()
        assigned = tuple(ticket for ticket in opened if ticket["state"] == "assigned")
        if opened and not assigned:
            ticket = opened[0]
            if len(opened) >= 2:
                return WaitingForMerge(ticket["id"], ticket["checkpoint"], "two open tickets")
            try:
                predecessor = waiting_checkpoint(ledger, ticket, row.branch)
            except (OSError, ValueError, style_ledger.LedgerError, subprocess.CalledProcessError) as error:
                return WaitingForMerge(ticket["id"], ticket["checkpoint"], f"footprint unknown: {error}")
        if assigned:
            ticket = assigned[0]
            try:
                ledger.check_ticket_pipeline(ticket["id"])
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                waiting = next((item for item in opened if item["state"] == "checkpointed"), ticket)
                return WaitingForMerge(waiting["id"], waiting["checkpoint"], f"footprint unknown: {error}")
            origin = ledger.ticket_origin(ticket["id"])
            if isinstance(origin, style_ledger.ApprovedProposal):
                style_proposals.proposal_command("start", str(origin.issue))
            write_phase(ledger, ticket, plan, text)
            ledger.clear_sync_wait(unit)
            return Resumed(ticket["id"])
        try:
            tip = synced_tip(ledger)
        except style_ledger.LedgerError as error:
            if isinstance(predecessor, WaitingCheckpoint):
                return WaitingForMerge(predecessor.ticket_id, predecessor.commit, f"footprint unknown: {error}")
            ledger.wait_for_sync(unit)
            return WaitingForSync(0, f"touched set unknown: {error}")
        standing = standing_style_production(repo)
        if not isinstance(standing, StandingStyleProduction):
            raise style_ledger.LedgerError(f"no standing style production: {standing.slug}")
        changes_on_main = main_changes(ledger, standing.branch_point, tip)
        if isinstance(changes_on_main, UnknownMainChanges):
            if isinstance(predecessor, WaitingCheckpoint):
                return WaitingForMerge(predecessor.ticket_id, predecessor.commit, f"footprint unknown: {changes_on_main.reason}")
            ledger.wait_for_sync(unit)
            return WaitingForSync(0, f"touched set unknown: {changes_on_main.reason}")
        # One catalog read serves both refill passes; it reads no shared state, so it runs before the lock.
        definitions = {rule.rule_id: rule for rule in rules.load_rules(repo)}
        def select(busy: BusyFiles) -> PhaseWritten | NoAvailableWork | WaitingForStructure | MergeStyleBranchFirst | WaitingForSync | WaitingForMerge:
            try:
                return select_family_work(ledger, unit, row.branch, plan, repo, doc, text, scope, busy, settings.first_files,
                                     definitions, changes_on_main, predecessor)
            except (OSError, ValueError, style_ledger.LedgerError, subprocess.CalledProcessError) as error:
                if isinstance(predecessor, WaitingCheckpoint):
                    return WaitingForMerge(predecessor.ticket_id, predecessor.commit, f"footprint unknown: {error}")
                raise

        with style_ledger.refill_lock(repo):
            board = berth_board.read_board(repo)
            if isinstance(board, berth_board.BoardUnavailable):
                return board
            busy = busy_files(repo, doc, ledger, unit, board)
            selected = select(busy)
            if isinstance(selected, WaitingForMerge):
                return selected
            if isinstance(predecessor, WaitingCheckpoint) and not isinstance(selected, PhaseWritten):
                return WaitingForMerge(predecessor.ticket_id, predecessor.commit, "no disjoint ticket available",
                                       selected.families)
            # A unit told to merge the style branch acts at once, so it records no sync wait.
            if isinstance(selected, PhaseWritten | MergeStyleBranchFirst):
                ledger.clear_sync_wait(unit)
                return selected
            if isinstance(selected, WaitingForSync):
                ledger.wait_for_sync(unit)
                return selected
        ledger.wait_for_sync(unit)
        with style_ledger.refill_lock(repo):
            board = berth_board.read_board(repo)
            if isinstance(board, berth_board.BoardUnavailable):
                return board
            busy = busy_files(repo, doc, ledger, unit, board)
            selected = select(busy)
            if isinstance(selected, WaitingForMerge):
                return selected
            if isinstance(predecessor, WaitingCheckpoint) and not isinstance(selected, PhaseWritten):
                return WaitingForMerge(predecessor.ticket_id, predecessor.commit, "no disjoint ticket available",
                                       selected.families)
            if isinstance(selected, PhaseWritten | MergeStyleBranchFirst):
                ledger.clear_sync_wait(unit)
                return selected
            if isinstance(selected, WaitingForStructure | WaitingForSync):
                return selected
            return WaitingForSync(len(ledger.unclassified_rules()), families=selected.families)


class SplitRankExemptions(style_proposals.RankExemptions):
    def __init__(self, ledger: style_ledger.Ledger, tip: str, cache: style_ledger.GitReadCache) -> None:
        super().__init__(ledger, tip)
        self.cache: style_ledger.GitReadCache = cache

    @override
    def source(self, path: str) -> str:
        if path not in self.sources:
            self.sources[path] = self.cache.text(self.tip, path)
        return self.sources[path]


class SplitEditedFiles(EditedFiles):
    def __init__(self, repo: Path, ledger: style_ledger.Ledger, base: UnitBase, cache: style_ledger.GitReadCache) -> None:
        super().__init__(repo, ledger, base)
        self.cache: style_ledger.GitReadCache = cache

    @override
    def read(self, named: Mapping[str, tuple[str, ...]]) -> None:
        for commit, paths in named.items():
            self.cache.read(commit, paths)
            for path in paths:
                blob = self.cache.blob(commit, path)
                if not blob:
                    raise style_ledger.CheckCouldNotRun(f"{commit}: no blob for {path}")
                self.blobs[commit, path] = blob
                self.texts[blob] = self.cache.contents[blob].decode(errors="replace")


class SplitMovedSites(style_proposals.MovedSites):
    def __init__(self, ledger: style_ledger.Ledger, tip: str, cache: style_ledger.GitReadCache) -> None:
        super().__init__(ledger, tip)
        self.cache: style_ledger.GitReadCache = cache

    @override
    def lines_at(self, commit: str, path: str) -> frozenset[str]:
        key = (commit, path)
        if key not in self.lines:
            self.lines[key] = frozenset(line.strip() for line in self.cache.text(commit, path).splitlines())
        return self.lines[key]


SplitCategory = Literal["ready", "changed", "structure", "other"]


def classify_split(ledger: style_ledger.Ledger, unit: str, doc: Path, base: UnitBase, scope: PilotScope,
                   busy: BusyFiles, definitions: Mapping[str, rules.Rule], cache: style_ledger.GitReadCache,
                   measured_at: str) -> style_ledger.RecordedSplit:
    """Partition approved filed findings without applying any refill transition."""
    family = rules.style_family(unit)
    if isinstance(family, rules.NotStyleUnit):
        raise style_ledger.LedgerError(f"{unit}: unknown style family")
    tip = ledger.synced_commit()
    cache.warm(tip)
    scopes = family_pilot_scopes(doc, ledger.repo)
    waits = style_proposals.ranked_work(ledger, ledger.cells(), scopes, reader=cache).waits(family)
    exemptions = SplitRankExemptions(ledger, tip, cache)
    edits = SplitEditedFiles(ledger.repo, ledger, base, cache)
    sites = SplitMovedSites(ledger, tip, cache)
    unsynced = unsynced_changes(ledger.repo, base, tip)
    directories = cache.tip(tip).crate_dirs
    try:
        folder = Path(production_field(doc.read_text().splitlines(), "Proposals")).expanduser()
    except Refusal:
        selected: tuple[Issue, ...] = ()
    else:
        if not folder.is_absolute():
            folder = doc.parent.parent / folder
        selected = style_proposals.approved_decisions(folder, unit)
    population: list[tuple[style_ledger.HeldFinding, Path]] = []
    decisions: dict[Path, bool] = {}
    for finding in ledger.held_findings(style_ledger.HeldFindingsInState("filed")):
        if not isinstance(finding.state, style_ledger.Filed):
            continue
        path = finding.state.proposal.expanduser().resolve()
        if path not in decisions:
            if not path.is_file():
                decisions[path] = False
            else:
                issue = Issue(path, frontmatter(path))
                decisions[path] = (rules.style_family(issue.value("unit")) == family
                                   and isinstance(decision_answer(issue), ApprovedDecision))
        if decisions[path]:
            population.append((finding, path))
    opened = ledger.open_build_finding_ids()
    # Match the stored proposal key exactly as refill does. A merged build still holds its findings.
    unbuilt = {finding.finding_id: issue for issue in selected if issue.value("style_rule") in definitions
               for finding in _unbuilt_findings(ledger, issue.path)}
    pending = tuple(finding for finding, _path in population
                    if finding.finding_id not in opened and finding.finding_id in unbuilt)
    reading = (ledger.declaration_reading((finding.finding_id for finding in pending), style_proposals.approved_decision)
               if pending else style_ledger.DeclarationReading(tip, ()))
    outcomes = {outcome.finding.finding_id: outcome for outcome in reading.outcomes}
    skipped: set[Path] = set()
    for outcome in reading.outcomes:
        if isinstance(outcome, style_ledger.DeclarationMoved | style_ledger.DeclarationRemoved):
            if not isinstance(outcome.finding.state, style_ledger.Filed):
                continue
            path = outcome.finding.state.proposal
            try:
                require_generated_body(path)
                _ = style_proposals.decision_parent(path)
            except (style_ledger.LedgerError, Refusal):
                skipped.add(path.resolve())
    counts: Counter[SplitCategory] = Counter()
    for finding, path in population:
        if finding.finding_id in opened:
            counts["ready"] += 1
            continue
        issue = unbuilt.get(finding.finding_id)
        if issue is None or path in skipped or (definition := definitions.get(issue.value("style_rule"))) is None:
            counts["other"] += 1
            continue
        outcome = outcomes[finding.finding_id]
        if isinstance(outcome, style_ledger.DeclarationMoved):
            finding = replace(finding, path=outcome.path, line=outcome.line,
                              follow=style_ledger.FollowedDeclaration(finding.path, finding.line, tip))
        elif not isinstance(outcome, style_ledger.DeclarationNotRead | style_ledger.DeclarationInPlace
                            | style_ledger.KeptForOpenBuild):
            counts["other"] += 1
            continue
        # Refill waives a missing file or a moved site only when the declaration step did not read it.
        if isinstance(outcome, style_ledger.DeclarationNotRead) and (
                not cache.blob(tip, finding.path) or sites.destinations((finding,))):
            counts["other"] += 1
            continue
        crate = style_proposals.crate_for(directories, finding.path)
        if (style_proposals.ranked_below_unit(family, crate, scopes)
                and not style_proposals.judged_at_current_file(ledger, finding, tip)):
            category: SplitCategory = ("other" if isinstance(ledger.judged_again_without(finding, definition),
                                                            style_ledger.SupersedingJudgment) else "changed")
        elif finding.path in unsynced:
            category = "changed"
        elif not crate_in_scope(scope, crate):
            category = "other"
        elif finding.path in waits and not exemptions.allows(finding, family):
            category = "structure"
        elif busy.contains(finding.path):
            category = "other"
        elif edits.uncertain(finding) or any(busy.contains(path) for path in edits.of(finding)):
            category = "other"
        else:
            category = "ready"
        counts[category] += 1
    return style_ledger.RecordedSplit(counts["ready"], counts["changed"], counts["structure"], counts["other"], measured_at)


@dataclass(frozen=True)
class SplitDeadline:
    at: float
    timeout_minutes: float

    def timeout_error(self) -> TimeoutError:
        return TimeoutError(f"time limit of {self.timeout_minutes:g} min reached")

    def remaining_seconds(self) -> float:
        remaining = self.at - time.monotonic()
        if remaining <= 0:
            raise self.timeout_error()
        return remaining


@contextmanager
def split_time_limit(minutes: float) -> Generator[SplitDeadline]:
    if not math.isfinite(minutes) or minutes <= 0:
        raise style_ledger.LedgerError("timeout-minutes must be positive and finite")

    began = time.monotonic()
    deadline = SplitDeadline(began + minutes * 60, minutes)

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise deadline.timeout_error()

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, minutes * 60)
    try:
        yield deadline
    finally:
        _ = signal.setitimer(signal.ITIMER_REAL, 0)
        _ = signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            _ = signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - began)),
                                 previous_timer[1])


def split(unit: str, repo: Path, timeout_minutes: float = 90) -> style_ledger.RecordedSplit:
    """Scan a backup copy, then store only a complete split on the original ledger."""
    measured_at = datetime.now(UTC).isoformat()
    with split_time_limit(timeout_minutes) as deadline:
        repo = repo.expanduser().resolve()
        style_ledger.require_split_table(repo)
        doc = style_document(repo)
        row = next((row for row in unit_worktrees(doc) if row.unit == unit), None)
        if row is None:
            raise style_ledger.LedgerError(f"{unit}: no Units row in {doc}")
        plan = unit_plan_path(doc, unit, row.worktree)
        scope = unit_plan_settings(plan.read_text(), repo).pilot_scope
        board = berth_board.read_board(repo)
        if isinstance(board, berth_board.BoardUnavailable):
            raise style_ledger.LedgerError(f"board unavailable: {board.reason}")
        cache = style_ledger.GitReadCache(repo)
        with style_ledger.preview_ledger(repo, git_cache=cache) as ledger:
            busy = busy_files(repo, doc, ledger, unit, board)
            definitions = {rule.rule_id: rule for rule in rules.load_rules(repo)}
            result = classify_split(ledger, unit, doc, unit_base(repo, row.branch), scope, busy, definitions, cache,
                                    measured_at)
        style_ledger.record_split(repo, unit, result,
                                  busy_timeout_seconds=min(style_ledger.BUSY_TIMEOUT_SECONDS, deadline.remaining_seconds()))
    return result


def split_line(unit: str, result: style_ledger.RecordedSplit, minutes: float) -> str:
    return f"{unit}: {result.ready}/{result.changed}/{result.structure}/{result.other} in {minutes:.2f} min"


def split_all(production: Path) -> int:
    """Run the live style units one at a time, each in a low-priority child."""
    production = production.expanduser().resolve()
    rows = [row for row in live_unit_table(production.read_text().splitlines(),
                                          production.stem.removesuffix("-production"))
            if not isinstance(rules.style_family(cell_value(row["Unit"])), rules.NotStyleUnit)]
    if not rows:
        print("no style units")
        return 0
    with redirect_stdout(io.StringIO()):
        prefix = merge_gate.suite_scope_prefix()
    failed = False
    for row in rows:
        unit = cell_value(row["Unit"])
        try:
            worktree = Path(cell_value(row["Worktree"])).expanduser()
            if not worktree.is_absolute():
                worktree = production.parent.parent / worktree
            plan = unit_plan_path(production, unit, worktree)
            if not plan.is_file():
                raise style_ledger.LedgerError(f"no unit plan: {plan}")
            child = subprocess.run([*prefix, "nice", "-n", "19", sys.executable, str(Path(__file__).resolve()),
                                    "split", "--unit", unit, "--repo", str(worktree)],
                                   capture_output=True, text=True, check=False)
            if child.returncode:
                cause = (child.stderr.strip() or child.stdout.strip() or f"exit {child.returncode}")
                raise style_ledger.LedgerError(cause.removeprefix(f"split {unit}: "))
            print(child.stdout.strip())
        except Exception as error:
            failed = True
            print(f"{unit}: failed: {' '.join(str(error).split())}")
    return int(failed)


def split_all_running() -> int:
    """Run every running style production, yielding to a natedev build hold."""
    if build_hold.natedev_holds(build_hold.holder_directory()).in_force:
        print("split-all: natedev build hold in force; skipped")
        return 0
    found = False
    for runner in showrunners.registered_showrunners():
        if not runner["session"]:
            continue
        try:
            production = Path(runner["doc"]).expanduser()
            lines = production.read_text().splitlines()
            try:
                kind = production_field(lines, "Kind")
            except Refusal:
                continue
            if kind != "style":
                continue
            found = True
            if build_hold.natedev_holds(build_hold.holder_directory()).in_force:
                print("split-all: natedev build hold in force; skipped")
                return 0
            code = split_all(production)
            if code:
                print(f"split-all: {runner['slug']}: failed: exit {code}")
        except Exception as error:
            print(f"split-all: {runner['slug']}: failed: {' '.join(str(error).split())}")
    if not found:
        print("no running style productions")
    return 0


def refill_outcome(result: NextPhase) -> str:
    match result:
        case PhaseWritten():
            return "phase written"
        case Resumed():
            return "resumed"
        case RepairFirst():
            return "repair first"
        case WaitingForMerge():
            return "waiting for merge"
        case WaitingForSync(reason=reason):
            return "waiting for sync" + (f": {reason}" if reason else "")
        case WaitingForStructure(crates):
            return f"waiting for structure: {', '.join(crates)}"
        case MergeStyleBranchFirst():
            return "merge the style branch first"
        case berth_board.BoardUnavailable():
            return "board unavailable"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("split")
    _ = scan.add_argument("--unit", required=True)
    _ = scan.add_argument("--repo", type=Path, required=True)
    _ = scan.add_argument("--timeout-minutes", type=float, default=90)
    scans = sub.add_parser("split-all")
    targets = scans.add_mutually_exclusive_group(required=True)
    _ = targets.add_argument("--production", type=Path)
    _ = targets.add_argument("--all", action="store_true")
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
    ticket_widen = sub.add_parser("widen")
    _ = ticket_widen.add_argument("--repo", type=Path, required=True)
    _ = ticket_widen.add_argument("--ticket", required=True)
    _ = ticket_widen.add_argument("--files", nargs="+", required=True)
    _ = ticket_widen.add_argument("--reason", required=True)
    _ = ticket_widen.add_argument("--by", required=True)
    args = cast(PlanArguments, parser.parse_args(argv))
    if args.command == "split":
        began = time.monotonic()
        try:
            result = split(args.unit, args.repo, args.timeout_minutes)
        except Exception as error:
            print(f"split {args.unit}: {error}", file=sys.stderr)
            return 1
        print(split_line(args.unit, result, (time.monotonic() - began) / 60))
        return 0
    if args.command == "split-all":
        try:
            return split_all_running() if args.all else split_all(args.production)
        except Exception as error:
            print(f"split-all: {error}", file=sys.stderr)
            return 1
    try:
        if args.command == "gate":
            return gate(args.repo, args.ticket)
        if args.command == "widen":
            widen(args.repo, args.ticket, tuple(args.files), args.reason, args.by)
            return 0
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
        style_ledger.record_refill(args.repo, args.unit, refill_outcome(result))
        if isinstance(result, WaitingForSync | WaitingForMerge | WaitingForStructure | MergeStyleBranchFirst) and result.families:
            print("no available work: " + ", ".join(f"{item.family} {' '.join(item.reason.split())}" for item in result.families))
        match result:
            case PhaseWritten(ticket_id):
                print(f"phase written: {ticket_id}")
            case Resumed(ticket_id):
                print(f"resume {ticket_id}")
            case RepairFirst():
                print("repair first")
            case WaitingForMerge(ticket_id, checkpoint, reason):
                print(f"waiting for merge: {ticket_id} checkpointed at {checkpoint[:7]}" + (f"; {reason}" if reason else ""))
                return 5
            case berth_board.BoardUnavailable(reason):
                print(f"board unavailable: {reason}")
                return 4
            case WaitingForSync(unclassified, reason):
                print("waiting for sync" + (f": {reason}" if reason else ""))
                if unclassified:
                    print(f"unclassified: {unclassified} rules — pull nate_style", file=sys.stderr)
                return 3
            case WaitingForStructure(crates, unclassified):
                print(f"waiting for structure: {', '.join(crates)}")
                if unclassified:
                    print(f"unclassified: {unclassified} rules — pull nate_style", file=sys.stderr)
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
    files: list[str] = []
    reason: str = ""
    by: str = ""
    timeout_minutes: float = 90
    production: Path = Path()
    all: bool = False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
