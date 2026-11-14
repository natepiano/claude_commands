"""Measure the real build-log CLI without importing test modules."""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TypedDict, cast
from unittest import mock

from scripts.lib import test_hang_guard


class CliWork(TypedDict):
    cpu_seconds: float
    statements: int
    step_reads: int
    instructions: int
    index_updates: int
    launch_collections: int


@dataclass(frozen=True)
class MeasuredCliRun:
    result: subprocess.CompletedProcess[str]
    work: CliWork


def probe_cli() -> None:
    """Instrument the real CLI entry point, including its preparation work."""
    from scripts.buildlog import cli, index, launches

    statements: list[str] = []
    instruction_batches = 0
    instruction_limit = int(os.environ["BUILDLOG_TEST_INSTRUCTION_LIMIT"])
    read_only = index.read_only

    def count_instructions() -> int:
        nonlocal instruction_batches
        instruction_batches += 1
        # Stop pathological plans by work count rather than waiting for a timeout.
        return int(instruction_batches * 1000 > instruction_limit)

    def traced_read_only() -> sqlite3.Connection:
        connection = read_only()
        connection.set_trace_callback(statements.append)
        connection.set_progress_handler(count_instructions, 1000)
        return connection

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    began = time.process_time()
    with mock.patch.object(index, "read_only", traced_read_only), mock.patch.object(index, "update", wraps=index.update) as updating, mock.patch.object(launches, "collect", wraps=launches.collect) as collecting:
        status = cli.main(sys.argv[1:])
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    work: CliWork = {
        "cpu_seconds": time.process_time() - began + after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
        "statements": len(statements),
        "step_reads": sum("JOIN steps" in sql or "FROM steps" in sql for sql in statements),
        "instructions": instruction_batches * 1000,
        "index_updates": updating.call_count,
        "launch_collections": collecting.call_count,
    }
    print("CLI_WORK=" + json.dumps(work), file=sys.stderr)
    raise SystemExit(status)


def measure_cli_work(command: str, environment: dict[str, str], row_count: int) -> MeasuredCliRun:
    from scripts.buildlog import record

    with mock.patch.dict(os.environ, environment, clear=True):
        host = record.local_step_identity().hosts[0]
    result = subprocess.run(
        [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", "from scripts.buildlog.cli_probe_test_support import probe_cli; "
         + "from scripts.buildlog import store; "
         + f"store.host_name = lambda: {host!r}; probe_cli()", command],
        cwd=Path(__file__).resolve().parents[2],
        env={**environment, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib/python_startup"), "BUILDLOG_TEST_INSTRUCTION_LIMIT": str(row_count * 500)},
        capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
    )
    if result.returncode != 0:
        raise AssertionError(f"CLI failed: {result.stderr}\n{result.stdout}")
    metrics = next(line.removeprefix("CLI_WORK=") for line in result.stderr.splitlines() if line.startswith("CLI_WORK="))
    return MeasuredCliRun(result, cast(CliWork, json.loads(metrics)))
