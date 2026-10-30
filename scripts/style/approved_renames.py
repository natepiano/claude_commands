"""Read an approved rename finding and decide whether its change is in a file.

Certainty comes from name presence: a file cannot declare, in any form, a name that is in none of its identifiers.
The check is AppliedInCode only when no identifier is the old name and the scan finds the new name declared under the
parent, and RenameNotApplied only when no identifier is the new name and the scan finds the old name declared under the
parent. Every other case, both names present included, is NoAppliedCheck, so the scan never decides between two names.
A file whose macros or attributes can make names that are not its identifiers is NoAppliedCheck too.

Local binding proofs compare original and edited token streams. They locate each declaration, exclude shadowed
scopes, and account for each changed use, including struct shorthand and format captures.

A file rename finding moves one module file to a sibling file. Its check reads which of the two files is there: it is
AppliedInCode when only the new file is, RenameNotApplied when only the old file is, and NoAppliedCheck otherwise. It is
NoAppliedCheck too when a folder of child modules is at either name, because a person moves that folder.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

import style_footprint

# The keywords that declare an item with a name that other names can sit under.
PARENT_KEYWORDS = frozenset({"enum", "struct", "union", "trait", "mod"})
# The keywords that declare an item directly in a module, trait or impl body.
ITEM_KEYWORDS = frozenset({"fn", "struct", "enum", "union", "trait", "type", "const", "static", "mod"})
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
# The end of a sentence: `.`, `;` or the end of the text. A comma or a colon can start an exception, so it is not an end.
CLAUSE_END = r"\s*(?:[.;]|$)"
# What follows the move of a negated subject: a place word or the end of the clause, as in `moves into a file`.
SUBJECT_MOVE_END = re.compile(rf"\s+(?:into|out|from|to|between|across|within)\b|{CLAUSE_END}", re.IGNORECASE)
# An object of `no` right after a move, then the end of the clause, as in `move no skipped definitions.`; a place word
# after the object can start an exception, as in `move no helpers into a file except LockError`.
NEGATED_OBJECT = re.compile(rf"\s+{NO_LEAD}(?:\s+[\w-]+){{0,3}}?\s+{CODE_NOUN}{CLAUSE_END}", re.IGNORECASE)
# A clause of `no` and at most three words before a move word that is itself the noun, as in `no additional split`.
NEGATED_NOUN = re.compile(rf"\s*{NO_LEAD}(?:\s+[\w-]+){{0,3}}\s*", re.IGNORECASE)
# What follows a move word that is a noun: a form of `be`, as in `no additional split is introduced`.
NOUN_MOVE_END = re.compile(r"\s+(?:is|are|was|were)\b", re.IGNORECASE)
# The file stems that are their folder's module or a crate root, not a module of their own name.
ROOT_STEMS = frozenset({"mod", "lib", "main"})


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
    For a binding rename, its declaration and every visible use have the approved name without a collision.
    """


@dataclass(frozen=True)
class RenameNotApplied:
    """No identifier of the file is the new name, and the file still declares the old name under its parent.

    For a file rename, the old file at path is there and the new file is not.
    For a binding rename, no approved edit is in its located scope.
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
class BindingRename:
    """One approved local name pair, located by the finding's original line and text."""

    line: int
    text: str
    old: str
    new: str


@dataclass(frozen=True)
class NoBindingRename:
    """The finding does not specify exactly one unqualified identifier pair."""


@dataclass(frozen=True)
class PatternSite:
    """A binding pattern's token interval and the intervals where its bindings are visible."""

    kind: str
    start: int
    end: int
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class BindingScope:
    """A located declaration and its visible token intervals, excluding shadowing declarations and their uses."""

    kind: str
    declaration: int
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class NoBindingScope:
    """Why a finding cannot be located as exactly one local binding."""

    reason: str


@dataclass(frozen=True)
class BindingProof:
    """Per-binding results, refusal reasons, whole-edit coverage and original-to-edited token line locations."""

    checks: Mapping[BindingRename, AppliedCheck]
    refusals: Mapping[BindingRename, str]
    explained: bool
    line_map: Mapping[int, int]


@dataclass(frozen=True)
class CommentRename:
    """One exact identifier mention in a comment, at source character offsets."""

    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class BindingComments:
    """Mechanical comment edits and the enclosing parameter function, when present."""

    edits: tuple[CommentRename, ...]
    indexes: frozenset[int]
    functions: tuple[str, ...]


@dataclass(frozen=True)
class _SourceComment:
    start: int
    end: int
    gap: int
    text: str


def identifier_mentions(text: str, name: str) -> tuple[tuple[int, int], ...]:
    """Case-exact whole identifiers, excluding components of paths, filenames and kebab words."""
    return tuple(match.span() for match in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text)
                 if text[max(0, match.start() - 1):match.start()] not in {"/", "\\", "-"}
                 if not re.search(r"(?:[A-Za-z0-9_][-./\\]|::)$", text[max(0, match.start() - 2):match.start()])
                 and not re.match(r"(?:[-./\\][A-Za-z0-9_]|::)", text[match.end():]))


def backtick_mentions(text: str, name: str) -> tuple[tuple[int, int], ...]:
    """Whole identifier mentions inside Markdown code spans, including intra-doc code links."""
    code = tuple(match.span(2) for match in re.finditer(r"(`+)(.*?)\1", text, re.DOTALL))
    return tuple((start, end) for start, end in identifier_mentions(text, name)
                 if any(left <= start and end <= right for left, right in code))


