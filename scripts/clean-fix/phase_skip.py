#!/usr/bin/env python3
"""Skip or re-enable individual clean-fix style targets.

The conf is an opt-in allowlist, so "skipping" a target means commenting its
allowlist line out, and "enabling" uncomments it. Style targets live in
``[projects]``.

Skips are tagged with the ``#CLEAN_FIX_SKIP#`` marker so ``enable`` /
``enable-all`` only reverse temporary skips and never touch plain doc comments.

Usage:
    phase_skip.py [skip|enable|enable-all|status] [project ...]
    phase_skip.py style [skip|enable|enable-all|status] [project ...]
"""

from __future__ import annotations

import argparse
import re
import sys

from collections.abc import Sequence
from typing import NamedTuple
from typing import Literal, cast
from pathlib import Path

CONF_FILE = Path(__file__).resolve().parent / "clean-fix.conf"
MARKER = "#CLEAN_FIX_SKIP#"

SECTION_RE = re.compile(r"^\[(?P<name>.+)\]\s*$")
PROJECTS_SECTION = "projects"
PASS_LABEL = "style"

PhaseSkipAction = Literal["skip", "enable", "enable-all", "status"]


class ActiveCheckout(NamedTuple):
    entry: str
    key: str
    checkout: str
    checkout_root: str


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


def project_key(entry: str) -> str:
    return entry.rsplit("/", 1)[-1] if "/" in entry else entry


def checkout_root(checkout: str) -> str:
    return checkout.split("/", 1)[0]


def uncommented_body(line: str) -> str:
    body = line.strip()
    if body.startswith(MARKER):
        body = body[len(MARKER):]
    return body.split("#", 1)[0].strip()


def active_checkouts(lines: list[str]) -> list[ActiveCheckout]:
    redirects: list[ActiveCheckout] = []
    for line, section in zip(lines, section_of_lines(lines), strict=True):
        if section != "active_checkout":
            continue
        body = uncommented_body(line)
        if not body or "=" not in body:
            continue
        entry, _, checkout = body.partition("=")
        entry = entry.strip()
        checkout = checkout.strip()
        if not entry or not checkout:
            continue
        redirects.append(
            ActiveCheckout(
                entry=entry,
                key=project_key(entry),
                checkout=checkout,
                checkout_root=checkout_root(checkout),
            )
        )
    return redirects


def target_key(name: str, redirects: Sequence[ActiveCheckout]) -> str:
    normalized = project_key(name)
    for redirect in redirects:
        if name in {
            redirect.entry,
            redirect.key,
            redirect.checkout,
            redirect.checkout_root,
        }:
            return redirect.key
        if name.startswith(f"{redirect.checkout_root}/"):
            return redirect.key
        if normalized == redirect.key:
            return redirect.key
    return normalized


def entry_key(line: str, section: str) -> str | None:
    """Project name a section line represents, whether active or skip-tagged.

    Returns None for blanks, section headers, and plain ``#`` doc comments.
    In ``[projects]``, a member line (``<dir>/<subpath>``) is keyed by its last
    path segment; everywhere else the entry text is its own key.
    """
    body = uncommented_body(line)
    if not body or SECTION_RE.match(body):
        return None
    if section == "projects":
        return project_key(body)
    return body


def is_tagged(line: str) -> bool:
    return line.strip().startswith(MARKER)


def skip_entry(name: str, lines: list[str]) -> tuple[list[str], str]:
    section = PROJECTS_SECTION
    out = list(lines)
    redirects = active_checkouts(out)
    target = target_key(name, redirects)
    matched = 0
    changed = 0
    for index, sec in enumerate(section_of_lines(out)):
        if sec != section or entry_key(out[index], section) != target:
            continue
        matched += 1
        if is_tagged(out[index]):
            continue
        out[index] = f"{MARKER} {out[index].strip()}"
        changed += 1
    if matched == 0:
        return out, f"UNKNOWN {name}: no [{section}] entry"
    if changed == 0:
        return out, f"ALREADY-SKIPPED {target} ({PASS_LABEL})"
    suffix = "entry" if changed == 1 else "entries"
    return out, f"SKIP {target} ({PASS_LABEL}): commented {changed} {suffix} in [{section}]"


