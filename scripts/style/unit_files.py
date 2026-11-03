"""Find files held or changed by production unit worktrees."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

from git_process import GitExitAnswers, run_git

import style_ledger
from scripts.production import staff, staff_sources, unit_lookup, session_staff
from production_units import cell_value, production_field, unit_table


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
    lines = doc.read_text().splitlines()
    sources = unit_lookup.member_sources()
    slug = unit_lookup.production_slug_for_document(doc, record, sources)
    branch = production_field(lines, "Merge branch")
    result: list[UnitWorktree] = []
    for row in unit_table(lines):
        unit = cell_value(row["Unit"])
        try:
            selected = sources.member(staff_sources.UnitMember(slug, unit), record)
            location = unit_lookup.local_worktree(selected)
            if isinstance(location, staff.RemovedWorktree):
                continue
            result.append(UnitWorktree(unit, cell_value(row["Branch"]),
                                       unit_lookup.plan_worktree(row, selected), branch))
        except (staff.StaffUnreadable, staff.StaffRefused) as error:
            print(f"style: {unit}: {error}", file=sys.stderr)
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
