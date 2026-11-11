#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Restore producers, save the shared layout, and refresh detection rules."""
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
from scripts.lib import conf, operation_deadline, process_group
from scripts.maintenance.settings import Schedule, schedule, table


def _cadence() -> Schedule:
    config = Path(os.environ.get("HERDR_UPKEEP_CONFIG", str(ROOT / "config/herdr_upkeep.toml")))
    return schedule(table(tomllib.loads(config.read_text()))["job"])


def upkeep() -> None:
    from scripts.accounts import accounts
    from scripts.agents import stop_resume
    from scripts.herdr import detection_rules, herdr

    cadence = _cadence()
    deadline = min(time.monotonic() + cadence.timeout_seconds,
                   float(os.environ.get("HERDR_UPKEEP_DEADLINE", "inf")))
    with operation_deadline.within(deadline), accounts.settings_report_scope():
        _ = herdr.restore_server(herdr.shared_session(),
            int(operation_deadline.remaining(herdr.PROGRAM_TIMEOUT_SECONDS) * 1000),
            session_kind=herdr.security_session())
        running = {session["name"] for session in herdr.live_sessions()}
        herdr.save_shared_layout(running)
        _ = detection_rules.refresh(running, herdr.reload_agent_rules)
        producers = accounts.producer_upkeep_members()
        if producers:
            offset = int(time.time() // cadence.interval_seconds) % len(producers)
            producers = producers[offset:] + producers[:offset]
        start_seconds = conf.read("shutdown").number("START_SECONDS")
        for row in producers:
            if time.monotonic() >= deadline:
                print("herdr-upkeep: producer budget ended; retry remaining producers next run", file=sys.stderr)
                break
            try:
                with operation_deadline.within(min(deadline, time.monotonic() + start_seconds)):
                    result = stop_resume.resume(str(row.id))
                if isinstance(result, stop_resume.ResumeFailed):
                    print(f"herdr-upkeep: {row.name}: {result.reason}", file=sys.stderr)
                elif isinstance(result, stop_resume.SessionNotFound):
                    print(f"herdr-upkeep: {row.name}: session not found", file=sys.stderr)
            except Exception as error:
                print(f"herdr-upkeep: {row.name}: {error}", file=sys.stderr)


def run_job() -> int:
    cadence = _cadence()
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
                              cwd=ROOT, start_new_session=True,
                              env={**os.environ, "HERDR_UPKEEP_DEADLINE": str(started + cadence.timeout_seconds)}) as child:
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
