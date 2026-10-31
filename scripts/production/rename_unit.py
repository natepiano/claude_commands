#!/usr/bin/env python3
"""Plan, apply and verify one unit's rename, journaled so a refusal resumes where it stopped.

Usage: rename_unit.py plan|apply|verify <slug> <old> <new> [--scratch <showrunner state directory>]

`<old>` is the unit's Units id, or its stem; `<new>` takes an id or a stem and becomes
`<stem>-unit`, on branch `<stem>` in `~/worktrees/<stem>`. A same-name request repairs
only mismatched names and paths; an already matching unit stays unchanged. apply saves the request before it changes anything, pauses
the unit through stop_resume, pauses its Codex workers, stops its runs'
app-servers and keeps each paused worker's detached runs (it refuses while one works inside the
worktree), merges the merge branch into the unit, moves the branch and
worktree (unit_move), commits `chore(<slug>): rename unit <old> to <new>` on the merge branch, which
merge_branch_history follows, merges that commit into the unit, moves the unit's state, retires the
old branch, points the unit's shutdown record at the new names (each paused Codex worker's launcher
folder and resume prompt included), relaunches the unit from it and verifies; verify also fails
while a process works in the old worktree path. One apply does all of it: it stops the unit and waits for the relaunched
unit to come up live (LIVE_WAIT_SECONDS); a rerun resumes an interrupted rename.
The steps that write git run under the merge lock (merge_checkpoint.merge_lock), so they
wait for a merge gate that is running, and a gate waits for them. plan writes nothing: it runs every refusal check and prints the steps with their old and new values.
verify reruns the last step on a finished rename.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "herdr"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "style"))
import add_unit  # noqa: E402
from production_units import Production, read_production  # noqa: E402
import issues  # noqa: E402
import merge_checkpoint  # noqa: E402
import phase_table  # noqa: E402
import production_units  # noqa: E402
import proposal_issue  # noqa: E402
import proposals_folder  # noqa: E402
import retire_unit  # noqa: E402
import pane_record  # noqa: E402
import rename_state  # noqa: E402
import review_pool  # noqa: E402
from scripts.production import showrunners  # noqa: E402
import stall_watch  # noqa: E402
import unit_lookup  # noqa: E402
import herdr  # noqa: E402
import unit_move  # noqa: E402
import unit_names  # noqa: E402
import unit_rename_sweep  # noqa: E402
from rename_state import RenameRefused, read_json, write_json, write_text  # noqa: E402
from unit_move import PathMove, git  # noqa: E402
from user_zone import user_zone  # noqa: E402
from scripts.production import staff, staff_sources

if TYPE_CHECKING:
    from scripts.style import style_ledger
    from scripts.shutdown import shutdown_record
    from scripts.shutdown.shutdown_record import SessionShutdown
else:
    import style_ledger
    import shutdown_record
    from shutdown_record import SessionShutdown

RESERVED = frozenset({add_unit.NIGHTLY_REVIEW_UNIT})
# Columns a unit rename edits outside the Units row; a File or Gate cell is never touched.
EDITED_COLUMNS = {"## Hub files": ("Owner unit", "Other units that touch it"), "## Gates": ("Waiting", "Waits on")}
PROGRESS = ("merged", "doc_commit", "unit_tip", "account", "session_id", "kept", "old_session")
SHUTDOWN = Path(os.environ.get("RENAME_UNIT_SHUTDOWN") or Path(__file__).resolve().parent.parent / "shutdown/shutdown.py")
STOPPED = frozenset({"stopped", "already gone", "process identity lost"})
LIVE_WAIT_SECONDS = float(os.environ.get("RENAME_UNIT_LIVE_WAIT") or 180)
# A run supervisor passes SIGTERM to its command and exits when that ends: a lint seen
# 2026-10-20 took longer than retire_unit's 10 s.
RUN_STOP_WAIT_SECONDS = 120.0
POLL_SECONDS = 1.0


class RenameWaiting(RenameRefused):
    """The unit is still stopping after STOP_WAIT_SECONDS: a rerun of apply resumes once it has stopped."""


class StopIdentityEvidence(Enum):
    """Whether the journal established a pane target, including explicitly having none."""

    RECORDED = "recorded"
    LEGACY_UNKNOWN = "legacy-unknown"


class StopTarget(Protocol):
    """What the stop steps read: the unit's production and ids, and where its worktree is now.
    A rename and a transfer both stop a unit this way."""
    @property
    def slug(self) -> str: ...
    @property
    def old(self) -> str: ...
    @property
    def new(self) -> str: ...
    @property
    def id_matches(self) -> bool: ...
    # Bound before stopping, so relaunch keeps the conversation and account.
    account: str
    session_id: str
    pane_session: str
    pane_id: str
    terminal_id: str
    stop_identity: StopIdentityEvidence

    def worktree(self) -> Path: ...


@dataclass(frozen=True)
class UnboundStaffMember:
    """A read-only unit description has not been bound to a staff identity."""


@dataclass
class UnitRename:
    slug: str
    old: str
    new: str
    doc: str
    checkout: str
    merge_branch: str
    old_branch: str
    new_branch: str
    old_worktree: str
    new_worktree: str
    tip: str
    scratch: str
    step: int = 0
    # Filled as the steps run: the unit tip after the merge-branch merge, the rename commit, the
    # unit tip that holds it, and the shutdown record the unit relaunches from (empty when the unit
    # was not running).
    merged: str = ""
    doc_commit: str = ""
    unit_tip: str = ""
    account: str = ""
    session_id: str = ""
    # Exact place and terminal saved before stopping; conversation metadata can clear on exit.
    pane_session: str = ""
    pane_id: str = ""
    terminal_id: str = ""
    stop_identity: StopIdentityEvidence = StopIdentityEvidence.RECORDED
    # Why the unit branch took no merge: its work was in progress (an open phase or uncommitted work).
    kept: str = ""
    # The Claude session's name before the rename, which verify finds nowhere live afterwards.
    old_session: str = ""
    # The id already matches; branch, worktree, tab and session may still need repair.
    id_matches: bool = False
    staff_id: staff.StaffId | UnboundStaffMember = field(default_factory=UnboundStaffMember)

    @property
    def message(self) -> str:
        if self.id_matches:
            return f"chore({self.slug}): repair unit {self.new} names"
        return f"chore({self.slug}): rename unit {self.old} to {self.new}"

    @property
    def moves_git(self) -> bool:
        return self.old_branch != self.new_branch or self.old_worktree != self.new_worktree

    def worktree(self) -> Path:
        """Where the unit's worktree is now: the git step moves it."""
        return Path(self.new_worktree if self.step >= step_number(_move_git) else self.old_worktree)


PANE_IDENTITY_FIELDS = ("pane_session", "pane_id", "terminal_id")
REQUIRED = tuple(field.name for field in dataclasses.fields(UnitRename) if field.name not in ("step", "id_matches", "staff_id", "stop_identity", *PANE_IDENTITY_FIELDS, *PROGRESS))
LOCATION_FIELDS = ("doc", "checkout")


def _production(slug: str) -> Production:
    members = staff_sources.showrunner_members(staff.snapshot())
    named = [current for current, row in members.items() if row.name == slug]
    if len(named) > 1:
        raise RenameRefused(f"{slug}: staff showrunner has more than one production")
    slug = named[0] if named else showrunners.canonical_slug(slug)
    instance = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}"
    checked = showrunners.checked_doc(instance)
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{slug} has no readable update timer: {checked.reason}")
    return read_production(checked.path)


def _rows(doc: Path) -> list[dict[str, str]]:
    return production_units.unit_table(doc.read_text(encoding="utf-8").splitlines())


def _ids(old: str, new: str, units: list[str]) -> tuple[str, str]:
    """The old and new unit ids as given: an exact Units id is kept, a stem gains `-unit`."""
    try:
        return (old if old in units else unit_names.unit_id(old)), unit_names.unit_id(new)
    except unit_names.BadName as error:
        raise RenameRefused(str(error)) from error


def _build(slug: str, old: str, new: str, scratch: Path) -> UnitRename:
    production = _production(slug)
    slug = production.slug
    rows = _rows(production.doc)
    units = [production_units.cell_value(row.get("Unit", "")) for row in rows]
    given = old
    old, new = _ids(old, new, units)
    if old not in units:
        # The user names a unit by what they see: its Claude session name (user, 2026-10-19).
        old = _unit_of_session(slug, given, units) or old
    if RESERVED & {old, new}:
        raise RenameRefused(f"{' and '.join(sorted(RESERVED & {old, new}))} is known to code and keeps its name")
    id_matches = old == new
    old_session = _session_name(slug, old)
    if new in units and not id_matches:
        raise RenameRefused(f"{new} is already in the Units table")
    matching = [row for row, unit in zip(rows, units, strict=True) if unit == old]
    if len(matching) != 1:
        raise RenameRefused(f"{old}: expected one Units row, found {len(matching)}")
    row = matching[0]
    if production_units.plan_cell_is_retired(row.get("Plan", "")):
        raise RenameRefused(f"{old} is retired; its row is history")
    recorded_branch = production_units.cell_value(row.get("Branch", ""))
    worktree = Path(production_units.cell_value(row.get("Worktree", ""))).expanduser()
    new_stem = unit_names.stem(new)
    new_branch = unit_names.branch(new_stem)
    new_worktree = unit_names.worktree(new_stem)
    if id_matches and (new_worktree / ".git").is_file():
        worktree = new_worktree
    if not (worktree / ".git").is_file():
        # A Mac unit's worktree is on the Mac: version one renames natedev units only.
        raise RenameRefused(f"{old}: no linked worktree at {worktree} on this machine; a Mac unit is not renamed")
    old_branch = git(worktree, "branch", "--show-current")
    if old_branch != recorded_branch and not id_matches:
        raise RenameRefused(f"{worktree} is not on {recorded_branch}")
    if not old_branch:
        raise RenameRefused(f"{worktree} is not on a branch")
    rename = UnitRename(slug, old, new, str(production.doc), str(production.checkout), production.merge_branch,
                      old_branch, new_branch, str(worktree), str(new_worktree),
                      git(worktree, "rev-parse", "HEAD"), str(scratch), old_session=old_session,
                      id_matches=id_matches)
    doc_text = production.doc.read_text(encoding="utf-8")
    if (id_matches and old_branch == new_branch and worktree == new_worktree
            and old_session in ("", new_stem) and not _tab_failures(rename)
            and _doc_text(rename, doc_text) == doc_text):
        rename.step = len(STEPS)
    return rename


