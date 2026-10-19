#!/usr/bin/env python3
"""Plan, apply and verify one unit's rename, journaled so a refusal resumes where it stopped.

Usage: rename_unit.py plan|apply|verify <slug> <old> <new> --scratch <showrunner scratchpad>

`<old>` is the unit's Units id, or its stem; `<new>` takes an id or a stem and becomes
`<stem>-unit`, as add_unit names a unit. apply saves the request before it changes anything, stops
the unit through the shutdown path, merges the merge branch into the unit, moves the branch and
worktree (unit_move), commits `chore(<slug>): rename unit <old> to <new>` on the merge branch, which
merge_branch_history follows, merges that commit into the unit, moves the unit's state, retires the
old branch, points the unit's shutdown record at the new names, relaunches the unit from it and
verifies. A stop takes a while: apply exits 2 while the unit is stopping, and a rerun resumes. plan
writes nothing: it runs every refusal check and prints the steps with their old and new values.
verify reruns the last step on a finished rename.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "delegate"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "herdr"))
import add_unit  # noqa: E402
import issues  # noqa: E402
import phase_table  # noqa: E402
import production_units  # noqa: E402
import proposal_issue  # noqa: E402
import proposals_folder  # noqa: E402
import rename_state  # noqa: E402
import review_pool  # noqa: E402
import showrunners  # noqa: E402
import stall_watch  # noqa: E402
import unit_lookup  # noqa: E402
import unit_move  # noqa: E402
import unit_names  # noqa: E402
import unit_rename_sweep  # noqa: E402
from rename_state import RenameRefused, read_json, write_json, write_text  # noqa: E402
from unit_move import PathMove, git  # noqa: E402
from user_zone import user_zone  # noqa: E402

if TYPE_CHECKING:
    from scripts.shutdown import record as shutdown_record
    from scripts.shutdown.record import SessionShutdown
else:
    import record as shutdown_record
    from record import SessionShutdown

RESERVED = frozenset({"review-unit", add_unit.NIGHTLY_REVIEW_UNIT})
# Columns a unit rename edits outside the Units row; a File or Gate cell is never touched.
EDITED_COLUMNS = {"## Hub files": ("Owner unit", "Other units that touch it"), "## Gates": ("Waiting", "Waits on")}
PROGRESS = ("merged", "doc_commit", "unit_tip", "account", "session_id", "kept")
SHUTDOWN = Path(os.environ.get("RENAME_UNIT_SHUTDOWN") or Path(__file__).resolve().parent.parent / "shutdown/shutdown.py")
STOPPED = frozenset({"stopped", "already gone", "process identity lost"})
STOPPING = frozenset({"waiting", "reaping", "ready", "stopping", "held by usage limit", "passive worker ready"})


class RenameWaiting(RenameRefused):
    """The unit is still stopping: a rerun of apply resumes once it has stopped."""


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
    # Why the unit branch took no merge: its work was in progress (an open phase or uncommitted work).
    kept: str = ""

    @property
    def message(self) -> str:
        return f"chore({self.slug}): rename unit {self.old} to {self.new}"

    def worktree(self) -> Path:
        """Where the unit's worktree is now: the git step moves it."""
        return Path(self.new_worktree if self.step >= step_number(_move_git) else self.old_worktree)


REQUIRED = tuple(field.name for field in dataclasses.fields(UnitRename) if field.name not in ("step", *PROGRESS))


def _swap(value: str, old_stem: str, new_stem: str, what: str) -> str:
    swapped = unit_names.swap_stem(value, old_stem, new_stem)
    if swapped is None:
        raise RenameRefused(f"{what} {value} does not end with -{old_stem}")
    return swapped