def _source_comments(source: str, spans: tuple[style_footprint.TokenSpan, ...]) -> tuple[_SourceComment, ...]:
    """Read only token gaps: strings and character literals can never become comments."""
    result: list[_SourceComment] = []
    for gap, (begin, finish) in enumerate(zip((0, *(span.end for span in spans)),
                                            (*(span.start for span in spans), len(source)), strict=True)):
        cursor = begin
        while cursor < finish:
            start = cursor
            if source.startswith("//", cursor):
                newline = source.find("\n", cursor, finish)
                cursor = finish if newline < 0 else newline
            elif source.startswith("/*", cursor):
                cursor += 2
                depth = 1
                while cursor < finish and depth:
                    if source.startswith("/*", cursor):
                        depth += 1
                        cursor += 2
                    elif source.startswith("*/", cursor):
                        depth -= 1
                        cursor += 2
                    else:
                        cursor += 1
            else:
                cursor += 1
                continue
            result.append(_SourceComment(start, cursor, gap, source[start:cursor]))
    return tuple(result)


def binding_rename(kind: str, before: str, after: str, line: int, text: str) -> BindingRename | NoBindingRename:
    """Read a bare name pair or a token-preserving snippet with exactly one distinct identifier replacement."""
    old, new = style_footprint.tokens(before), style_footprint.tokens(after)
    if kind != "rename" or len(old) != len(new) or any(token.value == "::" for token in (*old, *new)):
        return NoBindingRename()
    pairs = {(a.value, b.value) for a, b in zip(old, new) if a != b}
    if len(pairs) != 1:
        return NoBindingRename()
    previous, replacement = pairs.pop()
    if not all(style_footprint.IDENTIFIER.fullmatch(name) and not name.startswith("r#")
               and name not in style_footprint.RUST_KEYWORDS for name in (previous, replacement)):
        return NoBindingRename()
    return BindingRename(line, text, previous, replacement)



@dataclass(frozen=True)
class RenameDeclaration:
    """The finding's declaration token, with lexical ranges for comment and capture guards."""

    offset: int
    scope: BindingScope


def refactor_rename(kind: str, before: str, after: str, line: int, text: str) -> BindingRename | NoBindingRename:
    """One identifier replacement, including qualified fields and raw identifiers; no type/restructure edits."""
    old, new = style_footprint.tokens(before), style_footprint.tokens(after)
    if kind != "rename" or len(old) != len(new):
        return NoBindingRename()
    pairs = {(a.value, b.value) for a, b in zip(old, new) if a != b}
    if len(pairs) != 1:
        return NoBindingRename()
    previous, replacement = pairs.pop()
    if not all(style_footprint.IDENTIFIER.fullmatch(name) and name not in style_footprint.RUST_KEYWORDS
               for name in (previous, replacement)):
        return NoBindingRename()
    return BindingRename(line, text, previous, replacement)


def rename_declaration(source: str, rename: BindingRename) -> RenameDeclaration | NoBindingScope:
    """Locate syntax only. Rust-analyzer owns reference resolution and the rename edit."""
    scan = _BindingScan(source)
    lines = [i for i, text in enumerate(source.splitlines(), 1) if text.strip() == rename.text.strip()]
    if not lines:
        return NoBindingScope("finding text is absent")
    distance = min(abs(line - rename.line) for line in lines)
    nearest = [line for line in lines if abs(line - rename.line) == distance]
    if len(nearest) != 1:
        return NoBindingScope("finding location is tied")
    indexes = {i for i, span in enumerate(scan.spans) if scan.tokenized.lines[i] == nearest[0]
               and span.token.value.removeprefix("r#") == rename.old.removeprefix("r#")}
    candidates: dict[int, BindingScope] = {}
    for site in scan.sites:
        for i in indexes:
            if scan.binding_word(site, i, rename.old, raw=True):
                candidates[i] = BindingScope(site.kind, i, site.ranges)
    for start, word in enumerate(scan.words):
        if word not in {"struct", "enum", "union"}:
            continue
        body = next((i for i in range(start + 2, len(scan.words)) if scan.words[i] in {"{", "(", ";"}), -1)
        if body not in scan.ends or scan.words[body] != "{":
            continue
        for i in indexes:
            if body < i < scan.ends[body] and (scan.words[i + 1:i + 2] == (":",)
                    or word == "enum" and scan.parents.get(i) == body and scan.words[i - 1] in {"{", ","}):
                candidates[i] = BindingScope("field" if scan.words[i + 1:i + 2] == (":",) else "variant",
                                             i, ((start, scan.ends[body] + 1),))
    for i in indexes:
        if i and scan.words[i - 1] in {"fn", "struct", "enum", "union", "trait", "type", "const", "static", "mod"}:
            candidates[i] = BindingScope("item", i, ((max(0, i - 1), len(scan.words)),))
    if len(candidates) != 1:
        return NoBindingScope("finding line has no unique declaration of " + repr(rename.old))
    index, scope = next(iter(candidates.items()))
    # Reuse precise shadowing ranges where available, without any file-wide refusal.
    local = scan.locate(rename)
    if isinstance(local, BindingScope) and local.declaration == index:
        scope = local
    return RenameDeclaration(scan.spans[index].start, scope)


@dataclass(frozen=True)
class DistinctPatternBindings:
    """The proposed name duplicates no binding in this pattern or parameter list."""


@dataclass(frozen=True)
class PatternBindingCollision:
    name: str
    line: int

    @property
    def reason(self) -> str:
        return f"new name {self.name!r} is already a binding in the same pattern or parameter list at line {self.line}"