def _staff_id(slug: str, unit: str) -> staff.StaffId:
    found = staff_sources.member(staff_sources.UnitMember(slug, unit), staff.snapshot())
    if isinstance(found, staff_sources.NoSuchMember):
        raise staff.StaffRefused(f"{slug}/{unit}: no matching unit staff row")
    return found.row.id


def _unit_of_session(slug: str, name: str, units: list[str]) -> str:
    """The one unit whose live or recorded Claude session is called `name`; empty when none is."""
    try:
        marked = unit_lookup.marked_units(slug)
    except OSError as error:
        raise RenameRefused(f"{name}: cannot tell which unit's session it names: {error}") from error
    found = {unit for unit, place in marked.items()
             if isinstance(place.claude, unit_lookup.LiveClaude) and place.claude.name == name}
    for item in shutdown_records(slug, tuple(units)):
        host = item["session"]["host"]
        if host["kind"] == "unit" and item["session"]["name"] == name:
            found.add(host["unit"])
    if len(found) > 1:
        raise RenameRefused(f"{name} names the sessions of {' and '.join(sorted(found))}; give the unit id")
    return next(iter(found), "")


def _session_name(slug: str, unit: str) -> str:
    """The unit's Claude session name: the live one, else its stopped record's; empty when neither."""
    try:
        found = unit_lookup.marked_unit(slug, unit)
    except OSError as error:
        raise RenameRefused(f"{unit}: cannot tell whether Claude is running: {error}") from error
    if found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
        return found.claude.name
    names = [item["session"]["name"] for item in shutdown_records(slug, (unit,))]
    return names[0] if names else ""


def _check_stopped(rename: UnitRename, *, relaunch: bool = False) -> None:
    """The unit runs no Claude under either id, and nothing works inside its worktree.

    Its runs' leftovers are ended here too: a rename resumed past its stop step never ran that
    step's `_stop_runs`, such as one stopped before the stop ended runs. Only
    relaunch may accept its recorded session already live under the new id."""
    try:
        marked = unit_lookup.marked_units(rename.slug)
    except OSError as error:
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {error}") from error
    relaunched = False
    for unit in (rename.old, rename.new):
        found = marked.get(unit)
        if found and isinstance(found.claude, unit_lookup.LiveClaude):
            if relaunch and unit == rename.new and found.claude.session_id == rename.session_id:
                relaunched = True
            else:
                raise RenameRefused(f"{unit} has a live Claude; stop it first")
        if found and isinstance(found.claude, unit_lookup.ClaudeUnknown):
            raise RenameRefused(f"{unit}: cannot tell whether Claude is running: {found.claude.reason}")
    if relaunched:
        # Restart owns this session already. Do not stop the workers it restored.
        return
    _stop_runs(rename)
    pids = unit_move.processes_inside(rename.worktree())
    if pids:
        raise RenameRefused(f"processes {', '.join(map(str, pids))} work inside {rename.worktree()}; stop them first")


def check_stoppable(rename: StopTarget) -> None:
    """What the stop step cannot settle: the new id running, a Claude it cannot read, or processes
    in the worktree of a unit with no Claude to stop. A live old unit is the stop step's to stop."""
    _refuse_detached_worktree(rename)
    try:
        marked = unit_lookup.marked_units(rename.slug)
    except OSError as error:
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {error}") from error
    old, new = marked.get(rename.old), marked.get(rename.new)
    if new and not rename.id_matches and not isinstance(new.claude, unit_lookup.ClaudeNotRunning):
        raise RenameRefused(f"{rename.new} already runs in session {new.place.session}")
    if old and isinstance(old.claude, unit_lookup.ClaudeUnknown):
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {old.claude.reason}")
    pane = unit_move.unit_pane(old, rename.slug)
    if old and isinstance(old.claude, unit_lookup.LiveClaude):
        return
    inside = unit_move.processes_inside(rename.worktree())
    runs = retire_unit.run_processes(rename.worktree(), inside) if inside else {}
    retained = _retained_command_sessions(rename.worktree())
    pids: list[int] = []
    for pid in inside:
        if pane is not None and pid == pane.shell:
            continue  # The guarded close, after the stop, must remove it.
        if pid in runs:
            continue
        try:
            if os.getsid(pid) in retained:
                continue
        except ProcessLookupError:
            continue
        pids.append(pid)
    if pids:
        raise RenameRefused(f"processes {', '.join(map(str, pids))} work inside {rename.worktree()}; stop them first")


def _check_checkout(rename: UnitRename, *, resume_doc: bool = False) -> None:
    # Untracked files are left alone: the doc commit stages only tracked changes, and the checkout
    # keeps untracked files of its own, such as the production log, which `!docs/**` keeps visible.
    checkout = Path(rename.checkout)
    if git(checkout, "branch", "--show-current") == rename.merge_branch:
        if not git(checkout, "status", "--porcelain", "--untracked-files=no"):
            return
        if resume_doc and not rename.doc_commit and _own_doc_edits(rename):
            return
    raise RenameRefused(f"showrunner checkout must be clean and on {rename.merge_branch}")


def _own_doc_edits(rename: UnitRename) -> bool:
    """Accept only complete, deterministic writes of the pending doc step.

    The crash may follow any one write. Unwritten files remain at HEAD; changed
    files must exactly equal the rename applied to HEAD, including whitespace.
    """
    checkout, doc = Path(rename.checkout), Path(rename.doc)
    allowed = {str(path.relative_to(checkout)): path for path in (doc, *_plans(rename))
               if path.is_relative_to(checkout)}
    changed = set(git(checkout, "diff", "--name-only", "-z", "HEAD").split("\0")) - {""}
    if not changed or changed - allowed.keys():
        return False
    if git(checkout, "diff", "--summary", "HEAD") or git(checkout, "diff", "--cached", "--summary"):
        # Atomic text writes cannot add/delete paths or change their Git modes.
        return False
    staged = set(git(checkout, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
    if staged - changed:
        return False
    for relative in changed:
        path = allowed[relative]
        original = subprocess.run(["git", "-C", str(checkout), "show", f"HEAD:{relative}"],
                                  capture_output=True, text=True, check=False)
        if original.returncode or not path.is_file():
            return False
        expected = _doc_text(rename, original.stdout) if path == doc else _plan_text(rename, original.stdout)
        if path.read_bytes() != expected.encode("utf-8"):
            return False
        if relative in staged:
            indexed = subprocess.run(["git", "-C", str(checkout), "show", f":{relative}"],
                                     capture_output=True, check=False)
            if indexed.returncode or indexed.stdout != expected.encode("utf-8"):
                return False
    return True


def _preflight(rename: UnitRename) -> None:
    """The read-only refusals of a fresh rename; a resumed one accepts what its own steps made."""
    checkout = Path(rename.checkout)
    if rename.step == len(STEPS):
        return
    _check_checkout(rename)
    if rename.old_branch != rename.new_branch and (unit_move.branch_tip(checkout, rename.new_branch)
                                                  or unit_move.remote_tip(checkout, rename.new_branch)):
        raise RenameRefused(f"branch {rename.new_branch} already exists")
    if rename.old_worktree != rename.new_worktree and unit_move.exists(Path(rename.new_worktree)):
        raise RenameRefused(f"destination already exists: {rename.new_worktree}")
    if rename_state.unfinished(rename_state.production_journal(rename.slug)):
        raise RenameRefused(f"{rename.slug}: finish the production rename first")
    if not rename.id_matches and _is_style(rename):
        try:
            with style_ledger.preview_ledger(_style_repository(rename)) as ledger:
                _ = ledger.rename_unit(rename.old, rename.new)
        except style_ledger.LedgerError as error:
            raise RenameRefused(str(error)) from error
    # A unit mid-phase is renamed too: its commits move with the branch, and _merge leaves work in
    # progress alone (user, 2026-10-18).
    check_stoppable(rename)
    if not rename.id_matches:
        record = staff.snapshot()
        key = unit_names.name_key(unit_names.name(rename.new))
        if key != unit_names.name_key(unit_names.name(rename.old)) and any(
                unit_names.name_key(row.name) == key for row in record.find(kind="unit")):
            raise staff.StaffRefused(f"unit name already exists: {key}")


def _token(old: str, new: str, text: str) -> str:
    return re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])", new, text)


