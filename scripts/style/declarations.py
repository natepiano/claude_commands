"""Read which item a Rust file declares at a line, and every item a file declares.

A declaration is an item's keyword, its name and its owner: the impl or trait whose body holds it directly, else the
module level. Inline module names are not part of it, so an item that a split moves from `mod a { … }` into `a.rs`
keeps its declaration.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
import difflib
from enum import StrEnum
from functools import cached_property, lru_cache
from itertools import accumulate
import re

import style_footprint
from approved_renames import ITEM_KEYWORDS, type_path

# The items whose body is in braces; the other items end at their semicolon.
BRACED_KEYWORDS = frozenset({"fn", "struct", "enum", "union", "trait", "mod", "impl"})


@dataclass(frozen=True)
class ModuleItem:
    """The item sits at a file's top level, in an inline module or in a function body."""


@dataclass(frozen=True)
class ImplOrTraitItem:
    """The item sits directly in an impl body or a trait body. name is the impl's type, or the trait's name.

    For a trait impl, the type is the path after `for`; name is its last segment.
    """

    name: str


Owner = ModuleItem | ImplOrTraitItem


@dataclass(frozen=True)
class Declaration:
    keyword: str
    name: str
    owner: Owner


def declaration_line(declaration: Declaration) -> str:
    """`<keyword> <name>`, or `<keyword> <owner>::<name>` for an item of an impl or trait."""
    owner = f"{declaration.owner.name}::" if isinstance(declaration.owner, ImplOrTraitItem) else ""
    return f"{declaration.keyword} {owner}{declaration.name}"


@dataclass(frozen=True)
class FileLevelImpl:
    """The impl header is outside every brace block."""


@dataclass(frozen=True)
class BlockScopedImpl:
    """The impl header sits at this word inside one or more brace blocks."""

    header_word: int


@dataclass(frozen=True)
class UnprovedImplPosition:
    """The site's words do not prove one import scope."""


ImplPosition = FileLevelImpl | BlockScopedImpl | UnprovedImplPosition


@dataclass(frozen=True)
class ImplementedTrait:
    """The readable path of the trait whose impl directly owns the item."""

    path: tuple[str, ...]
    arguments: tuple[str, ...] = ()
    position: ImplPosition = FileLevelImpl()

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("an implemented trait needs a nonempty path")


@dataclass(frozen=True)
class NoImplementedTrait:
    """The item is not directly owned by a trait implementation."""


@dataclass(frozen=True)
class UnreadableImplementedTrait:
    """A trait impl owns the item, but its trait path cannot be read safely."""


TraitMembership = ImplementedTrait | NoImplementedTrait | UnreadableImplementedTrait


@dataclass(frozen=True)
class ImplementedType:
    """The complete readable type path and arguments from the owning impl header."""

    path: tuple[str, ...]
    arguments: tuple[str, ...] = ()
    position: ImplPosition = FileLevelImpl()


@dataclass(frozen=True)
class NoImplementedType:
    """The item is not directly owned by an impl."""


@dataclass(frozen=True)
class UnreadableImplementedType:
    """The owning impl's type cannot be read as a complete path and arguments."""


TypeMembership = ImplementedType | NoImplementedType | UnreadableImplementedType


@dataclass(frozen=True)
class DeclaredItem:
    """One named item and its lines: its keyword, its first owned attribute (else its keyword) and its end."""

    declaration: Declaration
    keyword_line: int
    first_line: int
    last_line: int
    implemented_trait: TraitMembership = NoImplementedTrait()
    # Members use their owning header's scope; other items use their own scope.
    import_position: ImplPosition = FileLevelImpl()
    implemented_type: TypeMembership = NoImplementedType()


@dataclass(frozen=True)
class NoDeclarationAtLine:
    """No named item holds the line: it is in an impl header, a `use` item, or in no item, or the file ends before it."""


@dataclass(frozen=True)
class _Body:
    """The words of an item's opening and closing brace."""

    open: int
    close: int


