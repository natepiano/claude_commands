#!/usr/bin/env python3
"""Publish and recover verification wait folders."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import process_start
from scripts.lib.atomic_file import write_text_atomic


def recovery_root(session: Path, worktree: Path) -> Path:
    return session / "verify_waits" / hashlib.sha256(os.fsencode(worktree.resolve())).hexdigest()


def create_wait_folder(session: Path, worktree: Path, pid: int) -> Path:
    root = recovery_root(session, worktree)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".verify-hold-new.{pid}.", dir=root))
    start = process_start.process_start(pid, Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")))
    if isinstance(start, process_start.ProcessGone):
        raise OSError("wait owner has ended")
    write_text_atomic(staging / "owner.json", json.dumps({"pid": pid, "start_time": start.text}), create_parent=False)
    directory = staging.with_name(staging.name.removeprefix("." ).replace("verify-hold-new.", "verify-hold.", 1))
    _ = staging.rename(directory)
    return directory


def owner_state(directory: Path) -> process_start.OwnerCheck:
    root = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
    try:
        record = cast(object, json.loads((directory / "owner.json").read_text()))
        if not isinstance(record, dict):
            raise ValueError("invalid owner record")
        owner = cast(dict[str, object], record)
        pid, start = owner.get("pid"), owner.get("start_time")
        if type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start:
            raise ValueError("invalid owner identity")
        return process_start.check_owner(pid, process_start.parse_start(start), root)
    except (OSError, ValueError, TypeError):
        try:
            pid = int(directory.name.split(".")[2 if directory.name.startswith(".") else 1])
            start = process_start.process_start(pid, root)
            return process_start.OwnerCheck.GONE if isinstance(start, process_start.ProcessGone) else process_start.OwnerCheck.ALIVE
        except (ValueError, IndexError, process_start.ProcessReadError):
            return process_start.OwnerCheck.UNKNOWN


def retire(directory: Path, close_wait: Callable[[str], None]) -> None:
    try:
        markers = sorted(path for path in directory.iterdir() if path.name.startswith("phase-wait."))
    except FileNotFoundError:
        return
    for marker in markers:
        try:
            wait_id = marker.read_text().strip()
        except FileNotFoundError:
            continue
        if not wait_id:
            raise OSError(f"cannot close empty wait marker {marker}")
        close_wait(wait_id)
    retired = directory.with_name(f".verify-hold-removing.{uuid.uuid4()}")
    _ = directory.rename(retired)
    shutil.rmtree(retired)


def recover(session: Path, directory: Path) -> None:
    if not directory.exists():
        return
    from scripts.direct import progress_history
    with progress_history.wait_run_lock(session):
        retire(directory, lambda wait_id: progress_history.close_wait_id(session, wait_id))


def sweep_wait_folders(session: Path, worktree: Path, current: Path | None) -> None:
    root = recovery_root(session, worktree)
    failures: list[str] = []
    for directory in root.glob("*"):
        if not directory.is_dir() or directory == current:
            continue
        try:
            if directory.name.startswith(".verify-hold-removing."):
                shutil.rmtree(directory)
            elif directory.name.startswith(("verify-hold.", ".verify-hold-new.")):
                state = owner_state(directory)
                if state.stale:
                    recover(session, directory)
                elif state is process_start.OwnerCheck.UNKNOWN and directory.name.startswith("verify-hold."):
                    failures.append(f"owner is unknown: {directory}")
        except (OSError, SystemExit) as error:
            failures.append(str(error))
    if failures:
        raise OSError("; ".join(failures))


def close_marker(session: Path, marker: Path) -> None:
    try:
        wait_id = marker.read_text().strip()
    except FileNotFoundError:
        return
    from scripts.direct import progress_history
    with progress_history.wait_run_lock(session):
        progress_history.close_wait_id(session, wait_id)
        marker.unlink(missing_ok=True)


class Arguments(argparse.Namespace):
    command: str = ""
    session_dir: Path = Path()
    worktree: Path = Path()
    pid: int = 0
    directory: Path = Path()
    current: Path | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create-wait-folder", "sweep-wait-folders", "recover-wait-folder", "close-marker"):
        command = commands.add_parser(name)
        _ = command.add_argument("--session-dir", required=True, type=Path)
        if name in ("create-wait-folder", "sweep-wait-folders"):
            _ = command.add_argument("--worktree", type=Path, default=Path.cwd())
        if name == "create-wait-folder":
            _ = command.add_argument("--pid", required=True, type=int)
        elif name == "sweep-wait-folders":
            _ = command.add_argument("--current", type=Path)
        else:
            _ = command.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.command == "create-wait-folder":
            print(create_wait_folder(args.session_dir, args.worktree, args.pid))
        elif args.command == "sweep-wait-folders":
            sweep_wait_folders(args.session_dir, args.worktree, args.current)
        elif args.command == "close-marker":
            close_marker(args.session_dir, args.directory)
        else:
            recover(args.session_dir, args.directory)
    except (OSError, SystemExit) as error:
        print(f"verify.sh: wait history NotRecorded: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