def pattern_binding_check(source: str, declaration: RenameDeclaration,
                          rename: BindingRename) -> DistinctPatternBindings | PatternBindingCollision:
    scan = _BindingScan(source)
    scope = declaration.scope
    owners = [site for site in scan.sites if site.start <= scope.declaration < site.end
              and scan.binding_word(site, scope.declaration, rename.old, raw=True)]
    siblings = [site for site in scan.sites if site in owners or any(
        site.kind == owner.kind and owner.kind in {"fn parameter", "closure"}
        and site.ranges[-1] == owner.ranges[-1] for owner in owners)]
    for site in siblings:
        for i in range(site.start, site.end):
            if scan.binding_word(site, i, rename.new, raw=True):
                return PatternBindingCollision(rename.new, scan.tokenized.lines[i])
    return DistinctPatternBindings()


def refactor_comments(source: str, declaration: RenameDeclaration, rename: BindingRename) -> BindingComments:
    """Mechanical identifier mentions in the declaration's comments, including prose."""
    scan = _BindingScan(source)
    comments = scan.binding_comments(declaration.scope, rename)
    if isinstance(comments, BindingComments):
        return comments
    indexes = {i for i, comment in enumerate(scan.comments)
               if any(start <= comment.gap <= end for start, end in declaration.scope.ranges)}
    if declaration.scope.kind in {"fn parameter", "field", "variant", "item"}:
        start = min(a for a, _ in declaration.scope.ranges)
        indexes.update(i for i, comment in enumerate(scan.comments)
                       if style_footprint.outer_doc(comment.text) and start - 3 <= comment.gap <= declaration.scope.declaration)
    edits = tuple(CommentRename(comment.start + a, comment.start + b, rename.new)
                  for i, comment in enumerate(scan.comments) if i in indexes
                  for a, b in identifier_mentions(comment.text, rename.old))
    return BindingComments(edits, frozenset(indexes), ())


def _contains(ranges: tuple[tuple[int, int], ...], index: int) -> bool:
    return any(start <= index < end for start, end in ranges)