def _production(slug: str) -> add_unit.Production:
    instance = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}"
    checked = showrunners.checked_doc(instance)
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{slug} has no readable update timer: {checked.reason}")
    return add_unit.read_production(checked.path)


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
    rows = _rows(production.doc)
    units = [production_units.cell_value(row.get("Unit", "")) for row in rows]
    old, new = _ids(old, new, units)
    if RESERVED & {old, new}:
        raise RenameRefused(f"{' and '.join(sorted(RESERVED & {old, new}))} is known to code and keeps its name")
    if old == new:
        raise RenameRefused(f"{old} already has that name")
    if new in units:
        raise RenameRefused(f"{new} is already in the Units table")
    matching = [row for row, unit in zip(rows, units, strict=True) if unit == old]
    if len(matching) != 1:
        raise RenameRefused(f"{old}: expected one Units row, found {len(matching)}")
    row = matching[0]
    if production_units.plan_cell_is_retired(row.get("Plan", "")):
        raise RenameRefused(f"{old} is retired; its row is history")
    old_branch = production_units.cell_value(row.get("Branch", ""))
    worktree = Path(production_units.cell_value(row.get("Worktree", ""))).expanduser()
    new_branch = _swap(old_branch, unit_names.stem(old), unit_names.stem(new), "branch")
    new_worktree = worktree.with_name(_swap(worktree.name, unit_names.stem(old), unit_names.stem(new), "worktree"))
    if not (worktree / ".git").is_file():
        # A Mac unit's worktree is on the Mac: version one renames natedev units only.
        raise RenameRefused(f"{old}: no linked worktree at {worktree} on this machine; a Mac unit is not renamed")
    if git(worktree, "branch", "--show-current") != old_branch:
        raise RenameRefused(f"{worktree} is not on {old_branch}")
    return UnitRename(slug, old, new, str(production.doc), str(production.checkout), production.merge_branch,
                      old_branch, new_branch, str(worktree), str(new_worktree),
                      git(worktree, "rev-parse", "HEAD"), str(scratch))


def _check_stopped(rename: UnitRename) -> None:
    """The unit runs no Claude under either id, and nothing works inside its worktree."""
    try:
        marked = unit_lookup.marked_units(rename.slug)
    except OSError as error:
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {error}") from error
    for unit in (rename.old, rename.new):
        found = marked.get(unit)
        if found and isinstance(found.claude, unit_lookup.LiveClaude):
            raise RenameRefused(f"{unit} has a live Claude; stop it first")
        if found and isinstance(found.claude, unit_lookup.ClaudeUnknown):
            raise RenameRefused(f"{unit}: cannot tell whether Claude is running: {found.claude.reason}")
    pids = unit_move.processes_inside(rename.worktree())
    if pids:
        raise RenameRefused(f"processes {', '.join(map(str, pids))} work inside {rename.worktree()}; stop them first")


def _check_stoppable(rename: UnitRename) -> None:
    """What the stop step cannot settle: the new id running, a Claude it cannot read, or processes
    in the worktree of a unit with no Claude to stop. A live old unit is the stop step's to stop."""
    try:
        marked = unit_lookup.marked_units(rename.slug)
    except OSError as error:
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {error}") from error
    old, new = marked.get(rename.old), marked.get(rename.new)
    if new and not isinstance(new.claude, unit_lookup.ClaudeNotRunning):
        raise RenameRefused(f"{rename.new} already runs in session {new.place.session}")
    if old and isinstance(old.claude, unit_lookup.ClaudeUnknown):
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {old.claude.reason}")
    if old and isinstance(old.claude, unit_lookup.LiveClaude):
        return
    pids = unit_move.processes_inside(rename.worktree())
    if pids:
        raise RenameRefused(f"processes {', '.join(map(str, pids))} work inside {rename.worktree()}; stop them first")


def _check_checkout(rename: UnitRename) -> None:
    # Untracked files are left alone: the doc commit stages only tracked changes, and the checkout
    # keeps untracked files of its own, such as the production log, which `!docs/**` keeps visible.
    checkout = Path(rename.checkout)
    if (git(checkout, "branch", "--show-current") != rename.merge_branch
            or git(checkout, "status", "--porcelain", "--untracked-files=no")):
        raise RenameRefused(f"showrunner checkout must be clean and on {rename.merge_branch}")


