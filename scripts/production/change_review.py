#!/usr/bin/env python3
"""The change-review list: the trailer every checkpoint commit carries, and the rows a merge adds.

    change_review.py check --repo R --checkpoint C --previous P --merge-branch B
    change_review.py append --repo R --list L --merged M --checkpoint C --previous P --merge-branch B
    change_review.py drift --repo R --merge-branch B --list L --since YYYY-MM-DD

A checkpoint(...) commit names what the user sees change and how to check it, one line each:
`Change-Review: <what you see change> | <how to check it>`, or `Change-Review: none — <why nothing
visible changes>`. A checkpoint that changes only Markdown needs none. A pushed checkpoint that
missed its line is covered by a later one carrying the lines and `Change-Review-Covers: <hash>`. The merge gate refuses a
unit's checkpoint without one, then adds one row per visible line under the list's "## To review":
the next C number, the push date, the merged hash and "not yet" for the Mac build. It writes the
file and never commits it. A production doc names its list in a `- **Change review:** <path>`
line in its Production Context; a production without one still needs the trailer but keeps no list.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone  # noqa: E402

TRAILER = "Change-Review"
LIST_HEADER = "- **Change review:**"
SECTION = "## To review"
NOT_YET = "not yet"
SHORT = 8
# Commits made before the rule landed carry no line and are not read: 2026-10-15 00:00 EDT.
CHECKED_FROM = 1792036800
LANDED = "2026-10-15"
_NONE = re.compile(r"^none\s*[—–-]+\s*(?P<why>\S.*)$")
_LINE = re.compile(rf"^{TRAILER}:\s*(?P<value>.*)$", re.MULTILINE)
_COVERS = re.compile(rf"^{TRAILER}-Covers:\s*(?P<sha>[0-9a-f]{{7,40}})\s*$", re.MULTILINE)
_NUMBER = re.compile(r"^\| C(\d+) \|", re.MULTILINE)


@dataclass(frozen=True)
class Visible:
    change: str
    check: str


@dataclass(frozen=True)
class Invisible:
    why: str


@dataclass(frozen=True)
class Malformed:
    text: str


Entry = Visible | Invisible | Malformed


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    message: str
    files: tuple[str, ...]


class ChangeReviewError(Exception):
    """A list that cannot take rows, or a git read that failed."""


def parse(value: str) -> Entry:
    text = value.strip()
    if (none := _NONE.match(text)) is not None:
        return Invisible(none.group("why").strip())
    change, separator, check = text.partition(" | ")
    if separator and change.strip() and check.strip() and " | " not in check:
        return Visible(change.strip(), check.strip())
    return Malformed(text)


def entries(message: str) -> list[Entry]:
    return [parse(found.group("value")) for found in _LINE.finditer(message)]


def needs_trailer(subject: str, files: tuple[str, ...]) -> bool:
    """A checkpoint(...) commit needs one unless every file it changes is Markdown."""
    return subject.startswith("checkpoint(") and not (files and all(name.endswith(".md") for name in files))


def problem(subject: str, message: str, files: tuple[str, ...]) -> str:
    """Why a commit's message fails the rule, or the empty string."""
    if not needs_trailer(subject, files):
        return ""
    found = entries(message)
    if not found:
        return (f"no {TRAILER} line: add `{TRAILER}: <what you see change> | <how to check it>` "
                + f"or `{TRAILER}: none — <why nothing visible changes>`")
    bad = [item.text for item in found if isinstance(item, Malformed)]
    if bad:
        return f"{TRAILER} line {bad[0]!r} is neither `<change> | <how to check>` nor `none — <why>`"
    return ""


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ChangeReviewError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout


def unit_commits(repo: Path, checkpoint: str, previous: str, merge_branch: str,
                 checked_from: int = CHECKED_FROM) -> list[Commit]:
    """The unit's own commits since its last checkpoint, without what it merged from the merge branch."""
    records = _git(repo, "log", "--no-merges", "--format=%H %ct", checkpoint, f"^{previous}",
                   f"^origin/{merge_branch}", "--").splitlines()
    return [Commit(sha, *_described(repo, sha)) for sha, stamp in (record.split() for record in records)
            if int(stamp) >= checked_from]


def missing(commits: list[Commit]) -> list[str]:
    """Each checkpoint without a good line that no passing commit names in a Change-Review-Covers line.

    A pushed commit cannot be amended, so a later checkpoint carries the lines for one that missed them.
    """
    covered = [found.group("sha") for commit in commits if entries(commit.message)
               and not problem(commit.subject, commit.message, commit.files) for found in _COVERS.finditer(commit.message)]
    return [f"{commit.sha[:SHORT]} {commit.subject}: {reason}" for commit in commits
            if (reason := problem(commit.subject, commit.message, commit.files))
            and not any(commit.sha.startswith(sha) for sha in covered)]


