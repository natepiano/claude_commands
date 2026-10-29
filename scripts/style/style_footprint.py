#!/usr/bin/env python3
"""Conservatively separate mechanical style diffs from findings needing review."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping
from functools import lru_cache
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

from candidate_generators import boilerplate_allows, mask_source

RAW_STRING = re.compile(r'(?:br|cr|r)(#*)"')
QUOTED_LITERAL = re.compile(r'''(?:b|c)?"(?:\\.|[^"\\])*"|b?'(?:\\u\{[0-9A-Fa-f_]+\}|\\x[0-9A-Fa-f]{2}|\\.|[^'\\\n])' ''',
                            re.VERBOSE | re.DOTALL)
NUMBER = re.compile(r"\d[\w.]*(?:[+-]\d+)?")
IDENTIFIER = re.compile(r"(?:r#)?[A-Za-z_][A-Za-z_0-9]*")
PUNCTUATION = re.compile(r"::|->|=>|==|!=|<=|>=|&&|\|\||[{}()\[\];:,.<>!&|+*/%=\-?#@'$^~]")
FILE_CHANGED_LINE_LIMIT = 200
# The std traits whose derives add no names to the item they decorate.
STD_DERIVES = frozenset({"Clone", "Copy", "PartialEq", "Eq", "Hash", "PartialOrd", "Ord", "Default", "Debug"})
# Snake-case names a bare word reaches without an import: prelude functions, primitive types and path keywords.
PRELUDE_NAMES = frozenset({
    "drop", "bool", "char", "str", "f32", "f64", "i8", "i16", "i32", "i64", "i128", "isize",
    "u8", "u16", "u32", "u64", "u128", "usize", "self", "super", "crate",
})
# The Rust keywords, strict and reserved, of edition 2024: a name with one of these needs `r#`. `edition_keywords`
# gives those of an older edition.
RUST_KEYWORDS = frozenset({
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum", "extern", "false", "fn", "for",
    "gen", "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self",
    "static", "struct", "super", "trait", "true", "try", "type", "unsafe", "use", "where", "while", "abstract", "become",
    "box", "do", "final", "macro", "override", "priv", "typeof", "unsized", "virtual", "yield",
})
# The keywords of editions 2018 to 2021, where `gen` is a name. A scan with no crate, so no edition, reads these: a
# scan that reads a name too often refuses, and one that misses a name can pass a change it does not read.
KEYWORDS_BEFORE_2024 = RUST_KEYWORDS - {"gen"}


class FootprintError(Exception):
    pass


class CheckCouldNotRun(Exception):
    """A git command failed, so the change was not judged."""


@dataclass(frozen=True)
class Token:
    value: str
    kind: Literal["identifier", "literal", "punctuation", "unknown"]


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    removed: tuple[str, ...]
    added: tuple[str, ...]


@dataclass(frozen=True)
class WholeDiff:
    pass


@dataclass(frozen=True)
class MappedFinding:
    finding_id: str


class FileStatus(Enum):
    MODIFIED = "M"
    ADDED = "A"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPE_CHANGED = "T"
    UNMERGED = "U"
    UNKNOWN = "X"
    BROKEN_PAIR = "B"


@dataclass(frozen=True)
class FileChange:
    status: FileStatus
    old_path: str
    path: str
    before: str
    after: str
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True)
class Finding:
    label: WholeDiff | MappedFinding
    changes: tuple[FileChange, ...]


class FindingOwnershipInput(TypedDict, total=False):
    files: object
    hunks: object


class HunkOwnershipInput(TypedDict, total=False):
    path: object
    old_start: object
    new_start: object


@dataclass(frozen=True)
class HunkOwnership:
    path: str
    old_start: int
    new_start: int


@dataclass(frozen=True)
class Mechanical:
    pass


@dataclass(frozen=True)
class Review:
    reasons: tuple[str, ...]


def git(repo: Path, *arguments: str, input_text: str = "") -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], input=input_text,
                            capture_output=True, text=True, errors="surrogateescape", check=False)
    if result.returncode:
        raise CheckCouldNotRun(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def folded_base(repo: Path, base: str, head: str) -> str:
    current = git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    merges = git(repo, "rev-list", "--reverse", "--first-parent", "--merges", f"{current}..{head}").splitlines()
    for merge in merges:
        parents = git(repo, "show", "-s", "--format=%P", merge).split()[1:]
        for parent in parents:
            result = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", current, parent],
                                    capture_output=True, text=True, check=False)
            if result.returncode == 1:
                raise FootprintError("merge in range")
            if result.returncode:
                raise CheckCouldNotRun(f"git merge-tree: {result.stderr.strip()} (exit {result.returncode})")
            tree = result.stdout.splitlines()[0]
            # Subsequent folds need ancestry, not merely the intermediate tree.
            environment = dict(os.environ, GIT_AUTHOR_NAME="Style footprint", GIT_AUTHOR_EMAIL="style@example.invalid",
                               GIT_COMMITTER_NAME="Style footprint", GIT_COMMITTER_EMAIL="style@example.invalid")
            commit = subprocess.run(["git", "-C", str(repo), "commit-tree", "--no-gpg-sign", tree, "-p", current, "-p", parent],
                                    input="Fold merged-in work for style comparison\n", capture_output=True,
                                    text=True, env=environment, check=False)
            if commit.returncode:
                raise CheckCouldNotRun(commit.stderr.strip())
            current = commit.stdout.strip()
    return current


@dataclass(frozen=True)
class TokenizedText:
    tokens: tuple[Token, ...]
    lines: tuple[int, ...]


@lru_cache(maxsize=128)
def tokenized_text(text: str) -> TokenizedText:
    """Keep literals opaque; comments cannot turn their contents into Rust code."""
    result: list[Token] = []
    lines: list[int] = []
    previous, line = 0, 1

    def append(token: Token) -> None:
        nonlocal previous, line
        line += text.count("\n", previous, cursor)
        previous = cursor
        result.append(token)
        lines.append(line)

    cursor = 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        if text.startswith("//", cursor):
            end = text.find("\n", cursor)
            cursor = len(text) if end < 0 else end
            continue
        if text.startswith("/*", cursor):
            depth = 1
            cursor += 2
            while cursor < len(text) and depth:
                if text.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif text.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                else:
                    cursor += 1
            if depth:
                append(Token("unterminated comment", "unknown"))
            continue
        raw = RAW_STRING.match(text, cursor)
        if raw:
            terminator = '"' + raw.group(1)
            end = text.find(terminator, raw.end())
            finish = len(text) if end < 0 else end + len(terminator)
            append(Token(text[cursor:finish], "literal" if end >= 0 else "unknown"))
            cursor = finish
            continue
        quoted = QUOTED_LITERAL.match(text, cursor)
        if quoted:
            append(Token(quoted.group(), "literal"))
            cursor = quoted.end()
            continue
        number = NUMBER.match(text, cursor)
        if number:
            append(Token(number.group(), "literal"))
            cursor = number.end()
            continue
        identifier = IDENTIFIER.match(text, cursor)
        if identifier:
            word = identifier.group()
            append(Token(word, "literal" if word in {"true", "false"} else "identifier"))
            cursor = identifier.end()
            continue
        punctuation = PUNCTUATION.match(text, cursor)
        if punctuation:
            append(Token(punctuation.group(), "punctuation"))
            cursor = punctuation.end()
            continue
        append(Token(text[cursor], "unknown"))
        cursor += 1
    return TokenizedText(tuple(result), tuple(lines))


def tokens(text: str) -> tuple[Token, ...]:
    return tokenized_text(text).tokens


@dataclass(frozen=True)
class TokenSpan:
    token: Token
    start: int
    end: int


class MoveRefused(Exception):
    """The change is not a proven item move; the message says why."""


class UnreadableTokenSpans(MoveRefused):
    """Code tokens could not be located; an empty, readable source is a different result."""


def token_spans(text: str) -> tuple[TokenSpan, ...]:
    """Locate code tokens without treating comments as replacement sites."""
    result: list[TokenSpan] = []
    cursor = 0
    for token in tokens(text):
        while cursor < len(text):
            if text[cursor].isspace():
                cursor += 1
            elif text.startswith("//", cursor):
                end = text.find("\n", cursor)
                cursor = len(text) if end < 0 else end
            elif text.startswith("/*", cursor):
                depth = 1
                cursor += 2
                while cursor < len(text) and depth:
                    if text.startswith("/*", cursor):
                        depth += 1
                        cursor += 2
                    elif text.startswith("*/", cursor):
                        depth -= 1
                        cursor += 2
                    else:
                        cursor += 1
            else:
                break
        if token.kind == "unknown" or not text.startswith(token.value, cursor):
            raise UnreadableTokenSpans("source token offsets are unavailable")
        end = cursor + len(token.value)
        result.append(TokenSpan(token, cursor, end))
        cursor = end
    return tuple(result)


def parse_hunks(patch: str) -> tuple[Hunk, ...]:
    hunks: list[Hunk] = []
    old_start = old_count = new_start = 0
    removed: list[str] = []
    added: list[str] = []
    active = False
    for line in patch.splitlines():
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            if active:
                hunks.append(Hunk(old_start, old_count, new_start, tuple(removed), tuple(added)))
            old_start, old_count, new_start = int(match[1]), int(match[2] or "1"), int(match[3])
            removed, added = [], []
            active = True
        elif active and line.startswith("-"):
            removed.append(line[1:])
        elif active and line.startswith("+"):
            added.append(line[1:])
    if active:
        hunks.append(Hunk(old_start, old_count, new_start, tuple(removed), tuple(added)))
    return tuple(hunks)


def changes_between(repo: Path, base: str, head: str) -> tuple[FileChange, ...]:
    entries = iter(git(repo, "diff", "--no-ext-diff", "--name-status", "-z", "-M", base, head, "--").split("\0"))
    result: list[FileChange] = []
    for status in entries:
        if not status:
            continue
        old_path = next(entries)
        path = next(entries) if status.startswith(("R", "C")) else old_path
        before = "" if status == "A" else git(repo, "show", f"{base}:{old_path}")
        after = "" if status == "D" else git(repo, "show", f"{head}:{path}")
        patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--unified=0", "-M", base, head, "--", old_path, path)
        result.append(FileChange(FileStatus(status[0]), old_path, path, before, after, parse_hunks(patch)))
    return tuple(result)


def select_hunks(change: FileChange, selected: tuple[Hunk, ...]) -> FileChange:
    lines = change.before.splitlines(keepends=True)
    for hunk in reversed(selected):
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        lines[start:start + hunk.old_count] = [line + "\n" for line in hunk.added]
    return FileChange(change.status, change.old_path, change.path, change.before, "".join(lines), selected)


def finding_groups(path: Path, changes: tuple[FileChange, ...]) -> tuple[Finding, ...]:
    """Map ids to file lists, or to {files: [...], hunks: [{path, old_start, new_start}]}.

    A hunk selector names its unified-diff start coordinates. Every changed hunk
    must have exactly one owner; omitted changes never silently pass the gate.
    """
    data: object = cast(object, json.loads(path.read_text()))
    if not isinstance(data, dict):
        raise FootprintError("finding map must be an object")
    mapping = cast(dict[str, object], data)
    claimed: set[tuple[str, int]] = set()
    findings: list[Finding] = []
    for name, specification in mapping.items():
        files: list[object] = []
        selectors: list[object] = []
        if isinstance(specification, list):
            files = cast(list[object], specification)
        elif isinstance(specification, dict):
            owner = cast(FindingOwnershipInput, cast(object, specification))
            raw_files, raw_hunks = owner.get("files", []), owner.get("hunks", [])
            if not isinstance(raw_files, list) or not isinstance(raw_hunks, list):
                raise FootprintError(f"{name}: files and hunks must be lists")
            files, selectors = cast(list[object], raw_files), cast(list[object], raw_hunks)
        else:
            raise FootprintError(f"{name}: invalid finding ownership")
        if any(not isinstance(file, str) for file in files):
            raise FootprintError(f"{name}: file paths must be strings")
        owners: list[HunkOwnership] = []
        for selector in selectors:
            if not isinstance(selector, dict):
                raise FootprintError(f"{name}: invalid hunk selector")
            item = cast(HunkOwnershipInput, cast(object, selector))
            file_path, old_start, new_start = item.get("path"), item.get("old_start"), item.get("new_start")
            if not isinstance(file_path, str) or type(old_start) is not int or type(new_start) is not int:
                raise FootprintError(f"{name}: hunk needs path, old_start and new_start")
            owners.append(HunkOwnership(file_path, old_start, new_start))
        selected: list[FileChange] = []
        for change in changes:
            owned: list[Hunk] = []
            for index, hunk in enumerate(change.hunks):
                matches = change.path in files or change.old_path in files
                for owner in owners:
                    if owner.path == change.path and owner.old_start == hunk.old_start and owner.new_start == hunk.new_start:
                        matches = True
                if matches:
                    key = (change.path, index)
                    if key in claimed:
                        raise FootprintError(f"{change.path}: hunk owned twice")
                    claimed.add(key)
                    owned.append(hunk)
            if owned or (not change.hunks and change.path in files):
                selected.append(select_hunks(change, tuple(owned)))
        if not selected:
            raise FootprintError(f"{name}: no changed files or hunks")
        findings.append(Finding(MappedFinding(name), tuple(selected)))
    if len(claimed) != sum(len(change.hunks) for change in changes) or any(
        not change.hunks and not any(change.path == owned.path for finding in findings for owned in finding.changes)
        for change in changes
    ):
        raise FootprintError("finding map leaves changes unowned")
    return tuple(findings)


class RevisionTree:
    def __init__(self, repo: Path, revision: str) -> None:
        self.repo: Path = repo
        self.revision: str = revision
        self.paths: frozenset[str] = frozenset(filter(None, git(repo, "ls-tree", "-r", "--name-only", "-z", revision).split("\0")))
        self.cache: dict[str, str] = {}
        self.public_globs: dict[str, bool] = {}

    def text(self, path: str) -> str:
        if path not in self.cache:
            self.cache[path] = git(self.repo, "show", f"{self.revision}:{path}")
        return self.cache[path]

    def crate(self, path: str) -> str:
        for directory in PurePosixPath(path).parents:
            if str(directory / "Cargo.toml") in self.paths:
                return str(directory)
        return "."

    def externally_reachable(self, path: str) -> bool:
        crate = self.crate(path)
        if self.has_public_glob(crate):
            return True  # A glob re-export prevents establishing module privacy.
        root = PurePosixPath(crate) / "src"
        try:
            relative = PurePosixPath(path).relative_to(root)
        except ValueError:
            return True  # An unfamiliar layout cannot establish privacy.
        names = list(relative.with_suffix("").parts)
        if names[-1] in {"lib", "main", "mod"}:
            _ = names.pop()
        parent = str(root / "lib.rs")
        if parent not in self.paths:
            parent = str(root / "main.rs")
        for index, name in enumerate(names):
            if parent not in self.paths:
                return True
            source = " ".join(token.value for token in tokens(self.text(parent)))
            declaration = re.search(r"(?:(pub)(?:\s*\([^)]*\))?\s+)?mod\s+" + re.escape(name) + r"\s*;", source)
            if declaration and (not declaration[1] or "(" in declaration.group()):
                return False
            directory = root.joinpath(*names[:index + 1])
            parent = str(directory.with_suffix(".rs"))
            if parent not in self.paths:
                parent = str(directory / "mod.rs")
        return True

    def has_public_glob(self, crate: str) -> bool:
        if crate not in self.public_globs:
            self.public_globs[crate] = any(
                self.crate(path) == crate and path.endswith(".rs") and public_glob(self.text(path))
                for path in self.paths
            )
        return self.public_globs[crate]


def public_glob(source: str) -> bool:
    words = tuple(token.value for token in tokens(source))
    public_use = False
    for index, word in enumerate(words):
        if word == "use":
            public_use = index > 0 and words[index - 1] == "pub"
        elif word == ";":
            public_use = False
        elif word == "*" and public_use:
            return True
    return False


def documented_text(source: str) -> tuple[str, ...]:
    """Read complete doc comments, including interior lines outside diff prefixes."""
    comments: list[str] = []
    cursor = 0
    while cursor < len(source):
        start = cursor
        if source.startswith("//", cursor):
            end = source.find("\n", cursor)
            cursor = len(source) if end < 0 else end
            if source.startswith(("///", "//!"), start):
                comments.append(source[start:cursor])
        elif source.startswith("/*", cursor):
            depth = 1
            cursor += 2
            while cursor < len(source) and depth:
                if source.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif source.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                else:
                    cursor += 1
            if source.startswith(("/**", "/*!"), start):
                comments.append(source[start:cursor])
        else:
            raw = RAW_STRING.match(source, cursor)
            quoted = QUOTED_LITERAL.match(source, cursor)
            if raw:
                terminator = '"' + raw[1]
                end = source.find(terminator, raw.end())
                cursor = len(source) if end < 0 else end + len(terminator)
            elif quoted:
                cursor = quoted.end()
            else:
                cursor += 1
    return tuple(comments)


def attribute_spans(words: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(words):
        if words[cursor] != "#":
            cursor += 1
            continue
        start = cursor
        cursor += 1
        if cursor < len(words) and words[cursor] == "!":
            cursor += 1
        if cursor == len(words) or words[cursor] != "[":
            continue
        depth = 1
        cursor += 1
        while cursor < len(words) and depth:
            depth += (words[cursor] == "[") - (words[cursor] == "]")
            cursor += 1
        spans.append((start, cursor))
    return tuple(spans)


def attribute_owners(words: tuple[str, ...]) -> dict[int, int]:
    owners: dict[int, int] = {}
    spans = attribute_spans(words)
    following_attributes = dict(spans)
    for start, end in spans:
        cursor = end
        while cursor < len(words):
            following = following_attributes.get(cursor, cursor)
            if following != cursor:
                cursor = following
                continue
            word = words[cursor]
            if word in {"fn", "struct", "union", "enum", "type", "trait", "const", "static", "mod", "impl"} and not (
                word == "const" and cursor + 1 < len(words) and words[cursor + 1] == "fn"
            ):
                owners[start] = cursor
                break
            if word == "pub":
                cursor += 1
                if cursor < len(words) and words[cursor] == "(":
                    depth = 1
                    cursor += 1
                    while cursor < len(words) and depth:
                        depth += (words[cursor] == "(") - (words[cursor] == ")")
                        cursor += 1
            elif word in {"async", "unsafe", "default", "auto", "const"}:
                cursor += 1
            elif word == "extern":
                cursor += 1
                if cursor < len(words) and words[cursor].startswith('"'):
                    cursor += 1
            else:
                break  # Do not attach an unknown item's attribute to its contents.
    return owners


def decorated_items(words: tuple[str, ...]) -> dict[int, tuple[str, ...]]:
    items: dict[int, tuple[str, ...]] = {}
    owners = attribute_owners(words)
    for start, end in attribute_spans(words):
        if start in owners:
            item = owners[start]
            items[item] = (*items.get(item, ()), *words[start:end])
    return items


def without_derives(source_tokens: tuple[Token, ...]) -> tuple[Token, ...]:
    words = tuple(token.value for token in source_tokens)
    owners = attribute_owners(words)
    omitted = {index for start, end in attribute_spans(words)
               if words[start + 2] == "derive" and start in owners
               and words[owners[start]] in {"struct", "union", "enum", "trait", "type"}
               for index in range(start, end)}
    return tuple(token for index, token in enumerate(source_tokens) if index not in omitted)


def macro_invocation_lines(source: str) -> frozenset[int]:
    """Locate complete token trees; macro expansion can observe whitespace."""
    lines: set[int] = set()
    delimiters: list[str] = []
    macro_starts: dict[int, int] = {}
    recent: list[tuple[str, int]] = []
    cursor, line = 0, 1
    while cursor < len(source):
        start = cursor
        if source[cursor].isspace():
            line += source[cursor] == "\n"
            cursor += 1
            continue
        if source.startswith("//", cursor):
            end = source.find("\n", cursor)
            cursor = len(source) if end < 0 else end
            continue
        if source.startswith("/*", cursor):
            depth = 1
            cursor += 2
            while cursor < len(source) and depth:
                if source.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif source.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                else:
                    cursor += 1
            line += source.count("\n", start, cursor)
            continue
        raw = RAW_STRING.match(source, cursor)
        quoted = QUOTED_LITERAL.match(source, cursor)
        if raw:
            end = source.find('"' + raw[1], raw.end())
            cursor = len(source) if end < 0 else end + 1 + len(raw[1])
        elif quoted:
            cursor = quoted.end()
        else:
            match = IDENTIFIER.match(source, cursor) or NUMBER.match(source, cursor) or PUNCTUATION.match(source, cursor)
            cursor = match.end() if match else cursor + 1
        word = source[start:cursor]
        if word in {"(", "[", "{"}:
            delimiters.append({"(": ")", "[": "]", "{": "}"}[word])
            if (len(recent) == 2 and recent[-1][0] == "!" and IDENTIFIER.fullmatch(recent[-2][0])
                    and recent[-2][0] not in {"return", "break", "yield", "if", "while", "match", "in", "else"}):
                macro_starts[len(delimiters)] = recent[-2][1]
        elif delimiters and word == delimiters[-1]:
            if len(delimiters) in macro_starts:
                lines.update(range(macro_starts.pop(len(delimiters)), line + 1))
            _ = delimiters.pop()
        recent = [*recent[-1:], (word, line)]
        line += source.count("\n", start, cursor)
    for start_line in macro_starts.values():
        lines.update(range(start_line, line + 1))
    return frozenset(lines)


def changes_macro_invocation(change: FileChange) -> bool:
    old_lines = macro_invocation_lines(change.before)
    new_lines = macro_invocation_lines(change.after)
    offset = 0
    for hunk in change.hunks:
        if any(line in old_lines for line in range(hunk.old_start, hunk.old_start + hunk.old_count)):
            return True
        # Finding-owned hunks reconstruct their own after text, whose line
        # numbers can differ from HEAD when another finding owns earlier edits.
        start = hunk.old_start + (not hunk.old_count) + offset
        if any(line in new_lines for line in range(start, start + len(hunk.added))):
            return True
        offset += len(hunk.added) - hunk.old_count
    return False


def behavior_flagged(lines: str) -> bool:
    line_tokens = tokens(lines)
    if any(token.kind == "literal" for token in line_tokens):
        return True
    words = [token.value for token in line_tokens]
    if set(words) & {"if", "match", "while", "==", "!=", "<=", ">="}:
        return True
    # Brackets in a declaration's type or in an explicit turbofish are not
    # comparisons. Other angle brackets remain uncertain and are flagged.
    signature = False
    type_position = False
    generic_depth = 0
    for index, word in enumerate(words):
        if word in {"fn", "struct", "enum", "trait", "type"}:
            signature = True
        elif word in {"{", ";", "="} and not generic_depth:
            signature = type_position = False
        elif word == "->" or (word == ":" and signature):
            type_position = True
        if word == "<":
            declaration = index >= 2 and words[index - 2] in {"fn", "struct", "enum", "trait", "type"}
            if generic_depth or type_position or declaration or (index and words[index - 1] == "::"):
                generic_depth += 1
            else:
                return True
        elif word == ">":
            if generic_depth:
                generic_depth -= 1
            else:
                return True
    return False


def shapes(source: str, public_only: bool, *, glob_reexported: bool = False) -> tuple[tuple[str, ...], ...]:
    """Read item headers, type bodies and trait-impl headers without judging bodies."""
    words = [token.value for token in tokens(source)]
    attributes = decorated_items(tuple(words))
    result: list[tuple[str, ...]] = []
    visibility = [True]
    module_braces: dict[int, bool] = {}
    for index, word in enumerate(words):
        if word == "mod":
            end = next((i for i in range(index, len(words)) if words[i] in {"{", ";"}), len(words))
            if end < len(words) and words[end] == "{":
                module_braces[end] = glob_reexported or (index > 0 and words[index - 1] == "pub")
        if word == "{":
            visibility.append(visibility[-1] and module_braces.get(index, True))
        elif word == "}":
            if len(visibility) > 1:
                _ = visibility.pop()
        if public_only:
            if word != "pub" or not visibility[-1] or index + 1 == len(words) or words[index + 1] == "(":
                continue
            start = index
        else:
            if word not in {"struct", "union", "enum", "trait", "impl", "type"}:
                continue
            start = index
        end = next((i for i in range(start, len(words)) if words[i] in {"{", ";"}), len(words))
        header = words[start:end]
        if not public_only and word == "impl" and "for" not in header:
            continue
        if word in {"struct", "union", "enum", "trait", "type"}:
            attached = attributes.get(index, ())
            for begin, finish in attribute_spans(attached):
                if attached[begin + 2] == "derive":
                    result.append(attached[begin:finish])
        if any(item in header for item in ("struct", "union", "enum", "trait")) and end < len(words) and words[end] == "{":
            depth, finish = 1, end + 1
            while finish < len(words) and depth:
                depth += (words[finish] == "{") - (words[finish] == "}")
                finish += 1
            end = finish
        result.append(tuple(words[start:end]))
    return tuple(result)


@dataclass(frozen=True)
class TokenRewrite:
    start: int
    end: int
    replacement: tuple[Token, ...]


@dataclass(frozen=True)
class ImportProof:
    before: tuple[TokenRewrite, ...] = ()
    after: tuple[TokenRewrite, ...] = ()


@dataclass(frozen=True)
class LintAttribute:
    start: int
    end: int
    level: str
    inner: bool
    names: frozenset[tuple[str, ...]]
    reason: tuple[Token, ...]


@dataclass(frozen=True)
class LintAttributeProof:
    before: tuple[TokenRewrite, ...] = ()
    after: tuple[TokenRewrite, ...] = ()
    # Each removed attribute with the attribute that replaces it, or None when it goes whole.
    pairs: tuple[tuple[LintAttribute, LintAttribute | None], ...] = ()


@dataclass(frozen=True)
class FileTokenRewrites:
    before: tuple[TokenRewrite, ...]
    after: tuple[TokenRewrite, ...]


@dataclass(frozen=True)
class UseItem:
    start: int
    end: int
    paths: tuple[tuple[str, ...], ...]
    attributed: bool


@dataclass(frozen=True)
class FunctionRename:
    path: str
    start: int
    end: int
    old: str
    new: str


@dataclass(frozen=True)
class CrateRename:
    old: str
    new: str


@dataclass(frozen=True)
class EnumVariantRename:
    enum: str
    old: str
    new: str


@dataclass(frozen=True)
class EnumBody:
    name: str
    start: int
    end: int
    variants: tuple[int, ...]
    attributed_variants: frozenset[int]
    safe_attributes: bool


@dataclass(frozen=True)
class FileNormalization:
    rewrites: FileTokenRewrites
    renames: tuple[TokenRewrite, ...]
    lint_attributes: LintAttributeProof


def rewrite_tokens(source: tuple[Token, ...], rewrites: tuple[TokenRewrite, ...]) -> tuple[Token, ...]:
    result = list(source)
    for rewrite in sorted(rewrites, key=lambda item: item.start, reverse=True):
        result[rewrite.start:rewrite.end] = rewrite.replacement
    return tuple(result)


def closing(words: tuple[str, ...], start: int) -> int:
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack = [pairs[words[start]]]
    for index in range(start + 1, len(words)):
        word = words[index]
        if word in pairs:
            stack.append(pairs[word])
        elif word in {"}", ")", "]"}:
            if not stack or word != stack.pop():
                return len(words)
            if not stack:
                return index
    return len(words)


def lint_attributes(source: tuple[Token, ...]) -> tuple[LintAttribute, ...]:
    words = tuple(token.value for token in source)
    result: list[LintAttribute] = []
    for start, end in attribute_spans(words):
        inner = words[start + 1] == "!"
        name = start + 3 if inner else start + 2
        if name + 3 >= end or words[name] not in {"allow", "expect"} or words[name + 1] != "(":
            continue
        if words[end - 2:end] != (")", "]"):
            continue
        arguments: list[tuple[Token, ...]] = []
        begin = name + 2
        for index in range(begin, end - 2):
            if words[index] == ",":
                arguments.append(source[begin:index])
                begin = index + 1
        if begin < end - 2:
            arguments.append(source[begin:end - 2])
        names: set[tuple[str, ...]] = set()
        reason: tuple[Token, ...] = ()
        valid = True
        for argument in arguments:
            values = tuple(token.value for token in argument)
            if len(argument) == 3 and values[:2] == ("reason", "=") and argument[2].kind == "literal":
                if reason or not (values[2].startswith('"') or RAW_STRING.match(values[2])):
                    valid = False
                    break
                reason = argument
            elif argument and len(argument) % 2 and all(
                token.kind == "identifier" if index % 2 == 0 else token.value == "::"
                for index, token in enumerate(argument)
            ):
                names.add(values)
            else:
                valid = False
                break
        if valid:
            result.append(LintAttribute(start, end, words[name], inner, frozenset(names), reason))
    return tuple(result)


def prove_lint_removals(change: FileChange) -> LintAttributeProof:
    old, new = tokens(change.before), tokens(change.after)
    old_attributes, new_attributes = lint_attributes(old), lint_attributes(new)
    if not old_attributes:
        return LintAttributeProof()

    def units(source: tuple[Token, ...], attributes: tuple[LintAttribute, ...]) -> tuple[tuple[int, int], ...]:
        spans = {item.start: item.end for item in attributes}
        result: list[tuple[int, int]] = []
        cursor = 0
        while cursor < len(source):
            end = spans.get(cursor, cursor + 1)
            result.append((cursor, end))
            cursor = end
        return tuple(result)

    old_units, new_units = units(old, old_attributes), units(new, new_attributes)
    old_by_start = {item.start: item for item in old_attributes}
    new_by_start = {item.start: item for item in new_attributes}
    old_starts = tuple(start for start, _ in old_units)
    new_starts = tuple(start for start, _ in new_units)
    old_ends = tuple(end for _, end in old_units)
    new_ends = tuple(end for _, end in new_units)
    operations: list[tuple[str, int, int, int, int]] = []
    offset = 0
    for hunk in change.hunks:
        new_line = hunk.old_start + (not hunk.old_count) + offset
        ranges: list[tuple[int, int]] = []
        for text, line, count, starts, ends in (
            (change.before, hunk.old_start, len(hunk.removed), old_starts, old_ends),
            (change.after, new_line, len(hunk.added), new_starts, new_ends),
        ):
            parsed = tokenized_text(text)
            start = bisect_left(parsed.lines, line)
            end = bisect_left(parsed.lines, line + count)
            # A changed line inside a multiline attribute owns the whole unit.
            begin = bisect_left(ends, start + 1)
            ranges.append((begin, bisect_left(starts, end)))
        (old_begin, old_finish), (new_begin, new_finish) = ranges
        matcher = difflib.SequenceMatcher(
            None, tuple(old[start:end] for start, end in old_units[old_begin:old_finish]),
            tuple(new[start:end] for start, end in new_units[new_begin:new_finish]), autojunk=False,
        )
        operations.extend((operation, a + old_begin, b + old_begin, c + new_begin, d + new_begin)
                          for operation, a, b, c, d in matcher.get_opcodes())
        offset += len(hunk.added) - hunk.old_count
    before: list[TokenRewrite] = []
    after: list[TokenRewrite] = []
    pairs: list[tuple[LintAttribute, LintAttribute | None]] = []
    for operation, old_start, old_end, new_start, new_end in operations:
        if operation == "equal":
            continue
        removed = old_units[old_start:old_end]
        added = new_units[new_start:new_end]
        if any(span[0] not in old_by_start for span in removed) or any(
            span[0] not in new_by_start for span in added
        ):
            continue
        pending: list[TokenRewrite] = []
        matched: list[tuple[LintAttribute, LintAttribute | None]] = []
        cursor = 0
        for span in removed:
            previous = old_by_start[span[0]]
            replacement: LintAttribute | None = None
            if cursor < len(added):
                following = new_by_start[added[cursor][0]]
                if (previous.level == following.level and previous.inner == following.inner
                        and following.names <= previous.names and following.reason in {(), previous.reason}):
                    pending.append(TokenRewrite(following.start, following.end, ()))
                    replacement = following
                    cursor += 1
            matched.append((previous, replacement))
        if cursor == len(added):
            before.extend(TokenRewrite(start, end, ()) for start, end in removed)
            after.extend(pending)
            pairs.extend(matched)
    return LintAttributeProof(tuple(dict.fromkeys(before)), tuple(dict.fromkeys(after)), tuple(dict.fromkeys(pairs)))


def original_rewrites(source: tuple[Token, ...], omitted: tuple[TokenRewrite, ...],
                      rewrites: tuple[TokenRewrite, ...]) -> tuple[TokenRewrite, ...]:
    origins = [index for index in range(len(source)) if not any(item.start <= index < item.end for item in omitted)]
    return omitted + tuple(TokenRewrite(origins[item.start], origins[item.end - 1] + 1, item.replacement)
                           for item in rewrites)


def use_paths(words: tuple[str, ...], prefix: tuple[str, ...] = ()) -> tuple[tuple[str, ...], ...]:
    # Unknown syntax (aliases, globs, incomplete groups) has no proof.
    if not words:
        return ()
    if "{" in words:
        begin = words.index("{")
        end = closing(words, begin)
        if end != len(words) - 1 or (begin and words[begin - 1] != "::"):
            return ()
        stem = words[:begin - 1] if begin else ()
        if any(word != "::" and not IDENTIFIER.fullmatch(word) for word in stem):
            return ()
        head = prefix + tuple(word for word in stem if word != "::")
        result: list[tuple[str, ...]] = []
        cursor, depth = begin + 1, 0
        for index in range(begin + 1, end + 1):
            if words[index] == "{":
                depth += 1
            elif words[index] == "}":
                depth -= 1
            if (words[index] == "," and depth == 0) or index == end:
                part = words[cursor:index]
                if part:
                    paths = use_paths(part, head)
                    if not paths:
                        return ()
                    result.extend(paths)
                cursor = index + 1
        return tuple(result)
    if any((index % 2 == 0 and not IDENTIFIER.fullmatch(word)) or
           (index % 2 == 1 and word != "::") for index, word in enumerate(words)) or len(words) % 2 == 0:
        return ()
    return (prefix + words[::2],)


def use_items(words: tuple[str, ...]) -> tuple[UseItem, ...]:
    result: list[UseItem] = []
    attributes_by_end = {end: start for start, end in attribute_spans(words)}
    opening = {end: start for start, end in delimiter_ends(words).items()}
    semicolons = [len(words)] * len(words)
    following = len(words)
    for index in range(len(words) - 1, -1, -1):
        semicolons[index] = following
        if words[index] == ";":
            following = index
    for index, word in enumerate(words):
        if word != "use":
            continue
        end = semicolons[index]
        start = index
        if index and words[index - 1] == "pub":
            start = index - 1
        elif index and words[index - 1] == ")":
            begin = opening.get(index - 1, 0)
            if begin and words[begin - 1] == "pub":
                start = begin - 1
        attributed = start in attributes_by_end
        while start in attributes_by_end:
            start = attributes_by_end[start]
        result.append(UseItem(start, min(end + 1, len(words)), use_paths(words[index + 1:end]), attributed))
    return tuple(result)


def plain_use(words: tuple[str, ...], item: UseItem) -> bool:
    return bool(item.paths) and not item.attributed and words[item.start] == "use"


@dataclass(frozen=True)
class ImportScope:
    module: tuple[str, ...]
    block: int


@dataclass(frozen=True)
class ImportContext:
    scopes: tuple[ImportScope, ...]
    module_blocks: frozenset[int]
    block_ends: dict[int, int]
    configurations: tuple[frozenset[tuple[str, ...]], ...]


@dataclass(frozen=True)
class AddedImport:
    path: tuple[str, ...]
    module: tuple[str, ...]
    configuration: tuple[str, ...]


def delimiter_ends(words: tuple[str, ...]) -> dict[int, int]:
    stack: list[int] = []
    ends: dict[int, int] = {}
    pairs = {"}": "{", ")": "(", "]": "["}
    for index, word in enumerate(words):
        if word in {"{", "(", "["}:
            stack.append(index)
        elif word in pairs and stack and words[stack[-1]] == pairs[word]:
            ends[stack.pop()] = index
    return ends


def macro_spans(words: tuple[str, ...], ends: dict[int, int]) -> tuple[tuple[int, int], ...]:
    """The span of each macro token tree, from its `!` to the word after its closing delimiter.

    A `macro_rules!` tree starts after the macro's name.
    """
    spans: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        if word == "!":
            begin = index + 2 if index and words[index - 1] == "macro_rules" else index + 1
            if begin in ends:
                spans.append((index, ends[begin] + 1))
    return tuple(spans)


def closes_named_generics(words: tuple[str, ...], close: int, openers: dict[int, int]) -> bool:
    """Whether the `>` at close ends the generics of a turbofish (`f::<T>`) or of a `fn` or `struct` name.

    Any other `>` before a paren can be a comparison, as in `a > (b,)`.
    """
    depth = 0
    cursor = close
    while cursor >= 0:
        word = words[cursor]
        if word in {")", "]", "}"}:
            if cursor not in openers:
                return False
            cursor = openers[cursor] - 1
            continue
        if word in {"(", "[", "{", ";"}:
            return False
        depth += (word == ">") - (word == "<")
        if not depth:
            return words[cursor - 1:cursor] == ("::",) or (cursor >= 2 and words[cursor - 2] in {"fn", "struct"})
        cursor -= 1
    return False


def trailing_commas(words: tuple[str, ...], keywords: frozenset[str]) -> frozenset[int]:
    """The indexes of the commas that end a brace or bracket list, or a paren list after a name, `>`, `)` or `]`.

    A paren list there is the arguments of a call, a parameter list or a tuple struct pattern, where a trailing comma
    does not change the code, so a reformat that adds or drops one leaves the code the same. Any other paren list can be
    a tuple, where `(x,)` and `(x)` differ, so its comma stays. A comma inside a macro token tree or an attribute stays
    too: a macro arm or an attribute macro can tell `[1,]` from `[1]`.

    Some of those leads can come before a tuple, so they do not count: a lifetime or label (`&'a (u8,)`,
    `break 'a (1,)`), the `]` that ends an attribute (`#[cfg(x)] (a,) =>`), the `)` that ends a visibility
    (`pub(crate) (u8,)`), and a `>` that does not end named generics (`closes_named_generics`). A word of keywords,
    the keywords of the crate's edition, is not a name, so before 2024 `gen(1,)` is a call.
    """
    ends = delimiter_ends(words)
    openers = {close: opening for opening, close in ends.items()}
    attributes = attribute_spans(words)
    attribute_ends = {end for _, end in attributes}
    in_macros = {index for start, end in (*macro_spans(words, ends), *attributes) for index in range(start + 1, end)}

    def call_lead(index: int) -> bool:
        """Whether the word before the paren at index makes its list the arguments of a call or a parameter list."""
        lead = words[index - 1] if index else ""
        if lead == ">":
            return closes_named_generics(words, index - 1, openers)
        if lead == "]":
            return index not in attribute_ends
        if lead == ")":
            return words[openers[index - 1] - 1:openers[index - 1]] != ("pub",) if index - 1 in openers else True
        return (IDENTIFIER.fullmatch(lead) is not None and lead not in keywords
                and words[index - 2:index - 1] != ("'",))

    droppable: list[bool] = []
    dropped: set[int] = set()
    for index, word in enumerate(words):
        if word in {"(", "[", "{"}:
            droppable.append(word != "(" or call_lead(index))
        elif (word in {")", "]", "}"} and droppable and droppable.pop() and words[index - 1:index] == (",",)
              and index - 1 not in in_macros):
            dropped.add(index - 1)
    return frozenset(dropped)


def without_trailing_commas(source_tokens: tuple[Token, ...]) -> tuple[Token, ...]:
    """The tokens without the commas that `trailing_commas` finds, which rustfmt adds or drops when it joins lines. The
    check reads no manifest, so it reads the keywords before 2024: from 2024 on `gen (` is not Rust code."""
    dropped = trailing_commas(tuple(token.value for token in source_tokens), KEYWORDS_BEFORE_2024)
    return tuple(token for index, token in enumerate(source_tokens) if index not in dropped)


@dataclass(frozen=True)
class OutsideCrate:
    """The file is not a module file of the renamed file's crate, so its `crate`, `self` and `super` paths do not resolve."""


