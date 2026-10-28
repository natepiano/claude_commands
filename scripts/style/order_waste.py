#!/usr/bin/env python3
"""Measure wasted style work by rule pair (A, B): A's fix changed code that B's finding or fix had to read again.

Reads a style ledger and its repository; writes nothing to either.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

import style_ledger
from style_ledger import DeclarationGone, DuplicateOf, ExceptionVerdict, JudgedAgain, WaiveProvenance


class OrderWasteError(Exception):
    """The ledger or the repository cannot be read."""


# Rule groups the analysis names, by catalog slug.
TYPES_RULES = (
    "dont-create-traits-for-single-implementations", "make-invalid-states-unrepresentable", "public-failures-return-named-errors",
    "enums-over-bool-for-owned-booleans", "enums-over-option-when-none-has-meaning", "if-else-chains-signal-missing-types",
    "prefer-from-impl-over-named-constructors", "use-bevy_kana-in-all-bevy-crates", "bevy-reflection-registration",
    "extract-observer-run-conditions", "bevy-plugin-ownership", "prefer-events-over-messages",
)
VALUES_RULES = (
    "never-prefix-unused-fields-or-variables-with", "prefer-observers-over-polling", "system-params-carry-their-behavior",
    "tests-live-with-the-type-under-test", "no-magic-values", "derive-test-values-from-production-constants",
    "import-constants-at-the-top", "prefer-functional-patterns", "test-module-allow-boilerplate", "always-use-nextest",
    "agent-must-review-allows", "constants-file-organization",
)
NAMES_RULES = (
    "spell-out-names", "dont-repeat-enum-domain-in-variant-names", "prefer-type-named-fields-and-bindings",
    "dont-repeat-type-name-in-fields", "avoid-repeated-field-affixes", "name-bindings-to-match-parameters",
    "comments-name-code-constructs", "forbidden-words",
)
MODULES_RULES = (
    "types-live-with-their-behavior", "when-to-split-a-module", "split-by-type-ownership", "module-roots-as-table-of-contents",
    "name-submodules-after-anchor-types", "module-structure", "workspace-dependencies",
)
MODULE_MOVES = (
    "types-live-with-their-behavior", "when-to-split-a-module", "split-by-type-ownership", "name-submodules-after-anchor-types",
    "module-roots-as-table-of-contents",
)
MEND_FIXES = (
    "use-narrowest-visibility", "use-pubcrate-in-top-level-private-modules", "prefer-local-relative-imports",
    "import-the-module-for-functions-not-the-function-itself", "import-types-directly",
)
TIER_A_RULES = (
    *MEND_FIXES, "avoid-redundant-closures", "borrow-the-slice-not-the-container", "collapse-if-let-with-inner-conditions",
    "imports-go-at-the-top-of-the-file", "inline-variables-in-format-strings", "no-single-component-use-imports",
    "omit-return-in-expression-position", "one-use-per-line", "use-usizefrombool-for-bool-to-integer-conversion",
)
CONTENT_RULES = (*TYPES_RULES, *VALUES_RULES, *NAMES_RULES)
NAMES_BEFORE_COMMENTS = tuple(rule for rule in NAMES_RULES if rule not in ("comments-name-code-constructs", "forbidden-words"))
NAMES_BEFORE_WORDS = tuple(rule for rule in NAMES_RULES if rule != "forbidden-words")
FAMILY_RULES: Mapping[str, tuple[str, ...]] = {
    "types": TYPES_RULES, "values": VALUES_RULES, "names": NAMES_RULES, "modules": MODULES_RULES,
}
GROUP_NAMES: Mapping[tuple[str, ...], str] = {
    TYPES_RULES: "every types rule", NAMES_RULES: "every names rule", MODULE_MOVES: "every module move",
    MEND_FIXES: "mend fixes", TIER_A_RULES: "tier A", CONTENT_RULES: "every content change",
    NAMES_BEFORE_COMMENTS: "every other names rule", NAMES_BEFORE_WORDS: "every other names rule",
}

# The proposed order's layers 1 to 6 (rule_order_analysis.md, phase 81), each in its listed order. Tier A runs around
# them and has no position; always-use-nextest and workspace-dependencies take any position.
ANALYSIS_LAYERS: tuple[tuple[str, ...], ...] = (
    ("never-prefix-unused-fields-or-variables-with", "prefer-events-over-messages", "prefer-observers-over-polling",
     "system-params-carry-their-behavior"),
    ("dont-create-traits-for-single-implementations", "make-invalid-states-unrepresentable", "public-failures-return-named-errors",
     "enums-over-bool-for-owned-booleans", "enums-over-option-when-none-has-meaning", "if-else-chains-signal-missing-types",
     "prefer-from-impl-over-named-constructors", "use-bevy_kana-in-all-bevy-crates", "bevy-reflection-registration",
     "extract-observer-run-conditions", "bevy-plugin-ownership"),
    ("types-live-with-their-behavior", "when-to-split-a-module", "split-by-type-ownership", "module-roots-as-table-of-contents"),
    ("tests-live-with-the-type-under-test", "no-magic-values", "derive-test-values-from-production-constants",
     "import-constants-at-the-top", "prefer-functional-patterns", "test-module-allow-boilerplate", "agent-must-review-allows"),
    ("spell-out-names", "dont-repeat-enum-domain-in-variant-names", "name-submodules-after-anchor-types",
     "prefer-type-named-fields-and-bindings", "dont-repeat-type-name-in-fields", "avoid-repeated-field-affixes",
     "name-bindings-to-match-parameters", "comments-name-code-constructs", "forbidden-words"),
    ("constants-file-organization", "module-structure"),
)
POSITION: Mapping[str, int] = {rule: index for index, rule in enumerate(rule for layer in ANALYSIS_LAYERS for rule in layer)}


@dataclass(frozen=True)
class AnalysisEdge:
    """Every rule in before goes before every rule in after."""

    before: tuple[str, ...]
    after: tuple[str, ...]

    def label(self) -> str:
        def side(rules: tuple[str, ...]) -> str:
            return GROUP_NAMES.get(rules, ", ".join(rules))

        return f"{side(self.before)} > {side(self.after)}"

    def pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((first, then) for first in self.before for then in self.after if first != then)


def _edge(before: str | tuple[str, ...], after: str | tuple[str, ...]) -> AnalysisEdge:
    return AnalysisEdge((before,) if isinstance(before, str) else before, (after,) if isinstance(after, str) else after)


_NEW_ENUM_RULES = ("enums-over-bool-for-owned-booleans", "enums-over-option-when-none-has-meaning", "if-else-chains-signal-missing-types",
                   "make-invalid-states-unrepresentable", "public-failures-return-named-errors")
ANALYSIS_EDGES: tuple[AnalysisEdge, ...] = (
    # Covered by the family rank.
    _edge("enums-over-option-when-none-has-meaning", "prefer-functional-patterns"),
    _edge("enums-over-bool-for-owned-booleans", "prefer-functional-patterns"),
    _edge("make-invalid-states-unrepresentable", "prefer-functional-patterns"),
    _edge("public-failures-return-named-errors", "prefer-functional-patterns"),
    _edge("prefer-from-impl-over-named-constructors", "prefer-functional-patterns"),
    _edge("if-else-chains-signal-missing-types", "no-magic-values"),
    _edge("prefer-events-over-messages", "prefer-observers-over-polling"),
    _edge("use-bevy_kana-in-all-bevy-crates", "agent-must-review-allows"),
    _edge(_NEW_ENUM_RULES, "dont-repeat-enum-domain-in-variant-names"),
    _edge(("enums-over-bool-for-owned-booleans", "enums-over-option-when-none-has-meaning"), "prefer-type-named-fields-and-bindings"),
    _edge("no-magic-values", ("spell-out-names", "forbidden-words")),
    _edge("system-params-carry-their-behavior", "prefer-type-named-fields-and-bindings"),
    _edge(TYPES_RULES, "comments-name-code-constructs"),
    # Contradicts the family rank.
    _edge("prefer-observers-over-polling", "extract-observer-run-conditions"),
    _edge("prefer-observers-over-polling", "bevy-plugin-ownership"),
    _edge("never-prefix-unused-fields-or-variables-with",
          ("enums-over-option-when-none-has-meaning", "enums-over-bool-for-owned-booleans", "make-invalid-states-unrepresentable")),
    _edge(("spell-out-names", "forbidden-words"), "constants-file-organization"),
    # Same family.
    _edge("tests-live-with-the-type-under-test", "derive-test-values-from-production-constants"),
    _edge("tests-live-with-the-type-under-test", "no-magic-values"),
    _edge("no-magic-values", "derive-test-values-from-production-constants"),
    _edge("no-magic-values", "constants-file-organization"),
    _edge("no-magic-values", "import-constants-at-the-top"),
    _edge("tests-live-with-the-type-under-test", "test-module-allow-boilerplate"),
    _edge("system-params-carry-their-behavior", "agent-must-review-allows"),
    _edge(("prefer-observers-over-polling", "prefer-functional-patterns", "never-prefix-unused-fields-or-variables-with"),
          "agent-must-review-allows"),
    _edge("make-invalid-states-unrepresentable", "enums-over-option-when-none-has-meaning"),
    _edge("make-invalid-states-unrepresentable", "enums-over-bool-for-owned-booleans"),
    _edge("public-failures-return-named-errors", "enums-over-option-when-none-has-meaning"),
    _edge("enums-over-bool-for-owned-booleans", "prefer-from-impl-over-named-constructors"),
    _edge("if-else-chains-signal-missing-types", "prefer-from-impl-over-named-constructors"),
    _edge("dont-create-traits-for-single-implementations", "prefer-from-impl-over-named-constructors"),
    _edge("bevy-reflection-registration", "bevy-plugin-ownership"),
    _edge("prefer-events-over-messages", "extract-observer-run-conditions"),
    _edge("prefer-events-over-messages", "bevy-plugin-ownership"),
    _edge("spell-out-names", "prefer-type-named-fields-and-bindings"),
    _edge("dont-repeat-enum-domain-in-variant-names", "prefer-type-named-fields-and-bindings"),
    _edge("prefer-type-named-fields-and-bindings", "dont-repeat-type-name-in-fields"),
    _edge("dont-repeat-type-name-in-fields", "avoid-repeated-field-affixes"),
    _edge(("prefer-type-named-fields-and-bindings", "spell-out-names"), "name-bindings-to-match-parameters"),
    _edge(NAMES_BEFORE_COMMENTS, "comments-name-code-constructs"),
    _edge(NAMES_BEFORE_WORDS, "forbidden-words"),
    # Modules.
    _edge((*_NEW_ENUM_RULES, "dont-create-traits-for-single-implementations"), "types-live-with-their-behavior"),
    _edge("types-live-with-their-behavior", "when-to-split-a-module"),
    _edge("when-to-split-a-module", "split-by-type-ownership"),
    _edge("split-by-type-ownership", "name-submodules-after-anchor-types"),
    _edge(("types-live-with-their-behavior", "split-by-type-ownership"), "tests-live-with-the-type-under-test"),
    _edge("when-to-split-a-module", "no-magic-values"),
    _edge(("types-live-with-their-behavior", "when-to-split-a-module"), "import-constants-at-the-top"),
    _edge(("spell-out-names", "dont-repeat-enum-domain-in-variant-names"), "name-submodules-after-anchor-types"),
    _edge(("when-to-split-a-module", "types-live-with-their-behavior"), "module-roots-as-table-of-contents"),
    _edge("bevy-plugin-ownership", ("module-structure", "module-roots-as-table-of-contents")),
    _edge(CONTENT_RULES, "module-structure"),
    _edge("use-bevy_kana-in-all-bevy-crates", "workspace-dependencies"),
    _edge(MODULE_MOVES, "comments-name-code-constructs"),
    _edge("system-params-carry-their-behavior", "types-live-with-their-behavior"),
    # Tier A.
    _edge(MODULE_MOVES, MEND_FIXES),
    _edge("tests-live-with-the-type-under-test", "use-narrowest-visibility"),
    _edge("agent-must-review-allows", TIER_A_RULES),
    _edge(("no-magic-values", "import-constants-at-the-top"), ("imports-go-at-the-top-of-the-file", "one-use-per-line")),
    _edge("prefer-functional-patterns",
          ("avoid-redundant-closures", "omit-return-in-expression-position", "collapse-if-let-with-inner-conditions")),
    _edge("enums-over-bool-for-owned-booleans", "use-usizefrombool-for-bool-to-integer-conversion"),
    _edge("enums-over-option-when-none-has-meaning", "collapse-if-let-with-inner-conditions"),
    _edge(NAMES_RULES, "inline-variables-in-format-strings"),
    _edge(TIER_A_RULES, CONTENT_RULES),
)
EDGE_PAIRS: frozenset[tuple[str, str]] = frozenset(pair for edge in ANALYSIS_EDGES for pair in edge.pairs())


def canonical_rule(rule_id: str) -> str:
    """A ledger rule id without its component: `global:rust/no-magic-values.md#uncovered` is `global:rust/no-magic-values.md`."""
    return rule_id.split("#", 1)[0]


def rule_slug(rule_id: str) -> str | None:
    """The analysis slug of a global Rust rule: `global:rust/no-magic-values.md` is `no-magic-values`; other rules have none."""
    rule = canonical_rule(rule_id)
    return PurePosixPath(rule).stem if rule.startswith("global:rust/") else None


@dataclass(frozen=True, order=True)
class RuleFix:
    """The ledger names this rule for the change."""

    rule: str

    def label(self) -> str:
        return self.rule

    def slugs(self) -> tuple[str, ...]:
        """The analysis slugs of this side."""
        slug = rule_slug(self.rule)
        return (slug,) if slug else ()


@dataclass(frozen=True, order=True)
class FamilyRead:
    """A read ticket of the family made the change, and no ledger row names which of its rules did."""

    family: str

    def label(self) -> str:
        return f"{self.family} read"

    def slugs(self) -> tuple[str, ...]:
        """The analysis slugs of this side."""
        return FAMILY_RULES.get(self.family, ())


Side = RuleFix | FamilyRead


class AnalysisOrder(StrEnum):
    """Where the analysis puts A against B: by one of its edges, or only by the positions of its proposed order."""

    A_FIRST_BY_EDGE = "A first, edge"
    A_FIRST_BY_POSITION = "A first, position"
    B_FIRST_BY_EDGE = "B first, edge"
    B_FIRST_BY_POSITION = "B first, position"
    UNORDERED = "unordered"


_A_FIRST = frozenset({AnalysisOrder.A_FIRST_BY_EDGE, AnalysisOrder.A_FIRST_BY_POSITION})
_B_FIRST = frozenset({AnalysisOrder.B_FIRST_BY_EDGE, AnalysisOrder.B_FIRST_BY_POSITION})


def _rule_order(first: str, then: str) -> AnalysisOrder:
    if (first, then) in EDGE_PAIRS:
        return AnalysisOrder.A_FIRST_BY_EDGE
    if (then, first) in EDGE_PAIRS:
        return AnalysisOrder.B_FIRST_BY_EDGE
    if first in POSITION and then in POSITION:
        return AnalysisOrder.A_FIRST_BY_POSITION if POSITION[first] < POSITION[then] else AnalysisOrder.B_FIRST_BY_POSITION
    return AnalysisOrder.UNORDERED


def analysis_order(changed_by: Side, redone: Side) -> AnalysisOrder:
    """Where the analysis puts A against B; a family read is ordered only when each of its rules agrees."""
    orders = {_rule_order(first, then) for first in changed_by.slugs() for then in redone.slugs() if first != then}
    if len(orders) == 1:
        return orders.pop()
    if orders and orders <= _A_FIRST:
        return AnalysisOrder.A_FIRST_BY_POSITION
    if orders and orders <= _B_FIRST:
        return AnalysisOrder.B_FIRST_BY_POSITION
    return AnalysisOrder.UNORDERED


@dataclass(frozen=True)
class MergedTicket:
    id: str
    family: str
    rules: tuple[str, ...]
    base: str
    checkpoint: str
    merged_at: datetime
    # The rules with a `fixed` verdict of this ticket, by path.
    fixed: Mapping[str, frozenset[str]]

    def sides(self, path: str) -> frozenset[Side]:
        """Who made this ticket's change at path: its fixed verdicts there, else its one rule, else its family's read."""
        if path in self.fixed:
            return frozenset(RuleFix(rule) for rule in self.fixed[path])
        if len(self.rules) == 1:
            return frozenset({RuleFix(self.rules[0])})
        return frozenset({FamilyRead(self.family)})


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


def merged_tickets(connection: sqlite3.Connection) -> tuple[MergedTicket, ...]:
    fixed: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for ticket, rule, path in cast(list[tuple[str, str, str]], connection.execute(
            "SELECT ticket,rule,path FROM judgments WHERE verdict='fixed'").fetchall()):
        fixed[(ticket, path)].add(canonical_rule(rule))
    by_ticket: defaultdict[str, dict[str, frozenset[str]]] = defaultdict(dict)
    for (ticket, path), rules in fixed.items():
        by_ticket[ticket][path] = frozenset(rules)
    rows = cast(list[tuple[str, str, str, str, str, str]], connection.execute(
        "SELECT id,family,rules,base,checkpoint,updated_at FROM tickets WHERE state='merged' ORDER BY updated_at,id").fetchall())
    return tuple(MergedTicket(id, family, tuple(sorted({canonical_rule(rule) for rule in cast(list[str], json.loads(rules))})),
                              base, checkpoint, _instant(merged_at), by_ticket.get(id, {}))
                 for id, family, rules, base, checkpoint, merged_at in rows)


@dataclass(frozen=True)
class LedgerWaive:
    """The ledger waived the approved finding: its file is gone, its site moved, or its changed file was judged again without it."""


# Why a counted finding ended: each reason says its file changed under it.
StaleReason = JudgedAgain | DeclarationGone | LedgerWaive


@dataclass(frozen=True)
class EndedFinding:
    finding_id: str
    rule: str
    # The finding's path, and the path its declaration had before its last follow.
    paths: frozenset[str]
    ticket: str
    created_at: datetime
    ended_at: datetime
    reason: StaleReason


@dataclass(frozen=True)
class EndedFindings:
    counted: tuple[EndedFinding, ...]
    # Ended findings whose ledger reason is a decision or bookkeeping, not a changed file, by reason.
    not_counted: Mapping[str, int]


def ended_findings(connection: sqlite3.Connection) -> EndedFindings:
    """Withdrawn and waived findings, read as `Ledger._held` reads them."""
    recorded = dict(cast(list[tuple[int, str]], connection.execute("SELECT id,recorded_at FROM judgments").fetchall()))
    rows = cast(list[tuple[str, str, str, str, str, str, str, int, str, str, str, str, str]], connection.execute(
        "SELECT finding_id,rule,path,ticket,state,proposal,duplicate_of,withdrawn_by,declaration_gone_at,followed_from,"
        + "waive_provenance,created_at,updated_at FROM held_findings WHERE state IN ('withdrawn','waived') ORDER BY finding_id",
    ).fetchall())
    counted: list[EndedFinding] = []
    not_counted: Counter[str] = Counter()
    for (finding_id, rule, path, ticket, state, proposal, duplicate_of, withdrawn_by, declaration_gone_at, followed_from,
         provenance, created_at, updated_at) in rows:
        ended_at = _instant(updated_at)
        reason: StaleReason | DuplicateOf | ExceptionVerdict | WaiveProvenance
        if state == "waived":
            waive = WaiveProvenance(provenance)
            reason = LedgerWaive() if waive == WaiveProvenance.LEDGER else waive
        elif duplicate_of:
            reason = DuplicateOf(duplicate_of)
        elif declaration_gone_at:
            reason = DeclarationGone(declaration_gone_at)
        elif proposal:
            reason = ExceptionVerdict(withdrawn_by)
        else:
            reason = JudgedAgain(withdrawn_by)
            if withdrawn_by in recorded:
                ended_at = min(ended_at, _instant(recorded[withdrawn_by]))
        match reason:
            case JudgedAgain() | DeclarationGone() | LedgerWaive():
                counted.append(EndedFinding(finding_id, canonical_rule(rule), frozenset(filter(None, (path, followed_from))), ticket,
                                            _instant(created_at), ended_at, reason))
            case DuplicateOf():
                not_counted["bare rename duplicate"] += 1
            case ExceptionVerdict():
                not_counted["exception under a newer rule text"] += 1
            case WaiveProvenance():
                not_counted[f"{reason.value} waive"] += 1
    return EndedFindings(tuple(counted), dict(not_counted))


@dataclass(frozen=True)
class RemovedLine:
    number: int
    text: str


@dataclass(frozen=True)
class Hunk:
    old_start: int
    removed: tuple[RemovedLine, ...]
    added: tuple[str, ...]


@dataclass(frozen=True)
class FileChange:
    """One file between two revisions. before and after are its paths; an added or deleted file has its one path in both."""

    before: str
    after: str
    hunks: tuple[Hunk, ...]


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", *arguments],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise OrderWasteError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--no-textconv", "-U0", "-M")


def diff_changes(repo: Path, before: str, after: str) -> tuple[FileChange, ...]:
    """The changes from revision before to revision after."""
    return parse_diff(git(repo, "diff", *_DIFF_OPTIONS, before, after))


def parse_diff(output: str) -> tuple[FileChange, ...]:
    """The file changes of a `-U0` diff, in output order; lines outside a file's diff are skipped."""
    changes: list[FileChange] = []
    before = after = ""
    hunks: list[Hunk] = []
    removed: list[RemovedLine] = []
    added: list[str] = []
    old_line = old_start = 0

    def close_hunk() -> None:
        if removed or added:
            hunks.append(Hunk(old_start, tuple(removed), tuple(added)))
        removed.clear()
        added.clear()

    def close_file() -> None:
        close_hunk()
        if before or after:
            changes.append(FileChange(before or after, after or before, tuple(hunks)))
        hunks.clear()

    for line in output.splitlines():
        if line.startswith("diff --git "):
            close_file()
            before = after = ""
        elif line.startswith("rename from "):
            before = line.removeprefix("rename from ")
        elif line.startswith("rename to "):
            after = line.removeprefix("rename to ")
        elif line.startswith("--- "):
            before = line.removeprefix("--- a/") if line != "--- /dev/null" else ""
        elif line.startswith("+++ "):
            after = line.removeprefix("+++ b/") if line != "+++ /dev/null" else ""
        elif match := _HUNK_HEADER.match(line):
            close_hunk()
            old_start = old_line = int(match[1])
        elif line.startswith("-"):
            removed.append(RemovedLine(old_line, line[1:]))
            old_line += 1
        elif line.startswith("+"):
            added.append(line[1:])
    close_file()
    return tuple(changes)


