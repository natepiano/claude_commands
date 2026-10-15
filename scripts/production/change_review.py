#!/usr/bin/env python3
"""The change-review list: the trailer every checkpoint commit carries, and the rows a merge adds.

    change_review.py check --repo R --checkpoint C --previous P --merge-branch B
    change_review.py append --repo R --list L --merged M --checkpoint C --previous P --merge-branch B
    change_review.py drift --repo R --merge-branch B --list L --since YYYY-MM-DD
    change_review.py add --list L --change "<what changed>" --check "<how to check it>" [--merged M]
    change_review.py show --list L
    change_review.py verdict --list L --row C<n> --verdict "<the user's words>"
    change_review.py list-path [--production DOC]

A checkpoint(...) commit names what the user sees change and how to check it, one line each:
`Change-Review: <what you see change> | <how to check it>`, or `Change-Review: none — <why nothing
visible changes>`. A checkpoint that changes only Markdown needs none. A pushed checkpoint that
missed its line is covered by a later one carrying the lines and `Change-Review-Covers: <hash>`. The merge gate refuses a
unit's checkpoint without one, then adds one row per visible line under the list's "## To review":
the next C number, the push date and the merged hash, and "not yet" when the table has an "On the
Mac build" column. `add` writes one row for a change no checkpoint line carries; `show` prints the
rows still to review; `verdict` moves a row to "## Reviewed" with the user's verdict and today's
date. Every write holds a lock on the list's directory, and the first write to a missing list
creates it from the skeleton. A list in a git work tree is committed alone after each write, still
under the lock, and never pushed; a refused commit warns and the next write commits it. A
production doc names its list in a `- **Change review:** <path>` line in its Production Context;
`list-path` prints it, from the calling showrunner's update timer when no doc is given.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TypeVar, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone  # noqa: E402

TRAILER = "Change-Review"
LIST_HEADER = "- **Change review:**"
SECTION = "## To review"
REVIEWED = "## Reviewed"
CHECK = "How to check"
NOT_YET = "not yet"
SHORT = 8
# Commits made before the rule landed carry no line and are not read: 2026-10-15 00:00 EDT.
CHECKED_FROM = 1792036800
LANDED = "2026-10-15"
_NONE = re.compile(r"^none\s*[—–-]+\s*(?P<why>\S.*)$")
_LINE = re.compile(rf"^{TRAILER}:\s*(?P<value>.*)$", re.MULTILINE)
_COVERS = re.compile(rf"^{TRAILER}-Covers:\s*(?P<sha>[0-9a-f]{{7,40}})\s*$", re.MULTILINE)
_NUMBER = re.compile(r"^\| C(\d+) \|", re.MULTILINE)
_ROW = re.compile(r"^[A-Z]\d+$")
_LANDS = re.compile(r"^(works|fine)\b", re.IGNORECASE)
# A cell boundary is a `|` that _cell did not escape.
_BOUNDARY = re.compile(r"(?<!\\)\|")
SKELETON = """# Change review

Ask for "change review" any time to walk it one change at a time. A reviewed row moves to Reviewed with your verdict.

## To review

| # | Change | Merged | How to check |
| --- | --- | --- | --- |

## Reviewed

| # | Change | Verdict | Date |
| --- | --- | --- | --- |
"""
Result = TypeVar("Result")


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


def _plain(cell: str) -> str:
    return cell.replace("\\|", "|")


def _cells(line: str) -> list[str]:
    """A table row's cells, still escaped as written."""
    return [cell.strip() for cell in _BOUNDARY.split(line.strip())[1:-1]]


def read(path: Path) -> str:
    """The list's text; a list not yet written reads as the skeleton."""
    return path.read_text(encoding="utf-8") if path.exists() else SKELETON


