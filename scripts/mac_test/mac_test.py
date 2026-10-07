#!/usr/bin/env python3
"""Coordinate exclusive test use of the Mac."""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Literal, TypedDict, cast


class RunRecord(TypedDict):
    """A process that currently owns Mac test capacity."""

    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


BlockRecord = TypedDict(
    "BlockRecord",
    {
        "holder": str,
        "session": str | None,
        "for": str,
        "since": str,
        "state": Literal["pending", "active"],
    },
)


class NoRun:
    """The state in which no process owns the Mac."""


class NoBlock:
    """The state in which no showrunner excludes Mac work."""


RunState = RunRecord | NoRun
BlockState = BlockRecord | NoBlock

NO_RUN = NoRun()
NO_BLOCK = NoBlock()


class CliArguments(argparse.Namespace):
    """Arguments accepted across the mac-test subcommands."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.pid: int = 0
        self.what: str = ""
        self.worktree: str = ""
        self.wait: float = 0.0
        self.holder: str = ""
        self.reason: str = ""


class StatePaths:
    """Files protected by one mac-test lock."""

    def __init__(self, directory: Path) -> None:
        self.directory: Path = directory
        self.lock: Path = directory / "state.lock"
        self.run: Path = directory / "run.json"
        self.block: Path = directory / "block.json"


def state_paths() -> StatePaths:
    configured = os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")
    return StatePaths(Path(configured).expanduser())


@contextmanager
def locked(paths: StatePaths) -> Generator[None, None, None]:
    paths.directory.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json_object(path: Path) -> dict[str, object] | None:
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return cast(dict[str, object], decoded)


def read_run(path: Path) -> RunState:
    values = read_json_object(path)
    if values is None:
        return NO_RUN
    pid = values.get("pid")
    proc_start = values.get("proc_start")
    what = values.get("what")
    worktree = values.get("worktree")
    since = values.get("since")
    if (
        type(pid) is not int
        or not isinstance(proc_start, str)
        or not isinstance(what, str)
        or not isinstance(worktree, str)
        or not isinstance(since, str)
    ):
        raise ValueError(f"{path} has an invalid run record")
    return {
        "pid": pid,
        "proc_start": proc_start,
        "what": what,
        "worktree": worktree,
        "since": since,
    }


def read_block(path: Path) -> BlockState:
    values = read_json_object(path)
    if values is None:
        return NO_BLOCK
    holder = values.get("holder")
    session = values.get("session")
    reason = values.get("for")
    since = values.get("since")
    state = values.get("state")
    if (
        not isinstance(holder, str)
        or not (session is None or isinstance(session, str))
        or not isinstance(reason, str)
        or not isinstance(since, str)
        or state not in ("pending", "active")
    ):
        raise ValueError(f"{path} has an invalid block record")
    return {
        "holder": holder,
        "session": session,
        "for": reason,
        "since": since,
        "state": state,
    }


def write_json(path: Path, record: RunRecord | BlockRecord) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            json.dump(record, output, separators=(",", ":"))
            _ = output.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_time(instant: str, include_zone: bool = True) -> str:
    format_string = "%H:%M %Z" if include_zone else "%H:%M"
    return datetime.fromisoformat(instant).astimezone().strftime(format_string)


def current_local_time() -> str:
    return datetime.now().astimezone().strftime("%H:%M %Z")


def process_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    closing_parenthesis = stat.rfind(")")
    fields_after_name = stat[closing_parenthesis + 1 :].split()
    if closing_parenthesis < 0 or len(fields_after_name) <= 19:
        return None
    return fields_after_name[19]


def run_is_live(run: RunRecord) -> bool:
    return process_start_time(run["pid"]) == run["proc_start"]


def message_script(environment_name: str, file_name: str) -> Path:
    configured = os.environ.get(environment_name)
    if configured is not None:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "message" / file_name


def notification_target(block: BlockRecord) -> str:
    session = block["session"]
    if session is None:
        return block["holder"]
    sessions_script = message_script("MAC_TEST_SESSIONS", "sessions.py")
    result = subprocess.run(
        [sys.executable, str(sessions_script), "socket", f"session:{session}"],
        capture_output=True,
        text=True,
        check=False,
    )
    socket = result.stdout.strip()
    if result.returncode == 0 and socket:
        return f"uds:{socket}"
    return block["holder"]


def send_mac_free(block: BlockRecord, ended_work: str) -> None:
    text = (
        f"Message from mac-test: the Mac is free. {ended_work} ended at {current_local_time()}; "
        f"your block ({block['for']}) is active, and nothing is built or tested there until you run unblock."
    )
    send_script = message_script("MAC_TEST_SEND", "send.py")
    result = subprocess.run(
        [
            sys.executable,
            str(send_script),
            "--to",
            notification_target(block),
            "--from",
            "mac-test",
            "--summary",
            "Mac is free",
            "--key",
            f"mac-free-{block['since']}",
            "--text",
            text,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print("warning: the Mac is free message could not be delivered", file=sys.stderr)


def activate_pending_block(paths: StatePaths, block: BlockRecord, ended_work: str) -> BlockRecord:
    active: BlockRecord = {
        "holder": block["holder"],
        "session": block["session"],
        "for": block["for"],
        "since": block["since"],
        "state": "active",
    }
    write_json(paths.block, active)
    send_mac_free(active, ended_work)
    return active


def settle(paths: StatePaths, ended_work: str = "Mac work") -> tuple[RunState, BlockState]:
    run = read_run(paths.run)
    if isinstance(run, dict) and not run_is_live(run):
        ended_work = run["what"]
        paths.run.unlink(missing_ok=True)
        run = NO_RUN

    block = read_block(paths.block)
    if isinstance(block, dict) and block["state"] == "pending" and isinstance(run, NoRun):
        block = activate_pending_block(paths, block, ended_work)
    return run, block


def command_claim(args: CliArguments, paths: StatePaths) -> int:
    deadline = time.monotonic() + max(args.wait, 0.0)
    while True:
        with locked(paths):
            run, block = settle(paths)
            if isinstance(block, dict):
                print(f"blocked by {block['holder']}: {block['for']}")
                return 10
            if isinstance(run, NoRun):
                proc_start = process_start_time(args.pid)
                if proc_start is None:
                    print(f"cannot claim: process {args.pid} is not running", file=sys.stderr)
                    return 1
                claimed: RunRecord = {
                    "pid": args.pid,
                    "proc_start": proc_start,
                    "what": args.what,
                    "worktree": args.worktree,
                    "since": utc_now(),
                }
                write_json(paths.run, claimed)
                print("claimed")
                return 0
            busy_run = run

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"busy: {busy_run['what']} since {local_time(busy_run['since'], include_zone=False)}")
            return 11
        time.sleep(min(2.0, remaining))


def command_release(args: CliArguments, paths: StatePaths) -> int:
    with locked(paths):
        run, _block = settle(paths)
        if isinstance(run, dict) and run["pid"] == args.pid:
            paths.run.unlink(missing_ok=True)
            _ = settle(paths, run["what"])
    return 0


def command_block(args: CliArguments, paths: StatePaths) -> int:
    with locked(paths):
        run, block = settle(paths)
        if isinstance(block, dict) and block["holder"] != args.holder:
            print(f"already blocked by {block['holder']}")
            return 1
        session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        since = block["since"] if isinstance(block, dict) else utc_now()
        if isinstance(run, dict):
            requested: BlockRecord = {
                "holder": args.holder,
                "session": session,
                "for": args.reason,
                "since": since,
                "state": "pending",
            }
            write_json(paths.block, requested)
            pending_message = f"Block pending for {args.holder}: {run['what']} is running on the Mac. Nothing new starts there, and you get a message when it ends."
            print(pending_message)
            return 0
        requested = {
            "holder": args.holder,
            "session": session,
            "for": args.reason,
            "since": since,
            "state": "active",
        }
        write_json(paths.block, requested)
        print(f"Mac blocked for {args.holder}: nothing is running there, it is free now.")
        return 0


def command_unblock(args: CliArguments, paths: StatePaths) -> int:
    with locked(paths):
        _run, block = settle(paths)
        if isinstance(block, NoBlock):
            print("no block")
            return 0
        if block["holder"] != args.holder:
            print(f"blocked by {block['holder']}")
            return 1
        paths.block.unlink(missing_ok=True)
        print("Mac unblocked.")
        return 0


def command_status(paths: StatePaths) -> int:
    with locked(paths):
        run, block = settle(paths)
        if isinstance(run, NoRun):
            print("free")
        else:
            print(f"running: {run['what']} ({run['worktree']}) since {local_time(run['since'])}")
        if isinstance(block, NoBlock):
            print("no block")
        elif block["state"] == "pending":
            print(f"block pending for {block['holder']}: {block['for']}")
        else:
            print(f"blocked by {block['holder']} since {local_time(block['since'])}: {block['for']}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    claim = commands.add_parser("claim")
    _ = claim.add_argument("--pid", type=int, required=True)
    _ = claim.add_argument("--what", required=True)
    _ = claim.add_argument("--worktree", default=os.getcwd())
    _ = claim.add_argument("--wait", type=float, default=0.0)

    release = commands.add_parser("release")
    _ = release.add_argument("--pid", type=int, required=True)

    block = commands.add_parser("block")
    _ = block.add_argument("--holder", required=True)
    _ = block.add_argument("--for", dest="reason", required=True)

    unblock = commands.add_parser("unblock")
    _ = unblock.add_argument("--holder", required=True)

    _ = commands.add_parser("status")
    return root


def main() -> int:
    args = parser().parse_args(namespace=CliArguments())
    paths = state_paths()
    if args.command == "claim":
        return command_claim(args, paths)
    if args.command == "release":
        return command_release(args, paths)
    if args.command == "block":
        return command_block(args, paths)
    if args.command == "unblock":
        return command_unblock(args, paths)
    return command_status(paths)


if __name__ == "__main__":
    raise SystemExit(main())
