#!/usr/bin/env python3
"""Keep a terminal available while its Claude child stops and resumes."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import NotRequired, TypedDict, cast

from scripts.herdr import herdr
from scripts.production import staff, worker
from scripts.agents import pane_record
from scripts.agents.launch_names import LaunchNames
from scripts.lib.agent_program import resolve_program
from scripts.lib.models_registry import agent_environment, claude_arguments
from scripts.lib.resume_message import NO_WORKERS, ResumeWorkers, WorkersHeld, resume_message, worker_mode
from scripts.whoami.account import NAMED_ACCOUNT_VARIABLES

HOLD_SECONDS = 300
POLL_SECONDS = 0.5


def restored_launch(argv: list[str], environment: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Restore only this pane's resumed conversation; explicit prompts remain unchanged."""
    if not argv or Path(argv[0]).name != "claude" or environment.get("HERDR_ENV") != "1":
        return argv, environment
    parsed = pane_record.parse_claude_arguments(argv)
    identity = parsed.value(pane_record.RESUME_FLAGS)
    if not identity:
        return argv, environment
    place = herdr.HerdrPlace(environment.get("HERDR_SESSION", "default"),
                            environment.get("HERDR_PANE_ID", ""))
    entry = pane_record.matching_launch(place, identity)
    if entry is None:
        return argv, environment
    restored = {**environment, **LaunchNames.read().environment(entry)}
    restored.update({key: environment[key] for key in NAMED_ACCOUNT_VARIABLES if key in environment})
    if not parsed.has_prompt and environment.get("RELAUNCH_PROMPT_DELIVERY") != "pane":
        workers: ResumeWorkers | WorkersHeld = NO_WORKERS
        row: staff.StaffRow | None = None
        try:
            record = staff.snapshot()
            row = record.by_current_session(identity)
            if row is not None:
                workers = worker_mode()
                if not isinstance(workers, WorkersHeld):
                    workers = worker.resume_workers(row.id, record)
        except (OSError, ValueError) as error:
            print(f"{identity}: workers anomaly: {error}", file=sys.stderr)
        argv = [*argv, resume_message("you were stopped and resumed", workers=workers, unit=row)]
    return argv, restored


class RelaunchRequest(TypedDict):
    argv: list[str]
    cwd: NotRequired[str]
    environment: dict[str, str]
    unset: list[str]
    written_at: str
    note: str


def state_root() -> Path:
    return Path(os.environ.get("SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")))


def request_path(pid: int) -> Path:
    return state_root() / "relaunch" / f"{pid}.json"


def hold_path(pid: int) -> Path:
    return state_root() / "relaunch" / f"{pid}.hold"


def write_hold(pid: int) -> None:
    path = hold_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n")


def write_request(pid: int, request: RelaunchRequest) -> None:
    path = request_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{pid}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(request, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _request(value: object) -> RelaunchRequest:
    if not isinstance(value, dict):
        raise ValueError("relaunch request must be an object")
    values = cast(dict[str, object], value)
    for key in ("argv", "unset"):
        entries = values.get(key)
        if not isinstance(entries, list) or not all(isinstance(item, str) for item in cast(list[object], entries)):
            raise ValueError(f"relaunch request {key} must be strings")
    if not values["argv"]:
        raise ValueError("relaunch request argv is empty")
    environment = values.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in cast(dict[object, object], environment).items()
    ):
        raise ValueError("relaunch request environment must contain strings")
    for key in ("written_at", "note"):
        if not isinstance(values.get(key), str):
            raise ValueError(f"relaunch request {key} must be a string")
    if "cwd" in values and not isinstance(values["cwd"], str):
        raise ValueError("relaunch request cwd must be a string")
    return cast(RelaunchRequest, cast(object, values))


def _execute_request(path: Path) -> None:
    request = _request(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    path.unlink()
    hold_path(os.getpid()).unlink(missing_ok=True)
    environment = dict(os.environ)
    for key in request["unset"]:
        _ = environment.pop(key, None)
    environment.update(request["environment"])
    if "cwd" in request:
        os.chdir(request["cwd"])
    print(" ".join(request["note"].splitlines()), flush=True)
    script = str(Path(__file__).resolve())
    os.execvpe(sys.executable, [sys.executable, script, "run", "--", *request["argv"]], environment)


def child_launch(argv: list[str], environment: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Build the exact child arguments and environment for a supervisor launch."""
    program = ""
    if argv and argv[0] in ("claude", "codex"):
        program = resolve_program("claude" if argv[0] == "claude" else "codex")
    argv, environment = restored_launch(argv, environment)
    if program:
        argv = [program, *argv[1:]]
    if argv and Path(argv[0]).name == "claude":
        argv = [argv[0], *claude_arguments(argv[1:], environment)]
    return argv, environment


def run(argv: list[str]) -> int:
    """Run a child on this terminal, then consume a restart or wait under a hold."""
    argv, environment = child_launch(argv, dict(os.environ))
    child: subprocess.Popen[bytes] | None = None

    def ignore(_number: int, _frame: FrameType | None) -> None:
        pass

    def forward(number: int, _frame: FrameType | None) -> None:
        if child is not None:
            try:
                child.send_signal(number)
            except ProcessLookupError:
                pass

    previous = {number: signal.getsignal(number) for number in (
        signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGTERM, signal.SIGHUP,
    )}
    try:
        # A caught handler becomes the default on exec; SIG_IGN would also make
        # the child ignore keyboard signals.
        for number in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
            _ = signal.signal(number, ignore)
        for number in (signal.SIGTERM, signal.SIGHUP):
            _ = signal.signal(number, forward)
        child = subprocess.Popen(argv, env=agent_environment({**environment, "RELAUNCH_SUPERVISED": "1"}))
        code = child.wait()
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)
    code = code if code >= 0 else 128 - code
    request = request_path(os.getpid())
    hold = hold_path(os.getpid())
    announced = False
    duration = float(os.environ.get("SHUTDOWN_RELAUNCH_HOLD_SECONDS", str(HOLD_SECONDS)))
    while True:
        if request.exists():
            try:
                _execute_request(request)
            except (OSError, ValueError) as error:
                request.unlink(missing_ok=True)
                hold.unlink(missing_ok=True)
                print(f"could not restart here: {error}", file=sys.stderr, flush=True)
                return code
        try:
            remaining = duration - (time.time() - hold.stat().st_mtime)
        except FileNotFoundError:
            return code
        if remaining <= 0:
            hold.unlink(missing_ok=True)
            print("no restart came; this window is free", flush=True)
            return code
        if not announced:
            print("claude stopped for a restart; waiting for it here", flush=True)
            announced = True
        time.sleep(min(POLL_SECONDS, remaining))


def main(arguments: list[str]) -> int:
    if len(arguments) < 3 or arguments[:2] != ["run", "--"]:
        print("usage: relaunch.py run -- <argv...>", file=sys.stderr)
        return 2
    try:
        return run(arguments[2:])
    except OSError as error:
        print(f"could not start child: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