def is_code(path: str) -> bool:
    return path.endswith(".rs") or PurePosixPath(path).name == "Cargo.toml"


def changed_lines(changes: tuple[FileChange, ...]) -> dict[tuple[FileChange, Hunk], tuple[int, ...]]:
    """The old line numbers each hunk really changed: a removed line whose text the change adds again anywhere was moved."""
    added = Counter(text.strip() for change in changes for hunk in change.hunks for text in hunk.added)
    result: dict[tuple[FileChange, Hunk], tuple[int, ...]] = {}
    for change in changes:
        for hunk in change.hunks:
            numbers: list[int] = []
            for line in hunk.removed:
                if added[line.text.strip()]:
                    added[line.text.strip()] -= 1
                else:
                    numbers.append(line.number)
            if numbers and is_code(change.before):
                result[(change, hunk)] = tuple(numbers)
    return result


_BLAME_HEADER = re.compile(r"^([0-9a-f]{40,64}) \d+ (\d+)")


@dataclass(frozen=True)
class BlamedLine:
    commit: str
    path: str


def blame(repo: Path, revision: str, path: str, numbers: Iterable[int], *options: str) -> dict[int, BlamedLine]:
    """Who last wrote each line at revision."""
    runs: list[list[int]] = []
    for number in sorted(set(numbers)):
        if runs and runs[-1][1] == number - 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    ranges = [argument for start, end in runs for argument in ("-L", f"{start},{end}")]
    output = git(repo, "blame", *options, "--line-porcelain", *ranges, revision, "--", path)
    lines: dict[int, BlamedLine] = {}
    source = ""
    number = 0
    for line in output.splitlines():
        if line.startswith("\t"):
            continue
        if header := _BLAME_HEADER.match(line):
            source, number = header[1], int(header[2])
        elif line.startswith("filename "):
            lines[number] = BlamedLine(source, line.removeprefix("filename "))
    return lines


