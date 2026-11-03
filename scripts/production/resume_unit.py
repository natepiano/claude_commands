#!/usr/bin/env python3
"""Resume one retired unit: make its worktree again and resume the session its director last used.

Usage: resume_unit.py <slug> <unit> [--dry-run]

A showrunner runs it through /showrunner:resume_unit. retire_unit.py leaves the unit's Units row with
"(retired <date>, worktree removed, session <id>)" in front of its Plan cell, and the row still names
the worktree path, the branch and the plan. resume_unit refuses, and changes nothing, when the row is
not retired or names no session, when no Claude config dir holds that session's transcript under its
start folder's project folder (`claude --resume` finds a transcript only there), when a folder at the
worktree path is not the unit's worktree, or when the branch is still here or on origin without it.
Every account links its `projects` folder to the shared one, so a units account that moved since the
retire reads the same transcript.

A note can include ", folder <path>" after the session id. Older notes find the transcript across
all config dirs and read the first cwd matching the project folder that holds it; none, ambiguous
project folders or no matching cwd refuse. When the
start folder is outside the worktree, resume uses --cwd there and restores the unit worktree too.
The parked configuration supplies the session, folder, branch and plan after backfill. A preview
does not recover a session by name or need the original retirement note's session and folder.

When the note names a retired ref and tip, resume fetches that ref and restores the branch and
worktree at that exact tip, even when the session starts elsewhere. It checks worktree HEAD before
deleting the origin ref. A stop after branch creation or ref deletion resumes safely on a rerun.
Without a saved ref, it makes the worktree from the merge branch as before. Then it resumes the
session through `add_unit.py --resume` (the units account, the unit's name and Remote Control); and
takes the retired note out of the Plan cell, committed on the merge branch. Each step checks whether
it is done, so a rerun after a failure resumes: the note goes last, because it holds the session id.
Resume restores the retained staff row and keeps its id and parent.
--dry-run prints each refusal and each step, and changes nothing.

Exit 0 when the unit is resumed; 1 on a refusal or a failed step; 2 on usage.
"""
from __future__ import annotations


import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import completion_receipt, staff, staff_sources, unit_config
from scripts.shutdown import remote
from scripts.lib import conf

import add_unit
import production_units
import retire_unit
import unit_lookup
import unit_move
import unit_names
import pane_record
from production_units import Production
from atomic_file import write_text_atomic

ADD_UNIT = Path(__file__).resolve().parent / "add_unit.py"
RETIRED = re.compile(r"^\s*\(retired [^)]*\)\s*")
SESSION = re.compile(r"^\s*\(retired [^)]*, session ([0-9A-Za-z-]+)(?:, folder (.*?))?(?:, (?:ref|staff) [^)]*)?\)")


class Refused(Exception):
    pass


class BackfillRequired(Refused):
    """The configuration must be filled before preview or apply."""



RecordedStartFolder = unit_config.RecordedStartFolder
UnrecordedStartFolder = unit_config.UnrecordedStartFolder


@dataclass(frozen=True)
class SavedUnitName:
    """A caller has supplied only the saved unit name."""


@dataclass(frozen=True)
class RetiredUnit:
    production: Production
    name: str
    worktree: Path
    branch: str
    plan: str
    session_id: str
    retired: bool
    start_folder: RecordedStartFolder | UnrecordedStartFolder
    saved_branch: retire_unit.SavedBranch | retire_unit.NoSavedBranch = retire_unit.NoSavedBranch()
    recovery: unit_config.UnitConfig | unit_config.NoRetiredSession = unit_config.NoRetiredSession()
    display_name: str | SavedUnitName = field(default_factory=SavedUnitName)