CrateModule = tuple[str, ...] | OutsideCrate


@dataclass(frozen=True)
class UnresolvedPath:
    """The path does not start at `crate`, `self` or `super`, or its file is outside the crate, so it names no known module."""


@dataclass(frozen=True)
class UseTrees:
    """The path before each segment and glob of the file's `use` items, and the `use` keyword of each segment and alias."""

    prefixes: Mapping[int, tuple[str, ...]]
    keywords: Mapping[int, int]


# The anchors that start a path the gate resolves from the file's own module.
PATH_ANCHORS = frozenset({"crate", "self", "super"})
# The words after which an item keyword can stand, besides an attribute, a `pub(..)` and an `extern` ABI string.
ITEM_LEADS = frozenset({"{", "}", ";", "pub", "unsafe", "async", "const", "extern", "default"})


def item_starts(words: tuple[str, ...]) -> frozenset[int]:
    """The word indexes where an item keyword can stand: the first word, and the word after a brace, a semicolon, an
    attribute, a visibility, an item qualifier or an ABI string."""
    leads = {index for index, word in enumerate(words) if word in ITEM_LEADS and words[index - 1:index] != ("'",)}
    leads.update(end - 1 for _, end in attribute_spans(words))
    leads.update(closing(words, index + 1) for index, word in enumerate(words[:-1])
                 if word == "pub" and words[index + 1] == "(")
    leads.update(index for index in range(1, len(words)) if words[index - 1] == "extern" and words[index].startswith('"'))
    return frozenset({0} | {lead + 1 for lead in leads})


def crate_module(path: str, root: PurePosixPath) -> CrateModule:
    """The module path of a Rust file under the crate root folder: `()` for the crate root file."""
    file = PurePosixPath(path)
    if file.suffix != ".rs" or not file.is_relative_to(root):
        return OutsideCrate()
    folders = file.relative_to(root).parent.parts
    if folders[:1] == ("bin",):
        # Each file in `bin` is the root of a crate of its own.
        return OutsideCrate()
    if not folders and file.stem in {"lib", "main"}:
        return ()
    return folders if file.stem == "mod" else (*folders, file.stem)


def resolved_module(prefix: tuple[str, ...], module: CrateModule) -> tuple[str, ...] | UnresolvedPath:
    """The module that a path starting at `crate`, `self` or `super` names from module."""
    if isinstance(module, OutsideCrate) or not prefix or prefix[0] not in PATH_ANCHORS:
        return UnresolvedPath()
    rest = prefix[1:]
    base = () if prefix[0] == "crate" else module
    if prefix[0] == "super":
        rest = prefix
        while rest and rest[0] == "super":
            if not base:
                return UnresolvedPath()
            base, rest = base[:-1], rest[1:]
    return UnresolvedPath() if PATH_ANCHORS & set(rest) else base + rest


def code_item_starts(words: tuple[str, ...]) -> frozenset[int]:
    """The item starts outside every macro token tree: a macro reads the words in its tree as tokens, not as items."""
    in_macros = {index for start, end in macro_spans(words, delimiter_ends(words))
                 for index in range(start + 1, end)}
    return item_starts(words) - in_macros


def use_trees(words: tuple[str, ...], starts: frozenset[int]) -> UseTrees:
    prefixes: dict[int, tuple[str, ...]] = {}
    keywords: dict[int, int] = {}
    for keyword, word in enumerate(words):
        if word != "use" or keyword not in starts:
            continue
        groups: list[tuple[str, ...]] = [()]
        path: list[str] = []
        cursor = keyword + 1
        while cursor < len(words) and words[cursor] != ";":
            part = words[cursor]
            if part == "{":
                groups.append(groups[-1] + tuple(path))
                path = []
            elif part in {"}", ","}:
                if part == "}" and len(groups) > 1:
                    _ = groups.pop()
                path = []
            elif part == "as":
                cursor += 1
                keywords[cursor] = keyword
            elif part != "::":
                prefixes[cursor] = groups[-1] + tuple(path)
                keywords[cursor] = keyword
                path.append(part)
            cursor += 1
    return UseTrees(prefixes, keywords)


def sorted_use_groups(words: tuple[str, ...]) -> tuple[str, ...]:
    """The words of a use tree with the entries of each brace group in sorted order, no word dropped."""
    result: list[str] = []
    cursor = 0
    while cursor < len(words):
        if words[cursor] != "{":
            result.append(words[cursor])
            cursor += 1
            continue
        end = closing(words, cursor)
        entries: list[tuple[str, ...]] = []
        start = inner = cursor + 1
        while inner < end:
            if words[inner] == "{":
                inner = closing(words, inner)
            elif words[inner] == ",":
                entries.append(sorted_use_groups(words[start:inner]))
                start = inner + 1
            inner += 1
        entries.append(sorted_use_groups(words[start:end]))
        result.append("{")
        for position, entry in enumerate(sorted(entries)):
            result.extend((",", *entry) if position else entry)
        result.append("}")
        cursor = end + 1
    return tuple(result)