@dataclass(frozen=True)
class OwnWork:
    """What a ticket's own commits did: its first-parent commits after its base, without merges, which bring in other work.

    removed and added hold the own net lines by file, under the file's last own name. A line an own commit removes or adds
    back cancels an earlier own commit's addition or removal of it, so an edit they put back leaves nothing; a reorder inside
    one commit stays.
    """

    commits: frozenset[str]
    removed: Mapping[str, Counter[str]]
    added: Mapping[str, Counter[str]]
    # The files the own commits renamed, under their last names.
    renamed: frozenset[str]

    @staticmethod
    def read(repo: Path, ticket: MergedTicket) -> OwnWork:
        if not ticket.base or ticket.base == ticket.checkpoint:
            return OwnWork(frozenset(), {}, {}, frozenset())
        output = git(repo, "log", "--first-parent", "--no-merges", "--reverse", "-p", "--format=%x00%H", *_DIFF_OPTIONS,
                     ticket.checkpoint, "^" + ticket.base)
        commits: set[str] = set()
        removed: defaultdict[str, Counter[str]] = defaultdict(Counter)
        added: defaultdict[str, Counter[str]] = defaultdict(Counter)
        renamed: set[str] = set()
        for commit, _, diff in (chunk.partition("\n") for chunk in output.split("\0")[1:]):
            commits.add(commit)
            for change in parse_diff(diff):
                path = change.after
                if change.before != path:
                    removed[path] += removed.pop(change.before, Counter())
                    added[path] += added.pop(change.before, Counter())
                    renamed.discard(change.before)
                    renamed.add(path)
                removes = Counter(line.text for hunk in change.hunks for line in hunk.removed)
                adds = Counter(text for hunk in change.hunks for text in hunk.added)
                removes_back, adds_back = removes & added[path], adds & removed[path]
                removed[path] = removed[path] - adds_back + (removes - removes_back)
                added[path] = added[path] - removes_back + (adds - adds_back)
        return OwnWork(frozenset(commits), dict(removed), dict(added), frozenset(renamed))

    def net_lines(self, lines: Mapping[str, Counter[str]], change: FileChange) -> Counter[str]:
        """The own net lines of change's file, which the own commits name by its path before or after the change."""
        net = Counter(lines.get(change.after, Counter()))
        if change.before != change.after:
            net.update(lines.get(change.before, Counter()))
        return net