def _edit(path: Path, write: Callable[[list[str]], Result], what: Callable[[Result], str]) -> Result:
    """Change the list's lines under the lock, write and commit them; a change that raises writes nothing.

    The lock is the list's directory, so concurrent writers never lose a row and no lock file
    lands beside the list. A missing list starts from the skeleton. `what` names the write in
    the commit subject.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        lines = read(path).splitlines(keepends=True)
        result = write(lines)
        _ = path.write_text("".join(lines), encoding="utf-8")
        _commit(path, what(result))
        return result
    finally:
        os.close(directory)


def _commit(path: Path, what: str) -> None:
    """Commit the list alone when it sits in a git work tree; nothing is pushed.

    `--only` with the one path commits the file's whole content and leaves every other change,
    staged or not, where it was. A refusal (another git process holding index.lock, say) warns
    on one line and leaves the write standing: the next write's commit carries this one too.
    """

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        # Literal pathspecs: a list name could hold glob characters.
        return subprocess.run(["git", "-C", str(path.parent), "--literal-pathspecs", *arguments],
                              capture_output=True, text=True, check=False)

    if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return
    steps = [] if git("ls-files", "--", path.name).stdout.strip() else [["add", "--intent-to-add", "--", path.name]]
    steps.append(["commit", "--quiet", "--only", "-m", f"change-review: {path.stem} {what}", "--", path.name])
    for step in steps:
        done = git(*step)
        if done.returncode:
            reason = next(iter((done.stderr or done.stdout).strip().splitlines()), f"git {step[0]} failed")
            print(f"change review: {path} written but not committed: {reason}; the next write commits it",
                  file=sys.stderr)
            return


def _table(path: Path, lines: list[str], section: str) -> tuple[list[str], int, int]:
    """The column names of the table under `section`, the index of its first row and one past its last."""
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == section)
    except StopIteration:
        raise ChangeReviewError(f"{path} has no `{section}` section") from None
    header = next((index for index in range(start + 1, len(lines))
                   if lines[index].startswith("| # |") or lines[index].startswith("## ")), None)
    if header is None or not lines[header].startswith("| # |"):
        raise ChangeReviewError(f"{path}: `{section}` has no `| # |` table")
    end = header + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return _cells(lines[header]), min(header + 2, end), end


def _row(path: Path, columns: list[str], values: dict[str, str]) -> str:
    """A row with each column's value, in the table's own column order."""
    if unknown := [column for column in columns if column not in values]:
        raise ChangeReviewError(f"{path}: no value for the `{unknown[0]}` column")
    return "| " + " | ".join(values[column] for column in columns) + " |"


def _insert(lines: list[str], at: int, rows: list[str]) -> None:
    if at and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines[at:at] = [row + "\n" for row in rows]


def append_rows(path: Path, changes: list[Visible], day: date, merged: str) -> list[str]:
    """Add one row per change at the end of the To review table; return the rows written.

    `merged` is the merge's hash, or empty for a change no merge names. The Mac build column is
    written only when the table has one.
    """
    if not changes:
        return []

    def write(lines: list[str]) -> list[str]:
        columns, _, end = _table(path, lines, SECTION)
        number = next_number("".join(lines))
        when = f"{day.isoformat()} · {_cell(merged)}" if merged else day.isoformat()
        rows = [_row(path, columns, {"#": f"C{number + offset}", "Change": _cell(item.change), "Merged": when,
                                     "On the Mac build": NOT_YET, CHECK: _cell(item.check)})
                for offset, item in enumerate(changes)]
        _insert(lines, end, rows)
        return rows

    def what(rows: list[str]) -> str:
        ids = [_cells(row)[0] for row in rows]
        named = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
        if not merged:
            return f"add {named}"
        return f"{'row' if len(ids) == 1 else 'rows'} {named} from {merged}"

    return _edit(path, write, what)


def pending(path: Path) -> list[dict[str, str]]:
    """Each row still to review, as its cells by column name. Reads only."""
    lines = read(path).splitlines(keepends=True)
    columns, first, end = _table(path, lines, SECTION)
    return [dict(zip(columns, (_plain(cell) for cell in _cells(line)))) for line in lines[first:end]]


def describe(row: dict[str, str]) -> str:
    """One row in plain lines: its id and change, how to check it, then each other column."""
    rest = [f"  {column}: {value}" for column, value in row.items() if column not in ("#", "Change", CHECK)]
    return "\n".join([f"{row.get('#', '')} {row.get('Change', '')}", f"  {CHECK}: {row.get(CHECK, '')}", *rest])


def lands(verdict: str) -> bool:
    """A verdict beginning `works` or `fine` lands the row; any other returns it to its unit."""
    return _LANDS.match(verdict.strip()) is not None


def record_verdict(path: Path, row: str, verdict: str, day: date) -> str:
    """Move `row` from To review to the top of the Reviewed table; return its Merged cell."""

    def write(lines: list[str]) -> str:
        columns, first, end = _table(path, lines, SECTION)
        index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
        if index is None:
            raise ChangeReviewError(f"{row} is not on the list to review")
        cells = dict(zip(columns, _cells(lines.pop(index))))
        reviewed, top, _ = _table(path, lines, REVIEWED)
        _insert(lines, top, [_row(path, reviewed, {"#": row, "Change": cells.get("Change", ""),
                                                   "Verdict": _cell(verdict), "Date": day.isoformat()})])
        return _plain(cells.get("Merged", ""))

    return _edit(path, write, lambda _: f"verdict {row}")


def production_list(production: str) -> Path:
    """The list the production doc names; without a doc, the calling showrunner's production."""
    if production:
        doc = Path(production).expanduser()
    else:
        import showrunners  # only this action reads the update timers

        found = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        if found is None:
            raise ChangeReviewError("this session is no production's showrunner; pass --production <doc>")
        doc = found
    path = list_path(doc)
    if path is None:
        raise ChangeReviewError(f"{doc} has no `{LIST_HEADER}` line")
    return path


