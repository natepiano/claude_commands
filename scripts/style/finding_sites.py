"""Evidence for retaining a finding's source site, never a verdict on changed code."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from typing import final

import declarations
import style_footprint


@final
@dataclass(frozen=True)
class UnchangedSite:
    path: str
    line: int


@final
@dataclass(frozen=True)
class GoneSite:
    reason: str


@final
@dataclass(frozen=True)
class NeedsJudgment:
    reason: str


SiteEvidence = UnchangedSite | GoneSite | NeedsJudgment


@dataclass(frozen=True)
class EnclosingItem:
    identity: tuple[str, ...]
    kind: str
    first: int
    last: int


class UnreadableSourceItems(ValueError):
    """The item parser could not establish all enclosing identities in this blob."""


class SourceSites:
    """A single immutable UTF-8 blob; parsing and ownership indexes are shared by its findings."""

    def __init__(self, source: str) -> None:
        self.source: str = source
        self.lines: tuple[str, ...] = tuple(source.splitlines(keepends=True))
        self._owners: dict[int, declarations.DeclaredItem | declarations.NoDeclarationAtLine] = {}

    @cached_property
    def declared(self) -> declarations.DeclaredFile:
        return declarations.read_declarations(self.source)

    def owner(self, line: int) -> declarations.DeclaredItem | declarations.NoDeclarationAtLine:
        if line not in self._owners:
            self._owners[line] = self.declared.declaration_at(line)
        return self._owners[line]

    @cached_property
    def identities(self) -> Counter[declarations.Declaration]:
        return Counter(item.declaration for item in self.declared.declared_items)

    @cached_property
    def occurrences(self) -> dict[declarations.DeclaredItem, Counter[str]]:
        return {item: Counter(line.strip() for line in self.lines[item.first_line - 1:item.last_line])
                for item in self.declared.declared_items}

    @cached_property
    def skeleton(self) -> tuple[str, ...]:
        """Imports, impl headers and unsupported source remain outside proved fn/type bodies."""
        covered = {line for item in self.declared.declared_items
                   if item.declaration.keyword in {'fn', 'struct', 'enum', 'union', 'type', 'trait'}
                   for line in range(item.first_line, item.last_line + 1)}
        return tuple(text for line, text in enumerate(self.lines, 1) if line not in covered)

    @cached_property
    def item_reading(self) -> tuple[EnclosingItem, ...] | UnreadableSourceItems:
        spans = style_footprint.token_spans(self.source)
        if any(span.token.kind == "unknown" for span in spans):
            return UnreadableSourceItems("unreadable Rust tokens")
        words = tuple(span.token.value for span in spans)
        ends = style_footprint.delimiter_ends(words)
        offsets = [0]
        for line in self.lines:
            offsets.append(offsets[-1] + len(line))
        result: list[EnclosingItem] = []

        def visit(start: int, end: int, owner: tuple[str, ...]) -> bool:
            parsed = style_footprint.source_items(words[start:end])
            if parsed is None:
                return False
            for item in parsed:
                first, keyword, last = start + item.start, start + item.keyword, start + item.end
                identity = (*owner, item.title)
                result.append(EnclosingItem(identity, item.kind,
                                            bisect_right(offsets, spans[first].start),
                                            bisect_right(offsets, spans[last - 1].end - 1)))
                if item.kind in {"mod", "impl", "trait"}:
                    opening = next((word for word in range(keyword, last) if words[word] == "{" and ends.get(word) == last - 1), -1)
                    if opening >= 0:
                        if not visit(opening + 1, last - 1, identity):
                            return False
            return True

        if spans and not visit(0, len(words), ()):
            return UnreadableSourceItems("partly unreadable Rust items")
        return tuple(result)

    @property
    def items(self) -> tuple[EnclosingItem, ...]:
        reading = self.item_reading
        if isinstance(reading, UnreadableSourceItems):
            raise reading
        return reading

    def module_owner(self, line: int) -> tuple[tuple[str, ...], ...]:
        return tuple(item.identity for item in self.items if item.kind == "mod" and item.first <= line <= item.last)

    def same_owner(self, old_line: int, other: SourceSites, new_line: int) -> bool:
        old = self.owner(old_line)
        new = other.owner(new_line)
        if self.module_owner(old_line) != other.module_owner(new_line):
            return False
        text = self.lines[old_line - 1].strip()
        if isinstance(old, declarations.DeclaredItem) and isinstance(new, declarations.DeclaredItem):
            if any(isinstance(owner, (declarations.UnreadableImplementedType, declarations.UnreadableImplementedTrait))
                   for owner in (old.implemented_type, old.implemented_trait, new.implemented_type, new.implemented_trait)):
                return False
            return ((old.declaration, old.implemented_type, old.implemented_trait)
                    == (new.declaration, new.implemented_type, new.implemented_trait)
                    and self.identities[old.declaration] == other.identities[new.declaration] == 1
                    and self.occurrences[old][text] == other.occurrences[new][text] == 1)
        if isinstance(old, declarations.DeclaredItem) or isinstance(new, declarations.DeclaredItem):
            return False
        # Outside readable declarations require a globally unique line in each file.
        return (sum(line.strip() == text for line in self.lines)
                == sum(line.strip() == text for line in other.lines) == 1)

    def locates_subject(self, line: int, subject: str) -> bool:
        """A type proposal's before text must name source in the item at its anchor."""
        owner = self.owner(line)
        if isinstance(owner, declarations.DeclaredItem):
            source = "".join(self.lines[owner.first_line - 1:owner.last_line])
        else:
            enclosing = [item for item in self.items if item.first <= line <= item.last]
            if not enclosing:
                return False
            item = min(enclosing, key=lambda item: item.last - item.first)
            source = "".join(self.lines[item.first - 1:item.last])
        before = style_footprint.tokens(subject)
        words = style_footprint.tokens(source)
        return bool(before) and any(words[start:start + len(before)] == before
                                    for start in range(len(words) - len(before) + 1))

    def same_item(self, old_line: int, other: SourceSites, new_line: int) -> bool:
        old = self.owner(old_line)
        new = other.owner(new_line)
        if not isinstance(old, declarations.DeclaredItem) or not isinstance(new, declarations.DeclaredItem):
            # Impl headers have no named declaration. Use the existing item parser's complete span.
            before = [item for item in self.items if item.kind == "impl" and item.first <= old_line <= item.last]
            after = [item for item in other.items if item.kind == "impl" and item.first <= new_line <= item.last]
            if len(before) != 1 or len(after) != 1:
                return False
            a, b = before[0], after[0]
            return (a.identity == b.identity
                    and sum(item.identity == a.identity for item in self.items) == 1
                    and sum(item.identity == b.identity for item in other.items) == 1
                    and self.lines[a.first - 1:a.last] == other.lines[b.first - 1:b.last]
                    and self.skeleton == other.skeleton)
        if old.declaration.keyword not in {'fn', 'struct', 'enum', 'union', 'type', 'trait'}:
            return False
        return (self.same_owner(old_line, other, new_line)
                and self.lines[old.first_line - 1:old.last_line] == other.lines[new.first_line - 1:new.last_line]
                and self.skeleton == other.skeleton)


