#!/usr/bin/env python3
"""Restore a retired unit's worktree and resume its staff session.

Usage: resume_unit.py <production_name> <unit> [--dry-run]
Staff supplies the folder, branch, and session id. The parked configuration supplies the plan.
A preview changes no files. A repeated command preserves completed steps.
Exit 0 on success, 1 on refusal or failure, and 2 on invalid arguments.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import completion_receipt, staff, staff_sources, unit_config, worker
from scripts.shutdown import remote
from scripts.lib import conf
from scripts.message import sessions

from scripts.production import add_unit
from scripts.production import retire_unit
from scripts.production import unit_lookup
from scripts.production import unit_names
from scripts.shutdown import pane_record
from scripts.production.production_doc import Production, Refusal

ADD_UNIT = Path(__file__).resolve().parent / "add_unit.py"


class Refused(Exception):
    pass


class BackfillRequired(Refused):
    """The configuration must be filled before preview or apply."""


@dataclass(frozen=True)
class NoRecoveryRequired:
    production: Production
    name: str


@dataclass(frozen=True)
class RetiredUnitRecovery:
    production: Production
    name: str
    worktree: Path
    branch: str
    plan: unit_config.PlanFile | unit_config.StandbyPlan | unit_config.PermanentPlan
    session_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise Refused(f"{self.name}: recovery requires a session id")


def find_unit(production_name: str, given: str, *, record: staff.StaffSnapshot | None = None) -> RetiredUnitRecovery | NoRecoveryRequired:
    record = staff.initial_snapshot() if record is None else record
    production = retire_unit.production_of(production_name, record=record)
    row = record.by_id(staff.StaffId(given))
    member = (staff_sources.MemberFound(row) if row is not None and row.kind == "unit" else
              staff_sources.member(staff_sources.UnitMember(production.name, given), record))
    name = member.row.name if isinstance(member, staff_sources.MemberFound) else given
    if isinstance(member, staff_sources.NoSuchMember):
        raise Refused(f"{name}: no staff row")
    location = member.row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise Refused(f"{name}: staff worktree is unfilled")
    if location.machine != remote.local_machine():
        raise Refused(f"{name}: staff worktree belongs to {location.machine}; run resume there")
    worktree = location.path
    saved = unit_config.read_parked(member.row.id, cleanup=False)
    current = unit_config.read(worktree, cleanup=False)
    if isinstance(saved, unit_config.ConfigMissing) and isinstance(current, unit_config.ConfigMissing):
        raise BackfillRequired(f"{name}: no .unit.toml in {worktree} or ~/.local/state/staff/units/{member.row.id}.toml; "
                               + "ask the showrunner")
    if isinstance(saved, unit_config.ConfigMissing):
        if isinstance(location, staff.RemovedWorktree) or not isinstance(current, unit_config.UnitConfig):
            raise Refused(f"{name}: parked retirement facts are missing")
        return NoRecoveryRequired(production, name)
    pair = unit_config.place(member.row.id, worktree, cleanup=False)
    if isinstance(pair, unit_config.Both) and not pair.same:
        raise Refused(f"{pair.worktree} and {pair.parked}: configurations differ; no file was changed")
    return RetiredUnitRecovery(production, name, worktree, member.row.name, saved.plan, member.row.current_session)


def resume_record(production: Production, name: str) -> Path:
    result = retire_unit.run(["git", "-C", str(production.checkout), "rev-parse", "--git-path",
                              f"resume-units/{name}"])
    if result.returncode:
        raise Refused(f"cannot find the resume record: {retire_unit.failure(result)}")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else production.checkout / path


def _git(unit: RetiredUnitRecovery, *arguments: str) -> str:
    result = retire_unit.run(["git", "-C", str(unit.production.checkout), *arguments])
    return result.stdout.strip() if result.returncode == 0 else ""


def has_worktree(unit: RetiredUnitRecovery) -> bool:
    """Whether the worktree path holds the unit's worktree on its branch."""
    return ((unit.worktree / ".git").is_file()
            and retire_unit.run(["git", "-C", str(unit.worktree), "branch", "--show-current"]).stdout.strip()
            == unit.branch)


def has_transcript(unit: RetiredUnitRecovery) -> bool:
    return bool(sessions.transcript_locations(unit.session_id))


def is_running(unit: RetiredUnitRecovery) -> bool:
    """Whether the unit's mark is on a live Claude that is the session it resumes."""
    try:
        found = unit_lookup.labeled_session(unit.production.name, unit.name)
    except OSError as error:
        raise Refused(f"{unit.name}: cannot tell whether Claude is running: {error}") from error
    return (found is not None and isinstance(found.claude, unit_lookup.LiveClaude)
            and found.claude.session_id == unit.session_id)


