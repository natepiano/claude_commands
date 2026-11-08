"""Retained worker clocks, file events and run status."""
from __future__ import annotations

import fcntl
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TextIO, cast, override
from unittest.mock import patch

from scripts.agents import worker_lifecycle
from scripts.agents import codex_mesh_test_support as fixture_support
from scripts.whoami.note_file import note_name
from scripts.agents.codex_mesh_test_support import FixtureLoopBudget, LifecycleTestClock, StubAppServer
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.unit_runs import STATE_FILE

class StubServerShutdownTests(unittest.TestCase):
    def test_close_wakes_a_blocking_listener_and_reaps_its_thread(self) -> None:
        server = StubAppServer(lambda _request: [])
        try:
            LauncherHangGuard('test_close_wakes_a_blocking_listener_and_reaps_its_thread').event(server.accept_waiting)
            self.assertIsNone(server.listener.gettimeout())
        finally:
            server.close()
        self.assertFalse(server.thread.is_alive())
        self.assertEqual(server.listener.fileno(), -1)
        self.assertEqual(server.close_reader.fileno(), -1)
        self.assertEqual(server.close_writer.fileno(), -1)


class LifecycleClockTests(unittest.TestCase):
    def test_hang_guard_uses_one_wall_clock_when_lifecycle_time_is_patched(self) -> None:
        with patch.object(time, "monotonic", return_value=10.0):
            guard = LauncherHangGuard("fresh machine")
            remaining = guard.remaining()
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, TEST_HANG_GUARD_SECS)

    def test_registry_time_is_excluded_during_resolution_and_after_failure(self) -> None:
        ticks = [1000.0]
        clock = LifecycleTestClock()

        def resolve() -> None:
            ticks[0] += 10
            self.assertEqual(clock.time(), 1000)
            self.assertEqual(clock.monotonic(), 1000)
            raise ValueError("resolver failed")

        with (patch.object(time, "time", new=lambda: ticks[0]),
              patch.object(time, "monotonic", new=lambda: ticks[0]),
              patch.object(time, "perf_counter", new=lambda: ticks[0])):
            with self.assertRaisesRegex(ValueError, "resolver failed"):
                clock.registry_resolution(resolve)()
            ticks[0] += 1
            self.assertEqual(clock.time(), 1001)
            self.assertEqual(clock.monotonic(), 1001)

    def test_readiness_excludes_registry_time_but_still_expires_without_an_event(self) -> None:
        for delayed in (False, True):
            with self.subTest(delayed=delayed):
                ticks = [1000.0]
                clock = LifecycleTestClock()
                ready = threading.Event()
                waits: list[float | None] = []

                def resolve() -> None:
                    ticks[0] += 10

                def wait(timeout: float | None = None) -> bool:
                    waits.append(timeout)
                    if delayed and len(waits) == 1:
                        clock.registry_resolution(resolve)()
                    else:
                        ticks[0] += 1
                        if delayed:
                            ready.set()
                    return ready.is_set()

                with (patch.object(time, "monotonic", new=lambda: ticks[0]),
                      patch.object(time, "perf_counter", new=lambda: ticks[0]),
                      patch.object(ready, "wait", new=wait)):
                    self.assertEqual(clock.wait_for_submission(ready, timeout=1), delayed)
                self.assertEqual(waits, [1, 1] if delayed else [1])

    def test_result_wait_excludes_registry_time_and_preserves_task_errors(self) -> None:
        for completed in (False, True):
            with self.subTest(completed=completed):
                ticks = [1000.0]
                clock = LifecycleTestClock()
                ready = threading.Event()
                future = Future[int]()
                waits: list[float | None] = []

                def resolve() -> None:
                    ticks[0] += 10

                def wait(timeout: float | None = None) -> bool:
                    waits.append(timeout)
                    if len(waits) == 1:
                        clock.registry_resolution(resolve)()
                    else:
                        ticks[0] += 1
                        if completed:
                            future.set_result(17)
                    return ready.is_set()

                with (patch.object(time, "monotonic", new=lambda: ticks[0]),
                      patch.object(time, "perf_counter", new=lambda: ticks[0]),
                      patch.object(threading, "Event", new=lambda: ready),
                      patch.object(ready, "wait", new=wait)):
                    if completed:
                        self.assertEqual(clock.result(future, timeout=1), 17)
                    else:
                        with self.assertRaises(TimeoutError):
                            _ = clock.result(future, timeout=1)
                self.assertEqual(waits, [1, 1])
        failed = Future[int]()
        failed.set_exception(ValueError("worker assertion"))
        with self.assertRaisesRegex(ValueError, "worker assertion"):
            _ = LifecycleTestClock().result(failed, timeout=1)

    def test_callback_budget_uses_the_lifecycle_clock_and_retains_its_limits(self) -> None:
        ticks = [1000.0]
        clock = LifecycleTestClock()
        with (patch.object(time, "monotonic", new=lambda: ticks[0]),
              patch.object(time, "perf_counter", new=lambda: ticks[0])):
            budget = FixtureLoopBudget("callback", seconds=3, clock=clock.monotonic)

            def resolve() -> None:
                ticks[0] += 10
                self.assertEqual(budget.check(), 3)

            clock.registry_resolution(resolve)()
            ticks[0] += 2
            self.assertEqual(budget.check(), 1)
            ticks[0] += 1
            with self.assertRaisesRegex(AssertionError, "fixture deadline"):
                _ = budget.check()
            bounded = FixtureLoopBudget("callback count", max_calls=1, clock=clock.monotonic)
            _ = bounded.check()
            with self.assertRaisesRegex(AssertionError, "fixture calls"):
                _ = bounded.check()