def find_unit(slug: str, given: str, *, record: staff.StaffSnapshot | None = None) -> RetiredUnit:
    production = retire_unit.production_of(slug)
    rows = production_units.unit_table(production.doc.read_text(encoding="utf-8").splitlines())
    ids = [production_units.cell_value(row.get("Unit", "")) for row in rows]
    name = given if given in ids else unit_names.unit_id(given)
    matching = [row for row, unit in zip(rows, ids, strict=True) if unit == name]
    if len(matching) > 1:
        raise Refused(f"{name}: expected one Units row, found {len(matching)}")
    row = matching[0] if matching else {}
    plan = row.get("Plan", "")
    pending = resume_record(production, name)
    if not production_units.plan_cell_is_retired(plan) and pending.exists():
        retired_plan = pending.read_text(encoding="utf-8")
        if RETIRED.sub("", retired_plan).strip() == plan.strip():
            plan = retired_plan
    retired = production_units.plan_cell_is_retired(plan)
    record = staff.initial_snapshot() if record is None else record
    sources = unit_lookup.member_sources()
    member = sources.member(staff_sources.UnitMember(production.slug, name), record)
    if isinstance(member, staff_sources.NoSuchMember):
        raise Refused(f"{name}: no staff row; run staff_sources.py fill --retired")
    location = member.row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise Refused(f"{name}: staff worktree is unfilled; run staff_sources.py fill --retired")
    if location.machine != remote.local_machine():
        raise Refused(f"{name}: staff worktree belongs to {location.machine}; run resume there")
    worktree = location.path
    retired = isinstance(location, staff.RemovedWorktree)
    path_text = production_units.cell_value(row.get("Worktree", ""))
    if path_text and path_text not in ("—", "-") and Path(path_text).expanduser() != worktree:
        raise Refused(f"{name}: staff worktree {worktree} conflicts with Units row {path_text}")
    branch = production_units.cell_value(row.get("Branch", ""))
    saved = unit_config.read_parked(member.row.id, cleanup=False)
    current = unit_config.read(worktree, cleanup=False)
    if isinstance(saved, unit_config.ConfigMissing) and isinstance(current, unit_config.ConfigMissing):
        raise BackfillRequired(f"{name}: no .unit.toml in {worktree} or ~/.local/state/staff/units/{member.row.id}.toml; "
                               + "run staff_sources.py fill --retired or ask the showrunner")
    if not retired and isinstance(saved, unit_config.ConfigMissing):
        return RetiredUnit(production, name, worktree, branch, production_units.cell_value(plan), "", False,
                           UnrecordedStartFolder(), display_name=member.row.name)
    if isinstance(saved, unit_config.ConfigMissing):
        saved = staff_sources.prepare_retirement({**row, "Plan": plan}, sources, member, slug)
        assert isinstance(current, unit_config.UnitConfig)
        saved = replace(saved, responsibilities=current.responsibilities)
        key = unit_config.first_difference(replace(saved, retired=unit_config.NoRetiredSession()), current)
        if key:
            raise Refused(f"{worktree / '.unit.toml'}: {key} differs from retirement facts; no file was changed")
    assert isinstance(saved, unit_config.UnitConfig)
    if not isinstance(saved.retired, unit_config.RetiredSession):
        raise Refused(f"{name}: parked retirement facts are incomplete")
    pair = unit_config.place(member.row.id, worktree, cleanup=False)
    if isinstance(pair, unit_config.Both):
        if not pair.same:
            raise Refused(f"{pair.worktree} and {pair.parked}: configurations differ; no file was changed")
    facts = saved.retired
    if branch and branch not in ("—", "-") and facts.branch.value != branch:
        raise Refused(f"{name}: parked branch conflicts with Units row")
    if not isinstance(facts.start_folder, unit_config.RecordedStartFolder):
        raise Refused(f"{name}: parked retirement folder is missing; run staff_sources.py fill --retired")
    config_plan = (saved.plan.path.as_posix() if isinstance(saved.plan, unit_config.PlanFile) else
                   "permanent" if isinstance(saved.plan, unit_config.PermanentPlan) else "standby")
    folder = RecordedStartFolder(facts.start_folder.path)
    return RetiredUnit(production, name, worktree, facts.branch.value, config_plan, member.row.current_session,
                       True, folder, retire_unit.saved_branch_in_note(plan), saved, member.row.name)


