#!/usr/bin/env python3
"""Stream style-fix orchestrator + agent log lines to stdout, exit on launcher-exit.

Replaces the inline `tail -F | awk` pipeline used by the /style_eval Monitor.
That pipeline relied on `pkill -f` to terminate the tail when the launcher's
EXIT trap fired, but inside the Claude Code sandbox `pkill` is denied access
to macOS's `sysmond` process-list service and silently fails. This script
avoids the issue by reading the log files directly in Python.

Usage: style-fix-monitor.py <project-name>
"""

import glob
import os
import re
import sys
import time
from typing import IO

EMIT_PREFIXES = [
    r"\[progress ",
    r"cargo ",
    r"Compiling ",
    r" {4}Finished ",
    r" {4}Checking ",
    r" {4}Running ",
    r"test result:",
    r"thread .* panicked",
    r"warning:",
    r"error:",
    r"error\[",
    r"Fix Summary",
]
EMIT_RE = re.compile("^(" + "|".join(EMIT_PREFIXES) + ")")
EXIT_RE = re.compile(r"^\[progress [^\]]+\] phase=launcher-exit ")

POLL_SECS = 0.5
LOG_DIR = os.path.expanduser("~/.local/logs/clean-fix")


def latest_manual_log() -> str | None:
    matches = glob.glob(f"{LOG_DIR}/style-fix-manual-*.log")
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def open_for_tail(path: str) -> IO[str] | None:
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    _ = f.seek(0, os.SEEK_END)
    return f


def main() -> int:
    if len(sys.argv) != 2:
        _ = sys.stderr.write("usage: style-fix-monitor.py <project-name>\n")
        return 2
    project = sys.argv[1]
    agent_log_path = f"/private/tmp/claude/style_fix_{project}.log"

    manual_log_path: str | None = None
    while manual_log_path is None:
        manual_log_path = latest_manual_log()
        if manual_log_path is None:
            time.sleep(POLL_SECS)

    handles: dict[str, IO[str]] = {}
    manual_handle = open_for_tail(manual_log_path)
    if manual_handle is not None:
        handles[manual_log_path] = manual_handle

    while True:
        progressed = False

        if agent_log_path not in handles:
            agent_handle = open_for_tail(agent_log_path)
            if agent_handle is not None:
                handles[agent_log_path] = agent_handle

        for handle in list(handles.values()):
            while True:
                line = handle.readline()
                if not line:
                    break
                progressed = True
                if EMIT_RE.match(line):
                    _ = sys.stdout.write(line)
                    _ = sys.stdout.flush()
                if EXIT_RE.match(line):
                    return 0

        if not progressed:
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    sys.exit(main())