@dataclass(frozen=True)
class ModuleItem:
    """A `use` item or `mod <name>;` item: its first word (attributes and visibility included), its keyword and its `;`."""

    start: int
    keyword: int
    end: int


def module_items(words: tuple[str, ...]) -> tuple[ModuleItem, ...]:
    """Each `use` item and `mod <name>;` item, in order.

    An item with a `macro_use` attribute is left out: its place decides where its macros can be used.
    """
    starts = code_item_starts(words)
    attributes = {end: start for start, end in attribute_spans(words)}
    visibilities = {closing(words, index + 1) + 1: index for index, word in enumerate(words[:-1])
                    if word == "pub" and words[index + 1] == "("}
    items: list[ModuleItem] = []
    for keyword, word in enumerate(words):
        if keyword not in starts:
            continue
        if word == "use":
            end = next((index for index in range(keyword, len(words)) if words[index] == ";"), len(words))
        elif word == "mod" and words[keyword + 2:keyword + 3] == (";",):
            end = keyword + 2
        else:
            continue
        start = visibilities.get(keyword, keyword - 1 if words[keyword - 1:keyword] == ("pub",) else keyword)
        while start in attributes:
            start = attributes[start]
        if end < len(words) and "macro_use" not in words[start:keyword]:
            items.append(ModuleItem(start, keyword, end))
    return tuple(items)


def module_runs(words: tuple[str, ...]) -> tuple[tuple[ModuleItem, ...], ...]:
    """The runs of adjacent module items, in order."""
    runs: list[list[ModuleItem]] = []
    for item in module_items(words):
        if runs and runs[-1][-1].end + 1 == item.start:
            runs[-1].append(item)
        else:
            runs.append([item])
    return tuple(tuple(run) for run in runs)


def comment_gaps(text: str, spans: tuple[TokenSpan, ...],
                 renamed: Callable[[str], str]) -> tuple[tuple[str, ...], ...]:
    """The stripped lines of the comments before each code token, then after the last one, each passed through renamed."""
    ends = (0, *(span.end for span in spans))
    starts = (*(span.start for span in spans), len(text))
    return tuple(tuple(renamed(line.strip()) for line in text[end:start].splitlines() if line.strip())
                 for end, start in zip(ends, starts, strict=True))


def outer_doc(line: str) -> bool:
    """Whether the stripped comment line starts an outer doc comment, an attribute of the item after it."""
    return ((line.startswith("///") and not line.startswith("////"))
            or (line.startswith("/**") and not line.startswith(("/***", "/**/"))))


# A comment line, keyed by the count of the words of its piece before it.
CommentLine = tuple[int, str]
# The code words of one place in the code, and the comment lines before them and inside them.
CodePiece = tuple[tuple[str, ...], tuple[CommentLine, ...]]


def run_items(words: tuple[str, ...], gaps: tuple[tuple[str, ...], ...], run: tuple[ModuleItem, ...],
              dropped: frozenset[int]) -> tuple[tuple[str, ...], tuple[CodePiece, ...]]:
    """The comment lines before a run of module items that stay in place, and each item of the run with its comments.

    An item has its words without the dropped commas, the entries of a `use` tree sorted, and the lines of the gap
    before its first word and of the gaps inside it, each keyed by the count of the item's words before it. Only the
    use tree sorts: an attribute, the visibility and a `mod` item keep their words. The gap before the run stays in
    place up to its first outer doc comment, as rustfmt leaves it there.
    """
    first = gaps[run[0].start]
    doc = next((index for index, line in enumerate(first) if outer_doc(line)), len(first))

    def kept(start: int, end: int) -> tuple[str, ...]:
        return tuple(words[index] for index in range(start, end) if index not in dropped)

    items: list[CodePiece] = []
    for item in run:
        tree = kept(item.keyword + 1, item.end)
        code = (*kept(item.start, item.keyword + 1), *(sorted_use_groups(tree) if words[item.keyword] == "use" else tree),
                words[item.end])
        lines: list[CommentLine] = []
        key = 0
        for gap in range(item.start, item.end + 1):
            lines.extend((key, line) for line in (first[doc:] if gap == run[0].start else gaps[gap]))
            key += gap not in dropped
        items.append((code, tuple(lines)))
    return first[:doc], tuple(items)


def canonical_code(words: tuple[str, ...], gaps: tuple[tuple[str, ...], ...],
                   keywords: frozenset[str] = KEYWORDS_BEFORE_2024) -> tuple[CodePiece, ...]:
    """The code in canonical order: each word with the comment lines before it, and each item of a run of adjacent
    module items with its comment lines, the run's items sorted.

    A trailing comma drops (`trailing_commas`, which reads keywords, the keywords of the crate's edition; a caller with
    no manifest gets those before 2024), and its comment lines go to the next piece; no other word drops. Use tree
    entries and the items of a run sort, and a comment moves with its item. None of these changes the code, and rustfmt
    makes them when a rename changes a name's sort order or line length. A comment line keeps its place among the words
    of its piece, so a comment that moves to another place in the code changes the pieces.
    """
    dropped = trailing_commas(words, keywords)
    pieces: list[CodePiece] = []
    lines: list[str] = []

    def place(index: int) -> None:
        """Add word index, or after the last word the comment lines left, as the next piece."""
        lines.extend(gaps[index])
        if (index not in dropped and index < len(words)) or (index == len(words) and lines):
            pieces.append((words[index:index + 1], tuple((0, line) for line in lines)))
            lines.clear()

    cursor = 0
    for run in module_runs(words):
        for index in range(cursor, run[0].start):
            place(index)
        before, items = run_items(words, gaps, run, dropped)
        if lines or before:
            pieces.append(((), tuple((0, line) for line in (*lines, *before))))
            lines.clear()
        pieces.extend(sorted(items))
        cursor = run[-1].end + 1
    for index in range(cursor, len(gaps)):
        place(index)
    return tuple(pieces)


def cfg_attribute(words: tuple[str, ...]) -> bool:
    return (words[:4] == ("#", "[", "cfg", "(") and words[-2:] == (")", "]")
            and closing(words, 1) == len(words) - 1 and closing(words, 3) == len(words) - 2)


def import_context(words: tuple[str, ...]) -> ImportContext:
    ends = delimiter_ends(words)
    modules = {index + 2: words[index + 1] for index, word in enumerate(words[:-2])
               if word == "mod" and words[index + 2] == "{"}
    scopes: list[ImportScope] = []
    blocks: list[int] = []
    module_names: list[str] = []
    module_counts: dict[tuple[tuple[str, ...], str], int] = {}
    for index, word in enumerate(words):
        scopes.append(ImportScope(tuple(module_names), blocks[-1] if blocks else -1))
        if word == "{":
            blocks.append(index)
            if index in modules:
                key = (tuple(module_names), modules[index])
                module_counts[key] = module_counts.get(key, 0) + 1
                module_names.append(f"{modules[index]}@{module_counts[key]}")
        elif word == "}" and blocks:
            begin = blocks.pop()
            if begin in modules:
                _ = module_names.pop()
    # Sweep cfg-bearing item extents once instead of searching all items per site.
    starts: dict[int, list[tuple[str, ...]]] = {}
    finishes: dict[int, list[tuple[str, ...]]] = {}
    owners = attribute_owners(words)
    for begin, end in attribute_spans(words):
        attribute = words[begin:end]
        owner = owners.get(begin)
        if not cfg_attribute(attribute) or owner is None or words[owner] not in {
            "fn", "impl", "mod", "struct", "enum", "const", "static",
        }:
            continue
        cursor = owner + 1
        while cursor < len(words) and words[cursor] not in {"{", ";"}:
            if words[cursor] in {"(", "["}:
                cursor = ends.get(cursor, cursor)
            cursor += 1
        finish = ends.get(cursor, cursor) + 1
        starts.setdefault(owner, []).append(attribute)
        finishes.setdefault(finish, []).append(attribute)
    active: dict[tuple[str, ...], int] = {}
    configurations: list[frozenset[tuple[str, ...]]] = []
    for index in range(len(words)):
        for attribute in finishes.get(index, ()):
            active[attribute] -= 1
            if not active[attribute]:
                del active[attribute]
        for attribute in starts.get(index, ()):
            active[attribute] = active.get(attribute, 0) + 1
        configurations.append(frozenset(active))
    return ImportContext(tuple(scopes), frozenset(modules), ends, tuple(configurations))


def added_use_configuration(words: tuple[str, ...], item: UseItem) -> tuple[tuple[str, ...], ...]:
    # Zero entries means refused; one empty entry means unconditional.
    if plain_use(words, item):
        return ((),)
    if not item.paths or words[item.start] != "#":
        return ()
    end = closing(words, item.start + 1) + 1 if words[item.start + 1] == "[" else item.start
    attribute = words[item.start:end]
    return (attribute,) if cfg_attribute(attribute) and end < item.end and words[end] == "use" else ()


def path_only_roots(before: tuple[str, ...], after: tuple[str, ...]) -> frozenset[str]:
    candidates = {word for index, word in enumerate(before[:-1]) if before[index + 1] == "::"}
    invalid: set[str] = set()
    for words in (before, after):
        for index, word in enumerate(words):
            if word in candidates and (index + 1 == len(words) or words[index + 1] != "::"
                                      or (index and words[index - 1] == "::")):
                invalid.add(word)
    return frozenset(candidates - invalid)


def scoped_import_sites(source: tuple[Token, ...], uses: tuple[UseItem, ...],
                        context: ImportContext, added: tuple[AddedImport, ...],
                        roots: frozenset[str]) -> tuple[TokenRewrite, ...]:
    words = tuple(token.value for token in source)
    use_indices = {i for item in uses for i in range(item.start, item.end)}
    aliases: dict[tuple[tuple[str, ...], str], set[tuple[str, ...]]] = {}
    blocked_qualifiers: set[tuple[tuple[str, ...], str]] = set()
    block_globs: list[tuple[int, int]] = []
    for item in uses:
        scope = context.scopes[item.start]
        module_use = scope.block == -1 or scope.block in context.module_blocks
        if module_use and plain_use(words, item):
            for path in item.paths:
                aliases.setdefault((scope.module, path[-1]), set()).add(path)
        elif not plain_use(words, item) and "*" not in words[item.start:item.end] and not any(
            candidate.module == scope.module and candidate.path in item.paths and candidate.configuration
            and words[item.start:item.start + len(candidate.configuration)] == candidate.configuration
            for candidate in added
        ):
            blocked_qualifiers.update((scope.module, word) for word in words[item.start:item.end])
        if not module_use:
            if "*" in words[item.start:item.end]:
                block_globs.append((scope.block, context.block_ends.get(scope.block, len(words))))
            # A block-level use can shadow a module qualifier.
            blocked_qualifiers.update((scope.module, path[-1]) for path in item.paths)
    blocked_sites: set[int] = set()
    previous_end = 0
    for begin, end in sorted(block_globs):
        blocked_sites.update(range(max(begin, previous_end), end))
        previous_end = max(previous_end, end)
    by_name: dict[str, list[AddedImport]] = {}
    for item in added:
        by_name.setdefault(item.path[-1], []).append(item)
    replacements: list[TokenRewrite] = []
    cursor = 0
    while cursor < len(words):
        if cursor in use_indices or not IDENTIFIER.fullmatch(words[cursor]):
            cursor += 1
            continue
        end = cursor + 1
        while end + 1 < len(words) and words[end] == "::" and IDENTIFIER.fullmatch(words[end + 1]):
            end += 2
        segments = words[cursor:end:2]
        scope = context.scopes[cursor].module
        if len(segments) > 1 and segments[-1] in by_name:
            resolved = segments
            key = (scope, segments[0])
            options = aliases.get(key, set())
            if segments[0] not in {"crate", "self", "super", "std", "core", "alloc"}:
                resolved = next(iter(options)) + segments[1:] if len(options) == 1 else (
                    segments if segments[0] in roots and not options else ()
                )
            candidates = [item for item in by_name[segments[-1]] if item.path == resolved
                          and item.module == scope and (not item.configuration or
                              item.configuration in context.configurations[cursor])]
            if candidates and key not in blocked_qualifiers and cursor not in blocked_sites:
                replacements.append(TokenRewrite(cursor, end, (source[end - 1],)))
        cursor = end
    return tuple(replacements)


def removed_name_stays_bound(words: tuple[str, ...], uses: tuple[UseItem, ...], use_indices: set[int],
                             path: tuple[str, ...], children: set[tuple[str, ...]]) -> bool:
    # A bare word resolves through local bindings, same-module items, globs or the prelude. With no
    # glob and no prelude name, it names the same thing after the import goes, or the build fails.
    # A snake-case path with a child imported in the same module is a module, never a pattern or value.
    # Macro token trees are refused: a macro can take the word into another namespace.
    name = path[-1]
    sites = [index for index, word in enumerate(words) if word == name and index not in use_indices]
    if not sites:
        return True
    if (name in PRELUDE_NAMES or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or not any(child[:len(path)] == path and len(child) > len(path) for child in children)
            or any("*" in words[item.start:item.end] for item in uses)):
        return False
    spans = attribute_spans(words) + macro_spans(words, delimiter_ends(words))
    hidden = {index for begin, end in spans for index in range(begin, end)}
    return all(index not in hidden and (index == 0 or words[index - 1] != "::")
               and (index + 1 == len(words) or words[index + 1] not in {"::", "!"}) for index in sites)


def prove_imports(change: FileChange) -> ImportProof:
    old, new = tokens(change.before), tokens(change.after)
    before, after = tuple(t.value for t in old), tuple(t.value for t in new)
    old_uses, new_uses = use_items(before), use_items(after)
    old_context, new_context = import_context(before), import_context(after)
    remaining = list(new_uses)
    removed_items: list[UseItem] = []
    unchanged_items: list[UseItem] = []
    for item in old_uses:
        match = next((other for other in remaining if before[item.start:item.end] == after[other.start:other.end]
                      and old_context.scopes[item.start].module == new_context.scopes[other.start].module), None)
        if match is None:
            removed_items.append(item)
        else:
            remaining.remove(match)
            unchanged_items.append(item)
    added_items = tuple(remaining)
    if not removed_items and not added_items:
        return ImportProof()
    if any(not plain_use(before, item) for item in removed_items):
        return ImportProof()
    candidates: list[AddedImport] = []
    for item in added_items:
        scope = new_context.scopes[item.start]
        configurations = added_use_configuration(after, item)
        if not configurations or (scope.block != -1 and scope.block not in new_context.module_blocks):
            return ImportProof()
        candidates.extend(AddedImport(path, scope.module, configurations[0]) for path in item.paths)
    if any(path[-1] in before[unchanged.start:unchanged.end] for item in added_items for path in item.paths
           for unchanged in unchanged_items):
        return ImportProof()
    old_paths = {path for item in old_uses for path in item.paths}
    new_paths = {path for item in new_uses for path in item.paths}
    added = new_paths - old_paths
    candidates = [item for item in candidates if item.path in added]
    replaced_paths = {path for item in removed_items for path in item.paths}
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", path[-1]) for item in added_items for path in item.paths
           if path not in replaced_paths):
        return ImportProof()
    if len({path[-1] for path in added}) != len(added):
        return ImportProof()
    old_use_indices = {i for item in old_uses for i in range(item.start, item.end)}
    new_use_indices = {i for item in new_uses for i in range(item.start, item.end)}
    # A removed path stays imported only where a module-level plain use in its module still names it.
    kept: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for item in new_uses:
        scope = new_context.scopes[item.start]
        if plain_use(after, item) and (scope.block == -1 or scope.block in new_context.module_blocks):
            kept.update((scope.module, path) for path in item.paths)
    for item in removed_items:
        # A block can redefine a path, so a removed block-level use keeps the strict rule: no bare word left.
        scope = old_context.scopes[item.start]
        module_level = scope.block == -1 or scope.block in old_context.module_blocks
        children = {other.path for other in candidates if module_level and other.module == scope.module}
        if any(not (module_level and (scope.module, path) in kept)
               and not removed_name_stays_bound(after, new_uses, new_use_indices, path, children) for path in item.paths):
            return ImportProof()
    for path in added:
        name = path[-1]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            return ImportProof()
        if any(name in before[item.start:item.end] for item in old_uses):
            return ImportProof()
        if any(word == name and i not in old_use_indices and
               (i == 0 or before[i - 1] != "::") and
               (i + 1 == len(before) or before[i + 1] != "::") for i, word in enumerate(before)):
            return ImportProof()
    roots = path_only_roots(before, after)
    old_sites = scoped_import_sites(old, old_uses, old_context, tuple(candidates), roots)
    new_sites = scoped_import_sites(new, new_uses, new_context, tuple(candidates), roots)
    added_names = {path[-1] for path in added}
    for index, word in enumerate(after):
        if word not in added_names or index in new_use_indices:
            continue
        if (index and after[index - 1] == "::") or (index + 1 < len(after) and after[index + 1] == "::"):
            continue
        if not any(item.path[-1] == word and item.module == new_context.scopes[index].module
                   and (not item.configuration or item.configuration in new_context.configurations[index])
                   for item in candidates):
            return ImportProof()
    used = {tuple(before[site.start:site.end:2])[-1] for site in old_sites}
    if used != {path[-1] for path in added}:
        return ImportProof()
    return ImportProof(tuple(TokenRewrite(item.start, item.end, ()) for item in removed_items) + old_sites,
                       tuple(TokenRewrite(item.start, item.end, ()) for item in added_items) + new_sites)


