#!/usr/bin/env python3
"""Detect memory kills when sccache hides the rustc signal."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from datetime import datetime
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import cast

from scripts.lib.process_failure import failure_detail


def _epoch(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z").timestamp()


def journal_output(started: float, ended: float, source: tuple[str, ...], *, json_output: bool) -> str:
    arguments = ["journalctl", *source, "--since", "@" + str(started),
                 "--until", "@" + str(ended), "--no-pager"]
    if json_output:
        arguments.extend(["-o", "json"])
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    except OSError as error:
        print(f"memory_kill.py: cannot read the journal: {error}", file=sys.stderr)
        return ""
    if result.returncode:
        print(f"memory_kill.py: cannot read the journal: {failure_detail(result)}", file=sys.stderr)
        return ""
    return result.stdout


def _has_diagnostic(log: str) -> bool:
    for line in log.splitlines():
        # Cargo summaries do not count as compiler diagnostics.
        # A diagnostic from any crate prevents a retry without signal text.
        if re.match(r"\s*error(?:\[E\d+\]|:)", line) and not re.match(
                r"\s*error: (?:could not compile |command `|build failed|process didn't exit successfully:)", line):
            return True
        if line.startswith("{"):
            try:
                value = cast(object, json.loads(line))
            except ValueError:
                continue
            if isinstance(value, dict):
                diagnostic = cast(dict[str, object], value)
                detail = diagnostic.get("message")
                if diagnostic.get("level") == "error":
                    return True
                if isinstance(detail, dict) and cast(dict[str, object], detail).get("level") == "error":
                    return True
    return False


def was_memory_kill(log: str, started: str, ended: str) -> bool:
    """Preserve signal retries. Require crate and time evidence for silent exits."""
    try:
        start, end = _epoch(started), _epoch(ended)
    except (ValueError, OverflowError):
        return False
    if not (math.isfinite(start) and math.isfinite(end) and 0 < start <= end):
        return False
    log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log)
    if re.search(r"SIGKILL|SIGTERM|signal: (9|15)", log):
        # Preserve the signal rule for test binaries, linkers, and build scripts.
        # Any earlyoom or kernel kill in the attempt window is sufficient.
        if re.search(r"sending SIG(TERM|KILL) to process",
                     journal_output(start, end, ("-u", "earlyoom"), json_output=False)):
            return True
        return bool(re.search(r"Memory cgroup out of memory|Out of memory: Killed",
                              journal_output(start, end, ("-k",), json_output=False)))

    if _has_diagnostic(log):
        return False
    failed = {match[1] for match in re.finditer(r"could not compile `([\w-]+)`", log)}
    for line in log.splitlines():
        if "process didn't exit successfully:" in line:
            failed.update(match[1] for match in re.finditer(r"--crate-name(?:\s+|=)(\w+)", line))
    failed = {crate.replace("-", "_") for crate in failed}
    if not failed:
        return False
    journal = journal_output(start, end, ("-u", "earlyoom"), json_output=True)
    for line in journal.splitlines():
        try:
            value = cast(object, json.loads(line))
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, object], value)
            raw_time, message = row.get("__REALTIME_TIMESTAMP"), row.get("MESSAGE")
            if not isinstance(raw_time, (str, int)) or not isinstance(message, str):
                continue
            at = int(raw_time) / 1_000_000
        except (ValueError, OverflowError):
            continue
        if not start <= at <= end:
            continue
        kill = re.search(r'sending SIG(?:TERM|KILL) to process \d+ uid \d+ "rustc":.*cmdline "(.*)"', message)
        if kill and any(match[1] in failed for match in re.finditer(r"--crate-name(?:\s+|=)(\w+)", kill[1])):
            return True
    return False


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        return 1
    started, ended, log_path = arguments
    try:
        log = Path(log_path).read_text(errors="replace")
    except OSError:
        return 1
    return 0 if was_memory_kill(log, started, ended) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
