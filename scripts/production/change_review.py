#!/usr/bin/env python3
"""The change-review list: the trailer every checkpoint commit carries, and the rows a merge adds.

    change_review.py check --repo R --checkpoint C --previous P --merge-branch B
    change_review.py append --repo R [--production DOC] [--list L] --merged M --checkpoint C --previous P --merge-branch B
    change_review.py drift --repo R --merge-branch B [--production DOC] [--list L] --since YYYY-MM-DD
    change_review.py add [--production DOC] [--list L] --change "<what changed for you>" --check "<how to try it>" --see "<what you see>" [--merged M] [--urgency user|blocks|soon|normal|later] [--due ISO] [--after C<n>]
    change_review.py edit [--production DOC] [--list L] --row C<n> --change "<what changed for you>" --check "<how to try it>" --see "<what you see>"
    change_review.py show [--production DOC] [--list L]
    change_review.py verdict [--production DOC] [--list L] --row C<n> --verdict "<the user's words>"
    change_review.py mark-important [--production DOC] [--list L] --row C<n>
    change_review.py list-path [--production DOC]
    change_review.py showrunner [--production DOC]

A checkpoint(...) commit tells a person with no context what changed, how to try it and what they
see, one line each: `Change-Review: <what changed for you> | <how to try it: where and what to type> |
<what you see>`, three parts split on " | ", where "where" names the machine (natedev or the Mac) and
the session (a terminal, a showrunner session, a unit session or any Claude session). For example:
`Change-Review: Panels keep their shields | In a natedev terminal, run `hana` | Each panel shows its
shield`. A line with another number of parts, or an empty part, is refused. `Change-Review: none —
<why nothing visible changes>` names a change the user cannot see. A checkpoint that changes only
Markdown needs none. A pushed checkpoint that
missed its line is covered by a later one carrying the lines and `Change-Review-Covers: <hash>`. The merge gate refuses a
unit's checkpoint without one, then adds one row per visible line under the list's "## To review":
the next C number, the push date and the merged hash, the How to check cell `<how to try it> You
see: <what you see>`, and "not yet" when the table has an "On the Mac build" column. `add` writes one
row for a change no checkpoint line carries; `edit` rewrites one row still to review in place and keeps
its id, Merged cell and position; `show` prints the current row and how many wait after it; `verdict` moves a row to "## Reviewed" with the user's verdict and today's
date; `mark-important` marks a row the user called important.

To review keeps the order of urgency.py, rewritten on every write: the row `show` last printed
first until its verdict, then by urgency, then by C number. A row's Urgency cell holds its level,
due time and prerequisite (`soon, due 2026-10-22, after C3`); it is empty for a normal row, and a
list gains the column on its first row that needs it. Every write holds a lock on the list's directory, and the first write to a missing list
creates it from the skeleton. Every write sets the frontmatter's date_modified and adds the
writing production's doc to its productions. A list in a git work tree is committed alone after
each write, still under the lock, and never pushed; a refused commit warns and the next write
commits it.

Each top-level showrunner keeps one list, `change-review/change-review - <showrunner>.md`, and a
child production's rows go to its root's list. A production opts in with a `- **Change review:**
<path>` line in its Production Context. While the root showrunner runs, its session name names the
list; otherwise the line's path does. `list-path` prints the list and `showrunner` the name, each
from the calling showrunner's update timer when no doc is given; add, show and verdict find the
list the same way unless --list names one; edit does too. Every write names its production, the same way.
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
import urgency  # noqa: E402
from user_zone import user_zone  # noqa: E402

# The merge gate imports this module as scripts.production.change_review; its siblings still resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import issues  # noqa: E402

TRAILER = "Change-Review"
LIST_HEADER = "- **Change review:**"
SECTION = "## To review"
REVIEWED = "## Reviewed"
CHECK = "How to check"
URGENCY = "Urgency"
NOT_YET = "not yet"
FORM = "<what changed for you> | <how to try it: where and what to type> | <what you see>"
SEPARATOR = " | "
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
LISTS = Path(os.environ.get("CHANGE_REVIEW_DIR") or Path.home() / "rust/hanadocs/change-review")
PREFIX = "change-review - "
FENCE = "---"
PRODUCTIONS = "productions"
_KEY = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")
_ITEM = re.compile(r"^\s+-\s*(.*)$")
# The tables every list holds; a new list puts its frontmatter and title above them.
BODY = """Ask for "change review" any time to walk it one change at a time. A reviewed row moves to Reviewed with your verdict.

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
    see: str

    def how(self) -> str:
        """The row's How to check cell: how to try the change, then what the user sees."""
        stop = "" if self.check.endswith((".", "!", "?", ":")) else "."
        return f"{self.check}{stop} You see: {self.see}"


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
    parts = [part.strip() for part in text.split(SEPARATOR)]
    if len(parts) == 3 and all(parts):
        return Visible(parts[0], parts[1], parts[2])
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
        return f"no {TRAILER} line: add `{TRAILER}: {FORM}` or `{TRAILER}: none — <why nothing visible changes>`"
    bad = [item.text for item in found if isinstance(item, Malformed)]
    if bad:
        return (f"{TRAILER} line {bad[0]!r} needs three parts split by {SEPARATOR.strip()!r}: "
                + f"`{TRAILER}: {FORM}`, or `{TRAILER}: none — <why nothing visible changes>`")
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
    """Every visible change a checkpoint names, also one whose files are all Markdown and need no line."""
    return [item for commit in reversed(commits) if commit.subject.startswith("checkpoint(")
            for item in entries(commit.message) if isinstance(item, Visible)]


