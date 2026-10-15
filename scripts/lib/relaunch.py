#!/usr/bin/env python3
"""Keep a terminal available while its Claude child stops and resumes."""

from __future__ import annotations

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
from typing import TypedDict, cast

HOLD_SECONDS = 300
POLL_SECONDS = 0.5


class RelaunchRequest(TypedDict):
    argv: list[str]
    cwd: str
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
    for key in ("cwd", "written_at", "note"):
        if not isinstance(values.get(key), str):
            raise ValueError(f"relaunch request {key} must be a string")
    return cast(RelaunchRequest, cast(object, values))


def _execute_request(path: Path) -> None:
    request = _request(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    path.unlink()
    hold_path(os.getpid()).unlink(missing_ok=True)
    environment = dict(os.environ)
    for key in request["unset"]:
        _ = environment.pop(key, None)
    environment.update(request["environment"])
    os.chdir(request["cwd"])
    print(" ".join(request["note"].splitlines()), flush=True)
    script = str(Path(__file__).resolve())
    os.execvpe(sys.executable, [sys.executable, script, "run", "--", *request["argv"]], environment)


def run(argv: list[str]) -> int:
    """Run a child on this terminal, then consume a restart or wait under a hold."""
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
        child = subprocess.Popen(argv, env={**os.environ, "RELAUNCH_SUPERVISED": "1"})
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
