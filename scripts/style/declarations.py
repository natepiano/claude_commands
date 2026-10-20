"""Read which item a Rust file declares at a line, and every item a file declares.

A declaration is an item's keyword, its name and its owner: the impl or trait whose body holds it directly, else the
module level. Inline module names are not part of it, so an item that a split moves from `mod a { … }` into `a.rs`
keeps its declaration.
"""

from __future__ import annotations

from dataclasses import dataclass

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
class DeclaredItem:
    """One named item and its lines: its keyword, its first owned attribute (else its keyword) and its end."""

    declaration: Declaration
    keyword_line: int
    first_line: int
    last_line: int


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


def _impl_name(words: tuple[str, ...], span: _Span) -> str:
    """The last segment of the impl's type: the path after a `for` outside angle brackets, else the whole header.

    It is '' when the type is not a plain path.
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
    return path[-1] if path else ""


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
    # The bodies that own their items directly: each impl's type name and each trait's name.
    owning: list[tuple[_Body, str]] = []
    for index, span in spans.items():
        if isinstance(span.body, _Body) and words[index] == "impl":
            owning.append((span.body, _impl_name(words, span)))
        elif isinstance(span.body, _Body) and words[index] == "trait" and index + 1 < len(words):
            owning.append((span.body, words[index + 1]))
    item_bodies = [span.body for index, span in spans.items() if isinstance(span.body, _Body) and words[index] in ITEM_KEYWORDS]
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
        closest = max((body for body in (*(body for body, _ in owning), *item_bodies) if body.open < index < body.close),
                      key=lambda body: body.open, default=_NoBody())
        owner: Owner = ModuleItem()
        for body, owner_name in owning:
            if body == closest:
                if not owner_name:
                    break
                owner = ImplOrTraitItem(owner_name)
        else:
            item_spans.append(span)
            items.append(DeclaredItem(Declaration(keyword, name, owner), lines[index],
                                      lines[first_attribute.get(index, index)], lines[span.end]))
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
