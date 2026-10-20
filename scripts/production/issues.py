#!/usr/bin/env python3
"""Read the frontmatter of the issues in a hanadocs issues folder.

An issue is one `*.md` directly in the folder; its research notes live in `<folder>/<slug>/` and
are not issues. Its frontmatter sits between the leading `---` lines, one `key: value` per line,
a list either inline as `[a, b]` or as `- item` lines below its key. Obsidian writes a link as
`"[[target]]"` or `"[[target|alias]]"`; a value reads as the alias, else the target.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

FENCE = "---"
# The tag every proposal issue carries.
PROPOSAL_TAG = "proposal"
FIELD = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):\s*(?P<value>.*)$")
ITEM = re.compile(r"^\s+-\s*(?P<value>.*)$")
WIKI_LINK = re.compile(r"^\[\[(?P<target>[^\]|]*)(?:\|(?P<alias>[^\]]*))?\]\]$")


class Issue(NamedTuple):
    path: Path
    # Every field as a list: a plain value is a list of one, an empty one a list of none.
    fields: dict[str, list[str]]

    def value(self, key: str) -> str:
        """The field's first value, or nothing when it is empty or missing."""
        return next(iter(self.fields.get(key, [])), "")


def plain(value: str) -> str:
    """A value without its quotes and the brackets of a wiki link."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    link = WIKI_LINK.match(text)
    return (link["alias"] or link["target"]).strip() if link else text


def values(raw: str) -> list[str]:
    """The values one `key: value` line holds: an inline list's items, or the one plain value."""
    text = raw.strip()
    if text.startswith("[") and not text.startswith("[["):
        items = [plain(item) for item in text.strip("[]").split(",")]
        return [item for item in items if item]
    one = plain(text)
    return [one] if one else []


def frontmatter(path: Path) -> dict[str, list[str]]:
    """The file's frontmatter fields, or none when it has no closed frontmatter or cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    if not lines or lines[0].strip() != FENCE or FENCE not in (line.strip() for line in lines[1:]):
        return {}
    fields: dict[str, list[str]] = {}
    key = ""
    for line in lines[1:]:
        if line.strip() == FENCE:
            break
        if (item := ITEM.match(line)) and key:
            fields[key].extend(values(item["value"]))
        elif found := FIELD.match(line):
            key = found["key"]
            fields[key] = values(found["value"])
    return fields


def open_issues(folder: Path, tag: str) -> list[Issue]:
    """The issues directly in the folder whose status is open and whose tags hold the tag, by name."""
    issues = (Issue(path, frontmatter(path)) for path in sorted(folder.glob("*.md")) if path.is_file())
    return [issue for issue in issues if issue.value("status") == "open" and tag in issue.fields.get("tags", [])]