def _preflight(rename: UnitRename) -> None:
    """The read-only refusals of a fresh rename; a resumed one accepts what its own steps made."""
    checkout = Path(rename.checkout)
    _check_checkout(rename)
    if unit_move.branch_tip(checkout, rename.new_branch) or unit_move.remote_tip(checkout, rename.new_branch):
        raise RenameRefused(f"branch {rename.new_branch} already exists")
    if unit_move.exists(Path(rename.new_worktree)):
        raise RenameRefused(f"destination already exists: {rename.new_worktree}")
    if rename_state.unfinished(rename_state.production_journal(rename.slug)):
        raise RenameRefused(f"{rename.slug}: finish the production rename first")
    # A unit mid-phase is renamed too: its commits move with the branch, and _merge leaves work in
    # progress alone (user, 2026-10-18).
    _check_stoppable(rename)


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


def _plans(rename: UnitRename) -> list[Path]:
    checkout = Path(rename.checkout)
    row = next(row for row in _rows(Path(rename.doc)) if production_units.cell_value(row.get("Unit", "")) in
               (rename.old, rename.new))
    return [] if production_units.plan_cell_is_retired(row.get("Plan", "")) else list(
        unit_move.plan_paths(row.get("Plan", ""), checkout))


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
    unit_move.push_branch(checkout, rename.old_branch, rename.new_branch, rename.merged)
    unit_move.move_worktree(checkout, rename.old_branch, rename.new_branch, Path(rename.old_worktree),
                            Path(rename.new_worktree), rename.merged)


def _move_claude(rename: UnitRename, _save: Callable[[], None]) -> None:
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
            allowed = {str(path.relative_to(checkout)) for path in (doc, *plans)}
            if changed - allowed:
                raise RenameRefused("unrelated checkout changes during unit rename")
            _ = git(checkout, "add", "--", *sorted(changed))
            _ = git(checkout, "commit", "-m", rename.message)
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
    _ = rename_state.rename_scratch(rename.old, rename.new, Path(rename.scratch))
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
    today = datetime.now(user_zone()).date()
    for issue in _unit_proposals(rename, rename.old):
        _ = proposal_issue.edit(issue.path, {"unit": rename.new}, today)
    _ = phase_table.rename_unit(rename.slug, rename.old, rename.new)


def _retire(rename: UnitRename, _save: Callable[[], None]) -> None:
    checkout = Path(rename.checkout)
    if unit_move.remote_tip(checkout, rename.new_branch) != rename.unit_tip:
        raise RenameRefused(f"origin {rename.new_branch} is not at {rename.unit_tip[:12]}; old branch kept")
    unit_move.delete_branch(checkout, rename.old_branch, rename.merged)


def _shutdown(*arguments: str) -> None:
    """Run one shutdown verb on this machine. Without a session id in its environment it finds the
    account that holds the session, not the showrunner's own."""
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    result = subprocess.run([sys.executable, str(SHUTDOWN), *arguments, "--here"], env=environment,
                            capture_output=True, text=True, check=False, timeout=900)
    if result.returncode:
        raise RenameRefused(f"shutdown {arguments[0]}: {(result.stderr or result.stdout).strip()}")


def _unit_records(rename: UnitRename) -> list[SessionShutdown]:
    """This production's shutdown records of the unit, under either id."""
    try:
        saved = shutdown_record.all_session_records()
    except (shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"shutdown records unreadable: {error}") from error
    found: list[SessionShutdown] = []
    for item in saved:
        host = item["session"]["host"]
        if host["kind"] == "unit" and host["production"] == rename.slug and host["unit"] in (rename.old, rename.new):
            found.append(item)
    return found