@dataclass(frozen=True)
class _NoBody:
    """The item ends at its semicolon, or is not a braced item."""


@dataclass(frozen=True)
class _Span:
    """The words of one item, impl or `use`, and its body."""

    keyword: int
    end: int
    body: _Body | _NoBody


def _span(words: tuple[str, ...], keyword: int) -> _Span:
    """Where the item at keyword ends: its body's closing brace, or the semicolon outside brackets that ends it."""
    braced = words[keyword] in BRACED_KEYWORDS
    cursor = keyword + 1
    generic_depth = 0
    while cursor < len(words):
        word = words[cursor]
        if word in {"(", "["}:
            cursor = style_footprint.closing(words, cursor)
        elif word == "<":
            generic_depth += 1
        elif word == ">" and generic_depth:
            generic_depth -= 1
        elif word == "{":
            end = style_footprint.closing(words, cursor)
            if braced and not generic_depth:
                return _Span(keyword, min(end, len(words) - 1), _Body(cursor, end))
            cursor = end
        elif word == ";":
            return _Span(keyword, cursor, _NoBody())
        cursor += 1
    return _Span(keyword, len(words) - 1, _NoBody())


@dataclass(frozen=True)
class _UnreadableImplOwner:
    """The implementing type is not a readable path; its direct members are left out."""


@dataclass(frozen=True)
class _ImplHeader:
    """The implementing type's name and the trait relationship read from one header."""

    owner: ImplOrTraitItem | _UnreadableImplOwner
    implemented_trait: TraitMembership
    implemented_type: TypeMembership


def _implemented_trait(words: tuple[str, ...]) -> ImplementedTrait | UnreadableImplementedTrait:
    """Read a trait path and its complete argument list without dropping trailing words."""
    path = type_path(words)
    if not path:
        return UnreadableImplementedTrait()
    start = len(path) * 2 - 1
    if start == len(words):
        return ImplementedTrait(path)
    if words[start] != "<" or words[-1] != ">":
        return UnreadableImplementedTrait()
    depth = 0
    brackets: list[str] = []
    for index in range(start, len(words)):
        word = words[index]
        if word in ("(", "[", "{"):
            brackets.append(word)
        elif word in (")", "]", "}"):
            if not brackets or brackets.pop() != {")": "(", "]": "[", "}": "{"}[word]:
                return UnreadableImplementedTrait()
        elif not brackets:
            depth += (word == "<") - (word == ">")
            if depth <= 0 and index != len(words) - 1:
                return UnreadableImplementedTrait()
    if depth or brackets or len(words[start:]) <= 2:
        return UnreadableImplementedTrait()
    return ImplementedTrait(path, words[start:])


def _impl_header(words: tuple[str, ...], span: _Span) -> _ImplHeader:
    """Split at `for` outside generic arguments, excluding the `for` of a higher-ranked bound.

    An unreadable implementing type still leaves out its direct members.
    """
    header = words[span.keyword + 1:span.body.open if isinstance(span.body, _Body) else span.end]
    depth, begin = 0, 0
    if header[:1] == ("<",):
        for index, word in enumerate(header):
            depth += (word == "<") - (word == ">")
            if not depth:
                begin = index + 1
                break
    depth, separator = 0, -1
    for index in range(begin, len(header)):
        depth += (header[index] == "<") - (header[index] == ">")
        if header[index] == "for" and not depth and header[index + 1:index + 2] != ("<",):
            separator = index
            break
    type_words = header[separator + 1:] if separator >= 0 else header[begin:]
    path = type_path(type_words)
    depth = 0
    type_end = len(type_words)
    for index, word in enumerate(type_words):
        depth += (word == "<") - (word == ">")
        if word == "where" and not depth:
            type_end = index
            break
    parsed_type = _implemented_trait(type_words[:type_end])
    implementing_type: TypeMembership = UnreadableImplementedType()
    if isinstance(parsed_type, ImplementedTrait):
        implementing_type = ImplementedType(parsed_type.path, parsed_type.arguments)
    trait_words = header[begin:separator] if separator >= 0 else ()
    trait: TraitMembership = NoImplementedTrait()
    if separator >= 0:
        trait = _implemented_trait(trait_words)
    return _ImplHeader(ImplOrTraitItem(path[-1]) if path else _UnreadableImplOwner(), trait, implementing_type)