def _doc_text(rename: UnitRename, text: str) -> str:
    """The Units row's names, and the unit id in Hub files unit cells and open Gates unit cells."""
    lines = text.splitlines(keepends=True)
    section = ""
    headings: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("## "):
            section, headings = line.strip(), []
            continue
        if not line.startswith("|"):
            continue
        cells = production_units.row_cells(line)
        if not headings:
            headings = cells
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        row = dict(zip(headings, cells, strict=False))
        if section == "## Units" and production_units.cell_value(row.get("Unit", "")) == rename.old:
            values = {"Unit": rename.new, "Worktree": rename.new_worktree, "Branch": rename.new_branch}
            values = {column: f"`{value}`" if "`" in row[column] else value for column, value in values.items()}
        elif section in EDITED_COLUMNS and not (section == "## Gates" and
                                                row.get("Clears when", "").lower().startswith("cleared")):
            values = {column: _token(rename.old, rename.new, row[column])
                      for column in EDITED_COLUMNS[section] if column in row}
        else:
            continue
        for column, value in values.items():
            cells[headings.index(column)] = value
        if cells != production_units.row_cells(line):
            lines[index] = "| " + " | ".join(cells) + " |\n"
    return "".join(lines)


def _is_style(rename: UnitRename) -> bool:
    try:
        return production_units.production_field(Path(rename.doc).read_text(encoding="utf-8").splitlines(), "Kind") == "style"
    except production_units.Refusal:
        return False


def _style_repository(rename: UnitRename) -> Path:
    repository = production_units.production_field(Path(rename.doc).read_text(encoding="utf-8").splitlines(), "Repository")
    if not repository:
        raise RenameRefused(f"{rename.doc}: style production lacks Repository")
    return Path(repository).expanduser()


def _plans(rename: UnitRename) -> list[Path]:
    checkout = Path(rename.checkout)
    row = next(row for row in _rows(Path(rename.doc)) if production_units.cell_value(row.get("Unit", "")) in
               (rename.old, rename.new))
    cell = row.get("Plan", "")
    if production_units.plan_cell_is_retired(cell):
        return []
    if not _is_style(rename):
        return list(unit_move.plan_paths(cell, checkout))
    relative = Path(production_units.cell_value(cell))
    if relative.parts[:3] != ("docs", "style-production", "units") or ".." in relative.parts or relative.suffix != ".md":
        raise RenameRefused(f"{rename.doc}: invalid style Plan cell: {cell}")
    return [path for directory in dict.fromkeys((checkout, rename.worktree()))
            if (path := directory / relative).is_file()]


def _plan_text(rename: UnitRename, text: str) -> str:
    """The unit id in the plan's `> **Production:**` header; the plan's prose is history."""
    return "".join(_token(rename.old, rename.new, line) if line.startswith("> **Production") else line
                   for line in text.splitlines(keepends=True))


def _subject(directory: Path, ref: str) -> str:
    return git(directory, "show", "-s", "--format=%s", ref)


def _in_progress(worktree: Path) -> str:
    """Why the unit's work is in progress, or empty: a merge into it could lose uncommitted work."""
    run = unit_move.open_run(worktree)
    phase = unit_move.open_phase(run) if run else ""
    if phase:
        return f"phase {phase} is open"
    return "the worktree has uncommitted work" if git(worktree, "status", "--porcelain") else ""


def _merge(rename: UnitRename, _save: Callable[[], None]) -> None:
    worktree, checkout = Path(rename.old_worktree), Path(rename.checkout)
    if not rename.merged:
        rename.kept = _in_progress(worktree)
    if rename.kept:
        # Work in progress keeps its tip: the unit takes the merge branch and the rename commit
        # at its own next merge.
        unit_move.check_tip(checkout, rename.old_branch, rename.tip)
        rename.merged = rename.tip
        return
    _ = git(checkout, "fetch", "origin", rename.merge_branch)
    message = f"merge({rename.old_branch}): origin/{rename.merge_branch} before renaming {rename.old} to {rename.new}"
    head = git(worktree, "rev-parse", "HEAD")
    # A merge made before the journal recorded it is adopted, not made again.
    if head != rename.tip and not (git(worktree, "rev-parse", "HEAD^1") == rename.tip
                                   and _subject(worktree, "HEAD") == message):
        raise RenameRefused(f"{rename.old_branch} moved to {head[:12]} during the rename")
    rename.merged = unit_move.merge_into(worktree, f"origin/{rename.merge_branch}", message)


def _move_git(rename: UnitRename, _save: Callable[[], None]) -> None:
    checkout = Path(rename.checkout)
    if rename.old_branch != rename.new_branch:
        unit_move.push_branch(checkout, rename.old_branch, rename.new_branch, rename.merged)
    if rename.old_worktree != rename.new_worktree:
        Path(rename.new_worktree).parent.mkdir(parents=True, exist_ok=True)
        unit_move.move_worktree(checkout, rename.old_branch, rename.new_branch, Path(rename.old_worktree),
                                Path(rename.new_worktree), rename.merged)
    elif rename.old_branch != rename.new_branch:
        _ = git(Path(rename.new_worktree), "switch", rename.new_branch)


def _move_claude(rename: UnitRename, _save: Callable[[], None]) -> None:
    if rename.old_worktree != rename.new_worktree:
        unit_move.move_configs(rename.old_worktree, rename.new_worktree)


def _commit_doc(rename: UnitRename, save: Callable[[], None]) -> None:
    checkout, doc = Path(rename.checkout), Path(rename.doc)
    if not rename.doc_commit:
        if _subject(checkout, "HEAD") == rename.message:
            # Committed before the journal recorded the hash.
            rename.doc_commit = git(checkout, "rev-parse", "HEAD")
        else:
            plans = _plans(rename)
            write_text(doc, _doc_text(rename, doc.read_text(encoding="utf-8")))
            for path in plans:
                text = path.read_text(encoding="utf-8")
                if (updated := _plan_text(rename, text)) != text:
                    write_text(path, updated)
            changed = set(git(checkout, "diff", "--name-only", "-z", "HEAD").split("\0")) - {""}
            allowed = {str(path.relative_to(checkout)) for path in (doc, *plans) if path.is_relative_to(checkout)}
            if changed - allowed:
                raise RenameRefused("unrelated checkout changes during unit rename")
            if changed:
                _ = git(checkout, "add", "--", *sorted(changed))
            _ = git(checkout, "commit", *(["--allow-empty"] if _is_style(rename) else []), "-m", rename.message)
            rename.doc_commit = git(checkout, "rev-parse", "HEAD")
        save()
    # Recovery retries only the push, of the commit the journal names.
    _ = git(checkout, "merge-base", "--is-ancestor", rename.doc_commit, rename.merge_branch)
    _ = git(checkout, "push", "origin", f"{rename.merge_branch}:refs/heads/{rename.merge_branch}")


def _merge_rename(rename: UnitRename, save: Callable[[], None]) -> None:
    """The unit branch takes the rename commit, so the unit's next checkpoint carries the new names."""
    worktree = Path(rename.new_worktree)
    if not rename.unit_tip and rename.kept:
        rename.unit_tip = rename.merged
        save()
    if not rename.unit_tip:
        message = f"merge({rename.new_branch}): {rename.merge_branch} at {rename.doc_commit[:8]} — {rename.message}"
        rename.unit_tip = unit_move.merge_into(worktree, rename.doc_commit, message)
        save()
    unit_move.check_tip(Path(rename.checkout), rename.new_branch, rename.unit_tip)
    _ = git(worktree, "push", "origin", f"{rename.new_branch}:refs/heads/{rename.new_branch}")


def _move_state(rename: UnitRename, _save: Callable[[], None]) -> None:
    from scripts.astra import design_roles
    from scripts.direct import workers
    workers.rename_unit(rename.slug, rename.old, rename.new,
                        runs=tuple(set((*retire_unit.runs_of(Path(rename.old_worktree)),
                                        *retire_unit.runs_of(Path(rename.new_worktree))))))
    try:
        design_roles.rename_holder(rename.slug, rename.old, rename.new, rename.old_worktree, rename.new_worktree)
    except (OSError, ValueError, SystemExit) as error:
        raise RenameRefused(str(error)) from error
    if not rename.id_matches and _is_style(rename):
        try:
            with style_ledger.open_ledger(_style_repository(rename)) as ledger:
                _ = ledger.rename_unit(rename.old, rename.new)
        except style_ledger.LedgerError as error:
            raise RenameRefused(str(error)) from error
    # Free text in the live lists names the branch and the worktree too.
    paths = ((rename.old_branch, rename.new_branch), (rename.old_worktree, rename.new_worktree))
    if rename.id_matches:
        _ = rename_state.reword_showrunner_lists(Path(rename.scratch) / "showrunner_state.json", paths)
    else:
        _ = rename_state.rename_scratch(rename.old, rename.new, Path(rename.scratch), paths)
    _ = rename_state.rename_outstanding(rename.slug, ((rename.old, rename.new), *paths))
    # Other productions' showrunners, such as the parent, can have the unit in their lists too.
    reworded, _ = _other_showrunner_states(rename)
    for path in reworded:
        _ = rename_state.reword_showrunner_lists(path, ((rename.old, rename.new), *paths))
        _ = rename_state.rename_outstanding(path.parent.parent.name, ((rename.old, rename.new), *paths))
    if not rename.id_matches:
        unit_move.move(PathMove(stall_watch.stretch_path(rename.slug, rename.old),
                                stall_watch.stretch_path(rename.slug, rename.new)))
        try:
            _ = review_pool.rename_unit(rename.slug, rename.old, rename.new)
        except review_pool.PoolError as error:
            raise RenameRefused(str(error)) from error
    old, new = Path(rename.old_worktree), Path(rename.new_worktree)
    run = unit_move.newest_run(old) or unit_move.newest_run(new)
    if run is not None:
        unit_move.move_run(run, old, new, rename.new_branch)
    if not rename.id_matches:
        today = datetime.now(user_zone()).date()
        for issue in _unit_proposals(rename, rename.old):
            _ = proposal_issue.edit(issue.path, {"unit": rename.new}, today)
        identity = phase_table.production_identity(read_production(Path(rename.doc)))
        _ = phase_table.rename_unit(identity, rename.old, rename.new)


