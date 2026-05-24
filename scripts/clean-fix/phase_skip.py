#!/usr/bin/env python3
"""Skip or re-enable individual projects per build phase (clean vs style).

Two scopes, one per conf section:

  - ``clean``  -> ``[exclude]``. Skips a standalone top-level repo under ~/rust
    from the clean+build pass. (Standalone repos share this list with the style
    pass, so a clean skip also removes them from style.)
  - ``style``  -> ``[workspace_members]``. Skips a workspace member from the
    style eval/fix pass by commenting its line. Clean never reads this section.

Both scopes tag their edits with the ``#CLEAN_FIX_SKIP#`` marker, so ``enable`` /
``enable-all`` only reverse temporary skips and never disturb permanent
``[exclude]`` entries (bevy, tracy, blender, ...).

Usage:
    phase_skip.py <clean|style> [skip|enable|enable-all|status] [project ...]
    phase_skip.py <clean|style>                  # same as status
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

CONF_FILE = Path(__file__).resolve().parent / "clean-fix.conf"
RUST_DIR = Path.home() / "rust"
MARKER = "#CLEAN_FIX_SKIP#"

SECTION_RE = re.compile(r"^\[(?P<name>.+)\]\s*$")


@dataclass
class ExcludeEntry:
    index: int
    value: str
    tagged: bool


def read_lines() -> list[str]:
    return CONF_FILE.read_text().splitlines()


def write_lines(lines: list[str]) -> None:
    _ = CONF_FILE.write_text("\n".join(lines) + "\n")


def section_of_lines(lines: list[str]) -> list[str | None]:
    """Section name in effect for each line (a header line counts as belonging
    to the section it opens)."""
    sections: list[str | None] = []
    current: str | None = None
    for line in lines:
        match = SECTION_RE.match(line.strip())
        if match:
            current = match.group("name")
        sections.append(current)
    return sections


def ws_member_key(line: str) -> str | None:
    """Project name for a ``[workspace_members]`` line, active or skip-tagged.

    Returns None for blanks, pure ``#`` doc comments, and anything without
    ``=``."""
    body = line
    if body.startswith(MARKER):
        body = body[len(MARKER):].lstrip()
    body = body.strip()
    if not body or body.startswith("#") or "=" not in body:
        return None
    key = body.split("=", 1)[0].strip()
    return key or None


def workspace_member_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if sec != "workspace_members":
            continue
        key = ws_member_key(line)
        if key:
            names.add(key)
    return names


def exclude_entries(lines: list[str]) -> list[ExcludeEntry]:
    entries: list[ExcludeEntry] = []
    for index, (line, sec) in enumerate(zip(lines, section_of_lines(lines), strict=True)):
        if sec != "exclude":
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or SECTION_RE.match(stripped):
            continue
        value = stripped.split("#", 1)[0].strip()
        if not value:
            continue
        entries.append(ExcludeEntry(index=index, value=value, tagged=MARKER in line))
    return entries


def project_kind(name: str, lines: list[str]) -> str:
    if name in workspace_member_names(lines):
        return "workspace_member"
    if (RUST_DIR / name).is_dir():
        return "standalone"
    return "unknown"


def insert_exclude_line(lines: list[str], new_line: str) -> tuple[list[str], bool]:
    """Insert ``new_line`` at the end of ``[exclude]``, before trailing blanks.
    Returns (lines, found_section)."""
    sections = section_of_lines(lines)
    start: int | None = None
    end = len(lines)
    for index, sec in enumerate(sections):
        if sec == "exclude" and start is None:
            start = index
        elif start is not None and sec != "exclude":
            end = index
            break
    if start is None:
        return lines, False
    insert_at = end
    while insert_at - 1 > start and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    return lines[:insert_at] + [new_line] + lines[insert_at:], True


def skip_member(name: str, lines: list[str]) -> tuple[list[str], str]:
    out = list(lines)
    for index, sec in enumerate(section_of_lines(out)):
        if sec != "workspace_members" or ws_member_key(out[index]) != name:
            continue
        if out[index].startswith(MARKER):
            return out, f"ALREADY-SKIPPED {name} (style)"
        out[index] = f"{MARKER} {out[index]}"
        return out, f"SKIP {name} (style): commented in [workspace_members]"
    return out, f"UNKNOWN {name}: no [workspace_members] entry"


def enable_member(name: str, lines: list[str]) -> tuple[list[str], str]:
    out = list(lines)
    for index, sec in enumerate(section_of_lines(out)):
        if sec != "workspace_members" or ws_member_key(out[index]) != name:
            continue
        if not out[index].startswith(MARKER):
            return out, f"NOT-SKIPPED {name} (style): already active"
        out[index] = out[index][len(MARKER):].lstrip()
        return out, f"ENABLED {name} (style)"
    return out, f"UNKNOWN {name}: no [workspace_members] entry"


def skip_exclude(name: str, lines: list[str]) -> tuple[list[str], str]:
    for entry in exclude_entries(lines):
        if entry.value != name:
            continue
        if entry.tagged:
            return lines, f"ALREADY-SKIPPED {name} (clean)"
        return lines, (
            f"PERMANENT {name} (clean): already a plain [exclude] entry; "
            f"left unchanged"
        )
    out, found = insert_exclude_line(lines, f"{name} {MARKER}")
    if not found:
        return out, f"ERROR {name}: no [exclude] section in conf"
    return out, f"SKIP {name} (clean): added to [exclude]"


def enable_exclude(name: str, lines: list[str]) -> tuple[list[str], str]:
    for entry in exclude_entries(lines):
        if entry.value != name:
            continue
        if not entry.tagged:
            return lines, (
                f"PERMANENT {name} (clean): plain [exclude] entry, not a temp "
                f"skip; left unchanged"
            )
        return lines[:entry.index] + lines[entry.index + 1:], f"ENABLED {name} (clean)"
    return lines, f"NOT-SKIPPED {name} (clean): not in [exclude]"


def enable_all(scope: str, lines: list[str]) -> tuple[list[str], list[str]]:
    out: list[str] = []
    msgs: list[str] = []
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if scope == "style" and sec == "workspace_members" and line.startswith(MARKER):
            key = ws_member_key(line)
            out.append(line[len(MARKER):].lstrip())
            if key:
                msgs.append(f"ENABLED {key} (style)")
            continue
        if scope == "clean" and sec == "exclude" and MARKER in line and not line.strip().startswith("#"):
            value = line.strip().split("#", 1)[0].strip()
            msgs.append(f"ENABLED {value} (clean)")
            continue
        out.append(line)
    return out, msgs


def collect_skipped(scope: str, lines: list[str]) -> list[str]:
    skipped: list[str] = []
    for line, sec in zip(lines, section_of_lines(lines), strict=True):
        if scope == "style" and sec == "workspace_members" and line.startswith(MARKER):
            key = ws_member_key(line)
            if key:
                skipped.append(key)
        elif scope == "clean" and sec == "exclude" and MARKER in line and not line.strip().startswith("#"):
            skipped.append(line.strip().split("#", 1)[0].strip())
    return skipped


def run_skip(scope: str, projects: list[str]) -> int:
    lines = read_lines()
    exit_code = 0
    for name in projects:
        kind = project_kind(name, lines)
        if scope == "clean":
            if kind == "workspace_member":
                print(f"WRONG-SCOPE {name}: workspace member — use /skip_fix")
                exit_code = 1
                continue
            if kind == "unknown":
                print(f"UNKNOWN {name}: no ~/rust/{name} directory")
                exit_code = 1
                continue
            lines, msg = skip_exclude(name, lines)
        else:
            if kind == "standalone":
                print(f"WRONG-SCOPE {name}: standalone repo — use /skip_clean")
                exit_code = 1
                continue
            if kind == "unknown":
                print(f"UNKNOWN {name}: not a [workspace_members] entry")
                exit_code = 1
                continue
            lines, msg = skip_member(name, lines)
        print(msg)
    write_lines(lines)
    return exit_code


def run_enable(scope: str, projects: list[str]) -> int:
    lines = read_lines()
    exit_code = 0
    for name in projects:
        if scope == "clean":
            lines, msg = enable_exclude(name, lines)
        else:
            lines, msg = enable_member(name, lines)
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
        print(f"No projects currently skipped from {pass_name}.")
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
    parser = argparse.ArgumentParser(description="Skip/enable projects per pass.")
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