def _take(lines: Counter[str], text: str) -> bool:
    """Takes one line of text from lines; False when none is left."""
    if lines[text] <= 0:
        return False
    lines[text] -= 1
    return True


def own_changes(repo: Path, ticket: MergedTicket, work: OwnWork) -> tuple[FileChange, ...]:
    """The ticket's own net change: the lines of its net change from base to checkpoint that its own commits made.

    A removed or added line is its own when the own net lines of its file hold its text, and a rename when an own commit made
    it. Work a merge brought in is not its own, also in a file whose own edit was put back.
    """
    kept: list[FileChange] = []
    for change in diff_changes(repo, ticket.base, ticket.checkpoint):
        removed = work.net_lines(work.removed, change)
        added = work.net_lines(work.added, change)
        hunks = tuple(hunk for hunk in (Hunk(hunk.old_start, tuple(line for line in hunk.removed if _take(removed, line.text)),
                                             tuple(text for text in hunk.added if _take(added, text)))
                                        for hunk in change.hunks) if hunk.removed or hunk.added)
        if hunks or change.before != change.after and change.after in work.renamed:
            kept.append(FileChange(change.before, change.after, hunks))
    return tuple(kept)


@dataclass(frozen=True, order=True)
class StaleFinding:
    """Kind (a): A's merged fix changed the file of B's finding, which then ended as stale. One per finding and pair."""

    finding_id: str
    path: str


