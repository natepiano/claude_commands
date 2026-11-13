"""Real verify.sh Mac fixture with disposable tools."""
from __future__ import annotations

import unittest
import json
import os
import runpy
import select
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast, final
from unittest import mock
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import TARGETS_WRAPPER
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, VERIFY as VERIFY, stub_build_tool_path as stub_build_tool_path, processes_carrying
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS, FixtureHang


@final
class VerifyMacFixtureTests(VerifyMacOffloadFixture):
    def test_targets_wrapper_interrupted_publication_keeps_complete_state(self) -> None:
        arguments = ["--leave-hook", "hook with spaces \N{SNOWMAN}"]
        publications = (
            ("wait-local", "local-wait-args", json.dumps(arguments), ""),
            ("wait-local", "local-wait-args", json.dumps(arguments), '["previous complete args"]'),
            ("wait-local", "local-wait-added", "added\n", ""),
            ("register-local", "registered", "registered\n", ""),
        )
        replace = os.replace
        for index, (command, name, content, previous) in enumerate(publications):
            with self.subTest(name=name, previous=previous):
                root = self.runner_state / str(index)
                root.mkdir()
                target = root / name
                if previous:
                    _ = target.write_text(previous, encoding="utf-8")

                def interrupt_publication(source: str | Path, destination: str | Path) -> None:
                    if Path(destination) == target:
                        self.assertEqual(Path(source).read_text(encoding="utf-8"), content)
                        self.assertEqual(target.exists(), bool(previous))
                        if previous:
                            self.assertEqual(target.read_text(encoding="utf-8"), previous)
                        raise OSError("interrupted publication")
                    replace(source, destination)

                def run_wrapper() -> None:
                    with self.assertRaises(SystemExit) as exited:
                        exec(TARGETS_WRAPPER, {"__name__": "__main__"})
                    self.assertEqual(exited.exception.code, 10)

                with mock.patch.dict(os.environ, {**self.environment,
                        "TEST_RUNNER_STATE": str(root), "TEST_LOCAL_WAIT_SECONDS": "1",
                        "BUILDLOG_HOLD_WAIT_FILE": str(root / "hold-wait"),
                        "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets/build_targets.py")}), \
                        mock.patch.object(sys, "argv", ["targets-wrapper", command, *arguments]), \
                        mock.patch.object(runpy, "run_path", side_effect=SystemExit(10)):
                    with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=interrupt_publication):
                        with self.assertRaisesRegex(OSError, "interrupted publication"):
                            run_wrapper()
                    self.assertEqual(target.exists(), bool(previous))
                    if previous:
                        self.assertEqual(target.read_text(encoding="utf-8"), previous)
                    self.assertEqual(list(root.glob(f".{name}.*")), [])
                    run_wrapper()
                self.assertEqual(target.read_text(encoding="utf-8"), content)
                self.assertEqual(list(root.glob(f".{name}.*")), [])

    def test_fixture_waits_for_a_detached_child_to_finish_before_checking_for_leaks(self) -> None:
        release = self.runner_state / "recorder-release"
        child = subprocess.Popen(
            [sys.executable, self.environment["TEST_WAIT_HELPER"], str(release)],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        observe_exits = select.select
        observed: list[int] = []

        def release_on_exit_wait(readers: list[int], writers: list[int], errors: list[int],
                                 timeout: float) -> tuple[list[int], list[int], list[int]]:
            observed.extend(readers)
            release.touch()
            return observe_exits(readers, writers, errors, timeout)

        try:
            with mock.patch.object(select, "select", side_effect=release_on_exit_wait):
                self.assert_nothing_outlives()
            output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertTrue(observed, "cleanup did not observe the live child's exit")
            self.assertEqual(child.returncode, 0, output + errors)
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_exceptional_verify_exit_stops_its_processes_before_the_next_case(self) -> None:
        for failure in (subprocess.TimeoutExpired("verify", 0), FixtureHang("the fixture stopped")):
            with self.subTest(failure=type(failure).__name__):
                process = subprocess.Popen([sys.executable, "-c", """
import subprocess
from pathlib import Path
import os
from scripts.direct.wait_for_test_state import wait_for_file
child = subprocess.Popen(["sleep", "300"])
print("ready", flush=True)
wait_for_file(Path(os.environ["TEST_RUNNER_STATE"]) / "exception-release")
"""], cwd=VERIFY.parents[2], env=self.environment, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, start_new_session=True)
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline(), "ready\n")
                communicate = process.communicate
                first = True

                def interrupt(timeout: float) -> tuple[str, str]:
                    nonlocal first
                    if first:
                        first = False
                        raise failure
                    return communicate(timeout=timeout)

                try:
                    with mock.patch.object(self, "start_verify", return_value=process), \
                         mock.patch.object(process, "communicate", side_effect=interrupt), \
                         self.assertRaises(type(failure)):
                        _ = self.verify("test", "sample")
                    self.assertIsNotNone(process.poll())
                    self.assertEqual(processes_carrying(self.child_marker()), set())
                finally:
                    self.stop_verify(process)

    def test_build_tools_cannot_use_the_host_path(self) -> None:
        host_tools = self.root / "host-tools"
        host_tools.mkdir()
        reached = self.root / "host-build-tool-ran"
        for name in ("cargo", "rustc", "nextest"):
            self.write_executable(host_tools / name, f'#!/bin/sh\ntouch "{reached}"\nexit 98\n')
        private_tools = self.root / "private-tools"
        private_tools.mkdir()
        with mock.patch.dict(os.environ, {"PATH": f"{host_tools}{os.pathsep}{os.environ['PATH']}"}):
            private_path = stub_build_tool_path(private_tools)
        for name in ("cargo", "rustc", "nextest"):
            with self.subTest(tool=name):
                result = subprocess.run([name, "--version"], env={**self.environment, "PATH": private_path},
                                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 99, result.stdout + result.stderr)
        self.assertFalse(reached.exists())

    def test_hold_fixture_reaps_its_recorders_without_waiting_for_an_existing_recorder(self) -> None:
        for delayed_start in (False, True):
            with self.subTest(delayed_start=delayed_start):
                tree = self.root / f"record-tree-{delayed_start}/scripts"
                (tree / "build_hold").mkdir(parents=True)
                (tree / "buildlog").mkdir()
                (tree / "lib").symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
                (tree / "direct").symlink_to(VERIFY.parent.parent / "direct", target_is_directory=True)
                release = tree.parent / "recorder-release"
                completed = tree.parent / "recorder-completed"
                self.write_executable(tree / "buildlog/record.py", f'''
from pathlib import Path
from scripts.direct.wait_for_test_state import wait_for_file
wait_for_file(Path({str(release)!r}))
Path({str(completed)!r}).touch()
''')
                outside_release = threading.Event()
                outside = threading.Thread(target=outside_release.wait, name="hold-event-recorder", daemon=True)
                outside.start()
                original_join = threading.Thread.join
                original_start = threading.Thread.start
                original_wait = threading.Event.wait
                owner = threading.current_thread()
                starting, allow_start, closing = threading.Event(), threading.Event(), threading.Event()
                owned: list[threading.Thread] = []
                failures: list[BaseException] = []

                def release_on_join(thread: threading.Thread, timeout: float | None = None) -> None:
                    if thread.name == "hold-event-recorder":
                        self.assertIsNot(thread, outside, "fixture tried to wait for a recorder it did not start")
                        owned.append(thread)
                        release.touch()
                    original_join(thread, timeout)

                def delay_recorder_start(thread: threading.Thread) -> None:
                    if delayed_start and thread.name == "hold-event-recorder":
                        starting.set()
                        if not allow_start.wait(TEST_HANG_GUARD_SECS):
                            raise AssertionError("fixture never waited for recorder startup")
                    original_start(thread)

                def release_start_on_wait(event: threading.Event, timeout: float | None = None) -> bool:
                    if threading.current_thread() is owner and closing.is_set():
                        allow_start.set()
                    return original_wait(event, timeout)

                def dispatch() -> None:
                    try:
                        _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                            build_hold.HolderIdentity(f"hana-{delayed_start}", "hana"), "fixture recording", build_hold.NoReleaseEta(),
                            datetime.now().astimezone())
                    except BaseException as error:
                        failures.append(error)

                launcher = threading.Thread(target=dispatch, name="fixture-hold-start")
                existing = set(threading.enumerate())
                try:
                    with mock.patch.object(build_hold, "__file__", str(tree / "build_hold/build_hold.py")), \
                            mock.patch.object(threading.Thread, "join", release_on_join), \
                            mock.patch.object(threading.Thread, "start", delay_recorder_start), \
                            mock.patch.object(threading.Event, "wait", release_start_on_wait):
                        with self.hold_environment():
                            if delayed_start:
                                launcher.start()
                                self.assertTrue(starting.wait(TEST_HANG_GUARD_SECS), "recorder startup did not begin")
                            else:
                                dispatch()
                            closing.set()
                        self.assertTrue(completed.exists(), "fixture returned while its recorder still owned its files")
                    if delayed_start:
                        original_join(launcher, TEST_HANG_GUARD_SECS)
                        self.assertFalse(launcher.is_alive())
                    self.assertEqual(failures, [])
                    self.assertEqual(len(owned), 1)
                    self.assertFalse(owned[0].is_alive())
                    self.assertTrue(outside.is_alive())
                finally:
                    allow_start.set()
                    release.touch()
                    outside_release.set()
                    if launcher.ident is not None:
                        original_join(launcher, TEST_HANG_GUARD_SECS)
                    for thread in threading.enumerate():
                        if thread is outside or (thread not in existing and thread.name == "hold-event-recorder"):
                            original_join(thread, TEST_HANG_GUARD_SECS)
                            self.assertFalse(thread.is_alive())

    def test_hold_fixture_timeout_reaps_all_children_and_owned_threads_before_restoring_home(self) -> None:
        tree = self.root / "blocked-record-tree/scripts"
        (tree / "build_hold").mkdir(parents=True)
        (tree / "buildlog").mkdir()
        (tree / "lib").symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
        ready = self.root / "blocked-recorders"
        ready.mkdir()
        self.write_executable(tree / "buildlog/record.py", f'''
import os, signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}, sys.argv[4]).write_text(str(os.getpid()))
signal.pause()
''')
        children: list[subprocess.Popen[bytes]] = []
        threads: list[threading.Thread] = []
        launch = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)
        start = threading.Thread.start
        caller_home = os.environ.get("HOME")

        def capture_child(*arguments: object, **options: object) -> subprocess.Popen[bytes]:
            child = launch(*arguments, **options)
            children.append(child)
            return child

        def capture_thread(thread: threading.Thread) -> None:
            if thread.name == "hold-event-recorder":
                threads.append(thread)
            start(thread)

        try:
            with mock.patch.object(build_hold, "__file__", str(tree / "build_hold/build_hold.py")), \
                    mock.patch.object(subprocess, "Popen", side_effect=capture_child), \
                    mock.patch.object(threading.Thread, "start", capture_thread):
                with self.assertRaisesRegex(AssertionError, "fixture hold recorder never exited"):
                    with self.hold_environment(timeout=0):
                        for key in ("first", "second"):
                            _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                                build_hold.HolderIdentity(key, key), "blocked recording", build_hold.NoReleaseEta(),
                                datetime.now().astimezone())
                            self.wait_for_path(ready / key)
                self.assertEqual(len(children), 2)
                self.assertEqual(len(threads), 2)
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(os.environ.get("HOME"), caller_home)
        finally:
            # Also contain a deliberately broken implementation during regression checks.
            for child in children:
                if child.poll() is None:
                    child.kill()
                _ = child.wait()
            for thread in threads:
                thread.join(timeout=TEST_HANG_GUARD_SECS)


if __name__ == "__main__":
    _ = unittest.main()