def _line_value(doc: Path) -> str | None:
    """The path the doc's Change review line names: its first `quoted` part, else the words before
    any ` — ` note. None when the doc has no line."""
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith(LIST_HEADER):
            value = line.removeprefix(LIST_HEADER).strip()
            quoted = re.match(r"`([^`]+)`", value)
            return quoted.group(1).strip() if quoted else value.split(" — ")[0].strip()
    return None


def list_path(doc: Path) -> Path | None:
    """The production's list, or None when its doc has no Change review line.

    The list is named for the top-level showrunner while it runs; otherwise it is the path the
    line names. A line naming another path than the showrunner's list is warned about.
    """
    value = _line_value(doc)
    if value is None:
        return None
    named = Path(value).expanduser() if value else None
    try:
        derived = list_file(showrunner_of(doc))
    except (ChangeReviewError, OSError):
        return named
    if named is not None and named.absolute() != derived.absolute():
        print(f"change review: {doc} names {named} in its Change review line; the list is {derived}",
              file=sys.stderr)
    return derived


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


def row_id(row: str) -> str:
    """A written row's id, such as C24."""
    return _cells(row)[0]


def read(path: Path) -> str:
    """The list's text; a list not yet written reads as the skeleton's tables."""
    return path.read_text(encoding="utf-8") if path.exists() else BODY


def list_file(showrunner: str) -> Path:
    """The list of the top-level showrunner called `showrunner`."""
    return LISTS / f"{PREFIX}{showrunner}.md"


def _owner(path: Path) -> str:
    """The showrunner a list is named for."""
    return path.stem.removeprefix(PREFIX)


def _shown(doc: Path) -> str:
    """The doc's absolute path, under `~` when it is in the home directory."""
    text, home = str(doc.expanduser().absolute()), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _vault_date(day: date) -> str:
    """A date as the vault writes it: a quoted link to the day's note."""
    return f'"[[{day.isoformat()}]]"'


def skeleton(path: Path, production: Path, day: date) -> str:
    """A new list: frontmatter in key order, the showrunner's title, then the empty tables."""
    owner = _owner(path)
    return "\n".join([FENCE, f"date_created: {_vault_date(day)}", f"date_modified: {_vault_date(day)}",
                      f"{PRODUCTIONS}:", f'  - "{_shown(production)}"', f"showrunner: {owner}", FENCE, "",
                      f"# {owner} showrunner: changes to review", "", BODY])


def _key_span(lines: list[str], end: int, key: str) -> tuple[int, int] | None:
    """The key's line and the lines continuing it, as a slice of the frontmatter, or None."""
    for start in range(1, end):
        found = _KEY.match(lines[start])
        if found is None or found.group(1) != key:
            continue
        stop = start + 1
        while stop < end and _KEY.match(lines[stop]) is None:
            stop += 1
        return start, stop
    return None


def _set_key(lines: list[str], end: int, key: str, value: list[str]) -> None:
    """Replace the key's lines with `value`, or insert them before the first later key."""
    span = _key_span(lines, end, key)
    if span is None:
        at = next((index for index in range(1, end) if (found := _KEY.match(lines[index])) is not None
                   and found.group(1) > key), end)
        lines[at:at] = value
    else:
        lines[span[0]:span[1]] = value


