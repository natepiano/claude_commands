#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Read checkpoint trailers and write change review rows.

A visible trailer has three fields and an optional fourth field: after <step>.
An invisible change uses Change-Review: none — <reason>.
See commands/change_review.md for actions, list selection, urgency, verdicts, and scratch folders.

An open Production cell holds its unit's fixed staff id or its showrunner's fixed staff id.
Readers resolve current names and the unit's current parent.
Reviewed rows retain the production name recorded at the verdict.
Unknown owner names remain unresolved.

Run backfill-production [--list PATH] [--write] to resolve empty cells from recorded merge history.
List writes hold the directory lock. Normal writes update metadata and commit only the list.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar, cast

from scripts.lib.atomic_file import write_text_atomic  # noqa: E402
from scripts.lib import scratch  # noqa: E402
from scripts.lib import urgency  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402

from scripts.production import issues  # noqa: E402
from scripts.production.proposals_folder import owner_docs, scope_docs  # noqa: E402
from scripts.production import change_routing, session_staff, showrunner_tree, staff, unit_config, unit_lookup
from scripts.production.production_doc import Refusal, production_field
from scripts.shutdown import remote

TRAILER = "Change-Review"
COVERS = f"{TRAILER}-Covers"
HELD_NEXT_STEP = (f"commit a checkpoint (a plain commit, not a merge: a merge may carry only `{TRAILER}: none` lines) "
                  + f"whose {TRAILER} lines describe the change and that names each "
                  + f"listed commit in a `{COVERS}: <hash>` line, push it, and send its hash")
LIST_HEADER = "- **Change review:**"
SECTION = "## To review"
REVIEWED = "## Reviewed"
CHECK = "How to check"
URGENCY = "Urgency"
# Open rows hold the owner's fixed staff id. Reviewed rows keep their recorded production.
PRODUCTION = "Production"
PRODUCER = change_routing.PRODUCER
KIND = "Kind"
REVIEW_KINDS = ("See in action", "Read only")
NOT_YET = "not yet"
FORM = "<what changed for you> | <how to try it: where and what to type> | <what you see>"
SEPARATOR = " | "
SHORT = 8
# Commits made before the rule landed carry no line and are not read: 2026-10-15 00:00 EDT.
CHECKED_FROM = 1792036800
LANDED = "2026-10-15"
# Scan only merges made after the readiness rule starts.
READY_FROM = "2026-11-06"
_CURRENT_REPO = Path(".")
_NONE = re.compile(r"^none\s*[—–-]+\s*(?P<why>\S.*)$")
_LINE = re.compile(rf"^{TRAILER}:\s*(?P<value>.*)$", re.MULTILINE)
_COVERS = re.compile(rf"^{COVERS}:\s*(?P<sha>[0-9a-f]{{7,40}})\s*$", re.MULTILINE)
_NUMBER = re.compile(r"^\| C(\d+) \|", re.MULTILINE)
_ROW = re.compile(r"^[A-Z]\d+$")
_LANDS = re.compile(r"^\s*(?:(?:okay|ok)\s+)?(?:c\d+\s+)?(?:works|fine)\b", re.IGNORECASE)
# A cell boundary is a `|` that _cell did not escape.
_BOUNDARY = re.compile(r"(?<!\\)\|")
_BREAK_TAG = re.compile(r"<(\\*)br>")
_CHECKPOINT = re.compile(r"^checkpoint\((?P<branch>[^)]+)\):")
_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")
# The columns an older row keeps its merge hash in, in the order they are read.
HASH_COLUMNS = ("Merged", "On the Mac build")
lists_directory = change_routing.lists_directory
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
class ReadyNow:
    """The change needs no further live step."""


@dataclass(frozen=True)
class AfterStep:
    step: str


@dataclass(frozen=True)
class Visible:
    change: str
    check: str
    see: str
    readiness: ReadyNow | AfterStep = ReadyNow()

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
    if (len(parts) == 4 and all(parts) and parts[3].startswith("after ")
            and (step := parts[3].removeprefix("after ").strip())):
        return Visible(parts[0], parts[1], parts[2], AfterStep(step))
    return Malformed(text)


def entries(message: str) -> list[Entry]:
    return [parse(found.group("value")) for found in _LINE.finditer(message)]


def checkpoint_changes(message: str) -> tuple[str, ...]:
    """The visible change names in a checkpoint message."""
    return tuple(item.change for item in entries(message) if isinstance(item, Visible))


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
        return (f"{TRAILER} line {bad[0]!r} needs three parts split by {SEPARATOR.strip()!r}, optionally followed by ` | after <step>`: "
                + f"`{TRAILER}: {FORM}`, or `{TRAILER}: none — <why nothing visible changes>`")
    return ""


def merge_review_problem(message: str, *, is_merge: bool) -> str:
    """Merge messages allow only review lines that parse as Invisible."""
    if is_merge and (any(not isinstance(item, Invisible) for item in entries(message))
                     or re.search(rf"^{COVERS}:", message, re.MULTILINE)):
        return (f"Merge commits cannot carry visible {TRAILER} or {COVERS} lines. "
                + "Put the lines on a plain commit after the merge")
    return ""


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ChangeReviewError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout


def on_main(repo: Path, merged: str) -> bool:
    """Return whether origin/main contains the requested commit."""
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", merged, "origin/main"],
                          capture_output=True, check=False).returncode == 0


def _require_main(repo: Path, merged: str) -> None:
    if not on_main(repo, merged):
        raise ChangeReviewError(
            f"{merged} is not on origin/main; no row added. "
            + "A change goes on the list only when the user can check it now. "
            + "If it just landed, run git fetch and retry.")


def registered_productions() -> Iterable[Path]:
    """Production docs named by the registered update timers."""
    from scripts.production import showrunners

    return showrunners.timer_docs().values()


