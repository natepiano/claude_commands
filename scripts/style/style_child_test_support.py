"""Run private style tools through one fake at the queue boundary."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Unpack, cast, final
from unittest import mock

from scripts.build_queue import queue_place
from scripts.lint.memory_test_support import admission_configuration_fixture


@final
class UnqueuedStylePlace:
    """A fixture program lifetime without admission or shared state."""

    def __init__(self, repo: Path, before_start: Callable[[Path, Sequence[str]], None],
                 real_startup: bool, before_started: Callable[[], None]) -> None:
        self.real_startup: bool = real_startup
        self.before_started: Callable[[], None] = before_started
        self.startups: list[queue_place.QueuePlace] = []
        self.storage: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(prefix="style-startup-")
        self.repo: Path = repo
        self.before_start: Callable[[Path, Sequence[str]], None] = before_start
        self.arguments: list[tuple[str, ...]] = []
        self.processes: list[subprocess.Popen[bytes]] = []
        self.closed: bool = False
        self.on_started: Callable[[], None] = lambda: None

    def popen(self, arguments: Sequence[str], **options: Unpack[queue_place.PopenArguments]) -> subprocess.Popen[bytes]:
        self.arguments.append(tuple(arguments))
        self.before_start(self.repo, arguments)
        if self.real_startup:
            sidecar = Path(self.storage.name) / "program.cgroup"
            _ = sidecar.write_text("")
            place = queue_place.QueuePlace(Path(self.storage.name) / "private.reservation", sidecar,
                                           configuration=admission_configuration_fixture())
            self.startups.append(place)
            def started() -> None:
                self.before_started()
                self.on_started()
            place.on_started = started
            try:
                return place.popen(arguments, **options)
            finally:
                if isinstance(place.program, queue_place.StartedProgram):
                    self.processes.append(place.program.process)
        process = subprocess.Popen(arguments, **options)
        self.processes.append(process)
        self.on_started()
        return process

    def close(self) -> None:
        for place in self.startups:
            place.finish(failed=True)
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    _ = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.storage.cleanup()
        self.closed = True


@final
class StyleQueueFake:
    """Record each requested place and run only the fixture's direct children."""

    def __init__(self) -> None:
        self.real_startup: bool = False
        self.before_started: Callable[[], None] = lambda: None
        self.calls: list[tuple[str, Path]] = []
        self.batch_calls: list[tuple[tuple[str, ...], bool]] = []
        self.refusals: dict[str, str] = {}
        self.places: list[UnqueuedStylePlace] = []
        self.before_start: Callable[[Path, Sequence[str]], None] = lambda _repo, _arguments: None
        self.after_close: Callable[[], None] = lambda: None

    @contextmanager
    def __call__(self, program: str | Sequence[str], repo: Path, *, batch: bool = False) -> Generator[queue_place.QueuePlace]:
        arguments = (program,) if isinstance(program, str) else tuple(program)
        name = Path(arguments[0]).name
        self.calls.append((name, repo))
        self.batch_calls.append((arguments, batch))
        if name in self.refusals:
            raise queue_place.QueuePlaceRefused(self.refusals[name])
        place = UnqueuedStylePlace(repo, self.before_start, self.real_startup, self.before_started)
        self.places.append(place)
        try:
            yield cast(queue_place.QueuePlace, cast(object, place))
        finally:
            place.close()
            self.after_close()


@contextmanager
def unqueued_style_children() -> Generator[StyleQueueFake]:
    """Replace the queue boundary for direct calls and child CLI fixtures."""
    fake = StyleQueueFake()
    with mock.patch.object(queue_place, "queue_place", new=fake):
        yield fake


def style_cli_command(script: Path, *arguments: str) -> list[str]:
    """Use the same queue fake in a child Python interpreter."""
    bootstrap = (
        "import runpy, sys; "
        "from scripts.style.style_child_test_support import unqueued_style_children; "
        "sys.argv = sys.argv[1:]\n"
        "with unqueued_style_children():\n"
        "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    return [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", bootstrap, str(script), *arguments]


def install_refusal_tools(root: Path) -> None:
    """Format private changed files and complete private compiler and verification commands."""
    cargo = root / "tools/cargo"
    cargo.unlink(missing_ok=True)
    _ = cargo.write_text(f"#!{sys.executable}\n" + '''import subprocess
import sys
from pathlib import Path

if "fmt" in sys.argv:
    changed = subprocess.run(["git", "diff", "--name-only"], capture_output=True, check=True).stdout
    created = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, check=True).stdout
    for name in set((changed + created).decode().splitlines()):
        path = Path(name)
        if path.suffix == ".rs" and path.is_file():
            path.write_bytes(b"\\n" + path.read_bytes())
''')
    cargo.chmod(0o755)
    bash = root / "tools/bash"
    bash.unlink(missing_ok=True)
    _ = bash.write_text("#!/bin/sh\nexit 0\n")
    bash.chmod(0o755)
