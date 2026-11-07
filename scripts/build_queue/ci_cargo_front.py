#!/usr/bin/env python3
"""Hold admission while a CI cargo call and its descendants run."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Sequence
from contextlib import ExitStack
import ctypes
from functools import partial
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import admission_client as client
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import queue_place
from scripts.build_queue import step_estimator
from scripts.lint import memory_admit


BUILDING = frozenset({"build", "b", "check", "c", "clippy", "test", "t", "nextest", "run", "r",
                      "bench", "doc", "d", "rustc", "rustdoc", "fix", "install", "miri", "mend"})
GLOBAL_VALUES = frozenset({"-C", "--config", "-Z", "--color", "--manifest-path", "--lockfile-path",
                          "--target-dir", "--target", "-j", "--jobs", "-p", "--package", "--exclude",
                          "-F", "--features", "--profile", "--message-format", "--artifact-dir", "--out-dir"})
PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36
LIBC = ctypes.CDLL(None, use_errno=True)


def building_call(arguments: Sequence[str]) -> bool:
    values = iter(arguments)
    for value in values:
        if value in GLOBAL_VALUES:
            _ = next(values, "")
        elif not value.startswith(("-", "+")):
            return value in BUILDING
    return False


def cargo_path(explicit: str, front: Path) -> tuple[str, str]:
    """Keep the admission front before cargo-tile without recursive calls."""
    front = front.resolve()
    entries = [entry for entry in os.get_exec_path() if Path(entry or ".").resolve() != front.parent]
    if explicit:
        command = str(Path(explicit).absolute())
        directory = str(Path(command).parent)
        if directory in entries:
            entries = entries[entries.index(directory):]
        else:
            entries = [directory, *entries]
        return command, os.pathsep.join(entries)
    for index, directory in enumerate(entries):
        candidate = Path(directory or ".") / "cargo"
        if candidate.is_file() and os.access(candidate, os.X_OK) and candidate.resolve() != front:
            return str(candidate.absolute()), os.pathsep.join(entries[index:])
    raise FileNotFoundError("cargo: no cargo executable after the admission front")


def capture_progress(stream: int, sidecar: Path) -> None:
    parser = progress.CounterParser()
    while data := os.read(stream, 65536):
        _ = os.write(sys.stderr.fileno(), data)
        counter = parser.consume(data.decode(errors="replace"), complete=False)
        if isinstance(counter, progress.ProgressCounter):
            progress.publish_counter(sidecar, counter)
            progress.progress_path(sidecar, "json").chmod(0o644)
        else:
            progress.progress_path(sidecar, "json").unlink(missing_ok=True)


def cancel(number: int, frame: object) -> None:
    del frame
    raise SystemExit(128 + number)


def end_with_front(front_pid: int) -> None:
    if LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(1)
    if os.getppid() != front_pid:
        os._exit(1)


def run_cargo(command: str, arguments: Sequence[str], child_path: str) -> int:
    pid = os.getpid()
    start = memory_admit.process_start_time(pid)
    if start is None:
        raise RuntimeError("cargo admission owner has no process identity")
    argv = [command, *arguments]
    repository, worktree = step_estimator.git_identity(argv, os.getcwd())
    width = records.test_threads(argv)
    test_width: records.TestWidth = records.RequestedTestWidth(width) if width else records.UnspecifiedTestWidth()
    with tempfile.TemporaryDirectory(prefix=f"build-admission-ci-{os.getuid()}-") as folder:
        directory = Path(folder)
        directory.chmod(0o755)
        sidecar = directory / "cargo.cgroup"
        _ = sidecar.write_text("")
        sidecar.chmod(0o644)
        request = wire.ServiceEstimateRequest(client.request_id(), wire.Owner(pid, start), tuple(argv), os.getcwd(),
            client.CLOCK.now(), str(sidecar), repository, worktree,
            os.environ.get("RUNNER_NAME", ""), os.environ.get("GITHUB_JOB", ""), test_width)
        output = client.WaitOutput(configuration=wire.PROTOCOL_ACQUISITION_TIMING)
        grant = client.service_admit(request, output)
        if not isinstance(grant, wire.RunnerAdmitted):
            if isinstance(grant, wire.Refused):
                print(grant.reason, file=sys.stderr)
            return 1
        output.finished()
        place = queue_place.QueuePlace(sidecar, sidecar, configuration=grant.execution,
            purpose=queue_place.ProgramPurpose.RUNNER, arguments=tuple(argv))
        environment = dict(os.environ, PATH=child_path, CARGO_TERM_PROGRESS_WHEN="always", CARGO_TERM_PROGRESS_WIDTH="80")
        status = 1
        cleanup = ExitStack()
        try:
            if LIBC.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "cannot adopt cargo descendants")
            # The main thread forks because the death signal follows the thread that forks.
            process = place.popen(argv, env=environment, stderr=subprocess.PIPE,
                                  preexec_fn=partial(end_with_front, pid))
            stream = process.stderr
            if stream is None:
                raise RuntimeError("cargo progress pipe is unavailable")
            reader = threading.Thread(target=capture_progress, args=(stream.fileno(), sidecar), daemon=True)
            reader.start()
            _ = cleanup.callback(stream.close)
            _ = cleanup.callback(reader.join)
            status = process.wait()
        except SystemExit as error:
            status = error.code if isinstance(error.code, int) else 1
            raise
        finally:
            with queue_place.defer_cleanup_signals():
                try:
                    place.finish(failed=status != 0)
                finally:
                    try:
                        cleanup.close()
                    finally:
                        try:
                            while os.waitpid(-1, os.WNOHANG)[0]:
                                pass
                        except ChildProcessError:
                            pass
                        finally:
                            _ = client.release(grant.reservation, status, grant.cleanup_timing)
        return status if status >= 0 else 128 - status


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    explicit = ""
    front = Path(__file__)
    if arguments[:1] == ["--front"]:
        front, arguments = Path(arguments[1]), arguments[2:]
    if arguments[:1] == ["--cargo"]:
        explicit, arguments = arguments[1], arguments[2:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    command, child_path = cargo_path(explicit, front)
    if not building_call(arguments):
        os.execve(command, [command, *arguments], dict(os.environ, PATH=child_path))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        _ = signal.signal(number, cancel)
    return run_cargo(command, arguments, child_path)


if __name__ == "__main__":
    raise SystemExit(main())