def _other_showrunner_states(rename: UnitRename) -> tuple[list[Path], list[Path]]:
    """Each other production's showrunner_state.json: those whose lists take the new names, and those
    kept because their own production has a unit of the old id, or none can be read, so the old id
    in their text can name another unit."""
    docs = showrunners.timer_docs()
    reworded: list[Path] = []
    kept: list[Path] = []
    for path in sorted(showrunners.SHOWRUNNER_STATE_DIR.glob("*/session/showrunner_state.json")):
        slug = path.parent.parent.name
        if slug != rename.slug:
            (kept if _may_hold_unit(docs.get(slug), rename.old) else reworded).append(path)
    return reworded, kept


def _may_hold_unit(doc: Path | None, unit: str) -> bool:
    try:
        cells = production_units.unit_table(doc.read_text(encoding="utf-8").splitlines()) if doc else None
    except (OSError, production_units.Refusal):
        cells = None
    return cells is None or any(production_units.cell_value(row.get("Unit", "")) == unit for row in cells)


def _retire(rename: UnitRename, _save: Callable[[], None]) -> None:
    checkout = Path(rename.checkout)
    if unit_move.remote_tip(checkout, rename.new_branch) != rename.unit_tip:
        raise RenameRefused(f"origin {rename.new_branch} is not at {rename.unit_tip[:12]}; old branch kept")
    if rename.old_branch != rename.new_branch:
        unit_move.delete_branch(checkout, rename.old_branch, rename.merged)


def run_shutdown(*arguments: str) -> None:
    """Run one shutdown verb on this machine. Without a session id in its environment it finds the
    account that holds the session, not the showrunner's own."""
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    result = subprocess.run([sys.executable, str(SHUTDOWN), *arguments, "--here"], env=environment,
                            pass_fds=staff.operation_fds(),
                            capture_output=True, text=True, check=False, timeout=900)
    if result.returncode:
        raise RenameRefused(f"shutdown {arguments[0]}: {(result.stderr or result.stdout).strip()}")


def _unit_records(rename: StopTarget) -> list[SessionShutdown]:
    """The journaled director remains the same when its staff name or parent moves."""
    return shutdown_records(rename.slug, (rename.old, rename.new), session_id=rename.session_id)


def shutdown_records(slug: str, units: tuple[str, ...], *, session_id: str = "") -> list[SessionShutdown]:
    try:
        saved = shutdown_record.all_session_records()
    except (shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"shutdown records unreadable: {error}") from error
    found: list[SessionShutdown] = []
    for item in saved:
        host = item["session"]["host"]
        if host["kind"] != "unit" or host["unit"] not in units:
            continue
        recorded_id = item["session"]["session_id"]
        if (recorded_id == session_id if session_id else
                staff_sources.unit_parent(host["unit"], recorded_id).slug == slug):
            found.append(item)
    return found


def _bind_live_pane(rename: StopTarget, marked: unit_lookup.MarkedUnit) -> None:
    """Pin the terminal at the director's exact place before stopping its agent."""
    try:
        panes = herdr.Herdr(marked.place.session).panes()
    except (herdr.HerdrFailure, OSError, ValueError) as error:
        raise RenameRefused(f"{rename.old}: cannot read its director pane: {error}") from error
    matches = [pane for pane in panes if pane["pane_id"] == marked.place.pane]
    if len(matches) != 1 or not (terminal_id := matches[0].get("terminal_id", "")):
        raise RenameRefused(f"{rename.old}: director pane has no unique terminal identity")
    if sum(pane.get("terminal_id") == terminal_id for pane in panes) != 1:
        raise RenameRefused(f"{rename.old}: director terminal identity is ambiguous")
    if matches[0]["session_id"] not in (None, rename.session_id):
        raise RenameRefused(f"{rename.old}: director pane conversation does not match")
    rename.pane_session, rename.pane_id = marked.place.session, marked.place.pane
    rename.terminal_id = terminal_id
    rename.stop_identity = StopIdentityEvidence.RECORDED


def bind_stop_target(rename: StopTarget) -> None:
    """Journal the director before changing the staff name or parent used to find it."""
    records = _unit_records(rename)
    if len(records) > 1:
        raise RenameRefused(f"{rename.old} has {len(records)} shutdown records; settle them first")
    marked = unit_lookup.marked_unit(rename.slug, rename.old)
    if (records and marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude)
            and records[0]["session"]["session_id"] != marked.claude.session_id):
        raise RenameRefused(f"{rename.old}: director does not match its shutdown record")
    conversation = unit_move.unit_conversation(marked, session_id=rename.session_id)
    if isinstance(conversation, unit_move.UnitConversation):
        rename.account, rename.session_id = conversation.account, conversation.session_id
    elif records:
        rename.account, rename.session_id = records[0]["label"], records[0]["session"]["session_id"]
    elif marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
        raise RenameRefused(f"{rename.old}: session {marked.claude.session_id} has no pane launch account")
    elif rename.session_id and not rename.account:
        raise RenameRefused(f"{rename.old}: session {rename.session_id} has no pane launch account or shutdown record")
    if marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
        _bind_live_pane(rename, marked)


def _stopped_pane(rename: StopTarget) -> unit_move.UnitPane | None:
    """Recover the exact journaled terminal, even after its agent metadata clears."""
    if not rename.pane_session or not rename.pane_id or not rename.session_id or not rename.terminal_id:
        raise RenameRefused(f"{rename.old}: no journaled terminal identity for pane recovery")
    client = herdr.Herdr(rename.pane_session)
    try:
        panes = client.panes()
    except (herdr.HerdrFailure, OSError, ValueError) as error:
        raise RenameRefused(f"{rename.old}: cannot read its journaled pane: {error}") from error
    if any(not pane.get("terminal_id") for pane in panes):
        raise RenameRefused(f"{rename.old}: pane list has incomplete terminal identity")
    matches = [pane for pane in panes if pane.get("terminal_id") == rename.terminal_id]
    if any(pane["session_id"] == rename.session_id and pane.get("terminal_id") != rename.terminal_id
           for pane in panes):
        raise RenameRefused(f"{rename.old}: journaled conversation is on another terminal")
    if not matches:
        return None  # A completed close is safe to replay.
    if len(matches) != 1:
        raise RenameRefused(f"{rename.old}: journaled terminal identity is ambiguous")
    if matches[0]["pane_id"] != rename.pane_id:
        raise RenameRefused(f"{rename.old}: journaled terminal moved to pane {matches[0]['pane_id']}")
    if matches[0]["session_id"] not in (None, rename.session_id):
        raise RenameRefused(f"{rename.old}: journaled terminal holds another conversation")
    try:
        activity = client.pane_activity(rename.pane_id)
    except (herdr.HerdrFailure, OSError, ValueError) as error:
        raise RenameRefused(f"{rename.old}: cannot read journaled pane activity: {error}") from error
    if not isinstance(activity, herdr.PaneAtShell):
        raise RenameRefused(f"{rename.old}: journaled pane has foreground work")
    found = unit_lookup.MarkedUnit(rename.old, unit_lookup.ClaudeNotRunning(),
                                  herdr.HerdrPlace(rename.pane_session, rename.pane_id))
    return unit_move.unit_pane(found, rename.slug)


def stop_unit(rename: StopTarget, save: Callable[[], None]) -> None:
    """Stop the unit Claude through the shared API before moving its files.

    A live process cannot change its env mark and the director's context holds the old paths, so a
    rename of a running unit is always a relaunch.
    """
    # A resumed step rechecks whether its exact pane can be safely closed.
    # Other processes are checked after the stop has settled its own run work.
    _refuse_detached_worktree(rename)
    try:
        found = unit_lookup.marked_unit(rename.slug, rename.old)
        if rename.session_id and (found is None or isinstance(found.claude, unit_lookup.LiveClaude)
                                  and found.claude.session_id != rename.session_id):
            found = next((marked for _, marked in unit_lookup.observed_unit_marks()
                          if isinstance(marked.claude, unit_lookup.LiveClaude)
                          and marked.claude.session_id == rename.session_id), None)
    except OSError as error:
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {error}") from error
    live = found is not None and isinstance(found.claude, unit_lookup.LiveClaude)
    if rename.stop_identity is StopIdentityEvidence.LEGACY_UNKNOWN and not live:
        raise RenameRefused(f"{rename.old}: journal has no terminal identity; settle the pane by hand")
    if (not rename.pane_id or rename.stop_identity is StopIdentityEvidence.LEGACY_UNKNOWN) and found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
        rename.session_id = found.claude.session_id
        _bind_live_pane(rename, found)
        save()
    if rename.pane_id and not rename.terminal_id:
        raise RenameRefused(f"{rename.old}: no journaled terminal identity for pane recovery")
    recovering_pane = bool(rename.pane_id) and (found is None or not isinstance(found.claude, unit_lookup.LiveClaude))
    pane = None if recovering_pane else unit_move.unit_pane(found, rename.slug)
    from scripts.shutdown import stop_resume
    records = _unit_records(rename)
    if len(records) > 1:
        raise RenameRefused(f"{rename.old} has {len(records)} shutdown records; settle them first")
    identity = found.claude.session_id if found is not None and isinstance(found.claude, unit_lookup.LiveClaude) else rename.session_id
    if found is not None and isinstance(found.claude, unit_lookup.ClaudeUnknown):
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {found.claude.reason}")
    if not identity and len(records) == 1:
        identity = records[0]["session"]["session_id"]
    if identity and found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
        result = stop_resume.stop(identity)
        if not isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
            reason = result.reason if isinstance(result, stop_resume.StopFailed) else "no local session tab"
            raise RenameRefused(f"{rename.old}: stop refused: {reason}")
        rename.session_id = identity
        if records:
            for saved in records:
                _ = shutdown_record.update_session(saved["login"], identity,
                    lambda current: current.update(progress=shutdown_record.SessionStopped(
                        kind="stopped", at=datetime.now(UTC).isoformat(timespec="seconds"))))
    # Only the deleted conductor wrote stop issues; name the one command that settles its record.
    for saved in records:
        if saved["stop_issues"]:
            settle = shlex.join(["python3", "scripts/shutdown/shutdown.py", "forget",
                                 saved["session"]["session_id"], "--account", saved["label"]])
            raise RenameRefused(f"{rename.old}: an old stop record has unresolved cleanup issues; settle it: "
                                + f"cd ~/.claude && {settle}")
    _stop_runs(rename)
    if rename.pane_id and (recovering_pane or pane is not None):
        pane = _stopped_pane(rename)
    unit_move.close_unit_pane(pane)