class _BindingScan:
    """The delimiter and pattern facts shared by all rename checks on one original file."""

    def __init__(self, source: str) -> None:
        self.source: str = source
        self.tokenized: style_footprint.TokenizedText = style_footprint.tokenized_text(source)
        self.words: tuple[str, ...] = tuple(token.value for token in self.tokenized.tokens)
        self.plain_words: tuple[str, ...] = tuple(word.removeprefix("r#") for word in self.words)
        self.plain_word_set: frozenset[str] = frozenset(self.plain_words)
        self.name_indexes: dict[str, list[int]] = {}
        for index, token in enumerate(self.tokenized.tokens):
            if token.kind == "identifier":
                self.name_indexes.setdefault(token.value, []).append(index)
        self.ends: dict[int, int] = style_footprint.delimiter_ends(self.words)
        self.parents: dict[int, int] = {}
        stack: list[int] = []
        for index in range(len(self.words)):
            if stack and self.ends[stack[-1]] == index:
                _ = stack.pop()
            if stack:
                self.parents[index] = stack[-1]
            if index in self.ends:
                stack.append(index)
        self.macros: tuple[tuple[int, int], ...] = style_footprint.macro_spans(self.words, self.ends)
        self.macro_origins_uncertain: bool = self._uncertain_macro_origins()
        self.sites: tuple[PatternSite, ...] = self._pattern_sites()
        self.formats: frozenset[int] = self._format_arguments()
        self.spans: tuple[style_footprint.TokenSpan, ...] = style_footprint.token_spans(source)
        self.comments: tuple[_SourceComment, ...] = _source_comments(source, self.spans)

    def level(self, start: int, end: int, angles: bool = False) -> tuple[int, ...]:
        """Token indexes at one delimiter level; parameter lists also skip generic arguments."""
        result: list[int] = []
        cursor, depth = start, 0
        while cursor < end:
            word = self.words[cursor]
            if angles:
                depth = max(0, depth + (word == "<") - (word == ">") - 2 * (word == ">>"))
            if depth == 0:
                result.append(cursor)
            cursor = self.ends.get(cursor, cursor) + 1
        return tuple(result)

    def stop(self, start: int, end: int, marks: frozenset[str]) -> int:
        return next((index for index in self.level(start, end) if self.words[index] in marks), end)

    def block_end(self, index: int) -> int:
        parent = self.parents.get(index, -1)
        while parent >= 0 and self.words[parent] != "{":
            parent = self.parents.get(parent, -1)
        return self.ends.get(parent, len(self.words))

    def _parameters(self, start: int, end: int, kind: str, body: tuple[int, int]) -> list[PatternSite]:
        sites: list[PatternSite] = []
        begin = start
        for finish in (*[i for i in self.level(start, end, True) if self.words[i] == ","], end):
            colon = next((i for i in self.level(begin, finish) if self.words[i] == ":"), finish)
            if begin < colon and "self" not in self.words[begin:colon]:
                sites.append(PatternSite(kind, begin, colon, ((begin, colon), body)))
            begin = finish + 1
        return sites

    def _pattern_sites(self) -> tuple[PatternSite, ...]:
        sites: list[PatternSite] = []
        words, size = self.words, len(self.words)
        closure_ends: set[int] = set()
        for index, word in enumerate(words):
            if _contains(self.macros, index):
                continue
            lead = words[index - 1] if index else ""
            end = self.block_end(index)
            if word == "let" and lead in {"{", "}", ";", "]", "if", "while", "&&"}:
                finish = self.stop(index + 1, end, frozenset({"=", ";", ":", "else"}))
                if finish == end:
                    continue
                pattern_end = finish
                if words[finish] == ":":
                    finish = self.stop(finish + 1, end, frozenset({"=", ";"}))
                if lead in {"if", "while", "&&"}:
                    body = self.stop(finish + 1, end, frozenset({"&&", "{", "=>", ";"}))
                    # An unparenthesized block expression can be mistaken for the condition's body.
                    if body == finish + 1 or any(words[i] in {"if", "match", "loop", "while", "for", "async", "unsafe", "|"}
                                                 for i in self.level(finish + 1, body)):
                        continue
                    if body < end and words[body] in {"&&", "{"}:
                        block = self.stop(body, end, frozenset({"{", "=>", ";"}))
                        if block in self.ends and words[block] == "{":
                            sites.append(PatternSite("condition let", index + 1, pattern_end,
                                                     ((index + 1, pattern_end), (body, self.ends[block] + 1))))
                else:
                    semi = self.stop(finish, end, frozenset({";"}))
                    if semi < end:
                        sites.append(PatternSite("let", index + 1, pattern_end,
                                                 ((index + 1, pattern_end), (semi + 1, end))))
            elif word == "fn" and index + 2 < size and style_footprint.IDENTIFIER.fullmatch(words[index + 1]):
                paren = next((i for i in self.level(index + 2, end, True) if words[i] in {"(", "{", ";"}), end)
                if paren not in self.ends or words[paren] != "(":
                    continue
                close = self.ends[paren]
                body = self.stop(close + 1, end, frozenset({"{", ";"}))
                if body in self.ends and words[body] == "{":
                    sites.extend(self._parameters(paren + 1, close, "fn parameter", (body, self.ends[body] + 1)))
            elif word == "|" and index not in closure_ends and lead in {
                    "(", "[", "{", ",", ";", "=", "=>", "move", "return", "&&", "||", "!", ":", "async", "else"}:
                close = self.stop(index + 1, end, frozenset({"|", ";", "=>"}))
                if close == end or words[close] != "|":
                    continue
                closure_ends.add(close)
                body = close + 1
                if words[body:body + 1] == ("->",):
                    body = self.stop(body + 1, end, frozenset({"{", ";", "=>"}))
                if body >= end:
                    continue
                end = min(end, self.ends.get(self.parents.get(index, -1), end))
                finish = (self.ends[body] + 1 if words[body] == "{" and body in self.ends
                          else self.stop(body, end, frozenset({",", ";", "=>"})))
                if finish < size and words[finish] == "=>":
                    continue
                sites.extend(self._parameters(index + 1, close, "closure", (body, finish)))
            elif word == "for" and lead in {
                    "{", "}", ";", "=", "=>", "(", ",", ":", "else", "]", "return", "break"}:
                if words[index + 1:index + 2] == ("<",):
                    continue
                finish = self.stop(index + 1, end, frozenset({"in", ";", "{"}))
                if finish == end or words[finish] != "in":
                    continue
                body = self.stop(finish + 1, end, frozenset({"{", ";", "=>"}))
                if body in self.ends and words[body] == "{":
                    sites.append(PatternSite("for", index + 1, finish,
                                             ((index + 1, finish), (body, self.ends[body] + 1))))
            elif word == "match":
                body = self.stop(index + 1, end, frozenset({"{", ";"}))
                if body not in self.ends or words[body] != "{":
                    continue
                finish, begin = self.ends[body], body + 1
                while begin < finish:
                    arrow = self.stop(begin, finish, frozenset({"=>", ","}))
                    if arrow == finish or words[arrow] != "=>":
                        break
                    guard = self.stop(begin, arrow, frozenset({"if"}))
                    arm = arrow + 1
                    arm_end = (self.ends[arm] + 1 if arm in self.ends and words[arm] == "{"
                               else self.stop(arm, finish, frozenset({","})))
                    sites.append(PatternSite("match arm", begin, guard, ((begin, arm_end),)))
                    begin = arm_end + int(words[arm_end:arm_end + 1] == (",",))
        return tuple(sites)

    def binding_word(self, site: PatternSite, index: int, name: str, *, raw: bool = False) -> bool:
        words = self.words
        actual = words[index].removeprefix("r#") if raw else words[index]
        expected = name.removeprefix("r#") if raw else name
        if (not site.start <= index < site.end or actual != expected or name in style_footprint.RUST_KEYWORDS
                or not raw and name.startswith("r#") or not style_footprint.IDENTIFIER.fullmatch(name)):
            return False
        previous = words[index - 1] if index else ""
        following = words[index + 1] if index + 1 < len(words) else ""
        if previous in {".", "::", "'"} or following in {"::", "(", "{", "!"}:
            return False
        return following != ":" or self.parents.get(index) == self.parents.get(site.start)

    def locate(self, rename: BindingRename) -> BindingScope | NoBindingScope:
        lines = [i for i, text in enumerate(self.source.splitlines(), 1) if text.strip() == rename.text.strip()]
        if not lines:
            return NoBindingScope("finding text is absent")
        distance = min(abs(line - rename.line) for line in lines)
        nearest = [line for line in lines if abs(line - rename.line) == distance]
        if len(nearest) != 1:
            return NoBindingScope("finding location is tied")
        candidates = [(index, site) for site in self.sites for index in range(site.start, site.end)
                      if self.tokenized.lines[index] == nearest[0] and self.binding_word(site, index, rename.old)]
        if len({index for index, _ in candidates}) != 1:
            return NoBindingScope("finding line does not hold exactly one binding")
        declaration, site = min(candidates, key=lambda found: found[1].end - found[1].start)
        if site.kind in {"condition let", "let"}:
            end = self.block_end(site.start)
            scrutinee = self.stop(site.end, end, frozenset({"="})) + 1
            if site.kind == "condition let":
                # The existing generic-aware delimiter walk is only a refusal boundary:
                # any brace it skips disqualifies the scrutinee, including const arguments.
                finish = next((i for i in self.level(scrutinee, end, True)
                               if self.words[i] in {"&&", "{", ";", "=>"}), end)
            else:
                finish = self.stop(scrutinee, end, frozenset({"else", ";"}))
                if self.words[finish:finish + 1] != ("else",):
                    finish = scrutinee
            if "{" in self.words[scrutinee:finish]:
                return NoBindingScope("condition let scrutinee contains a block")
        if site.kind == "for":
            iterator = site.end + 1
            body, finish = site.ranges[-1]
            following = self.words[finish:finish + 1]
            if (body == iterator or "{" in self.words[iterator:body]
                    or set(self.words[iterator:body]) & {"if", "match", "loop", "while", "for", "async", "unsafe"}
                    or following and following[0] in {"else", "{", ".", "?", "(", "[", "+", "-", "*", "/", "|", "&"}):
                return NoBindingScope("for iterator contains a block before its body")
        hidden = [(begin, finish) for other in self.sites if other != site
                  and _contains(site.ranges, other.start)
                  and any(self.binding_word(other, index, rename.old) for index in range(other.start, other.end))
                  for begin, finish in other.ranges]
        # Named items cannot capture their surrounding local bindings.
        starts = style_footprint.item_starts(self.words)
        for index in starts:
            if index >= len(self.words) or not _contains(site.ranges, index) or self.words[index] not in ITEM_KEYWORDS:
                continue
            end = self.stop(index + 1, self.block_end(index), frozenset({"{", ";"}))
            hidden.append((index, self.ends.get(end, end) + 1))
        ranges = list(site.ranges)
        for begin, finish in hidden:
            ranges = [(a, b) for start, end in ranges for a, b in
                      ((start, min(end, begin)), (max(start, finish), end)) if a < b]
        return BindingScope(site.kind, declaration, tuple(ranges))

    def file_refusal(self, rename: BindingRename, *, resolve_names: bool = False) -> str:
        """Whole-file exclusions: local visibility cannot prove these names are safe to rename."""
        if not resolve_names and (self.name_indexes.get(rename.new) or self.name_indexes.get("r#" + rename.new)):
            return "new name already occurs in file"
        if style_footprint.holds_glob_use(self.words):
            return "file contains a glob import"
        if resolve_names:
            return ""
        binding_indexes = {i for site in self.sites for i in range(site.start, site.end)
                           if self.binding_word(site, i, rename.old)}
        old_names = {rename.old, "r#" + rename.old}
        if any(self.words[i] in old_names for start, end in style_footprint.attribute_spans(self.words)
               for i in range(start, end)) or any(
                   self.words[i] in old_names and (self.words[i - 1:i] == ("::",) or self.words[i + 1:i + 2] == ("::",))
                   for i in range(len(self.words))):
            return "old name also occurs as an item or type"
        # Item headers include generic declarations, aliases, imports and all signature types.
        # Exempt only identified parameter patterns; a matching name elsewhere in a header refuses the file.
        for i, word in enumerate(self.words):
            if word in {"fn", "struct", "union", "enum", "trait", "type", "const", "static", "mod", "use", "impl"}:
                end = self.stop(i + 1, len(self.words), frozenset({";"} if word == "use" else {"{", ";"}))
                if word in {"enum", "trait"} and end in self.ends:
                    end = self.ends[end] + 1
                if any(self.words[j] in old_names and j not in binding_indexes
                       for j in range(i + 1, end)):
                    return "old name also occurs as an item or type"
            # Type annotations, casts and explicit generic arguments. Including uncertain colon
            # expressions is deliberate: this is a refusal, not an attempt to parse Rust types.
            if word in {":", "->", "as"} or word == "<" and i and self.words[i - 1] == "::":
                end = next((j for j in self.level(i + 1, len(self.words), True)
                            if self.words[j] in {"=", ";", ",", "{", "}", ")", "=>", "|", ">", ">>"}), len(self.words))
                if any(self.words[j] in old_names and j not in binding_indexes
                       for j in range(i + 1, end)):
                    return "old name also occurs as an item or type"
        return ""

    def _uncertain_macro_origins(self) -> bool:
        """Definitions and possible imports invalidate every standard macro name in this file."""
        if self.plain_word_set & {"macro_rules", "macro_use", "macro"} or style_footprint.holds_glob_use(self.words):
            return True
        called = {word for i, word in enumerate(self.plain_words[:-1]) if self.plain_words[i + 1] == "!"}
        return any(self.plain_words[i] in called for i in style_footprint.use_words(self.words, self.ends))

    def uncertain_format_macros(self) -> bool:
        """A format-looking macro has no established capture semantics in these files."""
        names = {"println", "eprintln", "print", "eprint", "format", "format_args", "panic", "unreachable", "todo",
                 "unimplemented", "write", "writeln", "assert", "debug_assert", "assert_eq", "assert_ne",
                 "debug_assert_eq", "debug_assert_ne"}
        if "macro_rules" in self.plain_word_set:
            return True
        for i, word in enumerate(self.words):
            if word == "use":
                end = self.stop(i + 1, len(self.words), frozenset({";"}))
                if names & set(self.words[i + 1:end]):
                    return True
        return any(self.words[bang - 2:bang - 1] == ("::",) and self.words[bang - 1] in names
                   for bang, _ in self.macros)

    def binding_comments(self, scope: BindingScope, rename: BindingRename) -> BindingComments | NoBindingScope:
        indexes = {i for i, comment in enumerate(self.comments)
                   if any(start <= comment.gap < end or comment.gap == end and self.words[end - 1] != "}"
                          for start, end in scope.ranges)}
        functions: list[str] = []
        if scope.kind == "fn parameter":
            for fn, word in enumerate(self.words[:scope.declaration]):
                if word != "fn":
                    continue
                paren = next((i for i in self.level(fn + 2, len(self.words), True)
                              if self.words[i] in {"(", "{", ";"}), len(self.words))
                if not paren < scope.declaration < self.ends.get(paren, paren):
                    continue
                functions.append(self.words[fn + 1])
                prefix = min((start for start, owner in style_footprint.attribute_owners(self.words).items()
                              if owner == fn), default=fn)
                openings = {end: start for start, end in self.ends.items()}
                while prefix:
                    previous = prefix - 1
                    if self.words[previous] in {"pub", "async", "unsafe", "const", "extern"}:
                        prefix = previous
                    elif previous in openings and self.words[previous] == ")" and openings[previous] > 0 and self.words[openings[previous] - 1] == "pub":
                        prefix = openings[previous] - 1
                    elif self.tokenized.tokens[previous].kind == "literal" and previous and self.words[previous - 1] == "extern":
                        prefix = previous - 1
                    else:
                        break
                indexes.update(i for i, comment in enumerate(self.comments)
                               if prefix <= comment.gap <= fn and style_footprint.outer_doc(comment.text))
                break
        edits: list[CommentRename] = []
        for i in sorted(indexes):
            comment = self.comments[i]
            code = set(backtick_mentions(comment.text, rename.old))
            for start, end in identifier_mentions(comment.text, rename.old):
                link = (comment.text[start - 1:start] == "[" and comment.text[end:end + 1] == "]"
                        or comment.text[max(0, start - 2):start] == "](" and comment.text[end:end + 1] == ")")
                if (start, end) not in code and not link:
                    return NoBindingScope("prose names the binding")
                edits.append(CommentRename(comment.start + start, comment.start + end, rename.new))
        return BindingComments(tuple(edits), frozenset(indexes), tuple(functions))

    def shorthand(self, index: int) -> bool:
        parent = self.parents.get(index, -1)
        body = any(site.kind in {"closure", "fn parameter"} and site.ranges[-1][0] == parent for site in self.sites)
        return (parent >= 0 and self.words[parent] == "{" and not body
                and style_footprint.shorthand_field(self.words, index))

    def name_word(self, index: int) -> bool:
        words = self.words
        if not style_footprint.IDENTIFIER.fullmatch(words[index]):
            return False
        previous = words[index - 1] if index else ""
        following = words[index + 1] if index + 1 < len(words) else ""
        if previous in {".", "::", "'"} or following in {"::", "!"}:
            return False
        parent = self.parents.get(index, -1)
        field = (following == ":" and previous in {"{", ","} and parent >= 0 and words[parent] == "{"
                 and not any(self.binding_word(site, index, words[index]) for site in self.sites
                             if site.kind in {"closure", "fn parameter"}))
        return not field

    def parameter_collision(self, scope: BindingScope, name: str) -> bool:
        """Sibling parameters share a body even when an inner shadow has split the located scope's ranges."""
        if scope.kind not in {"fn parameter", "closure"}:
            return False
        bodies = {site.ranges[-1] for site in self.sites if site.kind == scope.kind
                  and site.start <= scope.declaration < site.end}
        return any(site.ranges[-1] in bodies and any(self.binding_word(site, i, name) for i in range(site.start, site.end))
                   for site in self.sites if site.kind == scope.kind)

    def unread_binding(self, scope: BindingScope, rename: BindingRename) -> bool:
        """Refuse scoping syntax the scan cannot account for, including bindings inside macro token trees."""
        parsed = {site.start - 1 for site in self.sites}
        if any(_contains(scope.ranges, i) and word == "let" and i not in parsed for i, word in enumerate(self.words)):
            return True
        for bang, finish in self.macros:
            if not any(start < finish and bang < end for start, end in scope.ranges):
                continue
            if set(self.words[bang:finish]) & {"let", "fn", "for", "|"}:
                return True
            if self.words[bang - 1] == "matches":
                comma = self.stop(bang + 2, finish - 1, frozenset({","}))
                guard = self.stop(comma + 1, finish - 1, frozenset({"if"}))
                if {rename.old, rename.new} & set(self.words[comma + 1:guard]):
                    return True
        return False

    def _format_arguments(self) -> frozenset[int]:
        result: set[int] = set()
        plain = {"println", "eprintln", "print", "eprint", "format", "format_args", "panic", "unreachable",
                 "todo", "unimplemented"}
        for bang, finish in self.macros:
            name = self.words[bang - 1] if bang else ""
            minimum = (0 if name in plain else 1 if name in {"write", "writeln", "assert", "debug_assert"}
                       else 2 if name in {"assert_eq", "assert_ne", "debug_assert_eq", "debug_assert_ne"} else -1)
            if minimum < 0:
                continue
            argument = 0
            for index in self.level(bang + 2, finish - 1):
                if self.words[index] == ",":
                    argument += 1
                elif argument == minimum and (self.words[index].startswith('"') or re.match(r'r#*"', self.words[index])):
                    # Only a literal at the start of the format argument has Rust capture semantics.
                    previous = self.words[index - 1]
                    if previous in {"(", "[", "{", ","}:
                        result.add(index)
        return frozenset(result)


