#!/usr/bin/env python3
"""Scratch folders that belong to a task, and go when the task ends.

Agents made loose folders in /tmp and ~/rust (repo copies, repros, test trees) and nothing removed
them: sessions never end, so a session end is no cleanup point (user, 2026-10-26). Each scratch
folder belongs to a task, and the task's end removes it:

    scratch.py new --task <task> [--name <word>] [--visible]   make one; prints its path
    scratch.py adopt <path> --task <task> [--name <word>] [--visible]   move a loose folder in
    scratch.py renew <path>      mark it changed, so the 4 h idle rule waits again
    scratch.py end --task <task>  remove every folder of the task
    scratch.py list              each folder, its task and its last change

A task is `run:<run id>` (ends at end_session.sh), `phase:<unit>/<phase>` (ends when the phase's
checkpoint merges) or `row:<showrunner>-C<n>` (ends at change_review.py verdict). A folder lives
in ROOT as `<task>-<word>-<6 chars>`; `--visible` (rows only) puts it in VISIBLE_ROOT as
`<showrunner>-C<n>-<word>`, where cargo-port lists it. Each holds RECORD with its task, maker and
times. The hourly tmp sweep (scripts/lint/tmp_sweep.py) judges each folder in ROOT: one that did
not change for 4 h and no process uses goes, whether its task ended or not; `renew` is how a folder
lives longer. A row's folder waits for its verdict, and VISIBLE_ROOT is never swept. A git worktree
goes through `git worktree remove`, so its source repo keeps no stale entry. The scratch guard
hook (scripts/hooks/pre-tool-use-scratch-guard.py) refuses a loose folder in /tmp or ~/rust.
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
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT_ENV = "SCRATCH_ROOT"
VISIBLE_ROOT_ENV = "SCRATCH_VISIBLE_ROOT"
DEFAULT_ROOT = "/tmp/claude/scratch"
DEFAULT_VISIBLE_ROOT = "~/rust/repro"
RECORD = ".scratch.json"
TASK = re.compile(r"(?P<kind>run|phase|row):(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)?)")
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


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def visible_root() -> Path:
    return Path(os.environ.get(VISIBLE_ROOT_ENV) or DEFAULT_VISIBLE_ROOT).expanduser()


def checked_task(task: str, visible: bool) -> re.Match[str]:
    match = TASK.fullmatch(task)
    if match is None:
        raise ScratchError(f"{task!r} is no task: use run:<id>, phase:<unit>/<phase> or row:<showrunner>-C<n>")
    if match["kind"] == "row" and not ROW_NAME.fullmatch(match["name"]):
        raise ScratchError(f"{task!r}: a row task is row:<showrunner>-C<n>")
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


def write_record(path: Path, task: str, now: float) -> None:
    maker = next((os.environ[name] for name in SESSION_VARIABLES if os.environ.get(name)), "")
    record = {"task": task, "maker": maker, "created": now, "renewed": now}
    _ = (path / RECORD).write_text(json.dumps(record) + "\n", encoding="utf-8")


def new(task: str, word: str = "", visible: bool = False, now: float | None = None) -> Path:
    path = target(task, word, visible)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    write_record(path, task, time.time() if now is None else now)
    return path


def adopt(source: Path, task: str, word: str = "", visible: bool = False, now: float | None = None) -> Path:
    """Move a loose folder into a scratch root and record its task. A git worktree's link moves
    with it unrepaired: `git worktree repair` from the new place fixes it when it must work."""
    if source.is_symlink() or not source.is_dir():
        raise ScratchError(f"{source} is no folder")
    path = target(task, word, visible)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.move(str(source), str(path))
    write_record(path, task, time.time() if now is None else now)
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


def end(task: str) -> list[Path]:
    _ = checked_task(task, False)
    removed: list[Path] = []
    for folder in folders():
        if folder.task == task:
            remove(folder.path)
            removed.append(folder.path)
    return removed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scratch.py", description="Scratch folders that belong to a task.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "adopt"):
        command = commands.add_parser(name)
        if name == "adopt":
            _ = command.add_argument("path")
        _ = command.add_argument("--task", required=True)
        _ = command.add_argument("--name", default="")
        _ = command.add_argument("--visible", action="store_true")
    _ = commands.add_parser("renew").add_argument("path")
    _ = commands.add_parser("end").add_argument("--task", required=True)
    _ = commands.add_parser("list")
    arguments = parser.parse_args(argv)
    command = cast(str, arguments.command)
    try:
        if command == "new":
            print(new(cast(str, arguments.task), cast(str, arguments.name), cast(bool, arguments.visible)))
        elif command == "adopt":
            print(adopt(Path(cast(str, arguments.path)), cast(str, arguments.task), cast(str, arguments.name),
                        cast(bool, arguments.visible)))
        elif command == "renew":
            renew(Path(cast(str, arguments.path)))
        elif command == "end":
            for path in end(cast(str, arguments.task)):
                print(f"removed {path}")
        else:
            for folder in folders():
                age = (time.time() - folder.changed) / 3600
                print(f"{folder.path}\t{folder.task}\t{age:.1f} h since a change")
    except (ScratchError, OSError) as error:
        print(f"scratch: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
