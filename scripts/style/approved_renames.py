"""Read an approved rename finding and decide whether its change is in a file.

Certainty comes from name presence: a file cannot declare, in any form, a name that is in none of its identifiers.
The check is AppliedInCode only when no identifier is the old name and the scan finds the new name declared under the
parent, and RenameNotApplied only when no identifier is the new name and the scan finds the old name declared under the
parent. Every other case, both names present included, is NoAppliedCheck, so the scan never decides between two names.
A file whose macros or attributes can make names that are not its identifiers is NoAppliedCheck too.

A file rename finding moves one module file to a sibling file. Its check reads which of the two files is there: it is
AppliedInCode when only the new file is, RenameNotApplied when only the old file is, and NoAppliedCheck otherwise. It is
NoAppliedCheck too when a folder of child modules is at either name, because a person moves that folder.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

import style_footprint

# The keywords that declare an item with a name that other names can sit under.
PARENT_KEYWORDS = frozenset({"enum", "struct", "union", "trait", "mod"})
# The keywords that declare an item directly in a module, trait or impl body.
ITEM_KEYWORDS = frozenset({"fn", "struct", "enum", "union", "trait", "type", "const", "static", "mod"})
# The words after which an item keyword can stand, besides an attribute, a `pub(..)` and an `extern` ABI string.
ITEM_LEADS = frozenset({"{", "}", ";", "pub", "unsafe", "async", "const", "extern", "default"})
# The macros that make an item name from other tokens or bring in items from another file.
NAME_MACROS = frozenset({"include", "concat_idents", "paste"})
# A relative Rust file path: folders of name characters, then a file whose stem can be a module name.
RUST_FILE = r"(?:[A-Za-z0-9_-]+/)*[A-Za-z_][A-Za-z0-9_]*\.rs"
# A file rename finding's after: each path ends at a space, a mark, the end of a sentence or the end of the text.
FILE_RENAME = re.compile(rf"Rename ({RUST_FILE}) to ({RUST_FILE})(?=$|[\s;,:)]|\.(?:\s|$))")
# Words after a file rename that name a second file pair: another change.
SECOND_FILE_PAIR = re.compile(rf"{RUST_FILE}\s+to\s+{RUST_FILE}", re.IGNORECASE)
# A move, split or merge of code; a word joined to another with a hyphen, as in `finding-merging`, is a name.
CODE_MOVE = re.compile(r"(?<![\w-])(?:mov(?:e|es|ed|ing)|split(?:s|ting)?|merg(?:e|es|ed|ing))(?![\w-])", re.IGNORECASE)
# The text between the start of a clause and a move word: a mark or a joining word ends the clause before it.
CLAUSE_START = re.compile(r"[.;:,()]|\b(?:and|but|then)\b", re.IGNORECASE)
# The words right before a move that negate it: `does not move`, `doesn't move`, `cannot move`, `never moves` or
# `without moving`. Only these exact forms count, so any other text names a move and a person decides.
NEGATED_VERB = re.compile(
    r"(?:\b(?:does|do|did|will|shall|must|should|may|can|could|would)\s+not|n't|\bcannot|\bnever|\bwithout)\s*$",
    re.IGNORECASE)
# A `no` that negates the words after it: `no longer`, `no more` and the like do not.
NO_LEAD = r"no(?!\s+(?:longer|more|less|fewer|later|sooner|doubt|matter)\b)"
# A noun that names code.
CODE_NOUN = r"(?:definitions?|items?|types?|behaviou?r|code|functions?|methods?|impls?|tests?|helpers?|facades?|splits?)"
# A clause that is a subject of `no` before its move: `nothing`, `none`, or `no`, at most three words and a noun that
# names code, as in `no existing definition moves`.
NEGATED_SUBJECT = re.compile(rf"\s*(?:nothing|none|{NO_LEAD}(?:\s+[\w-]+){{0,3}}?\s+{CODE_NOUN})\s*", re.IGNORECASE)
# What follows the move of a negated subject: a place word or the end of the clause, as in `moves into a file`.
SUBJECT_MOVE_END = re.compile(r"\s+(?:into|out|from|to|between|across|within)\b|\s*(?:[.;:,)]|$)", re.IGNORECASE)
# A clause of `no` and at most three words before a move word that is itself the noun, as in `no additional split`.
NEGATED_NOUN = re.compile(rf"\s*{NO_LEAD}(?:\s+[\w-]+){{0,3}}\s*", re.IGNORECASE)
# What follows a move word that is a noun: a form of `be`, as in `no additional split is introduced`.
NOUN_MOVE_END = re.compile(r"\s+(?:is|are|was|were)\b", re.IGNORECASE)
# The file stems that are their folder's module or a crate root, not a module of their own name.
ROOT_STEMS = frozenset({"mod", "lib", "main"})
# The Rust keywords, strict and reserved in any edition: a module with one of these names needs `r#`.
RUST_KEYWORDS = frozenset({
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum", "extern", "false", "fn", "for",
    "gen", "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self",
    "static", "struct", "super", "trait", "true", "try", "type", "unsafe", "use", "where", "while", "abstract", "become",
    "box", "do", "final", "macro", "override", "priv", "typeof", "unsized", "virtual", "yield",
})


@dataclass(frozen=True)
class ApprovedRename:
    """A rename of one item under one parent, and the path the finding names before the parent."""

    qualifier: tuple[str, ...]
    parent: str
    old: str
    new: str


@dataclass(frozen=True)
class NoQualifiedRename:
    """The finding does not rename one item under one parent: a type or restructure finding, or a rename with no such pair."""


@dataclass(frozen=True)
class FileRename:
    """A rename of one module file to a sibling file, as paths from the repository root."""

    old: str
    new: str

    @property
    def old_stem(self) -> str:
        return PurePosixPath(self.old).stem

    @property
    def new_stem(self) -> str:
        return PurePosixPath(self.new).stem

    @property
    def module_folders(self) -> tuple[str, str]:
        """The folders that hold the child modules of the old file and of the new file."""
        return self.old.removesuffix(".rs"), self.new.removesuffix(".rs")


@dataclass(frozen=True)
class NoFileRename:
    """The finding does not rename one module file.

    Its after does not start with `Rename <a> to <b>`, its path does not end with <a>, the two files are not sibling
    module files with different names that are not Rust keywords, or the rest of its after names another change.
    """


@dataclass(frozen=True)
class MissingFile:
    """Nothing is at the path in the code that is read."""


@dataclass(frozen=True)
class Folder:
    """A folder is at the path in the code that is read."""


# Reads one path of the code a check looks at, relative to the repository root: a file's bytes, a folder or nothing.
FileReader = Callable[[str], bytes | Folder | MissingFile]


@dataclass(frozen=True)
class AppliedInCode:
    """No identifier of the file is the old name, and the file declares the new name under its parent.

    For a file rename, the old file is gone and the new file is there.
    """


@dataclass(frozen=True)
class RenameNotApplied:
    """No identifier of the file is the new name, and the file still declares the old name under its parent.

    For a file rename, the old file at path is there and the new file is not.
    """

    path: str


@dataclass(frozen=True)
class NoAppliedCheck:
    """The code cannot show whether the change is in, so a person checks it.

    The finding is not a qualified rename, its file holds both names or neither, a macro of the file can make names,
    or the scan does not find with certainty the one name it holds declared under the parent. For a file rename, both
    files or neither are there, or a folder of child modules is at the old or the new name.
    """

    kind: str


AppliedCheck = AppliedInCode | RenameNotApplied | NoAppliedCheck


@dataclass(frozen=True)
class Declarations:
    """The word indexes that declare one name under its parent: fields, variants and impl, trait or module items."""

    sites: frozenset[int]


@dataclass(frozen=True)
class UncertainScan:
    """The scan does not read with certainty where the file declares the name under its parent.

    More than one item has the parent's name, its declaration carries an attribute other than a derive of std traits,
    the modules around its declaration do not end with the finding's path, or an impl names the parent in other
    modules, inside a block that is not a home module body, by a different path or in a header the scan does not read.
    """


@dataclass(frozen=True)
class InlineModule:
    """The name and the braces of one `mod` item whose body is in the file."""

    name: str
    start: int
    end: int


@dataclass(frozen=True)
class ImplBlock:
    """The braces of one impl that declares names under a parent."""

    start: int
    end: int


def approved_rename(kind: str, before: str, after: str) -> ApprovedRename | NoQualifiedRename:
    """The rename a finding approves, when its before and after are one `<parent>::<name>` pair."""
    old, new = before.split("::"), after.split("::")
    if (kind != "rename" or len(old) < 2 or len(old) != len(new)
            or old[:-1] != new[:-1] or old[-1] == new[-1]
            or not style_footprint.IDENTIFIER.fullmatch(old[-1])
            or not style_footprint.IDENTIFIER.fullmatch(new[-1])):
        return NoQualifiedRename()
    return ApprovedRename(tuple(old[:-2]), old[-2], old[-1], new[-1])


def file_rename(kind: str, path: str, after: str) -> FileRename | NoFileRename:
    """The file rename a finding approves, when its after starts with `Rename <a> to <b>` and its path ends with <a>."""
    match = FILE_RENAME.match(after)
    if kind != "rename" or not match or names_other_change(after[match.end():]):
        return NoFileRename()
    named_old, named_new = PurePosixPath(match[1]), PurePosixPath(match[2])
    stems = {named_old.stem, named_new.stem}
    if ((path != match[1] and not path.endswith("/" + match[1])) or named_old.parent != named_new.parent
            or len(stems) == 1 or stems & ROOT_STEMS or stems & RUST_KEYWORDS):
        return NoFileRename()
    return FileRename(path, path.removesuffix(match[1]) + match[2])


def names_other_change(rest: str) -> bool:
    """Whether the text after a file rename names a second file pair, or a move, split or merge it does not negate."""
    if SECOND_FILE_PAIR.search(rest):
        return True
    for move in CODE_MOVE.finditer(rest):
        clause = rest[max((start.end() for start in CLAUSE_START.finditer(rest, 0, move.start())), default=0):move.start()]
        negated_subject = NEGATED_SUBJECT.fullmatch(clause) and SUBJECT_MOVE_END.match(rest, move.end())
        negated_noun = NEGATED_NOUN.fullmatch(clause) and NOUN_MOVE_END.match(rest, move.end())
        if not (NEGATED_VERB.search(clause) or negated_subject or negated_noun):
            return True
    return False


def file_module(path: str) -> str:
    """The parent name of the file's top level: `crate` for a crate root, else the module the file is."""
    file = PurePosixPath(path)
    if file.name in {"lib.rs", "main.rs"}:
        return "crate"
    return file.parent.name if file.name == "mod.rs" else file.stem