def _refuse_detached_worktree(rename: StopTarget) -> None:
    if not isinstance(rename, UnitRename):
        return
    from scripts.direct import workers
    worktree = rename.worktree().resolve()
    for folder in retire_unit.runs_of(rename.worktree()):
        for path, record in workers.retained_detached_runs(folder):
            cwd = Path(str(record.get("cwd", ""))).resolve()
            if cwd == worktree or worktree in cwd.parents:
                raise RenameRefused(f"detached run {path.stem} of {record.get('worker', '')} works inside "
                                    + f"{worktree}; rerun apply once it ends")


def _retained_command_sessions(worktree: Path) -> set[int]:
    from scripts.direct import workers
    protected: set[int] = set()
    for folder in retire_unit.runs_of(worktree):
        for _, record in workers.retained_detached_runs(folder):
            pid = record.get("pid")
            if isinstance(pid, int):
                try:
                    protected.add(os.getsid(pid))
                except ProcessLookupError:
                    pass
    return protected


def _stop_runs(rename: StopTarget) -> None:
    """Pause unit workers and clean up processes outside their retained work."""
    from scripts.direct import workers
    worktree = rename.worktree()
    folders = retire_unit.runs_of(worktree)
    _refuse_detached_worktree(rename)
    servers = {item.run: item for item in retire_unit.mesh_runs(folders)}
    for folder in folders:
        if folder not in servers and not (folder / "mesh_roster.json").exists():
            continue
        result = retire_unit.run([sys.executable, str(retire_unit.MESH), "stop", "--pause", "--session-dir", str(folder)])
        if result.returncode:
            if workers.ledger_for_run(folder).path.exists():
                raise RenameRefused(f"cannot pause run {folder}: {retire_unit.failure(result)}")
            print(f"{retire_unit.failure(result)}; SIGTERM instead")
        elif folder in servers:
            print(f"stopped: app-server pid {servers[folder].pid} of run {folder.name}")
    workers.pause_unit(rename.slug, rename.old)
    protected = _retained_command_sessions(worktree)
    inside = unit_move.processes_inside(worktree) if unit_move.exists(worktree) else []
    runs = retire_unit.run_processes(worktree, inside) if inside else {}
    for pid in list(runs):
        try:
            if os.getsid(pid) in protected:
                del runs[pid]
        except ProcessLookupError:
            del runs[pid]
    left = retire_unit.terminate(sorted(runs), runs, RUN_STOP_WAIT_SECONDS)
    if left:
        raise RenameRefused("; ".join(left))




def _moved(path: str, old: str, new: str) -> str:
    return new + path[len(old):] if path == old or path.startswith(old + "/") else path


def renamed_note(rename: UnitRename) -> str:
    plans = ", ".join(str(path).replace(rename.checkout, rename.new_worktree, 1) for path in _plans(rename))
    return (f"While you were stopped the showrunner renamed you: you are now {rename.new}, in worktree "
            + f"{rename.new_worktree} on branch {rename.new_branch}"
            + (f", and your plan is at {plans}" if plans else "") + ". Use these names from now on."
            + (f" Your work was in progress ({rename.kept}), so your branch kept its commits and took no merge: "
               + f"the rename commit comes with your next merge of {rename.merge_branch}. Your paused Codex workers "
               + f"now work in {rename.new_worktree}; say so in your next message to each." if rename.kept else ""))


def seat_note(rename: UnitRename) -> str:
    """The line a paused Codex worker reads in its resume prompt when its worktree moved."""
    return (f"While you were paused the showrunner renamed your unit: your worktree is now {rename.new_worktree} "
            + f"on branch {rename.new_branch}. {rename.old_worktree} no longer exists; wherever your instructions "
            + f"name it, use {rename.new_worktree}.")


def repoint_shutdown_worktree(saved: SessionShutdown, old: str, new: str,
                              host_names: dict[str, str], note: str) -> None:
    """Move restore paths and worker launchers without changing the unit's identity."""
    session = saved["session"]
    host = session["host"]
    if host["kind"] != "unit":
        raise RenameRefused("shutdown record is not a unit's")
    if saved["progress"]["kind"] not in STOPPED:
        raise RenameRefused(f"shutdown record reads {saved['progress']['kind']}; settle it first")
    host["session_name"] = host_names.get(host["session_name"], host["session_name"])
    if host["plan"]["kind"] == "plan":
        host["plan"]["path"] = _moved(host["plan"]["path"], old, new)
    session["cwd"] = _moved(session["cwd"], old, new)
    for worker in saved["workers"]:
        worker["cwd"] = _moved(worker["cwd"], old, new)
    if old == new:
        return
    for worker in saved.get("codex_seats", []):
        worker["launcher_command"] = [_moved(argument, old, new) for argument in worker["launcher_command"]]
        prompt = Path(worker["run_dir"]) / f"{worker['seat']}.resume-prompt.md"
        text = prompt.read_text(encoding="utf-8") if prompt.exists() else ""
        if note not in text:
            _ = prompt.write_text(f"{text}{note}\n", encoding="utf-8")


def _host_names(rename: UnitRename) -> dict[str, str]:
    """Each herdr session name made from the old stem or the old session name, to its new form."""
    import herdr

    new_stem = unit_names.stem(rename.new)
    olds = {unit_names.stem(rename.old), rename.old_branch,
            *([rename.old_session] if rename.old_session else [])}
    return {herdr.generated_session_name(old_form): herdr.generated_session_name(new_stem)
            for old in olds for old_form in (old, f"{rename.slug}-{old}")}


def _restart_record(rename: UnitRename, save: Callable[[], None]) -> None:
    """Point the stopped unit's shutdown record at its new names; restart reads the plan from the Units row.

    The Claude session takes the new stem whatever it was called before, and a herdr session name
    made from the old stem or the old session name takes the same form of the new stem (user,
    2026-10-19: builder-report kept its name when build-report-unit became builds-unit).
    """
    if not rename.session_id:
        return
    records = _unit_records(rename)
    if not records:
        return
    new_stem = unit_names.stem(rename.new)

    def change(saved: SessionShutdown) -> None:
        session = saved["session"]
        host = session["host"]
        if not rename.old_session:
            rename.old_session = session["name"]
        host_names = _host_names(rename)
        if host["kind"] != "unit" or host["unit"] not in (rename.old, rename.new):
            raise RenameRefused(f"shutdown record {rename.session_id} is not {rename.old}'s")
        if saved["progress"]["kind"] not in STOPPED:
            raise RenameRefused(f"{rename.old}'s shutdown record reads {saved['progress']['kind']}; settle it first")
        host["unit"] = rename.new
        repoint_shutdown_worktree(saved, rename.old_worktree, rename.new_worktree, host_names, seat_note(rename))
        session["name"] = new_stem
        saved["renamed"] = renamed_note(rename)

    try:
        _ = shutdown_record.update_session(records[0]["login"], rename.session_id, change)
    except (shutdown_record.NoLiveRecord, shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"{rename.old}'s shutdown record: {error}") from error
    save()


def _old_tab_titles(rename: UnitRename) -> frozenset[str]:
    """The unit's former display names, excluding its destination title."""
    return frozenset({unit_names.stem(rename.old), rename.old, rename.old_branch, rename.old_session}
                     - {"", unit_names.stem(rename.new)})


def _rename_saved_layout(rename: UnitRename) -> None:
    import herdr

    # Replace the saved title before the launcher orders and records its new tab.
    # merged_layout otherwise retains the absent old title for another 14 days.
    herdr.rename_saved_tab(herdr.shared_session(), showrunners.herdr_space(rename.slug, rename.doc),
                           _old_tab_titles(rename), unit_names.stem(rename.new))