def _stamp(path: Path, lines: list[str], production: Path, day: date) -> None:
    """Set date_modified and add `production` to productions; a list without frontmatter gains one."""
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        lines[0:0] = [f"{FENCE}\n", f"date_created: {_vault_date(day)}\n", f"showrunner: {_owner(path)}\n",
                      f"{FENCE}\n", "\n"]
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == FENCE), None)
    if end is None:
        raise ChangeReviewError(f"{path} has no closing --- for its frontmatter")
    _set_key(lines, end, "date_modified", [f"date_modified: {_vault_date(day)}\n"])
    end = next(index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == FENCE)
    span = _key_span(lines, end, PRODUCTIONS)
    listed: list[str] = []
    if span is not None:
        inline = cast(re.Match[str], _KEY.match(lines[span[0]])).group(2)
        listed = [*issues.values(inline), *(issues.plain(item.group(1)) for line in lines[span[0] + 1:span[1]]
                                            if (item := _ITEM.match(line)) is not None)]
    target = production.expanduser().absolute()
    if any(Path(item).expanduser().absolute() == target for item in listed):
        return
    _set_key(lines, end, PRODUCTIONS, [f"{PRODUCTIONS}:\n", *(f'  - "{item}"\n' for item in [*listed, _shown(production)])])


def _edit(path: Path, production: Path, day: date, write: Callable[[list[str]], Result],
          what: Callable[[Result], str]) -> Result:
    """Change the list's lines under the lock, write and commit them; a change that raises writes nothing.

    The lock is the list's directory, so concurrent writers never lose a row and no lock file
    lands beside the list. A missing list starts from the skeleton. Every write stamps the
    frontmatter for `production` on `day`. `what` names the write in the commit subject.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        text = path.read_text(encoding="utf-8") if path.exists() else skeleton(path, production, day)
        lines = text.splitlines(keepends=True)
        result = write(lines)
        _sort(path, lines)
        _stamp(path, lines, production, day)
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
    steps.append(["commit", "--quiet", "--only", "-m", f"change-review: {_owner(path)} {what}", "--", path.name])
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


def queue(path: Path) -> str:
    """The name urgency.py pins this list's current row under."""
    return f"change-{_owner(path)}"


def _add_column(path: Path, lines: list[str], section: str, name: str) -> None:
    """Give the table under `section` an empty last column `name`, unless it has one."""
    columns, first, end = _table(path, lines, section)
    if name in columns:
        return
    header = first - 2
    for index in range(header, end):
        cell = f" {name} |" if index == header else " --- |" if index == header + 1 else " |"
        lines[index] = lines[index].rstrip("\r\n") + cell + "\n"


def _rank(row: dict[str, str]) -> tuple[urgency.Urgency, str]:
    """A row's urgency and prerequisite, from its Urgency cell."""
    try:
        return urgency.parse_cell(row.get(URGENCY, ""))
    except urgency.UrgencyError as error:
        raise ChangeReviewError(f"{row.get('#', '')}: {error}") from None


def _number(row_text: str) -> int:
    found = re.match(r"C(\d+)$", row_text)
    return int(found.group(1)) if found else 0


def _merged_day(row: dict[str, str]) -> datetime | None:
    """The day the row's Merged cell starts with: the row's age."""
    try:
        return datetime.fromisoformat(row.get("Merged", "").split(" ", 1)[0])
    except ValueError:
        return None


def row_entries(rows: list[dict[str, str]]) -> list[urgency.Entry[dict[str, str]]]:
    """The rows as urgency.py entries, by C number: each with its level, merge day and prerequisite."""
    entries: list[urgency.Entry[dict[str, str]]] = []
    for row in sorted(rows, key=lambda row: _number(row.get("#", ""))):
        level, after = _rank(row)
        entries.append(urgency.Entry(row.get("#", ""), row, level, _merged_day(row), after=after))
    return entries


def ordered(rows: list[dict[str, str]], path: Path) -> list[dict[str, str]]:
    """The rows in urgency.py's order: the pinned row first while it waits, then by urgency, then by C
    number; a row the user moved to the end last."""
    return [entry.item for entry in urgency.queue_order(row_entries(rows), queue(path))]


def _sort(path: Path, lines: list[str]) -> None:
    """Write the To review rows in the order `show` takes them."""
    columns, first, end = _table(path, lines, SECTION)
    rows = {_cells(line)[0]: line for line in lines[first:end]}
    named = [dict(zip(columns, (_plain(cell) for cell in _cells(line)))) for line in lines[first:end]]
    lines[first:end] = [rows[row["#"]] for row in ordered(named, path)]