def resume_record(production: Production, name: str) -> Path:
    result = retire_unit.run(["git", "-C", str(production.checkout), "rev-parse", "--git-path",
                              f"resume-units/{name}"])
    if result.returncode:
        raise Refused(f"cannot find the resume record: {retire_unit.failure(result)}")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else production.checkout / path


def _git(unit: RetiredUnit, *arguments: str) -> str:
    result = retire_unit.run(["git", "-C", str(unit.production.checkout), *arguments])
    return result.stdout.strip() if result.returncode == 0 else ""


def has_worktree(unit: RetiredUnit) -> bool:
    """Whether the worktree path holds the unit's worktree on its branch."""
    return ((unit.worktree / ".git").is_file()
            and retire_unit.run(["git", "-C", str(unit.worktree), "branch", "--show-current"]).stdout.strip()
            == unit.branch)


def resume_folder(unit: RetiredUnit) -> Path:
    if isinstance(unit.start_folder, RecordedStartFolder):
        return unit.start_folder.path
    return retire_unit.director_resume_folder(unit.session_id)


def has_transcript(unit: RetiredUnit) -> bool:
    folder = unit_move.project_folder(str(resume_folder(unit)))
    return any((directory / "projects" / folder / f"{unit.session_id}.jsonl").is_file()
               for directory in unit_move.config_dirs())


def is_running(unit: RetiredUnit) -> bool:
    """Whether the unit's mark is on a live Claude that is the session it resumes."""
    try:
        found = unit_lookup.marked_unit(unit.production.slug, unit.name)
    except OSError as error:
        raise Refused(f"{unit.name}: cannot tell whether Claude is running: {error}") from error
    return (found is not None and isinstance(found.claude, unit_lookup.LiveClaude)
            and found.claude.session_id == unit.session_id)


def refusals(unit: RetiredUnit) -> list[str]:
    if not unit.retired:
        return [f"{unit.name} is not retired"]
    if not unit.session_id:
        return [f"{unit.name}: its Units row names no session to resume"]
    refused: list[str] = []
    folder = unit.worktree
    try:
        folder = resume_folder(unit)
        actual = retire_unit.director_resume_folder(unit.session_id)
        if actual.resolve() != folder.resolve():
            refused.append(f"{unit.name}: transcript folder {actual} conflicts with parked folder {folder}")
        if not has_transcript(unit):
            refused.append(f"{unit.name}: no account has the transcript of session {unit.session_id} under {folder}")
    except (retire_unit.Refused, OSError) as error:
        refused.append(f"{unit.name}: {error}")
    if unit.worktree.exists() and not has_worktree(unit):
        refused.append(f"{unit.worktree} exists and is not {unit.name}'s worktree on {unit.branch}")
    elif not unit.worktree.exists() and isinstance(unit.saved_branch, retire_unit.NoSavedBranch):
        if _git(unit, "branch", "--list", unit.branch):
            refused.append(f"branch {unit.branch} still exists; retire did not finish")
        if _git(unit, "ls-remote", "--heads", "origin", unit.branch):
            refused.append(f"origin {unit.branch} still exists; retire did not finish")
    return refused


def launch_command(unit: RetiredUnit) -> list[str]:
    if not unit.session_id:
        raise Refused(f"{unit.name}: its Units row names no session to resume")
    name = unit.name if isinstance(unit.display_name, SavedUnitName) else unit.display_name
    return [sys.executable, str(ADD_UNIT), "--production", str(unit.production.doc), name,
            "--resume", unit.session_id, "--cwd", str(resume_folder(unit)), *production_units.plan_arguments(unit.plan)]


