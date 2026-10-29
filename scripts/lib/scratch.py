#!/usr/bin/env python3
"""Scratch folders that belong to a task, and go when the task ends.

Agents made loose folders in /tmp and ~/rust (repo copies, repros, test trees) and nothing removed
them. Sessions run for weeks, so a session end is no cleanup point (user, 2026-10-26/27). Each
scratch folder belongs to a task, and the thing that ends the task removes it:

    scratch.py new [--task <task> | --until <when>] [--name <word>] [--visible]   make one; prints its path
    scratch.py adopt <path> [--task <task> | --until <when>] [--name <word>] [--visible]   move a loose folder in
    scratch.py hold <path> [--task <task> | --until <when>] --reason <why>   keep a folder where it is
    scratch.py release <path>    drop a hold
    scratch.py renew <path>      mark it changed
    scratch.py end --task <task>  remove every folder and held folder of the task
    scratch.py list              each folder and hold, its task and its last change

A task, and what ends it:

  * `run:<run id>`: end_session.sh, at the run's end;
  * `phase:<unit>/<phase>`: merge_checkpoint.py, when the phase's checkpoint merges;
  * `gate:<id>`: the gate's caller, with `scratch.py end`, when the gate ends;
  * `row:<showrunner>-C<n>`: change_review.py verdict;
  * `until:<YYYY-MM-DDTHHMMZ>`: the hourly tmp sweep, once the time passes. `--until` takes
    `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` in the user's zone. With no task, a folder gets
    `until:` DEFAULT_LIFE_SECONDS (24 h) from now.

A folder lives in ROOT as `<task>-<word>-<6 chars>`; `--visible` (rows only) puts it in VISIBLE_ROOT
as `<showrunner>-C<n>-<word>`, where cargo-port lists it. Each holds RECORD with its task, maker and
times. The hourly tmp sweep (scripts/lint/tmp_sweep.py) keeps a folder in ROOT while its task is
open, and removes it when its until: time passes, or after 7 idle days whatever its task, since an
ender can fail. A row's folder waits for its verdict, and VISIBLE_ROOT is never swept. A git
worktree goes through `git worktree remove`, so its source repo keeps no stale entry.

A hold keeps a folder that must stay where it is (a repo the user checks a change in). Its record
(task, maker, reason) is in the machine's HOLDS file, not in the folder, where a repo would show it
as uncommitted work. `end` removes a held folder with the task's other folders, unless a repo in
it holds work that is on no remote (unsaved_work): that hold stays, marked ended, for the folder
audit to name to its maker. The scratch guard hook
(scripts/hooks/pre-tool-use-scratch-guard.py) refuses a loose folder in /tmp, ~/rust and the
other roots.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.user_zone import user_zone  # noqa: E402

ROOT_ENV = "SCRATCH_ROOT"
VISIBLE_ROOT_ENV = "SCRATCH_VISIBLE_ROOT"
DEFAULT_ROOT = "/tmp/claude/scratch"
DEFAULT_VISIBLE_ROOT = "~/rust/repro"
RECORD = ".scratch.json"
HOLDS_ENV = "SCRATCH_HOLDS"
DEFAULT_HOLDS = "~/.local/state/scratch/holds.json"
TASK = re.compile(r"(?P<kind>run|phase|row|gate|until):(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)?)")
UNTIL_FORMAT = "%Y-%m-%dT%H%MZ"
DEFAULT_LIFE_SECONDS = 24 * 3600
# A repo deeper than this inside a removed folder is not looked for; build output is never a repo.
REPO_DEPTH = 3
SKIPPED_FOLDERS = frozenset({"target", "node_modules", ".git"})
ROW_NAME = re.compile(r"[A-Za-z0-9_-]+-C[0-9]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SUFFIX_LENGTH = 6
SUFFIX_CHARACTERS = string.ascii_lowercase + string.digits
SESSION_VARIABLES = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")


class ScratchError(Exception):
    pass


@dataclass(frozen=True)
class Folder:
    path: Path
    task: str
    changed: float


class Hold(TypedDict):
    task: str
    maker: str
    reason: str
    made: float
    ended: float


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def visible_root() -> Path:
    return Path(os.environ.get(VISIBLE_ROOT_ENV) or DEFAULT_VISIBLE_ROOT).expanduser()


def until_task(moment: float) -> str:
    return "until:" + datetime.fromtimestamp(moment, timezone.utc).strftime(UNTIL_FORMAT)


def parsed_until(text: str) -> str:
    """The until: task for `YYYY-MM-DD` (its end) or `YYYY-MM-DD HH:MM`, in the user's zone."""
    for form, day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            moment = datetime.strptime(text.strip(), form).replace(tzinfo=user_zone())
        except ValueError:
            continue
        return until_task(moment.timestamp() + (24 * 3600 if day else 0))
    raise ScratchError(f"{text!r}: --until takes YYYY-MM-DD or YYYY-MM-DD HH:MM")


