#!/usr/bin/env python3
"""Resume one retired unit: make its worktree again and resume the session its director last used.

Usage: resume_unit.py <slug> <unit> [--dry-run]

A showrunner runs it through /showrunner:resume_unit. retire_unit.py leaves the unit's Units row with
"(retired <date>, worktree removed, session <id>)" in front of its Plan cell, and the row still names
the worktree path, the branch and the plan. resume_unit refuses, and changes nothing, when the row is
not retired or names no session, when no Claude config dir holds that session's transcript under the
worktree's project folder (`claude --resume` finds a transcript only there), when a folder at the
worktree path is not the unit's worktree, or when the branch is still here or on origin without it.
Every account links its `projects` folder to the shared one, so a units account that moved since the
retire reads the same transcript.

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

import add_unit
import production_units
import retire_unit
import unit_lookup
import unit_move
import unit_names
from add_unit import Production

ADD_UNIT = Path(__file__).resolve().parent / "add_unit.py"
RETIRED = re.compile(r"^\s*\(retired [^)]*\)\s*")
SESSION = re.compile(r"^\s*\(retired [^)]*, session ([0-9A-Za-z-]+)\)")


class Refused(Exception):
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


def find_unit(slug: str, given: str) -> RetiredUnit:
    production = retire_unit.production_of(slug)
    rows = production_units.unit_table(production.doc.read_text(encoding="utf-8").splitlines())
    ids = [production_units.cell_value(row.get("Unit", "")) for row in rows]
    name = given if given in ids else unit_names.unit_id(given)
    matching = [row for row, unit in zip(rows, ids, strict=True) if unit == name]
    if len(matching) != 1:
        raise Refused(f"{name}: expected one Units row, found {len(matching)}")
    row = matching[0]
    plan = production_units.cell_value(row.get("Plan", ""))
    session = SESSION.match(plan)
    return RetiredUnit(production, name, Path(production_units.cell_value(row.get("Worktree", ""))).expanduser(),
                       production_units.cell_value(row.get("Branch", "")), RETIRED.sub("", plan),
                       session[1] if session else "", production_units.plan_cell_is_retired(plan))


def _git(unit: RetiredUnit, *arguments: str) -> str:
    result = retire_unit.run(["git", "-C", str(unit.production.checkout), *arguments])
    return result.stdout.strip() if result.returncode == 0 else ""


def has_worktree(unit: RetiredUnit) -> bool:
    """Whether the worktree path holds the unit's worktree on its branch."""
    return ((unit.worktree / ".git").is_file()
            and retire_unit.run(["git", "-C", str(unit.worktree), "branch", "--show-current"]).stdout.strip()
            == unit.branch)


def has_transcript(unit: RetiredUnit) -> bool:
    folder = unit_move.project_folder(str(unit.worktree))
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
    if not has_transcript(unit):
        refused.append(f"{unit.name}: no account has the transcript of session {unit.session_id} "
                       + f"under {unit.worktree}")
    if unit.worktree.exists() and not has_worktree(unit):
        refused.append(f"{unit.worktree} exists and is not {unit.name}'s worktree on {unit.branch}")
    elif not unit.worktree.exists():
        if _git(unit, "branch", "--list", unit.branch):
            refused.append(f"branch {unit.branch} still exists; retire did not finish")
        if _git(unit, "ls-remote", "--heads", "origin", unit.branch):
            refused.append(f"origin {unit.branch} still exists; retire did not finish")
    return refused


def plan_arguments(unit: RetiredUnit) -> list[str]:
    return [f"--{unit.plan}"] if unit.plan in ("standby", "permanent") else ["--plan", unit.plan]


def launch_command(unit: RetiredUnit) -> list[str]:
    return [sys.executable, str(ADD_UNIT), "--production", unit.production.slug, unit.name,
            "--resume", unit.session_id, "--cwd", str(unit.worktree), *plan_arguments(unit)]


def make_worktree(unit: RetiredUnit) -> None:
    if has_worktree(unit):
        return
    result = retire_unit.run(["git", "-C", str(unit.production.checkout), "worktree", "add", str(unit.worktree),
                              "-b", unit.branch, unit.production.merge_branch])
    if result.returncode:
        raise Refused(f"worktree {unit.worktree}: {retire_unit.failure(result)}")
    print(f"made: worktree {unit.worktree} on {unit.branch}, from {unit.production.merge_branch}")


def launch(unit: RetiredUnit) -> None:
    if is_running(unit):
        return
    result = retire_unit.run(launch_command(unit))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode:
        raise Refused(f"add_unit.py --resume: {retire_unit.failure(result)}")
    print(f"resumed: session {unit.session_id} in {unit.worktree}")


def unmark(unit: RetiredUnit) -> None:
    """Take the retired note out of the Units row's Plan cell and commit the doc on the merge branch."""
    doc = unit.production.doc
    lines = doc.read_text(encoding="utf-8").split("\n")
    headings = production_units.unit_headings(lines)
    _, rows = production_units.unit_rows(lines)
    plan_at = headings.index("Plan") + 1
    for index, line in enumerate(lines):
        parts = line.split("|")
        if line in rows and production_units.cell_value(parts[headings.index("Unit") + 1].strip()) == unit.name:
            parts[plan_at] = f" {RETIRED.sub('', parts[plan_at].strip())} "
            lines[index] = "|".join(parts)
            break
    _ = doc.write_text("\n".join(lines), encoding="utf-8")
    add_unit.commit_paths(unit.production, [doc], f"production({unit.production.slug}): resume {unit.name}")
    print(f"unmarked: the Units row, committed on {unit.production.merge_branch}")


def dry_run(unit: RetiredUnit) -> int:
    print(f"resume {unit.name} of {unit.production.slug}: dry run, nothing changes")
    refused = refusals(unit)
    print("\n".join(f"refused: {reason}" for reason in refused))
    if not has_worktree(unit):
        print(f"would make: worktree {unit.worktree} on {unit.branch}, from {unit.production.merge_branch}")
    print(f"would resume: session {unit.session_id} through add_unit.py --resume")
    print(f"would unmark: the Units row, committed on {unit.production.merge_branch}")
    return 1 if refused else 0


def resume(unit: RetiredUnit) -> int:
    refused = refusals(unit)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    make_worktree(unit)
    launch(unit)
    unmark(unit)
    print(f"{unit.name} resumed")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        unit = find_unit(cast(str, options.slug), cast(str, options.unit))
        return dry_run(unit) if cast(bool, options.dry_run) else resume(unit)
    except (Refused, retire_unit.Refused, production_units.Refusal, unit_names.BadName, OSError) as error:
        print(f"refused: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
