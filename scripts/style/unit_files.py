"""Find files held or changed by production unit worktrees."""

from __future__ import annotations

import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

from git_process import GitExitAnswers, run_git

import style_ledger
from scripts.production import session_staff, staff, unit_lookup, unit_names


@dataclass(frozen=True)
class UnitWorktree:
    unit: str
    branch: str
    worktree: Path
    merge_branch: str


@dataclass(frozen=True)
class UnitFilesWithoutBranches:
    unit: str
    worktree: Path


@dataclass(frozen=True)
class UnitTicketsOnly:
    unit: str


def git(repo: Path, *arguments: str) -> str:
    return run_git(("git", "-C", str(repo), *arguments)).stdout.decode().replace("\r\n", "\n").replace("\r", "\n")


def file_text(repo: Path, commit: str, path: str) -> str:
    """Read a Git blob, replacing non-UTF-8 bytes with U+FFFD."""
    return run_git(("git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}")).stdout.decode(errors="replace")


def unit_worktrees(doc: Path, *,
                   record: staff.StaffSnapshot | session_staff.ReadCurrentStaff = session_staff.ReadCurrentStaff.CURRENT) -> tuple[UnitWorktree, ...]:
    record = staff.snapshot() if isinstance(record, session_staff.ReadCurrentStaff) else record
    resolver = session_staff.RowResolver(record)
    return tuple(row for row in _unit_file_sources(unit_lookup.doc_owner(doc, record), resolver)
                 if isinstance(row, UnitWorktree))


def _unit_file_sources(owner: staff.StaffRow, resolver: session_staff.RowResolver
                       ) -> tuple[UnitWorktree | UnitFilesWithoutBranches | UnitTicketsOnly, ...]:
    result: list[UnitWorktree | UnitFilesWithoutBranches | UnitTicketsOnly] = []
    for row in resolver.record.find(kind="unit", parent=owner.id):
        unit = unit_names.unit_id(row.name)
        if isinstance(row.worktree, staff.RemovedWorktree):
            continue
        try:
            location = unit_lookup.local_worktree(session_staff.MemberFound(row))
        except staff.StaffUnreadable as error:
            _unit_problem(unit, str(error))
            result.append(UnitTicketsOnly(unit))
            continue
        if isinstance(location, staff.RemovedWorktree):
            continue
        branch = resolver.merge_branch(row)
        if isinstance(branch, session_staff.BranchUnreadable):
            _unit_problem(unit, branch.reason)
            result.append(UnitFilesWithoutBranches(unit, location.path))
            continue
        match resolver.unit_branch(row):
            case session_staff.Branch(name):
                result.append(UnitWorktree(unit, name, location.path, branch.name))
            case problem:
                _unit_problem(unit, problem.reason)
                result.append(UnitFilesWithoutBranches(unit, location.path))
    return tuple(result)


def _print_unit_problem(unit: str, reason: str) -> None:
    print(f"style: {unit}: {' '.join(reason.splitlines())}", file=sys.stderr)


_problem_reporter: ContextVar[Callable[[str, str], None]] = ContextVar("unit_problem_reporter", default=_print_unit_problem)


@contextmanager
def report_unit_problems_once() -> Generator[None]:
    """Report each unit problem once during one command."""
    reported: set[tuple[str, str]] = set()

    def report(unit: str, reason: str) -> None:
        problem = (unit, ' '.join(reason.splitlines()))
        if problem not in reported:
            reported.add(problem)
            _print_unit_problem(*problem)

    token = _problem_reporter.set(report)
    try:
        yield
    finally:
        _problem_reporter.reset(token)


def _unit_problem(unit: str, reason: str) -> None:
    _problem_reporter.get()(unit, reason)


def unit_file_holders(repo: Path, doc: Path, ledger: style_ledger.Ledger, own_unit: str = "") -> dict[str, frozenset[str]]:
    record = staff.snapshot()
    resolver = session_staff.RowResolver(record)
    owner = unit_lookup.doc_owner(doc, record)
    parent = record.by_id(owner.parent) if owner.parent else None
    units = _unit_file_sources(owner, resolver)
    if parent is not None:
        units = (*_unit_file_sources(parent, resolver), *units)
    holders: dict[str, frozenset[str]] = {}
    for row in units:
        busy: set[str] = set()
        if isinstance(row, UnitWorktree) and "HEAD" in (row.branch, row.merge_branch):
            source = "unit" if row.branch == "HEAD" else "production"
            _unit_problem(row.unit, f"the {source} worktree has a detached HEAD")
        elif isinstance(row, UnitWorktree):
            exists = run_git(("git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                              "refs/heads/" + row.branch.removeprefix("refs/heads/")),
                             answers=GitExitAnswers(frozenset({0, 1})))
            if exists.returncode == 0:
                busy.update(filter(None, git(repo, "diff", "--name-only", "--no-renames", "-z", f"{row.merge_branch}...{row.branch}").split("\0")))
        if not isinstance(row, UnitTicketsOnly) and (row.worktree / ".git").exists():
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