def _stop(rename: UnitRename, _save: Callable[[], None]) -> None:
    """Stop the unit through the shutdown path, so its record carries the relaunch.

    A live process cannot change its env mark and the director's context holds the old paths, so a
    rename of a running unit is always a relaunch.
    """
    records = _unit_records(rename)
    if len(records) > 1:
        raise RenameRefused(f"{rename.old} has {len(records)} shutdown records; settle them first")
    if records:
        saved = records[0]
        progress = saved["progress"]["kind"]
        if saved["then"] == "restart":
            raise RenameRefused(f"a shutdown will restart {rename.old}; rename once it is back")
        if progress in STOPPING:
            raise RenameWaiting(f"{rename.old} is stopping ({progress}); rerun apply once it has stopped")
        if progress not in STOPPED:
            raise RenameRefused(f"{rename.old}'s shutdown record reads {progress}; settle it first")
        rename.account, rename.session_id = saved["login"], saved["session"]["session_id"]
        return
    try:
        found = unit_lookup.marked_unit(rename.slug, rename.old)
    except OSError as error:
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {error}") from error
    if found is None or isinstance(found.claude, unit_lookup.ClaudeNotRunning):
        return
    if isinstance(found.claude, unit_lookup.ClaudeUnknown):
        raise RenameRefused(f"{rename.old}: cannot tell whether Claude is running: {found.claude.reason}")
    _shutdown("down", "--only", found.claude.session_id)
    raise RenameWaiting(f"{rename.old} is stopping; rerun apply once it has stopped")


def _moved(path: str, old: str, new: str) -> str:
    return new + path[len(old):] if path == old or path.startswith(old + "/") else path


def renamed_note(rename: UnitRename) -> str:
    plans = ", ".join(str(path).replace(rename.checkout, rename.new_worktree, 1) for path in _plans(rename))
    return (f"While you were stopped the showrunner renamed you: you are now {rename.new}, in worktree "
            + f"{rename.new_worktree} on branch {rename.new_branch}"
            + (f", and your plan is at {plans}" if plans else "") + ". Use these names from now on."
            + (f" Your work was in progress ({rename.kept}), so your branch kept its commits and took no merge: "
               + f"the rename commit comes with your next merge of {rename.merge_branch}. Your paused Codex seats "
               + f"now work in {rename.new_worktree}; say so in your next message to each." if rename.kept else ""))


def _restart_record(rename: UnitRename, _save: Callable[[], None]) -> None:
    """Point the stopped unit's shutdown record at its new names; restart reads the plan from the Units row.

    The Claude session and herdr names follow the unit only while they are the names add_unit gave
    it: a name the user chose stays.
    """
    if not rename.session_id:
        return
    old_stem, new_stem = unit_names.stem(rename.old), unit_names.stem(rename.new)
    import herdr

    host_names = {herdr.generated_session_name(form.format(slug=rename.slug, stem=old_stem)):
                  herdr.generated_session_name(form.format(slug=rename.slug, stem=new_stem))
                  for form in ("{stem}", "{slug}-{stem}")}

    def change(saved: SessionShutdown) -> None:
        session = saved["session"]
        host = session["host"]
        if host["kind"] != "unit" or host["unit"] not in (rename.old, rename.new):
            raise RenameRefused(f"shutdown record {rename.session_id} is not {rename.old}'s")
        if saved["progress"]["kind"] not in STOPPED:
            raise RenameRefused(f"{rename.old}'s shutdown record reads {saved['progress']['kind']}; settle it first")
        host["unit"] = rename.new
        host["session_name"] = host_names.get(host["session_name"], host["session_name"])
        if host["plan"]["kind"] == "plan":
            host["plan"]["path"] = _moved(host["plan"]["path"], rename.old_worktree, rename.new_worktree)
        if session["name"] == old_stem:
            session["name"] = new_stem
        session["cwd"] = _moved(session["cwd"], rename.old_worktree, rename.new_worktree)
        for worker in saved["workers"]:
            worker["cwd"] = _moved(worker["cwd"], rename.old_worktree, rename.new_worktree)
        saved["renamed"] = renamed_note(rename)

    try:
        _ = shutdown_record.update_session(rename.account, rename.session_id, change)
    except (shutdown_record.NoLiveRecord, shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"{rename.old}'s shutdown record: {error}") from error


def _relaunch(rename: UnitRename, _save: Callable[[], None]) -> None:
    if rename.session_id:
        _shutdown("restart", "--only", rename.session_id)