def common_git_directory(repo: Path) -> Path:
    """The repository directory shared by all its worktrees."""
    directory = Path(_git(repo, "rev-parse", "--git-common-dir").strip())
    return (repo / directory).resolve()


def reviewed_merge_refs(repo: Path, productions: Callable[[], Iterable[Path]], *,
                        record: staff.StaffSnapshot | None = None) -> list[str]:
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
            if list_path(doc, record=record) is None:
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
    records = _git(repo, "log", "-z", "--format=%H %ct %P%n%B", checkpoint, f"^{previous}",
                   *(f"^{ref}" for ref in excluded), "--").split("\0")
    commits: list[Commit] = []
    for record in filter(str.strip, records):
        header, message = record.split("\n", 1)
        sha, stamp, *parents = header.split()
        if int(stamp) < checked_from:
            continue
        if len(parents) < 2:
            commits.append(Commit(sha, message.partition("\n")[0], message, _files(repo, sha)))
        elif reason := merge_review_problem(message, is_merge=True):
            raise ChangeReviewError(f"{sha[:SHORT]}: {reason}. Make the merge again without the lines")
    return commits


def missing(commits: list[Commit]) -> list[str]:
    """Each checkpoint without a good line that no passing commit names in a Change-Review-Covers line.

    A pushed commit cannot be amended, so a later checkpoint carries the lines for one that missed them.
    """
    covered = [found.group("sha") for commit in commits if entries(commit.message)
               and not problem(commit.subject, commit.message, commit.files) for found in _COVERS.finditer(commit.message)]
    return [f"{commit.sha[:SHORT]} {commit.subject}: {reason}" for commit in commits
            if (reason := problem(commit.subject, commit.message, commit.files))
            and not any(commit.sha.startswith(sha) for sha in covered)]


def _checkpoint_entries(commits: list[Commit]) -> Iterable[tuple[Commit, Visible]]:
    for commit in reversed(commits):
        if commit.subject.startswith("checkpoint("):
            for item in entries(commit.message):
                if isinstance(item, Visible):
                    yield commit, item


def visible(commits: list[Commit]) -> list[Visible]:
    """Read each visible change from checkpoint commits."""
    return [item for _, item in _checkpoint_entries(commits)]


def _line_value(doc: Path) -> str | None:
    """The path the doc's Change review line names: its first `quoted` part, else the words before
    any ` — ` note. None when the doc has no line."""
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith(LIST_HEADER):
            value = line.removeprefix(LIST_HEADER).strip()
            quoted = re.match(r"`([^`]+)`", value)
            return quoted.group(1).strip() if quoted else value.split(" — ")[0].strip()
    return None


def list_path(doc: Path, *,
              record: staff.StaffSnapshot | None = None) -> Path | None:
    """Return the root showrunner's list, or the document's path when its owner cannot be resolved.

    Return None when the document has no Change review line.
    Report a different path in that line.
    """
    value = _line_value(doc)
    if value is None:
        return None
    named = Path(value).expanduser() if value else None
    try:
        derived = list_file(showrunner_of(doc, record=record))
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
    """Encode a cell as one table line.

    A final line separator can drop because callers strip cell text.
    """
    escaped = _BREAK_TAG.sub(lambda match: "<\\" + match[1] + "br>", text.replace("|", "\\|"))
    return "<br>".join(escaped.splitlines())


def _plain(cell: str) -> str:
    """Decode line breaks and remove one backslash from each typed break tag."""
    return _BREAK_TAG.sub(lambda match: "<" + match[1][1:] + "br>" if match[1] else "\n",
                          cell.replace("\\|", "|"))


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
    return lists_directory() / f"{PREFIX}{showrunner}.md"


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
    with _list_lock(path):
        text = path.read_text(encoding="utf-8") if path.exists() else skeleton(path, production, day)
        lines = text.splitlines(keepends=True)
        result = write(lines)
        if lines == text.splitlines(keepends=True):
            _commit(path, what(result))
            return result
        _sort(path, lines)
        _stamp(path, lines, production, day)
        write_text_atomic(path, "".join(lines))
        _commit(path, what(result))
        return result


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


