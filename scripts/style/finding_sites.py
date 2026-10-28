"""Evidence for retaining a finding's source site, never a verdict on changed code."""

from __future__ import annotations

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
class NeedsJudgment:
    reason: str


SiteEvidence = UnchangedSite | NeedsJudgment


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
    def span_reading(self) -> tuple[style_footprint.TokenSpan, ...] | style_footprint.UnreadableTokenSpans:
        try:
            return style_footprint.token_spans(self.source)
        except style_footprint.UnreadableTokenSpans as error:
            return error

    @property
    def spans(self) -> tuple[style_footprint.TokenSpan, ...]:
        reading = self.span_reading
        if isinstance(reading, style_footprint.UnreadableTokenSpans):
            raise reading
        return reading

    @cached_property
    def item_reading(self) -> tuple[EnclosingItem, ...] | UnreadableSourceItems:
        try:
            spans = self.spans
        except style_footprint.UnreadableTokenSpans:
            return UnreadableSourceItems("unreadable Rust tokens")
        words = tuple(span.token.value for span in spans)
        ends = style_footprint.delimiter_ends(words)
        attributes = dict(style_footprint.attribute_spans(words))
        offsets = [0]
        for line in self.lines:
            offsets.append(offsets[-1] + len(line))
        result: list[EnclosingItem] = []

        def record(item: style_footprint.SourceItem, start: int, owner: tuple[str, ...]) -> bool:
            first, keyword, last = start + item.start, start + item.keyword, start + item.end
            identity = (*owner, item.title)
            result.append(EnclosingItem(identity, item.kind,
                                        bisect_right(offsets, spans[first].start),
                                        bisect_right(offsets, spans[last - 1].end - 1)))
            # Container bodies consist of items. Function, type and initializer bodies also
            # contain statements, but any nested item still belongs to this complete chain.
            cursor = keyword + 1
            while cursor < last:
                if words[cursor] in {"{", "(", "["}:
                    close = ends.get(cursor)
                    if close is None or close >= last:
                        return False
                    if item.kind in {"mod", "impl", "trait"} and words[cursor] == "{" and close == last - 1:
                        return visit(cursor + 1, close, identity)
                    if not nested(cursor + 1, close, identity,
                                  at_item=words[cursor] == "{" and item.kind not in {"struct", "enum", "union"}):
                        return False
                    cursor = close + 1
                else:
                    cursor += 1
            return True

        def statement_start(start: int, keyword: int, end: int) -> bool:
            """Only recognized expressions/statements may occupy a non-item boundary."""
            word = words[start]
            if keyword != start:
                # These qualifiers can lead block/closure expressions as well as items.
                prefix = words[start:keyword]
                return (all(part in {"async", "unsafe", "const"} for part in prefix)
                        and words[keyword:keyword + 1] in (("{",), ("move",), ("|",), ("||",)))
            if word in {"let", "if", "else", "match", "while", "for", "loop", "return", "break", "continue",
                        "yield", "move", "try", "const", "{", "(", "[", ";", "&", "&&", "*", "!", "-", "|", "||"}:
                return True
            if word == "'":
                return (start + 2 < end and spans[start + 1].token.kind == "identifier"
                        and words[start + 2] == ":")
            if spans[start].token.kind == "literal":
                return True
            # A plain name starts a path, call, macro, operator expression or a bare value.
            # An unknown modifier followed by an item keyword is none of these: refuse it.
            return (spans[start].token.kind == "identifier" and word not in style_footprint.RUST_KEYWORDS - {"self", "super", "crate", "Self", "true", "false"}
                    and (start + 1 == end or words[start + 1] in {
                        "::", "(", "[", "{", "!", ".", "?", ";", "=", "+", "-", "*", "/", "%", "&", "&&", "|", "||",
                        "^", "<", ">", "==", "!=", "<=", ">=",
                    }))

        def nested(start: int, end: int, owner: tuple[str, ...], *, at_item: bool) -> bool:
            cursor = start
            while cursor < end:
                keyword = cursor
                if at_item:
                    # Attributes, visibility and function qualifiers belong to the item that follows.
                    while keyword in attributes:
                        keyword = attributes[keyword]
                    keyword, _ = style_footprint.item_prefix(words, ends, keyword)
                word = words[keyword] if keyword < end else ""
                if (at_item and word in {"fn", "impl", "trait", "mod", "struct", "enum", "union", "type", "const", "static"}
                        and not (word == "const" and words[keyword + 1:keyword + 2] == ("{",))):
                    last = style_footprint.item_end(words, ends, keyword + 1,
                                                    body=word not in {"type", "const", "static"})
                    if last is None or last > end:
                        return False
                    items = style_footprint.source_items(words[cursor:last])
                    if items is None or len(items) != 1 or not record(items[0], cursor, owner):
                        return False
                    cursor = last
                    at_item = True
                elif at_item and not statement_start(cursor, keyword, end):
                    return False
                elif words[cursor] in {"{", "(", "["}:
                    close = ends.get(cursor)
                    block = words[cursor] == "{"
                    if close is None or close >= end or not nested(cursor + 1, close, owner, at_item=block):
                        return False
                    cursor = close + 1
                    at_item = block
                else:
                    at_item = words[cursor] == ";"
                    cursor += 1
            return True

        def visit(start: int, end: int, owner: tuple[str, ...]) -> bool:
            parsed = style_footprint.source_items(words[start:end])
            if parsed is None:
                return False
            for item in parsed:
                if not record(item, start, owner):
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

    def owner_chain(self, line: int) -> tuple[tuple[str, ...], ...]:
        return tuple(item.identity for item in self.items if item.first <= line <= item.last)

    def same_owner(self, old_line: int, other: SourceSites, new_line: int) -> bool:
        old = self.owner(old_line)
        new = other.owner(new_line)
        chain = self.owner_chain(old_line)
        if (chain != other.owner_chain(new_line)
                or any(sum(item.identity == identity for item in self.items) != 1
                       or sum(item.identity == identity for item in other.items) != 1 for identity in chain)):
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

