#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Save the shared layout and refresh detection rules on a runtime schedule."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from scripts.lib import process_group
from scripts.maintenance.settings import schedule, table


def upkeep() -> None:
    from scripts.herdr import detection_rules, herdr
    running = {session["name"] for session in herdr.live_sessions()}
    herdr.save_shared_layout(running)
    _ = detection_rules.refresh(running, herdr.reload_agent_rules)


def run_job() -> int:
    config = Path(os.environ.get("HERDR_UPKEEP_CONFIG", str(ROOT / "config/herdr_upkeep.toml")))
    cadence = schedule(table(tomllib.loads(config.read_text()))["job"])
    directory = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "herdr-upkeep"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("herdr-upkeep: already running; exit 0")
            return 0
        completed = directory / "completed"
        try:
            last = float(completed.read_text())
        except FileNotFoundError:
            last = float("-inf")
        if time.time() - last < cadence.interval_seconds:
            print("herdr-upkeep: not due; exit 0")
            return 0
        started = time.monotonic()
        with subprocess.Popen([str(ROOT / "scripts/lib/py"), str(Path(__file__).resolve()), "--work"],
                              cwd=ROOT, start_new_session=True) as child:
            try:
                code = child.wait(timeout=cadence.timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    process_group.send_process_group(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _ = child.wait()
                code = 124
        if code == 0:
            temporary = completed.with_suffix(".tmp")
            _ = temporary.write_text(str(time.time()) + "\n")
            os.replace(temporary, completed)
        print(f"herdr-upkeep: {time.monotonic() - started:.3f} s; exit {code}")
        return code


class Arguments(argparse.Namespace):
    work: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--work", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.work:
            upkeep()
            return 0
        return run_job()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"herdr-upkeep: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