def _relaunch(rename: UnitRename, _save: Callable[[], None]) -> None:
    _rename_saved_layout(rename)
    if rename.session_id:
        # Restart also resumes its own partial worker/timer restores on a retry.
        if _unit_records(rename):
            run_shutdown("restart", "--only", rename.session_id)
        else:
            rows = production_units.unit_table(Path(rename.doc).read_text(encoding="utf-8").splitlines())
            row = next((row for row in rows if production_units.cell_value(row["Unit"]) == rename.new), None)
            if row is None or not (plan := production_units.cell_value(row.get("Plan", ""))):
                raise RenameRefused(f"{rename.new}: no Plan in {rename.doc}")
            result = subprocess.run([sys.executable, str(Path(add_unit.__file__)),
                "--production", rename.doc, rename.new, *production_units.plan_arguments(plan),
                "--resume", rename.session_id, "--cwd", rename.new_worktree,
                *(["--account", rename.account] if rename.account else [])],
                pass_fds=staff.operation_fds(),
                capture_output=True, text=True, check=False)
            if result.returncode:
                raise RenameRefused((result.stderr or result.stdout).strip())
    # Keep the source launch until a relaunch succeeds, then remove its old names.
    # A stopped unit with no conversation also leaves no restorable old pane.
    if rename.old != rename.new:
        _ = pane_record.forget_unit(rename.old)
    if rename.session_id:
        _await_live(rename)


def _await_live(rename: UnitRename) -> None:
    """Wait for the relaunched unit to carry a live Claude mark, so verify does not run before it is up.
    Past LIVE_WAIT_SECONDS verify reports it as not running."""
    deadline = time.monotonic() + LIVE_WAIT_SECONDS
    while True:
        found = unit_lookup.marked_units(rename.slug).get(rename.new)
        if (found is not None and isinstance(found.claude, unit_lookup.LiveClaude)) or time.monotonic() >= deadline:
            return
        time.sleep(POLL_SECONDS)


def _unit_proposals(rename: UnitRename, unit: str) -> list[issues.Issue]:
    """Active issues naming `unit`, matched by proposer for style and by plan for other productions."""
    folder = proposals_folder.issues_folder(Path(rename.doc))
    if folder is None:
        return []
    style = _is_style(rename)
    owners = proposals_folder.owners(Path(rename.doc)) if style else frozenset[str]()
    plans = [] if style else [str(path.relative_to(Path(rename.checkout))) for path in _plans(rename)]
    # The issues folder is shared by every production; unit ids are only unique within one, so an
    # issue must name an owning proposer for style, or one of this unit's plans otherwise.
    return [issue for issue in issues.open_issues(folder, proposals_folder.PROPOSAL_TAG)
            if issue.value("unit") == unit and (proposals_folder.proposer(issue) in owners if style else any(
                plan in (issue.value("plan") or "") for plan in plans))]


def _git_failures(rename: UnitRename) -> list[str]:
    checkout, old, new = Path(rename.checkout), Path(rename.old_worktree), Path(rename.new_worktree)
    failures: list[str] = []
    if rename.old_branch != rename.new_branch and (unit_move.branch_tip(checkout, rename.old_branch)
                                                  or unit_move.remote_tip(checkout, rename.old_branch)):
        failures.append(f"git: branch {rename.old_branch} still exists")
    if rename.moves_git and unit_move.remote_tip(checkout, rename.new_branch) != unit_move.branch_tip(checkout, rename.new_branch):
        failures.append(f"git: origin {rename.new_branch} is not at the local tip")
    if old != new and (unit_move.exists(old) or f"worktree {old}" in git(
            checkout, "worktree", "list", "--porcelain").splitlines()):
        failures.append(f"git: worktree {old} still exists")
    if not (new / ".git").is_file() or git(new, "branch", "--show-current") != rename.new_branch:
        failures.append(f"git: {new} is not a worktree on {rename.new_branch}")
    return failures


def _process_failures(rename: UnitRename) -> list[str]:
    """No process has its working directory at the old path. Every stopped step refuses while a
    process works inside the worktree, so none moved with it; this finds one started there since,
    or one left in a removed folder."""
    if rename.old_worktree == rename.new_worktree:
        return []
    old = Path(rename.old_worktree)
    pids = unit_move.processes_inside(old)
    return [f"processes {', '.join(map(str, pids))} still work in {old}"] if pids else []


def _doc_failures(rename: UnitRename) -> list[str]:
    doc = Path(rename.doc)
    text = doc.read_text(encoding="utf-8")
    rows = [row for row in _rows(doc) if production_units.cell_value(row.get("Unit", "")) in (rename.old, rename.new)]
    cells = [{column: production_units.cell_value(row.get(column, "")) for column in ("Unit", "Worktree", "Branch")}
             for row in rows]
    failures: list[str] = []
    if cells != [{"Unit": rename.new, "Worktree": rename.new_worktree, "Branch": rename.new_branch}]:
        failures.append(f"{doc}: the Units row does not name {rename.new}, {rename.new_worktree} and {rename.new_branch}")
    # apply's own edit would change nothing: no Hub files or open Gates unit cell names the old id.
    if _doc_text(rename, text) != text:
        failures.append(f"{doc}: a Hub files or open Gates unit cell still names {rename.old}")
    for path in _plans(rename):
        plan = path.read_text(encoding="utf-8")
        if _plan_text(rename, plan) != plan:
            failures.append(f"{path}: its Production header still names {rename.old}")
    return failures


def _state_failures(rename: UnitRename) -> list[str]:
    old, new = Path(rename.old_worktree), Path(rename.new_worktree)
    failures = [f"{directory / 'projects'}: transcripts still under {old}" for directory in unit_move.config_dirs()
                if unit_move.project_folder(str(old)) != unit_move.project_folder(str(new))
                and (directory / "projects" / unit_move.project_folder(str(old))).exists()]
    if not rename.id_matches and stall_watch.stretch_path(rename.slug, rename.old).exists():
        failures.append(f"stall-watch: the stretch is still under {rename.old}")
    # Step 7 moves the newest run, the one run_state reads; older runs under the old path are history.
    if unit_move.newest_run(old) is not None and unit_move.newest_run(new) is None:
        failures.append(f"run records: the newest run still names {old}")
    if not rename.id_matches:
        failures.extend(f"{issue.path}: proposal still names {rename.old}" for issue in _unit_proposals(rename, rename.old))
    if not rename.id_matches and _is_style(rename):
        with style_ledger.preview_ledger(_style_repository(rename)) as ledger:
            if ledger.ticket_open(rename.old) or ledger.queued_tickets(rename.old):
                failures.append(f"style ledger: current tickets still name {rename.old}")
            if any(wait.unit == rename.old for wait in ledger.sync_waits()):
                failures.append(f"style ledger: sync wait still names {rename.old}")
    identity = phase_table.production_identity(read_production(Path(rename.doc)))
    if not rename.id_matches:
        failures.extend(f"{note}: phase note still names {rename.old}"
                        for note in phase_table.owned_notes(identity, rename.old))
    from scripts.astra import design_roles
    if not rename.id_matches:
        failures.extend(design_roles.rename_failures(rename.slug, rename.old, rename.old_worktree))
    return failures


def _tab_failures(rename: UnitRename) -> list[str]:
    """The unit's own tab label agrees with its short name, including a same-name repair."""
    import herdr

    found = unit_lookup.marked_unit(rename.slug, rename.new)
    expected = unit_names.stem(rename.new)
    if found is None:
        return [f"{rename.new}'s recorded host is named {host['session_name']}, not {expected}"
                for saved in shutdown_records(rename.slug, (rename.new,))
                if (host := saved["session"]["host"])["kind"] == "unit" and host["session_name"] != expected]
    try:
        panes = herdr.Herdr(found.place.session).panes()
    except (herdr.HerdrFailure, OSError) as error:
        return [f"herdr {found.place.session}: pane labels could not be read: {error}"]
    return [f"{rename.new}'s tab is labelled {pane.get('label')}, not {expected}"
            for pane in panes if pane["pane_id"] == found.place.pane and pane.get("label") != expected]


def _session_failures(rename: UnitRename) -> list[str]:
    if not rename.session_id:
        return []
    failures: list[str] = []
    records = _unit_records(rename)
    saved = records[0] if records else None
    if saved is not None and saved["session"]["host"]["kind"] == "unit" and saved["session"]["host"]["unit"] != rename.new:
        failures.append(f"shutdown record {rename.session_id} does not name {rename.new}")
    marked = unit_lookup.marked_units(rename.slug)
    found = marked.get(rename.new)
    if found is None or not isinstance(found.claude, unit_lookup.LiveClaude):
        failures.append(f"{rename.new} is not running; relaunch it with shutdown.py restart --here --only {rename.session_id}")
    # When the id already matched, the mark is the new id too; only the session name changed.
    if not rename.id_matches:
        for slug, old in unit_lookup.observed_unit_marks():
            if (slug == rename.slug and old.unit == rename.old
                    and isinstance(old.claude, (unit_lookup.LiveClaude, unit_lookup.ClaudeUnknown))):
                failures.append(f"a session still carries the mark {rename.old}: "
                                + unit_lookup.holder_text(old))
    new_stem = unit_names.stem(rename.new)
    if found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
        if found.claude.name != new_stem:
            failures.append(f"{rename.new}'s session is named {found.claude.name}, not {new_stem}")
        failures.extend(_label_failures(rename, found.place.session))
    return failures


def _label_failures(rename: UnitRename, session: str) -> list[str]:
    """A herdr pane in the unit's herdr session still labelled with the old session name."""
    if not rename.old_session or rename.old_session == unit_names.stem(rename.new):
        return []
    import herdr

    try:
        panes = herdr.Herdr(session).panes()
    except (herdr.HerdrFailure, OSError) as error:
        return [f"herdr {session}: pane labels could not be read: {error}"]
    return [f"herdr {session}:{pane['pane_id']} is still labelled {rename.old_session}"
            for pane in panes if pane.get("label") == rename.old_session]


