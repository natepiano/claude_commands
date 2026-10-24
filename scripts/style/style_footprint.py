#!/usr/bin/env python3
"""Conservatively separate mechanical style diffs from findings needing review."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from functools import lru_cache
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

RAW_STRING = re.compile(r'(?:br|cr|r)(#*)"')
QUOTED_LITERAL = re.compile(r'''(?:b|c)?"(?:\\.|[^"\\])*"|b?'(?:\\.|[^'\\\n])' ''', re.VERBOSE | re.DOTALL)
NUMBER = re.compile(r"\d[\w.]*(?:[+-]\d+)?")
IDENTIFIER = re.compile(r"(?:r#)?[A-Za-z_][A-Za-z_0-9]*")
PUNCTUATION = re.compile(r"::|->|=>|==|!=|<=|>=|&&|\|\||[{}()\[\];:,.<>!&|+*/%=\-?#@'$^~]")
FILE_CHANGED_LINE_LIMIT = 200
# The std traits whose derives add no names to the item they decorate.
STD_DERIVES = frozenset({"Clone", "Copy", "PartialEq", "Eq", "Hash", "PartialOrd", "Ord", "Default", "Debug"})


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
            return ()
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
        cursor = 0
        for span in removed:
            previous = old_by_start[span[0]]
            if cursor < len(added):
                following = new_by_start[added[cursor][0]]
                if (previous.level == following.level and previous.inner == following.inner
                        and following.names <= previous.names and following.reason in {(), previous.reason}):
                    pending.append(TokenRewrite(following.start, following.end, ()))
                    cursor += 1
        if cursor == len(added):
            before.extend(TokenRewrite(start, end, ()) for start, end in removed)
            after.extend(pending)
    return LintAttributeProof(tuple(dict.fromkeys(before)), tuple(dict.fromkeys(after)))


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
    added, removed = new_paths - old_paths, old_paths - new_paths
    candidates = [item for item in candidates if item.path in added]
    replaced_paths = {path for item in removed_items for path in item.paths}
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", path[-1]) for item in added_items for path in item.paths
           if path not in replaced_paths):
        return ImportProof()
    if len({path[-1] for path in added}) != len(added):
        return ImportProof()
    old_use_indices = {i for item in old_uses for i in range(item.start, item.end)}
    new_use_indices = {i for item in new_uses for i in range(item.start, item.end)}
    for path in removed:
        if any(word == path[-1] and i not in new_use_indices for i, word in enumerate(after)):
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
    spans = token_spans(change.before)
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
        has_glob = re.search(r"\buse\b[^;]*\*", " ".join(words)) is not None
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


def judge(finding: Finding, before: RevisionTree) -> Mechanical | Review:
    reasons: list[str] = []
    semantic_changes = {change.path: erase_proven_parameters(change) for change in finding.changes}
    proofs = normalizations(tuple(semantic_changes.values()), before)
    if len(finding.changes) > 10 or len({before.crate(change.path) for change in finding.changes}) > 1:
        if any(tokens(change.before) != tokens(change.after) for change in finding.changes):
            reasons.append("check 4: rename exceeds one crate or 10 files")
    for change in finding.changes:
        if sum(len(hunk.removed) + len(hunk.added) for hunk in change.hunks) >= FILE_CHANGED_LINE_LIMIT:
            reasons.append(f"check 5: {change.path}: {FILE_CHANGED_LINE_LIMIT} or more changed lines (size only)")
        proof = proofs[change.path]
        semantic_change = semantic_changes[change.path]
        lines = changed_text_without_lint_attributes(change, proof.lint_attributes)
        words = {token.value for token in tokens(lines)}
        lint_only = bool(proof.lint_attributes.before) and rewrite_tokens(
            tokens(change.before), proof.lint_attributes.before,
        ) == rewrite_tokens(tokens(change.after), proof.lint_attributes.after)
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
        if documented_text(change.before) != documented_text(change.after) or re.search(r"#\s*\[\s*(?:serde|reflect)|\brename\s*=", lines):
            reasons.append("check 6: serialized, reflected, configured or documented name flagged")
        old_tokens = without_derives(normalized)
        new_tokens = without_derives(rewrite_tokens(tokens(change.after), proof.rewrites.after))
        if old_tokens != new_tokens or any(token.kind == "unknown" for token in (*old_tokens, *new_tokens)):
            reasons.append("check 6: call target, method or other change not proven equivalent")
        if not change.path.endswith(".rs") and change.before != change.after:
            reasons.append("check 6: non-Rust change requires review")
        if not change.hunks:
            reasons.append("check 6: binary or metadata-only change requires review")
    return Review(tuple(dict.fromkeys(reasons))) if reasons else Mechanical()


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
