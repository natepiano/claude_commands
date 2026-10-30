#!/usr/bin/env python3
"""The change-review list: the trailer every checkpoint commit carries, and the rows a merge adds.

    change_review.py check --repo R --checkpoint C --previous P --merge-branch B
    change_review.py append --repo R [--production DOC] [--list L] --merged M --checkpoint C --previous P --merge-branch B
    change_review.py drift --repo R --merge-branch B [--production DOC] [--list L] --since YYYY-MM-DD
    change_review.py add [--production DOC] [--list L] --change "<what changed for you>" --check "<how to try it>" --see "<what you see>" [--merged M] [--urgency user|blocks|soon|normal|later] [--due ISO] [--after C<n>] [--hold <folder> ...]
    change_review.py edit [--production DOC] [--list L] --row ID --change "<what changed for you>" --check "<how to try it>" --see "<what you see>"
    change_review.py show [--production DOC] [--list L]
    change_review.py verdict [--production DOC] [--list L] --row ID --verdict "<the user's words>"
    change_review.py mark-important [--production DOC] [--list L] --row ID
    change_review.py list-path [--production DOC]
    change_review.py showrunner [--production DOC]
    change_review.py backfill-production [--list L] [--write]

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
date, and removes the row's scratch folders and held folders (task `row:<showrunner>-C<n>`,
scripts/lib/scratch.py) whichever way the verdict goes; `add --hold <folder>` holds a folder the user
checks the row in, where it is, until that verdict; `mark-important` marks a row the user called important.

Each row's Production cell names the production whose change it is: the production whose Units
table names the branch of the checkpoint that carries the row's line (`visible_owners`), else the
merging one. `backfill-production` fills the empty cells of older rows under To review, in the list
--list names or in every list, from the row's merge hash: the checkpoint that names the row's
change, else the one production of every checkpoint the merge brought in. It prints each cell it
fills and each row it cannot trace, with why, and writes only with --write.

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
For edit, verdict and mark-important, --row takes the ID /next printed: C434 selects the current
list; production/C9 selects that production's named list. An explicit --list must match that name.
With a production, show reads its current and earlier lists and keeps the same pin as /next.
Rows in other lists carry their list name; current-list rows keep their plain IDs. Without a
production, show --list reads every row in that explicit list.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import atomic_file  # noqa: E402
import scratch  # noqa: E402
import urgency  # noqa: E402
from user_zone import user_zone  # noqa: E402

# The merge gate imports this module as scripts.production.change_review; its siblings still resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import issues  # noqa: E402
from production_units import Refusal, cell_value, production_field, unit_table  # noqa: E402
from proposals_folder import owner_docs, scope_docs, slug_of  # noqa: E402

TRAILER = "Change-Review"
LIST_HEADER = "- **Change review:**"
SECTION = "## To review"
REVIEWED = "## Reviewed"
CHECK = "How to check"
URGENCY = "Urgency"
# The production a row's change is from: its slug. A row from before the column is the root's.
PRODUCTION = "Production"
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
_LANDS = re.compile(r"\b(works|fine)\b", re.IGNORECASE)
_FAILS = re.compile(r"\b(not|no|never|still|fails?|failed|broken|wrong|doesn't|don't|isn't)\b", re.IGNORECASE)
# A cell boundary is a `|` that _cell did not escape.
_BOUNDARY = re.compile(r"(?<!\\)\|")
_CHECKPOINT = re.compile(r"^checkpoint\((?P<branch>[^)]+)\):")
_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")
# The columns an older row keeps its merge hash in, in the order they are read.
HASH_COLUMNS = ("Merged", "On the Mac build")
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


def registered_productions() -> Iterable[Path]:
    """Production docs named by the registered update timers."""
    import showrunners

    return showrunners.timer_docs().values()


def common_git_directory(repo: Path) -> Path:
    """The repository directory shared by all its worktrees."""
    directory = Path(_git(repo, "rev-parse", "--git-common-dir").strip())
    return (repo / directory).resolve()


def reviewed_merge_refs(repo: Path, productions: Callable[[], Iterable[Path]]) -> list[str]:
    """Origin branches whose readable productions in this repository already write review rows."""
    try:
        common = common_git_directory(repo)
        available = set(_git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").splitlines())
        docs = tuple(productions())
    except (OSError, UnicodeError, ValueError, Refusal, ChangeReviewError):
        return []
    refs: list[str] = []
    for doc in docs:
        try:
            lines = doc.read_text(encoding="utf-8").splitlines()
            if list_path(doc) is None:
                continue
            where = Path(production_field(lines, "Repository")).expanduser()
            if common_git_directory(where) != common:
                continue
            branch = production_field(lines, "Merge branch")
            if f"refs/remotes/origin/{branch}" in available:
                refs.append(f"origin/{branch}")
        except (OSError, UnicodeError, ValueError, Refusal, ChangeReviewError):
            continue
    return list(dict.fromkeys(refs))


def unit_commits(repo: Path, checkpoint: str, previous: str, merge_branch: str,
                 checked_from: int = CHECKED_FROM, *,
                 productions: Callable[[], Iterable[Path]] = registered_productions) -> list[Commit]:
    """The unit's commits, excluding origin merge branches that already write review rows."""
    excluded = list(dict.fromkeys([f"origin/{merge_branch}", *reviewed_merge_refs(repo, productions)]))
    records = _git(repo, "log", "--no-merges", "--format=%H %ct", checkpoint, f"^{previous}",
                   *(f"^{ref}" for ref in excluded), "--").splitlines()
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
        if lines == text.splitlines(keepends=True):
            _commit(path, what(result))
            return result
        _sort(path, lines)
        _stamp(path, lines, production, day)
        atomic_file.replace_text(path, "".join(lines))
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
    if not git("status", "--porcelain", "--", path.name).stdout.strip():
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
                rank: str = "", owners: Sequence[Path] = ()) -> list[str]:
    """Add one row per change to the To review table; return the rows written.

    `merged` is the merge's hash, or empty for a change no merge names. The Mac build column is
    written only when the table has one. `production` is the doc whose change the rows are.
    `rank` is the rows' Urgency cell; empty is a normal row. The Production column takes the
    production's slug, so /next in a showrunner below the root shows only its own rows. `owners`,
    when given, holds each change's own production doc (`visible_owners`); else every row is
    `production`'s.
    """
    if not changes:
        return []

    def write(lines: list[str]) -> list[str]:
        if rank:
            _add_column(path, lines, SECTION, URGENCY)
        _add_column(path, lines, SECTION, PRODUCTION)
        columns, first, end = _table(path, lines, SECTION)
        if merged and (held := _held(columns, lines[first:end], changes, merged)):
            raise ChangeReviewError(f"{held} already holds that change from merge {merged}; no row added")
        number = next_number("".join(lines))
        when = f"{day.isoformat()} · {_cell(merged)}" if merged else day.isoformat()
        rows = [_row(path, columns, {"#": f"C{number + offset}", "Change": _cell(item.change), "Merged": when,
                                     "On the Mac build": NOT_YET, CHECK: _cell(item.how()), URGENCY: _cell(rank),
                                     PRODUCTION: _cell(row_production(owners[offset] if owners else production))})
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


def _held(columns: list[str], rows: list[str], changes: list[Visible], merged: str) -> str:
    """The To review row that already holds one of `changes` from the merge `merged`, or empty. A short
    hash and a longer one of the same merge match: on 2026-10-22 rows written with 8-character hashes
    repeated rows written with 7 (C94-C99 of C74-C79)."""
    wanted = {item.change for item in changes}
    for line in rows:
        row = dict(zip(columns, (_plain(cell) for cell in _cells(line))))
        found = _HASH.search(row.get("Merged", ""))
        if (found is not None and row.get("Change") in wanted
                and (found[0].startswith(merged) or merged.startswith(found[0]))):
            return row["#"]
    return ""


def pending(path: Path) -> list[dict[str, str]]:
    """Each row still to review, as its cells by column name. Reads only."""
    lines = read(path).splitlines(keepends=True)
    columns, first, end = _table(path, lines, SECTION)
    return [dict(zip(columns, (_plain(cell) for cell in _cells(line)))) for line in lines[first:end]]


def scope_list(doc: Path) -> Path:
    """The list that takes the production's rows: the one its Change review line names, else its root's."""
    import showrunners

    slug = slug_of(doc)
    root = showrunners.root_of(slug)
    try:
        return production_list(str(doc))
    except ChangeReviewError:
        if slug == root:
            raise
        return production_list(str(showrunners.timer_docs().get(root, doc)))


@dataclass(frozen=True)
class ProductionReviewList:
    path: Path
    default_production: str


@dataclass(frozen=True)
class ScopedReviewEntry:
    review_list: ProductionReviewList
    entry: urgency.Entry[dict[str, str]]


def scope_lists(path: Path, doc: Path, below: bool = False) -> tuple[ProductionReviewList, ...]:
    """The current list and each scoped production's own earlier list, once per path."""
    import showrunners

    lists = {path.absolute(): ProductionReviewList(path, showrunners.root_of(slug_of(doc)))}
    for production in scope_docs(doc, below):
        slug = slug_of(production)
        earlier = list_file(slug)
        if earlier.absolute() in lists or earlier.exists():
            lists[earlier.absolute()] = ProductionReviewList(earlier, slug)
    return tuple(lists.values())


def scoped_review_entries(path: Path, doc: Path, below: bool = False) -> list[ScopedReviewEntry]:
    """Read and rank each list independently; a bad list leaves the others in the queue."""
    scope = {slug_of(each) for each in scope_docs(doc, below)}
    entries: list[ScopedReviewEntry] = []
    for review_list in scope_lists(path, doc, below):
        try:
            rows = [row for row in pending(review_list.path)
                    if (row.get(PRODUCTION) or review_list.default_production) in scope]
            ranked = row_entries(rows)
        except (ChangeReviewError, OSError, UnicodeError) as error:
            print(f"change review: {review_list.path}: {error}", file=sys.stderr)
            continue
        entries.extend(ScopedReviewEntry(review_list, entry) for entry in ranked)
    return entries


def scope_rows(path: Path, doc: Path, below: bool = False) -> list[dict[str, str]]:
    """The production's open rows from its current list and its own earlier list.

    With `below`, include each lower production's rows and earlier list too.
    """
    return [row.entry.item for row in scoped_review_entries(path, doc, below)]


def scope_entries(path: Path, doc: Path, below: bool = False) -> list[urgency.Entry[dict[str, str]]]:
    """Keep current-list IDs and pins; name other rows and their prerequisites with their list."""
    entries: list[urgency.Entry[dict[str, str]]] = []
    for scoped in scoped_review_entries(path, doc, below):
        entry = scoped.entry
        prefix = (f"{_owner(scoped.review_list.path)}/"
                  if scoped.review_list.path.absolute() != path.absolute() else "")
        key = prefix + entry.key
        after = prefix + entry.after if entry.after and "/" not in entry.after else entry.after
        entries.append(replace(entry, key=key, item=entry.item | {"#": key}, after=after))
    return entries


class WholeReviewList(Enum):
    """An explicit list shown without a production filter."""

    ALL_ROWS = "all rows"


@dataclass(frozen=True)
class ChangeQueue:
    """The change rows a person can review and the name that keeps their pin and moved rows."""

    name: str
    entries: list[urgency.Entry[dict[str, str]]]


def change_queue(path: Path, scope: Path | WholeReviewList, below: bool = False) -> ChangeQueue:
    """The same change rows and pin for /next and change_review show."""
    if isinstance(scope, WholeReviewList):
        return ChangeQueue(queue(path), row_entries(pending(path)))
    import showrunners

    slug = slug_of(scope)
    name = queue(path) if slug == showrunners.root_of(slug) else f"{queue(path)}-{slug}"
    return ChangeQueue(urgency.scope_queue(name, below), scope_entries(path, scope, below))


def row_production(production: Path) -> str:
    """The Production cell for a change of the production doc `production`: the slug of the production
    it is credited to (`credited`)."""
    return slug_of(credited(production))


def credited(production: Path) -> Path:
    """The production a change of `production` belongs to. A style production's work lands on its parent's
    merge branch through a hand-off, so its changes are its parent's, and its showrunner does not show them
    (user, 2026-10-27). Any other production is its own."""
    seen = {production}
    while True:
        try:
            lines = production.read_text(encoding="utf-8").splitlines()
            if production_field(lines, "Kind") != "style":
                return production
            parent = Path(production_field(lines, "Parent production")).expanduser()
        except (OSError, UnicodeError, Refusal):
            return production
        if parent in seen:
            return production
        seen.add(parent)
        production = parent


def unit_branches(docs: Iterable[Path]) -> dict[str, Path]:
    """Each name a checkpoint can carry for a unit of the docs, with the doc whose Units table lists it.

    A checkpoint is named for the unit's branch or its plan, so both count, and so do the unit's name
    and its worktree's folder name, which a merge subject can carry.
    """
    found: dict[str, Path] = {}
    for doc in docs:
        try:
            rows = unit_table(doc.read_text(encoding="utf-8").splitlines())
        except (Refusal, OSError):
            continue
        for row in rows:
            names = (cell_value(row.get("Branch", "")), Path(cell_value(row.get("Plan", ""))).stem,
                     cell_value(row.get("Unit", "")), Path(cell_value(row.get("Worktree", ""))).name)
            for name in names:
                if name:
                    _ = found.setdefault(name, doc)
    return found


def _owner_of(commit: Commit, branches: dict[str, Path]) -> Path | None:
    """The production of the unit whose branch the checkpoint names, or None for another commit."""
    found = _CHECKPOINT.match(commit.subject)
    return branches.get(found["branch"]) if found else None


def visible_owners(commits: list[Commit], production: Path) -> list[Path]:
    """The production doc of each change `visible` gives, in its order: the production whose Units table
    names the checkpoint's branch, at `production` or below it, else `production` itself.

    A root's gate merges its children's merge branches too, and their rows belong to the child.
    """
    branches = unit_branches(owner_docs(production))
    return [_owner_of(commit, branches) or production for commit in reversed(commits)
            if commit.subject.startswith("checkpoint(")
            for item in entries(commit.message) if isinstance(item, Visible)]


def merge_commits(repo: Path, merge: str) -> list[Commit]:
    """The commits a merge brought in, without merges; none for a commit that is no merge."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", merge).split()[1:]
    if len(parents) < 2:
        return []
    records = _git(repo, "log", "--no-merges", "--format=%x1e%H%x1f%B", parents[1], f"^{parents[0]}", "--")
    commits: list[Commit] = []
    for record in records.split("\x1e")[1:]:
        sha, message = record.split("\x1f", 1)
        commits.append(Commit(sha, message.split("\n", 1)[0], message, ()))
    return commits


def _found_repo(repos: Iterable[Path], sha: str) -> Path | None:
    """The first repository that holds the commit."""
    return next((repo for repo in repos if subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
        capture_output=True, check=False).returncode == 0), None)


class Tree:
    """The productions one list belongs to: the units they list, their repositories and merge branches."""

    def __init__(self, docs: Iterable[Path]) -> None:
        self.docs: list[Path] = list(dict.fromkeys(docs))
        self.branches: dict[str, Path] = unit_branches(self.docs)
        self.repos: list[Path] = []
        self.targets: dict[tuple[Path, str], Path] = {}
        self._chains: dict[tuple[Path, str], set[str]] = {}
        for doc in self.docs:
            try:
                lines = doc.read_text(encoding="utf-8").splitlines()
                repo = Path(production_field(lines, "Repository")).expanduser()
            except (Refusal, OSError):
                continue
            if repo not in self.repos:
                self.repos.append(repo)
            try:
                _ = self.targets.setdefault((repo, production_field(lines, "Merge branch")), doc)
            except Refusal:
                pass

    def landed_on(self, repo: Path, sha: str) -> Path | None:
        """The production whose merge branch has the commit on its first-parent line: where it merged.

        A child merges its parent's branch in and the parent merges the child's, so plain ancestry
        names both; the first-parent line names only the branch the merge was made on.
        """
        full = _git(repo, "rev-parse", f"{sha}^{{commit}}").strip()
        for (where, branch), doc in self.targets.items():
            if where != repo:
                continue
            if (where, branch) not in self._chains:
                try:
                    self._chains[(where, branch)] = set(_git(repo, "rev-list", "--first-parent", branch, "--").split())
                except ChangeReviewError:
                    self._chains[(where, branch)] = set()
            if full in self._chains[(where, branch)]:
                return doc
        return None


def trace(row: dict[str, str], tree: Tree) -> Path | str:
    """The production of an older row, from its merge. In order: the checkpoint that names the row's
    change; else the one production of every checkpoint the merge brought in; else the production
    whose merge branch the merge landed on. A string says why it cannot be traced."""
    found = next((match.group(0) for column in HASH_COLUMNS
                  if (match := _HASH.search(row.get(column, ""))) is not None), None)
    if found is None:
        return "no merge hash"
    repo = _found_repo(tree.repos, found)
    if repo is None:
        return f"{found} is in no repository of this list's productions"
    commits = merge_commits(repo, found)
    change = row.get("Change", "")
    named = {owner for commit in commits if (owner := _owner_of(commit, tree.branches)) is not None
             and any(isinstance(item, Visible) and item.change == change for item in entries(commit.message))}
    if len(named) == 1:
        return named.pop()
    every = {owner for commit in commits if (owner := _owner_of(commit, tree.branches)) is not None}
    if len(every) == 1:
        return every.pop()
    if (target := tree.landed_on(repo, found)) is not None:
        return target
    if every:
        return f"{found} brings in checkpoints of {len(every)} productions"
    return f"{found} brings in no checkpoint of a listed unit and lands on no listed merge branch"


def list_tree(path: Path, docs: Iterable[Path]) -> Tree:
    """The productions whose root keeps the list `path`, with every production below each root."""
    import showrunners

    owned: list[Path] = []
    for doc in docs:
        slug = slug_of(doc)
        try:
            if showrunners.root_of(slug) != slug or list_path(doc) != path:
                continue
        except OSError:
            continue
        owned.extend(owner_docs(doc))
    return Tree(owned)


def backfill(path: Path, tree: Tree, write: bool) -> tuple[dict[str, str], dict[str, str]]:
    """Fill each empty Production cell under To review from the row's merge; return the cells filled, by
    row id, and the rows that cannot be traced, with why. Without `write` the list is only read.

    `tree` holds the productions the list belongs to (`list_tree`); a row traces only to one of them.
    """
    filled: dict[str, str] = {}
    untraced: dict[str, str] = {}
    owners: dict[str, Path] = {}
    for row in pending(path):
        if row.get(PRODUCTION, "").strip():
            continue
        found = trace(row, tree)
        if isinstance(found, Path):
            filled[row["#"]], owners[row["#"]] = row_production(found), found
        else:
            untraced[row["#"]] = found
    if not write or not filled:
        return filled, untraced

    def fill(lines: list[str]) -> None:
        _add_column(path, lines, SECTION, PRODUCTION)
        columns, first, end = _table(path, lines, SECTION)
        at = columns.index(PRODUCTION)
        for index in range(first, end):
            cells = _cells(lines[index])
            if cells[0] in filled and at < len(cells) and not cells[at]:
                cells[at] = _cell(filled[cells[0]])
                lines[index] = "| " + " | ".join(cells) + " |\n"

    _edit(path, next(iter(owners.values())), today(), fill, lambda _: f"production cells for {len(filled)} rows")
    return filled, untraced


def rename_production(path: Path, production: Path, old: str, new: str) -> list[str]:
    """Rewrite each Production cell under To review that names `old` to `new`; return the rows changed.

    A production rename keeps its rows on the list (`/showrunner:rename`). `production` is the renamed
    doc, which stamps the write. A list with no such cell is not written.
    """
    if not path.exists() or not any(row.get(PRODUCTION) == old for row in pending(path)):
        return []

    def rewrite(lines: list[str]) -> list[str]:
        columns, first, end = _table(path, lines, SECTION)
        at = columns.index(PRODUCTION)
        changed: list[str] = []
        for index in range(first, end):
            cells = _cells(lines[index])
            if at < len(cells) and _plain(cells[at]) == old:
                cells[at] = _cell(new)
                lines[index] = "| " + " | ".join(cells) + " |\n"
                changed.append(cells[0])
        return changed

    return _edit(path, production, today(), rewrite, lambda rows: f"production {old} renamed {new} in {len(rows)} rows")


def describe(row: dict[str, str]) -> str:
    """One row in plain lines: its id and change, how to check it, then each other column that has a value."""
    rest = [f"  {column}: {value}" for column, value in row.items() if column not in ("#", "Change", CHECK) and value]
    return "\n".join([f"{row.get('#', '')} {row.get('Change', '')}", f"  {CHECK}: {row.get(CHECK, '')}", *rest])


def lands(verdict: str) -> bool:
    """A verdict that says `works` or `fine` and no word of failure (`not`, `still`, `broken` ...) lands
    the row ("okay c17 works"); any other returns it to its unit."""
    return _LANDS.search(verdict) is not None and _FAILS.search(verdict) is None


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


@dataclass(frozen=True)
class RecordedMerge:
    text: str


@dataclass(frozen=True)
class MergeNotRecorded:
    pass


@dataclass(frozen=True)
class VerdictRecorded:
    merged: RecordedMerge | MergeNotRecorded


@dataclass(frozen=True)
class VerdictAlreadyRecorded:
    verdict: str
    day: str
    merged: RecordedMerge | MergeNotRecorded


@dataclass(frozen=True)
class VerdictConflict:
    recorded: str
    day: str


def row_merge(cells: dict[str, str]) -> RecordedMerge | MergeNotRecorded:
    text = _plain(cells.get("Merged", ""))
    return RecordedMerge(text) if text else MergeNotRecorded()


def record_verdict(path: Path, row: str, verdict: str, day: date,
                   production: Path) -> VerdictRecorded | VerdictAlreadyRecorded | VerdictConflict:
    """Record a verdict once, retaining its merge and the outcome of an earlier write."""

    def write(lines: list[str]) -> VerdictRecorded | VerdictAlreadyRecorded | VerdictConflict:
        columns, first, end = _table(path, lines, SECTION)
        index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
        if index is None:
            reviewed, first, end = _table(path, lines, REVIEWED)
            index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
            if index is None:
                raise ChangeReviewError(f"{row} is not on the list to review")
            cells = dict(zip(reviewed, _cells(lines[index])))
            if cells.get("Verdict", "") == _cell(verdict):
                return VerdictAlreadyRecorded(verdict, _plain(cells.get("Date", "")), row_merge(cells))
            return VerdictConflict(_plain(cells.get("Verdict", "")), _plain(cells.get("Date", "")))
        cells = dict(zip(columns, _cells(lines.pop(index))))
        _add_column(path, lines, REVIEWED, "Merged")
        reviewed, top, _ = _table(path, lines, REVIEWED)
        _insert(lines, top, [_row(path, reviewed, {"#": row, "Change": cells.get("Change", ""),
                                                   "Verdict": _cell(verdict), "Date": day.isoformat(),
                                                   "Merged": cells.get("Merged", "")})])
        return VerdictRecorded(row_merge(cells))

    return _edit(path, production, day, write, lambda _: f"verdict {row}")


def end_row_scratch(path: Path, row: str) -> list[str]:
    """Remove the scratch folders of `row` on the list at `path`; return one line per folder.

    A verdict ends the row either way: a row returned to its unit comes back as a new row.
    """
    try:
        return [f"removed {folder}" for folder in scratch.end(f"row:{_owner(path)}-{row}")]
    except (scratch.ScratchError, OSError) as error:
        return [f"scratch not removed: {error}"]


def hold_for_row(path: Path, row: str, folders: list[str]) -> list[str]:
    """Hold each folder the user checks `row` in until the row's verdict; return one line per folder."""
    lines: list[str] = []
    for folder in folders:
        try:
            task = scratch.hold(Path(folder).expanduser(), f"row:{_owner(path)}-{row}", f"change review {row}")
        except (scratch.ScratchError, OSError) as error:
            lines.append(f"not held: {error}")
            continue
        lines.append(f"held {folder} for {task}")
    return lines


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


def drift(repo: Path, merge_branch: str, text: str, since: str, foreign: Collection[str] = ()) -> list[str]:
    """Each merge on the merge branch since `since` whose visible changes have no row: none with its hash
    and none, under To review or Reviewed, with one of its changes. A checkpoint named in `foreign`
    (`foreign_units`) is another list's: a merge of main brings such changes in, and they are left out."""
    lines: list[str] = []
    changes = _changes(text)
    for record in _git(repo, "log", "--first-parent", "--merges", f"--since={since}",
                       "--format=%H %P", f"origin/{merge_branch}", "--").splitlines():
        merge, *parents = record.split()
        if len(parents) < 2:
            continue
        commits = [commit for sha in
                   _git(repo, "log", "--no-merges", "--format=%H", parents[1], f"^{parents[0]}", "--").split()
                   if not _named_in(commit := Commit(sha, *_described(repo, sha)), foreign)]
        short = _git(repo, "rev-parse", f"--short={SHORT}", merge).strip()
        if visible(commits) and f"· {short}" not in text and not any(item.change in changes for item in visible(commits)):
            lines.append(f"{short}: {len(visible(commits))} visible change(s) and no row in the list")
    return lines


def _named_in(commit: Commit, names: Collection[str]) -> bool:
    """Whether the commit is a checkpoint named for one of `names`."""
    found = _CHECKPOINT.match(commit.subject)
    return found is not None and found["branch"] in names


def foreign_units(production: Path) -> set[str]:
    """The checkpoint names of the units of every production outside the tree of `production`'s root.

    Empty when the productions cannot be read: drift then counts every checkpoint.
    """
    import showrunners

    try:
        docs = list(showrunners.timer_docs().values())
        root = showrunners.root_of(slug_of(production))
        top = next((doc for doc in docs if slug_of(doc) == root), production)
        return set(unit_branches(docs)) - set(unit_branches(owner_docs(top)))
    except (OSError, ValueError, Refusal):
        return set()


def _changes(text: str) -> set[str]:
    """The Change cell of every row in the list, under To review and under Reviewed alike.

    A reviewed row keeps no Merged cell, so drift knows a settled merge by its rows' changes.
    """
    found: set[str] = set()
    for line in text.splitlines():
        cells = _cells(line) if _NUMBER.match(line) else []
        if len(cells) > 1:
            found.add(_plain(cells[1]))
    return found


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
    write: bool = False
    hold: list[str] | None = None


def _list(options: CommandLine) -> Path:
    """The list --list names, else the production's."""
    return Path(options.list).expanduser() if options.list else production_list(options.production)


def _visible(options: CommandLine) -> Visible:
    """The change, how to try it and what the user sees, from --change, --check and --see."""
    return Visible(_words("change", options.change), _words("check", options.check), _words("see", options.see))


@dataclass(frozen=True)
class ReviewRowTarget:
    path: Path
    row: str


def _row_option(options: CommandLine) -> ReviewRowTarget:
    """The list and local row ID selected by --row, as /next printed it."""
    name, separator, row = options.row.strip().rpartition("/")
    row = _row_id("row", row)
    if not separator:
        return ReviewRowTarget(_list(options), row)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None:
        raise ChangeReviewError(f"--row {options.row!r} is not a row id such as C3 or production/C3")
    path = list_file(name)
    if options.list and Path(options.list).expanduser().resolve() != path.resolve():
        raise ChangeReviewError(f"--row {options.row} names {path}, which differs from --list {options.list}")
    return ReviewRowTarget(path, row)


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
            path = _list(options)
            rows = append_rows(path, [item], today(), options.merged.strip(), production_doc(options.production), rank)
            row = row_id(rows[0])
            print(row)
            for line in hold_for_row(path, row, options.hold or []):
                print(line)
        case "edit":
            target, item = _row_option(options), _visible(options)
            print(edit_row(target.path, target.row, item, today(), production_doc(options.production)))
        case "show":
            # One row only: the user asks for the next one when they want it (user, 2026-10-20).
            if options.list and not options.production:
                waiting = change_queue(_list(options), WholeReviewList.ALL_ROWS)
            else:
                doc = production_doc(options.production)
                path = Path(options.list).expanduser() if options.list else scope_list(doc)
                waiting = change_queue(path, doc)
            shown = urgency.top(waiting.entries, waiting.name)
            print(describe(shown.item) + (f"\n\n{len(waiting.entries) - 1} more to review" if len(waiting.entries) > 1 else "")
                  if shown is not None else "nothing to review")
        case "mark-important":
            target = _row_option(options)
            now = datetime.now(user_zone()).replace(tzinfo=None)
            print(mark_row(target.path, target.row, now, production_doc(options.production)))
        case "verdict":
            target = _row_option(options)
            verdict = _words("verdict", options.verdict)
            result = record_verdict(target.path, target.row, verdict, today(), production_doc(options.production))
            row = options.row.strip()
            if isinstance(result, VerdictConflict):
                print(f'change review: {row} has the verdict "{result.recorded}" from {result.day}; '
                      + 'this verdict is not recorded', file=sys.stderr)
            else:
                if isinstance(result, VerdictAlreadyRecorded):
                    print(f"{row} already has this verdict ({result.day}); the list is unchanged")
                merged = result.merged.text if isinstance(result.merged, RecordedMerge) else "not recorded"
                print(f"{row} landed" if lands(verdict) else f"{row} returns to its unit (merged {merged})")
            for line in end_row_scratch(target.path, target.row):
                print(line)
            if isinstance(result, VerdictConflict):
                return 1
        case "list-path":
            print(production_list(options.production))
        case "showrunner":
            print(showrunner_of(production_doc(options.production)))
        case "backfill-production":
            import showrunners

            docs = list(showrunners.timer_docs().values())
            lists = [Path(options.list).expanduser()] if options.list else sorted(LISTS.glob(f"{PREFIX}*.md"))
            for path in lists:
                filled, untraced = backfill(path, list_tree(path, docs), options.write)
                print(f"{path.name}: {len(filled)} {'filled' if options.write else 'to fill'}, "
                      + f"{len(untraced)} untraced")
                for row, slug in filled.items():
                    print(f"  {row}: {slug}")
                for row, why in untraced.items():
                    print(f"  {row}: untraced, {why}")
        case "drift":
            try:
                foreign = foreign_units(production_doc(options.production))
            except ChangeReviewError:
                foreign = set[str]()
            found = drift(Path(options.repo).resolve(), options.merge_branch, read(_list(options)), options.since,
                          foreign)
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
                                                   "mark-important", "list-path", "showrunner",
                                                   "backfill-production"))
    _ = parser.add_argument("--repo", default=".")
    for name in ("checkpoint", "previous", "merge-branch", "list", "merged", "since", "change", "check", "see",
                 "row", "verdict", "production", "urgency", "due", "after"):
        _ = parser.add_argument(f"--{name}", default="")
    _ = parser.add_argument("--write", action="store_true")
    _ = parser.add_argument("--hold", action="append")
    try:
        return _run(cast(CommandLine, parser.parse_args(argv)))
    except (ChangeReviewError, OSError) as error:
        print(f"change review: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