def refusals(unit: RetiredUnitRecovery) -> list[str]:
    refused: list[str] = []
    try:
        if not has_transcript(unit):
            refused.append(f"{unit.name}: no account has the transcript of session {unit.session_id}")
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


def launch_command(unit: RetiredUnitRecovery) -> list[str]:
    # The row name is both the branch and the add_unit name.
    return [sys.executable, str(ADD_UNIT), "--production", unit.production.name, unit.branch,
            "--resume", unit.session_id, "--cwd", str(unit.worktree), *unit_config.plan_arguments(unit.plan)]


def make_worktree(unit: RetiredUnitRecovery) -> None:
    if has_worktree(unit):
        return
    command = ["git", "-C", str(unit.production.checkout), "worktree", "add", str(unit.worktree)]
    command += ([unit.branch] if _git(unit, "branch", "--list", unit.branch) else
                ["-b", unit.branch, unit.production.merge_branch])
    result = retire_unit.run(command)
    if result.returncode:
        raise Refused(f"worktree {unit.worktree}: {retire_unit.failure(result)}")
    print(f"made: worktree {unit.worktree} on {unit.branch}, from {unit.production.merge_branch}")


def launch(unit: RetiredUnitRecovery) -> None:
    if is_running(unit) and not add_unit.pending_opening(unit.production, unit.name, unit.session_id):
        return
    result = retire_unit.run(launch_command(unit))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode:
        raise Refused(f"add_unit.py --resume: {retire_unit.failure(result)}")
    print(f"resumed: session {unit.session_id} in {unit.worktree}")


def dry_run(unit: RetiredUnitRecovery) -> int:
    print(f"resume {unit.name} of {unit.production.name}: dry run, nothing changes")
    refused = refusals(unit)
    print("\n".join(f"refused: {reason}" for reason in refused))
    if refused:
        return 1
    if not has_worktree(unit):
        source = unit.production.merge_branch
        print(f"would make: worktree {unit.worktree} on {unit.branch}, from {source}")
    print(f"would resume: session {unit.session_id} through add_unit.py --resume --cwd {unit.worktree}")
    return 1 if refused else 0


def resumed_session_proven(production_name: str, name: str, session_id: str, row: staff.StaffRow) -> bool:
    marked = unit_lookup.labeled_session(production_name, name)
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        return False
    entry = pane_record.read(marked.place.session).get(marked.place.pane)
    return (marked.claude.session_id == session_id and entry is not None
            and entry["session_id"] == row.id and session_id == row.current_session)


def resume(unit: RetiredUnitRecovery, record: staff.StaffSnapshot) -> int:
    refused = refusals(unit)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    member = staff_sources.member(staff_sources.UnitMember(unit.production.name, unit.name), record)
    if isinstance(member, staff_sources.NoSuchMember):
        raise Refused(f"{unit.name}: no retained staff row")
    make_worktree(unit)
    unit_config.restore(member.row.id, unit.worktree)
    row = staff.restore_unit(member.row.id, staff.PresentWorktree(remote.local_machine(), unit.worktree))
    launch(unit)
    if not resumed_session_proven(unit.production.name, unit.name, unit.session_id, row):
        raise Refused(f"{unit.name}: resumed native session identity is not proven; parked facts kept")
    completed = resume_record(unit.production, unit.name).with_suffix(".done")
    completion_receipt.write_receipt(completed, row.id, {"session_id": unit.session_id,
                                                   "doc": str(unit.production.doc), "unit": unit.name})
    unit_config.discard_parked(row.id)
    code = worker.restore(row.id)
    print(f"{unit.name} resumed")
    return int(code)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("production_name")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        record = staff.initial_snapshot()
        unit = find_unit(cast(str, options.production_name), cast(str, options.unit), record=record)
        completed = completion_receipt.read_receipt(resume_record(unit.production, unit.name).with_suffix(".done"),
                                                record)
        if (isinstance(unit, NoRecoveryRequired) and isinstance(completed, completion_receipt.OwnedReceipt)
                and completed.owner.kind == "unit" and completed.fields.get("doc") == str(unit.production.doc)
                and completed.fields.get("unit") == unit.name
                and isinstance(completed.owner.worktree, staff.PresentWorktree)
                and isinstance(completed.fields.get("session_id"), str)
                and resumed_session_proven(unit.production.name, unit.name,
                    cast(str, completed.fields["session_id"]), completed.owner)):
            code = worker.ExitCode.OK if cast(bool, options.dry_run) else worker.restore(completed.owner.id)
            print(f"{unit.name} already resumed")
            return int(code)
        if isinstance(unit, NoRecoveryRequired):
            raise Refused(f"{unit.name} is not retired")
        return dry_run(unit) if cast(bool, options.dry_run) else resume(unit, record)
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except BackfillRequired as error:
        print(str(error))
        return 1
    except (Refused, retire_unit.Refused, Refusal, unit_names.BadName, ValueError,
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