@dataclass(frozen=True, order=True)
class ReworkedHunk:
    """Kind (b): the own net change of A's later merged ticket changed lines that B's merged fix wrote."""

    ticket: str
    path: str
    old_start: int


Waste = StaleFinding | ReworkedHunk


@dataclass(frozen=True)
class Measurement:
    waste: Mapping[tuple[Side, Side], frozenset[Waste]]
    # Canonical ids of the rules the ledger names for a merged code change or a counted finding.
    seen: frozenset[str]
    not_counted: Mapping[str, int]
    # Merged tickets whose own net change touches a code path.
    changing_tickets: int
    counted_findings: int


@dataclass(frozen=True)
class TicketChanges:
    """The merged tickets, the own net change of each, and the tickets that own each commit."""

    tickets: Mapping[str, MergedTicket]
    changes: Mapping[str, tuple[FileChange, ...]]
    owners: Mapping[str, frozenset[str]]

    @staticmethod
    def read(connection: sqlite3.Connection, repo: Path) -> TicketChanges:
        tickets = {ticket.id: ticket for ticket in merged_tickets(connection)}
        work = {id: OwnWork.read(repo, ticket) for id, ticket in tickets.items()}
        owners: defaultdict[str, set[str]] = defaultdict(set)
        for id, own in work.items():
            for commit in own.commits:
                owners[commit].add(id)
        changes = {id: own_changes(repo, tickets[id], own) for id, own in work.items() if own.commits}
        return TicketChanges(tickets, changes, {commit: frozenset(ids) for commit, ids in owners.items()})

    def touched(self) -> dict[str, frozenset[str]]:
        """The tickets that changed each code path, under its path before and after the change."""
        by_path: defaultdict[str, set[str]] = defaultdict(set)
        for id, ticket_changes in self.changes.items():
            for change in (change for change in ticket_changes if is_code(change.after)):
                by_path[change.before].add(id)
                by_path[change.after].add(id)
        return {path: frozenset(ids) for path, ids in by_path.items()}


