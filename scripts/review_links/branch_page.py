#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Print a short link for deleting merged unit branches from the local origin."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import html
import json
import re
import secrets
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from scripts.direct import plan_history
from scripts.agents import remote
from scripts.lib import atomic_file, unit_runs
from scripts.lib.process_failure import failure_detail
from scripts.production import session_staff, staff
from scripts.production.production_doc import Refusal, production_field
from scripts.review_links import change_page, review_link

MANIFEST = ".review-link-branches.json"
PAGE_NAME = "branches.html"


@dataclass(frozen=True)
class MergedUnitBranch:
    name: str
    unit: str
    tip: str
    commit_date: str
    merge: str
    merge_date: str


@dataclass(frozen=True)
class BranchPage:
    repository: Path
    rows: tuple[MergedUnitBranch, ...]
    token: str


@dataclass(frozen=True)
class DeleteResult:
    name: str
    message: str


class InitialSelection(Enum):
    ALL = "Select every branch on a new page"


@dataclass(frozen=True)
class FormerUnit:
    name: str
    worktree: Path
    branch: str


@dataclass(frozen=True)
class LatestUnitBranch:
    started_at: float
    branch: str


@dataclass(frozen=True)
class UnitBranchEvidence:
    units: dict[str, str]
    latest_by_owner: dict[staff.StaffId, LatestUnitBranch]


class RepositoryScope:
    """Identify this origin and its recorded local worktrees."""

    def __init__(self, repository: Path) -> None:
        self.common: str = git_text(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.origin: str = git_text(repository, "remote", "get-url", "origin")
        self.registered: dict[Path, str] = {}
        worktree = repository
        for field in git_text(repository, "worktree", "list", "--porcelain", "-z").split("\0"):
            if field.startswith("worktree "):
                worktree = Path(field.removeprefix("worktree "))
            elif field.startswith("branch refs/heads/"):
                self.registered[worktree] = field.removeprefix("branch refs/heads/")
        self.former_worktrees: set[Path] = set()
        self.matches: dict[Path, bool] = {}

    def contains(self, worktree: Path) -> bool:
        worktree = worktree.expanduser().resolve()
        if worktree in self.matches:
            return self.matches[worktree]
        common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode == 0:
            origin = git(worktree, "remote", "get-url", "origin")
            matches = (common.stdout.strip() == self.common
                       or (origin.returncode == 0 and origin.stdout.strip() == self.origin))
        else:
            matches = worktree in self.registered or worktree in self.former_worktrees
        self.matches[worktree] = matches
        return matches


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(repository), *arguments],
                          capture_output=True, text=True, check=False)


def git_text(repository: Path, *arguments: str) -> str:
    result = git(repository, *arguments)
    result.check_returncode()
    return result.stdout.strip()


def history_roots() -> tuple[Path, ...]:
    """Read both history names when a migration leaves records in both folders."""
    if unit_runs.environ("HISTORY_DIR"):
        return (unit_runs.history_root(),)
    state = unit_runs.state_dir()
    return tuple(dict.fromkeys((unit_runs.history_root(), state / unit_runs.HISTORY_NAME,
                                state / unit_runs.FORMER_HISTORY_NAME)))


def former_units(repository: Path, scope: RepositoryScope) -> tuple[FormerUnit, ...]:
    """Read former staff rows from the production documents' retained Units tables."""
    found = git(repository, "grep", "-l", "^## Units", "refs/remotes/origin/main", "--", "docs/plans")
    if found.returncode == 1:
        return ()
    found.check_returncode()
    units: list[FormerUnit] = []
    for source in found.stdout.splitlines():
        lines = git_text(repository, "show", source).splitlines()
        if not scope.contains(Path(production_field(lines, "Repository"))):
            continue
        in_units = False
        columns: list[str] = []
        for line in lines:
            if line.startswith("## "):
                in_units = line == "## Units"
                columns = []
            if not in_units or not line.startswith("|"):
                continue
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if not columns:
                columns = cells
                continue
            row = dict(zip(columns, cells))
            name, tree, branch = (row.get(key, "") for key in ("Unit", "Worktree", "Branch"))
            machine = row.get("Machine", remote.local_machine())
            if (machine == "natedev" and name and branch and branch != "—"
                    and tree.startswith("/")):
                units.append(FormerUnit(name, Path(tree).resolve(), branch))
    scope.former_worktrees.update(unit.worktree for unit in units)
    return tuple(units)