class DeclarationSite(StrEnum):
    HEADER = "item header"
    STATEMENT = "body statement"
    UNSUPPORTED_CHILD = "unsupported child declaration"
    UNREADABLE = "unreadable site"


@dataclass(frozen=True)
class DeclaredFile:
    """A Rust file read once: its named items, and what a line needs to read as one of them."""

    # Every named item of the file, in file order. An item of an impl whose type is not a plain path is left out.
    declared_items: tuple[DeclaredItem, ...]
    source_lines: tuple[str, ...]
    # The span of each declared item, in the same order.
    _spans: tuple[_Span, ...]
    # The impls and `use` items: a line they hold most closely reads as no declaration.
    _others: tuple[_Span, ...]
    # The line of each word.
    _word_lines: tuple[int, ...]

    def declaration_at(self, line: int) -> DeclaredItem | NoDeclarationAtLine:
        """The innermost item whose lines hold the line. A line with no token reads as the next line that has one."""
        target = next((number for number in self._word_lines if number >= line), 0)
        if not target:
            return NoDeclarationAtLine()
        holding: list[tuple[int, int, int, DeclaredItem | NoDeclarationAtLine]] = [
            (item.first_line, -item.last_line, span.keyword, item) for span, item in zip(self._spans, self.declared_items, strict=True)
            if item.first_line <= target <= item.last_line]
        holding.extend((self._word_lines[span.keyword], -self._word_lines[span.end], span.keyword, NoDeclarationAtLine())
                       for span in self._others if self._word_lines[span.keyword] <= target <= self._word_lines[span.end])
        if not holding:
            return NoDeclarationAtLine()
        return max(holding, key=lambda entry: entry[:3])[3]

    @cached_property
    def _body_spans(self) -> tuple[style_footprint.TokenSpan, ...]:
        """Immutable source offsets shared by every item body in this file."""
        return style_footprint.token_spans("\n".join(self.source_lines))

    def item_body(self: DeclaredFile, item: DeclaredItem) -> ReadableBody | UnreadableBody:
        """Read only source inside this item's braces, including comments and header-line fragments."""
        index = self.declared_items.index(item)
        body = self._spans[index].body
        if not isinstance(body, _Body):
            return UnreadableBody()
        text = "\n".join(self.source_lines)
        try:
            spans = self._body_spans
        except style_footprint.UnreadableTokenSpans:
            return UnreadableBody()
        if body.close >= len(spans):
            return UnreadableBody()
        source = text[spans[body.open].end:spans[body.close].start]
        return ReadableBody(tuple(line for line in source.splitlines() if line.strip()))

    def site_kind(self, item: DeclaredItem, line: int) -> DeclarationSite:
        """Separate item sites and function statements from unsupported child declarations."""
        if not item.first_line <= line <= item.last_line:
            return DeclarationSite.UNREADABLE
        if line <= item.keyword_line:
            return DeclarationSite.HEADER
        span = self._spans[self.declared_items.index(item)]
        if not isinstance(span.body, _Body) or span.body.close >= len(self._word_lines):
            return DeclarationSite.UNREADABLE
        if line <= self._word_lines[span.body.open]:
            return DeclarationSite.UNSUPPORTED_CHILD
        if line >= self._word_lines[span.body.close]:
            return DeclarationSite.UNREADABLE
        if item.declaration.keyword != "fn":
            return DeclarationSite.UNSUPPORTED_CHILD
        return DeclarationSite.STATEMENT

    def lexical_parent(self: DeclaredFile, item: DeclaredItem) -> DeclaredItem | NoDeclarationAtLine:
        """The nearest named item whose body encloses this item."""
        index = self.declared_items.index(item)
        span = self._spans[index]
        parents = [(other_span.keyword, other) for other, other_span in zip(self.declared_items, self._spans, strict=True)
                   if isinstance(other_span.body, _Body) and other_span.body.open < span.keyword < other_span.body.close]
        return max(parents, key=lambda pair: pair[0])[1] if parents else NoDeclarationAtLine()

    def binding_scope(self, owner: DeclaredItem, binding: LocalBinding) -> tuple[tuple[int, tuple[str, ...]], ...]:
        """Describe enclosing blocks by their sibling order and header words, excluding the function header."""
        span = self._spans[self.declared_items.index(owner)]
        if not isinstance(span.body, _Body):
            return ()
        words = tuple(token.value for token in style_footprint.tokens("\n".join(self.source_lines)))
        stack: list[int] = []
        children: dict[int, int] = {}
        signatures: dict[int, tuple[int, tuple[str, ...]]] = {}
        for index in range(span.body.open, min(span.body.close, len(words))):
            if words[index] == "{":
                if stack:
                    parent = stack[-1]
                    ordinal = children.get(parent, 0)
                    children[parent] = ordinal + 1
                    start = index
                    while start > parent + 1 and words[start - 1] not in (";", "{", "}"):
                        start -= 1
                    signatures[index] = (ordinal, words[start:index])
                stack.append(index)
            elif words[index] == "}" and stack:
                _ = stack.pop()
        blocks = tuple(signatures[index] for index in binding.scope[1:] if index in signatures)
        if isinstance(binding.closure, OwningClosure):
            context = binding.closure.context
            if isinstance(context, ClosureCallArgument):
                origin = ("call", *context.callee, str(context.argument))
            elif isinstance(context, ClosureInitializer):
                origin = ("initializer", context.binding)
            else:
                origin = ("unproved closure",)
            return blocks + ((binding.closure.ordinal, ("closure", *origin, *binding.closure.header)),)
        return blocks

    def local_bindings(self: DeclaredFile, owner: DeclaredItem) -> tuple[LocalBinding, ...]:
        """Read plain let names and closure parameters, without adding them to item identity searches."""
        span = self._spans[self.declared_items.index(owner)]
        if not isinstance(span.body, _Body):
            return ()
        words = tuple(token.value for token in style_footprint.tokens("\n".join(self.source_lines)))
        bindings: list[LocalBinding] = []
        braces: list[int] = []
        closure_end = -1
        closures: dict[tuple[int, ...], int] = {}
        contexts: dict[tuple[tuple[int, ...], ClosureContext], int] = {}
        for index in range(span.body.open, min(span.body.close, len(words))):
            word = words[index]
            if word == "{":
                braces.append(index)
            elif word == "}" and braces:
                _ = braces.pop()
            if word == "let":
                name_index = index + 1 + (words[index + 1:index + 2] == ("mut",))
                if (name_index + 1 < len(words) and style_footprint.IDENTIFIER.fullmatch(words[name_index])
                        and words[name_index + 1] in ("=", ":")):
                    bindings.append(LocalBinding(words[name_index], self._word_lines[name_index], tuple(braces)))
            if word in ("|", "||") and index > closure_end:
                if index and words[index - 1] in ("move", "=", "(", ","):
                    end = (index if word == "||" else next(
                        (cursor for cursor in range(index + 1, min(span.body.close, len(words))) if words[cursor] == "|"), index))
                    scope = tuple(braces)
                    ordinal = closures.get(scope, 0)
                    closures[scope] = ordinal + 1
                    start = index - 1 if words[index - 1] == "move" else index
                    closure = OwningClosure(ordinal, words[start:end + 1], _closure_context(words, braces[-1], start))
                    key = (scope, closure.context)
                    contexts[key] = contexts.get(key, 0) + 1
                    parameters = words[index + 1:end]
                    if parameters and all(part == "," or style_footprint.IDENTIFIER.fullmatch(part) for part in parameters):
                        for cursor in range(index + 1, end):
                            if words[cursor] != ",":
                                bindings.append(LocalBinding(words[cursor], self._word_lines[cursor], scope, closure))
                    closure_end = end
        return tuple(replace(binding, closure=replace(binding.closure, context=UnprovedClosureContext()))
                     if isinstance(binding.closure, OwningClosure) and contexts[(binding.scope, binding.closure.context)] != 1
                     else binding for binding in bindings)