def _join_cells(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _row(path: Path, columns: list[str], values: dict[str, str]) -> str:
    """A row with each column's value, in the table's own column order."""
    if unknown := [column for column in columns if column not in values]:
        raise ChangeReviewError(f"{path}: no value for the `{unknown[0]}` column")
    return _join_cells(values[column] for column in columns)


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


def append_rows(path: Path, changes: Sequence[Visible | PublicationChange], day: date, merged: str, production: Path,
                rank: str = "", owners: Sequence[Path] = (),
                producers: Sequence[staff.StaffId | UnknownProducer] = (), *,
                record: staff.StaffSnapshot, repo: Path = _CURRENT_REPO,
                on_hold: Callable[[PublicationChange], None] | None = None,
                merge_changes: Sequence[PublicationChange] = ()) -> list[str]:
    """Add changes to To review with fixed staff IDs from record.

    owners supplies production documents; producers supplies fixed staff IDs for plain Visible changes.
    merge_changes supplies every change of this merge routed to this list.
    """
    if not changes:
        return []
    items = [change.item if isinstance(change, PublicationChange) else change for change in changes]
    row_owners = [change.source.production if isinstance(change, PublicationChange) and isinstance(change.source, UnitSource)
                  else owners[index] if owners else production for index, change in enumerate(changes)]
    row_producers = [change.producer if isinstance(change, PublicationChange)
                     else producers[index] if producers else UnknownProducer() for index, change in enumerate(changes)]

    identities = [str(unit_lookup.doc_owner(owner, record).id)
                  if isinstance(change, PublicationChange)
                  else str(producer) if isinstance(producer, str) else row_production(owner, record)
                  for change, owner, producer in zip(changes, row_owners, row_producers)]
    unregistered: set[str] = set()

    def write(lines: list[str]) -> list[str]:
        existing = _changes("".join(lines)) if any(isinstance(change, PublicationChange) for change in changes) else []
        covered = _merge_covered(existing, merge_changes, repo, merged)
        selected: list[int] = []
        for index, change in enumerate(changes):
            if isinstance(change, PublicationChange):
                if change in covered or (not merge_changes and _covered(existing, change, repo)):
                    continue
                if isinstance(change.item.readiness, AfterStep) and on_hold is not None:
                    on_hold(change)
                    continue
                existing.append({"Change": change.item.change, "Merged": merged})
            selected.append(index)
        if not selected:
            return []
        if rank:
            _add_column(path, lines, SECTION, URGENCY)
        _add_column(path, lines, SECTION, PRODUCTION)
        _add_column(path, lines, SECTION, PRODUCER)
        columns, first, end = _table(path, lines, SECTION)
        plain = [change for change in changes if isinstance(change, Visible)]
        if merged and (held := _held(columns, lines[first:end], plain, merged)):
            raise ChangeReviewError(f"{held} already holds that change from merge {merged}; no row added")
        number = next_number("".join(lines))
        when = f"{day.isoformat()} · {_cell(merged)}" if merged else day.isoformat()
        rows = [_row(path, columns, {"#": f"C{number + offset}", "Change": _cell(items[index].change), "Merged": when,
                                     "On the Mac build": NOT_YET, CHECK: _cell(items[index].how()), URGENCY: _cell(rank),
                                     KIND: "", PRODUCER: str(row_producers[index]) if isinstance(row_producers[index], str) else "",
                                     PRODUCTION: _cell(identities[index])})
                for offset, index in enumerate(selected)]
        _insert(lines, end, rows)
        unregistered.update(change.unit_name for index in selected
                            if isinstance(change := changes[index], PublicationChange)
                            and isinstance(change.producer, UnknownProducer) and change.unit_name)
        return rows

    def what(rows: list[str]) -> str:
        if not rows:
            return "no new rows"
        ids = [row_id(row) for row in rows]
        named = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
        if not merged:
            return f"add {named}"
        return f"{'row' if len(ids) == 1 else 'rows'} {named} from {merged}"

    rows = _edit(path, production, day, write, what)
    for name in sorted(unregistered):
        print(f"change review: no staff row for unit {name}; Producer stays empty")
    return rows


def _covered(rows: list[dict[str, str]], change: PublicationChange, repo: Path) -> bool:
    """A matching row covers checkpoints that its merge already contains."""
    for row in rows:
        if row.get("Change") != change.item.change:
            continue
        merged = _HASH.search(row.get("Merged", ""))
        if merged is None:
            return True
        known = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{merged[0]}^{{commit}}"],
                               capture_output=True, check=False)
        if known.returncode:
            return True
        ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", change.checkpoint, merged[0]],
                                  capture_output=True, check=False)
        if ancestor.returncode == 0:
            return True
    return False


def _merge_covered(rows: list[dict[str, str]], changes: Sequence[PublicationChange],
                   repo: Path, merged: str) -> list[PublicationChange]:
    """Use text matches first, then pair merge rows with ready changes and held changes from one step."""
    covered: list[PublicationChange] = []
    used: set[int] = set()
    for change in changes:
        for index, row in enumerate(rows):
            if _covered([row], change, repo):
                covered.append(change)
                used.add(index)
                break
    uncovered = [change for change in changes if change not in covered]
    ready = [change for change in uncovered if isinstance(change.item.readiness, ReadyNow)]
    held = [change for change in uncovered if isinstance(change.item.readiness, AfterStep)]
    steps = {change.item.readiness.step for change in held if isinstance(change.item.readiness, AfterStep)}
    remaining = iter(ready + (held if len(steps) == 1 else []))
    for index, row in enumerate(rows):
        found = _HASH.search(row.get("Merged", ""))
        if index not in used and found is not None and (found[0].startswith(merged) or merged.startswith(found[0])):
            change = next(remaining, None)
            if change is None:
                break
            covered.append(change)
    return covered


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
    return _review_table(path, lines, SECTION)


def scope_list(doc: Path, *, record: staff.StaffSnapshot | None = None) -> Path:
    """The list that takes the production's rows: the one its Change review line names, else its root's."""

    if doc.is_file() and (value := _line_value(doc)):
        return Path(value).expanduser()
    record = staff.snapshot() if record is None else record
    name = unit_lookup.doc_owner(doc, record).name
    if not doc.is_file():
        return list_file(name)
    root = showrunner_tree.root_of(name, record=record)
    try:
        return production_list(name, record=record)
    except ChangeReviewError:
        if name == root:
            raise
        return production_list(root, record=record)


@dataclass(frozen=True)
class ProductionReviewList:
    path: Path
    default_production: str


@dataclass(frozen=True)
class ScopedReviewEntry:
    review_list: ProductionReviewList
    entry: urgency.Entry[dict[str, str]]


def scope_lists(path: Path, doc: Path, below: bool = False, *,
                record: staff.StaffSnapshot | None = None) -> tuple[ProductionReviewList, ...]:
    """The current list and each scoped production's own earlier list, once per path."""
    record = staff.snapshot() if record is None else record
    owner = _owner(path) if path.name.startswith(PREFIX) else showrunner_of(doc, record=record)
    lists = {path.absolute(): ProductionReviewList(path, owner)}
    for production in scope_docs(doc, below, record=record):
        name = unit_lookup.doc_owner(production, record).name
        earlier = list_file(name)
        if earlier.absolute() in lists or earlier.exists():
            lists[earlier.absolute()] = ProductionReviewList(earlier, name)
    return tuple(lists.values())


def routed_review_entries(path: Path, doc: Path, below: bool = False,
                          record: staff.StaffSnapshot | None = None) -> list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]:
    """Read all source lists before the shared staff rule assigns their rows."""
    return _routed_review_entries(path, doc, below, record if record is not None else staff.snapshot())