def recorded_branches(record: staff.StaffSnapshot, roots: tuple[Path, ...],
                      resolver: session_staff.RowResolver, scope: RepositoryScope,
                      former: tuple[FormerUnit, ...]) -> UnitBranchEvidence:
    """Join run branches to current staff or to the former staff worktree."""
    branches = {unit.branch: unit.name for unit in former if scope.contains(unit.worktree)}
    by_worktree = {unit.worktree: unit for unit in former}
    branch_started: dict[str, float] = {}
    latest: dict[staff.StaffId, LatestUnitBranch] = {}
    for root in roots:
        for path in sorted((root / "runs").glob("*.jsonl")):
            event = plan_history.run_started_event(path)
            if event is None:
                continue
            if event.get("skill") not in ("unit-history", "plan-delegate"):
                continue
            try:
                owner = unit_runs.run_owner(event, record)
            except staff.StaffUnreadable:
                owner = unit_runs.RunOwnerUnrecorded("the owner left staff")
            branch = event.get("branch")
            if not isinstance(branch, str) or not branch:
                continue
            stamp = event.get("run_started_at", event.get("timestamp_epoch", 0))
            started = float(stamp) if isinstance(stamp, (int, float)) else 0.0
            if isinstance(owner, staff.StaffRow):
                if owner.id not in latest or started > latest[owner.id].started_at:
                    latest[owner.id] = LatestUnitBranch(started, branch)
            directory = event.get("working_dir", event.get("worktree", ""))
            if isinstance(directory, str) and Path(directory).is_absolute():
                worktree = Path(directory).resolve()
            elif isinstance(owner, staff.StaffRow) and isinstance(owner.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
                worktree = owner.worktree.path
            else:
                continue
            if not scope.contains(worktree):
                continue
            if branch in branch_started and started <= branch_started[branch]:
                continue
            if (isinstance(owner, staff.StaffRow) and owner.kind == "unit"
                    and staff.worktree_machine(owner) == "natedev"):
                branches[branch] = owner.name
                branch_started[branch] = started
            elif isinstance(owner, unit_runs.RunOwnerUnrecorded) and worktree in by_worktree:
                branches[branch] = by_worktree[worktree].name
                branch_started[branch] = started
    for row in record.find(kind="unit"):
        if (isinstance(row.worktree, staff.RemovedWorktree) and row.worktree.machine == "natedev"
                and scope.contains(row.worktree.path)):
            branch = resolver.unit_branch(row)
            if isinstance(branch, session_staff.Branch):
                branches[branch.name] = row.name
    return UnitBranchEvidence(branches, latest)


def is_ancestor(repository: Path, tip: str, target: str) -> bool:
    result = git(repository, "merge-base", "--is-ancestor", tip, target)
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def list_branches(repository: Path, record: staff.StaffSnapshot,
                  roots: tuple[Path, ...]) -> tuple[MergedUnitBranch, ...]:
    """Fetch origin's graph once without changing any branch on origin."""
    _ = git_text(repository, "fetch", "--no-write-fetch-head", "-q", "origin")
    scope = RepositoryScope(repository)
    resolver = session_staff.RowResolver(record)
    former = former_units(repository, scope)
    recorded = recorded_branches(record, roots, resolver, scope, former)
    protected = {"main"}
    for row in record.rows:
        if isinstance(row.worktree, staff.PresentWorktree):
            tree = row.worktree
            if tree.machine == "natedev" and tree.path in scope.registered:
                protected.add(scope.registered[tree.path])
            else:
                branch = resolver.unit_branch(row)
                if not isinstance(branch, session_staff.Branch):
                    if row.id not in recorded.latest_by_owner:
                        raise ValueError(f"{row.name}: cannot read the active branch: {branch.reason}")
                    protected.add(recorded.latest_by_owner[row.id].branch)
                else:
                    protected.add(branch.name)
        if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            document = resolver.production_doc(row)
            if not isinstance(document, session_staff.ProductionDoc):
                raise ValueError(f"{row.name}: cannot read the production: {document.reason}")
            protected.add(production_field(document.text.splitlines(), "Merge branch"))
    heads = {ref.removeprefix("refs/heads/"): tip for line in
             git_text(repository, "ls-remote", "--heads", "origin").splitlines()
             for tip, ref in [line.split()]}
    main = heads.get("main", "")
    if not main:
        raise ValueError("origin has no main branch")
    mainline = git_text(repository, "rev-list", "--first-parent", "--reverse", main).splitlines()
    rows: list[MergedUnitBranch] = []
    for name in sorted(recorded.units.keys() & heads.keys() - protected):
        tip = heads[name]
        if not is_ancestor(repository, tip, main):
            continue
        low, high = 0, len(mainline) - 1
        while low < high:
            middle = (low + high) // 2
            if is_ancestor(repository, tip, mainline[middle]):
                high = middle
            else:
                low = middle + 1
        merge = mainline[low]
        rows.append(MergedUnitBranch(name, recorded.units[name], tip,
                                    git_text(repository, "show", "-s", "--format=%cI", tip),
                                    merge, git_text(repository, "show", "-s", "--format=%cI", merge)))
    return tuple(rows)


def page_at(root: Path, request_path: str) -> BranchPage:
    match = re.fullmatch(r"/([a-z0-9]{6})/branches\.html", urlsplit(request_path).path)
    if match is None:
        raise FileNotFoundError("not a branch page")
    folder = root / match[1]
    manifest = folder / MANIFEST
    if folder.is_symlink() or manifest.is_symlink():
        raise ValueError("not a regular branch manifest")
    value = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("invalid branch manifest")
    fields = cast(dict[str, object], value)
    repository, token, raw_rows = (fields.get(key) for key in ("repository", "token", "rows"))
    if not (isinstance(repository, str) and Path(repository).is_absolute()
            and isinstance(token, str) and token and isinstance(raw_rows, list)):
        raise ValueError("invalid branch manifest")
    rows: list[MergedUnitBranch] = []
    for raw in cast(list[object], raw_rows):
        if not isinstance(raw, dict):
            raise ValueError("invalid branch row")
        row = cast(dict[str, object], raw)
        keys = ("name", "unit", "tip", "commit_date", "merge", "merge_date")
        if set(row) != set(keys) or not all(isinstance(row[key], str) and row[key] for key in keys):
            raise ValueError("invalid branch row")
        rows.append(MergedUnitBranch(*(cast(str, row[key]) for key in keys)))
    return BranchPage(Path(repository), tuple(rows), token)


def render(page: BranchPage, results: tuple[DeleteResult, ...] = (), *,
           selected: frozenset[str] | InitialSelection = InitialSelection.ALL) -> bytes:
    rows: list[str] = []
    for row in page.rows:
        name = html.escape(row.name, quote=True)
        checked = " checked" if isinstance(selected, InitialSelection) or row.name in selected else ""
        rows.append(f'''<section><label><input type="checkbox" name="branch" value="{name}"{checked}>
<strong>{name}</strong></label><p>Unit: {html.escape(row.unit)}
Last commit: {html.escape(row.commit_date)}
Merged into main: {html.escape(row.merge[:12])} · {html.escape(row.merge_date)}</p></section>''')
    notices = "".join(f"<li>{html.escape(result.name)}: {html.escape(result.message)}</li>" for result in results)
    content = '<h1>Branches to delete</h1>'
    if results:
        content += f'<h2>Delete results</h2><ul role="status">{notices}</ul>'
    if rows:
        content += f'''<p>Clear each branch that you want to keep.</p>
<form method="post"><input type="hidden" name="token" value="{html.escape(page.token, quote=True)}">
{"".join(rows)}<button>Delete</button></form>'''
    else:
        content += "<p>No branches are available to delete.</p>"
    return change_page.document(content).replace(b"<title>Changes to review</title>",
                                                b"<title>Branches to delete</title>", 1)


def submit(page: BranchPage, body: str) -> tuple[DeleteResult, ...]:
    fields = parse_qs(body, keep_blank_values=True)
    if fields.get("token") != [page.token]:
        raise ValueError("invalid branch form token")
    selected = tuple(dict.fromkeys(fields.get("branch", [])))
    listed = {row.name: row for row in page.rows}
    if any(name not in listed for name in selected):
        raise ValueError("the selection contains a branch outside this page")
    if not selected:
        return ()
    results: list[DeleteResult] = []
    for name in selected:
        try:
            result = git(page.repository, "push", "origin", "--delete", "--", name)
            message = "deleted" if result.returncode == 0 else failure_detail(result)
            results.append(DeleteResult(name, message))
        except OSError as error:
            results.append(DeleteResult(name, str(error)))
    return tuple(results)


def publish(repository: Path) -> str:
    page = BranchPage(repository.resolve(), list_branches(repository, staff.snapshot(), history_roots()),
                      secrets.token_urlsafe())
    root = review_link.review_root()
    with tempfile.TemporaryDirectory(prefix="review-branches-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_bytes(render(page))
        url = review_link.snapshot_at(root, [source], new=True)[0]
    folder = root / urlsplit(url).path.split("/")[1]
    manifest = {"repository": str(page.repository), "rows": [asdict(row) for row in page.rows], "token": page.token}
    atomic_file.write_bytes_atomic(folder / MANIFEST, json.dumps(manifest).encode("utf-8"), mode=0o600)
    return url


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--list", action="store_true", help="print names and count without publishing a page")
    args = parser.parse_args(arguments)
    repository = Path.home() / ".claude"
    try:
        if cast(bool, args.list):
            rows = list_branches(repository, staff.snapshot(), history_roots())
            for row in rows:
                print(row.name)
            print(f"{len(rows)} branches")
        else:
            print(publish(repository))
    except (OSError, ValueError, subprocess.SubprocessError, staff.StaffRefused, Refusal) as error:
        print(f"branch_page: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