def read_declarations(text: str) -> DeclaredFile:
    """Tokenize the file once and read its named items."""
    tokenized = style_footprint.tokenized_text(text)
    words = tuple(token.value for token in tokenized.tokens)
    lines = tokenized.lines
    starts = style_footprint.item_starts(words)
    owners = style_footprint.attribute_owners(words)
    first_attribute: dict[int, int] = {}
    for start, keyword in sorted(owners.items(), reverse=True):
        first_attribute[keyword] = start
    spans = {index: _span(words, index) for index, word in enumerate(words)
             if index in starts and (word in ITEM_KEYWORDS or word in {"impl", "use"})}
    body_headers = {span.body.open: index for index, span in spans.items() if isinstance(span.body, _Body)}
    # A member must sit directly in its impl's braces, including when other blocks hold nested items.
    enclosing: dict[int, int] = {}
    braces: list[int] = []
    for index, word in enumerate(words):
        if index in spans and braces:
            enclosing[index] = braces[-1]
        if word == "{":
            braces.append(index)
        elif word == "}" and braces:
            _ = braces.pop()
    # The bodies that own their items directly: each impl's type name and each trait's name.
    owning: list[tuple[_Body, ImplOrTraitItem | _UnreadableImplOwner, TraitMembership, TypeMembership]] = []
    for index, span in spans.items():
        if isinstance(span.body, _Body) and words[index] == "impl":
            header = _impl_header(words, span)
            membership = header.implemented_trait
            if isinstance(membership, ImplementedTrait) and index in enclosing:
                membership = replace(membership, position=BlockScopedImpl(index))
            implementing_type = header.implemented_type
            if isinstance(implementing_type, ImplementedType) and index in enclosing:
                implementing_type = replace(implementing_type, position=BlockScopedImpl(index))
            owning.append((span.body, header.owner, membership, implementing_type))
        elif isinstance(span.body, _Body) and words[index] == "trait" and index + 1 < len(words):
            owning.append((span.body, ImplOrTraitItem(words[index + 1]), NoImplementedTrait(), NoImplementedType()))
    item_spans: list[_Span] = []
    items: list[DeclaredItem] = []
    others: list[_Span] = []
    for index, span in spans.items():
        keyword = words[index]
        if keyword in {"impl", "use"}:
            others.append(span)
            continue
        offset = 2 if keyword == "static" and words[index + 1:index + 2] == ("mut",) else 1
        name = words[index + offset] if index + offset < len(words) else ""
        # `const fn` is a function, and `const _` names no item.
        if name in {"", "_", "fn"} or not style_footprint.IDENTIFIER.fullmatch(name):
            continue
        owner: Owner = ModuleItem()
        trait: TraitMembership = NoImplementedTrait()
        implementing_type: TypeMembership = NoImplementedType()
        for body, member_owner, membership, member_type in owning:
            if body.open == enclosing.get(index):
                if isinstance(member_owner, _UnreadableImplOwner):
                    break
                owner = member_owner
                trait = membership
                implementing_type = member_type
        else:
            import_header = body_headers[enclosing[index]] if isinstance(owner, ImplOrTraitItem) else index
            position: ImplPosition = BlockScopedImpl(import_header) if import_header in enclosing else FileLevelImpl()
            item_spans.append(span)
            items.append(DeclaredItem(Declaration(keyword, name, owner), lines[index],
                                      lines[first_attribute.get(index, index)], lines[span.end], trait, position, implementing_type))
    return DeclaredFile(tuple(items), tuple(text.splitlines()), tuple(item_spans), tuple(others), lines)