def body_start(words: tuple[str, ...], keyword: int) -> int:
    """The index of the `{` that opens the item at keyword, or the word count when the item has no braced body."""
    start = next((i for i in range(keyword + 2, len(words)) if words[i] in {"{", ";"}), len(words))
    return start if start < len(words) and words[start] == "{" else len(words)


def field_sites(words: tuple[str, ...], start: int, end: int) -> frozenset[int]:
    """The word indexes of the named fields in the struct body from start to end."""
    attributes = dict(style_footprint.attribute_spans(words))
    sites: set[int] = set()
    cursor = start + 1
    while cursor < end:
        while cursor in attributes:
            cursor = attributes[cursor]
        if cursor < end and words[cursor] == "pub":
            cursor += 1
            if cursor < end and words[cursor] == "(":
                cursor = style_footprint.closing(words, cursor) + 1
        if cursor + 1 < end and style_footprint.IDENTIFIER.fullmatch(words[cursor]) and words[cursor + 1] == ":":
            sites.add(cursor)
        while cursor < end and words[cursor] != ",":
            if words[cursor] in {"{", "(", "["}:
                cursor = style_footprint.closing(words, cursor)
            cursor += 1
        cursor += 1
    return frozenset(sites)


def type_path(words: tuple[str, ...]) -> tuple[str, ...]:
    """The segments of the type or trait path that the words of an impl header name, or none when it is not a path."""
    finish = next((i for i, word in enumerate(words) if word in {"<", "where"}), len(words))
    path = words[:finish]
    readable = len(path) % 2 == 1 and all(
        word == "::" if i % 2 else style_footprint.IDENTIFIER.fullmatch(word) is not None for i, word in enumerate(path))
    return path[::2] if readable else ()