class RecordedTurnSettingsTests(unittest.TestCase):
    def test_accepted_turn_settings_take_precedence_over_legacy_roster_fields(self) -> None:
        settings = worker_lifecycle.recorded_turn_settings({
            "turn_model": "accepted-model", "turn_effort": "high",
            "model": "historical-model", "effort": "low",
        })
        self.assertEqual((settings.model, settings.effort), ("accepted-model", "high"))

    def test_an_accepted_model_without_effort_does_not_borrow_the_legacy_effort(self) -> None:
        for effort in ({}, {"turn_effort": ""}, {"turn_effort": 123}):
            with self.subTest(effort=effort):
                settings = worker_lifecycle.recorded_turn_settings({
                    "turn_model": "accepted-model", "model": "historical-model", "effort": "max", **effort,
                })
                self.assertEqual((settings.model, settings.effort), ("accepted-model", ""))

    def test_a_missing_or_empty_accepted_model_uses_the_legacy_pair(self) -> None:
        for model in ({}, {"turn_model": ""}, {"turn_model": 123}):
            with self.subTest(model=model):
                settings = worker_lifecycle.recorded_turn_settings({
                    "turn_effort": "max", "model": "historical-model", "effort": "low", **model,
                })
                self.assertEqual((settings.model, settings.effort), ("historical-model", "low"))

    def test_unrecorded_settings_are_empty_and_legacy_fields_remain_supported(self) -> None:
        for record, expected in (({}, ("", "")), ({"model": "older-model"}, ("older-model", "")),
                                 ({"model": "older-model", "effort": "medium"}, ("older-model", "medium"))):
            with self.subTest(record=record):
                settings = worker_lifecycle.recorded_turn_settings(record)
                self.assertEqual((settings.model, settings.effort), expected)


class RunEvidenceTests(unittest.TestCase):
    def test_file_changes_wake_a_waiter_after_an_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / STATE_FILE
            _ = path.write_text("old")
            reader, writer = socket.socketpair()
            self.addCleanup(reader.close)
            self.addCleanup(writer.close)
            watch = (worker_lifecycle.LinuxFileChanges if sys.platform.startswith("linux")
                     else worker_lifecycle.KqueueFileChanges)({path})
            self.addCleanup(watch.close)
            replacement = path.with_suffix(".new")
            _ = replacement.write_text("new")
            os.replace(replacement, path)
            self.assertEqual(watch.wait(reader, time.monotonic() + TEST_HANG_GUARD_SECS),
                             worker_lifecycle.StateFilesChanged(frozenset({path})))
            _ = writer.send(b"ready")
            self.assertEqual(watch.wait(reader, time.monotonic() + TEST_HANG_GUARD_SECS),
                             worker_lifecycle.SocketReady.READY)

    def test_finished_status_requires_the_finish_stamp_and_allows_reactivation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / STATE_FILE
            for value, expected in (({}, ""), ({"status": "ended"}, ""),
                    ({"status": "ended", "finished_at": "now"}, "ended"),
                    ({"status": "active", "finished_at": "now"}, "")):
                with self.subTest(value=value):
                    _ = path.write_text(json.dumps(value))
                    self.assertEqual(worker_lifecycle.finished_run_status(folder), expected)



