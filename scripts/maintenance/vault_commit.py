"""Commit a job's generated notes in the hanadocs vault when more than their per-run lines changed.

Why: the vault is a git repo that sessions edit by hand while jobs rewrite their
generated notes in it. An uncommitted generated note sits in every session's
`git status` and is swept into someone else's commit, so each job commits its own
notes after writing them, and only those.

How: for each path given, the working-tree text and HEAD's text are compared with
every line matching a --timestamp pattern removed. When any path differs, the
paths that differ are committed with `git commit --only`, which leaves every other
change in the vault, staged or not, where it was. A file new to git is first
marked with `git add --intent-to-add`. Nothing is pushed here; ./hanadocs-push.nix
pushes on a timer of its own. A failed commit (another git process holding
index.lock, say) is printed to stderr and still exits 0: the difference is still
there, so the job's next run commits it. Called by agent-notes and the Nix configuration-note wrapper.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import cast

from scripts.maintenance.settings import settings
def git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Literal pathspecs: note names hold spaces and could hold glob characters.
    command = ["git", "-C", repo, "--literal-pathspecs", *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=settings().vault_git_seconds)


@dataclass(frozen=True)
class NoteContent:
    text: str


@dataclass(frozen=True)
class NoteAbsent:
    """This path has no note in the compared tree."""


def read(path: str) -> NoteContent | NoteAbsent:
    try:
        with open(path, encoding="utf-8") as handle:
            return NoteContent(handle.read())
    except FileNotFoundError:
        return NoteAbsent()


def without(note: NoteContent | NoteAbsent, patterns: list[re.Pattern[str]]) -> NoteContent | NoteAbsent:
    """Compare note contents without timestamps, preserving an absent path."""
    if isinstance(note, NoteAbsent):
        return note
    return NoteContent("\n".join(line for line in note.text.splitlines() if not any(p.search(line) for p in patterns)))


def changed(repo: str, rels: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    """The paths whose text, timestamps aside, differs from HEAD's."""
    out: list[str] = []
    for rel in rels:
        shown = git(repo, "show", f"HEAD:{rel}")
        head = NoteContent(shown.stdout) if shown.returncode == 0 else NoteAbsent()
        if without(read(os.path.join(repo, rel)), patterns) != without(head, patterns):
            out.append(rel)
    return out


def subject(job: str, rels: list[str]) -> str:
    named = settings().vault_named_paths
    names = [rel.removesuffix(".md") for rel in rels]
    if len(names) > named + 1:
        names = [*names[:named], f"{len(rels) - named} more"]
    return f"{job}: update {', '.join(names)}"


def commit(repo: str, job: str, rels: list[str]) -> bool:
    """Commit exactly these paths; False, with the reason on stderr, when git refuses."""
    untracked = [rel for rel in rels if not git(repo, "ls-files", "--", rel).stdout.strip()]
    steps = [["add", "--intent-to-add", "--", *untracked]] if untracked else []
    steps.append(["commit", "--quiet", "--only", "-m", subject(job, rels), "--", *rels])
    for step in steps:
        done = git(repo, *step)
        if done.returncode != 0:
            reason = (done.stderr or done.stdout).strip()
            print(f"{job}: commit failed, next run retries: {reason}", file=sys.stderr)
            return False
    print(subject(job, rels))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    _ = parser.add_argument("--job", required=True, help="the job's name, which opens the commit subject")
    _ = parser.add_argument(
        "--timestamp", action="append", default=[], help="a regex for lines that alone never warrant a commit"
    )
    _ = parser.add_argument("paths", nargs="+", help="the job's own notes, absolute or relative to here")
    args = parser.parse_args(argv)
    job = cast(str, args.job)
    paths = [os.path.realpath(p) for p in cast(list[str], args.paths)]
    patterns = [re.compile(p) for p in cast(list[str], args.timestamp)]
    top = git(os.path.dirname(paths[0]), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print(f"{job}: not committed, {os.path.dirname(paths[0])} is not in a git repo", file=sys.stderr)
        return 0
    repo = os.path.realpath(top.stdout.strip())
    rels = changed(repo, [os.path.relpath(p, repo) for p in paths], patterns)
    if not rels:
        print(f"{job}: nothing to commit beyond timestamps")
        return 0
    _ = commit(repo, job, rels)
    return 0


if __name__ == "__main__":
    sys.exit(main())