def moved_site_line(old: DeclaredFile, old_line: int, old_item: DeclaredItem, new: DeclaredFile, new_item: DeclaredItem) -> int:
    """The line of the new item that holds the old site line: a line of its own span, not of an item nested in it,
    whose stripped text is the old line's stripped text, nearest the old line's place below the keyword. Else the
    new item's keyword line.
    """
    site = old.source_lines[old_line - 1].strip() if 0 < old_line <= len(old.source_lines) else ""
    if not site:
        return new_item.keyword_line
    nested = [(item.first_line, item.last_line) for item in new.declared_items
              if item != new_item and new_item.first_line <= item.first_line and item.last_line <= new_item.last_line]
    matches = [number for number in range(new_item.first_line, min(new_item.last_line, len(new.source_lines)) + 1)
               if new.source_lines[number - 1].strip() == site and not any(first <= number <= last for first, last in nested)]
    offset = old_line - old_item.keyword_line
    return min(matches, key=lambda number: (abs(number - new_item.keyword_line - offset), number), default=new_item.keyword_line)


class StatementRefusal(StrEnum):
    TEXT_NOT_FOUND = "text not on a statement line of the moved function"
    TEXT_REPEATED = "text on more than one line of the moved function's body"


def statement_site(new: DeclaredFile, new_item: DeclaredItem, text: str) -> int | StatementRefusal:
    """The one body line of the new function whose code tokens hold the code tokens of the one-line text as one
    unbroken run, when it is a statement of the function itself, else why there is none. A line of an item nested in
    the body counts as a match, but it is not a statement of the function.

    Comments are not tokens, so a comment in the text or on the line does not count, and a string literal is one
    token, so a copy inside a string does not match. A text or file whose tokens cannot be read holds no match.
    """
    site = tuple(style_footprint.tokens(text)) if "\n" not in text.strip() else ()
    if not site or any(token.kind == "unknown" for token in site):
        return StatementRefusal.TEXT_NOT_FOUND
    source = "\n".join(new.source_lines)
    offsets = tuple(accumulate((len(line) + 1 for line in new.source_lines), initial=0))
    by_line: dict[int, list[style_footprint.Token]] = {}
    try:
        spans = style_footprint.token_spans(source)
    except style_footprint.UnreadableTokenSpans:
        return StatementRefusal.TEXT_NOT_FOUND
    for span in spans:
        by_line.setdefault(bisect_right(offsets, span.start), []).append(span.token)
    matches = [number for number in range(new_item.first_line, min(new_item.last_line, len(new.source_lines)) + 1)
               if new.site_kind(new_item, number) == DeclarationSite.STATEMENT
               and any(tuple(line[index:index + len(site)]) == site
                       for line in (by_line.get(number, []),) for index in range(len(line) - len(site) + 1))]
    if len(matches) > 1:
        return StatementRefusal.TEXT_REPEATED
    if not matches or new.declaration_at(matches[0]) != new_item:
        return StatementRefusal.TEXT_NOT_FOUND
    return matches[0]


