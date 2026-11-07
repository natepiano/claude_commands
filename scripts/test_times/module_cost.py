#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Measure module CPU, process starts, successful execs and wall time."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Literal, TypedDict, cast

from scripts.lib.terminal_cells import print_table, seconds


@dataclass(frozen=True)
class ProcessStarts:
    processes: int
    execs: int


def count_starts(lines: Iterable[str]) -> ProcessStarts:
    processes = execs = 0
    unfinished: dict[str, str] = {}
    for line in lines:
        prefix = re.match(r"^(?:\[pid\s+(\d+)\]\s+|(\d+)\s+)?(.*)$", line.strip())
        if prefix is None:
            continue
        pid = prefix[1] or prefix[2] or ""
        call = prefix[3]
        if call.endswith("<unfinished ...>"):
            unfinished[pid] = call.removesuffix("<unfinished ...>")
            continue
        resumed = re.match(r"<\.\.\. \w+ resumed>(.*)", call)
        if resumed:
            call = unfinished.pop(pid, "") + resumed[1]
        completed = re.match(r"(fork|vfork|clone|clone3|execve)\(.*\)\s+=\s+(-?\d+)(?:\s|$)", call)
        if completed is None:
            continue
        operation, result = completed[1], int(completed[2])
        if operation == "execve":
            execs += int(result == 0)
        elif result > 0 and "CLONE_THREAD" not in call:
            processes += 1
    return ProcessStarts(processes, execs)


class MeasuredTest(TypedDict):
    test: str
    kind: Literal["test", "fixture"]
    cpu_s: float
    s: float


class TimeReport(TypedDict):
    tests: list[MeasuredTest]
    fixtures: list[MeasuredTest]


@dataclass(frozen=True)
class ModuleCost:
    module: str
    cpu_s: float
    starts: ProcessStarts
    wall_s: float
    test_status: int
    trace_status: int
    tests: list[MeasuredTest]

    @property
    def passed(self) -> bool:
        return self.test_status == self.trace_status == 0

    def row(self) -> list[str]:
        status = "PASS" if self.passed else f"RED (test={self.test_status}, trace={self.trace_status})"
        return [self.module, seconds(self.cpu_s), str(self.starts.processes), str(self.starts.execs),
                seconds(self.wall_s), status]


def measure(tree: Path, module: str, strace: str) -> ModuleCost:
    folder, filename = module.rsplit("/", 1)
    command = [sys.executable, str(Path(__file__).with_name("module_run.py")), f"scripts/{folder}", filename]
    environment = dict(os.environ, HERDR_BIN="/nonexistent/herdr", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory(prefix="module-cost-") as temporary:
        root = Path(temporary)
        report = root / "times.json"
        environment["TEST_TIMES_OUT"] = str(report)
        with (root / "time.log").open("w+") as output:
            started = time.perf_counter()
            with subprocess.Popen(command, cwd=tree, env=environment,
                                  stdout=output, stderr=subprocess.STDOUT) as child:
                _, status, usage = os.wait4(child.pid, 0)
                child.returncode = test_status = os.waitstatus_to_exitcode(status)
            wall_s = time.perf_counter() - started
            if test_status:
                _ = output.seek(0)
                _ = shutil.copyfileobj(output, sys.stderr)
        tests: list[MeasuredTest] = []
        if report.is_file():
            data = cast(TimeReport, json.loads(report.read_text()))
            tests = [row for row in data["tests"] if row["kind"] == "test"] + data["fixtures"]
        del environment["TEST_TIMES_OUT"]
        trace = root / "starts.trace"
        trace.touch()
        with (root / "trace.log").open("w+") as output:
            traced = subprocess.run(
                [strace, "-f", "-e", "trace=fork,vfork,clone,clone3,execve", "-o", str(trace), *command],
                cwd=tree, env=environment, stdout=output, stderr=subprocess.STDOUT, check=False,
            )
            if traced.returncode:
                _ = output.seek(0)
                _ = shutil.copyfileobj(output, sys.stderr)
        with trace.open() as lines:
            starts = count_starts(lines)
    return ModuleCost(module, usage.ru_utime + usage.ru_stime, starts, wall_s, test_status, traced.returncode, tests)


@dataclass(init=False)
class CostArguments(argparse.Namespace):
    tree: Path
    modules: list[str]
    tests: int
    commands: int


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--tree", type=Path, required=True)
    _ = parser.add_argument("--tests", type=int, default=0, metavar="N")
    _ = parser.add_argument("--commands", type=int, default=0, metavar="N")
    _ = parser.add_argument("modules", nargs="+", metavar="folder/file")
    request = parser.parse_args(argv, namespace=CostArguments())
    strace = shutil.which("strace")
    if strace is None:
        print("module_cost: strace is not on PATH", file=sys.stderr)
        return 2
    costs = [measure(request.tree.resolve(), module, strace) for module in request.modules]
    passed = all(cost.passed for cost in costs)
    total = ["total", seconds(sum(cost.cpu_s for cost in costs)),
             str(sum(cost.starts.processes for cost in costs)), str(sum(cost.starts.execs for cost in costs)),
             seconds(sum(cost.wall_s for cost in costs)), "PASS" if passed else "RED"]
    print_table(["module", "CPU s", "processes", "execs", "wall s", "test status"],
                [*[cost.row() for cost in costs], total])
    print("Counts include the module_run supervisor: one extra process and no extra exec.")
    if request.tests:
        rows = [[cost.module, row["test"], row["kind"], seconds(row["cpu_s"]), seconds(row["s"])]
                for cost in costs
                for row in sorted(cost.tests, key=lambda row: (-row["cpu_s"], row["test"]))[:request.tests]]
        print_table(["module", "test or fixture", "kind", "CPU s", "wall s"], rows)
    if request.commands:
        from scripts.test_times import task_cpu

        environment = dict(os.environ, HERDR_BIN="/nonexistent/herdr", PYTHONDONTWRITEBYTECODE="1")
        _ = environment.pop("TEST_TIMES_OUT", None)
        for cost in costs:
            folder, filename = cost.module.rsplit("/", 1)
            command = [sys.executable, str(Path(__file__).with_name("module_run.py")),
                       f"scripts/{folder}", filename]
            tasks = task_cpu.measure(command, cwd=request.tree.resolve(), environment=environment)
            print_table([cost.module + " command", "starts", "CPU s"],
                        [[row.command, str(row.starts), seconds(row.cpu_s)]
                         for row in tasks.commands[:request.commands]])
            print(f"unassigned {tasks.cpu_s - sum(row.cpu_s for row in tasks.commands):+.6f} CPU s")
            passed = passed and tasks.status == 0
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
