"""Find files held or changed by production unit worktrees."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

from git_process import GitExitAnswers, run_git

import style_ledger
from scripts.production import session_staff, staff, unit_lookup, unit_names
from scripts.production.production_doc import production_field


@dataclass(frozen=True)
class UnitWorktree:
    unit: str
    branch: str
    worktree: Path
    merge_branch: str


def git(repo: Path, *arguments: str) -> str:
    return run_git(("git", "-C", str(repo), *arguments)).stdout.decode().replace("\r\n", "\n").replace("\r", "\n")


def file_text(repo: Path, commit: str, path: str) -> str:
    """Read a Git blob, replacing non-UTF-8 bytes with U+FFFD."""
    return run_git(("git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}")).stdout.decode(errors="replace")


def unit_worktrees(doc: Path, *,
                   record: staff.StaffSnapshot | session_staff.ReadCurrentStaff = session_staff.ReadCurrentStaff.CURRENT) -> tuple[UnitWorktree, ...]:
    record = staff.snapshot() if isinstance(record, session_staff.ReadCurrentStaff) else record
    branch = production_field(doc.read_text().splitlines(), "Merge branch")
    resolver = session_staff.RowResolver(record)
    result: list[UnitWorktree] = []
    for row in unit_lookup.doc_units(doc, record):
        unit = unit_names.unit_id(row.name)
        try:
            location = unit_lookup.local_worktree(session_staff.MemberFound(row))
        except staff.StaffUnreadable as error:
            print(f"style: {unit}: {error}", file=sys.stderr)
            continue
        if isinstance(location, staff.RemovedWorktree):
            continue
        match resolver.unit_branch(row):
            case session_staff.Branch(name):
                result.append(UnitWorktree(unit, name, location.path, branch))
            case problem:
                print(f"style: {unit}: {problem.reason}", file=sys.stderr)
    return tuple(result)


def unit_file_holders(repo: Path, doc: Path, ledger: style_ledger.Ledger, own_unit: str = "") -> dict[str, frozenset[str]]:
    lines = doc.read_text().splitlines()
    parent = Path(production_field(lines, "Parent production")).expanduser()
    record = staff.snapshot()
    units = (*unit_worktrees(parent, record=record), *unit_worktrees(doc, record=record))
    holders: dict[str, frozenset[str]] = {}
    for row in units:
        busy: set[str] = set()
        exists = run_git(("git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                          "refs/heads/" + row.branch.removeprefix("refs/heads/")),
                         answers=GitExitAnswers(frozenset({0, 1})))
        if exists.returncode == 0:
            busy.update(filter(None, git(repo, "diff", "--name-only", "--no-renames", "-z", f"{row.merge_branch}...{row.branch}").split("\0")))
        if (row.worktree / ".git").exists():
            busy.update(changed_files(row.worktree))
        if row.unit != own_unit:
            for ticket in ledger.ticket_open(row.unit):
                busy.update(ticket["files"])
        holders[row.unit] = holders.get(row.unit, frozenset()) | frozenset(busy)
    return holders


def changed_files(repo: Path) -> frozenset[str]:
    entries = iter(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0"))
    paths: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        paths.add(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            paths.add(next(entries))
    return frozenset(paths)
