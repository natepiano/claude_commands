"""Measure task CPU at kernel exit events and group it by the last command."""
from __future__ import annotations

from collections.abc import Sequence
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import resource
import signal
from typing import cast


@dataclass(frozen=True)
class CommandCPU:
    command: str
    starts: int
    cpu_s: float


@dataclass(frozen=True)
class TaskCPU:
    commands: list[CommandCPU]
    cpu_s: float
    status: int


@dataclass(frozen=True)
class ExitedTask:
    tid: int
    tgid: int
    cpu_s: float


def command_name(arguments: Sequence[str]) -> str:
    """Keep the program and the script when the program is an interpreter."""
    program = Path(arguments[0]).name
    python = program.startswith(("python", "pypy"))
    if not python and program not in {"bash", "sh", "zsh"}:
        return program
    options = iter(arguments[1:])
    for argument in options:
        if argument == "--":
            script = next(options, "")
            return f"{program} {script}" if script else program
        if not argument.startswith("-") or argument == "-":
            return f"{program} {argument}"
        if argument.startswith("--"):
            continue
        if argument.endswith("c"):
            return f"{program} -c"
        if python and argument.endswith("m"):
            return f"{program} -m {next(options, '')}".rstrip()
        if argument.endswith(("W", "X") if python else ("o", "O")):
            _ = next(options, "")
    return program


def task_ids(pid: int) -> tuple[int, int]:
    """Return a task's thread group id and its parent process id."""
    lines = Path(f"/proc/{pid}/status").read_bytes().splitlines()
    tgid = int(next(line.split()[1] for line in lines if line.startswith(b"Tgid:")))
    parent = int(next(line.split()[1] for line in lines if line.startswith(b"PPid:")))
    return tgid, parent


def measure(command: Sequence[str], *, cwd: Path, environment: dict[str, str]) -> TaskCPU:
    """Trace process events without stopping at system calls."""
    libc = ctypes.CDLL(None, use_errno=True)
    ptrace = libc.ptrace
    ptrace.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    ptrace.restype = ctypes.c_long

    def trace(request: int, pid: int, data: int = 0) -> int:
        result = cast(int, ptrace(request, pid, None, ctypes.c_void_p(data)))
        if result == -1:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    def resume(request: int, pid: int, delivered: int = 0) -> None:
        try:
            _ = trace(request, pid, delivered)
        except ProcessLookupError:
            pass

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    reader, writer = os.pipe()
    root = os.fork()
    if root == 0:
        try:
            os.chdir(cwd)
            for descriptor in (1, 2):
                with open(os.devnull, "wb") as output:
                    _ = os.dup2(output.fileno(), descriptor)
            os.close(writer)
            _ = os.read(reader, 1)
            os.close(reader)
            os.execvpe(command[0], list(command), environment)
        except BaseException:
            os._exit(127)
    commands = {root: command_name(command)}
    exited: list[ExitedTask] = []
    root_status = 0
    # Trace creation, exec and exit events. End tracees if the tracer ends.
    options = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 6) | (1 << 20)
    os.close(reader)
    _ = trace(0x4206, root, options)
    _ = os.write(writer, b"1")
    os.close(writer)
    while True:
        try:
            pid, status = os.waitpid(-1, 0x40000000)
        except ChildProcessError:
            break
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            if pid == root:
                root_status = os.waitstatus_to_exitcode(status)
            continue
        if not os.WIFSTOPPED(status):
            continue
        event = status >> 16
        if pid not in commands:
            tgid, parent = task_ids(pid)
            commands[pid] = commands[tgid if tgid != pid else parent]
        if event == 4:
            arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0")
            commands[pid] = command_name(arguments)
        elif event == 6:
            cpu_s = int(Path(f"/proc/{pid}/schedstat").read_text().split()[0]) / 1e9
            exited.append(ExitedTask(pid, task_ids(pid)[0], cpu_s))
        stopped = os.WSTOPSIG(status)
        if event == 128 and stopped in (signal.SIGSTOP, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
            resume(0x4208, pid)
        else:
            resume(7, pid, 0 if event else stopped)
    totals: dict[str, float] = {}
    starts: dict[str, int] = {}
    for task in exited:
        name = commands[task.tid]
        totals[name] = totals.get(name, 0.0) + task.cpu_s
        if task.tid == task.tgid:
            starts[name] = starts.get(name, 0) + 1
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = [CommandCPU(name, starts.get(name, 0), cpu) for name, cpu in totals.items()]
    rows.sort(key=lambda row: (-row.cpu_s, row.command))
    cpu_s = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    return TaskCPU(rows, cpu_s, root_status)
