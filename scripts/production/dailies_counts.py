#!/usr/bin/env python3
"""Count what config/dailies.conf names for one production, for the dailies' counts line.

Each line is `<production slug>.<label>=lines <file> <regex>`, the file's lines matching the
regex, `<production slug>.<label>=files <glob>`, the files matching the glob, or
`<production slug>.<label>=issues <folder> <tag>`, the open issues directly in the folder whose
tags hold the tag. `~` expands, and a hyphen in the label reads as a space. A production with no
lines gets no counts.
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import NamedTuple

from issues import open_issues

LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class CountsFailure(Exception):
    """A config line the counts cannot be read from."""


class LineCount(NamedTuple):
    path: Path
    pattern: re.Pattern[str]


class FileCount(NamedTuple):
    pattern: str


class IssueCount(NamedTuple):
    folder: Path
    tag: str


class CountSpec(NamedTuple):
    label: str
    source: LineCount | FileCount | IssueCount


class Counted(NamedTuple):
    label: str
    number: int


class SourceNotFound(NamedTuple):
    label: str


def config_path() -> Path:
    return Path(os.environ.get("DAILIES_COUNTS_CONFIG") or Path.home() / ".claude" / "config" / "dailies.conf")


def parse_line(line: str, number: int) -> tuple[str, CountSpec]:
    where = f"dailies.conf line {number}"
    key, separator, value = line.partition("=")
    slug, dot, label = key.strip().partition(".")
    if not separator or not dot or not slug or not LABEL.fullmatch(label):
        raise CountsFailure(f"{where}: expected <production slug>.<label>=lines <file> <regex> | files <glob> | issues <folder> <tag>")
    kind, _, rest = value.strip().partition(" ")
    rest = rest.strip()
    source: LineCount | FileCount | IssueCount
    if kind == "lines":
        path, _, regex = rest.partition(" ")
        if not path or not regex.strip():
            raise CountsFailure(f"{where}: lines needs a file and a regex")
        try:
            source = LineCount(Path(path).expanduser(), re.compile(regex.strip()))
        except re.error as error:
            raise CountsFailure(f"{where}: bad regex: {error}") from error
    elif kind == "files":
        if not rest:
            raise CountsFailure(f"{where}: files needs a glob")
        source = FileCount(os.path.expanduser(rest))
    elif kind == "issues":
        folder, _, tag = rest.partition(" ")
        if not folder or not tag.strip() or " " in tag.strip():
            raise CountsFailure(f"{where}: issues needs a folder and a tag")
        source = IssueCount(Path(folder).expanduser(), tag.strip())
    else:
        raise CountsFailure(f"{where}: the count kind is lines, files or issues, not {kind or 'nothing'}")
    return slug, CountSpec(label.replace("-", " "), source)


def read_specs(path: Path, slug: str) -> list[CountSpec]:
    if not path.exists():
        return []
    specs: list[CountSpec] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line_slug, spec = parse_line(line, number)
        if line_slug == slug:
            specs.append(spec)
    return specs


def count(spec: CountSpec) -> Counted | SourceNotFound:
    match spec.source:
        case LineCount(path, pattern):
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return SourceNotFound(spec.label)
            return Counted(spec.label, sum(1 for line in text.splitlines() if pattern.search(line)))
        case FileCount(pattern):
            if not Path(pattern).parent.is_dir():
                return SourceNotFound(spec.label)
            return Counted(spec.label, sum(1 for match in glob.glob(pattern) if Path(match).is_file()))
        case IssueCount(folder, tag):
            if not folder.is_dir():
                return SourceNotFound(spec.label)
            return Counted(spec.label, len(open_issues(folder, tag)))


def production_counts(slug: str) -> list[dict[str, object]]:
    """The input's `counts`: each label with its count, or `not_found` when its source is missing."""
    counts: list[dict[str, object]] = []
    for spec in read_specs(config_path(), slug):
        match count(spec):
            case Counted(label, number):
                counts.append({"label": label, "count": number})
            case SourceNotFound(label):
                counts.append({"label": label, "not_found": True})
    return counts