def item_starts(words: tuple[str, ...]) -> frozenset[int]:
    """The word indexes where an item keyword can stand: the first word, and the word after a brace, a semicolon, an
    attribute, a visibility, an item qualifier or an ABI string."""
    leads = {index for index, word in enumerate(words) if word in ITEM_LEADS and words[index - 1:index] != ("'",)}
    leads.update(end - 1 for _, end in style_footprint.attribute_spans(words))
    leads.update(style_footprint.closing(words, index + 1) for index, word in enumerate(words[:-1])
                 if word == "pub" and words[index + 1] == "(")
    leads.update(index for index in range(1, len(words)) if words[index - 1] == "extern" and words[index].startswith('"'))
    return frozenset({0} | {lead + 1 for lead in leads})


def inline_modules(words: tuple[str, ...], starts: frozenset[int]) -> tuple[InlineModule, ...]:
    """The `mod` items of the file that have a braced body, outer ones before the ones they hold."""
    modules: list[InlineModule] = []
    for index, word in enumerate(words[:-1]):
        if word == "mod" and index in starts and (start := body_start(words, index)) < len(words):
            modules.append(InlineModule(words[index + 1], start, style_footprint.closing(words, start)))
    return tuple(modules)


def enclosing(modules: tuple[InlineModule, ...], index: int) -> tuple[InlineModule, ...]:
    """The inline modules whose bodies hold the word at index, outermost first."""
    return tuple(module for module in modules if module.start < index < module.end)