WasteByPair = defaultdict[tuple[Side, Side], set[Waste]]


def _rework(repo: Path, history: TicketChanges, waste: WasteByPair) -> None:
    """Kind (b): for each line of a ticket's own net change, the earlier ticket whose own commit wrote it."""
    for id, ticket_changes in history.changes.items():
        ticket = history.tickets[id]
        for (change, hunk), numbers in changed_lines(ticket_changes).items():
            reworked = ReworkedHunk(id, change.after, hunk.old_start)
            for line in blame(repo, ticket.base, change.before, numbers, "-w", "-M", "-C").values():
                for earlier in history.owners.get(line.commit, frozenset()) - {id}:
                    for changed_by in ticket.sides(change.after):
                        for redone in history.tickets[earlier].sides(line.path) - {changed_by}:
                            waste[(changed_by, redone)].add(reworked)


def _stale(findings: EndedFindings, history: TicketChanges, touched: Mapping[str, frozenset[str]],
           waste: WasteByPair) -> Counter[str]:
    """Kind (a): a ticket of another rule merged a change to the finding's file after its creation and by its end.

    Returns the findings left without one, with the ledger's reasons for the findings it does not count.
    """
    not_counted = Counter(findings.not_counted)
    for finding in findings.counted:
        redone = RuleFix(finding.rule)
        stale: defaultdict[Side, set[str]] = defaultdict(set)
        for path in finding.paths:
            for id in touched.get(path, frozenset()) - {finding.ticket}:
                if finding.created_at < history.tickets[id].merged_at <= finding.ended_at:
                    for changed_by in history.tickets[id].sides(path) - {redone}:
                        stale[changed_by].add(path)
        if not stale:
            not_counted["no other rule's merged fix changed its file"] += 1
        for changed_by, paths in stale.items():
            waste[(changed_by, redone)].add(StaleFinding(finding.finding_id, min(paths)))
    return not_counted