def _words(name: str, value: str) -> str:
    """One line of words for a cell; empty or several lines is refused."""
    if not value.strip() or "\n" in value or "\r" in value:
        raise ChangeReviewError(f"--{name} needs one line of words")
    return value.strip()


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
    change: str = ""
    check: str = ""
    row: str = ""
    verdict: str = ""
    production: str = ""


def _run(options: CommandLine) -> int:
    review_list = Path(options.list).expanduser()
    match options.action:
        case "add":
            rows = append_rows(review_list, [Visible(_words("change", options.change), _words("check", options.check))],
                               today(), options.merged.strip())
            print(_cells(rows[0])[0])
        case "show":
            waiting = pending(review_list)
            print("\n\n".join(describe(row) for row in waiting) if waiting else "nothing to review")
        case "verdict":
            row = options.row.strip()
            if _ROW.match(row) is None:
                raise ChangeReviewError(f"--row {row!r} is not a row id such as C3")
            verdict = _words("verdict", options.verdict)
            merged = record_verdict(review_list, row, verdict, today())
            print(f"{row} landed" if lands(verdict) else f"{row} returns to its unit (merged {merged})")
        case "list-path":
            print(production_list(options.production))
        case "drift":
            found = drift(Path(options.repo).resolve(), options.merge_branch, read(review_list), options.since)
            print("\n".join(found) if found else "no drift")
            return 1 if found else 0
        case _:
            commits = unit_commits(Path(options.repo).resolve(), options.checkpoint, options.previous,
                                   options.merge_branch)
            if options.action == "check":
                refused = missing(commits)
                print("\n".join(refused) if refused else f"{len(commits)} commit(s) carry their {TRAILER} lines")
                return 1 if refused else 0
            rows = append_rows(review_list, visible(commits), today(), options.merged)
            print("\n".join(rows) if rows else "no visible change to add")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("action", choices=("check", "append", "drift", "add", "show", "verdict", "list-path"))
    _ = parser.add_argument("--repo", default=".")
    for name in ("checkpoint", "previous", "merge-branch", "list", "merged", "since", "change", "check", "row",
                 "verdict", "production"):
        _ = parser.add_argument(f"--{name}", default="")
    try:
        return _run(cast(CommandLine, parser.parse_args(argv)))
    except (ChangeReviewError, OSError) as error:
        print(f"change review: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