class MappingFailure(StrEnum):
    DELETED = "source site deleted"
    UNREADABLE = "source mapping unreadable"
    AMBIGUOUS = "several declarations own mapped site"
    OUTSIDE = "mapped site outside destination"


@dataclass(frozen=True)
class MappedDeclaration:
    path: str
    line: int
    declaration: Declaration


@dataclass(frozen=True)
class MappedNoDeclaration:
    path: str
    line: int


@dataclass(frozen=True)
class UnmappedSite:
    reason: MappingFailure


MappedSite = MappedDeclaration | MappedNoDeclaration | UnmappedSite


@dataclass(frozen=True)
class ReadableBody:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class UnreadableBody:
    """The item has no complete braced body."""


@dataclass(frozen=True)
class NoOwningClosure:
    """The binding is not a closure parameter."""


@dataclass(frozen=True)
class ClosureCallArgument:
    """The callee path and argument slot that hold a closure."""

    callee: tuple[str, ...]
    argument: int


@dataclass(frozen=True)
class ClosureInitializer:
    """The named let initializer that holds a closure, including inside a call."""

    binding: str


@dataclass(frozen=True)
class UnprovedClosureContext:
    """The closure has no supported independent statement or call identity."""


ClosureContext = ClosureCallArgument | ClosureInitializer | UnprovedClosureContext


