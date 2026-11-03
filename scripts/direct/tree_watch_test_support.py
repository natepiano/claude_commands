"""Private Git workspaces and an event-driven clock for the real watcher loop."""
from __future__ import annotations

from contextlib import chdir
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
import time
from threading import Thread
import traceback
from types import FunctionType, ModuleType, SimpleNamespace
from typing import ClassVar, override
import unittest
from unittest import mock

from scripts.direct import tree_watch
from scripts.production.cli_fixture import invoke_cli
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

WATCHER = Path(tree_watch.__file__)
WATCH_INTERVAL = 0.5
# Preserve the real change-detection and parent-exit speed bound.
WATCH_DEADLINE = WATCH_INTERVAL + 2

def write_workspace(root: Path) -> dict[str, object]:
    """A virtual workspace, including an unrelated member and a path dependency."""
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["sample", "dep", "unrelated"]\nresolver = "2"\n',
        "Cargo.lock": "# fixture lock\nversion = 3\n",
        ".cargo/config.toml": "[build]\njobs = 2\n",
        "rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n',
        "rust-toolchain": "stable\n",
        "docs/design.md": "external design\n",
        "plans/work.md": "external plan\n",
        "scripts/helper.py": "# external helper\n",
        "session/board.log": "session state\n",
        ".gitignore": "target/\n",
        "sample/fixtures/input.bin": "fixture data\n",
        "sample/src/removed.rs": "// removed input\n",
    }
    packages: list[dict[str, object]] = []
    for name in ("sample", "dep", "unrelated"):
        files[f"{name}/Cargo.toml"] = f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
        files[f"{name}/src/lib.rs"] = "pub fn value() -> u8 { 1 }\n"
        dependencies: list[dict[str, object]] = ([{"name": "dep", "path": str(root / "dep"), "kind": None,
                          "optional": False, "uses_default_features": True, "features": []}]
                        if name == "sample" else [])
        packages.append({"id": f"{name}-id", "name": name, "features": {},
                         "manifest_path": str(root / name / "Cargo.toml"),
                         "dependencies": dependencies,
                         "targets": [{"name": name, "kind": ["lib"], "test": True,
                                      "src_path": str(root / name / "src/lib.rs"),
                                      "required-features": []}]})
    files["sample/Cargo.toml"] += '\n[dependencies]\ndep = { path = "../dep" }\n'
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "workspace fixture"]):
        _ = subprocess.run(["git", "-C", str(root), *args], check=True,
                           capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
    return {"workspace_root": str(root), "workspace_members": [f"{name}-id" for name in
            ("sample", "dep", "unrelated")], "packages": packages,
            "target_directory": str(root / "target"), "metadata": {}}

class StopWatch(BaseException):
    """Release a parked watcher during cleanup without advancing its clock."""


class EventWatcher:
    """Execute production main; each permit advances one real snapshot iteration.

    Only main's sleep is replaced. Scope, Git snapshots, exception handling,
    parent liveness and publication all remain production code. A notice is sent
    after an iteration reaches sleep, or exits, so absence checks need no delay.
    """

    def __init__(self, arguments: list[str], metadata: dict[str, object]) -> None:
        self.permits: Queue[bool] = Queue()
        self.notices: Queue[float | None] = Queue()
        self.returncode: int | None = None
        self.errors: str = ""
        module = ModuleType("tree_watch_clock_fixture")
        vars(module).update(vars(tree_watch))
        vars(module)["sys"] = SimpleNamespace(stdin=io.StringIO(json.dumps(metadata)))
        vars(module)["time"] = SimpleNamespace(sleep=self.sleep)
        main = FunctionType(tree_watch.main.__code__, vars(module))

        def run() -> None:
            try:
                self.returncode = main(arguments)
            except StopWatch:
                self.returncode = -9
            except BaseException:
                self.errors = traceback.format_exc()
                self.returncode = 1
            finally:
                self.notices.put(None)

        self.thread: Thread = Thread(target=run, daemon=True)
        self.thread.start()
        self.observed()

    def sleep(self, interval: float) -> None:
        self.notices.put(interval)
        if not self.permits.get(timeout=TEST_HANG_GUARD_SECS):
            raise StopWatch()

    def observed(self) -> None:
        interval = self.notices.get(timeout=TEST_HANG_GUARD_SECS)
        if interval != WATCH_INTERVAL:
            raise AssertionError(f"watcher did not complete an unchanged iteration: {self.returncode}, {self.errors}")

    def advance(self) -> None:
        self.permits.put(True)

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.permits.put(False)

    def communicate(self, timeout: float) -> tuple[str, str]:
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("event-driven tree_watch.main", timeout)
        return "", self.errors


class TreeWatchFixture(unittest.TestCase):
    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    fixture_metadata: ClassVar[dict[str, object]]
    nested_metadata: ClassVar[dict[str, object]]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]
    metadata: dict[str, object]  # pyright: ignore[reportUninitializedInstanceVariable]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="tree-watch-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name).resolve() / "case"
        cls.fixture_root.mkdir()
        cls.fixture_snapshot = cls.fixture_root.parent / "snapshot"
        cls.fixture_metadata = write_workspace(cls.fixture_root)
        tools = cls.fixture_root / "tools"
        tools.mkdir()
        rustc = tools / "rustc"
        _ = rustc.write_text("#!/bin/sh\necho 'rustc fixture'\n")
        rustc.chmod(0o755)
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        cls.nested_metadata = write_workspace(cls.fixture_root / "rust")
        shutil.rmtree(cls.fixture_root / "rust/.git")
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot.with_name("nested-untracked"))
        for arguments in (["add", "rust"], ["-c", "user.name=Fixture", "-c",
                           "user.email=fixture@example.invalid", "commit", "-qm", "nested workspace"]):
            _ = subprocess.run(["git", *arguments], cwd=cls.fixture_root, check=True,
                               capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot.with_name("nested-committed"))
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.root = self.fixture_root
        _ = shutil.copytree(self.fixture_snapshot, self.root)
        self.addCleanup(shutil.rmtree, self.root)
        self.metadata = deepcopy(self.fixture_metadata)
        self.environment = {**os.environ, "PATH": f"{self.root / 'tools'}:{os.environ['PATH']}",
                            "VERIFY_TREE_WATCH_S": str(WATCH_INTERVAL),
                            "UNIT_SESSION_DIR": str(self.root / "session"),
                            "UNIT_RUN_DIR": str(self.root / "session"),
                            "PYTHONDONTWRITEBYTECODE": "1"}
        _ = self.enterContext(chdir(self.root))
        self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True))

    def nested_workspace(self, *, committed: bool = False) -> Path:
        shutil.rmtree(self.root)
        snapshot = self.fixture_snapshot.with_name("nested-committed" if committed else "nested-untracked")
        _ = shutil.copytree(snapshot, self.root)
        # Re-enter the replacement directory; cwd otherwise refers to the removed inode.
        os.chdir(self.root)
        self.metadata = deepcopy(self.nested_metadata)
        return self.root / "rust"

    def key_arguments(self, package: str = "sample") -> list[str]:
        scope = [] if package == "--workspace" else ["--package", package]
        return [*scope, "--"]

    def key(self, package: str = "sample") -> str:
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.metadata))):
            result = invoke_cli(tree_watch, lambda: tree_watch.main(["--watch-key", *self.key_arguments(package)]),
                                [], self.environment, self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout.strip(), r"^[a-f0-9]{64}$")
        return result.stdout.strip()

    def start_watcher(self, parent: int, package: str = "sample", *, real_process: bool = False) -> tuple[EventWatcher | subprocess.Popen[str], Path, str]:
        before = self.key(package)
        start = self.root / "start-key"
        fired = self.root / "fired-key"
        fired.unlink(missing_ok=True)
        _ = start.write_text(before + "\n")
        if real_process:
            process = subprocess.Popen([sys.executable, str(WATCHER), str(start), str(fired),
                                        str(parent), *self.key_arguments(package)],
                                       cwd=self.root, env=self.environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert process.stdin is not None
            _ = process.stdin.write(json.dumps(self.metadata))
            process.stdin.close()
            process.stdin = None
        else:
            process = EventWatcher([str(start), str(fired), str(parent), *self.key_arguments(package)], self.metadata)
        self.addCleanup(self.stop_watcher, process)
        return process, fired, before

    @staticmethod
    def stop_watcher(process: EventWatcher | subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def assert_fires(self, process: EventWatcher | subprocess.Popen[str], fired: Path, before: str) -> None:
        started = time.monotonic()
        if isinstance(process, EventWatcher):
            process.advance()
        output, errors = process.communicate(timeout=WATCH_DEADLINE)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertLess(time.monotonic() - started, WATCH_DEADLINE)
        self.assertTrue(fired.exists(), "watcher exited without publishing a changed key")
        self.assertNotEqual(fired.read_text().strip(), before)
        self.assertEqual(fired.read_text().strip(), self.key())

    def assert_observes_without_firing(self, process: EventWatcher | subprocess.Popen[str]) -> None:
        self.assertIsInstance(process, EventWatcher)
        assert isinstance(process, EventWatcher)
        # The original negative window covered two 0.5 s intervals. Complete
        # both snapshots explicitly, and check the watcher remains live after each.
        for _ in range(2):
            process.advance()
            process.observed()
            self.assertIsNone(process.poll(), "watcher exited during an unchanged or unreadable snapshot")