def make_worktree(unit: RetiredUnit) -> None:
    saved = unit.saved_branch
    ref_on_origin = False
    restored_worktree = False
    if isinstance(saved, retire_unit.SavedBranch):
        remote = retire_unit.run(["git", "-C", str(unit.production.checkout), "ls-remote", "--exit-code",
                                  "origin", saved.ref])
        if remote.returncode == 0:
            ref_on_origin = True
            if remote.stdout.splitlines() != [f"{saved.tip}\t{saved.ref}"]:
                raise Refused(f"{saved.ref} no longer holds {saved.tip}")
            fetched = retire_unit.run(["git", "-C", str(unit.production.checkout), "fetch", "origin", saved.ref])
            if fetched.returncode or _git(unit, "rev-parse", "FETCH_HEAD") != saved.tip:
                raise Refused(f"cannot fetch {saved.ref} at {saved.tip}: {retire_unit.failure(fetched)}")
        elif remote.returncode != 2:
            raise Refused(f"cannot read {saved.ref}: {retire_unit.failure(remote)}")
        # After ref deletion the relaunched director may already have committed.
        restored_worktree = remote.returncode == 2 and has_worktree(unit)
        branch_tip = _git(unit, "rev-parse", "--verify", f"refs/heads/{unit.branch}")
        descendant = (restored_worktree and retire_unit.run([
            "git", "-C", str(unit.production.checkout), "merge-base", "--is-ancestor",
            saved.tip, branch_tip]).returncode == 0)
        if branch_tip and branch_tip != saved.tip and not descendant:
            raise Refused(f"branch {unit.branch} is at {branch_tip}, expected saved tip {saved.tip}")
        if remote.returncode == 2 and not branch_tip:
            raise Refused(f"{saved.ref} is missing and branch {unit.branch} has not been restored")
    if not has_worktree(unit):
        command = ["git", "-C", str(unit.production.checkout), "worktree", "add", str(unit.worktree)]
        if isinstance(saved, retire_unit.SavedBranch):
            command += ([unit.branch] if _git(unit, "branch", "--list", unit.branch)
                        else ["-b", unit.branch, saved.tip])
        else:
            command += ["-b", unit.branch, unit.production.merge_branch]
        result = retire_unit.run(command)
        if result.returncode:
            raise Refused(f"worktree {unit.worktree}: {retire_unit.failure(result)}")
        source = saved.tip if isinstance(saved, retire_unit.SavedBranch) else unit.production.merge_branch
        print(f"made: worktree {unit.worktree} on {unit.branch}, from {source}")
    if isinstance(saved, retire_unit.SavedBranch):
        head = retire_unit.run(["git", "-C", str(unit.worktree), "rev-parse", "HEAD"])
        descendant = (restored_worktree and retire_unit.run([
            "git", "-C", str(unit.worktree), "merge-base", "--is-ancestor",
            saved.tip, "HEAD"]).returncode == 0)
        if head.returncode or (head.stdout.strip() != saved.tip and not descendant):
            raise Refused(f"worktree {unit.worktree} is not at saved tip {saved.tip}")
        if ref_on_origin:
            deleted = retire_unit.run(["git", "-C", str(unit.production.checkout), "push",
                                      f"--force-with-lease={saved.ref}:{saved.tip}", "origin", f":{saved.ref}"])
            if deleted.returncode:
                raise Refused(f"cannot delete restored {saved.ref}: {retire_unit.failure(deleted)}")
            print(f"restored: {saved.ref} at {saved.tip}; origin ref removed")


def launch(unit: RetiredUnit) -> None:
    if is_running(unit) and not add_unit.pending_opening(unit.production, unit.name, unit.session_id):
        return
    result = retire_unit.run(launch_command(unit))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode:
        raise Refused(f"add_unit.py --resume: {retire_unit.failure(result)}")
    print(f"resumed: session {unit.session_id} in {resume_folder(unit)}")


def unmark(unit: RetiredUnit) -> None:
    """Take the retired note out of the Units row's Plan cell and commit the doc on the merge branch."""
    doc = unit.production.doc
    pending = resume_record(unit.production, unit.name)
    lines = doc.read_text(encoding="utf-8").split("\n")
    headings = production_units.unit_headings(lines)
    _, rows = production_units.unit_rows(lines)
    plan_at = headings.index("Plan") + 1
    for index, line in enumerate(lines):
        parts = line.split("|")
        if line in rows and production_units.cell_value(parts[headings.index("Unit") + 1].strip()) == unit.name:
            if not pending.exists():
                pending.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(pending, parts[plan_at].strip())
            parts[plan_at] = f" {RETIRED.sub('', parts[plan_at].strip())} "
            lines[index] = "|".join(parts)
            break
    write_text_atomic(doc, "\n".join(lines))
    if add_unit.commit_paths(unit.production, [doc], f"production({unit.production.slug}): resume {unit.name}"):
        print(f"unmarked: the Units row, committed on {unit.production.merge_branch}")