def _control_failures(rename: UnitRename) -> list[str]:
    """The matcher must still find the old id in history: finding nothing there means it is broken."""
    subjects = git(Path(rename.checkout), "log", "--format=%s", rename.merge_branch)
    if unit_rename_sweep.token_pattern((rename.old,)).search(subjects):
        return []
    return [f"control: no {rename.merge_branch} subject names {rename.old}; the matcher is broken"]


def _layout_failures(rename: UnitRename) -> list[str]:
    """Tab titles can be just the stem, which the general id/branch/path sweep cannot match."""
    import herdr

    session = herdr.shared_session()
    workspace = showrunners.herdr_space(rename.slug, rename.doc)
    old_titles = _old_tab_titles(rename)
    return [f"herdr {herdr.layout_path(session)}: {workspace} tab is still labelled {tab['name']}"
            for space in herdr.saved_layout(session)["workspaces"] if space["name"] == workspace
            for tab in space["tabs"] if tab["name"] in old_titles]


def _sweep_roots(rename: UnitRename) -> unit_rename_sweep.Roots:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    scratch = Path(rename.scratch)
    reworded, kept = _other_showrunner_states(rename)
    return unit_rename_sweep.Roots(
        state=state, production_state=rename_state.unit_journal(rename.slug).parent,
        configs=tuple(directory for directory in unit_move.config_dirs() if not directory.name.endswith(".lock")),
        checkouts=(Path(rename.checkout), Path(rename.new_worktree)),
        live_files=(*(scratch / name for name in rename_state.SCRATCH_FILES), *reworded),
        history_prefixes=(str(Path(rename.checkout) / "docs/plans" / f"{rename.slug}-log"), *map(str, kept)))


def verify_failures(rename: UnitRename) -> list[str]:
    """Every live place holds the new names, and no unclassified file holds an old one."""
    try:
        old_names = tuple(old for old, new in (
            (rename.old, rename.new), (rename.old_branch, rename.new_branch),
            (rename.old_worktree, rename.new_worktree)) if old != new)
        return [*_doc_failures(rename), *_git_failures(rename), *_process_failures(rename), *_state_failures(rename),
                *_session_failures(rename), *_tab_failures(rename), *_layout_failures(rename),
                *([] if rename.id_matches else _control_failures(rename)),
                *(unit_rename_sweep.sweep(old_names, _sweep_roots(rename)) if old_names else [])]
    except (shutdown_record.InvalidRecord, production_units.Refusal, style_ledger.LedgerError) as error:
        raise RenameRefused(f"verify could not read a place: {error}") from error


def _verify(rename: UnitRename, _save: Callable[[], None]) -> None:
    failures = verify_failures(rename)
    if failures:
        raise RenameRefused("verify failed:\n  " + "\n  ".join(failures))


class MergeLockScope(Enum):
    NONE = auto()
    CHECKOUT_CHECK = auto()
    STEP = auto()


class Step(NamedTuple):
    run: Callable[[UnitRename, Callable[[], None]], None]
    describe: Callable[[UnitRename], str]
    # Whether the unit must be stopped before the step runs: the stop step stops it, and verify
    # runs after the relaunch.
    stopped: bool = True
    # Checkout validation and git work wait for a gate before taking any staff operation.
    # Steps needing neither skip the gate and its potentially mid-merge checkout entirely.
    merge_lock: MergeLockScope = MergeLockScope.STEP
    source_writes: bool = False


def _describe_stop(r: UnitRename) -> str:
    return (f"pause {r.old} and its Codex workers through the shutdown path; its record carries the relaunch; "
            + "then stop its runs' app-servers and keep detached commands")


def _describe_merge(r: UnitRename) -> str:
    return (f"merge origin/{r.merge_branch} into {r.old_branch} in {r.old_worktree}, unless its work is in "
            + "progress (an open phase or uncommitted work): then the branch keeps its tip")


def _describe_git(r: UnitRename) -> str:
    branch = f"branch {r.old_branch} -> {r.new_branch}" if r.old_branch != r.new_branch else f"keep branch {r.old_branch}"
    worktree = (f"worktree {r.old_worktree} -> {r.new_worktree}" if r.old_worktree != r.new_worktree
                else f"keep worktree {r.old_worktree}")
    return f"git: {branch}; {worktree}"


def _describe_claude(_: UnitRename) -> str:
    return "Claude: transcript folders and trust entries follow the worktree"


def _describe_doc(r: UnitRename) -> str:
    if r.id_matches:
        row = next(row for row in _rows(Path(r.doc)) if production_units.cell_value(row.get("Unit", "")) == r.old)
        changes = [f"{column} {previous} -> {expected}" for column, expected in
                   (("Branch", r.new_branch), ("Worktree", r.new_worktree))
                   if (previous := production_units.cell_value(row.get(column, ""))) != expected]
        return f"{r.merge_branch}: commit Units row repairs: {'; '.join(changes)}"
    return f"{r.merge_branch}: commit {r.message!r} (Units row, Hub files and open Gates unit cells, plan headers)"


def _describe_merge_rename(r: UnitRename) -> str:
    return f"{r.new_branch}: merge the rename commit and push it, unless the merge step kept the tip"


def _describe_state(r: UnitRename) -> str:
    if r.id_matches:
        changes = [f"{old} -> {new}" for old, new in
                   ((r.old_branch, r.new_branch), (r.old_worktree, r.new_worktree)) if old != new]
        return f"state: update branch and worktree references: {'; '.join(changes)}"
    return ("state: report scratch, stall-watch stretch, reviewer leases, run record, open proposals, "
            + f"phase notes: {r.old} -> {r.new}")


def _describe_retire(r: UnitRename) -> str:
    return (f"retire {r.old_branch} once origin {r.new_branch} holds the unit's new tip"
            if r.old_branch != r.new_branch else f"keep branch {r.old_branch}")


def _describe_record(r: UnitRename) -> str:
    session = (f"Claude session {r.old_session} -> {unit_names.stem(r.new)}"
               if r.old_session and r.old_session != unit_names.stem(r.new)
               else "Claude session already matches" if r.old_session else "no Claude session to name")
    if r.id_matches:
        if not r.old_session and not shutdown_records(r.slug, (r.old,)):
            return "already matches: no shutdown record to update"
        paths = f"; paths {r.old_worktree} -> {r.new_worktree}" if r.old_worktree != r.new_worktree else ""
        return f"shutdown record: {session}{paths}"
    return (f"shutdown record: unit {r.old} -> {r.new}, {session}, and the paths under its worktree, "
            + "each paused Codex worker's included, whose resume prompt names the new worktree")


def _describe_relaunch(r: UnitRename) -> str:
    if not r.session_id and not r.old_session and not shutdown_records(r.slug, (r.old, r.new)):
        return "already matches: no shutdown record to relaunch"
    return f"relaunch {r.new} from its shutdown record in {r.new_worktree}"


def _describe_verify(r: UnitRename) -> str:
    if r.id_matches:
        return f"verify: {r.new} has branch {r.new_branch}, worktree {r.new_worktree}, and matching tab and session names"
    return f"verify: every live place holds the new names, and nothing unclassified holds {r.old}"


STEPS = (
    Step(stop_unit, _describe_stop, stopped=False, merge_lock=MergeLockScope.CHECKOUT_CHECK, source_writes=True),
    Step(_merge, _describe_merge),
    Step(_move_git, _describe_git, source_writes=True),
    Step(_move_claude, _describe_claude, merge_lock=MergeLockScope.NONE, source_writes=True),
    Step(_commit_doc, _describe_doc, source_writes=True),
    Step(_merge_rename, _describe_merge_rename),
    Step(_move_state, _describe_state, source_writes=True),
    Step(_retire, _describe_retire),
    Step(_restart_record, _describe_record, merge_lock=MergeLockScope.NONE, source_writes=True),
    Step(_relaunch, _describe_relaunch, merge_lock=MergeLockScope.NONE, source_writes=True),
    Step(_verify, _describe_verify, stopped=False, merge_lock=MergeLockScope.NONE),
)


# The steps a name-only rename runs; the rest name the id, which already is the new one.
NAME_STEPS: tuple[Callable[[UnitRename, Callable[[], None]], None], ...] = (stop_unit, _restart_record, _relaunch, _verify)


def _runs_step(rename: UnitRename, step: Step) -> bool:
    if rename.id_matches:
        if step.run is _commit_doc:
            text = Path(rename.doc).read_text(encoding="utf-8")
            return bool(rename.doc_commit) or _doc_text(rename, text) != text or _own_doc_edits(rename)
        if step.run is _move_claude:
            return rename.old_worktree != rename.new_worktree
        if step.run is _retire:
            return rename.old_branch != rename.new_branch
    return not rename.id_matches or rename.moves_git or step.run in NAME_STEPS


def step_number(run: Callable[[UnitRename, Callable[[], None]], None]) -> int:
    return next(number for number, step in enumerate(STEPS, 1) if step.run is run)