def measure(connection: sqlite3.Connection, repo: Path) -> Measurement:
    history = TicketChanges.read(connection, repo)
    touched = history.touched()
    findings = ended_findings(connection)
    waste: WasteByPair = defaultdict(set)
    _rework(repo, history, waste)
    not_counted = _stale(findings, history, touched, waste)
    seen = {side.rule for path, ids in touched.items() for id in ids
            for side in history.tickets[id].sides(path) if isinstance(side, RuleFix)}
    seen.update(finding.rule for finding in findings.counted)
    return Measurement({pair: frozenset(items) for pair, items in waste.items()}, frozenset(seen), dict(not_counted),
                       len({id for ids in touched.values() for id in ids}), len(findings.counted))


class PairRow(TypedDict):
    changed_by: str
    redone: str
    stale_findings: int
    reworked_hunks: int
    total: int
    files: list[str]
    analysis: str


class EdgeRow(TypedDict):
    edge: str
    seen: bool


class Report(TypedDict):
    changing_tickets: int
    counted_findings: int
    pairs: list[PairRow]
    edges_without_waste: list[EdgeRow]
    not_counted: dict[str, int]


def report(measurement: Measurement) -> Report:
    pairs: list[PairRow] = []
    for (changed_by, redone), items in measurement.waste.items():
        stale = sum(1 for item in items if isinstance(item, StaleFinding))
        pairs.append(PairRow(changed_by=changed_by.label(), redone=redone.label(), stale_findings=stale,
                             reworked_hunks=len(items) - stale, total=len(items), files=sorted({item.path for item in items}),
                             analysis=analysis_order(changed_by, redone).value))
    pairs.sort(key=lambda row: (-row["total"], row["changed_by"], row["redone"]))
    wasted = {(first, then) for changed_by, redone in measurement.waste
              for first in changed_by.slugs() if isinstance(changed_by, RuleFix)
              for then in redone.slugs() if isinstance(redone, RuleFix)}
    seen = {slug for rule in measurement.seen if (slug := rule_slug(rule))}
    edges = [EdgeRow(edge=edge.label(), seen=bool(set(edge.before) & seen and set(edge.after) & seen))
             for edge in ANALYSIS_EDGES
             if not any(pair in wasted or pair[::-1] in wasted for pair in edge.pairs())]
    return Report(changing_tickets=measurement.changing_tickets, counted_findings=measurement.counted_findings, pairs=pairs,
                  edges_without_waste=edges, not_counted=dict(sorted(measurement.not_counted.items())))