class FixtureHomeSafetyTests(unittest.TestCase):
    def test_worker_selection_refuses_real_home_descendants_and_symlinks(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        real = root / "real"
        real.mkdir()
        alias = root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with patch(f"{fixture_support.__name__}.REAL_HOME", real):
            for home in (real, real / "nested", alias):
                with self.subTest(home=home), self.assertRaisesRegex(AssertionError, "real home"):
                    fixture_support.select_fixture_workers(home, notes=root / "notes")
        self.assertEqual(list(real.iterdir()), [])
        self.assertFalse((root / "notes").exists())

    def test_base_setup_rejects_unsafe_environment_before_creating_files(self) -> None:
        from scripts.agents.test_codex_mesh import MeshCommandFixture

        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        real = root / "real"
        real.mkdir()
        for name in ("HOME", "CODEX_HOME", "AGENT_NOTES_DIR", "MODELS_CONFIG_FILE"):
            environment = {key: str(root / "safe") for key in ("HOME", "CODEX_HOME", "AGENT_NOTES_DIR", "MODELS_CONFIG_FILE")}
            environment[name] = str(real / "nested")
            with (self.subTest(variable=name), patch(f"{fixture_support.__name__}.REAL_HOME", real),
                  patch.dict(os.environ, environment),
                  patch.object(tempfile, "TemporaryDirectory", wraps=tempfile.TemporaryDirectory) as temporary):
                fixture = MeshCommandFixture()
                try:
                    with self.assertRaisesRegex(AssertionError, name + " points into the real home"):
                        fixture.setUp()
                finally:
                    fixture.doCleanups()
                temporary.assert_not_called()

    def test_metadata_guard_catches_a_bypassed_refusal_in_an_absent_or_existing_tree(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                real = Path(directory)
                auth = real / ".codex/auth.json"
                if existing:
                    auth.parent.mkdir()
                    _ = auth.write_text("temporary account placeholder")
                before = fixture_support.fixture_write_metadata(real)
                with (patch(f"{fixture_support.__name__}.REAL_HOME", real),
                      patch(f"{fixture_support.__name__}.REAL_FIXTURE_METADATA", before)):
                    with self.assertRaisesRegex(AssertionError, "real home"):
                        fixture_support.select_fixture_workers(real, notes=real / "notes")
                    fixture_support.assert_real_codex_unchanged()
                    # Unrelated file changes do not change the account records.
                    session = real / ".codex/sessions/x.jsonl"
                    session.parent.mkdir(parents=True, exist_ok=True)
                    notes = real / "rust/hanadocs/agents"
                    notes.mkdir(parents=True)
                    live = [real / name for name in (".codex/config.toml", ".codex/hooks.json",
                                                     ".claude/scripts/lib/py", ".local/state/accounts/moves.jsonl")]
                    live.extend(notes / note_name(label) for label in ("codex1", "codex2"))
                    for text in ("first event", "another event"):
                        _ = session.write_text(text)
                        for path in live:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            _ = path.write_text(text)
                        fixture_support.assert_real_codex_unchanged()
                    # Inject the fault in the private fixture.
                    with patch(f"{fixture_support.__name__}.refuse_real_home"):
                        fixture_support.select_fixture_workers(real, notes=real / "rust/hanadocs/agents")
                    with self.assertRaises(AssertionError) as changed:
                        fixture_support.assert_real_codex_unchanged()
                    for name in (".codex/auth.json", ".claude/config/accounts.json",
                                 "rust/hanadocs/agents/codex9.md"):
                        self.assertIn(str(real / name), str(changed.exception))
        # Check an account directory created after import.
        for name in (".codex22/auth.json",):
            with self.subTest(path=name), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                real = Path(directory)
                path = real / name
                before = fixture_support.fixture_write_metadata(real)
                with (patch(f"{fixture_support.__name__}.REAL_HOME", real),
                      patch(f"{fixture_support.__name__}.REAL_FIXTURE_METADATA", before)):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_text("fixture content")
                    with self.assertRaisesRegex(AssertionError, "fixture targets"):
                        fixture_support.assert_real_codex_unchanged()
                    written = fixture_support.fixture_write_metadata(real)
                    with patch(f"{fixture_support.__name__}.REAL_FIXTURE_METADATA", written):
                        _ = path.write_text("changed fixture content")
                        with self.assertRaisesRegex(AssertionError, "fixture targets"):
                            fixture_support.assert_real_codex_unchanged()
                        path.unlink()
                        with self.assertRaisesRegex(AssertionError, "fixture targets"):
                            fixture_support.assert_real_codex_unchanged()
                    fixture_support.assert_real_codex_unchanged()


class LifecycleDeadlineTests(unittest.TestCase):
    session_dir: Path = Path()
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.lifecycle_clock: LifecycleTestClock = LifecycleTestClock()

    @override
    def setUp(self) -> None:
        self.session_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.lifecycle_clock = LifecycleTestClock()

    def test_sequential_lock_waits_share_one_monotonic_budget(self) -> None:
            first, second = self.session_dir / "first.lock", self.session_dir / "second.lock"
            self.lifecycle_clock.freeze()
            started = self.lifecycle_clock.monotonic()
            contended: list[str] = []
            flock = fcntl.flock
            with first.open("a+") as first_owner, second.open("a+") as second_owner:
                flock(first_owner, fcntl.LOCK_EX)
                flock(second_owner, fcntl.LOCK_EX)

                def acquire(descriptor: int | io.IOBase, mode: int) -> None:
                    try:
                        flock(descriptor, mode)
                    except BlockingIOError:
                        handle = descriptor if isinstance(descriptor, io.IOBase) else next((owner for owner in (first_owner, second_owner) if os.path.samestat(os.fstat(owner.fileno()), os.fstat(descriptor))), None)
                        assert handle is not None
                        contended.append(Path(str(cast(TextIO, cast(object, handle)).name)).name)
                        self.lifecycle_clock.advance(3)
                        if len(contended) == 1:
                            flock(first_owner, fcntl.LOCK_UN)
                        raise

                with (patch.object(fcntl, "flock", acquire),
                      patch.object(worker_lifecycle, "time", self.lifecycle_clock),
                      worker_lifecycle.operation_deadline(timeout=5) as deadline,
                      first.open("a+") as one, second.open("a+") as two):
                    self.assertEqual(deadline, started + 5)
                    worker_lifecycle.acquire_flock(one)
                    with self.assertRaisesRegex(TimeoutError, "second.lock"):
                        worker_lifecycle.acquire_flock(two, deadline=started + 100)
            self.assertEqual(contended, ["first.lock", "second.lock"])
            self.assertEqual(self.lifecycle_clock.monotonic() - started, 6)

    def test_nested_deadlines_restore_after_failure_without_extending_the_operation(self) -> None:
            path = self.session_dir / "scope.lock"
            self.lifecycle_clock.freeze()
            started = self.lifecycle_clock.monotonic()
            with (patch.object(worker_lifecycle, "time", self.lifecycle_clock),
                  worker_lifecycle.operation_deadline(timeout=10) as outer):
                with self.assertRaisesRegex(ValueError, "nested failure"):
                    with worker_lifecycle.operation_deadline(timeout=1) as inner:
                        self.assertEqual(inner, started + 1)
                        raise ValueError("nested failure")
                self.lifecycle_clock.advance(2)
                with worker_lifecycle.operation_deadline(deadline=started + 100) as restored:
                    self.assertEqual(restored, outer)
                    with path.open("a+") as handle:
                        worker_lifecycle.acquire_flock(handle)
                with worker_lifecycle.operation_deadline(timeout=3) as shortened:
                    self.assertEqual(shortened, started + 5)
            # A new operation has its own deadline.
            with worker_lifecycle.operation_deadline(timeout=20) as fresh:
                self.assertGreater(fresh, outer)

    def test_concurrent_shutdown_deadlines_do_not_leak_between_threads(self) -> None:
            self.lifecycle_clock.freeze()
            started = self.lifecycle_clock.monotonic()
            ready = threading.Barrier(3)
            release = threading.Event()
            guard = LauncherHangGuard("concurrent shutdown deadline scopes")

            def operation(timeout: float) -> float:
                with worker_lifecycle.operation_deadline(timeout=timeout) as deadline:
                    _ = ready.wait(timeout=TEST_HANG_GUARD_SECS)
                    guard.event(release)
                    with worker_lifecycle.operation_deadline() as inherited:
                        self.assertEqual(inherited, deadline)
                    path = self.session_dir / f"operation-{timeout}.lock"
                    with path.open("a+") as handle:
                        worker_lifecycle.acquire_flock(handle)
                    return deadline

            with patch.object(worker_lifecycle, "time", self.lifecycle_clock), ThreadPoolExecutor(max_workers=2) as executor:
                short = executor.submit(operation, 1)
                long = executor.submit(operation, 10)
                try:
                    _ = ready.wait(timeout=TEST_HANG_GUARD_SECS)
                    self.lifecycle_clock.advance(2)
                finally:
                    release.set()
                with self.assertRaises(TimeoutError):
                    _ = guard.result(short)
                self.assertEqual(guard.result(long), started + 10)