def _closure_context(words: tuple[str, ...], block: int, start: int) -> ClosureContext:
    """Read an enclosing call or named initializer independently of closure order."""
    stack: list[int] = []
    statement = block + 1
    for cursor in range(statement, start):
        word = words[cursor]
        if word in ("(", "[", "{"):
            stack.append(cursor)
        elif word in (")", "]", "}"):
            if not stack or words[stack.pop()] != {")": "(", "]": "[", "}": "{"}[word]:
                return UnprovedClosureContext()
            if word == "}" and not stack and words[statement:statement + 1] != ("let",):
                statement = cursor + 1
        elif word == ";" and not stack:
            statement = cursor + 1
    prefix = words[statement:start]
    if prefix[:1] == ("let",):
        name = 2 if prefix[1:2] == ("mut",) else 1
        if name + 1 >= len(prefix) or not style_footprint.IDENTIFIER.fullmatch(prefix[name]):
            return UnprovedClosureContext()
        assignment = name + 1
        if prefix[assignment] == ":":
            assignment += 1
            type_start = assignment
            brackets: list[str] = []
            while assignment < len(prefix):
                word = prefix[assignment]
                if word == "=" and not brackets:
                    break
                if word in ("(", "[", "{", "<"):
                    brackets.append(word)
                elif word in (")", "]", "}", ">"):
                    if not brackets or brackets.pop() != {")": "(", "]": "[", "}": "{", ">": "<"}[word]:
                        return UnprovedClosureContext()
                assignment += 1
            if brackets or assignment == type_start:
                return UnprovedClosureContext()
        if assignment < len(prefix) and prefix[assignment] == "=":
            return ClosureInitializer(prefix[name])
    if not stack or words[stack[-1]] != "(":
        return UnprovedClosureContext()
    opening = stack[-1]
    callee = opening - 1
    if callee < 0 or not style_footprint.IDENTIFIER.fullmatch(words[callee]):
        return UnprovedClosureContext()
    while (callee >= 2 and words[callee - 1] in ("::", ".")
           and style_footprint.IDENTIFIER.fullmatch(words[callee - 2])):
        callee -= 2
    argument, cursor = 0, opening + 1
    while cursor < start:
        if words[cursor] in ("(", "[", "{"):
            cursor = style_footprint.closing(words, cursor)
        elif words[cursor] == ",":
            argument += 1
        cursor += 1
    if words[start - 1] not in ("(", ","):
        return UnprovedClosureContext()
    return ClosureCallArgument(words[callee:opening], argument)


@dataclass(frozen=True)
class OwningClosure:
    """The parameter's closure, with its block order, header and independent call or binding context."""

    ordinal: int
    header: tuple[str, ...]
    context: ClosureContext


@dataclass(frozen=True)
class LocalBinding:
    name: str
    line: int
    # Brace words from the function body to this binding, for lexical comparisons.
    scope: tuple[int, ...]
    closure: OwningClosure | NoOwningClosure = NoOwningClosure()


@dataclass(frozen=True)
class NoLocalBinding:
    """The site is not one supported, unique binding declaration."""


@dataclass(frozen=True)
class UnreadableLocalBinding:
    """The line holds an unsupported or ambiguous binding pattern."""