def append_rows(path: Path, changes: list[Visible], day: date, merged: str, production: Path,
                rank: str = "") -> list[str]:
    """Add one row per change to the To review table; return the rows written.

    `merged` is the merge's hash, or empty for a change no merge names. The Mac build column is
    written only when the table has one. `production` is the doc whose change the rows are.
    `rank` is the rows' Urgency cell; empty is a normal row.
    """
    if not changes:
        return []

    def write(lines: list[str]) -> list[str]:
        if rank:
            _add_column(path, lines, SECTION, URGENCY)
        columns, _, end = _table(path, lines, SECTION)
        number = next_number("".join(lines))
        when = f"{day.isoformat()} · {_cell(merged)}" if merged else day.isoformat()
        rows = [_row(path, columns, {"#": f"C{number + offset}", "Change": _cell(item.change), "Merged": when,
                                     "On the Mac build": NOT_YET, CHECK: _cell(item.how()), URGENCY: _cell(rank)})
                for offset, item in enumerate(changes)]
        _insert(lines, end, rows)
        return rows

    def what(rows: list[str]) -> str:
        ids = [row_id(row) for row in rows]
        named = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
        if not merged:
            return f"add {named}"
        return f"{'row' if len(ids) == 1 else 'rows'} {named} from {merged}"

    return _edit(path, production, day, write, what)


def pending(path: Path) -> list[dict[str, str]]:
    """Each row still to review, as its cells by column name. Reads only."""
    lines = read(path).splitlines(keepends=True)
    columns, first, end = _table(path, lines, SECTION)
    return [dict(zip(columns, (_plain(cell) for cell in _cells(line)))) for line in lines[first:end]]


def describe(row: dict[str, str]) -> str:
    """One row in plain lines: its id and change, how to check it, then each other column that has a value."""
    rest = [f"  {column}: {value}" for column, value in row.items() if column not in ("#", "Change", CHECK) and value]
    return "\n".join([f"{row.get('#', '')} {row.get('Change', '')}", f"  {CHECK}: {row.get(CHECK, '')}", *rest])


def lands(verdict: str) -> bool:
    """A verdict beginning `works` or `fine` lands the row; any other returns it to its unit."""
    return _LANDS.match(verdict.strip()) is not None


def _to_review(path: Path, lines: list[str], row: str) -> tuple[list[str], int]:
    """The To review table's column names and the index of `row` in it; a row not there is refused."""
    columns, first, end = _table(path, lines, SECTION)
    index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
    if index is None:
        raise ChangeReviewError(f"{row} is not on the list to review")
    return columns, index


def edit_row(path: Path, row: str, item: Visible, day: date, production: Path) -> str:
    """Rewrite the Change and How to check cells of `row` in To review; return the row written.

    The row keeps its id, its Merged cell, every other cell and its place in the table.
    """

    def write(lines: list[str]) -> str:
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        written = _row(path, columns, {**cells, "Change": _cell(item.change), CHECK: _cell(item.how())})
        lines[index] = written + "\n"
        return written

    return _edit(path, production, day, write, lambda _: f"edit {row}")


def mark_row(path: Path, row: str, now: datetime, production: Path) -> str:
    """Mark `row` important to the user, keeping its prerequisite; return the row written."""

    def write(lines: list[str]) -> str:
        _add_column(path, lines, SECTION, URGENCY)
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        _, after = _rank({"#": row, URGENCY: _plain(cells.get(URGENCY, ""))})
        written = _row(path, columns, {**cells, URGENCY: _cell(urgency.cell(urgency.mark(now), after))})
        lines[index] = written + "\n"
        return written

    return _edit(path, production, now.date(), write, lambda _: f"mark {row} important")


def record_verdict(path: Path, row: str, verdict: str, day: date, production: Path) -> str:
    """Move `row` from To review to the top of the Reviewed table; return its Merged cell."""

    def write(lines: list[str]) -> str:
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines.pop(index))))
        reviewed, top, _ = _table(path, lines, REVIEWED)
        _insert(lines, top, [_row(path, reviewed, {"#": row, "Change": cells.get("Change", ""),
                                                   "Verdict": _cell(verdict), "Date": day.isoformat()})])
        return _plain(cells.get("Merged", ""))

    return _edit(path, production, day, write, lambda _: f"verdict {row}")


def production_doc(production: str) -> Path:
    """The production doc given; without one, the calling showrunner's."""
    if production:
        return Path(production).expanduser()
    import showrunners  # only the actions that name a production read the update timers

    found = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if found is None:
        raise ChangeReviewError("this session is no production's showrunner; pass --production <doc>")
    return found