def render(data: Report) -> str:
    lines = [
        "# Wasted work by rule order",
        "",
        f"{data['changing_tickets']} merged tickets changed code; {data['counted_findings']} withdrawn or waived findings ended "
        + "because their file changed.",
        "A changed code that B's finding (stale) or B's earlier fix (reworked) had to read again. "
        + "`<family> read` is a read ticket whose ledger rows name no rule for the change.",
        "Analysis: where rule_order_analysis.md puts A, by one of its edges or only by its proposed order; "
        + "A first prevents the waste.",
        "",
        "| A changed | B redone | stale findings | reworked hunks | total | files | analysis |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        *(f"| {row['changed_by']} | {row['redone']} | {row['stale_findings']} | {row['reworked_hunks']} | {row['total']} "
          + f"| {len(row['files'])} | {row['analysis']} |" for row in data["pairs"]),
    ]
    if data["not_counted"]:
        lines += ["", "Not counted: " + ", ".join(f"{count} {reason}" for reason, count in data["not_counted"].items()) + "."]
    seen = [row["edge"] for row in data["edges_without_waste"] if row["seen"]]
    unseen = [row["edge"] for row in data["edges_without_waste"] if not row["seen"]]
    lines += ["", "## Analysis edges with no observed waste", "",
              f"Seen in the data ({len(seen)}): both sides have a merged fix or an ended finding.",
              *(f"- {edge}" for edge in seen), "",
              f"Not seen ({len(unseen)}): a side has no merged fix and no ended finding.",
              *(f"- {edge}" for edge in unseen)]
    return "\n".join(lines) + "\n"


def open_read_only(ledger: Path) -> sqlite3.Connection:
    if not ledger.is_file():
        raise OrderWasteError(f"{ledger}: no ledger file")
    return sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)


class Arguments(argparse.Namespace):
    ledger: Path = Path()
    repo: Path = Path()
    json: bool = False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--ledger", type=Path, required=True)
    _ = parser.add_argument("--repo", type=Path, required=True)
    _ = parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv, namespace=Arguments())
    try:
        connection = open_read_only(arguments.ledger)
        try:
            data = report(measure(connection, arguments.repo))
        finally:
            connection.close()
    except (OSError, ValueError, sqlite3.Error, OrderWasteError, style_ledger.LedgerError) as error:
        print(f"order_waste: {error}", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2) if arguments.json else render(data), end="\n" if arguments.json else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