def _unit_proposals(rename: UnitRename, unit: str) -> list[issues.Issue]:
    """The production's active proposal issues that name `unit` for one of this unit's plans."""
    folder = proposals_folder.issues_folder(Path(rename.doc))
    if folder is None:
        return []
    plans = [str(path.relative_to(Path(rename.checkout))) for path in _plans(rename)]
    # The issues folder is shared by every production; unit ids are only unique within one, so an
    # issue counts only when its plan is one of this unit's plans.
    return [issue for issue in issues.open_issues(folder, proposals_folder.PROPOSAL_TAG)
            if issue.value("unit") == unit and any(plan in (issue.value("plan") or "") for plan in plans)]


def _git_failures(rename: UnitRename) -> list[str]:
    checkout, old, new = Path(rename.checkout), Path(rename.old_worktree), Path(rename.new_worktree)
    failures: list[str] = []
    if unit_move.branch_tip(checkout, rename.old_branch) or unit_move.remote_tip(checkout, rename.old_branch):
        failures.append(f"git: branch {rename.old_branch} still exists")
    if unit_move.remote_tip(checkout, rename.new_branch) != unit_move.branch_tip(checkout, rename.new_branch):
        failures.append(f"git: origin {rename.new_branch} is not at the local tip")
    if unit_move.exists(old) or f"worktree {old}" in git(checkout, "worktree", "list", "--porcelain").splitlines():
        failures.append(f"git: worktree {old} still exists")
    if not (new / ".git").is_file() or git(new, "branch", "--show-current") != rename.new_branch:
        failures.append(f"git: {new} is not a worktree on {rename.new_branch}")
    return failures


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
                if (directory / "projects" / unit_move.project_folder(str(old))).exists()]
    if stall_watch.stretch_path(rename.slug, rename.old).exists():
        failures.append(f"stall-watch: the stretch is still under {rename.old}")
    # Step 7 moves the newest run, the one run_state reads; older runs under the old path are history.
    if unit_move.newest_run(old) is not None and unit_move.newest_run(new) is None:
        failures.append(f"run records: the newest run still names {old}")
    failures.extend(f"{issue.path}: proposal still names {rename.old}" for issue in _unit_proposals(rename, rename.old))
    failures.extend(f"{note}: phase note still names {rename.old}"
                    for note in phase_table.owned_notes(rename.slug, rename.old))
    return failures


def _session_failures(rename: UnitRename) -> list[str]:
    if not rename.session_id:
        return []
    failures: list[str] = []
    saved = shutdown_record.find_session(rename.account, rename.session_id)
    if saved is not None and saved["session"]["host"]["kind"] == "unit" and saved["session"]["host"]["unit"] != rename.new:
        failures.append(f"shutdown record {rename.session_id} does not name {rename.new}")
    marked = unit_lookup.marked_units(rename.slug)
    found = marked.get(rename.new)
    if found is None or not isinstance(found.claude, unit_lookup.LiveClaude):
        failures.append(f"{rename.new} is not running; relaunch it with shutdown.py restart --here --only {rename.session_id}")
    if rename.old in marked:
        failures.append(f"a session still carries the mark {rename.old}")
    return failures


def _control_failures(rename: UnitRename) -> list[str]:
    """The matcher must still find the old id in history: finding nothing there means it is broken."""
    subjects = git(Path(rename.checkout), "log", "--format=%s", rename.merge_branch)
    if unit_rename_sweep.token_pattern((rename.old,)).search(subjects):
        return []
    return [f"control: no {rename.merge_branch} subject names {rename.old}; the matcher is broken"]


def _sweep_roots(rename: UnitRename) -> unit_rename_sweep.Roots:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    scratch = Path(rename.scratch)
    return unit_rename_sweep.Roots(
        state=state, production_state=rename_state.unit_journal(rename.slug).parent,
        configs=tuple(directory for directory in unit_move.config_dirs() if not directory.name.endswith(".lock")),
        checkouts=(Path(rename.checkout), Path(rename.new_worktree)),
        live_files=tuple(scratch / name for name in rename_state.SCRATCH_FILES),
        history_prefixes=(str(Path(rename.checkout) / "docs/plans" / f"{rename.slug}-log"),))


