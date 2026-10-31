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
start folder is outside the worktree, resume uses --cwd there and skips worktree and branch steps.
When the note has no session id, the unit's mark, newest retained shutdown record or interactive
transcript supplies it; no evidence refuses both a dry run and a real run.

Then it makes the worktree again at the same path, on the branch, from the merge branch; resumes the
session through `add_unit.py --resume` (the units account, the unit's name and Remote Control); and
takes the retired note out of the Plan cell, committed on the merge branch. Each step checks whether
it is done, so a rerun after a failure resumes: the note goes last, because it holds the session id.
--dry-run prints each refusal and each step, and changes nothing.

Exit 0 when the unit is resumed; 1 on a refusal or a failed step; 2 on usage.
"""
from __future__ import annotations


import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import completion_receipt, staff

import add_unit
import production_units
import retire_unit
import unit_lookup
import unit_move
import unit_names
from production_units import Production
from shutdown_record import InvalidRecord
from atomic_file import write_text_atomic

ADD_UNIT = Path(__file__).resolve().parent / "add_unit.py"
RETIRED = re.compile(r"^\s*\(retired [^)]*\)\s*")
SESSION = re.compile(r"^\s*\(retired [^)]*, session ([0-9A-Za-z-]+)(?:, folder ([^)]*))?\)")


class Refused(Exception):
    pass


@dataclass(frozen=True)
class RecordedStartFolder:
    path: Path


@dataclass(frozen=True)
class UnrecordedStartFolder:
    pass


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


def find_unit(slug: str, given: str) -> RetiredUnit:
    production = retire_unit.production_of(slug)
    rows = production_units.unit_table(production.doc.read_text(encoding="utf-8").splitlines())
    ids = [production_units.cell_value(row.get("Unit", "")) for row in rows]
    name = given if given in ids else unit_names.unit_id(given)
    matching = [row for row, unit in zip(rows, ids, strict=True) if unit == name]
    if len(matching) != 1:
        raise Refused(f"{name}: expected one Units row, found {len(matching)}")
    row = matching[0]
    plan = row.get("Plan", "")
    pending = resume_record(production, name)
    if not production_units.plan_cell_is_retired(plan) and pending.exists():
        retired_plan = pending.read_text(encoding="utf-8")
        if RETIRED.sub("", retired_plan).strip() == plan.strip():
            plan = retired_plan
    session = SESSION.match(plan)
    worktree = Path(production_units.cell_value(row.get("Worktree", ""))).expanduser()
    branch = production_units.cell_value(row.get("Branch", ""))
    retired = production_units.plan_cell_is_retired(plan)
    session_id = session[1] if session else ""
    if retired and not session_id:
        session_id = retire_unit.last_session(retire_unit.Unit(production, name, worktree, branch, retired))
        if not session_id:
            raise Refused(f"{name}: its Units row names no session to resume; no director session was found")
    return RetiredUnit(production, name, worktree, branch, production_units.cell_value(RETIRED.sub("", plan)),
                       session_id, retired,
                       RecordedStartFolder(Path(session[2]).expanduser()) if session and session[2] else UnrecordedStartFolder())


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
        if not has_transcript(unit):
            refused.append(f"{unit.name}: no account has the transcript of session {unit.session_id} under {folder}")
        if folder.resolve() != unit.worktree.resolve():
            return refused
    except (retire_unit.Refused, OSError) as error:
        refused.append(f"{unit.name}: {error}")
    if unit.worktree.exists() and not has_worktree(unit):
        refused.append(f"{unit.worktree} exists and is not {unit.name}'s worktree on {unit.branch}")
    elif not unit.worktree.exists():
        if _git(unit, "branch", "--list", unit.branch):
            refused.append(f"branch {unit.branch} still exists; retire did not finish")
        if _git(unit, "ls-remote", "--heads", "origin", unit.branch):
            refused.append(f"origin {unit.branch} still exists; retire did not finish")
    return refused


def launch_command(unit: RetiredUnit) -> list[str]:
    if not unit.session_id:
        raise Refused(f"{unit.name}: its Units row names no session to resume")
    return [sys.executable, str(ADD_UNIT), "--production", str(unit.production.doc), unit.name,
            "--resume", unit.session_id, "--cwd", str(resume_folder(unit)), *production_units.plan_arguments(unit.plan)]


def make_worktree(unit: RetiredUnit) -> None:
    if has_worktree(unit):
        return
    result = retire_unit.run(["git", "-C", str(unit.production.checkout), "worktree", "add", str(unit.worktree),
                              "-b", unit.branch, unit.production.merge_branch])
    if result.returncode:
        raise Refused(f"worktree {unit.worktree}: {retire_unit.failure(result)}")
    print(f"made: worktree {unit.worktree} on {unit.branch}, from {unit.production.merge_branch}")


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
    add_unit.commit_paths(unit.production, [doc], f"production({unit.production.slug}): resume {unit.name}")
    print(f"unmarked: the Units row, committed on {unit.production.merge_branch}")


def dry_run(unit: RetiredUnit) -> int:
    print(f"resume {unit.name} of {unit.production.slug}: dry run, nothing changes")
    refused = refusals(unit)
    print("\n".join(f"refused: {reason}" for reason in refused))
    if refused:
        return 1
    if resume_folder(unit).resolve() != unit.worktree.resolve():
        print(f"would skip: worktree and branch steps; session starts in {resume_folder(unit)}")
    elif not has_worktree(unit):
        print(f"would make: worktree {unit.worktree} on {unit.branch}, from {unit.production.merge_branch}")
    print(f"would resume: session {unit.session_id} through add_unit.py --resume --cwd {resume_folder(unit)}")
    print(f"would unmark: the Units row, committed on {unit.production.merge_branch}")
    return 1 if refused else 0


def resume(unit: RetiredUnit) -> int:
    refused = refusals(unit)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    row = add_unit.ensure_staff_unit(unit.production, unit.name)
    if resume_folder(unit).resolve() == unit.worktree.resolve():
        make_worktree(unit)
    launch(unit)
    unmark(unit)
    print(f"{unit.name} resumed")
    completed = resume_record(unit.production, unit.name).with_suffix(".done")
    completion_receipt.write_receipt(completed, row.id, {"session_id": unit.session_id,
                                                   "doc": str(unit.production.doc), "unit": unit.name})
    resume_record(unit.production, unit.name).unlink(missing_ok=True)
    return 0


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        unit = find_unit(cast(str, options.slug), cast(str, options.unit))
        completed = completion_receipt.read_receipt(resume_record(unit.production, unit.name).with_suffix(".done"),
                                                staff.initial_snapshot())
        if (not unit.retired and isinstance(completed, completion_receipt.OwnedReceipt)
                and completed.owner.kind == "unit" and completed.fields.get("doc") == str(unit.production.doc)
                and completed.fields.get("unit") == unit.name):
            print(f"{unit.name} already resumed")
            return 0
        return dry_run(unit) if cast(bool, options.dry_run) else resume(unit)
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except (Refused, retire_unit.Refused, production_units.Refusal, unit_names.BadName, InvalidRecord, OSError) as error:
        print(f"refused: {error}")
        return 1


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