class BindingScopeReader:
    """Binding locations and refusals sharing one immutable source scan."""

    def __init__(self, source: str) -> None:
        self._scan: _BindingScan | NoBindingScope
        try:
            self._scan = _BindingScan(source)
        except style_footprint.UnreadableTokenSpans as error:
            self._scan = NoBindingScope(str(error))

    def scope(self, rename: BindingRename) -> BindingScope | NoBindingScope:
        if isinstance(self._scan, NoBindingScope):
            return self._scan
        refusal = self._scan.file_refusal(rename)
        return NoBindingScope(refusal) if refusal else self._scan.locate(rename)


def binding_scope(source: str, rename: BindingRename) -> BindingScope | NoBindingScope:
    """Locate one binding and subtract each inner pattern and scope that shadows its old name."""
    return BindingScopeReader(source).scope(rename)


RESOLVABLE_NAME_REFUSALS = frozenset({"new name already occurs in file", "old name also occurs as an item or type"})


def resolution_scope(source: str, rename: BindingRename) -> BindingScope | NoBindingScope:
    """Locate a candidate for LSP proof; this is not evidence that its rename is safe.

    Only the two whole-file name exclusions yield to resolution. Ledger callers continue using binding_scope
    and binding_rename_check, whose text refusals are unchanged.
    """
    try:
        scan = _BindingScan(source)
    except style_footprint.UnreadableTokenSpans as error:
        return NoBindingScope(str(error))
    refusal = scan.file_refusal(rename, resolve_names=True)
    return NoBindingScope(refusal) if refusal else scan.locate(rename)