def impl_blocks(words: tuple[str, ...], rename: ApprovedRename, trait: bool, starts: frozenset[int],
                modules: tuple[InlineModule, ...], home: tuple[InlineModule, ...]) -> tuple[ImplBlock, ...] | UncertainScan:
    """The braced impls whose names sit under the parent: the impls of the trait for a trait, else the inherent impls.

    The trait and the type of a header part at a `for` outside angle brackets that does not open a higher-ranked bound.
    Home is the inline modules around the parent's declaration; an impl that names the parent in other modules, inside
    a block that is not a home module body, or by a path the finding's path does not end with, gives an uncertain scan.
    """
    blocks: list[ImplBlock] = []
    qualifiers: set[tuple[str, ...]] = set()
    depth = 0
    for index, word in enumerate(words):
        depth += (word == "{") - (word == "}")
        if word != "impl" or index not in starts:
            continue
        start = next((i for i in range(index + 1, len(words)) if words[i] in {"{", ";"}), len(words))
        header = words[index + 1:start]
        if rename.parent not in header:
            continue
        depths: list[int] = []
        for part in header:
            depths.append((depths[-1] if depths else 0) + (part == "<") - (part == ">"))
        if min(depths) < 0 or depths[-1]:
            return UncertainScan()
        begin = depths.index(0) + 1 if header[0] == "<" else 0
        separators = tuple(i for i in range(begin, len(header))
                           if header[i] == "for" and not depths[i] and header[i + 1:i + 2] != ("<",))
        if len(separators) > 1:
            return UncertainScan()
        if bool(separators) != trait:
            continue
        path = type_path(header[begin:separators[0]] if separators else header[begin:])
        if not path:
            return UncertainScan()
        if path[-1] != rename.parent:
            continue
        qualifier = path[:-1]
        if (qualifier and rename.qualifier[-len(qualifier):] != qualifier or enclosing(modules, index) != home
                or depth != len(home)):
            return UncertainScan()
        qualifiers.add(qualifier)
        if start < len(words) and words[start] == "{":
            blocks.append(ImplBlock(start, style_footprint.closing(words, start)))
    return UncertainScan() if len(qualifiers) > 1 else tuple(blocks)


