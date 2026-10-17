#!/usr/bin/env python3
"""Conservatively separate mechanical style diffs from findings needing review."""

from __future__ import annotations

import argparse
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


class FootprintError(Exception):
    pass


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
        raise FootprintError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
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
                raise FootprintError(f"git merge-tree: {result.stderr.strip()} (exit {result.returncode})")
            tree = result.stdout.splitlines()[0]
            # Subsequent folds need ancestry, not merely the intermediate tree.
            environment = dict(os.environ, GIT_AUTHOR_NAME="Style footprint", GIT_AUTHOR_EMAIL="style@example.invalid",
                               GIT_COMMITTER_NAME="Style footprint", GIT_COMMITTER_EMAIL="style@example.invalid")
            commit = subprocess.run(["git", "-C", str(repo), "commit-tree", "--no-gpg-sign", tree, "-p", current, "-p", parent],
                                    input="Fold merged-in work for style comparison\n", capture_output=True,
                                    text=True, env=environment, check=False)
            if commit.returncode:
                raise FootprintError(commit.stderr.strip())
            current = commit.stdout.strip()
    return current


def tokens(text: str) -> tuple[Token, ...]:
    """Keep literals opaque; comments cannot turn their contents into Rust code."""
    result: list[Token] = []
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
                result.append(Token("unterminated comment", "unknown"))
            continue
        raw = RAW_STRING.match(text, cursor)
        if raw:
            terminator = '"' + raw.group(1)
            end = text.find(terminator, raw.end())
            finish = len(text) if end < 0 else end + len(terminator)
            result.append(Token(text[cursor:finish], "literal" if end >= 0 else "unknown"))
            cursor = finish
            continue
        quoted = QUOTED_LITERAL.match(text, cursor)
        if quoted:
            result.append(Token(quoted.group(), "literal"))
            cursor = quoted.end()
            continue
        number = NUMBER.match(text, cursor)
        if number:
            result.append(Token(number.group(), "literal"))
            cursor = number.end()
            continue
        identifier = IDENTIFIER.match(text, cursor)
        if identifier:
            word = identifier.group()
            result.append(Token(word, "literal" if word in {"true", "false"} else "identifier"))
            cursor = identifier.end()
            continue
        punctuation = PUNCTUATION.match(text, cursor)
        if punctuation:
            result.append(Token(punctuation.group(), "punctuation"))
            cursor = punctuation.end()
            continue
        result.append(Token(text[cursor], "unknown"))
        cursor += 1
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
    words = " ".join(token.value for token in tokens(source))
    return re.search(r"\bpub use\b[^;]*\*", words) is not None


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
    for start, end in spans:
        cursor = end
        while cursor < len(words):
            following = next((finish for begin, finish in spans if begin == cursor), cursor)
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


def rename_map(changes: tuple[FileChange, ...], sources: RevisionTree) -> dict[str, str]:
    renames: dict[str, str] = {}
    definitions: set[tuple[str, str]] = set()
    for change in changes:
        before, after = tokens(change.before), tokens(change.after)
        if len(before) != len(after):
            return {}
        words = tuple(token.value for token in before)
        attributes = attribute_spans(words)
        decorated = decorated_items(words)
        # Uses may bind an imported item with the same spelling, even through
        # self:: when a glob brought that item into the module's namespace.
        has_glob = re.search(r"\buse\b[^;]*\*", " ".join(words)) is not None
        macro_ends: list[str] = []
        for index, (old, new) in enumerate(zip(before, after)):
            if old.value in {"(", "[", "{"} and (macro_ends or (index and before[index - 1].value == "!")):
                macro_ends.append({"(": ")", "[": "]", "{": "}"}[old.value])
            elif macro_ends and old.value == macro_ends[-1]:
                _ = macro_ends.pop()
            if old == new:
                continue
            if old.kind != "identifier" or new.kind != "identifier":
                return {}
            if any(start <= index < end for start, end in attributes) or index - 1 in decorated:
                return {}  # Attributes on the renamed item can observe its name.
            previous = before[index - 1].value if index else ""
            following = before[index + 1].value if index + 1 < len(before) else ""
            definition = previous in {"fn", "let", "const", "static", "struct", "enum", "type", "trait"}
            if has_glob and not definition:
                return {}
            if macro_ends or previous == ".":
                return {}
            if previous == "::" and (index < 2 or before[index - 2].value not in {"self", "super", "crate"}):
                return {}
            if previous in {"{", ","} and following in {"}", ","}:
                return {}  # A shorthand field also names a potentially foreign field.
            if renames.get(old.value, new.value) != new.value:
                return {}
            renames[old.value] = new.value
            if definition:
                definitions.add((old.value, new.value))
    if len(set(renames.values())) != len(renames) or any(pair not in definitions for pair in renames.items()):
        return {}
    crates = {sources.crate(change.path) for change in changes}
    if len(crates) != 1 or len(changes) > 10:
        return {}
    crate = next(iter(crates))
    owned = {change.path: change.after for change in changes}
    for old, new in renames.items():
        for path in sources.paths:
            if sources.crate(path) == crate:
                original = sources.text(path)
                if re.search(r"\b" + re.escape(new) + r"\b", original) or re.search(
                    r"\b" + re.escape(old) + r"\b", owned.get(path, original)
                ):
                    return {}
    return renames


def judge(finding: Finding, before: RevisionTree) -> Mechanical | Review:
    reasons: list[str] = []
    renames = rename_map(finding.changes, before)
    changed_lines = sum(len(hunk.removed) + len(hunk.added) for change in finding.changes for hunk in change.hunks)
    if changed_lines >= 200:
        reasons.append("check 5: 200 or more changed lines")
    if len(finding.changes) > 10 or len({before.crate(change.path) for change in finding.changes}) > 1:
        if renames or any(tokens(change.before) != tokens(change.after) for change in finding.changes):
            reasons.append("check 4: rename exceeds one crate or 10 files")
    for change in finding.changes:
        lines = "\n".join(line for hunk in change.hunks for line in (*hunk.removed, *hunk.added))
        words = {token.value for token in tokens(lines)}
        if change.status is not FileStatus.MODIFIED or "mod" in words:
            reasons.append("check 1: file or module layout changes")
        glob_reexported = before.has_public_glob(before.crate(change.path))
        public_before = shapes(change.before, True, glob_reexported=glob_reexported)
        public_after = shapes(change.after, True, glob_reexported=glob_reexported)
        if before.externally_reachable(change.path) and public_before != public_after:
            reasons.append("check 2: externally reachable item changes")
        old_shapes = shapes(change.before, False)
        normalized = tuple(tuple(renames.get(word, word) for word in shape) for shape in old_shapes)
        if normalized != shapes(change.after, False):
            reasons.append("check 3: type, fields, variants, generics or trait impl changes")
        if behavior_flagged(lines):
            reasons.append("check 6: condition, literal or control flow flagged")
        if changes_macro_invocation(change):
            reasons.append("check 6: macro invocation requires review")
        if documented_text(change.before) != documented_text(change.after) or re.search(r"#\s*\[\s*(?:serde|reflect)|\brename\s*=", lines):
            reasons.append("check 6: serialized, reflected, configured or documented name flagged")
        old_tokens, new_tokens = without_derives(tokens(change.before)), without_derives(tokens(change.after))
        normalized_tokens = tuple(Token(renames.get(token.value, token.value), token.kind) for token in old_tokens)
        if normalized_tokens != new_tokens or any(token.kind == "unknown" for token in (*old_tokens, *new_tokens)):
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
                raise FootprintError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
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
    except (FootprintError, OSError, ValueError, StopIteration) as error:
        print(f"review: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
