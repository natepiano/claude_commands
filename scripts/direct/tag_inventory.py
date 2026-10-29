#!/usr/bin/env python3
"""Check the tagged contracts reached from /unit:direct without changing them."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import cast

TAG = re.compile(r"<([A-Z][A-Za-z0-9]*)\s*/>")
DEFINITION = re.compile(r"(?m)^<([A-Z][A-Za-z0-9]*)>\s*$")
MARKDOWN_PATH = re.compile(r"(?:~/.claude/)?((?:commands|docs)/[\w./-]+\.md)")
COMMAND = re.compile(r"(?<![\w/])/(\w+):(\w+)")


def _references(text: str) -> list[str]:
    return [match[1] for match in TAG.finditer(text)]


def contract_text(text: str) -> str:
    """Remove fenced blocks and block quotes, keeping prose and inline tags."""
    lines: list[str] = []
    fence = ""
    width = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if fence:
            if marker and marker[1][0] == fence and len(marker[1]) >= width:
                fence = ""
            continue
        if marker:
            fence, width = marker[1][0], len(marker[1])
            continue
        if not stripped.startswith(">"):
            lines.append(line)
    return "\n".join(lines)


def _table_routes(text: str) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for line in contract_text(text).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 4:
            continue
        paths = [match[1] for match in MARKDOWN_PATH.finditer(cells[2])]
        if paths:
            for tag in _references(cells[1]):
                routes.append((tag, paths[0]))
    return routes


def contract_table_paths(text: str) -> list[str]:
    """Files the direct command's TagReferenceContract table names."""
    section = re.search(r"<TagReferenceContract>(.*?)</TagReferenceContract>", contract_text(text), re.S)
    return sorted({path for _, path in _table_routes(section[1])}) if section else []


@dataclass(frozen=True)
class ContractBody:
    tag: str
    path: str
    text: str


@dataclass(frozen=True)
class TagInventory:
    tags: tuple[str, ...]
    files: tuple[str, ...]
    issues: tuple[str, ...]


def _bodies(path: str, text: str) -> list[ContractBody]:
    bodies: list[ContractBody] = []
    for opening in DEFINITION.finditer(text):
        closing = text.find(f"</{opening[1]}>", opening.end())
        if closing >= 0:
            bodies.append(ContractBody(opening[1], path, text[opening.end():closing]))
    # Some step files name the whole document as their body instead of wrapping it.
    for declaration in re.finditer(r"[Dd]efines\s+`?<([A-Z][A-Za-z0-9]*)/>`?\s+in\s+full", text):
        if not any(body.tag == declaration[1] for body in bodies):
            bodies.append(ContractBody(declaration[1], path, text))
    return bodies


def _forward_path(body: ContractBody) -> str:
    forward = re.search(r"\bDefined in\s+`([^`]+)`", body.text, re.I)
    if forward is None:
        forward = re.search(r"^\s*Read\s+`([^`]+)`\s+in full", body.text, re.I)
    match = MARKDOWN_PATH.search(forward[1]) if forward else None
    return match[1] if match else ""


def inventory(root: Path, entry: str = "commands/unit/direct.md") -> TagInventory:
    documents: dict[str, str] = {}
    pending = [entry]
    while pending:
        path = pending.pop()
        if path in documents or path.startswith(("docs/plans/", "docs/as-built/")):
            continue
        try:
            text = contract_text((root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            documents[path] = ""
            continue
        documents[path] = text
        pending.extend(match[1] for match in MARKDOWN_PATH.finditer(text))
        pending.extend(f"commands/{match[1]}/{match[2]}.md" for match in COMMAND.finditer(text))
    all_bodies = [body for path, text in documents.items() for body in _bodies(path, text)]
    routes = {path: dict(_table_routes(text)) for path, text in documents.items()}
    # Table files and their document dependencies share the contract namespace.
    # Other commands join it only through a table; a linked command's own Guild
    # and other private sections stay outside this command's namespace.
    primary = {entry}
    pending = [entry]
    while pending:
        text = documents.get(pending.pop(), "")
        targets = {target for _, target in _table_routes(text)}
        targets.update(match[1] for match in MARKDOWN_PATH.finditer(text) if match[1].startswith("docs/"))
        for target in sorted(targets):
            if target not in primary and not target.startswith(("docs/plans/", "docs/as-built/")):
                primary.add(target)
                pending.append(target)
    needed = {(tag, path) for path in primary for tag in _references(documents.get(path, ""))}
    needed.update((body.tag, body.path) for body in all_bodies if body.path in primary)
    active: list[ContractBody] = []
    issues: set[str] = set()
    checked: set[tuple[str, str]] = set()
    while needed - checked:
        tag, caller = min(needed - checked)
        checked.add((tag, caller))
        candidates = [body for body in all_bodies if body.tag == tag]
        local = [body for body in candidates if body.path in primary]
        if local:
            candidates = local
        else:
            own = [body for body in candidates if body.path == caller]
            if own:
                candidates = own
        # A selected stub can forward through a file that is not in the table.
        # Keep that chain in scope without importing another command's namespace.
        forward_paths = {_forward_path(body) for body in candidates} - {""}
        while forward_paths:
            path = forward_paths.pop()
            forwarded = [body for body in all_bodies if body.tag == tag and body.path == path and body not in candidates]
            candidates.extend(forwarded)
            forward_paths.update(_forward_path(body) for body in forwarded if _forward_path(body))
        resolved: list[ContractBody] = []
        for body in candidates:
            target = routes[body.path].get(tag)
            if target is None:
                target = _forward_path(body) or body.path
            if target != body.path:
                continue
            resolved.append(body)
        # Check every route, even when an unrelated body would hide a stale path.
        forwards: dict[str, str] = {}
        for path, mappings in routes.items():
            if tag in mappings and (path in primary or any(body.path == path for body in candidates)):
                forwards[path] = mappings[tag]
        for body in candidates:
            target = _forward_path(body)
            if target:
                if body.path in forwards and forwards[body.path] != target:
                    issues.add(f"StalePath: <{tag}/> table and stub in {body.path} name different files")
                forwards[body.path] = target
        for source, target in forwards.items():
            seen = {source}
            while target in forwards:
                if target in seen:
                    issues.add(f"ForwardingCycle: <{tag}/> from {source}")
                    break
                seen.add(target)
                target = forwards[target]
            else:
                if not any(body.path == target for body in resolved):
                    issues.add(f"StalePath: <{tag}/> from {source} names {target} without its body")
        for source in primary:
            for routed_tag, target in _table_routes(documents.get(source, "")):
                if routed_tag == tag and target not in forwards and not any(body.path == target for body in resolved):
                    issues.add(f"StalePath: <{tag}/> from {source} names {target} without its body")
        if not resolved:
            issues.add(f"MissingBody: <{tag}/>")
        elif len(resolved) > 1:
            issues.add(f"TwoBodies: <{tag}/> in " + ", ".join(body.path for body in resolved))
        active.extend(resolved)
        for body in resolved:
            needed.update((reference, body.path) for reference in _references(body.text))
    if not documents.get(entry):
        issues.add(f"StalePath: entry {entry} is missing or unreadable")
    return TagInventory(tuple(sorted({tag for tag, _ in checked})),
                        tuple(sorted({body.path for body in active} | primary)), tuple(sorted(issues)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    root = cast(Path, args.root)
    result = inventory(root)
    print(f"{len(result.tags)} tags in {len(result.files)} files: " + ", ".join(result.tags))
    if result.issues:
        for issue in result.issues:
            print(issue, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
