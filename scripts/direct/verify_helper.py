#!/usr/bin/env python3
"""Run synchronous verify helpers in one process."""
from __future__ import annotations

import ast
import ctypes
from dataclasses import dataclass
import hashlib
import importlib.util
import inspect
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import time
import traceback
from types import ModuleType
from typing import BinaryIO, Callable, cast


PARENT_CHECK_INTERVAL_SECONDS = 1
PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class HelperCall:
    program: str
    directory: str
    arguments: list[str]
    environment: dict[str, str]
    stdin: str
    forwarded_environment: list[str]


@dataclass(frozen=True)
class LoadedProgram:
    module: ModuleType
    invoke: Callable[[], object]


class SeparateProcess:
    """The path needs its original interpreter process."""


def read_field(source: BinaryIO) -> str:
    value = bytearray()
    while byte := source.read(1):
        if byte == b"\0":
            return os.fsdecode(bytes(value))
        value.extend(byte)
    raise EOFError


def read_fields(source: BinaryIO) -> list[str]:
    values: list[str] = []
    while value := read_field(source):
        values.append(value)
    return values


def read_call(source: BinaryIO) -> HelperCall:
    program, directory = read_field(source), read_field(source)
    count = int(read_field(source))
    arguments: list[str] = []
    while len(arguments) < count:
        arguments.append(read_field(source))
    environment = dict(value.split("=", 1) for value in read_fields(source))
    return HelperCall(program, directory, arguments, environment, read_field(source), read_fields(source))


def load_program(path: str) -> LoadedProgram | SeparateProcess:
    # Shell fixtures and scripts without main retain their original entry point.
    source = Path(path).read_bytes()
    try:
        syntax = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return SeparateProcess()
    if not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in syntax.body):
        return SeparateProcess()
    name = "verify_helper_" + hashlib.sha256(os.fsencode(path)).hexdigest()
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        return SeparateProcess()
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    main = cast(Callable[..., object], getattr(module, "main"))
    if inspect.signature(main).parameters:
        return LoadedProgram(module, lambda: main(sys.argv[1:]))
    return LoadedProgram(module, main)


def run_call(call: HelperCall, programs: dict[str, LoadedProgram | SeparateProcess]) -> tuple[str, str, str]:
    environment = os.environ.copy()
    arguments, directory, search = sys.argv, os.getcwd(), sys.path[:]
    streams = sys.stdin, sys.stdout, sys.stderr
    temporary_parent = tempfile.tempdir
    started, cpu = time.time(), time.process_time()
    status = "fallback"
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="surrogateescape") as stdin, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="surrogateescape") as stdout, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="surrogateescape") as stderr, \
            tempfile.TemporaryFile() as forwarded:
        descriptors = [os.dup(number) for number in range(3)]
        try:
            os.environ.clear()
            os.environ.update(call.environment)
            os.chdir(call.directory)
            tempfile.tempdir = None
            sys.argv = [call.program, *call.arguments]
            sys.path.insert(0, str(Path(call.program).parent))
            _ = stdin.write(call.stdin)
            _ = stdin.seek(0)
            for number, stream in enumerate((stdin, stdout, stderr)):
                _ = os.dup2(stream.fileno(), number)
            sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
            if "VERIFY_RESULT_ENVIRONMENT_FD" in os.environ:
                _ = forwarded.write(b"".join(os.fsencode(value) + b"\0" for value in call.forwarded_environment))
                _ = forwarded.seek(0)
                os.environ["VERIFY_RESULT_ENVIRONMENT_FD"] = str(forwarded.fileno())
            if call.program not in programs:
                programs[call.program] = load_program(call.program)
            program = programs[call.program]
            if isinstance(program, LoadedProgram):
                try:
                    value = program.invoke()
                    status = str(value if isinstance(value, int) else 0)
                except SystemExit as exit_request:
                    value = exit_request.code
                    status = str(value if isinstance(value, int) else 0 if value is None else 1)
                    if value is not None and not isinstance(value, int):
                        print(value, file=sys.stderr)
                except Exception:
                    traceback.print_exc()
                    status = "1"
        except Exception:
            # An import or protocol failure uses the original process path.
            status = "fallback"
        finally:
            stdout.flush()
            stderr.flush()
            for number, descriptor in enumerate(descriptors):
                _ = os.dup2(descriptor, number)
                os.close(descriptor)
            sys.stdin, sys.stdout, sys.stderr = streams
            sys.argv, sys.path[:] = arguments, search
            os.chdir(directory)
            os.environ.clear()
            os.environ.update(environment)
            tempfile.tempdir = temporary_parent
        _ = stdout.seek(0)
        _ = stderr.seek(0)
        output, errors = stdout.read(), stderr.read()
    if status != "fallback" and (trace := call.environment.get("VERIFY_PY_TRACE")):
        try:
            with open(trace, "a") as target:
                _ = target.write(f"{Path(call.program).name}\t{round((time.time() - started) * 1000)}\t{round((time.process_time() - cpu) * 1000)}\n")
            with open(trace + ".calls", "a") as target:
                _ = target.write("\t".join([Path(call.program).name, *call.arguments]) + "\n")
        except OSError:
            pass
    return status, output, errors


def stop_when_parent_exits(parent: int) -> None:
    """Let Linux stop the helper when its parent exits."""
    if sys.platform != "linux":
        return
    library = ctypes.CDLL(None, use_errno=True)
    if cast(int, library.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)) != 0:
        raise OSError(ctypes.get_errno(), "cannot set the helper parent-death signal")
    if parent == 1 or os.getppid() != parent:
        raise SystemExit(0)


def main() -> int:
    parent = os.getppid()
    stop_when_parent_exits(parent)
    programs: dict[str, LoadedProgram | SeparateProcess] = {}
    with os.fdopen(os.dup(0), "rb", buffering=0) as source, os.fdopen(os.dup(1), "wb") as reply:
        while True:
            if not select.select([source], [], [], PARENT_CHECK_INTERVAL_SECONDS)[0]:
                if os.getppid() != parent:
                    break
                continue
            try:
                call = read_call(source)
            except EOFError:
                break
            result = run_call(call, programs)
            _ = reply.write(b"".join(os.fsencode(value) + b"\0" for value in result))
            reply.flush()
    if len(sys.argv) == 2:
        Path(sys.argv[1]).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