def _routed_review_entries(path: Path, doc: Path, below: bool, record: staff.StaffSnapshot) -> list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]:
    """Route all rows with one consistent staff snapshot."""
    listed = {item.path.absolute(): item for item in scope_lists(path, doc, below, record=record)}
    for source in lists_directory().glob(PREFIX + "*.md"):
        _ = listed.setdefault(source.absolute(), ProductionReviewList(source, _owner(source)))
    documents = {row.name: row.doc.path for row in record.find(kind="showrunner")
                 if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))}
    snapshots: dict[Path, ReviewListSnapshot] = {}
    for source in listed:
        try:
            snapshot = review_snapshot(source)
        except (ChangeReviewError, OSError, UnicodeError) as error:
            print(f"change review: {source}: {error}", file=sys.stderr)
            continue
        snapshots[source] = snapshot
    documents = change_routing.production_documents(documents,
        ((listed[source].default_production, snapshot.text) for source, snapshot in snapshots.items()))
    routing = change_routing.StaffRouting(record, documents)
    names = {path.name for path in change_routing.review_root().glob(".review-link-delivery-*")}
    routed: list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]] = []
    for source, snapshot in snapshots.items():
        rows = change_routing.waiting_rows(source, snapshot.pending, names)
        try:
            entries = row_entries(rows)
        except ChangeReviewError as error:
            print(f"change review: {source}: {error}", file=sys.stderr)
            continue
        routed.extend((listed[source], routing.owner(entry.item, source), entry.item) for entry in entries)
    return routed


def review_scope(doc: Path, below: bool, record: staff.StaffSnapshot) -> set[str]:
    """Name the staff tabs counted for this production."""
    documents = set(scope_docs(doc, below, record=record))
    scope = {member.name for member in record.rows
             if member.kind == "showrunner" and isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))
             and member.doc.path in documents}
    return scope


def scoped_review_entries(path: Path, doc: Path, below: bool = False) -> list[ScopedReviewEntry]:
    """Use the same staff tab as the change page."""
    record = staff.snapshot()
    scope = review_scope(doc, below, record)
    grouped: dict[Path, tuple[ProductionReviewList, list[dict[str, str]]]] = {}
    for review_list, owner, row in _routed_review_entries(path, doc, below, record):
        if owner.tab in scope:
            if review_list.path not in grouped:
                grouped[review_list.path] = (review_list, [])
            grouped[review_list.path][1].append(row)
    found: list[ScopedReviewEntry] = []
    for review_list, rows in grouped.values():
        try:
            found.extend(ScopedReviewEntry(review_list, entry) for entry in row_entries(rows))
        except ChangeReviewError as error:
            print(f"change review: {review_list.path}: {error}", file=sys.stderr)
    return found


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
        return ChangeQueue(queue(path), row_entries(change_routing.waiting_rows(path, pending(path))))

    record = staff.snapshot()
    name = unit_lookup.doc_owner(scope, record).name
    name = queue(path) if name == showrunner_tree.root_of(name, record=record) else f"{queue(path)}-{name}"
    return ChangeQueue(urgency.scope_queue(name, below), scope_entries(path, scope, below))


def row_production(production: Path, record: staff.StaffSnapshot) -> str:
    """Read the fixed showrunner id for a production-owned change."""
    return str(unit_lookup.doc_owner(credited(production.expanduser(), record), record).id)


def credited(production: Path, record: staff.StaffSnapshot) -> Path:
    """Credit a style production to its staff parent. Other productions keep their own credit."""
    row = record.by_doc(remote.local_machine(), production)
    while row is not None and isinstance(row.parent, staff.ReportsTo):
        try:
            if production_field(production.read_text(encoding="utf-8").splitlines(), "Kind") != "style":
                return production
        except (OSError, UnicodeError, Refusal):
            return production
        row = record.by_id(row.parent.id)
        if row is None or not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            return production
        production = row.doc.path
    return production


@dataclass(frozen=True)
class MissingUnitWorktree:
    """The unit's branch worktree is unavailable."""


@dataclass(frozen=True)
class UnknownProducer:
    """The change has no known staff producer."""


UNKNOWN_PRODUCER = UnknownProducer()


@dataclass(frozen=True)
class UnitSource:
    """The production document and branch worktree of a unit."""

    production: Path
    worktree: Path | MissingUnitWorktree
    name: str
    producer: staff.StaffId | UnknownProducer = UnknownProducer()


def unit_branches(docs: Iterable[Path]) -> dict[str, UnitSource]:
    """Resolve exact branch names before plan, unit and folder aliases."""
    return _unit_branches(docs, _branch_snapshot())


def _branch_snapshot() -> staff.StaffSnapshot:
    """Return the staff snapshot used to resolve branches."""
    try:
        record = staff.snapshot()
    except staff.StaffUnreadable as error:
        print(f"change review: {error}", file=sys.stderr)
        record = staff.StaffSnapshot(())
    return record


def _unit_branches(docs: Iterable[Path], record: staff.StaffSnapshot) -> dict[str, UnitSource]:
    """Resolve branch names and aliases from the unit staff rows of each document."""
    return _branches_from_units(_document_units(docs, record), record)


def _document_units(docs: Iterable[Path], record: staff.StaffSnapshot) -> list[tuple[Path, staff.StaffRow]]:
    """Collect each document's units and report unreadable staff records."""
    pairs: list[tuple[Path, staff.StaffRow]] = []
    for doc in docs:
        try:
            rows = unit_lookup.doc_units(doc, record)
        except staff.StaffUnreadable as error:
            print(f"change review: {error}", file=sys.stderr)
            continue
        pairs.extend((doc, row) for row in rows)
    return pairs