def _load(path: Path, *, dry_run: bool = False) -> UnitRename:
    fields = unit_move.json_object(read_json(path))
    stop_identity = (StopIdentityEvidence.RECORDED if all(name in fields for name in PANE_IDENTITY_FIELDS)
                     else StopIdentityEvidence.LEGACY_UNKNOWN)
    step = fields.get("step")
    id_matches = fields.get("id_matches", False)
    if type(id_matches) is not bool:
        raise RenameRefused(f"{path}: invalid unit rename journal")
    if (not all(isinstance(fields.get(name), str) and fields.get(name) for name in REQUIRED if name not in LOCATION_FIELDS)
            or type(step) is not int or not all(isinstance(fields.get(name, ""), str) for name in PROGRESS)):
        raise RenameRefused(f"{path}: invalid unit rename journal")
    # A showrunner checkout can move while the rename is open. Ignore legacy locations.
    production = _production(cast(str, fields["slug"]))
    selectors = tuple(staff_sources.UnitMember(production.slug, cast(str, fields[name]))
                      for name in ("old", "new"))
    if not dry_run:
        fields = rename_state.migrate_staff_ids(path, fields, {"staff_id": selectors})
    if dry_run and "staff_id" not in fields:
        identity = UnboundStaffMember()
    elif isinstance(fields.get("staff_id"), str) and fields["staff_id"]:
        identity = staff.StaffId(cast(str, fields["staff_id"]))
    else:
        raise RenameRefused(f"{path}: invalid unit rename staff identity")
    fields.update(doc=str(production.doc), checkout=str(production.checkout))
    merged, doc_commit, unit_tip, account, session_id, kept, old_session = (cast(str, fields.get(name, ""))
                                                                            for name in PROGRESS)
    pane_session, pane_id, terminal_id = (fields.get(name, "") for name in PANE_IDENTITY_FIELDS)
    if (not isinstance(pane_session, str) or not isinstance(pane_id, str) or not isinstance(terminal_id, str)
            or bool(pane_session) != bool(pane_id) or terminal_id and not pane_id):
        raise RenameRefused(f"{path}: invalid journaled pane identity")
    return UnitRename(*(cast(str, fields[name]) for name in REQUIRED), step=step, merged=merged,
                      pane_session=pane_session, pane_id=pane_id, terminal_id=terminal_id,
                      stop_identity=stop_identity,
                      doc_commit=doc_commit, unit_tip=unit_tip, account=account, session_id=session_id, kept=kept,
                      old_session=old_session, id_matches=id_matches, staff_id=identity)


def _save(path: Path, rename: UnitRename) -> None:
    # The claim holds until the record names the new unit: from then on the relaunch, and any
    # later launch, finds every name moved.
    saved = cast(dict[str, object], asdict(rename))
    del saved["stop_identity"]
    if rename.stop_identity is StopIdentityEvidence.LEGACY_UNKNOWN:
        for name in PANE_IDENTITY_FIELDS:
            del saved[name]
    for name in LOCATION_FIELDS:
        del saved[name]
    write_json(path, {**saved, "done": rename.step == len(STEPS),
                      "claimed": rename.step < step_number(_restart_record)})


def _resumed(path: Path, old: str, new: str, *, dry_run: bool = False) -> UnitRename | None:
    """The unfinished rename the journal holds, which must be this request; None without one."""
    if not path.exists():
        return None
    fields = unit_move.json_object(read_json(path))
    try:
        same = (fields.get("old") in (old, unit_names.unit_id(old)) or
                (old and old == fields.get("old_session"))) and unit_names.unit_id(new) == fields.get("new")
    except unit_names.BadName:
        same = False
    finished = fields.get("done") is True
    if finished and (not same or fields.get("old") == fields.get("new")):
        return None
    record = staff.snapshot()
    identity = fields.get("staff_id")
    if isinstance(identity, str) and record.by_id(staff.StaffId(identity)) is None:
        return None
    if "staff_id" not in fields:
        slug = path.parent.name
        if not any(isinstance(staff_sources.member(staff_sources.UnitMember(slug, value), record),
                              staff_sources.MemberFound)
                   for name in ("old", "new") if isinstance(value := fields.get(name), str)):
            return None
    recorded = _load(path, dry_run=dry_run)
    if not same:
        raise RenameRefused(f"the unfinished rename is {recorded.old} to {recorded.new}; resume it first with "
                            + f"rename_unit.py apply {recorded.slug} {recorded.old} {recorded.new}")
    return recorded


def describe(rename: UnitRename) -> list[str]:
    """Every step, and which part of the request each changes or already matches."""
    lines: list[str] = []
    for number, step in enumerate(STEPS, 1):
        if rename.step == len(STEPS) or not _runs_step(rename, step):
            lines.append(f"{number}. already matches: the id is {rename.new}, so nothing here changes")
            continue
        lines.append(f"{number}. {step.describe(rename)}{'  (done)' if number <= rename.step else ''}")
    return lines


def plan(slug: str, old: str, new: str, scratch: Path) -> UnitRename:
    slug = _production(slug).slug
    recorded = _resumed(rename_state.unit_journal(slug), old, new, dry_run=True)
    if recorded is not None:
        return recorded
    rename = _build(slug, old, new, scratch)
    _preflight(rename)
    return rename


def _rename_staff(rename: UnitRename) -> None:
    if isinstance(rename.staff_id, UnboundStaffMember):
        raise staff.StaffRefused(f"{rename.slug}/{rename.old}: no journaled staff identity")
    _ = staff.rename(rename.staff_id, unit_names.name(rename.new))


def apply(slug: str, old: str, new: str, scratch: Path) -> UnitRename:
    slug = _production(slug).slug
    path = rename_state.unit_journal(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with unit_move.production_lock():
        if rename_state.unfinished(rename_state.production_journal(slug)):
            raise RenameRefused(f"{slug}: finish the production rename first")
        rename = _resumed(path, old, new)
        fresh = rename is None
        if rename is None:
            rename = _build(slug, old, new, scratch)
            if rename.step == len(STEPS):
                if rename_state.unfinished(path):
                    path.unlink()
                print("done; unchanged")
                return rename
            with _merge_lock(rename):
                _preflight(rename)
        with staff.operation():
            if fresh:
                rename.staff_id = _staff_id(rename.slug, rename.old)
                bind_stop_target(rename)
                # The request is saved before anything changes, so every later refusal resumes.
                _save(path, rename)
            if rename.session_id and not rename.account:
                # Old journals may have lost both the account and its launch entry. Refuse before
                # changing staff or files when no surviving source can identify the account.
                bind_stop_target(rename)
                _save(path, rename)
            try:
                _rename_staff(rename)
            except staff.StaffRefused:
                if fresh:
                    path.unlink(missing_ok=True)
                raise
        if rename.step >= step_number(_relaunch):
            # Older trees journaled relaunch before removing its saved old title.
            # Reconcile that source without replaying the already completed restart.
            _rename_saved_layout(rename)
        if rename.step == len(STEPS):
            print("done; unchanged")
        for number in range(rename.step + 1, len(STEPS) + 1):
            step = STEPS[number - 1]
            if not _runs_step(rename, step):
                rename.step = number
                _save(path, rename)
                continue
            with ExitStack() as merge_scope:
                if step.merge_lock is not MergeLockScope.NONE:
                    _ = merge_scope.enter_context(_merge_lock(rename))
                    _check_checkout(rename, resume_doc=step.run is _commit_doc)
                    if step.merge_lock is MergeLockScope.CHECKOUT_CHECK:
                        merge_scope.close()
                with ExitStack() as source_scope:
                    if step.stopped or step.source_writes:
                        _ = source_scope.enter_context(staff.operation())
                    if step.stopped:
                        _check_stopped(rename, relaunch=step.run is _relaunch)
                    if not step.source_writes:
                        source_scope.close()
                    step.run(rename, lambda: _save(path, rename))
            rename.step = number
            _save(path, rename)
    return rename


def _merge_lock(rename: UnitRename) -> AbstractContextManager[None]:
    return merge_checkpoint.merge_lock(Path(rename.checkout), rename.slug)


def verify(slug: str, old: str, new: str) -> UnitRename:
    """Rerun the verify step of a rename that has reached it."""
    slug = _production(slug).slug
    path = rename_state.unit_journal(slug)
    if not path.exists():
        raise RenameRefused(f"{slug} has no unit rename to verify")
    recorded = _load(path)
    if recorded.old not in (old, unit_names.unit_id(old)) or recorded.new != unit_names.unit_id(new):
        raise RenameRefused(f"the recorded rename is {recorded.old} to {recorded.new}")
    if recorded.step < step_number(_verify) - 1:
        raise RenameRefused(f"{recorded.old} to {recorded.new} is at step {recorded.step}; finish it with apply")
    _verify(recorded, lambda: None)
    # A verify that passes finishes the rename, so the next rename is not refused as resuming this one.
    recorded.step = len(STEPS)
    _save(path, recorded)
    return recorded


def main(argv: list[str]) -> int:
    return staff.cli_result(lambda: _main(argv))


def _main(argv: list[str]) -> int:
    from scripts.direct.workers import WorkerUnavailable
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("slug")
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    _ = parser.add_argument("--scratch", help="the showrunner state directory; the production's own by default")
    args = parser.parse_args(argv)
    action, slug = cast(str, args.action), cast(str, args.slug)
    given = cast(str | None, args.scratch)
    old, new = cast(str, args.old), cast(str, args.new)
    scratch = Path(given) if given else showrunners.session_state_dir(slug)
    try:
        rename = (plan(slug, old, new, scratch) if action == "plan" else apply(slug, old, new, scratch)
                  if action == "apply" else verify(slug, old, new))
    except (staff.StaffRefused, staff.StaffNotFound, staff.StaffUnreadable):
        raise
    except RenameWaiting as error:
        print(f"rename_unit: {error}", file=sys.stderr)
        return 2
    except (RenameRefused, production_units.Refusal, unit_names.BadName, WorkerUnavailable, OSError, UnicodeError) as error:
        print(f"rename_unit: {error}", file=sys.stderr)
        return 1
    for line in describe(rename):
        print(line)
    if action != "plan":
        print(f"{rename.old} is now {rename.new}" + (f", relaunched in {rename.new_worktree}" if rename.session_id
                                                     else f"; it was not running: launch it from {rename.new_worktree}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