def verify_failures(rename: UnitRename) -> list[str]:
    """Every live place holds the new names, and no unclassified file holds an old one."""
    try:
        return [*_doc_failures(rename), *_git_failures(rename), *_state_failures(rename),
                *_session_failures(rename), *_control_failures(rename),
                *unit_rename_sweep.sweep((rename.old, rename.old_branch, rename.old_worktree), _sweep_roots(rename))]
    except (shutdown_record.InvalidRecord, production_units.Refusal) as error:
        raise RenameRefused(f"verify could not read a place: {error}") from error


def _verify(rename: UnitRename, _save: Callable[[], None]) -> None:
    failures = verify_failures(rename)
    if failures:
        raise RenameRefused("verify failed:\n  " + "\n  ".join(failures))


class Step(NamedTuple):
    run: Callable[[UnitRename, Callable[[], None]], None]
    describe: Callable[[UnitRename], str]
    # Whether the unit must be stopped before the step runs: the stop step stops it, and verify
    # runs after the relaunch.
    stopped: bool = True


def _describe_stop(r: UnitRename) -> str:
    return f"stop {r.old} through the shutdown path; its record carries the relaunch"


def _describe_merge(r: UnitRename) -> str:
    return (f"merge origin/{r.merge_branch} into {r.old_branch} in {r.old_worktree}, unless its work is in "
            + "progress (an open phase or uncommitted work): then the branch keeps its tip")


def _describe_git(r: UnitRename) -> str:
    return f"git: branch {r.old_branch} -> {r.new_branch}; worktree {r.old_worktree} -> {r.new_worktree}"


def _describe_claude(_: UnitRename) -> str:
    return "Claude: transcript folders and trust entries follow the worktree"


def _describe_doc(r: UnitRename) -> str:
    return f"{r.merge_branch}: commit {r.message!r} (Units row, Hub files and open Gates unit cells, plan headers)"


def _describe_merge_rename(r: UnitRename) -> str:
    return f"{r.new_branch}: merge the rename commit and push it, unless the merge step kept the tip"


def _describe_state(r: UnitRename) -> str:
    return ("state: report scratch, stall-watch stretch, reviewer leases and memory, run record, open proposals, "
            + f"phase notes: {r.old} -> {r.new}")


def _describe_retire(r: UnitRename) -> str:
    return f"retire {r.old_branch} once origin {r.new_branch} holds the unit's new tip"


def _describe_record(r: UnitRename) -> str:
    return f"shutdown record: unit {r.old} -> {r.new}, and the paths under its worktree"


def _describe_relaunch(r: UnitRename) -> str:
    return f"relaunch {r.new} from its shutdown record in {r.new_worktree}"


def _describe_verify(r: UnitRename) -> str:
    return f"verify: every live place holds the new names, and nothing unclassified holds {r.old}"


STEPS = (
    Step(_stop, _describe_stop, stopped=False),
    Step(_merge, _describe_merge),
    Step(_move_git, _describe_git),
    Step(_move_claude, _describe_claude),
    Step(_commit_doc, _describe_doc),
    Step(_merge_rename, _describe_merge_rename),
    Step(_move_state, _describe_state),
    Step(_retire, _describe_retire),
    Step(_restart_record, _describe_record),
    Step(_relaunch, _describe_relaunch),
    Step(_verify, _describe_verify, stopped=False),
)


def step_number(run: Callable[[UnitRename, Callable[[], None]], None]) -> int:
    return next(number for number, step in enumerate(STEPS, 1) if step.run is run)


def _load(path: Path) -> UnitRename:
    fields = unit_move.json_object(read_json(path))
    step = fields.get("step")
    if (not all(isinstance(fields.get(name), str) and fields.get(name) for name in REQUIRED)
            or type(step) is not int or not all(isinstance(fields.get(name, ""), str) for name in PROGRESS)):
        raise RenameRefused(f"{path}: invalid unit rename journal")
    merged, doc_commit, unit_tip, account, session_id, kept = (cast(str, fields.get(name, "")) for name in PROGRESS)
    return UnitRename(*(cast(str, fields[name]) for name in REQUIRED), step=step, merged=merged,
                      doc_commit=doc_commit, unit_tip=unit_tip, account=account, session_id=session_id, kept=kept)