def _branches_from_units(pairs: Iterable[tuple[Path, staff.StaffRow]],
                         record: staff.StaffSnapshot) -> dict[str, UnitSource]:
    """Resolve exact branches before aliases, in the supplied unit order."""
    found: dict[str, UnitSource] = {}
    aliases: list[tuple[UnitSource, tuple[str, ...]]] = []
    resolver = session_staff.RowResolver(record)
    machine = remote.local_machine()
    for doc, row in pairs:
        tree = row.worktree
        worktree = (tree.path if isinstance(tree, staff.PresentWorktree) and tree.machine == machine
                    else MissingUnitWorktree())
        source = UnitSource(doc, worktree, row.name, row.id)
        branch = resolver.unit_branch(row)
        if isinstance(branch, session_staff.Branch):
            _ = found.setdefault(branch.name, source)
        try:
            config = resolver.unit_config(row)
        except unit_config.ConfigRefused as error:
            print(f"change review: {source.name}: {error}", file=sys.stderr)
            config = unit_config.ConfigMissing(str(error))
        plan = (config.plan.path.stem if isinstance(config, unit_config.UnitConfig)
                and isinstance(config.plan, unit_config.PlanFile) else "")
        aliases.append((source, (plan, source.name, worktree.name if isinstance(worktree, Path) else "")))
    for source, names in aliases:
        for name in names:
            if name:
                _ = found.setdefault(name, source)
    return found


def _publication_branches(production: Path, record: staff.StaffSnapshot) -> dict[str, UnitSource]:
    """Resolve live units before retired units, with scoped units first in each group."""
    pairs = _document_units(_publication_docs(production, record), record)
    scoped = {pair[1].id for pair in pairs}
    machine = remote.local_machine()
    resolver = session_staff.RowResolver(record)
    for row in record.find(kind="unit"):
        if row.id in scoped or isinstance(row.worktree, staff.WorktreeUnfilled) or row.worktree.machine != machine:
            continue
        doc = resolver.production_doc(row)
        pairs.append((doc.path if isinstance(doc, session_staff.ProductionDoc) else production, row))
    pairs.sort(key=lambda pair: isinstance(pair[1].worktree, staff.RemovedWorktree))
    return _branches_from_units(pairs, record)


def _owner_of(commit: Commit, branches: dict[str, UnitSource]) -> Path | None:
    """The production of the unit whose branch the checkpoint names, or None for another commit."""
    found = _CHECKPOINT.match(commit.subject)
    unit = branches.get(found["branch"]) if found else None
    return unit.production if unit is not None else None


@dataclass(frozen=True)
class UnresolvedSource:
    """The checkpoint has no matching unit source."""


@dataclass(frozen=True)
class PublicationChange:
    """One checkpoint change with its unit and fixed producer identity."""

    item: Visible
    source: UnitSource | UnresolvedSource
    producer: staff.StaffId | UnknownProducer
    checkpoint: str
    unit_name: str


def producer_for(source: UnitSource, record: staff.StaffSnapshot, machine: str) -> staff.StaffId | UnknownProducer:
    """Use the fixed producer from the shared member join on this machine."""
    if isinstance(source.producer, UnknownProducer):
        return UnknownProducer()
    member = record.by_id(source.producer)
    if member is None or member.kind != "unit" or isinstance(member.worktree, staff.WorktreeUnfilled):
        return UnknownProducer()
    return member.id if member.worktree.machine == machine else UnknownProducer()


def producer_at(directory: Path, record: staff.StaffSnapshot) -> staff.StaffId | UnknownProducer:
    """Read the fixed id of the unit that owns the requested directory."""
    machine = remote.local_machine()
    member = record.unit_by_worktree(machine, directory) or record.by_worktree(machine, directory)
    return member.id if member is not None and member.kind == "unit" else UnknownProducer()


def unit_producer(unit: str) -> staff.StaffId | UnknownProducer:
    """Read the producer of one unit across the staff record."""
    try:
        return unit_lookup.unit_row(unit, staff.snapshot()).id
    except staff.StaffUnreadable:
        return UNKNOWN_PRODUCER


def _publication_docs(production: Path, record: staff.StaffSnapshot) -> list[Path]:
    """Resolve child production documents with the held staff snapshot."""
    return scope_docs(production, below=True, record=record)


def publication_changes(commits: list[Commit], production: Path, *,
                        record: staff.StaffSnapshot | None = None,
                        known_producer: staff.StaffId | UnknownProducer = UNKNOWN_PRODUCER) -> list[PublicationChange]:
    """Resolve each visible change's unit and producer in one pass."""
    record = staff.snapshot() if record is None else record
    branches = _publication_branches(production, record)
    machine = remote.local_machine()
    changes: list[PublicationChange] = []
    for commit, item in _checkpoint_entries(commits):
        match = _CHECKPOINT.match(commit.subject)
        source = branches.get(match["branch"]) if match is not None else None
        producer = producer_for(source, record, machine) if source is not None else UNKNOWN_PRODUCER
        if isinstance(known_producer, str) and (source is None or source.producer == known_producer):
            producer = known_producer
        name = source.name if source is not None and source.name else match["branch"] if match is not None else ""
        changes.append(PublicationChange(item, source if source is not None else UnresolvedSource(), producer, commit.sha, name))
    return changes