def task_end(task: str) -> float | None:
    """When an until: task ends; None for a task that only its ender ends."""
    match = TASK.fullmatch(task)
    if match is None or match["kind"] != "until":
        return None
    try:
        return datetime.strptime(match["name"], UNTIL_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def ended(task: str, now: float) -> bool:
    end_at = task_end(task)
    return end_at is not None and end_at <= now


def checked_task(task: str, visible: bool) -> re.Match[str]:
    match = TASK.fullmatch(task)
    if match is None:
        raise ScratchError(f"{task!r} is no task: use run:<id>, phase:<unit>/<phase>, gate:<id>, "
                           + "row:<showrunner>-C<n> or --until <when>")
    if match["kind"] == "row" and not ROW_NAME.fullmatch(match["name"]):
        raise ScratchError(f"{task!r}: a row task is row:<showrunner>-C<n>")
    if match["kind"] == "until" and task_end(task) is None:
        raise ScratchError(f"{task!r}: an until task is until:YYYY-MM-DDTHHMMZ; use --until")
    if visible and match["kind"] != "row":
        raise ScratchError("--visible is for a change review row's repro only")
    return match


def target(task: str, word: str, visible: bool) -> Path:
    """The new folder's path for the task; never one that exists."""
    match = checked_task(task, visible)
    if word and not WORD.fullmatch(word):
        raise ScratchError(f"{word!r}: a name is letters, digits, - and _")
    if visible:
        path = visible_root() / "-".join(part for part in (match["name"], word) if part)
        if os.path.lexists(path):
            raise ScratchError(f"{path} exists")
        return path
    suffix = "".join(secrets.choice(SUFFIX_CHARACTERS) for _ in range(SUFFIX_LENGTH))
    slug = f"{match['kind']}-{match['name'].replace('/', '-')}"
    return root() / "-".join(part for part in (slug, word, suffix) if part)


def maker() -> str:
    return next((os.environ[name] for name in SESSION_VARIABLES if os.environ.get(name)), "")


def task_or_default(task: str, now: float) -> str:
    return task or until_task(now + DEFAULT_LIFE_SECONDS)


def write_record(path: Path, task: str, now: float) -> None:
    record = {"task": task, "maker": maker(), "created": now, "renewed": now}
    _ = (path / RECORD).write_text(json.dumps(record) + "\n", encoding="utf-8")


def new(task: str = "", word: str = "", visible: bool = False, now: float | None = None) -> Path:
    moment = time.time() if now is None else now
    task = task_or_default(task, moment)
    path = target(task, word, visible)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    write_record(path, task, moment)
    return path


def adopt(source: Path, task: str, word: str = "", visible: bool = False, now: float | None = None) -> Path:
    """Move a loose folder into a scratch root and record its task. A git worktree's link moves
    with it unrepaired: `git worktree repair` from the new place fixes it when it must work."""
    if source.is_symlink() or not source.is_dir():
        raise ScratchError(f"{source} is no folder")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment)
    path = target(task, word, visible)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.move(str(source), str(path))
    write_record(path, task, moment)
    return path