def binding_comments(source: str, rename: BindingRename) -> BindingComments | NoBindingScope:
    """Plan only exact code/link mentions; prose requires a normal ticket."""
    try:
        scan = _BindingScan(source)
    except style_footprint.UnreadableTokenSpans as error:
        return NoBindingScope(str(error))
    scope = scan.locate(rename)
    return scope if isinstance(scope, NoBindingScope) else scan.binding_comments(scope, rename)


def _captures(literal: str) -> tuple[tuple[int, int], ...]:
    """Named format captures, including width/precision uses so an unedited use cannot disappear from the proof."""
    captures: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(literal):
        if literal[cursor:cursor + 2] in {"{{", "}}"}:
            cursor += 2
            continue
        if literal[cursor] == "{":
            end = literal.find("}", cursor + 1)
            if end < 0:
                break
            name = style_footprint.IDENTIFIER.match(literal, cursor + 1)
            if name and name.end() <= end and literal[name.end():name.end() + 1] in {"}", ":"}:
                captures.append(name.span())
            for width in re.finditer(r"[A-Za-z_][A-Za-z_0-9]*\$", literal[cursor + 1:end]):
                captures.append((cursor + 1 + width.start(), cursor + width.end()))
            cursor = end + 1
        else:
            cursor += 1
    return tuple(captures)