def _save(path: Path, rename: UnitRename) -> None:
    # The claim holds until the record names the new unit: from then on the relaunch, and any
    # later launch, finds every name moved.
    write_json(path, {**asdict(rename), "done": rename.step == len(STEPS),
                      "claimed": rename.step < step_number(_restart_record)})


def _resumed(path: Path, old: str, new: str) -> UnitRename | None:
    """The unfinished rename the journal holds, which must be this request; None without one."""
    if not rename_state.unfinished(path):
        return None
    recorded = _load(path)
    try:
        same = recorded.old in (old, unit_names.unit_id(old)) and unit_names.unit_id(new) == recorded.new
    except unit_names.BadName:
        same = False
    if not same:
        raise RenameRefused(f"the unfinished rename is {recorded.old} to {recorded.new}; resume it first with "
                            + f"rename_unit.py apply {recorded.slug} {recorded.old} {recorded.new}")
    return recorded


def describe(rename: UnitRename) -> list[str]:
    return [f"{number}. {step.describe(rename)}{'  (done)' if number <= rename.step else ''}"
            for number, step in enumerate(STEPS, 1)]


def plan(slug: str, old: str, new: str, scratch: Path) -> UnitRename:
    slug = showrunners.canonical_slug(slug)
    recorded = _resumed(rename_state.unit_journal(slug), old, new)
    if recorded is not None:
        return recorded
    rename = _build(slug, old, new, scratch)
    _preflight(rename)
    return rename


def apply(slug: str, old: str, new: str, scratch: Path) -> UnitRename:
    slug = showrunners.canonical_slug(slug)
    path = rename_state.unit_journal(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with unit_move.production_lock():
        if rename_state.unfinished(rename_state.production_journal(slug)):
            raise RenameRefused(f"{slug}: finish the production rename first")
        rename = _resumed(path, old, new)
        if rename is None:
            rename = _build(slug, old, new, scratch)
            _preflight(rename)
            # The request is saved before anything changes, so every later refusal resumes.
            _save(path, rename)
        for number in range(rename.step + 1, len(STEPS) + 1):
            # Revalidated before every step: a resumed run may come back to a changed machine.
            _check_checkout(rename)
            if STEPS[number - 1].stopped:
                _check_stopped(rename)
            STEPS[number - 1].run(rename, lambda: _save(path, rename))
            rename.step = number
            _save(path, rename)
    return rename


def verify(slug: str, old: str, new: str) -> UnitRename:
    """Rerun the verify step of a rename that has reached it."""
    slug = showrunners.canonical_slug(slug)
    path = rename_state.unit_journal(slug)
    if not path.exists():
        raise RenameRefused(f"{slug} has no unit rename to verify")
    recorded = _load(path)
    if recorded.old not in (old, unit_names.unit_id(old)) or recorded.new != unit_names.unit_id(new):
        raise RenameRefused(f"the recorded rename is {recorded.old} to {recorded.new}")
    if recorded.step < step_number(_verify) - 1:
        raise RenameRefused(f"{recorded.old} to {recorded.new} is at step {recorded.step}; finish it with apply")
    _verify(recorded, lambda: None)
    return recorded


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("slug")
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    _ = parser.add_argument("--scratch", required=True, help="the showrunner's scratchpad directory")
    args = parser.parse_args(argv)
    action, slug = cast(str, args.action), cast(str, args.slug)
    old, new, scratch = cast(str, args.old), cast(str, args.new), Path(cast(str, args.scratch))
    try:
        rename = (plan(slug, old, new, scratch) if action == "plan" else apply(slug, old, new, scratch)
                  if action == "apply" else verify(slug, old, new))
    except RenameWaiting as error:
        print(f"rename_unit: {error}", file=sys.stderr)
        return 2
    except (RenameRefused, production_units.Refusal, unit_names.BadName, OSError, UnicodeError) as error:
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