def std_derives_only(attributes: tuple[str, ...]) -> bool:
    """Whether every attribute in the words derives only std traits, so none of them adds a name."""
    return all(finish - start >= 6 and attributes[start + 2:start + 4] == ("derive", "(")
               and all(word in style_footprint.STD_DERIVES or word == "," for word in attributes[start + 4:finish - 2])
               for start, finish in style_footprint.attribute_spans(attributes))


def declaration_sites(words: tuple[str, ...], path: str, rename: ApprovedRename, name: str) -> Declarations | UncertainScan:
    """Where the file at path declares name directly under the rename's parent."""
    parent = rename.parent
    starts = item_starts(words)
    modules = inline_modules(words, starts)
    declared = tuple(index for index, word in enumerate(words[:-1])
                     if word in PARENT_KEYWORDS and index in starts and words[index + 1] == parent
                     and (word != "mod" or body_start(words, index) < len(words)))
    top_level = file_module(path) == parent
    attributes = style_footprint.decorated_items(words)
    if (len(declared) + int(top_level) > 1
            or not all(std_derives_only(attributes.get(index, ())) for index in declared)):
        return UncertainScan()
    home = enclosing(modules, declared[0]) if declared else ()
    home_path = tuple(module.name for module in home)
    if (declared or top_level) and rename.qualifier and home_path[-len(rename.qualifier):] != rename.qualifier:
        return UncertainScan()
    impls = impl_blocks(words, rename, any(words[index] == "trait" for index in declared), starts, modules, home)
    if isinstance(impls, UncertainScan):
        return UncertainScan()
    sites = {index for body in style_footprint.enum_bodies(words) if body.name == parent
             for index in body.variants if words[index] == name}
    bodies = [(block.start, block.end) for block in impls]
    if top_level:
        bodies.append((-1, len(words)))
    for index in declared:
        start = body_start(words, index)
        if start == len(words):
            continue
        if words[index] in {"struct", "union"}:
            sites.update(site for site in field_sites(words, start, style_footprint.closing(words, start))
                         if words[site] == name)
        elif words[index] in {"mod", "trait"}:
            bodies.append((start, style_footprint.closing(words, start)))
    for start, end in bodies:
        cursor = start + 1
        while cursor < end:
            if words[cursor] in ITEM_KEYWORDS and cursor in starts and cursor + 1 < end and words[cursor + 1] == name:
                sites.add(cursor + 1)
            if words[cursor] in {"{", "(", "["}:
                cursor = style_footprint.closing(words, cursor)
            cursor += 1
    return Declarations(frozenset(sites))


def applied_check(kind: str, before: str, after: str, path: str, read: FileReader) -> AppliedCheck:
    """Whether a finding's change is in the code that read reads."""
    moved = file_rename(kind, path, after)
    if isinstance(moved, FileRename):
        old, new = read(moved.old), read(moved.new)
        if (isinstance(old, bytes) == isinstance(new, bytes)
                or not all(isinstance(read(folder), MissingFile) for folder in moved.module_folders)):
            return NoAppliedCheck(kind)
        return AppliedInCode() if isinstance(new, bytes) else RenameNotApplied(moved.old)
    rename = approved_rename(kind, before, after)
    source = read(path)
    # A file that is not there declares neither name.
    if isinstance(rename, NoQualifiedRename) or not isinstance(source, bytes):
        return NoAppliedCheck(kind)
    source_tokens = style_footprint.tokens(source.decode(errors="replace"))
    names = {token.value.removeprefix("r#") for token in source_tokens if token.kind == "identifier"}
    words = tuple(token.value for token in source_tokens)
    if ((rename.old in names) == (rename.new in names) or names & NAME_MACROS
            or any(pair == ("[", "<") for pair in zip(words, words[1:]))):
        return NoAppliedCheck(kind)
    # The file cannot declare a name that is in none of its identifiers, so the name it holds settles the check.
    held = rename.old if rename.old in names else rename.new
    found = declaration_sites(words, path, rename, held)
    if isinstance(found, UncertainScan) or not found.sites:
        return NoAppliedCheck(kind)
    return RenameNotApplied(path) if held == rename.old else AppliedInCode()