def showrunner_of(doc: Path) -> str:
    """The name of the top-level showrunner whose list takes the production's rows.

    A child production's rows go to its root's list. The name is the root showrunner's session
    name as it is now, never the production slug, so it is read only while that showrunner runs.
    """
    import showrunners

    try:
        root = showrunners.root_of(showrunners.canonical_slug(showrunners.production_slug(str(doc))))
    except ValueError as error:
        raise ChangeReviewError(str(error)) from None
    name = showrunners.current_name(root)
    if not name:
        raise ChangeReviewError(f"the showrunner of {root} is not running; the list is named for it")
    return name


def production_list(production: str) -> Path:
    """The list the production doc names; without a doc, the calling showrunner's production."""
    doc = production_doc(production)
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
    see: str = ""
    row: str = ""
    verdict: str = ""
    production: str = ""
    urgency: str = ""
    due: str = ""
    after: str = ""


def _list(options: CommandLine) -> Path:
    """The list --list names, else the production's."""
    return Path(options.list).expanduser() if options.list else production_list(options.production)


def _visible(options: CommandLine) -> Visible:
    """The change, how to try it and what the user sees, from --change, --check and --see."""
    return Visible(_words("change", options.change), _words("check", options.check), _words("see", options.see))


def _row_option(options: CommandLine) -> str:
    """The row id --row names, such as C3."""
    return _row_id("row", options.row)


def _row_id(name: str, value: str) -> str:
    row = value.strip()
    if _ROW.match(row) is None:
        raise ChangeReviewError(f"--{name} {row!r} is not a row id such as C3")
    return row


def _rank_option(options: CommandLine) -> str:
    """The Urgency cell --urgency, --due and --after give; empty for a normal row."""
    try:
        level = urgency.read(options.urgency, options.due)
    except urgency.UrgencyError as error:
        raise ChangeReviewError(str(error)) from None
    if level.level == urgency.USER:
        level = urgency.mark(datetime.now(user_zone()).replace(tzinfo=None))
    return urgency.cell(level, _row_id("after", options.after) if options.after.strip() else "")


def _run(options: CommandLine) -> int:
    match options.action:
        case "add":
            item, rank = _visible(options), _rank_option(options)
            rows = append_rows(_list(options), [item], today(), options.merged.strip(),
                               production_doc(options.production), rank)
            print(row_id(rows[0]))
        case "edit":
            row, item = _row_option(options), _visible(options)
            print(edit_row(_list(options), row, item, today(), production_doc(options.production)))
        case "show":
            # One row only: the user asks for the next one when they want it (user, 2026-10-20).
            path = _list(options)
            waiting = pending(path)
            shown = urgency.top(row_entries(waiting), queue(path))
            print(describe(shown.item) + (f"\n\n{len(waiting) - 1} more to review" if len(waiting) > 1 else "")
                  if shown is not None else "nothing to review")
        case "mark-important":
            row = _row_option(options)
            now = datetime.now(user_zone()).replace(tzinfo=None)
            print(mark_row(_list(options), row, now, production_doc(options.production)))
        case "verdict":
            row = _row_option(options)
            verdict = _words("verdict", options.verdict)
            merged = record_verdict(_list(options), row, verdict, today(), production_doc(options.production))
            print(f"{row} landed" if lands(verdict) else f"{row} returns to its unit (merged {merged})")
        case "list-path":
            print(production_list(options.production))
        case "showrunner":
            print(showrunner_of(production_doc(options.production)))
        case "drift":
            found = drift(Path(options.repo).resolve(), options.merge_branch, read(_list(options)), options.since)
            print("\n".join(found) if found else "no drift")
            return 1 if found else 0
        case _:
            commits = unit_commits(Path(options.repo).resolve(), options.checkpoint, options.previous,
                                   options.merge_branch)
            if options.action == "check":
                refused = missing(commits)
                print("\n".join(refused) if refused else f"{len(commits)} commit(s) carry their {TRAILER} lines")
                return 1 if refused else 0
            rows = append_rows(_list(options), visible(commits), today(), options.merged,
                               production_doc(options.production))
            print("\n".join(rows) if rows else "no visible change to add")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("action", choices=("check", "append", "drift", "add", "edit", "show", "verdict",
                                                   "mark-important", "list-path", "showrunner"))
    _ = parser.add_argument("--repo", default=".")
    for name in ("checkpoint", "previous", "merge-branch", "list", "merged", "since", "change", "check", "see",
                 "row", "verdict", "production", "urgency", "due", "after"):
        _ = parser.add_argument(f"--{name}", default="")
    try:
        return _run(cast(CommandLine, parser.parse_args(argv)))
    except (ChangeReviewError, OSError) as error:
        print(f"change review: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