def function_bodies(words: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        if word == "fn":
            begin = next((i for i in range(index + 1, len(words)) if words[i] in {"{", ";"}), len(words))
            if begin < len(words) and words[begin] == "{":
                result.append((index, closing(words, begin) + 1))
    return tuple(result)


def enum_bodies(words: tuple[str, ...]) -> tuple[EnumBody, ...]:
    result: list[EnumBody] = []
    attributes = decorated_items(words)
    for index, word in enumerate(words[:-1]):
        if word != "enum":
            continue
        begin = next((i for i in range(index + 2, len(words)) if words[i] in {"{", ";"}), len(words))
        if begin == len(words) or words[begin] != "{":
            continue
        end = closing(words, begin)
        variants: list[int] = []
        attributed_variants: set[int] = set()
        spans = dict(attribute_spans(words))
        cursor = begin + 1
        while cursor < end:
            attributed = cursor in spans
            while cursor in spans:
                cursor = spans[cursor]
            if cursor < end and IDENTIFIER.fullmatch(words[cursor]):
                variants.append(cursor)
                if attributed:
                    attributed_variants.add(cursor)
            while cursor < end and words[cursor] != ",":
                if words[cursor] in {"{", "(", "["}:
                    cursor = closing(words, cursor)
                cursor += 1
            cursor += 1
        safe = True
        attached = attributes.get(index, ())
        for start, finish in attribute_spans(attached):
            attribute = attached[start:finish]
            if len(attribute) < 6 or attribute[2:4] != ("derive", "(") or any(
                name not in STD_DERIVES and name != "," for name in attribute[4:-2]
            ):
                safe = False
        result.append(EnumBody(words[index + 1], begin, end, tuple(variants), frozenset(attributed_variants), safe))
    return tuple(result)


def impl_bodies(words: tuple[str, ...], enum: str) -> tuple[tuple[int, int], ...]:
    bodies: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        if word != "impl":
            continue
        begin = next((i for i in range(index + 1, len(words)) if words[i] in {"{", ";"}), len(words))
        header = words[index + 1:begin]
        target = header[header.index("for") + 1:] if "for" in header else header
        if target and target[0] == "<":
            depth, cursor = 1, 1
            while cursor < len(target) and depth:
                depth += (target[cursor] == "<") - (target[cursor] == ">")
                cursor += 1
            target = target[cursor:]
        finish = next((i for i, part in enumerate(target) if part in {"<", "where"}), len(target))
        target = target[:finish]
        if target and target[-1] == enum and all(
            IDENTIFIER.fullmatch(part) or part == "::" for part in target
        ) and begin < len(words) and words[begin] == "{":
            bodies.append((begin, closing(words, begin)))
    return tuple(bodies)


def variant_sites(words: tuple[str, ...], enum: str, name: str) -> frozenset[int]:
    sites = {i for body in enum_bodies(words) if body.name == enum for i in body.variants if words[i] == name}
    impls = impl_bodies(words, enum)
    for index, word in enumerate(words):
        if word != name or index < 2 or words[index - 1] != "::":
            continue
        qualifier = words[index - 2]
        if (qualifier == enum and (index < 3 or words[index - 3] != "::")) or (
            qualifier == "Self" and any(start < index < end for start, end in impls)
        ):
            sites.add(index)
    for item in use_items(words):
        # Flatten each leaf separately, retaining its original token index.
        prefixes: list[tuple[str, ...]] = [()]
        path: list[str] = []
        for index in range(item.start, item.end):
            word = words[index]
            if word in {"use", "pub", "::"}:
                continue
            if word == "{":
                prefixes.append(prefixes[-1] + tuple(path))
                path = []
            elif word in {",", "}", ";"}:
                path = []
                if word == "}" and len(prefixes) > 1:
                    _ = prefixes.pop()
            elif IDENTIFIER.fullmatch(word):
                path.append(word)
                if word == name and (prefixes[-1] + tuple(path))[-2:] == (enum, name):
                    sites.add(index)
    imported = [path for item in use_items(words) for path in item.paths if path[-1] == name]
    if len(imported) == 1 and imported[0][-2:] == (enum, name):
        # A bare imported variant is safe only without a competing binding.
        definitions = {"let", "mut", "fn", "const", "static", "struct", "enum", "type", "mod"}
        if not any(index and words[index - 1] in definitions for index, word in enumerate(words) if word == name):
            sites.update(index for index, word in enumerate(words) if word == name and
                         (index == 0 or words[index - 1] not in {"::", "."}) and
                         (index + 1 == len(words) or words[index + 1] not in {"::", ":"}))
    return frozenset(sites)


def shorthand_field(words: tuple[str, ...], index: int) -> bool:
    if not index or index + 1 == len(words) or words[index - 1] not in {"{", ","} or words[index + 1] not in {"}", ","}:
        return False
    for begin in range(index - 1, -1, -1):
        if words[begin] == "{" and closing(words, begin) >= index:
            if not begin:
                return False
            boundary = next((i + 1 for i in range(begin - 1, -1, -1) if words[i] in {";", "{", "}"}), 0)
            header = words[boundary:begin]
            nested = header.count("(") != header.count(")") or header.count("[") != header.count("]")
            if not nested and set(header) & {"fn", "if", "while", "match", "for"}:
                return False
            owner = words[begin - 1]
            return owner == ">" or bool(IDENTIFIER.fullmatch(owner) and owner not in {
                "else", "loop", "unsafe", "async", "try", "const", "return", "break",
            })
    return False


def local_binding_is_safe(old: tuple[Token, ...], new: tuple[Token, ...], index: int,
                          start: int, end: int) -> bool:
    words = tuple(token.value for token in old)
    old_name, new_name = words[index], new[index].value
    let = index - 2 if words[index - 1] == "mut" else index - 1
    if not let or words[let - 1] not in {"{", ";", "}"}:
        return False
    blocks = [(begin, closing(words, begin)) for begin in range(start, let) if words[begin] == "{"
              and closing(words, begin) > let]
    if not blocks:
        return False
    block_start, block_end = min(blocks, key=lambda block: block[1] - block[0])
    cursor = let + 1
    while cursor < block_end and words[cursor] != ";":
        if words[cursor] in {"(", "[", "{"}:
            cursor = closing(words, cursor)
        cursor += 1
    if cursor >= block_end:
        return False
    if new_name in words[start:end] or any(token.value == old_name for token in new[start:end]):
        return False
    if "fn" in words[start + 1:end] or "macro_rules" in words[start:end]:
        return False
    capture = re.compile(r"\{" + re.escape(old_name) + r"(?=[}:])")
    if any(token.kind == "literal" and capture.search(token.value)
           for token in (*old[start:end], *new[start:end])):
        return False
    for occurrence in range(start, end):
        if words[occurrence] != old_name or occurrence == index:
            continue
        if not cursor < occurrence < block_end or occurrence <= block_start:
            return False
        binding = words[occurrence - 1] == "let" or words[occurrence - 2:occurrence] == ("let", "mut")
        if not binding and (words[occurrence - 1] in {".", "::"} or words[occurrence + 1] in {"::", "!", ":"}):
            return False
    return True


@dataclass(frozen=True)
class ParameterBody:
    start: int
    end: int


@dataclass(frozen=True)
class BodylessFunction:
    pass


@dataclass(frozen=True)
class FunctionParameters:
    function: int
    start: int
    end: int
    parameters: tuple[tuple[int, int], ...]
    body: ParameterBody | BodylessFunction


def function_parameters(source: tuple[Token, ...]) -> tuple[FunctionParameters, ...]:
    """Read named function parameters outside attributes and macro token trees."""
    words = tuple(token.value for token in source)
    ends = delimiter_ends(words)
    excluded = (*attribute_spans(words), *macro_spans(words, ends))
    result: list[FunctionParameters] = []
    for index, word in enumerate(words[:-1]):
        if word != "fn" or not IDENTIFIER.fullmatch(words[index + 1]) or any(
            start <= index < end for start, end in excluded
        ):
            continue
        cursor = index + 2
        if cursor < len(words) and words[cursor] == "<":
            depth = 1
            cursor += 1
            while cursor < len(words) and depth:
                if words[cursor] in {"{", ";"}:
                    break
                depth += (words[cursor] == "<") - (words[cursor] == ">")
                cursor += 1
            if depth:
                continue
        if cursor not in ends or words[cursor] != "(":
            continue
        start, end = cursor, ends[cursor]
        parameters: list[tuple[int, int]] = []
        begin, cursor, depth = start + 1, start + 1, 0
        while cursor < end:
            if cursor in ends:
                cursor = ends[cursor] + 1
                continue
            depth += (words[cursor] == "<") - (words[cursor] == ">")
            if words[cursor] == "," and depth == 0:
                parameters.append((begin, cursor))
                begin = cursor + 1
            cursor += 1
        if depth:
            continue
        if begin < end:
            parameters.append((begin, end))
        cursor, depth = end + 1, 0
        while cursor < len(words):
            word = words[cursor]
            if not depth and word in {"{", ";", "}"}:
                break
            depth += (word == "<") - (word == ">")
            if depth < 0:
                break
            cursor = ends[cursor] + 1 if cursor in ends else cursor + 1
        if cursor == len(words) or depth or words[cursor] == "}":
            continue
        if words[cursor] == ";":
            body = BodylessFunction()
        elif cursor in ends:
            body = ParameterBody(cursor + 1, ends[cursor])
        else:
            continue
        result.append(FunctionParameters(index, start, end, tuple(parameters), body))
    return tuple(result)


def prove_parameter_erasures(old: tuple[Token, ...], new: tuple[Token, ...]) -> tuple[TokenRewrite, ...]:
    if len(old) != len(new):
        return ()
    following = {item.function: item for item in function_parameters(new)}
    result: list[TokenRewrite] = []
    for item in function_parameters(old):
        revised = following.get(item.function)
        if revised is None or (item.start, item.end, item.parameters) != (
            revised.start, revised.end, revised.parameters
        ) or old[item.function:item.start] != new[item.function:revised.start]:
            continue
        for start, end in item.parameters:
            name = old[start].value
            if not re.fullmatch(r"_[A-Za-z_0-9]+", name) or new[start].value != "_" or (
                start + 1 >= end or old[start + 1].value != ":" or old[start + 1:end] != new[start + 1:end]
            ):
                continue
            capture = re.compile(r"\{" + re.escape(name) + r"(?=[}:])")
            bodies = tuple(source[body.start:body.end] for source, body in (
                (old, item.body), (new, revised.body),
            ) if isinstance(body, ParameterBody))
            if any((token.kind == "identifier" and token.value.removeprefix("r#") == name) or (
                       token.kind == "literal" and (
                           capture.search(token.value) or ("\\" in token.value and "{" in token.value)
                       )
                   )
                   for body in bodies for token in body):
                continue
            result.append(TokenRewrite(start, start + 1, (new[start],)))
    return tuple(result)


def erase_proven_parameters(change: FileChange) -> FileChange:
    """Remove only proved name erasures before the existing semantic checks."""
    erasures = prove_parameter_erasures(tokens(change.before), tokens(change.after))
    if not erasures:
        return change
    try:
        spans = token_spans(change.before)
    except UnreadableTokenSpans:
        return change
    if not spans:
        return change
    before = change.before
    for rewrite in reversed(erasures):
        span = spans[rewrite.start]
        before = before[:span.start] + "_" + before[span.end:]
    patch = "\n".join(difflib.unified_diff(before.splitlines(), change.after.splitlines(), n=0, lineterm=""))
    return FileChange(change.status, change.old_path, change.path, before, change.after, parse_hunks(patch))


def enum_name_is_unique(originals: dict[str, str], owned: dict[str, str], enum: str) -> bool:
    kinds = {"struct", "enum", "union", "type", "trait", "const", "static", "mod", "fn"}
    for revised in (False, True):
        definitions: list[str] = []
        for path, source in originals.items():
            words = tuple(token.value for token in tokens(owned.get(path, source) if revised else source))
            for index, word in enumerate(words[:-1]):
                name = index + 2 if word == "static" and words[index + 1] == "mut" else index + 1
                if word in kinds and name < len(words) and words[name] == enum:
                    definitions.append(word)
        if definitions != ["enum"]:
            return False
    return True


def enum_sites_have_import(words: tuple[str, ...], enum: str, names: tuple[str, ...]) -> bool:
    if any(body.name == enum for body in enum_bodies(words)) or not any(
        variant_sites(words, enum, name) for name in names
    ):
        return True
    uses = use_items(words)
    return not any("*" in words[item.start:item.end] for item in uses) and any(
        not item.attributed and any(path[0] in {"crate", "self", "super"} and path[-1] == enum for path in item.paths)
        for item in uses
    )


def prove_renames(changes: tuple[FileChange, ...], sources: RevisionTree,
                  imports: dict[str, ImportProof]) -> dict[str, tuple[TokenRewrite, ...]]:
    empty: dict[str, tuple[TokenRewrite, ...]] = {}
    crates = {sources.crate(change.path) for change in changes}
    if len(changes) > 10 or len(crates) != 1:
        return empty
    crate = next(iter(crates))
    pairs: dict[str, tuple[tuple[Token, ...], tuple[Token, ...]]] = {}
    locals_found: list[FunctionRename] = []
    variants: set[EnumVariantRename] = set()
    globals_found: set[CrateRename] = set()
    for change in changes:
        proof = imports[change.path]
        old = rewrite_tokens(tokens(change.before), proof.before)
        new = rewrite_tokens(tokens(change.after), proof.after)
        pairs[change.path] = old, new
        if len(old) != len(new):
            return empty
        words = tuple(t.value for t in old)
        for index, (left, right) in enumerate(zip(old, new)):
            if left == right:
                continue
            if left.kind != "identifier" or right.kind != "identifier":
                return empty
            variant = next((body for body in enum_bodies(words) if index in body.variants), None)
            if variant is not None:
                if not variant.safe_attributes or index in variant.attributed_variants:
                    return empty
                variants.add(EnumVariantRename(variant.name, left.value, right.value))
            elif index and (words[index - 1] == "let" or
                            (index > 1 and words[index - 2:index] == ("let", "mut"))):
                bodies = [(start, end) for start, end in function_bodies(words) if start < index < end]
                if not bodies:
                    return empty
                start, end = min(bodies, key=lambda body: body[1] - body[0])
                rename = FunctionRename(change.path, start, end, left.value, right.value)
                if rename not in locals_found:
                    if not local_binding_is_safe(old, new, index, start, end):
                        return empty
                    locals_found.append(rename)
            elif index and words[index - 1] in {"fn", "const", "static", "struct", "enum", "type", "trait"}:
                globals_found.add(CrateRename(left.value, right.value))
    if len({rename.old for rename in globals_found}) != len(globals_found) or len(
        {rename.new for rename in globals_found}
    ) != len(globals_found):
        return empty
    originals = {path: sources.text(path) for path in sources.paths
                 if (globals_found or (variants and path.endswith(".rs"))) and sources.crate(path) == crate}
    owned = {change.path: change.after for change in changes}
    for rename in globals_found:
        if any(re.search(r"\b" + re.escape(rename.new) + r"\b", source) or
               re.search(r"\b" + re.escape(rename.old) + r"\b", owned.get(path, source))
               for path, source in originals.items()):
            return empty
    for rename in variants:
        definitions = [body for source in originals.values() for body in enum_bodies(tuple(t.value for t in tokens(source)))
                       if body.name == rename.enum]
        if len(definitions) != 1 or not definitions[0].safe_attributes or not enum_name_is_unique(
            originals, owned, rename.enum,
        ):
            return empty
        for path, source in originals.items():
            words = tuple(t.value for t in tokens(source))
            imports_variant = any(path[-2:] == (rename.enum, rename.old)
                                  for item in use_items(words) for path in item.paths)
            if any(words[index] == rename.new for body in enum_bodies(words) if body.name == rename.enum
                   for index in body.variants) or (imports_variant and any(
                       word == rename.new and (index == 0 or words[index - 1] != "::")
                       for index, word in enumerate(words)
                   )):
                return empty
            if any("*" in words[item.start:item.end] and
                   any(words[i:i + 2] == (qualifier, "::") for qualifier in {rename.enum, "Self"}
                       for i in range(item.start, item.end - 1)) for item in use_items(words)):
                return empty
            remaining = tuple(t.value for t in tokens(owned.get(path, source)))
            if not all(enum_sites_have_import(version, rename.enum, (rename.old, rename.new))
                       for version in (words, remaining)):
                return empty
            if variant_sites(remaining, rename.enum, rename.old) or any(
                remaining[index - 2:index] == (rename.enum, "::")
                for index, word in enumerate(remaining) if word == rename.old
            ):
                return empty
    result: dict[str, tuple[TokenRewrite, ...]] = {}
    for path, (old, new) in pairs.items():
        words = tuple(t.value for t in old)
        attributes = attribute_spans(words)
        decorated = decorated_items(words)
        has_glob = holds_glob_use(words)
        replacements: list[TokenRewrite] = []
        for index, (left, right) in enumerate(zip(old, new)):
            if left == right:
                continue
            previous = words[index - 1] if index else ""
            variant_match = any(left.value == rename.old and right.value == rename.new and
                                index in variant_sites(words, rename.enum, rename.old) for rename in variants)
            local_match = any(rename.path == path and rename.start <= index < rename.end and
                              left.value == rename.old and right.value == rename.new for rename in locals_found)
            global_match = CrateRename(left.value, right.value) in globals_found
            definition = previous in {"fn", "const", "static", "struct", "enum", "type", "trait", "let"}
            if any(start <= index < end for start, end in attributes) or index - 1 in decorated:
                return empty
            if previous == "." or (has_glob and not definition and not variant_match):
                return empty
            if not variant_match:
                if previous == "::" and (index < 2 or words[index - 2] not in {"self", "super", "crate"}):
                    return empty
                if shorthand_field(words, index):
                    return empty
            if not (variant_match or local_match or global_match):
                return empty
            replacements.append(TokenRewrite(index, index + 1, (right,)))
        result[path] = tuple(replacements)
    return result


def normalizations(changes: tuple[FileChange, ...], sources: RevisionTree) -> dict[str, FileNormalization]:
    lint_proofs = {change.path: prove_lint_removals(change) for change in changes}
    prepared = tuple(FileChange(
        change.status, change.old_path, change.path,
        " ".join(token.value for token in rewrite_tokens(tokens(change.before), lint_proofs[change.path].before)),
        " ".join(token.value for token in rewrite_tokens(tokens(change.after), lint_proofs[change.path].after)),
        change.hunks,
    ) for change in changes)
    imports = {change.path: prove_imports(change) for change in prepared}
    renames = prove_renames(prepared, sources, imports)
    return {change.path: FileNormalization(FileTokenRewrites(
        original_rewrites(tokens(change.before), lint_proofs[change.path].before, imports[change.path].before),
        original_rewrites(tokens(change.after), lint_proofs[change.path].after, imports[change.path].after),
    ), renames.get(change.path, ()), lint_proofs[change.path]) for change in changes}


def normalized_source(change: FileChange, proof: FileNormalization) -> tuple[Token, ...]:
    return rewrite_tokens(rewrite_tokens(tokens(change.before), proof.rewrites.before), proof.renames)


@lru_cache(maxsize=32)
def rewritten_lines(source: str, rewrites: tuple[TokenRewrite, ...]) -> tuple[tuple[Token, ...], ...]:
    parsed = tokenized_text(source)
    lines: list[list[Token]] = [[] for _ in range(source.count("\n") + 2)]
    replacements = {item.start: item for item in rewrites}
    cursor = 0
    while cursor < len(parsed.tokens):
        rewrite = replacements.get(cursor)
        if rewrite is None:
            lines[parsed.lines[cursor]].append(parsed.tokens[cursor])
            cursor += 1
        else:
            lines[parsed.lines[cursor]].extend(rewrite.replacement)
            cursor = rewrite.end
    return tuple(tuple(line) for line in lines)


def line_tokens(source: str, line: int, rewrites: tuple[TokenRewrite, ...]) -> tuple[Token, ...]:
    lines = rewritten_lines(source, rewrites)
    return lines[line] if 0 <= line < len(lines) else ()


def line_flag_omissions(change: FileChange, proof: FileNormalization) -> FileTokenRewrites:
    before = tuple(item for item in proof.rewrites.before if not item.replacement)
    after = tuple(item for item in proof.rewrites.after if not item.replacement)
    after_indices = {index for item in after for index in range(item.start, item.end)}
    words = tuple(token.value for token in tokens(change.after))
    configurations = {words[start:end] for start, end in attribute_spans(words)
                      if start in after_indices and cfg_attribute(words[start:end])}
    if not configurations:
        return FileTokenRewrites(before, after)
    # Git can align an item's unchanged cfg with the new import's identical cfg.
    # Keep that line alignment from flagging a retained condition as a literal edit.
    results: list[tuple[TokenRewrite, ...]] = []
    for text, omissions in ((change.before, before), (change.after, after)):
        words = tuple(token.value for token in tokens(text))
        omitted = {index for item in omissions for index in range(item.start, item.end)}
        results.append(omissions + tuple(TokenRewrite(start, end, ())
                                        for start, end in attribute_spans(words)
                                        if start not in omitted and words[start:end] in configurations))
    return FileTokenRewrites(results[0], results[1])


def flagged_hunks(change: FileChange, proof: FileNormalization) -> bool:
    old = tokens(change.before)
    # Rename indices follow proven removals. Map them back for hunk comparisons.
    origins = list(range(len(old)))
    for rewrite in sorted(proof.rewrites.before, key=lambda item: item.start, reverse=True):
        origins[rewrite.start:rewrite.end] = [rewrite.end - 1] * len(rewrite.replacement)
    renames = tuple(TokenRewrite(origins[item.start], origins[item.start] + 1, item.replacement) for item in proof.renames)
    before_rewrites = proof.rewrites.before + renames
    omissions = line_flag_omissions(change, proof)
    before_omissions, after_omissions = omissions.before, omissions.after
    offset = 0
    for hunk in change.hunks:
        new_start = hunk.old_start + (not hunk.old_count) + offset
        removed_lines = tuple(" ".join(token.value for token in line_tokens(
            change.before, hunk.old_start + index, before_omissions,
        )) for index in range(len(hunk.removed))) if before_omissions else hunk.removed
        added_lines = tuple(" ".join(token.value for token in line_tokens(
            change.after, new_start + index, after_omissions,
        )) for index in range(len(hunk.added))) if after_omissions else hunk.added
        if len(hunk.removed) == len(hunk.added):
            for index, (removed, added) in enumerate(zip(removed_lines, added_lines)):
                if line_tokens(change.before, hunk.old_start + index, before_rewrites) != line_tokens(
                    change.after, new_start + index, proof.rewrites.after
                ) and behavior_flagged(removed + "\n" + added):
                    return True
        elif behavior_flagged("\n".join((*removed_lines, *added_lines))):
            return True
        offset += len(hunk.added) - hunk.old_count
    return False


def changed_text_without_lint_attributes(change: FileChange, proof: LintAttributeProof) -> str:
    if not proof.before:
        return "\n".join(line for hunk in change.hunks for line in (*hunk.removed, *hunk.added))
    lines: list[str] = []
    offset = 0
    for hunk in change.hunks:
        new_start = hunk.old_start + (not hunk.old_count) + offset
        for source, start, count, rewrites in (
            (change.before, hunk.old_start, len(hunk.removed), proof.before),
            (change.after, new_start, len(hunk.added), proof.after),
        ):
            lines.extend(" ".join(token.value for token in line_tokens(source, start + index, rewrites))
                         for index in range(count))
        offset += len(hunk.added) - hunk.old_count
    return "\n".join(lines)


@dataclass(frozen=True)
class FileSpan:
    """A character span of one side of a changed file, and the text that replaces it: empty text removes the span."""

    path: str
    after: bool
    start: int
    end: int
    text: str = ""


@dataclass(frozen=True)
class ItemMove:
    """One item that moves unchanged from file A to file B.

    The source is its span in A's before text and the destination its span in B's after text, each with the comment
    lines right above it and a comment that ends its last line. The source use is A's added `use` of the item in B: a
    re-export or an import for A's own uses. An impl has no name.
    """

    kind: str
    name: str
    source: FileSpan
    destination: FileSpan
    source_use: FileSpan | None


@dataclass(frozen=True)
class ItemMoves:
    """The proven moves, which can go between several pairs of files, and every other span the moves explain: each
    A's removed uses, each B's added uses, the added `mod` lines and each re-pointed path, which is respelled to the
    path of its item's B in both texts."""

    moves: tuple[ItemMove, ...]
    removed_uses: tuple[FileSpan, ...]
    added_uses: tuple[FileSpan, ...]
    module_lines: tuple[FileSpan, ...]
    sites: tuple[FileSpan, ...]

    def spans(self, path: str, after: bool) -> tuple[FileSpan, ...]:
        """The spans of one side of one file, in order."""
        every = (*(span for move in self.moves for span in (move.source, move.destination, move.source_use) if span),
                 *self.removed_uses, *self.added_uses, *self.module_lines, *self.sites)
        return tuple(sorted((span for span in every if span.path == path and span.after == after),
                            key=lambda span: span.start))


@dataclass(frozen=True)
class NoItemMoves:
    reason: str


@dataclass(frozen=True)
class SourceItem:
    """A module-level item: its first word (attributes included), its keyword and the word after its end."""

    kind: str
    name: str
    title: str
    visibility: str
    start: int
    keyword: int
    end: int


@dataclass(frozen=True)
class MoveScope:
    """The names a file's module binds at module level: its items, its child modules and its `use` items."""

    module: CrateModule
    library: str | None
    definitions: frozenset[str]
    types: frozenset[str]
    traits: frozenset[str]
    modules: frozenset[str]
    imports: Mapping[str, tuple[tuple[str, ...], ...]]
    blocks: frozenset[str]


@dataclass(frozen=True)
class MoveText:
    path: str
    after: bool
    text: str
    words: tuple[str, ...]
    spans: tuple[TokenSpan, ...]
    items: tuple[SourceItem, ...]
    scope: MoveScope


# The item kinds that can move.
MOVABLE_KINDS = frozenset({"const", "static", "fn", "struct", "enum", "type", "impl"})
# The kinds whose name a path can name as a type, not as a module.
TYPE_KINDS = frozenset({"struct", "enum", "union", "trait", "type"})
VISIBILITY_RANKS = {"": 0, "pub(crate)": 1, "pub": 2}
VISIBILITY_SPELLINGS: dict[tuple[str, ...], str] = {(): "", ("pub",): "pub", ("pub", "(", "crate", ")"): "pub(crate)"}
NO_NAMES: frozenset[str] = frozenset()
# The attributes a moved item can carry: none of them reads the item's module path or decides whether it exists.
INERT_ATTRIBUTES = frozenset({"doc", "allow", "warn", "deny", "expect", "forbid", "must_use", "inline", "cold", "repr",
                              "non_exhaustive", "default"})
# The attributes any other item of the files of a move can hold: the inert ones, a `cfg`, which only removes the item,
# and `test`.
ITEM_ATTRIBUTES = INERT_ATTRIBUTES | {"cfg", "test"}
# The inner attributes the files of a move can hold: lint levels and doc, which change no name and no lookup.
INNER_ATTRIBUTES = frozenset({"doc", "allow", "warn", "deny", "expect", "forbid"})
# The standard derives, which expand the same in every module.
STANDARD_DERIVES = frozenset({"Debug", "Clone", "Copy", "PartialEq", "Eq", "PartialOrd", "Ord", "Hash", "Default"})
# The paths a standard derive or a std macro can be written behind: bare, or straight from the root of `std` or
# `core`.
STANDARD_PATH_PREFIXES = frozenset({(), ("std", "::"), ("core", "::"), ("::", "std", "::"), ("::", "core", "::")})
# The crates every glob of the standard library starts at.
STANDARD_CRATES = frozenset({"std", "core", "alloc"})
# The crates of the sysroot, which a path can name without a dependency.
SYSROOT_CRATES = STANDARD_CRATES | {"proc_macro", "test"}


def visibility_of(words: tuple[str, ...]) -> str:
    """The visibility that words spell: private, `pub(crate)`, `pub` or other."""
    return VISIBILITY_SPELLINGS.get(words, "other")


def item_end(words: tuple[str, ...], ends: dict[int, int], cursor: int, body: bool) -> int | None:
    """The word after an item that ends at its first `;`, or, with body, at its first brace group, outside groups."""
    while cursor < len(words) and words[cursor] != ";" and not (body and words[cursor] == "{"):
        if words[cursor] in {"(", "[", "{"}:
            if cursor not in ends:
                return None
            cursor = ends[cursor]
        cursor += 1
    if cursor == len(words):
        return None
    return cursor + 1 if words[cursor] == ";" else ends[cursor] + 1 if cursor in ends else None


# These words may precede an item keyword. Const and extern can also start their own items.
ITEM_QUALIFIERS = frozenset({"pub", "default", "const", "async", "unsafe", "safe", "extern", "auto"})


def item_prefix(words: tuple[str, ...], ends: dict[int, int], start: int) -> tuple[int, str]:
    """The keyword offset and visibility after any sequence of item qualifiers.

    Attributes are consumed by the caller. An incomplete visibility ends at len(words), so
    the item parser refuses it. Const items and extern blocks/crate declarations retain their keyword.
    """
    cursor, visibility = start, ""
    while cursor < len(words):
        word = words[cursor]
        following = words[cursor + 1:cursor + 2]
        if word == "pub":
            close = ends.get(cursor + 1, len(words)) if following == ("(",) else cursor
            visibility = visibility_of(words[cursor:close + 1]) if not visibility else "other"
            cursor = close + 1
        elif word in {"default", "async", "unsafe", "safe", "auto"}:
            cursor += 1
        elif word == "const" and following and following[0] in ITEM_QUALIFIERS | {"fn"}:
            cursor += 1
        elif word == "extern":
            next_word = cursor + 1
            if following and following[0].startswith('"'):
                next_word += 1
            if words[next_word:next_word + 1] in (("{",), ("crate",)):
                break
            cursor = next_word
        else:
            break
    return cursor, visibility


def source_items(words: tuple[str, ...]) -> tuple[SourceItem, ...] | None:
    """Each module-level item of a file, or None when a word does not start a known item."""
    ends = delimiter_ends(words)
    openings = {end: start for start, end in ends.items()}
    attributes = dict(attribute_spans(words))
    items: list[SourceItem] = []
    cursor = 0
    while cursor < len(words):
        if cursor in attributes and words[cursor + 1] == "!":
            cursor = attributes[cursor]  # An inner attribute belongs to the module, not to an item.
            continue
        start = cursor
        while cursor in attributes:
            cursor = attributes[cursor]
        cursor, visibility = item_prefix(words, ends, cursor)
        if cursor >= len(words):
            return None
        keyword, word = cursor, words[cursor]
        following = words[keyword + 1] if keyword + 1 < len(words) else ""
        name, kind = following, word
        if word in {"use", "type", "const", "static"} or (word == "extern" and following == "crate"):
            end = item_end(words, ends, keyword, body=False)
            if word == "use":
                name = ""
            elif word == "static" and following == "mut":
                name = words[keyword + 2]
            elif word == "extern":
                kind, name = "extern crate", words[keyword + 2]
        elif word in {"fn", "struct", "enum", "union", "trait", "impl"} or (word == "extern" and following != "crate"):
            end = item_end(words, ends, keyword + 1, body=True)
            if word in {"impl", "extern"}:
                name = ""
        elif word == "mod":
            end = keyword + 3 if words[keyword + 2:keyword + 3] == (";",) else item_end(words, ends, keyword, body=True)
        elif word == "macro_rules" and following == "!":
            name, end = words[keyword + 2], item_end(words, ends, keyword + 3, body=True)
        elif IDENTIFIER.fullmatch(word):
            bang = keyword
            while words[bang + 1:bang + 3] and words[bang + 1] == "::" and IDENTIFIER.fullmatch(words[bang + 2]):
                bang += 2
            if words[bang + 1:bang + 2] != ("!",) or bang + 2 not in ends:
                return None
            kind, name = "macro", word
            end = ends[bang + 2] + 1
            if words[bang + 2] != "{":
                end = end + 1 if words[end:end + 1] == (";",) else None
        else:
            return None
        if end is None or not IDENTIFIER.fullmatch(name or "_"):
            return None
        title = " ".join(words[keyword:openings.get(end - 1, end)]) if kind == "impl" else f"{kind} {name}"
        items.append(SourceItem(kind, name, title, visibility, start, keyword, end))
        cursor = end
    return tuple(items)


def move_scope(words: tuple[str, ...], items: tuple[SourceItem, ...], module: CrateModule,
               library: str | None) -> MoveScope:
    """The module-level names of a file; raises MoveRefused for a `use` item it cannot read."""
    imports: dict[str, list[tuple[str, ...]]] = {}
    item_uses = {item.start for item in items if item.kind == "use"}
    for item in items:
        if item.kind != "use":
            continue
        body = words[item.keyword + 1:item.end - 1]
        if body[-2:] == ("::", "*") and use_paths(body[:-2]):
            continue  # A glob binds no name the scope can read; `refuse_globs` reads it.
        paths = use_paths(body)
        if not paths:
            raise MoveRefused(f"cannot read the use item `{' '.join(words[item.start:item.end])}`")
        for path in paths:
            if path[-1] == "self":
                path = path[:-1]
            imports.setdefault(path[-1], []).append(path)
    # A block can declare a `use` that shadows a module-level name for a path inside it.
    blocks = frozenset(path[-1] for use in use_items(words) if use.start not in item_uses for path in use.paths)
    named = [item for item in items if item.name and item.kind not in {"use", "mod", "macro"}]
    return MoveScope(
        module, library,
        frozenset(item.name for item in named),
        frozenset(item.name for item in named if item.kind in TYPE_KINDS),
        frozenset(item.name for item in named if item.kind == "trait"),
        frozenset(item.name for item in items if item.kind == "mod"),
        {name: tuple(paths) for name, paths in imports.items()},
        blocks,
    )


def scoped_path(prefix: tuple[str, ...], scope: MoveScope, inline: tuple[str, ...] = (),
                seen: frozenset[str] = NO_NAMES) -> tuple[str, ...] | UnresolvedPath:
    """What a path names from the scope: `("crate", *module)`, `("extern", *path)`, or `("item", *path)` for a path
    that starts at a type. Inline names the inline modules the path is written in."""
    if not prefix:
        return UnresolvedPath()
    head = prefix[0]
    if head in PATH_ANCHORS:
        module = scope.module if isinstance(scope.module, OutsideCrate) else (*scope.module, *inline)
        resolved = resolved_module(prefix, module)
        return resolved if isinstance(resolved, UnresolvedPath) else ("crate", *resolved)
    if inline or head in scope.blocks:
        return UnresolvedPath()
    # A path's head is in the type namespace: a module or a type declared here wins over an import of a value of
    # the same name, as two names in one namespace do not compile.
    if head in scope.modules:
        return UnresolvedPath() if isinstance(scope.module, OutsideCrate) else ("crate", *scope.module, *prefix)
    if head in scope.types or head == "Self":
        return ("item", *prefix)
    targets = scope.imports.get(head, ())
    if targets:
        if len(targets) != 1 or head in seen:
            return UnresolvedPath()
        target = scoped_path(targets[0], scope, seen=seen | {head})
        return target if isinstance(target, UnresolvedPath) else (*target, *prefix[1:])
    if head in scope.definitions:
        return UnresolvedPath()
    if isinstance(scope.module, OutsideCrate) and head == scope.library:
        return ("crate", *prefix[1:])
    return ("extern", *prefix)


def bound_path(name: str, scope: MoveScope) -> tuple[str, ...] | UnresolvedPath:
    """What a bare name binds to at module level: an item, an import, or `("free", name)` for a name the module does
    not bind (a prelude name or a local name)."""
    defined = name in scope.definitions or name in scope.modules
    targets = scope.imports.get(name, ())
    if (defined and targets) or len(targets) > 1:
        return UnresolvedPath()
    if defined:
        return UnresolvedPath() if isinstance(scope.module, OutsideCrate) else ("crate", *scope.module, name)
    if targets:
        return scoped_path(targets[0], scope, seen=frozenset({name}))
    return ("free", name)


def scope_names(text: MoveText, start: int, end: int, keywords: frozenset[str]) -> frozenset[str]:
    """The names words start..end look up in the module scope: each identifier that is not one of keywords, the
    keywords of the crate's edition, and does not follow `::`, `.` or `'`, and each name a format literal captures.
    Before 2024 `gen()` calls the `gen` of the module."""
    names: set[str] = set()
    for index in range(start, end):
        token = text.spans[index].token
        if token.kind == "literal" and "{" in token.value:
            names.update(match[2] for match in re.finditer(r"(\{+)([A-Za-z_][A-Za-z0-9_]*)", token.value)
                         if len(match[1]) % 2)
        elif (token.kind == "identifier" and token.value not in keywords
              and text.words[index - 1:index] not in {("::",), (".",), ("'",)}):
            names.add(token.value)
    return frozenset(names)


def bare_names(words: tuple[str, ...], cut: frozenset[int]) -> frozenset[str]:
    """The words outside cut that do not follow `::` or `.`."""
    return frozenset(word for index, word in enumerate(words)
                     if index not in cut and words[index - 1:index] not in {("::",), (".",)})


def file_module(path: str, crate: str, sources: RevisionTree) -> CrateModule:
    """The module of a file of the crate; `src/main.rs` is the root of its own crate when `src/lib.rs` exists."""
    root = PurePosixPath(crate) / "src"
    if path == str(root / "main.rs") and str(root / "lib.rs") in sources.paths:
        return OutsideCrate()
    return crate_module(path, root)


def crate_manifest(sources: RevisionTree, crate: str) -> dict[str, object]:
    """The crate's Cargo.toml. Raises MoveRefused when it is missing or unreadable, when it sets a target `path`, and
    when `src/main.rs` declares a module beside `src/lib.rs`: each moves a file of the crate into a module tree the
    proof does not read."""
    root = PurePosixPath(crate)
    manifest = str(root / "Cargo.toml")
    if manifest not in sources.paths:
        raise MoveRefused(f"{manifest} is missing")
    try:
        data = cast(dict[str, object], tomllib.loads(sources.text(manifest)))
    except tomllib.TOMLDecodeError:
        raise MoveRefused(f"cannot read {manifest}") from None
    targets = [data.get("lib")]
    for kind in ("bin", "example", "test", "bench"):
        entries = data.get(kind)
        if isinstance(entries, list):
            targets.extend(cast(list[object], entries))
    if any(isinstance(target, dict) and "path" in target for target in targets):
        raise MoveRefused(f"{manifest} sets a target path")
    main = str(root / "src" / "main.rs")
    if (str(root / "src" / "lib.rs") in sources.paths and main in sources.paths
            and any(token.value == "mod" for token in tokens(sources.text(main)))):
        raise MoveRefused(f"{main} declares a module beside src/lib.rs")
    return data


def dependency_names(manifest: dict[str, object]) -> frozenset[str]:
    """The names the crate's code uses for its dependencies: the keys of each dependency table, also under `target`."""
    tables: list[object] = [manifest]
    target = manifest.get("target")
    if isinstance(target, dict):
        tables.extend(cast(dict[str, object], target).values())
    names: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            continue
        for kind in ("dependencies", "dev-dependencies", "build-dependencies"):
            dependencies = cast(dict[str, object], table).get(kind)
            if isinstance(dependencies, dict):
                names.update(key.replace("-", "_") for key in cast(dict[str, object], dependencies))
    return frozenset(names)


def bound_standard_names(sources: RevisionTree, crate: str, names: frozenset[str], befores: Mapping[str, MoveText],
                         afters: Mapping[str, MoveText]) -> frozenset[str]:
    """The names that a file of the crate, before or after the change, holds anywhere but before `::`: there a use
    leaf, a `use ... as`, an item or a `mod` can bind one of them."""
    pattern = re.compile(rf"\b(?:{'|'.join(sorted(names))})\b(?!\s*::)")
    found: set[str] = set()
    for path in crate_files(sources, crate, ()).keys() | afters.keys():
        if path in afters:
            sides = (befores[path].words, afters[path].words)
        elif pattern.search(sources.text(path)):
            sides = (tuple(token.value.removeprefix("r#") for token in tokens(sources.text(path))),)
        else:
            continue
        found.update(word for words in sides for index, word in enumerate(words)
                     if word in names and words[index + 1:index + 2] != ("::",))
    return frozenset(found)


# The std macros that expand to an expression or a statement and emit no item. A macro call is accepted only when
# it calls one of these, bare or from the root of `std` or `core`.
STD_EXPRESSION_MACROS = frozenset({
    "println", "eprintln", "print", "eprint", "format", "format_args", "write", "writeln", "vec", "assert",
    "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq", "debug_assert_ne", "panic", "unreachable", "todo",
    "unimplemented", "matches", "concat", "stringify", "env", "option_env",
})


@lru_cache(maxsize=128)
def raw_words(text: str) -> tuple[str, ...]:
    """The words of text as written, each with its `r#`."""
    return tuple(token.value for token in tokens(text))


@lru_cache(maxsize=128)
def plain_words(text: str) -> tuple[str, ...]:
    """The words of text, each without its `r#`."""
    return tuple(word.removeprefix("r#") for word in raw_words(text))


def macro_call_names(words: tuple[str, ...], keywords: frozenset[str]) -> frozenset[str]:
    """The name of each macro call, without its `r#`: a word before `!` and an opening delimiter that is not one of
    keywords, the keywords of the crate's edition. words keep their `r#`, and a word with `r#`, such as `r#async`, is
    never a keyword."""
    return frozenset(word.removeprefix("r#") for index, word in enumerate(words[:-2])
                     if IDENTIFIER.fullmatch(word) and (word.startswith("r#") or word not in keywords)
                     and words[index + 1] == "!" and words[index + 2] in {"(", "[", "{"})


def use_words(words: tuple[str, ...], ends: dict[int, int]) -> frozenset[int]:
    """The index of each word of a `use` item and of each word in the list of a `macro_use` attribute."""
    inside: set[int] = set()
    for index, word in enumerate(words):
        if word == "use":
            end = next((cursor for cursor in range(index, len(words)) if words[cursor] == ";"), len(words))
            inside.update(range(index, end))
        elif word == "macro_use" and index + 1 in ends:
            inside.update(range(index + 2, ends[index + 1]))
    return frozenset(inside)


def opens_macro_names(words: tuple[str, ...], standard: frozenset[str]) -> bool:
    """Whether the words of one file can bring a macro of another crate under a bare name: a `macro_use` word in any
    attribute, inner or outer, also inside a `cfg_attr` at any depth, or a glob use whose path can start at another
    crate. Only a glob from `crate`, `self` or `super`, or from `std`, `core` or `alloc` while standard is empty, is
    closed, and a `*` in a use the use trees cannot read counts as open."""
    if any("macro_use" in words[begin:end] for begin, end in attribute_spans(words)):
        return True
    trees = use_trees(words, item_starts(words))
    for index, word in enumerate(words):
        if word != "use":
            continue
        end = next((cursor for cursor in range(index, len(words)) if words[cursor] == ";"), len(words))
        for star in (cursor for cursor in range(index, end) if words[cursor] == "*"):
            head = trees.prefixes[star][:1] if star in trees.prefixes else ("",)
            if not (head and (head[0] in PATH_ANCHORS or (head[0] in STANDARD_CRATES and not standard))):
                return True
    return False


def macro_refusal(words: tuple[str, ...], names: frozenset[str],
                  prefixes: frozenset[tuple[str, ...]]) -> str | None:
    """Why the words of one file can bring a binding no scan reads, or None. names holds `include` and each name
    the crate calls as a macro, and prefixes holds the paths an allowed macro call can be written behind."""
    uses = use_words(words, delimiter_ends(words))
    for index, word in enumerate(words):
        following = words[index + 1:index + 2]
        if word == "include":
            if following == ("!",):
                return "an include! brings code the proof cannot read"
            return "a use or path of include! brings code the proof cannot read"
        if word in {"macro", "macro_rules"}:
            return "a crate macro the proof cannot read"
        if word not in names or following == ("::",):
            continue
        if following != ("!",):
            if index in uses or (index and words[index - 1] == "::"):
                return f"a use or path of {word}! the proof cannot resolve"
            continue
        start = index
        while start >= 2 and words[start - 1] == "::" and IDENTIFIER.fullmatch(words[start - 2]):
            start -= 2
        if start and words[start - 1] == "::":
            start -= 1
        if words[index + 2:index + 3] in {("(",), ("[",), ("{",)} and (
                word not in STD_EXPRESSION_MACROS or words[start:index] not in prefixes):
            return f"a macro call the proof cannot resolve: {word}!"
    return None


def refuse_binding_macros(sources: RevisionTree, crate: str, befores: Mapping[str, MoveText],
                          afters: Mapping[str, MoveText], standard: frozenset[str], keywords: frozenset[str]) -> None:
    """Raise MoveRefused for a file of the crate, before or after the change, whose macros can make a binding that
    no other scan reads. The rules form an allow list:

    - Any `macro_rules!` or `macro` item refuses, and so does any `include` word: a call, a use leaf, an alias
      source or a path segment such as `std::include`. The proof reads only .rs files, the included file can bind a
      name, and `use std::include as splice;` lets a macro call it under another name. `include_str!` and
      `include_bytes!` are other words.
    - A word that the crate calls as a macro refuses in a `use` item, in a `macro_use` list or as a path leaf, as
      `use tracing::info as log;` hides which macro `log!` calls.
    - A macro call anywhere in the crate, a fn body too, is accepted only from STD_EXPRESSION_MACROS. In a fn
      body `provider::imports!()` can expand to `use crate::writer::*;`, and a `match` arm such as `LIMIT => true`
      then binds a new name after `LIMIT` moves.
    - The call is accepted from the root of `std` or `core` only while standard is empty. standard holds each of
      `std`, `core` and `alloc` that is a dependency key or that a file of the crate binds: with
      `std = { package = "provider", version = "1" }`, `std::concat!()` calls a macro of `provider`.
    - The call is accepted bare only while no file of the crate opens a bare name to another crate's macros
      (`opens_macro_names`): with `use provider::*;`, `concat!()` can call a `concat` macro of `provider`.

    A raw word such as `r#use` counts as its plain spelling, so the scan reads each word without its `r#`, but a
    word with `r#` is never a keyword: `provider::r#async!()` is a macro call. Any other word before `!` that is not
    one of keywords, the keywords of the crate's edition, is a macro call, so `provider::gen!()` is one before 2024.
    A comment or a string does not count."""
    texts = tuple((path, text) for path in sorted(crate_files(sources, crate, ()).keys() | afters.keys())
                  for text in ((befores[path].text, afters[path].text) if path in afters else (sources.text(path),)))
    names = frozenset({"include"}).union(*(macro_call_names(raw_words(text), keywords)
                                           for _, text in texts if "!" in text))
    pattern = re.compile(r"\b(?:macro(?:_rules)?|" + "|".join(sorted(map(re.escape, names))) + r")\b")
    opened = any(opens_macro_names(plain_words(text), standard) for _, text in texts
                 if "macro_use" in text or ("*" in text and holds_glob_use(plain_words(text))))
    # A bare call is accepted while no file opens a bare name, and one from `std` or `core` while standard is empty.
    prefixes = frozenset(prefix for prefix in STANDARD_PATH_PREFIXES if not (standard if prefix else opened))
    for path, text in texts:
        refusal = macro_refusal(plain_words(text), names, prefixes) if pattern.search(text) else None
        if refusal is not None:
            raise MoveRefused(f"{path}: {refusal}")


def crate_edition(sources: RevisionTree, crate: str, manifest: dict[str, object]) -> int:
    """The crate's edition. Raise MoveRefused for a crate older than Rust 2018, where a path such as `use writer::*;`
    starts at the crate root, not at an extern crate. A missing `edition` means 2015, and `edition.workspace = true`
    reads the edition of the nearest workspace root manifest."""
    package = manifest.get("package")
    edition = cast(dict[str, object], package).get("edition", "2015") if isinstance(package, dict) else "2015"
    if isinstance(edition, dict) and cast(dict[str, object], edition).get("workspace") is True:
        edition = None
        for directory in (PurePosixPath(crate), *PurePosixPath(crate).parents):
            path = str(directory / "Cargo.toml")
            if path not in sources.paths:
                continue
            try:
                root = cast(dict[str, object], tomllib.loads(sources.text(path)))
            except tomllib.TOMLDecodeError:
                raise MoveRefused(f"cannot read {path}") from None
            workspace = root.get("workspace")
            if isinstance(workspace, dict):
                table = cast(dict[str, object], workspace).get("package")
                edition = cast(dict[str, object], table).get("edition") if isinstance(table, dict) else None
                break
    if not isinstance(edition, str) or not edition.isdigit() or int(edition) < 2018:
        raise MoveRefused(f"the crate's edition is {edition or 'unknown'}, older than 2018 or unreadable")
    return int(edition)


def edition_keywords(edition: int) -> frozenset[str]:
    """The strict and reserved keywords of an edition from 2018 on: RUST_KEYWORDS, without `gen` before 2024. The weak
    keywords, such as `union`, `macro_rules`, `raw` and `safe`, are names."""
    return RUST_KEYWORDS if edition >= 2024 else KEYWORDS_BEFORE_2024


def library_name(manifest: dict[str, object]) -> str | None:
    """The name other crates of the package use for the library: `[lib] name`, else the package name."""
    tables = cast(dict[str, dict[str, object]], manifest)
    name = tables.get("lib", {}).get("name", tables.get("package", {}).get("name"))
    return name.replace("-", "_") if isinstance(name, str) else None


def move_text(path: str, after: bool, text: str, module: CrateModule, library: str | None,
              touched: bool = True) -> MoveText:
    """The words, items and scope of one side of a file. A touched file refuses a use with an attribute, which can
    switch the use off, and a raw identifier, which names what its plain spelling names. `refuse_globs` reads its glob
    uses."""
    if any(token.kind == "unknown" for token in tokens(text)):
        raise MoveRefused(f"{path}: an unknown token")
    spans = token_spans(text)
    words = tuple(span.token.value for span in spans)
    items = source_items(words)
    if items is None:
        raise MoveRefused(f"{path}: cannot read its items")
    if touched:
        if any(use.attributed for use in use_items(words)):
            raise MoveRefused(f"{path}: a use with an attribute")
        if any(span.token.kind == "identifier" and span.token.value.startswith("r#") for span in spans):
            raise MoveRefused(f"{path}: a raw identifier")
        if any(attribute_comment(text, spans, begin, end) for begin, end in attribute_spans(words)):
            raise MoveRefused(f"{path}: a comment in an attribute")
    return MoveText(path, after, text, words, spans, items, move_scope(words, items, module, library))


def word_span(text: MoveText, start: int, end: int, replacement: str = "") -> FileSpan:
    """The characters of words start..end."""
    return FileSpan(text.path, text.after, text.spans[start].start, text.spans[end - 1].end, replacement)


def item_region(text: MoveText, item: SourceItem) -> FileSpan:
    """An item's span with the whole comment lines right above it, up to a blank line or a module doc line, and a
    `//` comment that ends its last line."""
    source = text.text
    start = text.spans[item.start].start
    end = text.spans[item.end - 1].end
    floor = text.spans[item.start - 1].end if item.start else 0
    begin = source.rfind("\n", 0, start) + 1
    if begin >= floor and not source[begin:start].strip():
        start = begin
        while start > floor:
            previous = source.rfind("\n", 0, start - 1) + 1
            line = source[previous:start].strip()
            if previous < floor or not line or line.startswith(("//!", "/*!")):
                break
            start = previous
    line_end = source.find("\n", end)
    line_end = len(source) if line_end < 0 else line_end
    if source[end:line_end].strip().startswith("//"):
        end = line_end
    return FileSpan(text.path, text.after, start, end)


def region_shape(text: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """The words of a span and the comment lines around them."""
    spans = token_spans(text)
    return tuple(span.token.value for span in spans), comment_gaps(text, spans, lambda line: line)


def item_key(text: MoveText, item: SourceItem) -> tuple[str, str, tuple[str, ...]]:
    return item.kind, item.title, text.words[item.start:item.end]


def word_range(text: MoveText, span: FileSpan) -> range:
    """The word indexes inside a character span."""
    starts = [token.start for token in text.spans]
    return range(bisect_left(starts, span.start), bisect_left(starts, span.end))


def private_members(text: MoveText, items: tuple[SourceItem, ...]) -> dict[str, str]:
    """The private fields of each struct and union and the private members of each inherent impl, by name, with the
    title of their item. A tuple field is named by its index."""
    words = text.words
    ends = delimiter_ends(words)
    openings = {end: start for start, end in ends.items()}
    result: dict[str, str] = {}

    def entries(open_index: int) -> list[int]:
        """The first word of each comma-separated entry of a group, outside nested groups and angle brackets."""
        firsts = [open_index + 1]
        cursor, angles = open_index + 1, 0
        while cursor < ends[open_index]:
            word = words[cursor]
            if word in {"(", "[", "{"} and cursor in ends:
                cursor = ends[cursor]
            angles += (word == "<") - (word == ">")
            if word == "," and not angles:
                firsts.append(cursor + 1)
            cursor += 1
        return [first for first in firsts if first < ends[open_index]]

    def member_visibility(first: int) -> tuple[str, int]:
        while words[first] == "#" and first + 1 in ends:
            first = ends[first + 1] + 1
        if words[first] != "pub":
            return "", first
        close = ends.get(first + 1, first) if words[first + 1] == "(" else first
        return visibility_of(words[first:close + 1]), close + 1

    for item in items:
        last = item.end - 1
        if item.kind in {"struct", "union"} and words[last] == "}" and last in openings:
            for first in entries(openings[last]):
                visibility, name = member_visibility(first)
                if visibility not in {"pub", "pub(crate)"} and words[name + 1:name + 2] == (":",):
                    result[words[name]] = item.title
        elif item.kind == "struct" and words[last] == ";":
            group = next((index for index in range(item.keyword, last) if words[index] == "(" and index in ends), None)
            for position, first in enumerate(entries(group) if group is not None else ()):
                if member_visibility(first)[0] not in {"pub", "pub(crate)"}:
                    result[str(position)] = item.title
        elif item.kind == "impl" and words[last] == "}" and last in openings:
            header = words[item.keyword:openings[last]]
            if any(word == "for" and header[index + 1:index + 2] != ("<",) for index, word in enumerate(header)):
                continue  # A trait impl's members are as visible as the trait.
            cursor = openings[last] + 1
            while cursor < last:
                word = words[cursor]
                if word in {"fn", "const", "type"} and words[cursor + 1] not in {"fn", "unsafe", "async", "extern"}:
                    lead = cursor - 1
                    while words[lead] in {"const", "unsafe", "async", "default", "extern"} or words[lead].startswith('"'):
                        lead -= 1
                    visibility = "pub" if words[lead] == "pub" else (
                        visibility_of(words[openings[lead] - 1:lead + 1]) if words[lead] == ")" and lead in openings
                        else "")
                    if visibility not in {"pub", "pub(crate)"}:
                        result[words[cursor + 1]] = item.title
                if word in {"(", "[", "{"} and cursor in ends:
                    cursor = ends[cursor]
                cursor += 1
    return result


def edited_text(text: str, spans: tuple[FileSpan, ...]) -> str:
    """The text with each span replaced; raises MoveRefused when two spans overlap."""
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        if span.start < cursor:
            raise MoveRefused("two edits of the move overlap")
        pieces.extend((text[cursor:span.start], span.text))
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def canonical_text(text: str, keywords: frozenset[str]) -> tuple[CodePiece, ...]:
    spans = token_spans(text)
    if any(token.kind == "unknown" for token in tokens(text)) or len(spans) != len(tokens(text)):
        raise MoveRefused("an edited text has an unknown token")
    return canonical_code(tuple(span.token.value for span in spans), comment_gaps(text, spans, lambda line: line),
                          keywords)


def module_path(module: tuple[str, ...]) -> str:
    return "::".join(("crate", *module))


def prove_item_moves(changes: tuple[FileChange, ...], sources: RevisionTree) -> ItemMoves | NoItemMoves:
    """Prove that the change only moves items between module files of the same crate: from each file A to one or
    more files B, and no file is both an A and a B.

    Each moved item keeps its words and comments, and every other edit re-points a path from the old module to the
    new one or adds or removes a `use` line the move needs. The proof refuses whenever it is not sure.
    """
    try:
        return proven_item_moves(changes, sources)
    except MoveRefused as refusal:
        return NoItemMoves(str(refusal))


def proven_item_moves(changes: tuple[FileChange, ...], sources: RevisionTree) -> ItemMoves:
    crates = {sources.crate(path) for change in changes for path in (change.old_path, change.path)}
    if len(crates) != 1:
        raise MoveRefused("the change crosses crates")
    crate = crates.pop()
    manifest = crate_manifest(sources, crate)
    keywords = edition_keywords(crate_edition(sources, crate, manifest))
    library = library_name(manifest)
    for change in changes:
        if change.status not in {FileStatus.MODIFIED, FileStatus.ADDED} or not change.path.endswith(".rs"):
            raise MoveRefused(f"{change.path}: not a modified or added Rust file")
    modules = {change.path: file_module(change.path, crate, sources) for change in changes}
    befores = {change.path: move_text(change.path, False, change.before, modules[change.path], library)
               for change in changes}
    afters = {change.path: move_text(change.path, True, change.after, modules[change.path], library)
              for change in changes}
    # A path from std, core or alloc, such as `std::Clone` in a derive, names the standard library only while no
    # dependency and no file of the crate gives the name to another crate or module.
    standard = (STANDARD_CRATES & dependency_names(manifest)
                | bound_standard_names(sources, crate, STANDARD_CRATES, befores, afters))
    refuse_binding_macros(sources, crate, befores, afters, standard, keywords)

    # Pair each item that leaves one file with its one copy in another.
    def keys(text: MoveText) -> Counter[tuple[str, str, tuple[str, ...]]]:
        return Counter(item_key(text, item) for item in text.items if item.kind != "use" and not (
            item.kind == "mod" and text.words[item.end - 1] == ";"))

    removed = {path: keys(befores[path]) - keys(afters[path]) for path in befores}
    added = {path: keys(afters[path]) - keys(befores[path]) for path in afters}
    pairs: list[tuple[str, str, tuple[str, str, tuple[str, ...]]]] = []
    for source, keyed in removed.items():
        for key, count in keyed.items():
            copies = [path for path in added if path != source and added[path][key]]
            if count > 1 or len(copies) > 1 or any(added[path][key] > 1 for path in copies):
                raise MoveRefused(f"two copies of {key[1]}")
            if copies:
                pairs.append((source, copies[0], key))
                added[copies[0]][key] -= 1
                keyed[key] = 0
    sources_of = {source for source, _, _ in pairs}
    destinations = {destination for _, destination, _ in pairs}
    if sources_of & destinations:
        raise MoveRefused(f"{min(sources_of & destinations)} is both a source and a destination of the move")
    titles = {path: {(kind, title) for kind, title, _ in +keyed} for path, keyed in removed.items()}
    for path, keyed in added.items():
        for key in +keyed:
            if any(other != path and key[:2] in titles[other] for other in titles):
                raise MoveRefused(f"{key[1]} changes during the move")
            if any(key in keys(text) for text in befores.values()) or untouched_copy(sources, crate, befores, key):
                raise MoveRefused(f"two copies of {key[1]}")
            if path in destinations:
                raise MoveRefused(f"{path} gains another item: {key[1]}")
    for path, keyed in removed.items():
        for key in +keyed:
            if key[:2] not in {(kind, title) for kind, title, _ in +added[path]}:
                raise MoveRefused(f"{path} loses {key[1]}")
    if not pairs:
        raise MoveRefused("no item moves")
    if any(isinstance(modules[path], OutsideCrate) for path in sources_of | destinations):
        raise MoveRefused("an item moves to or from a file outside the crate's module tree")
    # Each pair of files moves its items by the rules of one move; a file is the source or the destination of any
    # number of pairs. The source of a pair is A and its destination B.
    module_of = {path: module for path, module in modules.items() if not isinstance(module, OutsideCrate)}
    groups: dict[tuple[str, str], list[tuple[str, str, tuple[str, ...]]]] = {}
    for source, destination, key in pairs:
        groups.setdefault((source, destination), []).append(key)
    sides = (*(befores[path] for path in sorted(sources_of | destinations)),
             *(afters[path] for path in sorted(sources_of | destinations)))
    rebound = STANDARD_CRATES & {word for text in sides for word in text.words} & standard
    # A glob's first name names another crate only while it is a dependency or a sysroot crate that no file binds.
    candidates = dependency_names(manifest) | SYSROOT_CRATES
    externs = candidates - bound_standard_names(sources, crate, candidates, befores, afters)
    if rebound:
        named = " or ".join(dict.fromkeys(path for pair in groups for path in pair))
        raise MoveRefused(f"{named} names {min(rebound)}, and the crate gives the name to another crate or module")
    # A use can give the name of a standard derive or an allowed attribute to a macro of another crate.
    for text in sides:
        for name, paths in text.scope.imports.items():
            if name in STANDARD_DERIVES | ITEM_ATTRIBUTES and any(path[0] not in STANDARD_CRATES for path in paths):
                raise MoveRefused(f"{text.path} imports {name}, the name of a standard derive or attribute")

    # Each moved item keeps its words and its comments. The spans carry the paths of A and B.
    moved: list[tuple[SourceItem, SourceItem, FileSpan, FileSpan]] = []
    for (path_a, path_b), group in groups.items():
        before_a, after_b = befores[path_a], afters[path_b]
        for key in group:
            source_item = next(item for item in before_a.items if item_key(before_a, item) == key)
            destination_item = next(item for item in after_b.items if item_key(after_b, item) == key)
            if source_item.kind not in MOVABLE_KINDS:
                raise MoveRefused(f"a {source_item.kind} moves: {source_item.title}")
            if source_item.visibility == "other":
                raise MoveRefused(f"{source_item.title} has a visibility the proof cannot read")
            source, destination = item_region(before_a, source_item), item_region(after_b, destination_item)
            if region_shape(change_text(before_a, source)) != region_shape(change_text(after_b, destination)):
                raise MoveRefused(f"the doc comment of {source_item.title} is left behind or changed")
            moved.append((source_item, destination_item, source, destination))
    # A derive or an attribute macro can read the module path, as bevy's `Reflect` does for a type path, and a cfg
    # decides whether code exists, so a moved item carries only attributes that read nothing.
    for item, _, source, _ in moved:
        words = befores[source.path].words
        for begin, end in attribute_spans(words[item.start:item.end]):
            name = macro_attribute(words[item.start + begin:item.start + end])
            if name is not None:
                raise MoveRefused(f"{item.title} has an attribute a macro can read: {name}")
    for path, texts in (*((path, befores) for path in sorted(sources_of)),
                        *((path, afters) for path in sorted(destinations))):
        problem = module_chain(module_of[path], sources, crate, texts)
        if problem is not None:
            raise MoveRefused(problem)
    names = [item.name for item, _, _, _ in moved if item.kind != "impl"]
    if len(names) != len(set(names)):
        raise MoveRefused("two moved items have one name")
    # The pair of each moved name, and the cuts of its words: by pair, and by file over all its pairs.
    pair_of = {item.name: (source.path, destination.path) for item, _, source, destination in moved
               if item.kind != "impl"}
    pair_moves = {pair: [move for move in moved if (move[2].path, move[3].path) == pair] for pair in groups}
    pair_source_cuts = {pair: frozenset(index for _, _, span, _ in pair_moves[pair]
                                        for index in word_range(befores[pair[0]], span)) for pair in groups}
    pair_destination_cuts = {pair: frozenset(index for _, _, _, span in pair_moves[pair]
                                             for index in word_range(afters[pair[1]], span)) for pair in groups}
    source_cuts = {path: frozenset[int]().union(*(cut for pair, cut in pair_source_cuts.items() if pair[0] == path))
                   for path in sources_of}
    destination_cuts = {path: frozenset[int]().union(*(cut for pair, cut in pair_destination_cuts.items()
                                                       if pair[1] == path)) for path in destinations}
    pair_names = {pair: frozenset[str]().union(*(scope_names(befores[pair[0]], item.start, item.end, keywords)
                                                 for item, _, _, _ in pair_moves[pair])) for pair in groups}
    out_names = {path: frozenset[str]().union(*(names for pair, names in pair_names.items() if pair[0] == path))
                 for path in sources_of}
    in_names = {path: frozenset[str]().union(*(names for pair, names in pair_names.items() if pair[1] == path))
                for path in destinations}
    # A macro or an attribute macro outside a fn body can define a name that a moved item looks up, and expand it
    # differently in A and in B, and an inner attribute such as `no_implicit_prelude` changes how names resolve. The
    # checks above and below read the moved items.
    for text, cut in (*((befores[path], source_cuts[path]) for path in sorted(sources_of)),
                      *((afters[path], frozenset[int]()) for path in sorted(sources_of)),
                      *((befores[path], frozenset[int]()) for path in sorted(destinations)),
                      *((afters[path], destination_cuts[path]) for path in sorted(destinations))):
        expansion = item_level_expansion(text.words, cut, keywords)
        if expansion is not None:
            raise MoveRefused(f"{text.path} has {expansion}")

    # A new module file holds only moved items and uses, and its one added `mod` line is no more visible than they are.
    module_lines: list[FileSpan] = []
    for path_b in sorted(destinations):
        module_b = module_of[path_b]
        if next(change for change in changes if change.path == path_b).status != FileStatus.ADDED:
            continue
        if not module_b:
            raise MoveRefused(f"{path_b} is a new crate root")
        parents = [path for path, module in modules.items() if module == module_b[:-1]
                   and next(change for change in changes if change.path == path).status == FileStatus.MODIFIED]
        if len(parents) != 1:
            raise MoveRefused(f"no one changed parent file declares {path_b}")
        parent_before, parent_after = befores[parents[0]], afters[parents[0]]
        declared = Counter(parent_before.words[item.start:item.end] for item in parent_before.items
                           if item.kind == "mod")
        lines = [item for item in parent_after.items if item.kind == "mod" and item.name == module_b[-1]
                 and parent_after.words[item.end - 1] == ";"
                 and not declared[parent_after.words[item.start:item.end]]]
        if len(lines) != 1 or parent_after.words[lines[0].start] == "#":
            raise MoveRefused(f"{parents[0]} does not gain exactly one plain `mod {module_b[-1]};` line")
        rank = min((VISIBILITY_RANKS.get(item.visibility, 0) if item.kind != "impl" else 0)
                   for item, _, _, destination in moved if destination.path == path_b)
        if VISIBILITY_RANKS.get(lines[0].visibility, 3) > rank:
            raise MoveRefused(f"`mod {module_b[-1]};` is more visible than the items it holds")
        module_lines.append(word_span(parent_after, lines[0].start, lines[0].end))

    # B has no other binding of a moved name.
    for name in names:
        path_b = pair_of[name][1]
        if name in bare_names(befores[path_b].words, frozenset()):
            raise MoveRefused(f"name collision: {path_b} already has {name}")

    def use_counts(text: MoveText) -> Counter[tuple[str, ...]]:
        return Counter(text.words[item.start:item.end] for item in text.items if item.kind == "use")

    def use_changes(old: MoveText, new: MoveText) -> list[SourceItem]:
        """The `use` items of new whose words old does not have."""
        extra = use_counts(new) - use_counts(old)
        result: list[SourceItem] = []
        for item in new.items:
            words = new.words[item.start:item.end]
            if item.kind == "use" and extra[words]:
                extra[words] -= 1
                result.append(item)
        return result

    # A's added use of a moved item: a re-export as visible as the item, or a plain import for A's own uses. It
    # names the B the item moves to.
    source_uses: dict[str, SourceItem] = {}
    source_use_cuts: dict[str, frozenset[int]] = {}
    reexported: set[str] = set()
    for path_a in sorted(sources_of):
        after_a = afters[path_a]
        uses: dict[str, SourceItem] = {}
        for item in use_changes(befores[path_a], after_a):
            paths = use_paths(after_a.words[item.keyword + 1:item.end - 1])
            if len(paths) != 1 or pair_of.get(paths[0][-1], ("", ""))[0] != path_a or scoped_path(
                    paths[0][:-1], after_a.scope) != ("crate", *module_of[pair_of[paths[0][-1]][1]]):
                continue
            if paths[0][-1] in uses:
                raise MoveRefused(f"{path_a} gains more than one plain use of {paths[0][-1]}")
            uses[paths[0][-1]] = item
        for item, _, source, _ in moved:
            use = uses.get(item.name)
            if use is None or source.path != path_a:
                continue
            visibility = visibility_of(after_a.words[use.start:use.keyword])
            if visibility and visibility != item.visibility:
                raise MoveRefused(f"the re-export of {item.name} in {path_a} is `{visibility}`, and the item is "
                                  + f"`{item.visibility or 'private'}`")
            if visibility:
                reexported.add(item.name)
        source_uses.update(uses)
        source_use_cuts[path_a] = frozenset(index for use in uses.values() for index in range(use.start, use.end))

    # B's added uses import only what the moved items name, and nothing from an A of B.
    added_uses: list[FileSpan] = []
    added_leaves: dict[str, set[str]] = {}  # The names that B's added use lines import.
    destination_use_cuts: dict[str, frozenset[int]] = {}
    for path_b in sorted(destinations):
        before_b, after_b = befores[path_b], afters[path_b]
        before_b_names = bare_names(before_b.words, frozenset())
        leaves_b: set[str] = set()
        use_cut: set[int] = set()
        for item in use_changes(before_b, after_b):
            if after_b.words[item.start] != "use":
                raise MoveRefused(f"{path_b} gains a use that is not a plain `use`")
            for path in use_paths(after_b.words[item.keyword + 1:item.end - 1]):
                target = scoped_path(path, after_b.scope)
                if isinstance(target, UnresolvedPath):
                    raise MoveRefused(f"{path_b}: cannot resolve the added use of {'::'.join(path)}")
                for path_a in sorted(source for source, destination in groups if destination == path_b):
                    if target[:len(module_of[path_a]) + 1] == ("crate", *module_of[path_a]):
                        raise MoveRefused(f"{path_b} imports {'::'.join(path)} from {path_a}: an import cycle")
                leaf = path[-2] if path[-1] == "self" else path[-1]
                if leaf not in in_names[path_b]:
                    raise MoveRefused(f"{path_b} gains a use of {leaf}, and no moved item names it")
                if leaf in before_b_names:
                    raise MoveRefused(f"{path_b} already names {leaf}, and its added use can shadow it")
                leaves_b.add(leaf)
            added_uses.append(word_span(after_b, item.start, item.end))
            use_cut.update(range(item.start, item.end))
        added_leaves[path_b] = leaves_b
        destination_use_cuts[path_b] = frozenset(use_cut)

    # A's removed uses served only the items that leave it.
    removed_uses: list[FileSpan] = []
    removed_leaves: dict[str, set[str]] = {}  # The names that A's removed use lines imported.
    removed_use_cuts: dict[str, frozenset[int]] = {}
    for path_a in sorted(sources_of):
        before_a = befores[path_a]
        kept_cut = source_cuts[path_a] | frozenset(index for item in before_a.items if item.kind == "use"
                                                   for index in range(item.start, item.end))
        kept_names = bare_names(before_a.words, kept_cut)
        leaves_a: set[str] = set()
        removed_cut: set[int] = set()
        for item in use_changes(afters[path_a], before_a):
            paths = use_paths(before_a.words[item.keyword + 1:item.end - 1])
            leaves = {path[-2] if path[-1] == "self" else path[-1] for path in paths}
            if before_a.words[item.start] != "use" or not leaves <= out_names[path_a] or leaves & kept_names:
                continue  # The canonical comparison refuses a use that leaves for another reason.
            leaves_a.update(leaves)
            removed_uses.append(word_span(before_a, item.start, item.end))
            removed_cut.update(range(item.start, item.end))
        removed_leaves[path_a] = leaves_a
        removed_use_cuts[path_a] = frozenset(removed_cut)

    # Every name a moved item looks up binds the same in A before and in B after. After the move, the path of an A
    # names a moved item only through its re-export.
    moved_paths: dict[tuple[tuple[str, ...], bool], str] = {}
    for name, (path_a, path_b) in pair_of.items():
        moved_paths[(("crate", *module_of[path_a], name), False)] = name
        moved_paths[(("crate", *module_of[path_b], name), False)] = name
        moved_paths[(("crate", *module_of[path_b], name), True)] = name
        if name in reexported:
            moved_paths[(("crate", *module_of[path_a], name), True)] = name

    def mapped(target: tuple[str, ...] | UnresolvedPath, after: bool) -> tuple[str, ...] | UnresolvedPath:
        if isinstance(target, UnresolvedPath) or (target, after) not in moved_paths:
            return target
        return ("moved", moved_paths[(target, after)])

    refuse_globs(sources, crate, befores, afters, frozenset(module_of[path] for path in sources_of | destinations),
                 library, externs, keywords)

    # A glob at module level gives a moved item every name it imports and does not shadow, so A before and B after
    # glob the same modules and crates.
    def module_globs(text: MoveText) -> frozenset[tuple[str, ...]]:
        used = {index for item in text.items if item.kind == "use" for index in range(item.start, item.end)}
        return frozenset(target for index, target in file_globs(text.path, text.words, text.scope.module, library,
                                                                externs).items() if index in used)

    # A glob in A or B can bring a macro named like a standard derive or an allowed attribute, so only a glob of the
    # standard library or of a crate module that `refuse_globs` accepted stands there.
    for text in sides:
        for target in file_globs(text.path, text.words, text.scope.module, library, externs).values():
            if target[0] != "crate" and target[1] not in STANDARD_CRATES - standard:
                raise MoveRefused(f"{text.path}: a glob use of the crate {target[1]}")
    for path_a, path_b in groups:
        if module_globs(befores[path_a]) != module_globs(afters[path_b]):
            raise MoveRefused(f"{path_a} and {path_b} glob different modules or crates")
        for name in sorted(pair_names[(path_a, path_b)]):
            old = mapped(bound_path(name, befores[path_a].scope), False)
            new = mapped(bound_path(name, afters[path_b].scope), True)
            if isinstance(old, UnresolvedPath) or isinstance(new, UnresolvedPath) or old != new:
                raise MoveRefused(f"{name} does not bind the same in {path_a} and in {path_b}")
    refuse_crate_words(sources, crate, changes, afters)
    # A type name reads the module path of a type the move takes to B. Outside the touched files it stays diagnostic
    # text, as the file and line in the text of a `panic` or an `unwrap` do.
    for text in (*befores.values(), *afters.values()):
        if {"type_name", "type_name_of_val"} & set(text.words):
            raise MoveRefused(f"{text.path} reads a type name")
    for item, _, source, destination in moved:
        path_a, path_b = source.path, destination.path
        words = befores[path_a].words[item.start:item.end]
        for index, word in enumerate(words):
            if word in {"self", "super"} and words[index + 1:index + 2] == ("::",):
                raise MoveRefused(f"{item.title} has a `{word}::` path")
            if word == "crate" and words[index + 1:index + 2] == ("::",) and tuple(
                    words[index + 2:index + 2 * len(module_of[path_a]) + 1:2]) == module_of[path_a]:
                raise MoveRefused(f"{item.title} names {path_a} by its path: an import cycle")
            # The text of a moved item stays the same, so a path in it to a moved item keeps the old module.
            if word in pair_of and words[index - 1:index] == ("::",):
                raise MoveRefused(f"{item.title} names the moved item {word} by a path")
            if word == "use":
                raise MoveRefused(f"{item.title} has a use inside")
            if IDENTIFIER.fullmatch(word) and words[index + 1:index + 2] == ("!",) and words[index + 2:index + 3] in {
                    ("(",), ("[",), ("{",)}:
                # A macro reads its tokens and its context, such as `module_path!()`, which names the module.
                raise MoveRefused(f"{item.title} invokes the macro {word}")
        # A method call or an associated path, `u8::WIDTH`, can find its item through a trait in scope. Only an
        # anchor surely names a module, so a `::` after any other name or after `>` counts as a type's.
        if any((words[index] == "::" and words[index - 1] not in PATH_ANCHORS
                and (IDENTIFIER.fullmatch(words[index - 1]) or words[index - 1] == ">"))
               or (IDENTIFIER.fullmatch(words[index]) and words[index - 1] == "." and words[index + 1] in {"(", "::"})
               for index in range(1, len(words) - 1)):
            before_a, after_b = befores[path_a], afters[path_b]
            # The names the moved items' own use lines in A and in B import bind the same: the check above reads them.
            own_leaves = (removed_leaves[path_a] | added_leaves[path_b]) & pair_names[(path_a, path_b)]
            in_scope = {name for text in (before_a, after_b) for name in (*text.scope.traits, *text.scope.imports)
                        } - own_leaves
            for name in sorted(in_scope):
                if mapped(bound_path(name, before_a.scope), False) != mapped(bound_path(name, after_b.scope), True):
                    raise MoveRefused(f"{name} is in scope in only one of {path_a} and {path_b}: a method call or an "
                                      + f"associated path in {item.title} can change")

    # A still binds each name its other code uses.
    for path_a in sorted(sources_of):
        a_names = bare_names(afters[path_a].words, source_use_cuts[path_a])
        for item, _, source, _ in moved:
            if item.kind == "impl" or source.path != path_a:
                continue
            if item.name in a_names and item.name not in source_uses:
                raise MoveRefused(f"{path_a} still names {item.name}, and no use brings it")
            if item.visibility == "" and item.name in source_uses:
                raise MoveRefused(f"the private item {item.name} is still used outside {pair_of[item.name][1]}")
            if item.visibility == "pub" and item.name not in reexported and sources.externally_reachable(path_a):
                raise MoveRefused(f"the public item {item.name} leaves {path_a} without a re-export, and other "
                                  + f"crates can reach {path_a}")

    # Private members change who can see them. The kept code of A holds the items of A's other pairs.
    files = crate_files(sources, crate, changes)
    for (path_a, path_b), group in pair_moves.items():
        module_a, module_b = module_of[path_a], module_of[path_b]
        before_a = befores[path_a]
        owned = private_members(before_a, tuple(item for item, _, _, _ in group))
        for path, module in files.items():
            if isinstance(module, OutsideCrate) or (module[:len(module_a)] == module_a) == (
                    module[:len(module_b)] == module_b):
                continue
            words = afters[path].words if path in afters else tuple(token.value for token in tokens(sources.text(path)))
            cut = pair_destination_cuts[(path_a, path_b)] if path == path_b else frozenset[int]()
            for index in range(1, len(words)):
                if words[index] in owned and words[index - 1] in {".", "::"} and index not in cut:
                    raise MoveRefused(f"the private member {words[index]} of {owned[words[index]]} changes who can "
                                      + "see it")
        lineage_a = {module_a[:size] for size in range(len(module_a) + 1)}
        lineage_b = {module_b[:size] for size in range(len(module_b) + 1)}
        pair_cut = pair_source_cuts[(path_a, path_b)]
        members = {before_a.words[index] for index in pair_cut if before_a.words[index - 1] in {".", "::"}}
        for path, module in files.items():
            if isinstance(module, OutsideCrate) or module not in lineage_a ^ lineage_b:
                continue
            text = befores[path] if path in befores else move_text(path, False, sources.text(path), module, library,
                                                                   touched=False)
            kept = tuple(item for item in text.items if path != path_a or item.start not in pair_cut)
            for member, title in private_members(text, kept).items():
                if member in members:
                    raise MoveRefused(f"the private member {member} of {title} changes who can see it")

    # Every path to a moved item names A before and B after, or A through its re-export.
    sites: list[FileSpan] = []
    heads: set[str] = set()
    cuts: dict[tuple[str, bool], frozenset[int]] = {}
    for path in sources_of:
        cuts[(path, False)] = source_cuts[path] | removed_use_cuts[path]
        cuts[(path, True)] = source_use_cuts[path]
    for path in destinations:
        cuts[(path, True)] = destination_cuts[path] | destination_use_cuts[path]
    for line in module_lines:
        parent = afters[line.path]
        cuts[(line.path, True)] = cuts.get((line.path, True), frozenset()) | frozenset(word_range(parent, line))
    for item, _, source, destination in moved:
        if item.kind == "impl":
            continue
        path_a, path_b = source.path, destination.path
        module_a, module_b = module_of[path_a], module_of[path_b]
        for path, module in files.items():
            if path in befores:
                texts = (befores[path], afters[path])
            elif re.search(rf"\b{re.escape(item.name)}\b", sources.text(path)):
                texts = (move_text(path, False, sources.text(path), module, library, touched=False),)
            else:
                continue
            for text in texts:
                blocked: tuple[tuple[int, int], ...] | None = None
                for first, index, target in path_sites(text, item.name, cuts.get((path, text.after), frozenset())):
                    if target not in {("crate", *module_a), ("crate", *module_b)}:
                        continue
                    if item.visibility == "":
                        raise MoveRefused(f"the private item {item.name} is still used outside {path_b}")
                    if text.after or path not in befores:
                        if target == ("crate", *module_a) and item.name not in reexported:
                            raise MoveRefused(f"{item.name} leaves {path_a} without a re-export, and {path} still "
                                              + f"uses its old path")
                    elif target != ("crate", *module_a):
                        raise MoveRefused(f"{path}: a path to {item.name} names {path_b} before the move")
                    if path not in befores:
                        continue
                    if blocked is None:
                        blocked = (*attribute_spans(text.words), *macro_spans(text.words, delimiter_ends(text.words)))
                    if any(start <= index < end for start, end in blocked):
                        raise MoveRefused(f"{path}: a path to {item.name} is inside a macro or an attribute")
                    if isinstance(module, OutsideCrate) and library is None:
                        raise MoveRefused(f"{path}: cannot read the library name")
                    spelled = module_path(module_b) if not isinstance(module, OutsideCrate) else "::".join(
                        (library or "", *module_b))
                    sites.append(word_span(text, first, index - 1, spelled))
                    heads.add(text.words[first])
    # A re-pointed path's head names the same module only while no touched file holds the name in a place that can
    # bind it, such as a block item, a generic parameter or `extern crate self as`. The name can stand before `::`,
    # in a use, in a `mod <name>;` line, or after `fn`, which binds in the value namespace only.
    for head in sorted(heads - PATH_ANCHORS):
        for text in (*befores.values(), *afters.values()):
            words = text.words
            in_use = {index for use in use_items(words) for index in range(use.start, use.end)}
            for index, word in enumerate(words):
                if word == head and index not in in_use and words[index + 1:index + 2] != ("::",) and words[
                        index - 1:index] != ("fn",) and (words[index - 1:index], words[index + 1:index + 2]) != (
                        ("mod",), (";",)):
                    raise MoveRefused(f"{text.path}: {head} is declared or bound outside a path")

    proof = ItemMoves(
        tuple(ItemMove(item.kind, item.name, source, destination,
                       word_span(afters[source.path], source_uses[item.name].start, source_uses[item.name].end)
                       if item.kind != "impl" and item.name in source_uses else None)
              for item, _, source, destination in moved),
        tuple(removed_uses), tuple(added_uses), tuple(module_lines), tuple(sites))

    # No other edit: each file is the same once the moves are taken out.
    for change in changes:
        old = edited_text(change.before, proof.spans(change.path, False))
        new = edited_text(change.after, proof.spans(change.path, True))
        if change.status == FileStatus.ADDED:
            # Any `//!` or `/*!` refuses, also one after a block comment on its line, such as `/* a */ //! b`.
            if tokens(new) or re.search(r"//!|/\*!", new):
                raise MoveRefused(f"{change.path}: a new file holds more than the moved items and their uses")
        elif canonical_text(old, keywords) != canonical_text(new, keywords):
            raise MoveRefused(f"{change.path}: an edit is not a moved item, a re-pointed path or a use line")
    return proof


def item_level_expansion(words: tuple[str, ...], cut: frozenset[int], keywords: frozenset[str]) -> str | None:
    """What first outside cut and outside a fn body can expand to code or change how names resolve, or None: a macro
    call, a `macro_rules!` definition, an attribute outside ITEM_ATTRIBUTES or an inner one outside INNER_ATTRIBUTES.
    A word before `!` that is not one of keywords, the keywords of the crate's edition, is a macro call."""
    ends = delimiter_ends(words)
    attributes = dict(attribute_spans(words))
    index = 0
    while index < len(words) - 2:
        word = words[index]
        if index in cut:
            index += 1
            continue
        if index in attributes:
            inner = words[index + 1] == "!"
            name = macro_attribute(words[index:attributes[index]], INNER_ATTRIBUTES if inner else ITEM_ATTRIBUTES)
            if name is not None:
                return f"an {'inner ' if inner else ''}attribute outside a fn body: {name}"
            index = attributes[index]
            continue
        if word == "fn" and IDENTIFIER.fullmatch(words[index + 1]):
            end = item_end(words, ends, index + 1, body=True)
            if end is not None and words[end - 1] == "}":
                index = end
                continue
        if (word == "macro_rules" and words[index + 1] == "!") or (
                IDENTIFIER.fullmatch(word) and word not in keywords and words[index + 1] == "!"
                and words[index + 2] in {"(", "[", "{"}):
            return f"a macro outside a fn body: {word}"
        index += 1
    return None


def untouched_copy(sources: RevisionTree, crate: str, touched: Mapping[str, MoveText],
                   key: tuple[str, str, tuple[str, ...]]) -> bool:
    """Whether a file of the crate the change does not touch has an item with the key."""
    for path in crate_files(sources, crate, ()):
        source = sources.text(path)
        if path in touched or key[1].split()[-1] not in source:
            continue
        words = tuple(token.value for token in tokens(source))
        if any((item.kind, item.title, words[item.start:item.end]) == key for item in source_items(words) or ()):
            return True
    return False


def change_text(text: MoveText, span: FileSpan) -> str:
    return text.text[span.start:span.end]


def crate_files(sources: RevisionTree, crate: str, changes: tuple[FileChange, ...]) -> dict[str, CrateModule]:
    """Each Rust file of the crate after the change, with its module."""
    paths = {path for path in sources.paths if path.endswith(".rs") and sources.crate(path) == crate}
    paths.update(change.path for change in changes)
    return {path: file_module(path, crate, sources) for path in sorted(paths)}


def refuse_crate_words(sources: RevisionTree, crate: str, changes: tuple[FileChange, ...],
                       afters: Mapping[str, MoveText]) -> None:
    """Raise MoveRefused for a file of the crate after the change with a `path` attribute, which moves a module away
    from the file the proof reads for it, with a caller location, which reads the file and line of moved code
    through any chain of `track_caller` functions, or with a `macro_use` that can bring a macro named like a standard
    derive or an allowed attribute, or with an `extern crate` that takes another name, such as `std`. Rust reads
    `r#track_caller` as `track_caller`, so the scan reads each word without its `r#`."""
    for path in crate_files(sources, crate, changes):
        words = tuple(word.removeprefix("r#") for word in (
            afters[path].words if path in afters else tuple(token.value for token in tokens(sources.text(path)))))
        attributes = attribute_spans(words)
        if any(word == "path" for begin, end in attributes for word in words[begin:end]):
            raise MoveRefused(f"{path}: a `path` attribute")
        if {"Location", "track_caller"} & set(words):
            raise MoveRefused(f"{path} reads a caller location")
        for index in range(len(words) - 3):
            if words[index:index + 2] == ("extern", "crate") and words[index + 3] == "as":
                raise MoveRefused(f"{path}: an `extern crate` that takes another name")
        for begin, end in attributes:
            # A `cfg_attr` can hold a `macro_use` at any depth, so each `macro_use` word of the attribute counts.
            if words[begin + 1] == "!":
                continue
            for index in range(begin + 2, end):
                if words[index] != "macro_use":
                    continue
                # A list names the macros it brings. Without one, only an extern crate can bring a derive.
                listed = words[index + 1:index + 2] == ("(",)
                if (listed and (STANDARD_DERIVES | ITEM_ATTRIBUTES) & set(words[begin:end])) or (
                        not listed and extern_crate_follows(words, end, dict(attributes))):
                    raise MoveRefused(f"{path}: a `macro_use` can bring a macro named like a standard derive or "
                                      + "attribute")


def extern_crate_follows(words: tuple[str, ...], cursor: int, attributes: dict[int, int]) -> bool:
    """Whether the item at cursor, after its other attributes and its visibility, is an `extern crate`."""
    while cursor in attributes:
        cursor = attributes[cursor]
    if words[cursor:cursor + 1] == ("pub",):
        cursor += 1
        if words[cursor:cursor + 1] == ("(",):
            cursor = words.index(")", cursor) + 1
    return words[cursor:cursor + 2] == ("extern", "crate")


def attribute_name(words: tuple[str, ...], begin: int) -> str:
    """The first path word of the attribute that starts at begin, without an `r#` prefix: Rust reads `#[r#cfg]` as
    `#[cfg]`."""
    name = words[begin + 3] if words[begin + 1] == "!" else words[begin + 2]
    return name.removeprefix("r#")


def attribute_comment(text: str, spans: tuple[TokenSpan, ...], begin: int, end: int) -> bool:
    """Whether a comment sits between the tokens begin..end of an attribute, where a reader of the raw text can take
    it for code or miss the code after it."""
    return any(text[spans[index].end:spans[index + 1].start].strip() for index in range(begin, end - 1))


def macro_attribute(words: tuple[str, ...], allowed: frozenset[str] = INERT_ATTRIBUTES) -> str | None:
    """The path in the attribute words that a macro or a cfg can read, or None for an attribute that reads nothing:
    one allowed name (by default a doc, a lint level or a code generation hint), or a derive of standard traits only."""
    start = end = 3 if words[1] == "!" else 2
    while end < len(words) - 1 and (words[end] == "::" or IDENTIFIER.fullmatch(words[end])):
        end += 1
    path = words[start:end]
    if path != ("derive",):
        return None if len(path) == 1 and path[0] in allowed else "".join(path)
    if words[end:end + 1] != ("(",) or words[-2] != ")":
        return "derive"
    for derive in " ".join(words[end + 1:-2]).split(","):
        entry = tuple(derive.split())
        if entry and (entry[-1] not in STANDARD_DERIVES or entry[:-1] not in STANDARD_PATH_PREFIXES):
            return "".join(entry)
    return None


def configured(words: tuple[str, ...], start: int, end: int, inner: bool) -> bool:
    """Whether words start..end hold a `cfg` or `cfg_attr` attribute: an inner one with inner, else an outer one."""
    return any(start <= begin < end and (words[begin + 1] == "!") == inner
               and attribute_name(words, begin) in {"cfg", "cfg_attr"} for begin, _ in attribute_spans(words))


def module_chain(module: tuple[str, ...], sources: RevisionTree, crate: str,
                 texts: Mapping[str, MoveText]) -> str | None:
    """Why the crate does not compile the module's file plainly in the tree of texts, or None.

    Each module on the path from the crate root needs a `mod <name>;` line in its parent's file whose attributes are
    all inert and hold no comment, and no file below the crate root on the path may hold an inner `cfg` or
    `cfg_attr`.
    """
    files = crate_files(sources, crate, tuple())
    files.update({path: text.scope.module for path, text in texts.items()})
    for size in range(len(module) + 1):
        declared = False
        for path, file_module in files.items():
            if file_module != module[:size]:
                continue
            text = texts[path].text if path in texts else sources.text(path)
            spans = texts[path].spans if path in texts else token_spans(text)
            words = tuple(span.token.value for span in spans)
            if size and configured(words, 0, len(words), inner=True):
                return f"{path}: a cfg attribute on the path to {module_path(module)}"
            # An inner attribute such as `no_implicit_prelude` reaches the child modules. The crate root's reach A
            # and B alike.
            for begin, end in attribute_spans(words) if size else ():
                name = macro_attribute(words[begin:end], INNER_ATTRIBUTES) if words[begin + 1] == "!" else None
                if name is not None:
                    return f"{path}: the inner attribute {name} on the path to {module_path(module)}"
            if size == len(module):
                continue
            items = source_items(words)
            if items is None:
                return f"{path}: cannot read its items"
            for item in items:
                if item.kind == "mod" and item.name == module[size] and words[item.end - 1] == ";":
                    if configured(words, item.start, item.keyword, inner=False):
                        return f"{path}: a cfg attribute on the path to {module_path(module)}"
                    for begin, end in attribute_spans(words[item.start:item.keyword]):
                        name = macro_attribute(words[item.start + begin:item.start + end])
                        if name is not None:
                            return f"{path}: the attribute {name} on the path to {module_path(module)}"
                        if attribute_comment(text, spans, item.start + begin, item.start + end):
                            return f"{path}: a comment in an attribute on the path to {module_path(module)}"
                    declared = True
        if size < len(module) and not declared:
            return f"no file declares `mod {module[size]};` on the path to {module_path(module)}"
    return None


def refuse_globs(sources: RevisionTree, crate: str, befores: Mapping[str, MoveText], afters: Mapping[str, MoveText],
                 modules: frozenset[tuple[str, ...]], library: str | None, externs: frozenset[str],
                 keywords: frozenset[str]) -> None:
    """Raise MoveRefused for a glob use in a file of the crate, before or after the change, that can import from one
    of modules, the modules the items move from and to.

    One rule holds for touched and untouched files: a glob is accepted only when it imports from the standard
    library, from another crate, or from a module of the crate that exports only its own items, which the move does
    not change (`glob_module_problem`, which reads keywords, the keywords of the crate's edition).
    """
    files = {**crate_files(sources, crate, ()), **{path: text.scope.module for path, text in afters.items()}}
    problems: dict[tuple[str, ...], str | None] = {}
    for path, module in files.items():
        if path in afters:
            sides = (befores[path].words, afters[path].words)
        elif "*" in sources.text(path) and holds_glob_use(plain_words(sources.text(path))):
            sides = (plain_words(sources.text(path)),)
        else:
            continue
        for words in sides:
            for target in file_globs(path, words, module, library, externs).values():
                if target[0] != "crate":
                    continue
                if target[1:] not in problems:
                    problems[target[1:]] = glob_module_problem(target[1:], files, sources, afters, modules,
                                                               keywords)
                problem = problems[target[1:]]
                if problem is not None:
                    raise MoveRefused(f"{path}: a glob use of {module_path(target[1:])}, {problem}")


def holds_glob_use(words: tuple[str, ...]) -> bool:
    """Whether a `use` of words has a `*` before its `;`. It reads words, so a comment or a string cannot hide the
    glob: in `use provider:: // a;` and the next line ` *;`, a text search stops at the `;` of the comment. A caller
    can skip a text with no `*`, as each `*` word is in the text."""
    in_use = False
    for word in words:
        if word in {"use", ";"}:
            in_use = word == "use"
        elif word == "*" and in_use:
            return True
    return False


def file_globs(path: str, words: tuple[str, ...], module: CrateModule, library: str | None,
               externs: frozenset[str]) -> dict[int, tuple[str, ...]]:
    """What each glob use of a file imports from, by the index of its `*`: `("crate", *module)`, `("extern", *path)`
    for the standard library or another crate, or `("outside", *path)` for an anchored path in a file outside the
    crate's module tree, which cannot name the crate's modules. Raises MoveRefused for a glob it cannot resolve.

    Another first name names a crate only when it is in externs: a dependency key or a sysroot crate that no file of
    the crate, before or after, holds anywhere but right before `::`, where an item, a use, a `mod`, an `as` or a
    `macro_rules!` body can bind it. A glob inside a macro is refused: a macro reads it as tokens.
    """
    trees = use_trees(words, code_item_starts(words))
    for index, word in enumerate(words):
        if word == "use":
            end = next((cursor for cursor in range(index, len(words)) if words[cursor] == ";"), len(words))
            if any(words[star] == "*" and star not in trees.prefixes for star in range(index, end)):
                raise MoveRefused(f"{path}: a glob use the proof cannot read")
    context = import_context(words)
    globs: dict[int, tuple[str, ...]] = {}
    for index, prefix in trees.prefixes.items():
        if words[index] != "*":
            continue
        head = prefix[0] if prefix else ""
        if head in PATH_ANCHORS and isinstance(module, OutsideCrate):
            globs[index] = ("outside", *prefix)
        elif head in PATH_ANCHORS and not isinstance(module, OutsideCrate):
            inline = tuple(name.split("@")[0] for name in context.scopes[index].module)
            target = resolved_module(prefix, (*module, *inline))
            if isinstance(target, UnresolvedPath):
                raise MoveRefused(f"{path}: a glob use the proof cannot resolve")
            globs[index] = ("crate", *target)
        elif isinstance(module, OutsideCrate) and head == library:
            globs[index] = ("crate", *prefix[1:])
        elif head in externs:
            globs[index] = ("extern", *prefix)
        else:
            raise MoveRefused(f"{path}: a glob use the proof cannot resolve")
    return globs


def glob_module_problem(target: tuple[str, ...], files: Mapping[str, CrateModule], sources: RevisionTree,
                        afters: Mapping[str, MoveText], modules: frozenset[tuple[str, ...]],
                        keywords: frozenset[str]) -> str | None:
    """Why a glob of the crate module target can import from one of modules, or None.

    The module must not be one of modules or a parent of one, whose glob brings the child module's name. Its file must
    be its own and untouched, and hold no glob use, no public use and no macro, which can each export another module's
    items, and no visibility but private, `pub(crate)` and `pub`, which A and B can see alike.
    """
    if any(moved[:len(target)] == target for moved in modules):
        return "a module the move changes or a parent of one"
    paths = [path for path, module in files.items() if module == target]
    if len(paths) != 1:
        return "which has no file of its own"
    if paths[0] in afters:
        return "which the change touches"
    words = tuple(token.value.removeprefix("r#") for token in tokens(sources.text(paths[0])))
    if any("*" in words[use.start:use.end] or "pub" in words[use.start:use.end] for use in use_items(words)):
        return "which holds a glob use or a public use"
    if any(words[index:index + 2] == ("pub", "(") and words[index + 2:index + 4] != ("crate", ")")
           for index in range(len(words))):
        return "which holds a restricted visibility"
    expansion = item_level_expansion(words, frozenset(), keywords)
    if expansion is not None:
        return f"which has {expansion}"
    return None


def path_sites(text: MoveText, name: str,
               cut: frozenset[int]) -> tuple[tuple[int, int, tuple[str, ...] | UnresolvedPath], ...]:
    """Each module path written before `::name` outside cut: its first word, the word of the name and what it names.

    A path in a use group, or a path in an inline module that does not start at an anchor, raises MoveRefused.
    """
    words = text.words
    found = [index for index, word in enumerate(words) if word == name and index not in cut and index >= 2
             and words[index - 1] == "::" and words[index + 1:index + 2] != ("!",)]
    if not found:
        return ()
    context = import_context(words)
    trees = use_trees(words, code_item_starts(words))
    result: list[tuple[int, int, tuple[str, ...] | UnresolvedPath]] = []
    for index in found:
        first = index
        while first >= 2 and words[first - 1] == "::" and IDENTIFIER.fullmatch(words[first - 2]):
            first -= 2
        if first and words[first - 1] == "::":
            continue  # A path after a type, `<T>::name`, or after `::`, an extern crate.
        prefix = words[first:index - 1:2]
        if index in trees.prefixes and trees.prefixes[index] != prefix:
            raise MoveRefused(f"{text.path}: a use group names {name}")
        inline = tuple(module.split("@")[0] for module in context.scopes[index].module)
        target = scoped_path(prefix, text.scope, inline)
        if isinstance(target, UnresolvedPath):
            raise MoveRefused(f"{text.path}: cannot resolve the path {'::'.join(prefix)}::{name}")
        result.append((first, index, target))
    return tuple(result)


def judge(finding: Finding, before: RevisionTree) -> Mechanical | Review:
    return judge_changes(finding.changes, before, prove_item_moves(finding.changes, before))


def moved_files(changes: tuple[FileChange, ...], before: RevisionTree) -> frozenset[str]:
    """The files of the change that a proven item move explains, when `judge` finds the change mechanical: each file
    with a span of the move. A file with no span is not the move's, and any edit that judge refuses keeps them all out.
    """
    moves = prove_item_moves(changes, before)
    if isinstance(moves, NoItemMoves) or isinstance(judge_changes(changes, before, moves), Review):
        return frozenset()
    return frozenset(change.path for change in changes
                     if moves.spans(change.path, False) or moves.spans(change.path, True))


def lint_removal_files(changes: tuple[FileChange, ...], before: RevisionTree) -> frozenset[str]:
    """The files of the change whose whole edit is a proven lint name removal (`prove_lint_removals`), when `judge`
    finds the whole change mechanical. A file that adds an allow or changes code is not one, and neither is a file
    of a crate that `crate_emits_unread_code` finds."""
    if isinstance(judge_changes(changes, before, prove_item_moves(changes, before)), Review):
        return frozenset()
    crates = {before.crate(change.path) for change in changes}
    unread = {crate for crate in crates if crate_emits_unread_code(before, crate, changes)}
    return frozenset(change.path for change in changes
                     if before.crate(change.path) not in unread and stale_lint_removal(change, before))


# The words that can put code the generator does not read into an allow's scope, and a text prefilter for them.
UNREAD_CODE_WORDS = frozenset({"include", "macro", "macro_rules"})
UNREAD_CODE_PATTERN = re.compile(r"\b(?:include|macro(?:_rules)?)\b")


def crate_emits_unread_code(before: RevisionTree, crate: str, changes: tuple[FileChange, ...]) -> bool:
    """Whether a Rust file of the crate (`crate_files`), before or after the change, holds an `include`,
    `macro_rules` or `macro` word, `r#` stripped. A macro defined in any file of the crate, such as one in a parent
    module that a child module file calls, can emit `.unwrap(` into an allow's scope in another file, and any
    `include` word (a call, a use leaf, an alias source, a path segment) can bring a file that no scan reads. The
    generator reads one file, so this crate-wide reading is the gate's. A comment or a string does not count."""
    changed = {change.path: (change.before, change.after) for change in changes}
    for path in crate_files(before, crate, changes):
        texts = changed.get(path) or (before.text(path),)
        if any(UNREAD_CODE_PATTERN.search(text) and UNREAD_CODE_WORDS & set(plain_words(text)) for text in texts):
            return True
    return False


# Line breaks other than `\n` would move the generator's lines and columns away from the tokens' offsets.
OTHER_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")


def stale_lint_removal(change: FileChange, before: RevisionTree) -> bool:
    """Whether the whole edit of the file removes stale lint names from test-scope allow attributes. Each removed name
    must be one that the `test_allow_boilerplate` generator reports stale for that attribute in the before text, and
    the attribute must be in a test scope: inside or on an item that `cfg_test_items` finds, or in a file that
    `test_file` finds. A kept attribute keeps its prefix, its other lint names and its reason token for token; only a
    trailing comma may change. An attribute whose scope can reach code the generator does not read is not explained
    (`BoilerplateAllow.local`): an attribute on an out-of-line `mod x;`, one whose scope declares such a module, one
    in a file with any `include` word or a `macro_rules!` or `macro` item, and an inner attribute, which the
    generator does not read (nor an `expect`). Neither is an attribute in a macro token tree, which the macro can
    put anywhere."""
    proof = prove_lint_removals(change)
    if not removes_only_lint_names(change, proof) or OTHER_LINE_BREAKS & set(change.before):
        return False
    allows = {(allow.line, allow.column): allow for allow in boilerplate_allows(mask_source(change.before))}
    try:
        spans = token_spans(change.before)
    except UnreadableTokenSpans:
        return False
    old, new = tokens(change.before), tokens(change.after)
    words = tuple(token.value.removeprefix("r#") for token in old)
    tests = cfg_test_items(words)
    macros = macro_spans(words, delimiter_ends(words))
    whole_file: bool | None = None
    for previous, following in proof.pairs:
        if not spans:
            return False
        offset = spans[previous.start].start
        line_start = change.before.rfind("\n", 0, offset) + 1
        allow = allows.get((change.before.count("\n", 0, offset) + 1, offset - line_start))
        removed = previous.names - (following.names if following else frozenset())
        if (allow is None or not allow.local or not removed
                or not {"".join(name) for name in removed} <= set(allow.stale)
                or any(start < previous.start < end for start, end in macros)):
            return False
        if not any(start <= previous.start < end for start, end in tests):
            whole_file = test_file(before, change.path) if whole_file is None else whole_file
            if not whole_file:
                return False
        if following is not None and lint_arguments(new[following.start:following.end]) != [
                group for group in lint_arguments(old[previous.start:previous.end])
                if tuple(token.value for token in group) not in removed]:
            return False
        if following is not None and (old[previous.start:previous.start + 4] != new[following.start:following.start + 4]
                                      or old[previous.end - 2:previous.end] != new[following.end - 2:following.end]):
            return False
    return True


# The words of the one attribute that gates an item to test builds.
CFG_TEST = ("#", "[", "cfg", "(", "test", ")", "]")


def cfg_test_items(words: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    """The word range of each item that an outer attribute of exactly `#[cfg(test)]` gates, words without `r#`: from
    the first outer attribute of the item to the end of its body, or to its `;` when no body comes first. A
    `cfg(test)` inside `cfg_attr`, `any()`, `all()` or `not()` is another attribute, and an attribute that is not at
    an item start outside every macro token tree (`code_item_starts`) gates no item that the scan reads."""
    ends = delimiter_ends(words)
    starts = code_item_starts(words)
    outer = [span for span in attribute_spans(words) if words[span[0] + 1] != "!"]
    result: list[tuple[int, int]] = []
    index = 0
    while index < len(outer):
        first = index
        while index + 1 < len(outer) and outer[index + 1][0] == outer[index][1]:
            index += 1
        stack = outer[first:index + 1]
        index += 1
        if stack[0][0] not in starts or all(words[begin:end] != CFG_TEST for begin, end in stack):
            continue
        cursor = stack[-1][1]
        while cursor < len(words) and words[cursor] not in {"{", ";", ")", "]", "}"}:
            # A `;` or `{` in a paren or bracket, as in `-> [u8; 4]`, is not the item's own.
            cursor = ends.get(cursor, len(words)) + 1 if words[cursor] in {"(", "["} else cursor + 1
        if cursor < len(words) and words[cursor] == ";":
            result.append((stack[0][0], cursor + 1))
        elif cursor < len(words) and words[cursor] == "{" and cursor in ends:
            result.append((stack[0][0], ends[cursor] + 1))
    return tuple(result)


def test_file(sources: RevisionTree, path: str) -> bool:
    """Whether the whole file is test code of its package, the folder of the nearest Cargo.toml when that file has a
    [package] table: a file under the package's tests/ folder, or a module file under src/ that every out-of-line
    `mod` declaring it puts in a `cfg_test_items` item, or in a file that is itself such a module file. A crate root
    and a file under src/bin/ are not. The answer is no when the proof cannot read the package's module tree: the
    manifest is missing or unreadable, sets a target path or a build script (`crate_manifest`), or a file of the
    package has a `path` attribute, any `include` word (a call, a use leaf, an alias source or a path segment, as
    `use std::include as load;` calls it under another name), a `macro` item or a `mod` in a macro token tree."""
    crate = sources.crate(path)
    try:
        manifest = crate_manifest(sources, crate)
    except MoveRefused:
        return False
    package = manifest.get("package")
    if not isinstance(package, dict) or "build" in package:
        return False
    files = {file: plain_words(sources.text(file)) for file in crate_files(sources, crate, ())}
    for words in files.values():
        macros = macro_spans(words, delimiter_ends(words))
        attributes = attribute_spans(words)
        if ("macro" in words or "include" in words
                or any(word == "path" for begin, end in attributes for word in words[begin:end])
                or any(words[index] == "mod" for start, end in macros for index in range(start, end))):
            return False
    if PurePosixPath(path).relative_to(crate).parts[:1] == ("tests",):
        return True
    declarations: dict[str, list[tuple[str, bool]]] = {}
    for file, words in files.items():
        ends = delimiter_ends(words)
        tests = cfg_test_items(words)
        inline = [(index, ends[index + 2], words[index + 1]) for index in range(len(words) - 2)
                  if words[index] == "mod" and words[index + 2] == "{" and index + 2 in ends]
        folder = PurePosixPath(file).parent
        for index in range(len(words) - 2):
            if words[index] != "mod" or words[index + 2] != ";" or not IDENTIFIER.fullmatch(words[index + 1]):
                continue
            nesting = [name for start, end, name in inline if start < index < end]
            # The file can be a crate root or `mod.rs`, whose modules sit beside it, or another module file.
            for base in (folder, folder / PurePosixPath(file).stem):
                target = base.joinpath(*nesting, words[index + 1])
                for loaded in (str(target.with_suffix(".rs")), str(target / "mod.rs")):
                    declarations.setdefault(loaded, []).append(
                        (file, any(start <= index < end for start, end in tests)))

    def test_module(file: str, seen: frozenset[str]) -> bool:
        module = crate_module(file, PurePosixPath(crate) / "src")
        return (not isinstance(module, OutsideCrate) and bool(module) and file in declarations
                and all(gated or (parent not in seen and test_module(parent, seen | {parent}))
                        for parent, gated in declarations[file]))

    return test_module(path, frozenset({path}))


def lint_arguments(attribute: tuple[Token, ...]) -> list[tuple[Token, ...]]:
    """The comma-separated arguments of an outer `#[level(...)]` attribute, with no empty group for a trailing comma."""
    groups: list[tuple[Token, ...]] = []
    current: list[Token] = []
    for token in attribute[4:-2]:
        if token.value == ",":
            groups.append(tuple(current))
            current = []
        else:
            current.append(token)
    if current:
        groups.append(tuple(current))
    return groups


def removes_only_lint_names(change: FileChange, proof: LintAttributeProof) -> bool:
    """Whether the proof removes at least one lint name and the two sides match once its rewrites are made."""
    return bool(proof.before) and rewrite_tokens(tokens(change.before), proof.before) == rewrite_tokens(
        tokens(change.after), proof.after)


def changes_serialized_name(change: FileChange) -> bool:
    """Whether a removed or an added line holds a word of a `serde` or `reflect` attribute, or a `rename =`."""
    removed: set[int] = set()
    added: set[int] = set()
    offset = 0
    for hunk in change.hunks:
        new_start = hunk.old_start + (not hunk.old_count) + offset
        removed.update(range(hunk.old_start, hunk.old_start + len(hunk.removed)))
        added.update(range(new_start, new_start + len(hunk.added)))
        offset += len(hunk.added) - hunk.old_count
    return serialized_name_on(change.before, removed) or serialized_name_on(change.after, added)


def serialized_name_on(text: str, changed: set[int]) -> bool:
    """Whether a token on a changed line is in an attribute that holds a `serde` or `reflect` word at any depth, as in
    `#[cfg_attr(feature = "s", serde(skip))]`, or is a `rename` word or the `=` after it. It reads the tokens of the
    whole text, each word without its `r#`, so a comment cannot hide an attribute and a comment or a string cannot make
    one: `#/* note */[serde(skip)]` is an attribute, and `// #[serde(skip)]` is not."""
    if not changed:
        return False
    tokenized = tokenized_text(text)
    words = tuple(token.value.removeprefix("r#") for token in tokenized.tokens)
    touched = {index for index, (token, line) in enumerate(zip(tokenized.tokens, tokenized.lines))
               if not changed.isdisjoint(range(line, line + token.value.count("\n") + 1))}
    if any(words[index:index + 2] == ("rename", "=") and {index, index + 1} & touched for index in range(len(words))):
        return True
    return any(not touched.isdisjoint(range(begin, end)) and {"serde", "reflect"} & set(words[begin:end])
               for begin, end in attribute_spans(words))


def judge_changes(changes: tuple[FileChange, ...], before: RevisionTree,
                  moves: ItemMoves | NoItemMoves) -> Mechanical | Review:
    reasons: list[str] = []
    # A proven item move is mechanical at any size: the checks judge only what is left once its spans are taken out.
    if isinstance(moves, ItemMoves):
        changes = tuple(change for change in (unmoved_change(change, moves) for change in changes)
                        if change.before != change.after)
    semantic_changes = {change.path: erase_proven_parameters(change) for change in changes}
    proofs = normalizations(tuple(semantic_changes.values()), before)
    if len(changes) > 10 or len({before.crate(change.path) for change in changes}) > 1:
        if any(tokens(change.before) != tokens(change.after) for change in changes):
            reasons.append("check 4: rename exceeds one crate or 10 files")
    for change in changes:
        if sum(len(hunk.removed) + len(hunk.added) for hunk in change.hunks) >= FILE_CHANGED_LINE_LIMIT:
            reasons.append(f"check 5: {change.path}: {FILE_CHANGED_LINE_LIMIT} or more changed lines (size only)")
        proof = proofs[change.path]
        semantic_change = semantic_changes[change.path]
        lines = changed_text_without_lint_attributes(change, proof.lint_attributes)
        words = {token.value for token in tokens(lines)}
        lint_only = removes_only_lint_names(change, proof.lint_attributes)
        if change.status is not FileStatus.MODIFIED or ("mod" in words and not lint_only):
            reasons.append("check 1: file or module layout changes")
        glob_reexported = before.has_public_glob(before.crate(change.path))
        public_before = shapes(semantic_change.before, True, glob_reexported=glob_reexported)
        public_after = shapes(change.after, True, glob_reexported=glob_reexported)
        if before.externally_reachable(change.path) and public_before != public_after:
            reasons.append("check 2: externally reachable item changes")
        normalized = normalized_source(semantic_change, proof)
        if shapes(" ".join(token.value for token in normalized), False) != shapes(change.after, False):
            reasons.append("check 3: type, fields, variants, generics or trait impl changes")
        if flagged_hunks(semantic_change, proof):
            reasons.append("check 6: condition, literal or control flow flagged")
        if changes_macro_invocation(semantic_change):
            reasons.append("check 6: macro invocation requires review")
        if documented_text(change.before) != documented_text(change.after) or changes_serialized_name(change):
            reasons.append("check 6: serialized, reflected, configured or documented name flagged")
        old_tokens = without_trailing_commas(without_derives(normalized))
        new_tokens = without_trailing_commas(without_derives(
            rewrite_tokens(tokens(change.after), proof.rewrites.after)))
        if old_tokens != new_tokens or any(token.kind == "unknown" for token in (*old_tokens, *new_tokens)):
            reasons.append("check 6: call target, method or other change not proven equivalent")
        if not change.path.endswith(".rs") and change.before != change.after:
            reasons.append("check 6: non-Rust change requires review")
        if not change.hunks:
            reasons.append("check 6: binary or metadata-only change requires review")
    return Review(tuple(dict.fromkeys(reasons))) if reasons else Mechanical()


def unmoved_change(change: FileChange, moves: ItemMoves) -> FileChange:
    """The change with the spans the moves explain taken out of both sides: a modified change from what is left before
    to what is left after. A new file that holds only moved items and their uses is left with no code."""
    old = edited_text(change.before, moves.spans(change.path, False))
    new = edited_text(change.after, moves.spans(change.path, True))
    patch = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
    return FileChange(FileStatus.MODIFIED, change.old_path, change.path, old, new, parse_hunks(patch))


def worktree_commit(repo: Path) -> str:
    """Snapshot tracked and non-ignored work through a disposable index."""
    head = git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    index = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    with tempfile.TemporaryDirectory(prefix="style-footprint-") as directory:
        temporary_index = Path(directory) / "index"
        if index.exists():
            # Git uses the index timestamp to recheck files whose stat data may
            # still match after a same-size edit. Preserve that timestamp.
            _ = shutil.copy2(index, temporary_index)
        environment = {**os.environ, "GIT_INDEX_FILE": str(temporary_index)}

        def snapshot_git(*arguments: str) -> str:
            result = subprocess.run(["git", "-C", str(repo), *arguments], env=environment,
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                raise CheckCouldNotRun(result.stderr.strip() or f"git {' '.join(arguments)} failed")
            return result.stdout.strip()

        _ = snapshot_git("add", "-A", "--", ":/")
        tree = snapshot_git("write-tree")
        if tree == git(repo, "rev-parse", f"{head}^{{tree}}").strip():
            return head
        return snapshot_git("commit-tree", "--no-gpg-sign", tree, "-p", head, "-m", "style-footprint")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    _ = check.add_argument("--repo", type=Path, required=True)
    _ = check.add_argument("--base", required=True)
    after = check.add_mutually_exclusive_group(required=True)
    _ = after.add_argument("--head")
    _ = after.add_argument("--worktree", action="store_true")
    _ = check.add_argument("--finding-map", type=Path)
    arguments = parser.parse_args(argv)
    repo = cast(Path, arguments.repo)
    base = cast(str, arguments.base)
    finding_map = cast(Path | None, arguments.finding_map)
    try:
        head = worktree_commit(repo) if cast(bool, arguments.worktree) else git(
            repo, "rev-parse", "--verify", f"{cast(str, arguments.head)}^{{commit}}",
        ).strip()
        base = folded_base(repo, base, head)
        changes = changes_between(repo, base, head)
        findings = (Finding(WholeDiff(), changes),) if finding_map is None else finding_groups(finding_map, changes)
        before = RevisionTree(repo, base)
        needs_review = False
        for finding in findings:
            decision = judge(finding, before)
            prefix = f"{finding.label.finding_id}: " if isinstance(finding.label, MappedFinding) else ""
            if isinstance(decision, Review):
                needs_review = True
                print(prefix + "review: " + "; ".join(decision.reasons))
            else:
                print(prefix + "mechanical")
        return int(needs_review)
    except (CheckCouldNotRun, OSError, StopIteration) as error:
        print(f"error: {error}")
        return 2
    except (FootprintError, ValueError) as error:
        print(f"review: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
