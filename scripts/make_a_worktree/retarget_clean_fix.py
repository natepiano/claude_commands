#!/usr/bin/env python3
"""Redirect clean-fix's style eval/fix to a worktree, via clean-fix.conf.

A git worktree `W` is a full checkout of one repo `R` (its parent must be ~/rust
so its name is a valid clean-fix path relative to ~/rust). If `W`'s name is
prefixed — at a `_`/`-` boundary — by `R` itself or by one of `R`'s workspace
members named in [projects], the matched project(s) should evaluate/fix that
worktree instead of the primary, while keeping their identity/history.

That redirect lives in [active_checkout] (`<projects-entry> = <checkout-path>`);
the [projects] line is never touched, so the history key is always preserved.
`W` is also added to [build] so the worktree builds nightly (build everything).

Subcommands:
  detect --repo R --worktree W [--conf PATH]   print JSON describing the match
  apply  --repo R --worktree W [--conf PATH]   write the redirect(s) + [build] add
  revert --worktree W           [--conf PATH]  drop W's redirect(s) + [build] entry
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict

DEFAULT_CONF = Path.home() / ".claude" / "scripts" / "clean-fix" / "clean-fix.conf"
BOUNDARY = "_-"


class Redirect(TypedDict):
    entry: str  # the [projects] entry (identity / history key) — left side
    checkout: str  # the worktree checkout path it evaluates — right side


class DetectResult(TypedDict):
    match: bool
    repo: str
    worktree: str
    selector: str
    kind: str  # "repo" | "member" | "none"
    redirects: list[Redirect]
    build_add: str
    build_already: bool


def read_conf(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    """(header_index, end_index) for `[section]`; end is the next header or len.
    Raises ValueError if the section header is absent."""
    header = f"[{section}]"
    start = -1
    for i, raw in enumerate(lines):
        if raw.strip() == header:
            start = i
            break
    if start < 0:
        raise ValueError(f"section [{section}] not found")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("[") and s.endswith("]"):
            end = i
            break
    return start, end


def _entry_indices(lines: list[str], section: str) -> list[int]:
    """Indices of non-comment, non-blank entry lines inside `[section]`."""
    try:
        start, end = _section_bounds(lines, section)
    except ValueError:
        return []
    out: list[int] = []
    for i in range(start + 1, end):
        s = lines[i].strip()
        if s and not s.startswith("#"):
            out.append(i)
    return out


def _is_prefixed(name: str, sel: str) -> bool:
    if name == sel:
        return True
    return name.startswith(sel) and len(name) > len(sel) and name[len(sel)] in BOUNDARY


def detect(lines: list[str], repo: str, worktree: str) -> DetectResult:
    none: DetectResult = {
        "match": False, "repo": repo, "worktree": worktree, "selector": "",
        "kind": "none", "redirects": [], "build_add": worktree, "build_already": False,
    }

    projects = [lines[i].strip() for i in _entry_indices(lines, "projects")]
    group = [e for e in projects if e == repo or e.startswith(repo + "/")]
    if not group:
        return none

    selectors: set[str] = {repo}
    for entry in group:
        if "/" in entry:
            selectors.add(entry.rsplit("/", 1)[-1])

    matched = [s for s in selectors if _is_prefixed(worktree, s)]
    if not matched:
        return none
    selector = max(matched, key=len)

    if selector == repo:
        kind = "repo"
        move = group
    else:
        kind = "member"
        move = [e for e in group if "/" in e and e.rsplit("/", 1)[-1] == selector]

    redirects: list[Redirect] = [
        {"entry": e, "checkout": worktree + e[len(repo):]} for e in move
    ]
    build_entries = [lines[i].strip() for i in _entry_indices(lines, "build")]
    return {
        "match": True, "repo": repo, "worktree": worktree, "selector": selector,
        "kind": kind, "redirects": redirects, "build_add": worktree,
        "build_already": worktree in build_entries,
    }


def _last_content_index(lines: list[str], section: str) -> int:
    """Index to insert *after* so a new line stays inside `[section]`: the last
    non-blank line within the section (its header if the section is empty)."""
    start, end = _section_bounds(lines, section)
    last = start
    for i in range(start + 1, end):
        if lines[i].strip():
            last = i
    return last


def apply(lines: list[str], result: DetectResult) -> list[str]:
    out = list(lines)

    # Upsert each redirect into [active_checkout].
    for r in result["redirects"]:
        line = f"{r['entry']} = {r['checkout']}"
        replaced = False
        for i in _entry_indices(out, "active_checkout"):
            key = out[i].split("=", 1)[0].strip()
            if key == r["entry"]:
                out[i] = line
                replaced = True
                break
        if not replaced:
            out.insert(_last_content_index(out, "active_checkout") + 1, line)

    # Add the worktree to [build] (keep the primary — build everything).
    if not result["build_already"]:
        out.insert(_last_content_index(out, "build") + 1, result["build_add"])

    return out


def revert(lines: list[str], worktree: str) -> tuple[list[str], list[str]]:
    """Drop [active_checkout] redirects pointing into `worktree` and the worktree's
    [build] entry. Returns (new_lines, removed-descriptions)."""
    removed: list[str] = []
    drop: set[int] = set()

    for i in _entry_indices(lines, "active_checkout"):
        value = lines[i].split("=", 1)[1].strip() if "=" in lines[i] else ""
        if value.split("/", 1)[0] == worktree:
            drop.add(i)
            removed.append(f"redirect: {lines[i].strip()}")
    for i in _entry_indices(lines, "build"):
        if lines[i].strip() == worktree:
            drop.add(i)
            removed.append(f"build: {worktree}")

    out = [line for i, line in enumerate(lines) if i not in drop]
    return out, removed


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def parse_args(argv: list[str]) -> tuple[str, str, str, Path]:
    if len(argv) < 2:
        _die("usage: retarget_clean_fix.py {detect|apply|revert} [--repo R] --worktree W [--conf PATH]")
    cmd = argv[1]
    if cmd not in ("detect", "apply", "revert"):
        _die(f"unknown subcommand: {cmd}")
    repo = ""
    worktree = ""
    conf = DEFAULT_CONF
    rest = argv[2:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        if i + 1 >= len(rest):
            _die(f"missing value for {flag}")
        value = rest[i + 1]
        if flag == "--repo":
            repo = value
        elif flag == "--worktree":
            worktree = value
        elif flag == "--conf":
            conf = Path(value)
        else:
            _die(f"unknown flag: {flag}")
        i += 2
    if not worktree:
        _die("--worktree is required")
    if cmd in ("detect", "apply") and not repo:
        _die("--repo is required for detect/apply")
    return cmd, repo, worktree, conf


def main() -> None:
    cmd, repo, worktree, conf = parse_args(sys.argv)
    lines = read_conf(conf)

    if cmd == "revert":
        out, removed = revert(lines, worktree)
        if removed:
            _ = conf.write_text("\n".join(out) + "\n")
        print(json.dumps({"reverted": bool(removed), "removed": removed}, indent=2))
        return

    result = detect(lines, repo, worktree)
    if cmd == "detect":
        print(json.dumps(result, indent=2))
        return

    # apply
    if not result["match"]:
        print(json.dumps({"applied": False, "reason": "no match"}, indent=2))
        return
    _ = conf.write_text("\n".join(apply(lines, result)) + "\n")
    print(json.dumps({
        "applied": True,
        "redirects": result["redirects"],
        "build_add": result["build_add"],
        "build_already": result["build_already"],
    }, indent=2))


if __name__ == "__main__":
    main()
