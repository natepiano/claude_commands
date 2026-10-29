"""Find files held or changed by production unit worktrees."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

import style_ledger
from production_units import read_production
from production_units import cell_value, plan_cell_is_retired, production_field, unit_table


@dataclass(frozen=True)
class UnitWorktree:
    unit: str
    branch: str
    worktree: Path
    merge_branch: str


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise style_ledger.CheckCouldNotRun(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def file_text(repo: Path, commit: str, path: str) -> str:
    """The text of the file at path in commit. Bytes that are not UTF-8 read as U+FFFD, so a latin-1 file never raises."""
    shown = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}"], capture_output=True, check=False)
    if shown.returncode:
        raise style_ledger.CheckCouldNotRun(shown.stderr.decode(errors="replace").strip() or f"git cat-file {commit}:{path} failed")
    return shown.stdout.decode(errors="replace")


def unit_worktrees(doc: Path) -> tuple[UnitWorktree, ...]:
    production = read_production(doc)
    rows = unit_table(doc.read_text().splitlines())
    return tuple(
        UnitWorktree(cell_value(row["Unit"]), cell_value(row["Branch"]),
                     Path(cell_value(row["Worktree"])).expanduser(), production.merge_branch)
        for row in rows if not plan_cell_is_retired(row.get("Plan", ""))
    )


def unit_file_holders(repo: Path, doc: Path, ledger: style_ledger.Ledger, own_unit: str = "") -> dict[str, frozenset[str]]:
    lines = doc.read_text().splitlines()
    parent = Path(production_field(lines, "Parent production")).expanduser()
    units = (*unit_worktrees(parent), *unit_worktrees(doc))
    holders: dict[str, frozenset[str]] = {}
    for row in units:
        busy: set[str] = set()
        exists = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                                 "refs/heads/" + row.branch.removeprefix("refs/heads/")], check=False)
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