@lru_cache(maxsize=1024)
def item_body(file: DeclaredFile, item: DeclaredItem) -> ReadableBody | UnreadableBody:
    return file.item_body(item)


def preserved_body(old: ReadableBody, new: ReadableBody, before: str, after: str) -> int:
    """Count equal lines once, in order, after replacing only the renamed identifier."""
    source = "\n".join(old.lines)
    try:
        spans = style_footprint.token_spans(source)
    except style_footprint.UnreadableTokenSpans:
        return 0
    # A comment-only body needs no replacements; unreadable code supplies no proof.
    if not spans and style_footprint.tokens(source):
        return 0
    fragments: list[str] = []
    cursor = 0
    for span in spans:
        if span.token.kind == "identifier" and span.token.value == before:
            fragments.extend((source[cursor:span.start], after))
            cursor = span.end
    fragments.append(source[cursor:])
    lines = tuple("".join(fragments).splitlines())
    lengths = [0] * (len(new.lines) + 1)
    for line in lines:
        previous = 0
        for index, other in enumerate(new.lines, 1):
            saved = lengths[index]
            lengths[index] = previous + 1 if line == other else max(lengths[index], lengths[index - 1])
            previous = saved
    return lengths[-1]


def source_diff(before: str, after: str) -> str:
    """A deterministic zero-context line diff, independent of any chosen target."""
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), n=0))


def mapped_site(diff: str, path: str, line: int, destination: DeclaredFile) -> MappedSite:
    """Map an old line by hunk offsets before inspecting a destination declaration."""
    if line < 1:
        return UnmappedSite(MappingFailure.OUTSIDE)
    offset = 0
    target = line
    hunks = tuple(re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE))
    if diff.strip() and not hunks:
        return UnmappedSite(MappingFailure.UNREADABLE)
    for match in hunks:
        old_start, old_count = int(match[1]), int(match[2] or "1")
        new_start, new_count = int(match[3]), int(match[4] or "1")
        if old_count and old_start <= line < old_start + old_count:
            if not new_count:
                return UnmappedSite(MappingFailure.DELETED)
            target = new_start + min(line - old_start, new_count - 1)
            break
        if (old_count and old_start + old_count <= line) or (not old_count and old_start < line):
            offset += new_count - old_count
        else:
            target = line + offset
            break
    else:
        target = line + offset
    if not 0 < target <= len(destination.source_lines):
        return UnmappedSite(MappingFailure.OUTSIDE)
    item = destination.declaration_at(target)
    if not isinstance(item, DeclaredItem):
        return MappedNoDeclaration(path, target)
    peers = [other for other in destination.declared_items
             if target in (other.first_line, other.keyword_line, other.last_line) and other != item]
    if peers:
        return UnmappedSite(MappingFailure.AMBIGUOUS)
    return MappedDeclaration(path, target, item.declaration)


def lexical_parent(file: DeclaredFile, item: DeclaredItem) -> DeclaredItem | NoDeclarationAtLine:
    return file.lexical_parent(item)


@lru_cache(maxsize=1024)
def local_bindings(file: DeclaredFile, owner: DeclaredItem) -> tuple[LocalBinding, ...]:
    return file.local_bindings(owner)


def binding_at(file: DeclaredFile, owner: DeclaredItem, line: int) -> LocalBinding | NoLocalBinding | UnreadableLocalBinding:
    bindings = [binding for binding in local_bindings(file, owner) if binding.line == line]
    if len(bindings) == 1:
        binding = bindings[0]
        if isinstance(binding.closure, OwningClosure):
            if isinstance(binding.closure.context, UnprovedClosureContext):
                return UnreadableLocalBinding()
        return binding
    text = file.source_lines[line - 1] if 0 < line <= len(file.source_lines) else ""
    words = tuple(token.value for token in style_footprint.tokens(text))
    if bindings or "let" in words or "|" in words:
        return UnreadableLocalBinding()
    return NoLocalBinding()