def merge_commits(repo: Path, merge: str, *, excluded_refs: Sequence[str] = ()) -> list[Commit]:
    """The commits a merge brought in, without merges; none for a commit that is no merge."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", merge, "--").split()[1:]
    if len(parents) < 2:
        return []
    records = _git(repo, "log", "--no-merges", "--format=%x1e%H%x1f%B", parents[1], f"^{parents[0]}",
                   *(f"^{ref}" for ref in excluded_refs), "--")
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
        self.record: staff.StaffSnapshot = _branch_snapshot()
        self.branches: dict[str, UnitSource] = _unit_branches(self.docs, self.record)
        self.repos: list[Path] = []
        self.targets: dict[tuple[Path, str], Path] = {}
        self._chains: dict[tuple[Path, str], set[str]] = {}
        for doc in self.docs:
            try:
                lines = doc.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for field in ("Repository", "Showrunner checkout"):
                try:
                    repo = Path(production_field(lines, field)).expanduser()
                except Refusal:
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


def matching_checkpoint_names(commits: list[Commit], change: str) -> tuple[str, ...]:
    """Match the change text before resolving a checkpoint name."""
    return tuple(dict.fromkeys(match["branch"] for commit in commits
                 if change in checkpoint_changes(commit.message)
                 and (match := _CHECKPOINT.match(commit.subject)) is not None))


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
    named = {source.production for name in matching_checkpoint_names(commits, change)
             if (source := tree.branches.get(name)) is not None}
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

    owned: list[Path] = []
    record = staff.snapshot()
    for doc in docs:
        name = unit_lookup.doc_owner(doc, record).name
        try:
            if showrunner_tree.root_of(name, record=record) != name or list_path(doc, record=record) != path:
                continue
        except OSError:
            continue
        owned.extend(owner_docs(doc, record=record))
    return Tree(owned)


def _fill_column(path: Path, lines: list[str], section: str, column: str,
                 values: dict[str, str]) -> set[str]:
    """Add a column and fill only its empty cells."""
    _add_column(path, lines, section, column)
    columns, first, end = _table(path, lines, section)
    at = columns.index(column)
    changed: set[str] = set()
    for index in range(first, end):
        cells = _cells(lines[index])
        if cells[0] in values and at < len(cells) and not cells[at]:
            cells[at] = _cell(values[cells[0]])
            lines[index] = _join_cells(cells) + "\n"
            changed.add(cells[0])
    return changed


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
            filled[row["#"]], owners[row["#"]] = row_production(found, tree.record), found
        else:
            untraced[row["#"]] = found
    if not write or not filled:
        return filled, untraced

    def fill(lines: list[str]) -> None:
        _ = _fill_column(path, lines, SECTION, PRODUCTION, filled)

    _edit(path, next(iter(owners.values())), today(), fill, lambda _: f"production cells for {len(filled)} rows")
    return filled, untraced


@contextmanager
def _list_lock(path: Path) -> Generator[None]:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def describe(row: dict[str, str]) -> str:
    """One row in plain lines: its id and change, how to check it, then each other column that has a value."""
    record = staff.initial_snapshot()
    shown = dict(row)
    member = record.by_id(staff.StaffId(row.get(PRODUCTION, "")))
    if member is not None and not row.get("Verdict"):
        parent = record.by_id(member.parent.id) if member.kind == "unit" and isinstance(member.parent, staff.ReportsTo) else member
        shown[PRODUCTION] = parent.name if parent is not None else member.name
    producer = record.by_id(staff.StaffId(row.get(PRODUCER, "")))
    if producer is not None:
        shown[PRODUCER] = producer.name
    rest = [f"  {column}: {value}" for column, value in shown.items() if column not in ("#", "Change", CHECK) and value]
    return "\n".join([f"{row.get('#', '')} {row.get('Change', '')}", f"  {CHECK}: {row.get(CHECK, '')}", *rest])


def lands(verdict: str) -> bool:
    """Land on an opening `works` or `fine`, after optional `okay`/`ok` and row id.
    Case and later words do not affect the result; any other start returns the row."""
    return _LANDS.match(verdict) is not None


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
        identity = staff.initial_snapshot()
        member = identity.by_id(staff.StaffId(_plain(cells.get(PRODUCTION, ""))))
        if member is not None:
            parent = identity.by_id(member.parent.id) if member.kind == "unit" and isinstance(member.parent, staff.ReportsTo) else member
            cells[PRODUCTION] = _cell(parent.name if parent is not None else member.name)
        if not any(line.strip() == REVIEWED for line in lines):
            reviewed_rows = list[str](("\n" + BODY[BODY.index(REVIEWED):]).splitlines(keepends=True))
            _insert(lines, len(lines), reviewed_rows)
        _add_column(path, lines, REVIEWED, "Merged")
        if member is not None:
            _add_column(path, lines, REVIEWED, PRODUCTION)
        if PRODUCER in cells:
            _add_column(path, lines, REVIEWED, PRODUCER)
        # Page rows retain their description and proof in the same record after
        # answering. Older lists keep their existing Reviewed table shape.
        if cells.get(KIND):
            for column in (KIND, CHECK, PRODUCTION):
                _add_column(path, lines, REVIEWED, column)
        reviewed, top, _ = _table(path, lines, REVIEWED)
        _insert(lines, top, [_row(path, reviewed, {**dict.fromkeys(reviewed, ""), **cells, "#": row,
                                                   "Verdict": _cell(verdict), "Date": day.isoformat(),
                                                   "Merged": cells.get("Merged", "")})])
        return VerdictRecorded(row_merge(cells))

    return _edit(path, production, day, write, lambda _: f"verdict {row}")


def review_rows(path: Path) -> list[dict[str, str]]:
    """Read pending and saved page answers from the authoritative list."""
    snapshot = review_snapshot(path)
    return [*snapshot.pending, *snapshot.reviewed]


def _review_table(path: Path, lines: list[str], section: str) -> list[dict[str, str]]:
    """Decode one review table and refuse rows with missing or extra cells."""
    columns, first, end = _table(path, lines, section)
    result: list[dict[str, str]] = []
    for line in lines[first:end]:
        cells = _cells(line)
        if len(cells) != len(columns):
            raise ChangeReviewError(f"{path}: {cells[0] if cells else line.rstrip()} has the wrong number of cells")
        result.append(dict(zip(columns, map(_plain, cells))))
    return result


@dataclass(frozen=True)
class ReviewListSnapshot:
    """One list read, with its metadata and both review tables."""

    text: str
    pending: list[dict[str, str]]
    reviewed: list[dict[str, str]]


def review_snapshot(path: Path) -> ReviewListSnapshot:
    text = read(path)
    lines = text.splitlines(keepends=True)
    pending = _review_table(path, lines, SECTION)
    reviewed = _review_table(path, lines, REVIEWED) if any(line.strip() == REVIEWED for line in lines) else []
    return ReviewListSnapshot(text, pending, reviewed)


def set_row_kind(path: Path, row: str, kind: str, production: Path) -> str:
    """Classify by whether the change alters something the user runs or sees."""
    if kind not in REVIEW_KINDS:
        raise ChangeReviewError("--kind must be 'See in action' or 'Read only'")

    def write(lines: list[str]) -> str:
        _add_column(path, lines, SECTION, KIND)
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        written = _row(path, columns, {**cells, KIND: kind})
        lines[index] = written + "\n"
        return written

    return _edit(path, production, today(), write, lambda _: f"kind {row}")


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


def production_doc(production: str, *, record: staff.StaffSnapshot | None = None) -> Path:
    """The production doc given; without one, the calling showrunner's."""
    from scripts.production import showrunners

    if production:
        row = showrunners.owner_for(production, record)
        if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            return row.doc.path
        raise ChangeReviewError(f"{row.name}: no production document")
    found = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if found is None:
        raise ChangeReviewError("this session is no production's showrunner; pass --production <name>")
    return found


