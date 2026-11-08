"""Record worktrees and end them during the production merge."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scripts.lib.process_failure import failure_detail
from scripts.production import session_staff, staff
from scripts.shutdown import remote


@dataclass(frozen=True)
class WorktreeRecord:
    version: int
    path: str
    branch: str
    repo: str
    base: str
    merge_branch: str
    owner: str
    made_at: str


@dataclass(frozen=True)
class Landed:
    """Every commit and changed path satisfies the landing rule."""


@dataclass(frozen=True)
class NotLanded:
    commits: tuple[str, ...]
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ended:
    path: Path
    branch: str


@dataclass(frozen=True)
class Kept:
    path: Path
    reason: str


class Refused(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code: int = code


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise Refused(1, failure_detail(result))
    return result


def state_folder() -> Path:
    return Path(os.environ.get("WORKTREES_STATE_DIR", str(Path.home() / ".local/state/worktrees"))) / "made"


def record_file(path: Path) -> Path:
    return state_folder() / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def read_record(path: Path) -> WorktreeRecord:
    fields = cast(dict[str, object], json.loads(path.read_text()))
    return WorktreeRecord(
        version=cast(int, fields["version"]), path=cast(str, fields["path"]),
        branch=cast(str, fields["branch"]), repo=cast(str, fields["repo"]),
        base=cast(str, fields["base"]), merge_branch=cast(str, fields["merge_branch"]),
        owner=cast(str, fields["owner"]), made_at=cast(str, fields["made_at"]))


def write_record(record: WorktreeRecord) -> None:
    path = record_file(Path(record.path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(asdict(record), indent=2) + "\n")
    _ = temporary.replace(path)


def staff_record() -> staff.StaffSnapshot:
    try:
        return staff.snapshot()
    except staff.StaffAbsent:
        return staff.StaffSnapshot(())


def caller_branch(repo: Path, caller: Path, snapshot: staff.StaffSnapshot) -> tuple[str, str]:
    resolver = session_staff.RowResolver(snapshot)
    member = resolver.member_by_folder(remote.local_machine(), caller)
    if isinstance(member, session_staff.MemberFound):
        branch = resolver.merge_branch(member.row)
        if isinstance(branch, session_staff.Branch):
            return member.row.name, branch.name
    # With no merge branch, no production merge ends the tree: it stays.
    head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return "", head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else ""


def has_branch(repo: Path, branch: str) -> bool:
    result = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if result.returncode not in (0, 1):
        raise Refused(1, result.stderr.strip())
    return result.returncode == 0


def plan_path(path: str) -> bool:
    return path.startswith("docs/plans/")


def landed(record: WorktreeRecord) -> Landed | NotLanded:
    repo = Path(record.repo)
    if not has_branch(repo, record.branch):
        return Landed()
    target = f"refs/remotes/origin/{record.merge_branch}"
    branch = f"refs/heads/{record.branch}"
    equivalent = {line[2:] for line in git(repo, "cherry", target, branch).stdout.splitlines()
                  if line.startswith("- ")}
    remaining: list[str] = []
    for commit in git(repo, "rev-list", f"{target}..{branch}").stdout.splitlines():
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.split()
        if commit in equivalent or len(parents) > 2:
            continue
        paths = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit).stdout
        if all(plan_path(path) for path in paths.split("\0") if path):
            continue
        remaining.append(commit)
    changed = git(repo, "diff", "--name-only", "--no-renames", "-z", record.base, branch).stdout
    last_commit = int(git(repo, "log", "-1", "--format=%ct", branch).stdout.strip())
    unmatched: list[str] = []
    for path in changed.split("\0"):
        if not path or plan_path(path):
            continue
        difference = git(repo, "diff", "--quiet", target, branch, "--", path, check=False)
        if difference.returncode == 0:
            continue
        if difference.returncode != 1:
            raise Refused(1, difference.stderr.strip())
        last_change = git(repo, "log", "-1", "--format=%ct", target, "--", path).stdout.strip()
        if not last_change or int(last_change) <= last_commit:
            unmatched.append(path)
    if remaining or unmatched:
        return NotLanded(tuple(remaining), tuple(unmatched))
    return Landed()


def worktree_branches(repo: Path) -> dict[Path, str]:
    trees: dict[Path, str] = {}
    path = repo
    for field in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0"):
        if field.startswith("worktree "):
            path = Path(field.removeprefix("worktree ")).resolve()
        elif field.startswith("branch refs/heads/"):
            trees[path] = field.removeprefix("branch refs/heads/")
    return trees


def main_checkout(repo: Path) -> Path:
    first = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0", 1)[0]
    return Path(first.removeprefix("worktree ")).resolve()


def new_record(path: Path, branch: str, repo: Path, base: str,
               owner: str, merge_branch: str) -> WorktreeRecord:
    return WorktreeRecord(1, str(path), branch, str(repo), base, merge_branch, owner,
                          datetime.now(timezone.utc).isoformat())


def end_record(record: WorktreeRecord, snapshot: staff.StaffSnapshot) -> Ended | Kept:
    path, repo = Path(record.path), Path(record.repo)
    resolver = session_staff.RowResolver(snapshot)
    member = resolver.member_by_folder(remote.local_machine(), path)
    if isinstance(member, session_staff.MemberFound):
        return Kept(path, f"staff member {member.row.name} holds this worktree")
    if path.exists():
        dirty = git(path, "status", "--porcelain").stdout
        if dirty:
            return Kept(path, f"dirty worktree: {dirty.rstrip()}")
        head = git(path, "symbolic-ref", "-q", "HEAD", check=False)
        expected = f"refs/heads/{record.branch}"
        if head.returncode or head.stdout.strip() != expected:
            reason = head.stderr.strip() or f"HEAD is {head.stdout.strip() or 'detached'}; expected {expected}"
            return Kept(path, reason)
    result = landed(record)
    if isinstance(result, NotLanded):
        detail = "\n".join((*result.commits, *result.paths))
        return Kept(path, f"work has not landed: {detail}")
    registered = path in worktree_branches(repo)
    if path.exists() or registered:
        _ = git(repo, "worktree", "remove", str(path))
    if has_branch(repo, record.branch):
        _ = git(repo, "branch", "-D", record.branch)
    remote_branch = git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{record.branch}").stdout
    if remote_branch:
        _ = git(repo, "push", "origin", "--delete", record.branch)
    record_file(path).unlink(missing_ok=True)
    return Ended(path, record.branch)


def end_landed(repo: Path, merge_branch: str) -> list[Ended | Kept]:
    """End recorded work after a production push; retain each failed record."""
    folder = state_folder()
    try:
        records = sorted(folder.iterdir())
    except FileNotFoundError:
        return []
    except OSError as error:
        return [Kept(folder, str(error))]
    results: list[Ended | Kept] = []
    try:
        main_repo = main_checkout(repo)
        snapshot = staff_record()
    except (Refused, OSError, ValueError, staff.StaffRefused) as error:
        return [Kept(folder, str(error))]
    for path in records:
        if path.suffix != ".json":
            continue
        tree_path = path
        try:
            record = read_record(path)
            tree_path = Path(record.path)
            if Path(record.repo).resolve() != main_repo or record.merge_branch != merge_branch:
                continue
            results.append(end_record(record, snapshot))
        except (Refused, OSError, ValueError, KeyError, TypeError) as error:
            results.append(Kept(tree_path, str(error)))
    return results


def make(path: Path, branch: str, caller: Path, repo: Path, snapshot: staff.StaffSnapshot) -> None:
    owner, merge_branch = caller_branch(repo, caller, snapshot)
    base = git(caller, "rev-parse", "HEAD").stdout.strip()
    _ = git(caller, "worktree", "add", str(path), "-b", branch)
    record = new_record(path, branch, repo, base, owner, merge_branch)
    try:
        write_record(record)
    except OSError:
        _ = git(caller, "worktree", "remove", str(path))
        _ = git(caller, "branch", "-D", branch)
        raise
    print(f"made worktree {path} on {branch}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("make")
    _ = create.add_argument("path")
    _ = create.add_argument("branch")
    arguments = parser.parse_args()
    try:
        caller = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        repo = main_checkout(caller)
        snapshot = staff_record()
        make(Path(cast(str, arguments.path)).expanduser().resolve(), cast(str, arguments.branch),
             caller, repo, snapshot)
    except Refused as error:
        print(str(error), file=sys.stderr)
        return error.code
    except (OSError, ValueError, KeyError, staff.StaffRefused) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