def dry_run(unit: RetiredUnit) -> int:
    print(f"resume {unit.name} of {unit.production.slug}: dry run, nothing changes")
    refused = refusals(unit)
    print("\n".join(f"refused: {reason}" for reason in refused))
    if refused:
        return 1
    if not has_worktree(unit):
        source = (unit.saved_branch.tip if isinstance(unit.saved_branch, retire_unit.SavedBranch)
                  else unit.production.merge_branch)
        print(f"would make: worktree {unit.worktree} on {unit.branch}, from {source}")
    print(f"would resume: session {unit.session_id} through add_unit.py --resume --cwd {resume_folder(unit)}")
    print(f"would unmark: the Units row, committed on {unit.production.merge_branch}")
    return 1 if refused else 0


def resumed_session_proven(slug: str, name: str, session_id: str, row: staff.StaffRow) -> bool:
    marked = unit_lookup.marked_unit(slug, name)
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        return False
    entry = pane_record.read(marked.place.session).get(marked.place.pane)
    return (marked.claude.session_id == session_id and entry is not None
            and entry["session_id"] == row.id and session_id == row.current_session)


def resume(unit: RetiredUnit, record: staff.StaffSnapshot) -> int:
    refused = refusals(unit)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    member = staff_sources.member(staff_sources.UnitMember(unit.production.slug, unit.name), record)
    if isinstance(member, staff_sources.NoSuchMember):
        raise Refused(f"{unit.name}: no retained staff row")
    if isinstance(unit.recovery, unit_config.UnitConfig):
        _ = unit_config.write_parked(member.row.id, unit.recovery)
    make_worktree(unit)
    unit_config.restore(member.row.id, unit.worktree)
    row = staff.restore_unit(member.row.id, staff.PresentWorktree(remote.local_machine(), unit.worktree))
    launch(unit)
    if not resumed_session_proven(unit.production.slug, unit.name, unit.session_id, row):
        raise Refused(f"{unit.name}: resumed native session identity is not proven; parked facts kept")
    unmark(unit)
    print(f"{unit.name} resumed")
    completed = resume_record(unit.production, unit.name).with_suffix(".done")
    completion_receipt.write_receipt(completed, row.id, {"session_id": unit.session_id,
                                                   "doc": str(unit.production.doc), "unit": unit.name})
    resume_record(unit.production, unit.name).unlink(missing_ok=True)
    unit_config.discard_parked(row.id)
    return 0


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        record = staff.initial_snapshot()
        unit = find_unit(cast(str, options.slug), cast(str, options.unit), record=record)
        completed = completion_receipt.read_receipt(resume_record(unit.production, unit.name).with_suffix(".done"),
                                                record)
        if (not unit.retired and isinstance(completed, completion_receipt.OwnedReceipt)
                and completed.owner.kind == "unit" and completed.fields.get("doc") == str(unit.production.doc)
                and completed.fields.get("unit") == unit.name
                and isinstance(completed.owner.worktree, staff.PresentWorktree)
                and isinstance(completed.fields.get("session_id"), str)
                and resumed_session_proven(unit.production.slug, unit.name,
                    cast(str, completed.fields["session_id"]), completed.owner)):
            print(f"{unit.name} already resumed")
            return 0
        return dry_run(unit) if cast(bool, options.dry_run) else resume(unit, record)
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except BackfillRequired as error:
        print(str(error))
        return 1
    except (Refused, retire_unit.Refused, production_units.Refusal, unit_names.BadName, ValueError,
            conf.Unusable, subprocess.SubprocessError, OSError) as error:
        print(f"refused: {error}")
        return 1


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