def showrunner_of(doc: Path, *,
                  record: staff.StaffSnapshot | None = None) -> str:
    """Read the staff name of the root showrunner."""

    record = staff.snapshot() if record is None else record
    row = unit_lookup.doc_owner(doc, record)
    return showrunner_tree.root_of(row.name, record=record)


def production_list(production: str, *, record: staff.StaffSnapshot | None = None) -> Path:
    """Return the review list for the staff name, or for the calling showrunner when the name is empty."""
    doc = production_doc(production, record=record)
    path = list_path(doc, record=record)
    if path is None:
        raise ChangeReviewError(f"{doc} has no `{LIST_HEADER}` line")
    return path


def _words(name: str, value: str) -> str:
    """One line of words for a cell; empty or several lines is refused."""
    if not value.strip() or "\n" in value or "\r" in value:
        raise ChangeReviewError(f"--{name} needs one line of words")
    return value.strip()


@dataclass(frozen=True)
class KnownMergeProducer:
    """The unit identity supplied by the checkpoint request for one merge."""

    merge: str
    producer: staff.StaffId


@dataclass(frozen=True)
class ReadinessScan:
    """Scan published merges for changes that need no further step."""


@dataclass(frozen=True)
class CompletedStep:
    merged: str
    step: str


_READINESS_SCAN = ReadinessScan()


def _publication_exclusions(repo: Path, merge: str, refs: Iterable[str]) -> list[str]:
    """Exclude foreign history before each branch imports this merge."""
    merge = _git(repo, "rev-parse", merge).strip()
    excluded: set[str] = set()
    for ref in refs:
        path = [line.split() for line in _git(
            repo, "rev-list", "--ancestry-path", "--parents", f"{merge}..{ref}").splitlines()]
        if not path:
            if _git(repo, "rev-parse", ref).strip() != merge:
                excluded.add(ref)
            continue
        descendants = {merge, *(commit[0] for commit in path)}
        excluded.update(parent for commit in path for parent in commit[1:] if parent not in descendants)
    return sorted(excluded)


def list_ready(repo: Path, production: Path, merge_branch: str, *,
               review_list: Path | None = None, record: staff.StaffSnapshot | None = None,
               selection: ReadinessScan | CompletedStep = _READINESS_SCAN,
               known_producer: KnownMergeProducer | UnknownProducer = UNKNOWN_PRODUCER) -> list[str]:
    """Add live changes on origin/main, or release one completed step of one merge."""
    repo = repo.expanduser().resolve()
    try:
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "origin", "main"],
                                 capture_output=True, text=True, check=False)
    except OSError as error:
        print(f"change review: git fetch origin main failed; no rows added: {str(error).splitlines()[0]}", file=sys.stderr)
        return []
    if fetched.returncode:
        detail = " ".join((fetched.stderr or fetched.stdout).splitlines())
        print(f"change review: git fetch origin main failed; no rows added: {detail}", file=sys.stderr)
        return []
    if isinstance(selection, CompletedStep):
        _require_main(repo, selection.merged)
        merges = _git(repo, "log", "-1", "--format=%H%x09%s", selection.merged, "--").splitlines()
    else:
        merges = _git(repo, "log", "--reverse", "--first-parent", "--merges", f"--since={READY_FROM}T00:00:00Z",
                      "--format=%H%x09%s", f"origin/{merge_branch}", "--").splitlines()
    record = staff.snapshot() if record is None else record
    scoped = set(_publication_docs(production, record))
    outside_docs = [row.doc.path for row in record.find(kind="showrunner")
                    if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.path not in scoped]
    outside_refs = set(reviewed_merge_refs(repo, lambda: outside_docs, record=record))
    rows: list[str] = []
    matched_step = False
    for entry in merges:
        merge, subject = entry.split("\t", 1)
        if not subject.endswith(f" into {merge_branch}"):
            continue
        if not on_main(repo, merge):
            continue
        commits = merge_commits(repo, merge, excluded_refs=_publication_exclusions(repo, merge, outside_refs))
        short = _git(repo, "rev-parse", f"--short={SHORT}", merge).strip()
        routed: dict[tuple[Path, Path], list[PublicationChange]] = {}
        merge_changes: dict[Path, list[PublicationChange]] = {}
        noted: set[str] = set()

        def note(change: PublicationChange) -> None:
            readiness = change.item.readiness
            if not isinstance(readiness, AfterStep) or readiness.step in noted:
                return
            checkout = Path(__file__).resolve().parents[2]
            owner = unit_lookup.doc_owner(production, record)
            command = shlex.join([str(checkout / "scripts/lib/py"), str(checkout / "scripts/production/change_review.py"),
                                  "live", "--merged", short, "--step", readiness.step,
                                  "--repo", str(repo), "--production", owner.name])
            print(f"{short}: after {readiness.step}; run {command}")
            noted.add(readiness.step)

        producer = (known_producer.producer
                    if isinstance(known_producer, KnownMergeProducer) and known_producer.merge == merge
                    else UNKNOWN_PRODUCER)
        for change in publication_changes(commits, production, record=record, known_producer=producer):
            readiness = change.item.readiness
            if isinstance(selection, CompletedStep) and isinstance(readiness, AfterStep) and readiness.step == selection.step:
                matched_step = True
            owner_doc = credited(change.source.production if isinstance(change.source, UnitSource) else production, record)
            path = review_list if review_list is not None else list_path(owner_doc, record=record)
            if path is None:
                path = list_path(production, record=record)
            if path is None:
                continue
            routed.setdefault((path, owner_doc), []).append(change)
            merge_changes.setdefault(path, []).append(change)
        for (path, owner_doc), changes in routed.items():
            selected = changes
            if isinstance(selection, CompletedStep):
                selected = [change for change in changes if isinstance(change.item.readiness, AfterStep)
                            and change.item.readiness.step == selection.step]
            rows.extend(append_rows(path, selected, today(), short, owner_doc, record=record, repo=repo,
                                    on_hold=note if isinstance(selection, ReadinessScan) else None,
                                    merge_changes=merge_changes[path]))
    if isinstance(selection, CompletedStep) and not matched_step:
        raise ChangeReviewError(f"{selection.merged}: no change waits for step {selection.step!r}; no row added")
    return rows


