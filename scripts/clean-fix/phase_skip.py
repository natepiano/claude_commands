#!/usr/bin/env python3
"""Skip or re-enable individual targets per build phase (clean vs style).

The conf is an opt-in allowlist, so "skipping" a target means commenting its
allowlist line out, and "enabling" uncomments it. Two scopes, one section each:

  - ``clean``  -> ``[build]``.   The nightly clean/build/mend allowlist.
  - ``style``  -> ``[targets]``. The style eval/review/fix allowlist.

Skips are tagged with the ``#CLEAN_FIX_SKIP#`` marker so ``enable`` /
``enable-all`` only reverse temporary skips and never touch plain doc comments.

Usage:
    phase_skip.py <clean|style> [skip|enable|enable-all|status] [project ...]
    phase_skip.py <clean|style>                  # same as status
"""

from __future__ import annotations

import argparse
import re

from pathlib import Path

CONF_FILE = Path(__file__).resolve().parent / "clean-fix.conf"
MARKER = "#CLEAN_FIX_SKIP#"

SECTION_RE = re.compile(r"^\[(?P<name>.+)\]\s*$")
SCOPE_SECTION = {"clean": "build", "style": "targets"}


def read_lines() -> list[str]:
    return CONF_FILE.read_text().splitlines()


def write_lines(lines: list[str]) -> None:
    _ = CONF_FILE.write_text("\n".join(lines) + "\n")


def section_of_lines(lines: list[str]) -> list[str | None]:
    """Section name in effect for each line (a header belongs to the section it
    opens)."""
    sections: list[str | None] = []
    current: str | None = None
    for line in lines:
        match = SECTION_RE.match(line.strip())
        if match:
            current = match.group("name")
        sections.append(current)
    return sections


def entry_key(line: str, section: str) -> str | None:
    """Project name a section line represents, whether active or skip-tagged.

    Returns None for blanks, section headers, and plain ``#`` doc comments. In
    ``[targets]`` a member line (``<dir>/<subpath>``) is keyed by its last path
    segment; everywhere else the entry text is its own key.
    """
    body = line.strip()
    if body.startswith(MARKER):
        body = body[len(MARKER):]
    body = body.split("#", 1)[0].strip()
    if not body or SECTION_RE.match(body):
        return None
    if section == "targets" and "/" in body:
        return body.rsplit("/", 1)[-1]
    return body


def is_tagged(line: str) -> bool:
    return line.strip().startswith(MARKER)


def skip_entry(scope: str, name: str, lines: list[str]) -> tuple[list[str], str]:
    section = SCOPE_SECTION[scope]
    out = list(lines)
    for index, sec in enumerate(section_of_lines(out)):
        if sec != section or entry_key(out[index], section) != name:
            continue
        if is_tagged(out[index]):
            return out, f"ALREADY-SKIPPED {name} ({scope})"
        out[index] = f"{MARKER} {out[index].strip()}"
        return out, f"SKIP {name} ({scope}): commented in [{section}]"
    return out, f"UNKNOWN {name}: no [{section}] entry"


def enable_entry(scope: str, name: str, lines: list[str]) -> tuple[list[str], str]:
    section = SCOPE_SECTION[scope]
    out = list(lines)
    for index, sec in enumerate(section_of_lines(out)):
        if sec != section or entry_key(out[index], section) != name:
            continue
        if not is_tagged(out[index]):
            return out, f"NOT-SKIPPED {name} ({scope}): already active"
        out[index] = out[index].strip()[len(MARKER):].lstrip()
        return out, f"ENABLED {name} ({scope})"
    return out, f"UNKNOWN {name}: no [{section}] entry"


def enable_all(scope: str, lines: list[str]) -> tuple[list[str], list[str]]:
    section = SCOPE_SECTION[scope]
    out: list[str] = []
    msgs: list[str] = []
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if sec == section and is_tagged(line):
            key = entry_key(line, section)
            out.append(line.strip()[len(MARKER):].lstrip())
            if key:
                msgs.append(f"ENABLED {key} ({scope})")
            continue
        out.append(line)
    return out, msgs


def collect_skipped(scope: str, lines: list[str]) -> list[str]:
    section = SCOPE_SECTION[scope]
    skipped: list[str] = []
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if sec == section and is_tagged(line):
            key = entry_key(line, section)
            if key:
                skipped.append(key)
    return skipped


def run_skip(scope: str, projects: list[str]) -> int:
    lines = read_lines()
    exit_code = 0
    for name in projects:
        lines, msg = skip_entry(scope, name, lines)
        if msg.startswith("UNKNOWN"):
            exit_code = 1
        print(msg)
    write_lines(lines)
    return exit_code


def run_enable(scope: str, projects: list[str]) -> int:
    lines = read_lines()
    exit_code = 0
    for name in projects:
        lines, msg = enable_entry(scope, name, lines)
        if msg.startswith("UNKNOWN"):
            exit_code = 1
        print(msg)
    write_lines(lines)
    return exit_code


def run_enable_all(scope: str) -> int:
    out, msgs = enable_all(scope, read_lines())
    write_lines(out)
    if not msgs:
        print(f"Nothing skipped in {scope}; no changes.")
        return 0
    for msg in msgs:
        print(msg)
    return 0


def run_status(scope: str) -> int:
    skipped = collect_skipped(scope, read_lines())
    pass_name = "clean+build" if scope == "clean" else "style eval/fix"
    if not skipped:
        print(f"No targets currently skipped from {pass_name}.")
        return 0
    print(f"Currently skipped from {pass_name}:")
    for entry in skipped:
        print(f"  - {entry}")
    return 0


class CliArgs(argparse.Namespace):
    scope: str = ""
    action: str | None = None
    projects: list[str] = []


def parse_args() -> CliArgs:
    parser = argparse.ArgumentParser(description="Skip/enable targets per pass.")
    scopes = parser.add_subparsers(dest="scope", required=True)

    for scope_name in ("clean", "style"):
        scope_parser = scopes.add_parser(scope_name)
        actions = scope_parser.add_subparsers(dest="action", required=False)

        skip = actions.add_parser("skip")
        _ = skip.add_argument("projects", nargs="+")

        enable = actions.add_parser("enable")
        _ = enable.add_argument("projects", nargs="+")

        _ = actions.add_parser("enable-all")
        _ = actions.add_parser("status")

    return parser.parse_args(namespace=CliArgs())


def main() -> int:
    args = parse_args()
    action = args.action or "status"
    if action == "skip":
        return run_skip(args.scope, args.projects)
    if action == "enable":
        return run_enable(args.scope, args.projects)
    if action == "enable-all":
        return run_enable_all(args.scope)
    return run_status(args.scope)


if __name__ == "__main__":
    raise SystemExit(main())
