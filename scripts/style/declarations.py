"""Read which item a Rust file declares at a line, and every item a file declares.

A declaration is an item's keyword, its name and its owner: the impl or trait whose body holds it directly, else the
module level. Inline module names are not part of it, so an item that a split moves from `mod a { … }` into `a.rs`
keeps its declaration.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import style_footprint
from approved_renames import ITEM_KEYWORDS, item_starts, type_path

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
class DeclaredItem:
    """One named item and its lines: its keyword, its first owned attribute (else its keyword) and its end."""

    declaration: Declaration
    keyword_line: int
    first_line: int
    last_line: int
    implemented_trait: TraitMembership = NoImplementedTrait()
    # Members use their owning header's scope; other items use their own scope.
    import_position: ImplPosition = FileLevelImpl()


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
    while cursor < len(words):
        word = words[cursor]
        if word in {"(", "["}:
            cursor = style_footprint.closing(words, cursor)
        elif word == "{":
            end = style_footprint.closing(words, cursor)
            if braced:
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
    path = type_path(header[separator + 1:] if separator >= 0 else header[begin:])
    trait_words = header[begin:separator] if separator >= 0 else ()
    trait: TraitMembership = NoImplementedTrait()
    if separator >= 0:
        trait = _implemented_trait(trait_words)
    return _ImplHeader(ImplOrTraitItem(path[-1]) if path else _UnreadableImplOwner(), trait)


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


def read_declarations(text: str) -> DeclaredFile:
    """Tokenize the file once and read its named items."""
    tokenized = style_footprint.tokenized_text(text)
    words = tuple(token.value for token in tokenized.tokens)
    lines = tokenized.lines
    starts = item_starts(words)
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
    owning: list[tuple[_Body, ImplOrTraitItem | _UnreadableImplOwner, TraitMembership]] = []
    for index, span in spans.items():
        if isinstance(span.body, _Body) and words[index] == "impl":
            header = _impl_header(words, span)
            membership = header.implemented_trait
            if isinstance(membership, ImplementedTrait) and index in enclosing:
                membership = replace(membership, position=BlockScopedImpl(index))
            owning.append((span.body, header.owner, membership))
        elif isinstance(span.body, _Body) and words[index] == "trait" and index + 1 < len(words):
            owning.append((span.body, ImplOrTraitItem(words[index + 1]), NoImplementedTrait()))
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
        for body, member_owner, membership in owning:
            if body.open == enclosing.get(index):
                if isinstance(member_owner, _UnreadableImplOwner):
                    break
                owner = member_owner
                trait = membership
        else:
            import_header = body_headers[enclosing[index]] if isinstance(owner, ImplOrTraitItem) else index
            position: ImplPosition = BlockScopedImpl(import_header) if import_header in enclosing else FileLevelImpl()
            item_spans.append(span)
            items.append(DeclaredItem(Declaration(keyword, name, owner), lines[index],
                                      lines[first_attribute.get(index, index)], lines[span.end], trait, position))
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