def _changes(text: str) -> list[dict[str, str]]:
    """Read each row's cells under To review and Reviewed.

    A reviewed row keeps its change name and its Merged cell.
    """
    found: list[dict[str, str]] = []
    columns: list[str] = []
    for line in text.splitlines():
        if line.startswith("| # |"):
            columns = _cells(line)
        elif _NUMBER.match(line):
            found.append(dict(zip(columns, map(_plain, _cells(line)))))
    return found


def _files(repo: Path, sha: str) -> tuple[str, ...]:
    return tuple(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split())


class CommandLine(argparse.Namespace):
    action: str = ""
    repo: str = "."
    checkpoint: str = ""
    previous: str = ""
    merge_branch: str = ""
    list: str = ""
    merged: str = ""
    step: str = ""
    change: str = ""
    check: str = ""
    see: str = ""
    row: str = ""
    verdict: str = ""
    kind: str = ""
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
    change = _words("change", options.change)
    for name, value in (("check", options.check), ("see", options.see)):
        if not value.strip():
            _ = _words(name, value)
    return Visible(change, options.check.strip(), options.see.strip())


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


def _backfill_targets(options: CommandLine) -> tuple[list[Path], list[Path]]:
    from scripts.production import showrunners

    documents = list(showrunners.timer_docs().values())
    lists = [Path(options.list).expanduser()] if options.list else sorted(lists_directory().glob(f"{PREFIX}*.md"))
    return documents, lists


def _run(options: CommandLine) -> int:
    match options.action:
        case "add":
            repo = Path(options.repo).expanduser().resolve()
            merged = options.merged.strip()
            if merged:
                _require_main(repo, merged)
            item, rank = _visible(options), _rank_option(options)
            path = _list(options)
            record = staff.snapshot()
            rows = append_rows(path, [item], today(), merged, production_doc(options.production), rank,
                               producers=[producer_at(repo, record)], record=record)
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
        case "set-kind":
            target = _row_option(options)
            print(set_row_kind(target.path, target.row, options.kind, production_doc(options.production)))
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
            docs, lists = _backfill_targets(options)
            for path in lists:
                filled, untraced = backfill(path, list_tree(path, docs), options.write)
                print(f"{path.name}: {len(filled)} {'filled' if options.write else 'to fill'}, "
                      + f"{len(untraced)} untraced")
                for row, name in filled.items():
                    print(f"  {row}: {name}")
                for row, why in untraced.items():
                    print(f"  {row}: untraced, {why}")
        case "list-ready" | "live":
            selection = (CompletedStep(_words("merged", options.merged), _words("step", options.step))
                         if options.action == "live" else ReadinessScan())
            production = production_doc(options.production)
            branch = options.merge_branch or production_field(production.read_text(encoding="utf-8").splitlines(), "Merge branch")
            rows = list_ready(Path(options.repo), production, branch,
                              review_list=Path(options.list).expanduser() if options.list else None,
                              selection=selection)
            if rows:
                print("\n".join(rows))
        case "check":
            commits = unit_commits(Path(options.repo).resolve(), options.checkpoint, options.previous,
                                   options.merge_branch)
            refused = missing(commits)
            print("\n".join(refused) if refused else f"{len(commits)} commit(s) carry their {TRAILER} lines")
            return 1 if refused else 0
        case _:
            pass
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("action", choices=("check", "list-ready", "live", "add", "edit", "show", "verdict",
                                                   "mark-important", "set-kind", "list-path", "showrunner",
                                                   "backfill-production"))
    _ = parser.add_argument("--repo", default=".")
    for name in ("checkpoint", "previous", "merge-branch", "list", "merged", "step", "change", "check", "see",
                 "row", "verdict", "production", "urgency", "due", "after", "kind"):
        _ = parser.add_argument(f"--{name}", default="")
    _ = parser.add_argument("--write", action="store_true")
    _ = parser.add_argument("--hold", action="append")
    try:
        return _run(cast(CommandLine, parser.parse_args(argv)))
    except (ChangeReviewError, OSError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