def visible(commits: list[Commit]) -> list[Visible]:
    return [item for commit in reversed(commits) if needs_trailer(commit.subject, commit.files)
            for item in entries(commit.message) if isinstance(item, Visible)]


def list_path(doc: Path) -> Path | None:
    """The change-review list a production doc names, or None when it keeps none."""
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith(LIST_HEADER):
            value = line.removeprefix(LIST_HEADER).strip().strip("`")
            return Path(value).expanduser() if value else None
    return None


def next_number(text: str) -> int:
    """One past the highest C number anywhere in the list: numbers only go up."""
    return max((int(found.group(1)) for found in _NUMBER.finditer(text)), default=0) + 1


def today() -> date:
    return datetime.now(user_zone()).date()


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def append_rows(path: Path, changes: list[Visible], day: date, merged: str) -> list[str]:
    """Add one row per change at the end of the To review table; return the rows written."""
    if not changes:
        return []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    try:
        section = next(index for index, line in enumerate(lines) if line.strip() == SECTION)
    except StopIteration:
        raise ChangeReviewError(f"{path} has no `{SECTION}` section") from None
    header = next((index for index in range(section + 1, len(lines))
                   if lines[index].startswith("| # |") or lines[index].startswith("## ")), None)
    if header is None or not lines[header].startswith("| # |"):
        raise ChangeReviewError(f"{path}: `{SECTION}` has no `| # |` table")
    end = header + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    number = next_number("".join(lines))
    rows = [f"| C{number + offset} | {_cell(item.change)} | {day.isoformat()} · {merged} | {NOT_YET} | {_cell(item.check)} |"
            for offset, item in enumerate(changes)]
    if lines[end - 1] and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines[end:end] = [row + "\n" for row in rows]
    _ = path.write_text("".join(lines), encoding="utf-8")
    return rows


def drift(repo: Path, merge_branch: str, text: str, since: str) -> list[str]:
    """Each merge on the merge branch since `since` whose visible changes have no row with its hash."""
    lines: list[str] = []
    for record in _git(repo, "log", "--first-parent", "--merges", f"--since={since}",
                       "--format=%H %P", f"origin/{merge_branch}", "--").splitlines():
        merge, *parents = record.split()
        if len(parents) < 2:
            continue
        commits = [Commit(sha, *_described(repo, sha)) for sha in
                   _git(repo, "log", "--no-merges", "--format=%H", parents[1], f"^{parents[0]}", "--").split()]
        short = _git(repo, "rev-parse", f"--short={SHORT}", merge).strip()
        if visible(commits) and f"· {short}" not in text:
            lines.append(f"{short}: {len(visible(commits))} visible change(s) and no row in the list")
    return lines


def _described(repo: Path, sha: str) -> tuple[str, str, tuple[str, ...]]:
    message = _git(repo, "show", "-s", "--format=%B", sha)
    files = tuple(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split())
    return message.split("\n", 1)[0], message, files


class CommandLine(argparse.Namespace):
    action: str = ""
    repo: str = "."
    checkpoint: str = ""
    previous: str = ""
    merge_branch: str = ""
    list: str = ""
    merged: str = ""
    since: str = ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("action", choices=("check", "append", "drift"))
    _ = parser.add_argument("--repo", default=".")
    for name in ("checkpoint", "previous", "merge-branch", "list", "merged", "since"):
        _ = parser.add_argument(f"--{name}", default="")
    options = cast(CommandLine, parser.parse_args(argv))
    repo = Path(options.repo).resolve()
    try:
        if options.action == "drift":
            found = drift(repo, options.merge_branch, Path(options.list).expanduser().read_text(encoding="utf-8"), options.since)
            print("\n".join(found) if found else "no drift")
            return 1 if found else 0
        commits = unit_commits(repo, options.checkpoint, options.previous, options.merge_branch)
        if options.action == "check":
            refused = missing(commits)
            print("\n".join(refused) if refused else f"{len(commits)} commit(s) carry their {TRAILER} lines")
            return 1 if refused else 0
        rows = append_rows(Path(options.list).expanduser(), visible(commits), today(), options.merged)
        print("\n".join(rows) if rows else "no visible change to add")
        return 0
    except (ChangeReviewError, OSError) as error:
        print(f"change review: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
