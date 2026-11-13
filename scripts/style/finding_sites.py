"""Evidence for retaining a finding's source site, never a verdict on changed code."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cached_property
from enum import Enum, auto
import re
import zlib
from typing import final

from scripts.style import declarations
from scripts.style import style_footprint


@final
@dataclass(frozen=True)
class FindingAnchor:
    """The commit and coordinate whose bytes a finding actually names."""

    commit: str
    path: str
    line: int


@final
@dataclass(frozen=True)
class RenameJudgmentSite:
    """A current coordinate eligible for a new rename proof, never historical continuity."""

    anchor: FindingAnchor


@final
@dataclass(frozen=True)
class UnchangedSite:
    path: str
    line: int
    anchor: FindingAnchor


@final
@dataclass(frozen=True)
class NeedsJudgment:
    reason: str


@final
@dataclass(frozen=True)
class FilingEligibleWithoutHistory:
    """An existing finding may be filed, but no judgment proves its source bytes."""

    path: str
    line: int

    @property
    def reason(self) -> str:
        return "no judging ticket for source anchor"


SiteEvidence = UnchangedSite | NeedsJudgment | FilingEligibleWithoutHistory


@dataclass(frozen=True)
class ReadItemHeader:
    """An item keyword at item position and its independently read header."""

    keyword: int
    end: int
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class UnreadItemHeader:
    keyword: int
    reason: str


@dataclass(frozen=True)
class ReadItemBody:
    """The only body token range whose nested syntax has been read completely."""

    tokens: range


@dataclass(frozen=True)
class UnreadItemBody:
    lines: range


@dataclass(frozen=True)
class EnclosingItem:
    identity: tuple[str, ...]
    kind: str
    first: int
    last: int
    header_reading: ReadItemHeader | UnreadItemHeader
    body_reading: ReadItemBody | UnreadItemBody
    token_start: int
    token_end: int

    @property
    def unproved_lines(self) -> range:
        return self.body_reading.lines if isinstance(self.body_reading, UnreadItemBody) else range(0)

    @property
    def header(self) -> tuple[str, ...]:
        return self.header_reading.tokens if isinstance(self.header_reading, ReadItemHeader) else ()


class UnreadableSourceItems(ValueError):
    """The item parser could not establish all enclosing identities in this blob."""


class NestedSyntax(Enum):
    STATEMENTS = auto()
    FIELDS = auto()
    VARIANTS = auto()
    ARMS = auto()
    CONTINUATION = auto()
    PARAMETERS = auto()


class SourceSites:
    """A single immutable UTF-8 blob; parsing and ownership indexes are shared by its findings."""

    def __init__(self, source: str, reader: Callable[[str], declarations.DeclaredFile] = declarations.read_declarations) -> None:
        self._read_declarations: Callable[[str], declarations.DeclaredFile] = reader
        self._compressed_source: bytes = zlib.compress(source.encode(), level=1)
        self._owners: dict[int, declarations.DeclaredItem | declarations.NoDeclarationAtLine] = {}

    @property
    def source(self) -> str:
        return zlib.decompress(self._compressed_source).decode()

    @cached_property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.source.splitlines(keepends=True))

    def release_parsed(self) -> None:
        """Release indexes after the current findings finish their source reads."""
        self._owners.clear()
        for name in ("lines", "declared", "identities", "occurrences", "skeleton", "span_reading", "item_reading"):
            if name in self.__dict__:
                del self.__dict__[name]

    @cached_property
    def declared(self) -> declarations.DeclaredFile:
        return self._read_declarations(self.source)

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
            return self.declared.source_token_spans
        except style_footprint.UnreadableTokenSpans as error:
            return style_footprint.UnreadableTokenSpans(str(error))

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
        openers = {closing: opening for opening, closing in ends.items()}
        attributes = dict(style_footprint.attribute_spans(words))
        offsets = [0]
        for line in self.lines:
            offsets.append(offsets[-1] + len(line))
        result: list[EnclosingItem] = []

        item_keywords = {
            "fn", "struct", "enum", "union", "trait", "impl", "mod", "type", "const", "static",
            "macro_rules", "use", "extern",
        }
        # item_prefix remains the only qualifier scanner. This grammar validates its
        # complete result, including order and restricted visibility, before recording.
        name = r"(?:r#)?[A-Za-z_][A-Za-z_0-9]*"
        visibility_path = rf"{name}(?: :: {name})*"
        qualifiers = re.compile(
            rf"(?:pub(?: \( (?:crate|self|super|in {visibility_path}) \))? )?"
            + r"(?:default )?(?:const )?(?:async )?(?:unsafe )?(?:extern(?: ABI)? )?"
        )

        def item_prefix_is_closed(start: int, keyword: int) -> bool:
            while start in attributes:
                start = attributes[start]
            spelling = " ".join("ABI" if spans[index].token.kind == "literal" else words[index]
                                for index in range(start, keyword))
            return qualifiers.fullmatch(spelling + (" " if spelling else "")) is not None

        def record(item: style_footprint.SourceItem, start: int, owner: tuple[str, ...]) -> bool:
            first, keyword, last = start + item.start, start + item.keyword, start + item.end
            first_line = bisect_right(offsets, spans[first].start)
            last_line = bisect_right(offsets, spans[last - 1].end - 1)
            identity = (*owner, item.title)
            position = len(result)
            result.append(EnclosingItem(identity, item.kind, first_line, last_line,
                                        UnreadItemHeader(keyword, "item header not established"),
                                        UnreadItemBody(range(first_line, last_line + 1)),
                                        first, last))

            def refuse_body() -> bool:
                # Body uncertainty cannot erase an already proved header. Discard
                # hidden descendants while preserving independent sibling owners.
                retained = replace(result[position], body_reading=UnreadItemBody(range(first_line, last_line + 1)))
                del result[position:]
                result.append(retained)
                return True

            if not item_prefix_is_closed(first, keyword):
                return refuse_body()
            if item.kind in {"macro_rules", "macro"}:
                opening = keyword + (3 if item.kind == "macro_rules" else 2)
                if opening not in ends:
                    return refuse_body()
                result[position] = replace(result[position],
                    header_reading=ReadItemHeader(keyword, opening, words[first:opening]),
                    body_reading=UnreadItemBody(range(bisect_right(offsets, spans[opening].start),
                                                     bisect_right(offsets, spans[ends[opening]].end - 1) + 1)))
                return True
            cursor = keyword + 1
            while cursor < last:
                if words[cursor] in {"{", "(", "["}:
                    close = ends.get(cursor)
                    if close is None or close >= last:
                        return refuse_body()
                    if words[cursor] == "{" and close == last - 1:
                        result[position] = replace(result[position],
                            header_reading=ReadItemHeader(keyword, cursor, words[first:cursor]))
                    if item.kind in {"mod", "impl", "trait", "extern"} and words[cursor] == "{" and close == last - 1:
                        if not visit(cursor + 1, close, identity):
                            return refuse_body()
                        result[position] = replace(result[position], body_reading=ReadItemBody(range(cursor, last)))
                        return True
                    syntax = (NestedSyntax.PARAMETERS if item.kind == "fn" and words[cursor] == "("
                              else NestedSyntax.CONTINUATION)
                    if words[cursor] == "{":
                        syntax = (NestedSyntax.FIELDS if item.kind in {"struct", "union"}
                                  else NestedSyntax.VARIANTS if item.kind == "enum" else NestedSyntax.STATEMENTS)
                    if not nested(cursor + 1, close, identity, syntax):
                        return refuse_body()
                    cursor = close + 1
                else:
                    cursor += 1
            if isinstance(result[position].header_reading, UnreadItemHeader):
                result[position] = replace(result[position],
                    header_reading=ReadItemHeader(keyword, last, words[first:last]))
            header = result[position].header_reading
            assert isinstance(header, ReadItemHeader)
            result[position] = replace(result[position], body_reading=ReadItemBody(range(header.end, last)))
            return True

        def qualified_path(start: int, end: int) -> bool:
            depth = 0
            for cursor in range(start, end):
                word = words[cursor]
                if word == "<":
                    depth += 1
                elif word == ">":
                    depth -= 1
                    if depth == 0:
                        return (cursor > start + 1 and words[cursor + 1:cursor + 2] == ("::",)
                                and cursor + 2 < end and spans[cursor + 2].token.kind == "identifier")
                elif word not in {"::", "&", "'", ","} and spans[cursor].token.kind != "identifier":
                    return False
            return False

        def statement_start(start: int, end: int) -> bool:
            """Admit named expression starts; subsequent tokens remain item-aware."""
            word = words[start]
            expression_starts = {
                "let", "if", "else", "match", "while", "for", "loop", "return", "break", "continue",
                "yield", "move", "try", "const", "async", "unsafe", "self", "super", "crate", "Self", "true", "false",
            }
            if word in style_footprint.RUST_KEYWORDS - expression_starts:
                return False
            if word == "'":
                return (start + 2 < end and spans[start + 1].token.kind == "identifier"
                        and words[start + 2] == ":")
            if word == "<":
                return qualified_path(start, end)
            return (spans[start].token.kind in {"literal", "identifier"}
                    or word in {"{", "(", "[", ";", "&", "&&", "*", "!", "-", "|", "||", ".", "::"})

        def starts_item(cursor: int, end: int, parameter_type: bool = False) -> bool:
            word = words[cursor]
            following = words[cursor + 1:cursor + 2]
            if word == "impl" and parameter_type:
                return False  # An argument-position impl Trait is a type bound.
            if word == "const" and following in (("{",), ("move",), ("|",), ("||",)):
                return False
            if word in {"union", "macro_rules"}:
                return following == ("!",) if word == "macro_rules" else (
                    cursor + 1 < end and spans[cursor + 1].token.kind == "identifier")
            # A function pointer has no declared name, including after its ABI.
            # Reuse the qualifier scan; never consume a named fn or extern item.
            if word in {"fn", "extern"}:
                keyword, _ = style_footprint.item_prefix(words, ends, cursor)
                if words[keyword:keyword + 2] == ("fn", "("):
                    return False
            return word in item_keywords

        def nested(start: int, end: int, owner: tuple[str, ...], syntax: NestedSyntax) -> bool:
            cursor, segment = start, start
            parameter_type, impl_bound = False, False
            at_item = syntax in {NestedSyntax.STATEMENTS, NestedSyntax.ARMS}
            at_field = syntax in {NestedSyntax.FIELDS, NestedSyntax.VARIANTS}
            while cursor < end:
                if words[cursor] == "$" or spans[cursor].token.kind == "unknown":
                    return False
                if words[cursor] == "@":
                    pattern = cursor + 1
                    while pattern < end and (spans[pattern].token.kind == "identifier" or words[pattern] == "::"):
                        pattern += 1
                    if (cursor == start or spans[cursor - 1].token.kind != "identifier"
                            or pattern == cursor + 1 or words[pattern:pattern + 1] != ("{",)):
                        return False
                statement = cursor
                while statement in attributes:
                    statement = attributes[statement]
                if statement >= end:
                    return True
                if statement != cursor:
                    cursor = statement
                    segment = cursor
                if at_field:
                    field, _ = style_footprint.item_prefix(words, ends, cursor)
                    # Field visibility is the shared pub scanner's closed subset.
                    if field != cursor and (words[cursor] != "pub" or not item_prefix_is_closed(cursor, field)):
                        return False
                    if words[field:field + 2] == (".", "."):
                        cursor = field + 2
                    elif field < end and spans[field].token.kind == "identifier" and words[field] not in item_keywords:
                        cursor = field + 1
                        if words[cursor:cursor + 1] == (":",):
                            cursor += 1
                    else:
                        return False
                    at_field, at_item = False, False
                    continue
                keyword, _ = style_footprint.item_prefix(words, ends, cursor) if at_item else (cursor, "")
                word = words[keyword] if keyword < end else ""
                if at_item and keyword < end and starts_item(keyword, end, parameter_type):
                    if not item_prefix_is_closed(cursor, keyword):
                        return False
                    body = word not in {"type", "const", "static", "use"} and not (
                        word == "extern" and words[keyword + 1:keyword + 2] == ("crate",))
                    last = style_footprint.item_end(words, ends, keyword + 1, body=body)
                    if last is None or last > end:
                        return False
                    items = style_footprint.source_items(words[cursor:last])
                    if items is None or len(items) != 1 or not record(items[0], cursor, owner):
                        return False
                    cursor = segment = last
                    at_item = True
                elif starts_item(cursor, end, parameter_type):
                    # Every unrecorded item keyword refuses, even after an expression
                    # operator, closure prefix, generic argument or opaque group.
                    return False
                elif at_item and not statement_start(cursor, end):
                    return False
                elif words[cursor] in {"{", "(", "["}:
                    close = ends.get(cursor)
                    if close is None or close >= end:
                        return False
                    if words[cursor] == "{" and impl_bound:
                        return False  # A bound has no body; never hide an impl item here.
                    child = NestedSyntax.CONTINUATION
                    block = words[cursor] == "{"
                    if block:
                        lead = words[segment:cursor]
                        child = NestedSyntax.STATEMENTS
                        if lead[:1] == ("match",):
                            child = NestedSyntax.ARMS
                        elif (cursor > segment and spans[cursor - 1].token.kind == "identifier"
                              and words[cursor - 1] not in {"else", "unsafe", "async", "const", "try", "loop"}
                              and lead[:1] not in (("if",), ("while",), ("for",))):
                            child = NestedSyntax.FIELDS
                    if not nested(cursor + 1, close, owner, child):
                        return False
                    cursor = close + 1
                    # Only an actual statement block creates a fresh item position.
                    at_item = block and child in {NestedSyntax.STATEMENTS, NestedSyntax.ARMS}
                    if block and cursor < end:
                        continuation = words[cursor]
                        if continuation == "=>" and syntax == NestedSyntax.ARMS:
                            cursor += 1
                            segment = cursor
                            at_item = True
                        elif continuation == "=" and child == NestedSyntax.FIELDS:
                            cursor += 1
                            at_item = True
                        elif continuation == "," and syntax in {NestedSyntax.ARMS, NestedSyntax.FIELDS, NestedSyntax.VARIANTS}:
                            at_item = False
                        elif continuation in {"?", "."}:
                            at_item = False
                        elif continuation == ">" and style_footprint.closes_named_generics(
                                words, cursor, openers):
                            # A const argument's block continues its proved turbofish;
                            # its closing angle is not the start of a new statement.
                            at_item = False
                        elif continuation == "else":
                            at_item = True
                    if at_item:
                        segment = cursor
                else:
                    word = words[cursor]
                    if syntax == NestedSyntax.PARAMETERS:
                        if word == ":":
                            parameter_type = True
                        elif word in {",", "="}:
                            parameter_type = False
                        elif word == "impl" and parameter_type:
                            impl_bound = True
                    if word == ";":
                        at_item = syntax == NestedSyntax.STATEMENTS
                        segment = cursor + 1
                    elif word == ",":
                        at_field = syntax in {NestedSyntax.FIELDS, NestedSyntax.VARIANTS}
                        at_item = syntax == NestedSyntax.ARMS
                        segment = cursor + 1
                    elif word == "=>" and syntax == NestedSyntax.ARMS:
                        at_item = True
                        segment = cursor + 1
                    else:
                        at_item = False
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
        if any(line in item.unproved_lines for item in self.items):
            raise UnreadableSourceItems("site inside an unproved owner body")
        return tuple(item.identity for item in self.items if item.first <= line <= item.last)

    def same_owner(self, old_line: int, other: SourceSites, new_line: int) -> bool:
        old = self.owner(old_line)
        new = other.owner(new_line)
        chain = self.owner_chain(old_line)
        if (chain != other.owner_chain(new_line)
                or any(sum(item.identity == identity for item in self.items) != 1
                       or sum(item.identity == identity for item in other.items) != 1 for identity in chain)):
            return False
        # Names and nesting alone do not retain a judgment across a changed signature.
        # Headers come from the same item parser; comments and body edits are not headers.
        if (tuple(item.header for item in self.items if item.first <= old_line <= item.last)
                != tuple(item.header for item in other.items if item.first <= new_line <= item.last)):
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
        before = declarations.source_tokens(subject)
        words = declarations.source_tokens(source)
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