@dataclass(frozen=True)
class _BindingEdit:
    """An aligned name replacement, with token locations on both sides and its syntactic form."""

    before: int
    after: int
    old: str
    new: str
    form: str


def _binding_alignment(before: _BindingScan, after: _BindingScan,
                       pairs: frozenset[tuple[str, str]]) -> tuple[bool, tuple[_BindingEdit, ...], dict[int, int]]:
    edits: list[_BindingEdit] = []
    aligned: dict[int, int] = {}
    old, new = before.words, after.words
    a, b = 0, 0
    while a < len(old) and b < len(new):
        aligned[a] = b
        if (b + 2 < len(new) and new[b:b + 2] == (old[a], ":") and (old[a], new[b + 2]) in pairs
                and before.shorthand(a)):
            edits.append(_BindingEdit(a, b + 2, old[a], new[b + 2], "shorthand"))
            a, b = a + 1, b + 3
        elif (a + 2 < len(old) and old[a:a + 2] == (new[b], ":") and (old[a + 2], new[b]) in pairs
              and after.shorthand(b)):
            aligned[a + 1], aligned[a + 2] = b, b
            edits.append(_BindingEdit(a + 2, b, old[a + 2], new[b], "shorthand"))
            a, b = a + 3, b + 1
        elif old[a] == new[b]:
            a, b = a + 1, b + 1
        elif (old[a], new[b]) in pairs:
            edits.append(_BindingEdit(a, b, old[a], new[b], "name"))
            a, b = a + 1, b + 1
        elif a in before.formats and b in after.formats:
            left, right = old[a], new[b]
            left_captures, right_captures = _captures(left), _captures(right)
            if len(left_captures) != len(right_captures):
                return False, tuple(edits), aligned
            rebuilt, cursor = "", 0
            for (start, end), (target, finish) in zip(left_captures, right_captures):
                x, y = left[start:end], right[target:finish]
                if x != y:
                    if (x, y) not in pairs or left[start - 1] != "{":
                        return False, tuple(edits), aligned
                    edits.append(_BindingEdit(a, b, x, y, "capture"))
                rebuilt += left[cursor:start] + y
                cursor = end
            if rebuilt + left[cursor:] != right:
                return False, tuple(edits), aligned
            a, b = a + 1, b + 1
        else:
            return False, tuple(edits), aligned
    return a == len(old) and b == len(new), tuple(edits), aligned


