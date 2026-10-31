#!/usr/bin/env python3
"""Find the paths an assigned or pooled review changed in its working tree.

`baseline` records HEAD and, for each path `git status` names, its HEAD, index and worktree
entries. `changes` compares every path that was dirty before or after the turn, or that HEAD
moved under, and prints each one whose entries differ. A path git calls clean holds its HEAD
entry in the index and the worktree, so only dirty paths are read from disk.

With two or more phases open in the session, a changed path that `phase_reservation.attribute`
gives to an open phase other than the reviewed one is that phase's writer at work, not the
review's: it is noted on stderr and left out. Every other changed path stays the review's.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_reservation  # noqa: E402

# A path's HEAD, index and worktree entries, each `<mode> <sha1>`, or empty where it has none.
Entries = tuple[str, str, str]


class Baseline(TypedDict):
    head: str
    paths: dict[str, list[str]]


def git(root: Path, *command: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), "--literal-pathspecs", *command],
                            capture_output=True, check=False, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(command[:2])} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def records(output: bytes) -> list[str]:
    return [os.fsdecode(record) for record in output.split(b"\0") if record]


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").decode().strip()


def dirty_paths(root: Path) -> list[str]:
    output = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    return [record[3:] for record in records(output)]


def tree_entries(root: Path, commit: str, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    entries: dict[str, str] = {}
    for record in records(git(root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths)):
        meta, path = record.split("\t", 1)
        mode, _, sha = meta.split(" ")
        entries[path] = f"{mode} {sha}"
    return entries


def index_entries(root: Path, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    entries: dict[str, str] = {}
    for record in records(git(root, "ls-files", "-s", "-z", "--full-name", "--", *paths)):
        meta, path = record.split("\t", 1)
        mode, sha, stage = meta.split(" ")
        entry = f"{mode} {sha}" if stage == "0" else f"{mode} {sha} {stage}"
        entries[path] = f"{entries[path]}, {entry}" if path in entries else entry
    return entries


def worktree_entry(path: Path) -> str:
    """The path as git would stage it: its mode and blob id, or empty when there is no file."""
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            mode, data = "120000", os.fsencode(os.readlink(path))
        elif stat.S_ISREG(status.st_mode):
            mode, data = ("100755" if status.st_mode & 0o100 else "100644"), path.read_bytes()
        else:
            return ""
    except OSError:
        return ""
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return f"{mode} {blob}"


def dirty_entries(root: Path, commit: str, paths: list[str]) -> dict[str, Entries]:
    heads = tree_entries(root, commit, paths)
    indexes = index_entries(root, paths)
    return {path: (heads.get(path, ""), indexes.get(path, ""), worktree_entry(root / path)) for path in paths}


def clean_entries(heads: dict[str, str], path: str) -> Entries:
    entry = heads.get(path, "")
    return (entry, entry, entry)


def toplevel(directory: str) -> Path:
    return Path(git(Path(directory), "rev-parse", "--show-toplevel").decode().strip())


def read_baseline(path: str) -> Baseline:
    return cast(Baseline, json.loads(Path(path).read_text(encoding="utf-8")))


def changed_paths(root: Path, baseline: Baseline) -> list[str]:
    before_head, after_head = baseline["head"], head(root)
    before = {path: cast(Entries, tuple(entries)) for path, entries in baseline["paths"].items()}
    dirty = dirty_paths(root)
    moved = records(git(root, "diff", "--name-only", "-z", "--no-renames", before_head, after_head)) \
        if after_head != before_head else []
    candidates = sorted(set(before) | set(dirty) | set(moved))
    before_heads = tree_entries(root, before_head, [path for path in candidates if path not in before])
    after = dirty_entries(root, after_head, dirty)
    after_heads = tree_entries(root, after_head, [path for path in candidates if path not in after])
    return [path for path in candidates
            if before.get(path, clean_entries(before_heads, path))
            != after.get(path, clean_entries(after_heads, path))]


def other_phases_paths(session_dir: Path, phase: str, paths: list[str],
                       root: Path | phase_reservation.AllPhaseWorktrees =
                       phase_reservation.ALL_PHASE_WORKTREES) -> dict[str, list[str]]:
    """The changed paths each other open phase's files list owns, when the reviewed phase is open beside it."""
    phases = phase_reservation.open_phases(session_dir)
    reviewed = phase_reservation.phase_directory_name(phase)
    if len(phases) < 2 or reviewed not in {open_phase.phase for open_phase in phases}:
        return {}
    attribution = phase_reservation.attribute(phases, paths, root)
    if isinstance(attribution, phase_reservation.NoOpenPhaseHere):
        return {}
    owned = attribution.by_phase
    return {owner.phase: scopes for owner, scopes in owned.items() if owner.phase != reviewed}


def baseline_command(args: argparse.Namespace) -> int:
    root = toplevel(cast(str, args.root))
    commit = head(root)
    entries = dirty_entries(root, commit, dirty_paths(root))
    baseline: Baseline = {"head": commit, "paths": {path: list(value) for path, value in entries.items()}}
    _ = Path(cast(str, args.out)).write_text(json.dumps(baseline), encoding="utf-8")
    return 0


def changes_command(args: argparse.Namespace) -> int:
    root = toplevel(cast(str, args.root))
    changed = changed_paths(root, read_baseline(cast(str, args.baseline)))
    others = other_phases_paths(Path(cast(str, args.session_dir)), cast(str, args.phase), changed, root)
    for owner, scopes in sorted(others.items()):
        print(f"review tree: open phase {owner}'s files changed during the review: {' '.join(scopes)}",
              file=sys.stderr)
    left_out = {path for scopes in others.values() for path in scopes}
    for path in changed:
        if path not in left_out:
            print(path)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    baseline = commands.add_parser("baseline", help="record the tree before the review's turn")
    _ = baseline.add_argument("--root", required=True)
    _ = baseline.add_argument("--out", required=True)
    changes = commands.add_parser("changes", help="print the paths the review changed since its baseline")
    _ = changes.add_argument("--root", required=True)
    _ = changes.add_argument("--baseline", required=True)
    _ = changes.add_argument("--session-dir", required=True)
    _ = changes.add_argument("--phase", default="")
    args = parser.parse_args(argv)
    command = cast(str, args.command)
    return baseline_command(args) if command == "baseline" else changes_command(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