def task_of(path: Path) -> str | None:
    try:
        raw = cast(object, json.loads((path / RECORD).read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return None
    task = cast(dict[str, object], raw).get("task") if isinstance(raw, dict) else None
    return task if isinstance(task, str) else None


def folders() -> list[Folder]:
    """Every recorded scratch folder in both roots."""
    found: list[Folder] = []
    for base in (root(), visible_root()):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for path in entries:
            task = task_of(path) if path.is_dir() and not path.is_symlink() else None
            if task is not None:
                found.append(Folder(path, task, (path / RECORD).stat().st_mtime))
    return found


def renew(path: Path, now: float | None = None) -> None:
    task = task_of(path)
    if task is None:
        raise ScratchError(f"{path} is no scratch folder")
    moment = time.time() if now is None else now
    record = cast(dict[str, object], json.loads((path / RECORD).read_text(encoding="utf-8")))
    record["renewed"] = moment
    _ = (path / RECORD).write_text(json.dumps(record) + "\n", encoding="utf-8")
    os.utime(path / RECORD, (moment, moment))


def remove(path: Path) -> None:
    """Remove one folder; a linked git worktree through its source repo first."""
    if (path / ".git").is_file():
        _ = subprocess.run(["git", "-C", str(path), "worktree", "remove", "--force", str(path)],
                           capture_output=True, check=False)
    if os.path.lexists(path):
        shutil.rmtree(path)


def holds_file() -> Path:
    return Path(os.environ.get(HOLDS_ENV) or DEFAULT_HOLDS).expanduser()


def holds() -> dict[str, Hold]:
    """Every hold on this machine, by the held folder's real path."""
    try:
        raw = cast(object, json.loads(holds_file().read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    found: dict[str, Hold] = {}
    for path, value in cast(dict[str, object], raw).items():
        if isinstance(value, dict):
            fields = cast(dict[str, object], value)
            task, made, end_at = fields.get("task"), fields.get("made"), fields.get("ended", 0.0)
            if isinstance(task, str) and isinstance(made, (int, float)) and isinstance(end_at, (int, float)):
                found[path] = Hold(task=task, maker=str(fields.get("maker", "")), reason=str(fields.get("reason", "")),
                                   made=float(made), ended=float(end_at))
    return found


def write_holds(found: dict[str, Hold]) -> None:
    path = holds_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    _ = staged.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _ = staged.replace(path)


def hold(path: Path, task: str, reason: str, now: float | None = None) -> str:
    """Hold an existing folder for `task`; return the task."""
    if path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path} is no folder")
    if not reason.strip():
        raise ScratchError("a hold needs --reason")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment)
    _ = checked_task(task, False)
    found = holds()
    found[os.path.realpath(path)] = Hold(task=task, maker=maker(), reason=reason.strip(), made=moment, ended=0.0)
    write_holds(found)
    return task


def release(path: Path) -> bool:
    found = holds()
    if found.pop(os.path.realpath(path), None) is None:
        return False
    write_holds(found)
    return True


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)


def repos(path: Path, depth: int = REPO_DEPTH) -> list[Path]:
    """Each git repo or worktree at or under `path`, down to `depth` folders; links not followed."""
    if os.path.lexists(path / ".git"):
        return [path]
    if depth == 0:
        return []
    found: list[Path] = []
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name not in SKIPPED_FOLDERS and entry.is_dir() and not entry.is_symlink():
            found.extend(repos(entry, depth - 1))
    return found


def repo_problem(repo: Path) -> str | None:
    """Why removing this repo loses work, or None. A linked worktree's branches and stash live in its
    source repo, so only its own changes and its HEAD count."""
    linked = (repo / ".git").is_file()
    status = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode:
        return f"git cannot read it ({(status.stderr.strip().splitlines() or ['no message'])[0]})"
    if status.stdout.strip():
        return "it has uncommitted changes"
    if _git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode:
        return None
    if not linked and not _git(repo, "rev-parse", "--verify", "--quiet", "refs/stash").returncode:
        return "it has a stash"
    if not linked and not _git(repo, "remote").stdout.strip():
        return "it has commits and no remote"
    unpushed = _git(repo, "rev-list", "-n1", "HEAD" if linked else "--branches", "--not", "--remotes")
    if unpushed.returncode:
        return "git cannot list its commits"
    return "it has commits that are on no remote" if unpushed.stdout.strip() else None


def unsaved_work(path: Path) -> str | None:
    """`<repo>: <why>` for the first repo in `path` whose work removal would lose, or None."""
    for repo in repos(path):
        problem = repo_problem(repo)
        if problem is not None:
            return f"{repo}: {problem}"
    return None


def end(task: str, now: float | None = None, *, keep: Callable[[Path], bool] = lambda _: False) -> list[Path]:
    """Remove every folder and held folder of the task. A held folder whose repo holds unsaved work
    stays, and its hold is marked ended. Folders selected by keep stay unchanged."""
    _ = checked_task(task, False)
    removed: list[Path] = []
    for folder in folders():
        if folder.task == task and not keep(folder.path):
            remove(folder.path)
            removed.append(folder.path)
    found = holds()
    moment = time.time() if now is None else now
    changed = False
    for path, record in list(found.items()):
        if record["task"] != task or keep(Path(path)):
            continue
        changed = True
        held = Path(path)
        if os.path.lexists(held) and unsaved_work(held) is not None:
            record["ended"] = moment
            continue
        if os.path.lexists(held):
            remove(held)
            removed.append(held)
        del found[path]
    if changed:
        write_holds(found)
    return removed


def chosen_task(arguments: argparse.Namespace) -> str:
    """The --task given, the until: task of --until, or "" for the default."""
    until = cast(str, arguments.until)
    return parsed_until(until) if until else cast(str, arguments.task)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scratch.py", description="Scratch folders that belong to a task.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "adopt", "hold"):
        command = commands.add_parser(name)
        if name != "new":
            _ = command.add_argument("path")
        ends = command.add_mutually_exclusive_group()
        _ = ends.add_argument("--task", default="")
        _ = ends.add_argument("--until", default="")
        if name == "hold":
            _ = command.add_argument("--reason", required=True)
        else:
            _ = command.add_argument("--name", default="")
            _ = command.add_argument("--visible", action="store_true")
    _ = commands.add_parser("release").add_argument("path")
    _ = commands.add_parser("renew").add_argument("path")
    _ = commands.add_parser("end").add_argument("--task", required=True)
    _ = commands.add_parser("list")
    arguments = parser.parse_args(argv)
    command = cast(str, arguments.command)
    try:
        if command == "new":
            print(new(chosen_task(arguments), cast(str, arguments.name), cast(bool, arguments.visible)))
        elif command == "adopt":
            print(adopt(Path(cast(str, arguments.path)), chosen_task(arguments), cast(str, arguments.name),
                        cast(bool, arguments.visible)))
        elif command == "hold":
            path = Path(cast(str, arguments.path))
            print(f"held {os.path.realpath(path)} for {hold(path, chosen_task(arguments), cast(str, arguments.reason))}")
        elif command == "release":
            path = Path(cast(str, arguments.path))
            print(f"released {path}" if release(path) else f"{path} has no hold")
        elif command == "renew":
            renew(Path(cast(str, arguments.path)))
        elif command == "end":
            for path in end(cast(str, arguments.task)):
                print(f"removed {path}")
        else:
            now = time.time()
            for folder in folders():
                print(f"{folder.path}\t{folder.task}\t{(now - folder.changed) / 3600:.1f} h since a change")
            for path, record in sorted(holds().items()):
                state = "ended, unsaved work" if record["ended"] else "held"
                print(f"{path}\t{record['task']}\t{state}: {record['reason']}")
    except (ScratchError, OSError) as error:
        print(f"scratch: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