def enable_entry(name: str, lines: list[str]) -> tuple[list[str], str]:
    section = PROJECTS_SECTION
    out = list(lines)
    redirects = active_checkouts(out)
    target = target_key(name, redirects)
    matched = 0
    changed = 0
    for index, sec in enumerate(section_of_lines(out)):
        if sec != section or entry_key(out[index], section) != target:
            continue
        matched += 1
        if not is_tagged(out[index]):
            continue
        out[index] = out[index].strip()[len(MARKER):].lstrip()
        changed += 1
    if matched == 0:
        return out, f"UNKNOWN {name}: no [{section}] entry"
    if changed == 0:
        return out, f"NOT-SKIPPED {target} ({PASS_LABEL}): already active"
    return out, f"ENABLED {target} ({PASS_LABEL})"


def enable_all(lines: list[str]) -> tuple[list[str], list[str]]:
    section = PROJECTS_SECTION
    out: list[str] = []
    msgs: list[str] = []
    seen: set[str] = set()
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if sec == section and is_tagged(line):
            key = entry_key(line, section)
            out.append(line.strip()[len(MARKER):].lstrip())
            if key and key not in seen:
                msgs.append(f"ENABLED {key} ({PASS_LABEL})")
                seen.add(key)
            continue
        out.append(line)
    return out, msgs


def collect_skipped(lines: list[str]) -> list[str]:
    section = PROJECTS_SECTION
    skipped: list[str] = []
    seen: set[str] = set()
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if sec == section and is_tagged(line):
            key = entry_key(line, section)
            if key and key not in seen:
                skipped.append(key)
                seen.add(key)
    return skipped


def run_skip(projects: list[str]) -> int:
    lines = read_lines()
    exit_code = 0
    for name in projects:
        lines, msg = skip_entry(name, lines)
        if msg.startswith("UNKNOWN"):
            exit_code = 1
        print(msg)
    write_lines(lines)
    return exit_code


def run_enable(projects: list[str]) -> int:
    lines = read_lines()
    exit_code = 0
    for name in projects:
        lines, msg = enable_entry(name, lines)
        if msg.startswith("UNKNOWN"):
            exit_code = 1
        print(msg)
    write_lines(lines)
    return exit_code


def run_enable_all() -> int:
    out, msgs = enable_all(read_lines())
    write_lines(out)
    if not msgs:
        print(f"Nothing skipped in {PASS_LABEL}; no changes.")
        return 0
    for msg in msgs:
        print(msg)
    return 0


def run_status() -> int:
    skipped = collect_skipped(read_lines())
    pass_name = "style eval/fix"
    if not skipped:
        print(f"No targets currently skipped from {pass_name}.")
        return 0
    print(f"Currently skipped from {pass_name}:")
    for entry in skipped:
        print(f"  - {entry}")
    return 0


class CliArgs(argparse.Namespace):
    action: PhaseSkipAction = "status"
    projects: list[str] = []


class ArgparseCliArgs(argparse.Namespace):
    action: str | None = None
    projects: list[str] = []


def parse_args() -> CliArgs:
    parser = argparse.ArgumentParser(description="Skip/enable style targets.")
    actions = parser.add_subparsers(dest="action", required=False)

    skip = actions.add_parser("skip")
    _ = skip.add_argument("projects", nargs="+")

    enable = actions.add_parser("enable")
    _ = enable.add_argument("projects", nargs="+")

    _ = actions.add_parser("enable-all")
    _ = actions.add_parser("status")

    argv = sys.argv[1:]
    if argv[:1] == ["style"]:
        argv = argv[1:]
    parsed = parser.parse_args(argv, namespace=ArgparseCliArgs())
    action = "status" if parsed.action is None else cast(PhaseSkipAction, parsed.action)
    args = CliArgs()
    args.action = action
    args.projects = parsed.projects
    return args


def main() -> int:
    args = parse_args()
    if args.action == "skip":
        return run_skip(args.projects)
    if args.action == "enable":
        return run_enable(args.projects)
    if args.action == "enable-all":
        return run_enable_all()
    return run_status()


if __name__ == "__main__":
    raise SystemExit(main())