def binding_rename_check(path: str, before: str, after: str, renames: tuple[BindingRename, ...]) -> BindingProof:
    """Prove each scoped rename and whether those proven renames explain every token edit in the file."""
    try:
        original, edited = _BindingScan(before), _BindingScan(after)
    except style_footprint.UnreadableTokenSpans as error:
        return BindingProof({rename: NoAppliedCheck("rename") for rename in renames},
                            {rename: str(error) for rename in renames}, False, {})
    alignment, edits, aligned = _binding_alignment(original, edited, frozenset((r.old, r.new) for r in renames))
    checks: dict[BindingRename, AppliedCheck] = {}
    refusals: dict[BindingRename, str] = {}
    explained: set[_BindingEdit] = set()
    comment_plans: dict[BindingRename, BindingComments] = {}
    for rename in renames:
        scope = original.locate(rename)
        reason = original.file_refusal(rename)
        if reason:
            pass
        elif isinstance(scope, NoBindingScope):
            reason = scope.reason
        elif not alignment:
            reason = "tokens do not align as approved renames"
        else:
            comments = original.binding_comments(scope, rename)
            own = tuple(edit for edit in edits if (edit.old, edit.new) == (rename.old, rename.new)
                        and _contains(scope.ranges, edit.before))
            if not own:
                checks[rename] = RenameNotApplied(path)
                continue
            if not any(edit.before == scope.declaration for edit in own):
                reason = "declaration was not renamed"
            elif isinstance(comments, NoBindingScope):
                reason = comments.reason
            elif original.unread_binding(scope, rename):
                reason = "scope contains a binding whose visibility cannot be checked"
            elif any(edit.form == "capture" for edit in own) and original.uncertain_format_macros():
                reason = "file defines or imports a possible format macro"
            elif original.macro_origins_uncertain and any(
                    any(start < finish and bang < end for start, end in scope.ranges)
                    for bang, finish in original.macros):
                reason = "scope contains a macro with an uncertain origin"
            elif any(_contains(scope.ranges, i) for name in ("r#" + rename.old, "r#" + rename.new)
                     for i in original.name_indexes.get(name, ())):
                reason = "scope contains a raw spelling of the name"
            elif original.parameter_collision(scope, rename.new):
                reason = "another parameter binds the new name"
            elif any(_contains(scope.ranges, i) and not original.words[i].startswith("r")
                     and re.search(r"\\(?:u\{|x[0-9a-fA-F]{2})", original.words[i]) for i in original.formats):
                reason = "encoded format text can hide captures"
            elif any(_contains(scope.ranges, i) and original.words[i][start:end] == rename.new
                     for i in original.formats for start, end in _captures(original.words[i])):
                reason = "new format capture already occurs in scope"
            elif any(any(start < finish and bang < end for start, end in scope.ranges)
                     and original.words[bang - 1] not in style_footprint.STD_EXPRESSION_MACROS - {"concat", "stringify"}
                     for bang, finish in original.macros):
                reason = "scope contains a macro whose names cannot be checked"
            elif any(edit.form == "name" and (not original.name_word(edit.before)
                                             or original.shorthand(edit.before) or not edited.name_word(edit.after)
                                             or edited.shorthand(edit.after)) for edit in own):
                reason = "edit changes a field or qualified name"
            else:
                visible = {aligned[i] for start, end in scope.ranges for i in range(start, end) if i in aligned}
                # Expansion inserts a value token; collapse maps its old value to the surviving shorthand token.
                visible.update(edit.after for edit in own)
                targets = {edit.after for edit in own if edit.form != "capture"}
                if any(i in visible and edited.name_word(i) and (name == rename.old or i not in targets)
                       for name in (rename.old, rename.new) for i in edited.name_indexes.get(name, ())):
                    reason = "scope holds a missed use or an unexplained new name"
                elif any(edited.words[i][start:end] == rename.old for i in edited.formats & visible
                         for start, end in _captures(edited.words[i])):
                    reason = "scope holds a missed format capture"
                else:
                    checks[rename] = AppliedInCode()
                    assert isinstance(comments, BindingComments)
                    comment_plans[rename] = comments
                    explained.update(own)
                    continue
        refusals[rename] = reason
        checks[rename] = NoAppliedCheck("rename")
    comments_explained, bad_comments = _check_binding_comments(original, edited, aligned, tuple(comment_plans.values()))
    for rename, plan in comment_plans.items():
        if plan.indexes & bad_comments:
            checks[rename] = NoAppliedCheck("rename")
            refusals[rename] = "comment rename is missing or has an unexplained edit"
    line_map: dict[int, int] = {}
    if alignment:
        for old_index, new_index in aligned.items():
            _ = line_map.setdefault(original.tokenized.lines[old_index], edited.tokenized.lines[new_index])
    return BindingProof(checks, refusals, alignment and comments_explained and all(edit in explained for edit in edits), line_map)


def _check_binding_comments(before: _BindingScan, after: _BindingScan, aligned: Mapping[int, int],
                            plans: tuple[BindingComments, ...]) -> tuple[bool, frozenset[int]]:
    """Every comment stays at its aligned code boundary and changes only by planned mention replacements."""
    if len(before.spans) != len(before.words) or len(after.spans) != len(after.words):
        return False, frozenset(range(len(before.comments)))
    edits: dict[tuple[int, int], str] = {}
    for plan in plans:
        for edit in plan.edits:
            key = (edit.start, edit.end)
            if key in edits and edits[key] != edit.replacement:
                return False, frozenset(range(len(before.comments)))
            edits[key] = edit.replacement
    if len(before.comments) != len(after.comments):
        return False, frozenset(range(len(before.comments)))
    bad: set[int] = set()
    ordered_edits = sorted(edits.items(), reverse=True)
    for i, (original, edited) in enumerate(zip(before.comments, after.comments, strict=True)):
        expected = original.text
        for (start, end), replacement in ordered_edits:
            if original.start <= start < end <= original.end:
                expected = expected[:start - original.start] + replacement + expected[end - original.start:]
        gap = len(after.words) if original.gap == len(before.words) else aligned.get(original.gap, -1)
        if edited.text != expected or edited.gap != gap:
            bad.add(i)
    return not bad, frozenset(bad)


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
            or len(stems) == 1 or stems & ROOT_STEMS or stems & style_footprint.RUST_KEYWORDS):
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
        negated_object = NEGATED_OBJECT.match(rest, move.end())
        if not (NEGATED_VERB.search(clause) or negated_subject or negated_noun or negated_object):
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
    starts = style_footprint.item_starts(words)
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