def unchanged_lines(patch: str, before: SourceSites, after: SourceSites) -> dict[int, int]:
    """Map only equal intervals of the existing diff parser, including the unchanged tail."""
    result: dict[int, int] = {}
    old_cursor = new_cursor = 1
    for hunk in style_footprint.parse_hunks(patch):
        # A zero-length old range inserts AFTER old_start, unlike a nonempty range.
        boundary = hunk.old_start + (hunk.old_count == 0)
        for line in range(old_cursor, boundary):
            result[line] = new_cursor + line - old_cursor
        new_cursor += boundary - old_cursor + len(hunk.added)
        old_cursor = boundary + hunk.old_count
    for line in range(old_cursor, len(before.lines) + 1):
        result[line] = new_cursor + line - old_cursor
    # Git disregards some line-ending distinctions; byte equality is authoritative.
    return {old: new for old, new in result.items()
            if 1 <= new <= len(after.lines) and before.lines[old - 1] == after.lines[new - 1]}


def absent_subject(kind: str, rule: str, before: str, line: str,
                   words: frozenset[str], literals: frozenset[str]) -> SiteEvidence:
    if (kind == 'rename' and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', before)
            and re.search(r'\b' + re.escape(before) + r'\b', line) and before not in words):
        return GoneSite('before identifier absent from all current Rust code and comments')
    if rule == 'global:rust/no-magic-values.md':
        original = {token.value for token in style_footprint.tokens(line) if token.kind == 'literal'}
        if original and not original.intersection(literals):
            return GoneSite('all original literal spellings absent from current Rust tokens')
    return NeedsJudgment('source subject disappearance not proved')
