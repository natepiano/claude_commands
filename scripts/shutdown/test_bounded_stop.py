"""Bounded machine shutdown with disposable records and fake process identities."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from test_support import ShutdownTestCase, linux_process_fixture, empty_session_registry
from test_stop import NOW_UTC, entry, machine_inventory, top_level, unit, showrunner, worker_session
from scripts.lib import process_start as process_identity
from scripts.lib.process_start_test_support import stored_start_probe
from account import Account
import inventory
import herdr
import launch_permission
import shutdown_record
import settle
import shutdown
import stop
import relaunch
import sessions


_REAL_FRESH_INVENTORY = stop._fresh_inventory  # pyright: ignore[reportPrivateUsage]


class FlushedOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    @override
    def flush(self) -> None:
        self.flushed.append(self.getvalue())
        super().flush()


class BoundedShutdownTests(ShutdownTestCase):
    root: Path = Path()
    elapsed: float = 0
    processes: dict[int, inventory.ShutdownProcess] = {}
    signals: list[tuple[float, int, int]] = []
    stubborn: set[int] = set()
    natural_exits: dict[int, float] = {}
    output: FlushedOutput = FlushedOutput()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "state"), "USER_ZONE": "America/New_York", "BUILD_HOLD_DIR": str(self.root / "holds"), "BUILDLOG_DIR": str(self.root / "buildlog"), "VERIFY_STEPS_DIR": str(self.root / "steps"), "MAC_TEST_STATE_DIR": str(self.root / "mac")})))
        empty_session_registry(self, self.root / "registry")
        self.elapsed = 0
        self.processes = {}
        self.signals = []
        self.stubborn = set()
        self.natural_exits = {}
        self.output = FlushedOutput()
        # These tests supply their own process table; session admission sees no
        # replacement unless a case supplies one explicitly.
        _ = self.enterContext(patch.object(stop, "_fresh_inventory", return_value=machine_inventory([])))
        _ = self.enterContext(patch.object(inventory, "shutdown_server_identities", return_value=frozenset(), create=True))
        # Substitute only collection: parser/reconciliation and stop rules stay real.
        def collect(arguments: list[str], timeout: float) -> tuple[str, int]:
            shown = subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=timeout)
            return shown.stdout, -1
        _ = self.enterContext(patch.object(inventory, "_collect_process_table", collect, create=True))

    def process(self, pid: int, parent: int = 1, *, started: str = "", arguments: tuple[str, ...] = (),
                session_id: str = "", run_dir: str = "", build_unit: str = "") -> None:
        self.processes[pid] = inventory.ShutdownProcess(pid, parent, started or f"start-{pid}", arguments,
                                                       session_id, run_dir, build_unit)

    def identity(self, pid: int) -> str | None:
        if self.elapsed >= self.natural_exits.get(pid, float("inf")):
            _ = self.processes.pop(pid, None)
        process = self.processes.get(pid)
        return process.started if process is not None else None

    def probe(self, pid: int, _budget: float) -> inventory.ProcessIdentity:
        started = self.identity(pid)
        return inventory.ProcessLive(started) if started is not None else inventory.ProcessGone()

    def observe(self, _budget: float) -> inventory.ProcessObservation:
        for pid in list(self.processes):
            _ = self.identity(pid)
        return inventory.ProcessObservation(self.processes.copy())

    def advance(self, seconds: float) -> None:
        self.elapsed = round(self.elapsed + seconds, 6)
        self.assertLessEqual(self.elapsed, 66, "bounded stop exceeded its deadline and kill grace")

    def kill(self, pid: int, signum: int) -> None:
        self.signals.append((self.elapsed, pid, signum))
        if signum == signal.SIGKILL or pid not in self.stubborn:
            _ = self.processes.pop(pid, None)

    def save(self, session: inventory.Session) -> shutdown_record.SessionShutdown:
        self.process(session["pid"], started=session["proc_start"])
        saved = entry(session, "waiting")
        saved["stop_deadline"] = (NOW_UTC + timedelta(seconds=60)).isoformat()
        self.assertTrue(shutdown_record.create_session(saved))
        return saved

    def pause_workers(self, arguments: list[str]) -> int:
        self.assertEqual(arguments[:3], ["stop", "--pause", "--session-dir"])
        for pid in [401, 402]:
            _ = self.processes.pop(pid, None)
        return 0

    def run_stop(self, records: list[shutdown_record.SessionShutdown], grace: float = 60, *,
                 request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
        with redirect_stdout(self.output):
            return stop.bounded_stop(records, started=0, grace_seconds=grace, request_exit=request_exit,
                monotonic=lambda: self.elapsed, sleep=self.advance,
                clock=lambda: NOW_UTC + timedelta(seconds=self.elapsed),
                observe=self.observe, identity=self.probe,
                kill=self.kill, codex_mesh=lambda arguments, _budget: self.pause_workers(arguments))

    def linux_table(self, groups: dict[int, str], unread: set[int]) -> inventory.ProcessObservation:
        proc = self.root / "linux-proc"
        proc.mkdir(exist_ok=True)
        for pid, process in self.processes.items():
            directory = proc / str(pid)
            directory.mkdir(exist_ok=True)
            fields = ["S", str(process.parent), "0", str(process.process_session), *(["0"] * 15), process.started]
            _ = (directory / "stat").write_text(f"{pid} (process) " + " ".join(fields))
            _ = (directory / "cmdline").write_bytes(b"\0".join(
                word.encode() for word in process.arguments or ("bash", "script.sh")) + b"\0")
            _ = (directory / "cgroup").write_text("0::" + groups[pid] + "\n")
        def environment(directory: Path) -> dict[str, str]:
            pid = int(directory.name)
            if pid in unread:
                raise PermissionError("non-dumpable environment")
            marker = self.processes[pid].session_id
            return {"CLAUDE_CODE_SESSION_ID": marker} if marker else {}
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(inventory, "_snapshot_environment", environment)):
            return inventory.shutdown_processes()

    def test_down_all_reaps_a_detached_build_with_a_saved_worker_marker(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        worker = worker_session("worker", 201, owner="owner")
        worker["proc_start"] = "105"
        self.process(101, started="100")
        self.process(201, started="105")
        self.process(301, 900, started="110", session_id="worker", arguments=("bash", "verify.sh"))
        scope = "/user/app.slice/app-agents.slice/run-owner.scope"
        self.processes = self.linux_table({101: scope, 201: scope, 301: scope}, set()).processes.copy()
        self.natural_exits.update({101: 3, 201: 3})
        report = machine_inventory([director, worker])
        original = stop.bounded_stop
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float,
                    machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                    request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            del started
            self.assertEqual(records[0]["workers"], [worker])
            return original(records, started=0, machine_wide=machine_wide, keep=keep, request_exit=request_exit,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=self.observe, identity=self.probe, kill=self.kill)
        with (patch.object(inventory, "all_live_accounts", return_value=[report]),
              patch.object(settle, "_send_settle_message"), patch.object(stop, "bounded_stop", side_effect=bounded),
              patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), redirect_stdout(self.output)):
            result = settle.down_all()
        self.assertEqual(result, 0)
        self.assertEqual(self.processes, {})
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})
        self.assertIn("all 1 stopped", self.output.getvalue())

    def test_saved_worker_marker_owns_a_build_without_scope_evidence(self) -> None:
        saved = self.save(top_level("owner", 101))
        worker = worker_session("worker", 201, owner="owner")
        def workers(current: shutdown_record.SessionShutdown) -> None:
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", workers)
        self.process(201, started=worker["proc_start"])
        self.process(301, 900, session_id="worker", arguments=("bash", "verify.sh"))
        self.natural_exits.update({101: 3, 201: 3})
        result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.processes, {})
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})

    def test_retained_owner_scope_owns_a_build_with_a_foreign_marker(self) -> None:
        for survives in (False, True):
            with self.subTest(survives=survives):
                self.setUp()
                director = top_level("owner", 101)
                director["proc_start"] = "100"
                saved = self.save(director)
                self.process(301, 900, started="110", session_id="other")
                scope = "/user/app.slice/app-agents.slice/run-owner.scope"
                self.processes = self.linux_table({101: scope, 301: scope}, set()).processes.copy()
                self.natural_exits[101] = 3
                def kill(pid: int, signum: int) -> None:
                    if survives:
                        self.signals.append((self.elapsed, pid, signum))
                    else:
                        self.kill(pid, signum)
                with redirect_stdout(self.output):
                    result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                        sleep=self.advance, clock=lambda: NOW_UTC, observe=self.observe,
                        identity=self.probe, kill=kill)
                self.assertEqual(result[0]["progress"]["kind"], "stop failed" if survives else "stopped")
                self.assertEqual({pid for _, pid, _ in self.signals}, {301})
                self.assertEqual(301 in self.processes, survives)

    def test_uncertain_saved_worker_marker_keeps_its_observation_error(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "Thu Oct 29 19:00:00 2026"
        saved = self.save(director)
        def workers(current: shutdown_record.SessionShutdown) -> None:
            worker = worker_session("worker", 201, owner="owner")
            worker["proc_start"] = "Thu Oct 29 19:00:01 2026"
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", workers)
        self.processes = {}
        shown = self.darwin_tables("301 900 301 S Thu Oct 29 18:00:00 2026 build\n",
            "301 900 301 S Thu Oct 29 18:00:00 2026 changed CLAUDE_CODE_SESSION_ID=worker\n")
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def requester_pause_preserves_its_pane(self, *, machine_wide: bool) -> None:
        for marker in ("requester", "owner"):
            with self.subTest(marker=marker):
                self.setUp()
                run = str(self.root / "owner-run")
                director = top_level("owner", 101, run_dirs=[run])
                saved = self.save(director)
                owner_scope = "/user/app.slice/app-agents.slice/run-owner.scope"
                requester_scope = "/user/app.slice/app-agents.slice/run-requester.scope"
                rows = [(101, 100, owner_scope, ("claude",), ""),
                        (200, 6323, requester_scope, ("zsh",), ""),
                        (201, 200, requester_scope, ("claude",), ""),
                        (202, 201, requester_scope, ("zsh", "-c", "tool"), "requester"),
                        (260, 202, requester_scope, ("python3", "shutdown.py"), marker)]
                for pid, parent, scope, arguments, session_id in rows:
                    self.processes[pid] = inventory.ShutdownProcess(pid, parent, f"start-{pid}",
                        arguments, session_id, cgroups=frozenset({scope}))
                self.processes[101] = inventory.ShutdownProcess(101, 100, director["proc_start"],
                    ("claude",), cgroups=frozenset({owner_scope}))
                self.natural_exits[101] = 3
                requester = top_level("requester", 201)
                requester["proc_start"] = "start-201"
                captures: list[bool] = []
                def observe(budget: float) -> inventory.ProcessObservation:
                    shown = self.observe(budget)
                    captures.append(250 in shown.processes)
                    return shown
                def pause(arguments: list[str], _budget: float) -> int:
                    self.assertEqual(arguments, ["stop", "--pause", "--session-dir", run])
                    self.processes[250] = inventory.ShutdownProcess(250, 260, "start-250",
                        ("python3", "codex_mesh.py", *arguments), "requester",
                        cgroups=frozenset({requester_scope}))
                    return 0
                with (patch.object(os, "getpid", return_value=260), redirect_stdout(self.output)):
                    result = stop.bounded_stop([saved], started=0, machine_wide=machine_wide,
                        keep=(requester,) if machine_wide else (), monotonic=lambda: self.elapsed,
                        sleep=self.advance, clock=lambda: NOW_UTC, observe=observe,
                        identity=self.probe, kill=self.kill, codex_mesh=pause)
                self.assertEqual(result[0]["progress"]["kind"], "stopped")
                self.assertTrue(any(captures), "the pause subprocess must be observed")
                self.assertEqual(self.signals, [])
                self.assertEqual(set(self.processes), {200, 201, 202, 250, 260})

    def test_down_all_pause_command_never_claims_the_requester_pane(self) -> None:
        self.requester_pause_preserves_its_pane(machine_wide=True)

    def test_single_stop_pause_command_never_claims_the_requester_pane(self) -> None:
        self.requester_pause_preserves_its_pane(machine_wide=False)

    def test_initial_scopes_are_captured_before_exit_requests(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        def workers(current: shutdown_record.SessionShutdown) -> None:
            worker = worker_session("worker", 201, owner="owner")
            worker["proc_start"] = "105"
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", workers)
        self.process(201, started="105")
        self.process(301, 900, started="110", arguments=("zsh",))
        shown = self.linux_table({101: "/user/app.slice/app-agents.slice/run-director.scope",
            201: "/user/app.slice/app-agents.slice/run-worker.scope",
            301: "/user/app.slice/app-agents.slice/run-idle.scope"}, set())
        self.processes = shown.processes.copy()
        captures: list[set[int]] = []
        def observe(budget: float) -> inventory.ProcessObservation:
            captures.append(set(self.processes))
            return self.observe(budget)
        def exit_requested(_saved: shutdown_record.SessionShutdown) -> None:
            self.assertEqual(captures, [{101, 201, 301}])
            _ = self.processes.pop(101)
            _ = self.processes.pop(201)
        with redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, request_exit=exit_requested,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=observe, identity=self.probe, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertEqual(self.signals, [])
        self.assertEqual(set(self.processes), {301})

    def test_each_recorded_identity_needs_its_own_initial_scope(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        def workers(current: shutdown_record.SessionShutdown) -> None:
            worker = worker_session("worker", 201, owner="owner")
            worker["proc_start"] = "105"
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", workers)
        self.process(201, started="105")
        self.process(301, 900, started="110")
        table = self.linux_table({101: "/user/app.slice/app-agents.slice/run-director.scope",
            201: "/user/app.slice/app-agents.slice/run-worker.scope",
            301: "/user/app.slice/app-agents.slice/run-director.scope"}, set())
        self.processes = {pid: shown for pid, shown in table.processes.items() if pid != 101}
        result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertNotIn(301, {pid for _, pid, _ in self.signals})
        self.assertIn(301, self.processes)

    def test_complete_root_scope_coverage_does_not_exclude_marker_free_other_scopes(self) -> None:
        for missing in (False, True):
            for machine_wide in (False, True):
                with self.subTest(missing=missing, machine_wide=machine_wide):
                    self.setUp()
                    director = top_level("owner", 101)
                    director["proc_start"] = "100"
                    saved = self.save(director)
                    def workers(current: shutdown_record.SessionShutdown) -> None:
                        worker = worker_session("worker", 201, owner="owner")
                        worker["proc_start"] = "105"
                        current["workers"] = [worker]
                    saved = shutdown_record.update_session(saved["login"], "owner", workers)
                    self.process(201, started="105")
                    self.process(301, 900, started="110", arguments=("zsh",))
                    self.process(302, 900, started="111", arguments=("wl-copy",))
                    table = self.linux_table({101: "/user/app.slice/app-agents.slice/run-director.scope",
                        201: "/user/app.slice/app-agents.slice/run-worker.scope",
                        301: "/user/app.slice/app-agents.slice/run-idle.scope",
                        302: "/user/app.slice/app-agents.slice/run-idle.scope"}, set())
                    self.processes = table.processes.copy()
                    if missing:
                        _ = self.processes.pop(101)
                    else:
                        self.natural_exits[101] = 3
                    self.natural_exits[201] = 3
                    with redirect_stdout(self.output):
                        result = stop.bounded_stop([saved], started=0, machine_wide=machine_wide,
                            monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                            observe=self.observe, identity=self.probe, kill=self.kill)
                    self.assertEqual(result[0]["progress"]["kind"], "stop failed")
                    self.assertTrue({pid for _, pid, _ in self.signals} <= ({201} if missing else set()))
                    self.assertTrue({301, 302}.issubset(self.processes))
                    self.assertEqual(self.elapsed, 66)

    def test_another_live_recorded_director_excludes_its_marker_free_scope(self) -> None:
        for registry in (False, True):
            with self.subTest(registry=registry):
                self.setUp()
                director = top_level("owner", 101)
                director["proc_start"] = "100"
                saved = self.save(director)
                other = top_level("other", 401)
                other["proc_start"] = "90"
                if not registry:
                    _ = self.save(other)
                self.process(401, started="90")
                self.process(402, 900, started="110", arguments=("zsh",))
                table = self.linux_table({101: "/user/app.slice/app-agents.slice/run-owner.scope",
                    401: "/user/app.slice/app-agents.slice/run-other.scope",
                    402: "/user/app.slice/app-agents.slice/run-other.scope"}, set())
                self.processes = {pid: shown for pid, shown in table.processes.items() if pid != 101}
                registry_entry: sessions.SessionRecord = {"pid": 401, "sessionId": "other", "procStart": "90",
                    "name": "other", "messagingSocketPath": "", "updatedAt": 0, "tmux": "", "formerNames": []}
                with patch.object(sessions, "read_registry", return_value=sessions.RegistryRead(
                        (sessions.RegistryObservation(registry_entry, {}),) if registry else (), ())):
                    result = self.evidence_stop(saved)
                self.assertEqual(result[0]["progress"]["kind"], "stopped")
                self.assertEqual(self.signals, [])
                self.assertLess(self.elapsed, 2)

    def test_other_readable_session_marker_excludes_an_agents_process(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(401, 900, started="110", session_id="other")
        table = self.linux_table({101: "/user/app.slice/app-agents.slice/run-owner.scope",
            401: "/user/app.slice/app-agents.slice/run-other.scope"}, set())
        self.processes = {401: table.processes[401]}
        result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertLess(self.elapsed, 2)

    def test_later_replacement_zombie_retires_earlier_fallback_diagnostics(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.processes = {}
        shown = self.darwin_tables(
            "400 1 400 S Thu Oct 29 20:00:00 2026 (helper)\n",
            "400 1 400 Z Thu Oct 29 20:00:01 2026 (helper)\n")
        self.assertEqual(shown.errors, ())
        self.assertEqual(shown.terminated, frozenset({(400, "Thu Oct 29 20:00:00 2026"),
            (400, "Thu Oct 29 20:00:01 2026")}))
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertLess(self.elapsed, 2)

    def test_shared_herdr_server_identity_is_kept_but_an_extra_occupant_is_unknown(self) -> None:
        for extra in (False, True):
            with self.subTest(extra=extra):
                self.setUp()
                director = top_level("owner", 101)
                director["proc_start"] = "100"
                saved = self.save(director)
                self.process(900, started="10", arguments=("herdr", "server"))
                groups = {101: "/user/app.slice/app-agents.slice/run-owner.scope",
                          900: "/user/app.slice/herdr-agents.service"}
                if extra:
                    self.process(301, 900, started="110")
                    groups[301] = groups[900]
                shown = self.linux_table(groups, set())
                self.processes = shown.processes.copy()
                with (patch.object(inventory, "shutdown_server_identities", return_value=frozenset({(900, "10")}), create=True),
                      redirect_stdout(self.output)):
                    result = stop.bounded_stop([saved], started=0, grace_seconds=0, machine_wide=True,
                        monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                        observe=self.observe, identity=self.probe, kill=self.kill)
                self.assertEqual({pid for _, pid, _ in self.signals}, {101})
                self.assertEqual(result[0]["progress"]["kind"], "stop failed" if extra else "stopped")
                self.assertIn(900, self.processes)
                if extra:
                    self.assertIn("301", str(result[0]["progress"]))

    def test_readable_shared_service_occupant_is_unknown_without_signal_authority(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.process(301, 900, started="110")
        shown = self.linux_table({101: "/user/app.slice/app-agents.slice/run-owner.scope",
            301: "/user/app.slice/herdr-agents.service"}, set())
        self.processes = {301: shown.processes[301]}
        result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_readable_marker_free_build_in_retained_owner_scope_is_reaped(self) -> None:
        for survives in (False, True):
            with self.subTest(survives=survives):
                self.setUp()
                director = top_level("owner", 101)
                director["proc_start"] = "100"
                saved = self.save(director)
                shown = self.linux_table({101: "/user/app.slice/app-agents.slice/run-owner.scope"}, set())
                self.processes = shown.processes.copy()
                def observe(budget: float) -> inventory.ProcessObservation:
                    if self.elapsed >= 3:
                        self.process(301, 900, started="110")
                        self.processes[301] = inventory.ShutdownProcess(301, 900, "110",
                            cgroups=frozenset({"/user/app.slice/app-agents.slice/run-owner.scope"}))
                    return self.observe(budget)
                # Spawn once, then retain only its actual lifetime.
                spawned = False
                def capture(budget: float) -> inventory.ProcessObservation:
                    nonlocal spawned
                    if self.elapsed >= 3 and not spawned:
                        spawned = True
                        return observe(budget)
                    return self.observe(budget)
                self.natural_exits[101] = 3
                if survives:
                    self.stubborn.add(301)
                def kill(pid: int, signum: int) -> None:
                    if survives and pid == 301:
                        self.signals.append((self.elapsed, pid, signum))
                    else:
                        self.kill(pid, signum)
                with redirect_stdout(self.output):
                    result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                        sleep=self.advance, clock=lambda: NOW_UTC, observe=capture, identity=self.probe, kill=kill)
                self.assertIn(301, {pid for _, pid, _ in self.signals})
                self.assertEqual(result[0]["progress"]["kind"], "stop failed" if survives else "stopped")

    def test_marker_free_process_without_retained_owner_scope_is_unknown(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(301, 900, started="110")
        shown = self.linux_table({101: "/user/app.slice/app-agents.slice/run-owner.scope",
            301: "/user/app.slice/app-agents.slice/run-owner.scope"}, set())
        self.processes = {301: shown.processes[301]}
        result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_readable_marker_free_attach_client_in_desktop_scope_does_not_block(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(301, 900, started="110")
        shown = self.linux_table({101: "/user/app.slice/app-agents.slice/run-owner.scope",
            301: "/user/app.slice/app-ghostty-surface-transient-123.scope"}, set())
        self.processes = shown.processes.copy()
        self.natural_exits[101] = 3
        result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertIn(301, self.processes)
        self.assertEqual(self.signals, [])

    def test_missing_recorded_worker_parent_blocks_completion_through_real_darwin_parser(self) -> None:
        saved = self.save(top_level("owner", 101))
        def worker(current: shutdown_record.SessionShutdown) -> None:
            worker = worker_session("worker", 201, owner="owner")
            worker["proc_start"] = "Thu Oct 29 19:00:00 2026"
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", worker)
        self.processes = {}
        row = "301 201 301 S Thu Oct 29 20:00:00 2026 bash build.sh\n"
        shown = self.darwin_tables(row, row)
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_darwin_ambiguous_environment_markers_never_grant_or_erase_ownership(self) -> None:
        for tail in ('CLAUDE_CODE_SESSION_ID=owner NOTE="hello CLAUDE_CODE_SESSION_ID=other"',
                     'NOTE="hello CLAUDE_CODE_SESSION_ID=owner"',
                     'unparsed value CLAUDE_CODE_SESSION_ID=owner'):
            with self.subTest(tail=tail):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                self.processes = {}
                row = "301 1 301 S Thu Oct 29 20:00:00 2026 bash build.sh"
                shown = self.darwin_tables(row + "\n", row + " " + tail + "\n")
                with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
                    result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                        monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                        observe=lambda _budget: shown, kill=self.kill)
                self.assertEqual(result[0]["progress"]["kind"], "stop failed")
                self.assertIn("301", str(result[0]["progress"]))
                self.assertEqual(self.signals, [])

    def test_confirmed_zombie_retires_its_fallback_diagnostics(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.processes = {}
        row = "400 1 400 Z Thu Oct 29 20:00:00 2026 (helper)\n"
        shown = self.darwin_tables(row, row)
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(shown.errors, ())
        self.assertLess(self.elapsed, 1)

    def test_linux_readable_external_service_scope_allows_exit_and_claim_release_regardless_of_age(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(301, 900, started="110")
        self.process(900, started="10", arguments=("systemd", "--user"))
        shown = self.linux_table({101: "/user/app-agents.slice/run-owner.scope",
            301: "/user/background.slice/helper.service", 900: "/init.scope"}, {301})
        self.processes = shown.processes.copy()
        self.natural_exits[101] = 3
        hold = self.root / "holds" / "owner"
        hold.parent.mkdir()
        _ = hold.write_text(json.dumps({"holder_session": "owner"}))
        result = self.evidence_stop(saved, errors=shown.errors)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertFalse(hold.exists())
        self.assertLess(self.elapsed, 4)
        self.assertEqual(self.signals, [])
        self.assertIn(301, self.processes)

    def test_linux_unread_environment_in_owner_scope_is_reaped_despite_unrelated_adopter(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(301, 900, started="110")
        shown = self.linux_table({101: "/user/app-agents.slice/run-owner.scope",
            301: "/user/app-agents.slice/run-owner.scope"}, {301})
        self.processes = shown.processes.copy()
        self.natural_exits[101] = 3
        def observe(budget: float) -> inventory.ProcessObservation:
            table = self.observe(budget)
            return inventory.ProcessObservation(table.processes,
                shown.errors if 301 in table.processes else ())
        # The table retains readable cgroups while environment access fails.
        with redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=observe, identity=self.probe, kill=self.kill)
        self.assertEqual(self.processes, {})
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})

    def test_linux_unrecorded_transient_scope_with_unread_environment_is_unknown(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(301, 900, started="110")
        shown = self.linux_table({101: "/user/app-agents.slice/run-owner.scope",
            301: "/user/app.slice/run-detached.scope"}, {301})
        self.processes = shown.processes.copy()
        self.natural_exits[101] = 3
        result = self.evidence_stop(saved, errors=shown.errors)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertNotIn(301, {pid for _, pid, _ in self.signals})

    def test_darwin_arbitrary_subreaper_with_unread_environment_never_proves_unrelated(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(900, started="10", arguments=("tini",))
        self.process(301, 900, started="110")
        self.natural_exits[101] = 3
        with patch.object(sys, "platform", "darwin"):
            result = self.evidence_stop(saved, errors=("pid 301: environment unread",))
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertNotIn(301, {pid for _, pid, _ in self.signals})

    def test_darwin_comm_fallback_is_unknown_through_the_real_parser(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        observation = self.darwin_tables(
            "301 900 301 S Thu Oct 29 20:00:00 2026 (build)\n",
            "301 900 301 S Thu Oct 29 20:00:00 2026 (build)\n")
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_darwin_collector_parse_error_is_retired_with_its_exact_identity(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        shown = self.darwin_tables("901 1 901 R Thu Oct 29 20:00:00 2026 ps\n",
            "902 1 902 R Thu Oct 29 20:00:00 2026 ps NOTE=don't\n", collectors=(901, 902))
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(shown.errors, ())
        self.assertEqual(self.signals, [])

    def test_zombie_replacement_parent_does_not_authorize_its_child(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "Thu Oct 29 19:00:00 2026"
        saved = self.save(director)
        shown = self.darwin_tables(
            "101 1 101 S Thu Oct 29 20:00:00 2026 replacement\n301 101 301 S Thu Oct 29 20:00:01 2026 child\n",
            "101 1 101 Z Thu Oct 29 20:00:00 2026 replacement\n301 1 301 S Thu Oct 29 20:00:01 2026 child\n")
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, grace_seconds=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(self.signals, [])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_child_first_seen_after_parent_disappeared_is_unknown_without_signal_authority(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        self.process(301, 101)
        result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_proven_child_remains_owned_after_parent_exit_and_replacement_zombie(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.process(301, 101)
        self.natural_exits[101] = 3
        def observe(budget: float) -> inventory.ProcessObservation:
            shown = self.observe(budget)
            if 101 not in shown.processes:
                return inventory.ProcessObservation(shown.processes, terminated=frozenset({(101, "replacement")}))
            return shown
        with redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=observe, identity=self.probe, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})

    def test_inflight_legacy_claim_yields_to_bounded_adoption_before_any_completion(self) -> None:
        session = top_level("owner", 101)
        saved = entry(session, "ready")
        self.assertTrue(shutdown_record.create_session(saved))
        self.process(101, started=session["proc_start"])
        self.process(777, session_id="owner", arguments=("verify.sh",))
        def adopt(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            def bounded(current: shutdown_record.SessionShutdown) -> None:
                current["stop_deadline"] = (NOW_UTC + timedelta(seconds=60)).isoformat()
            _ = shutdown_record.update_session(saved["login"], "owner", bounded)
            return machine_inventory([session])
        result = stop.stop_session(saved, forced=True, fresh_inventory=adopt, clock=lambda: NOW_UTC,
            kill=self.kill, is_alive=lambda pid: pid in self.processes, session_record_exists=lambda _pid: False,
            sleep=self.advance, codex_mesh=lambda _arguments: 0)
        self.assertNotEqual(result["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        finished = self.run_stop([result], grace=0)
        self.assertEqual(finished[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.processes, {})

    def test_darwin_reconciled_quotes_do_not_hide_readable_unrelated_evidence(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        shown = self.darwin_tables(
            "400 900 400 S Thu Oct 29 20:00:00 2026 command don't\n",
            "400 900 400 S Thu Oct 29 20:00:00 2026 command don't NOTE=it's\n")
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertEqual(shown.errors, ())

    def test_unmatched_darwin_unrelated_prefix_change_does_not_block_other_owners(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        shown = self.darwin_tables("400 900 400 S Thu Oct 29 20:00:00 2026 original\n",
            "400 900 400 S Thu Oct 29 20:00:00 2026 changed NOTE=it's\n")
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: shown, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])

    def test_down_all_reaps_shared_herdr_scope_while_a_single_stop_retains_uncertainty(self) -> None:
        for machine_wide in (False, True):
            with self.subTest(machine_wide=machine_wide):
                self.setUp()
                director = top_level("owner", 101)
                director["proc_start"] = "100"
                _ = self.save(director)
                self.process(301, 900, started="110")
                shown = self.linux_table({101: "/user/app.slice/herdr-agents.service",
                    301: "/user/app.slice/herdr-agents.service"}, {301})
                self.processes = shown.processes.copy()
                self.natural_exits[101] = 3
                report = machine_inventory([director])
                original = stop.bounded_stop
                def observe(budget: float) -> inventory.ProcessObservation:
                    table = self.observe(budget)
                    return inventory.ProcessObservation(table.processes,
                        shown.errors if 301 in table.processes else ())
                def bounded(records: list[shutdown_record.SessionShutdown], *, started: float,
                            machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
                    del started
                    if machine_wide:
                        return original(records, started=0, machine_wide=True, keep=keep, request_exit=request_exit, monotonic=lambda: self.elapsed,
                            sleep=self.advance, clock=lambda: NOW_UTC, observe=observe, identity=self.probe, kill=self.kill)
                    return original(records, started=0, monotonic=lambda: self.elapsed,
                        sleep=self.advance, clock=lambda: NOW_UTC, observe=observe, identity=self.probe, kill=self.kill)
                if machine_wide:
                    with (patch.object(inventory, "all_live_accounts", return_value=[report]),
                          patch.object(settle, "_send_settle_message"), patch.object(stop, "bounded_stop", side_effect=bounded),
                          patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), redirect_stdout(self.output)):
                        result = settle.down_all()
                    self.assertEqual(result, 1)
                    self.assertEqual({pid for _, pid, _ in self.signals}, set())
                    self.assertEqual(set(self.processes), {301})
                else:
                    saved = shutdown_record.find_session(report["login"], "owner")
                    assert saved is not None
                    stopped = bounded([saved], started=0)
                    self.assertEqual(stopped[0]["progress"]["kind"], "stop failed")
                    self.assertIn(301, self.processes)
                    self.assertEqual(self.signals, [])

    def test_down_all_shared_scope_preserves_exact_requester_and_infrastructure_identities(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        requester = top_level("requester", 201)
        requester["proc_start"] = "101"
        protected = top_level("postmaster", 401)
        protected["proc_start"] = "102"
        for session in (director, requester, protected):
            self.process(session["pid"], started=session["proc_start"])
        self.process(301, 900, started="110", session_id="owner")
        shown = self.linux_table({pid: "/user/app.slice/herdr-agents.service"
            for pid in self.processes}, {201, 401})
        self.processes = shown.processes.copy()
        self.natural_exits[101] = 3
        report = machine_inventory([director, requester, protected])
        original = stop.bounded_stop
        def observe(budget: float) -> inventory.ProcessObservation:
            table = self.observe(budget)
            return inventory.ProcessObservation(table.processes, tuple(error for error in shown.errors
                if any(error.startswith(f"pid {pid}:") for pid in table.processes)))
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float,
                    machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            del started
            return original(records, started=0, machine_wide=machine_wide, keep=keep, request_exit=request_exit,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=observe, identity=self.probe, kill=self.kill)
        with (patch.object(inventory, "all_live_accounts", return_value=[report]),
              patch.object(settle, "_send_settle_message"), patch.object(stop, "bounded_stop", side_effect=bounded),
              patch.object(stop, "defer_requester") as deferred,
              patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "requester"}), redirect_stdout(self.output)):
            result = settle.down_all()
        self.assertEqual(result, 0)
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})
        self.assertEqual(set(self.processes), {201, 401})
        deferred.assert_called_once()
        self.assertEqual(deferred.call_args.args[0]["session"]["session_id"], "requester")
        self.assertIsNone(shutdown_record.find_session(report["login"], "postmaster"))

    def test_positive_marker_wins_over_readable_external_service_membership(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(301, 900, started="110", session_id="owner")
        shown = self.linux_table({101: "/user/app-agents.slice/run-owner.scope",
            301: "/user/background.slice/worker.service"}, set())
        self.processes = shown.processes.copy()
        self.natural_exits[101] = 3
        result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})
        self.assertEqual(self.processes, {})

    def test_darwin_readable_owner_start_excludes_an_older_candidate_despite_unparseable_worker_start(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        worker = worker_session("worker", 201, owner="owner")
        worker["proc_start"] = "unreadable-start"
        def seats(current: shutdown_record.SessionShutdown) -> None:
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", seats)
        self.natural_exits[101] = 3
        self.process(777, started="10", arguments=("tini",))
        with patch.object(sys, "platform", "darwin"):
            result = self.evidence_stop(saved, errors=("pid 777: environment unread",))
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertLess(self.elapsed, 4)
        self.assertEqual(self.signals, [])
        self.assertIn(777, self.processes)

    def test_unreadable_environment_of_a_new_reparented_process_remains_unknown(self) -> None:
        for parent in (1, 900):
            with self.subTest(parent=parent):
                self.setUp()
                director = top_level("owner", 101)
                director["proc_start"] = "100"
                saved = self.save(director)
                self.natural_exits[101] = 3
                self.process(900, started="10", arguments=("systemd", "--user"))
                self.process(301, parent, started="110", arguments=("bash", "script.sh"))
                self.processes[301] = inventory.ShutdownProcess(301, parent, "110", ("bash", "script.sh"), process_session=301)
                result = self.evidence_stop(saved, errors=("pid 301: environment unread: denied",))
                self.assertEqual(result[0]["progress"]["kind"], "stop failed")
                self.assertIn("301", str(result[0]["progress"]))
                self.assertIn(301, self.processes)
                self.assertNotIn(301, {pid for _, pid, _ in self.signals})

    def test_uncertain_darwin_eww_with_a_quote_retains_raw_owner_markers(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        companion = "302 500 301 S Thu Oct 29 20:00:00 2026 bash companion.sh\n"
        shown = self.darwin_tables(companion, companion + "301 1 301 S Thu Oct 29 20:00:00 2026 bash build.sh NOTE=don't CLAUDE_CODE_SESSION_ID=owner\n")
        observation = inventory.ProcessObservation(shown.processes,
            shown.errors + ("pid 302: environment unread: denied",), shown.uncertain, shown.terminated)
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertIn("302", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_unparsed_darwin_ownership_cannot_be_classified_as_unrelated(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        observation = self.darwin_tables("", "301 1 301 S Thu Oct 29 20:00:00 2026 bash script.sh don't CLAUDE_CODE_SESSION_ID=owner\n")
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_unreadable_cgroup_cannot_exclude_a_detached_owned_scope(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        self.process(301, arguments=("bash", "build.sh"))
        result = self.evidence_stop(saved, errors=("pid 301: cgroup unread: denied",))
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))

    def test_darwin_reparenting_preserves_positive_ancestry_for_the_same_identity(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.process(301, arguments=("bash", "build.sh"))
        def observation(_budget: float) -> inventory.ProcessObservation:
            owner = "101 1 101 S Thu Oct 29 20:00:00 2026 claude\n" if 101 in self.processes else ""
            first = "301 101 301 S Thu Oct 29 20:00:00 2026 bash build.sh\n" if 301 in self.processes else ""
            later = "301 1 301 S Thu Oct 29 20:00:00 2026 bash build.sh\n" if 301 in self.processes else ""
            return self.darwin_tables(owner + first, owner + later)
        def owner_start(current: shutdown_record.SessionShutdown) -> None:
            current["session"]["proc_start"] = "Thu Oct 29 20:00:00 2026"
        saved = shutdown_record.update_session(saved["login"], "owner", owner_start)
        self.processes[101] = inventory.ShutdownProcess(101, 1, saved["session"]["proc_start"])
        self.processes[301] = inventory.ShutdownProcess(301, 1, saved["session"]["proc_start"], ("bash", "build.sh"))
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {101, 301})
        self.assertEqual(self.processes, {})

    def test_reused_collector_pid_is_not_exempt_from_owned_process_evidence(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        observation = self.darwin_tables(
            "901 1 901 R Thu Oct 29 20:00:00 2026 ps\n",
            "901 101 901 S Thu Oct 29 20:00:01 2026 bash build.sh CLAUDE_CODE_SESSION_ID=owner\n",
            collectors=(901, 902))
        with patch.object(sys, "platform", "darwin"), redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("901", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_failed_bounded_attempt_is_terminal_for_scheduler_and_conductor_hold(self) -> None:
        saved = self.save(top_level("owner", 101))
        def failed(current: shutdown_record.SessionShutdown) -> None:
            current["force"] = "now"
            current["progress"] = {"kind": "stop failed", "at": NOW_UTC.isoformat(), "reason": "pid 777 unknown"}
        saved = shutdown_record.update_session(saved["login"], "owner", failed)
        owner = stop.fresh_session(saved["session"], machine_inventory([saved["session"]]))
        self.assertIsInstance(settle.next_step(saved, owner, [saved], NOW_UTC), settle.Finished)
        self.assertTrue(shutdown_record.shutdown_finished(saved))
        shutdown_record.set_account_hold(shutdown_record.AccountHold(login=saved["login"], label=saved["label"], machine=saved["machine"], request_id=saved["request_id"], requested_at=saved["requested_at"], requested_by=saved["requested_by"], then="stop"))
        self.assertIsInstance(shutdown_record.release_finished_account_hold(saved["login"], conducting=True), shutdown_record.HoldReleased)

    def test_legacy_stop_claim_cannot_take_a_record_adopted_by_a_bounded_request(self) -> None:
        saved = self.save(top_level("owner", 101))
        legacy = copy.deepcopy(saved)
        del legacy["stop_deadline"]
        with patch.object(stop, "_stop_owner") as legacy_stop:
            result = stop.stop_session(legacy, kill=self.kill, clock=lambda: NOW_UTC)
        legacy_stop.assert_not_called()
        self.assertEqual(result["progress"]["kind"], "waiting")
        self.assertEqual(self.signals, [])

    def test_now_retries_a_bounded_scope_after_its_director_has_exited(self) -> None:
        saved = self.save(top_level("owner", 101))
        def failed(current: shutdown_record.SessionShutdown) -> None:
            current["force"] = "now"
            current["progress"] = {"kind": "stop failed", "at": NOW_UTC.isoformat(), "reason": "old observation unknown"}
        _ = shutdown_record.update_session(saved["login"], "owner", failed)
        _ = self.processes.pop(101)
        self.process(777, arguments=("bash", "verify.sh"), session_id="owner")
        bounded = stop.bounded_stop
        def retry(records: list[shutdown_record.SessionShutdown], *, started: float, grace_seconds: float) -> list[shutdown_record.SessionShutdown]:
            del started
            return bounded(records, started=0, grace_seconds=grace_seconds,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=self.observe, identity=self.probe, kill=self.kill)
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([])),
              patch.object(settle, "unrecorded_local_sessions", return_value={}),
              patch.object(settle, "_launch_conductor") as conductor,
              patch.object(stop, "bounded_stop", side_effect=retry) as retried,
              redirect_stdout(self.output)):
            self.assertEqual(settle.now(Account("claude", saved["login"], saved["label"]), True, ("owner",)), 0)
        retried.assert_called_once()
        conductor.assert_not_called()
        self.assertEqual(self.processes, {})
        current = shutdown_record.find_session(saved["login"], "owner")
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "stopped")

    def test_explicit_bounded_retry_schedules_restart_after_its_director_is_gone(self) -> None:
        saved = self.save(top_level("owner", 101))
        def failed(current: shutdown_record.SessionShutdown) -> None:
            current["then"] = "restart"
            current["progress"] = {"kind": "stop failed", "at": NOW_UTC.isoformat(), "reason": "unknown build"}
        _ = shutdown_record.update_session(saved["login"], "owner", failed)
        def stopped(records: list[shutdown_record.SessionShutdown], *, started: float, grace_seconds: float) -> list[shutdown_record.SessionShutdown]:
            del started, grace_seconds
            def done(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = {"kind": "stopped", "at": NOW_UTC.isoformat()}
            return [shutdown_record.update_session(item["login"], item["session"]["session_id"], done) for item in records]
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([])),
              patch.object(settle, "unrecorded_local_sessions", return_value={}),
              patch.object(stop, "bounded_stop", side_effect=stopped) as bounded,
              patch.object(settle, "_launch_conductor") as conductor,
              redirect_stdout(self.output)):
            self.assertEqual(settle.now(Account("claude", saved["login"], saved["label"]), True, ("owner",)), 0)
        bounded.assert_called_once()
        conductor.assert_called_once_with(saved["login"], saved["label"])

    def test_successful_bounded_restart_request_keeps_its_restart_continuation(self) -> None:
        saved = self.save(top_level("owner", 101))
        saved["then"] = "restart"
        saved["progress"] = {"kind": "stopped", "at": NOW_UTC.isoformat()}
        owner = stop.fresh_session(saved["session"], machine_inventory([]))
        self.assertIsInstance(settle.next_step(saved, owner, [saved], NOW_UTC), settle.RelaunchSession)
        self.assertFalse(shutdown_record.shutdown_finished(saved))

    def test_rejected_final_write_preserves_cleanup_failure_in_result_and_output(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        def cleanup(*_args: object) -> None:
            def adopt(current: shutdown_record.SessionShutdown) -> None:
                current["session"]["pid"] = 202
                current["session"]["proc_start"] = "new-start"
            _ = shutdown_record.update_session(saved["login"], "owner", adopt)
            raise OSError("could not remove build hold")
        with patch.object(stop, "_release_build_claims", side_effect=cleanup):
            result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("could not remove build hold", str(result[0]["progress"]))
        self.assertIn("ownership changed", str(result[0]["progress"]))
        self.assertIn("could not remove build hold", self.output.getvalue())
        current = shutdown_record.find_session(saved["login"], "owner")
        assert current is not None
        self.assertEqual(current["session"]["pid"], 202)

    def test_cleanup_adoption_under_the_same_request_never_writes_the_new_directors_outcome(self) -> None:
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                self.natural_exits[101] = 3
                def adopt(*_args: object) -> None:
                    def replace(current: shutdown_record.SessionShutdown) -> None:
                        current["session"]["pid"] = 202
                        current["session"]["proc_start"] = "adopted-start"
                        current["progress"] = {"kind": "waiting"}
                    _ = shutdown_record.update_session(saved["login"], "owner", replace)
                    self.process(202, started="adopted-start")
                    if failed:
                        raise OSError("cleanup failed")
                with patch.object(stop, "_release_build_claims", side_effect=adopt):
                    result = self.run_stop([saved])
                current = shutdown_record.find_session(saved["login"], "owner")
                assert current is not None
                self.assertEqual(current["session"]["pid"], 202)
                self.assertEqual(current["progress"]["kind"], "waiting")
                self.assertEqual(result[0]["progress"]["kind"], "stop failed")
                self.assertIn(202, self.processes)
                self.assertNotIn("all 1 stopped", self.output.getvalue())

    def test_cleanup_ownership_change_never_overwrites_the_adopted_workers(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        def adopt(*_args: object) -> None:
            def replace(current: shutdown_record.SessionShutdown) -> None:
                current["session"]["run_dirs"] = ["/new/director/run"]
            _ = shutdown_record.update_session(saved["login"], "owner", replace)
        with patch.object(stop, "_release_build_claims", side_effect=adopt):
            result = self.run_stop([saved])
        current = shutdown_record.find_session(saved["login"], "owner")
        assert current is not None
        self.assertEqual(current["session"]["run_dirs"], ["/new/director/run"])
        self.assertNotEqual(current["progress"]["kind"], "stopped")
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")

    def test_unrelated_unreadable_environment_allows_natural_exit_and_build_claim_release(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.natural_exits[101] = 3
        self.process(777, started="10", arguments=("systemd", "--user"))
        shown = self.linux_table({101: "/user/app-agents.slice/run-owner.scope",
            777: "/user/background.slice/unrelated.service"}, {777})
        self.processes = shown.processes.copy()
        hold = self.root / "holds" / "owner"
        hold.parent.mkdir()
        _ = hold.write_text(json.dumps({"holder_session": "owner"}))
        result = self.evidence_stop(saved, errors=("pid 777: environment unread: denied",))
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertLess(self.elapsed, 4)
        self.assertFalse(hold.exists())
        self.assertEqual(self.signals, [])
        self.assertIn(777, self.processes)

    def test_owned_child_and_owned_group_read_errors_still_prevent_completion(self) -> None:
        for parent, group in ((101, 777), (1, 101)):
            with self.subTest(parent=parent, group=group):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                self.processes[101] = inventory.ShutdownProcess(101, 1, "start-owner", process_session=101)
                self.processes[777] = inventory.ShutdownProcess(777, parent, "child", process_session=group)
                result = self.evidence_stop(saved, errors=("pid 777: environment unread: denied",))
                self.assertEqual(result[0]["progress"]["kind"], "stop failed")
                self.assertIn("777", str(result[0]["progress"]))

    def test_probe_consuming_the_verification_reserve_does_not_authorize_a_signal(self) -> None:
        saved = self.save(top_level("owner", 101))
        def slow(pid: int, budget: float) -> inventory.ProcessIdentity:
            if self.elapsed >= 60:
                self.advance(budget)
            return self.probe(pid, budget)
        with redirect_stdout(self.output):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=self.observe,
                identity=slow, kill=self.kill)
        self.assertEqual(self.signals, [])
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertLessEqual(self.elapsed, 66)

    def test_requester_helper_unknown_identity_exhausts_its_absolute_budget_without_stopping_the_director(self) -> None:
        saved = self.save(top_level("caller", 101))
        def unknown(_pid: int, timeout: float = 5) -> inventory.ProcessIdentity:
            self.advance(min(timeout, 2))
            return inventory.ProcessUnknown("unreadable command")
        def bounded_sleep(seconds: float) -> None:
            self.advance(seconds)
        # A regression must fail promptly rather than loop forever on the old helper.
        def clock() -> float:
            if self.elapsed > 66:
                raise AssertionError("helper failed to stop at the absolute deadline")
            return self.elapsed
        with (patch.object(stop, "shutdown_identity", side_effect=unknown),
              patch.object(time, "monotonic", side_effect=clock),
              patch.object(time, "sleep", side_effect=bounded_sleep),
              patch.object(stop, "bounded_stop") as bounded,
              redirect_stdout(self.output)):
            self.assertEqual(stop.main(["finish-requester", saved["login"], "caller", "999", "command-start"]), 1)
        bounded.assert_not_called()
        self.assertLessEqual(self.elapsed, 66)

    def darwin_tables(self, first: str, later: str, *, collectors: tuple[int, int] = (-1, -2)) -> inventory.ProcessObservation:
        with (patch.object(sys, "platform", "darwin"),
              patch.object(inventory, "_collect_process_table", side_effect=[(first, collectors[0]), (later, collectors[1])])):
            return inventory.shutdown_processes()

    def test_darwin_replacement_in_the_later_table_never_authorizes_a_signal_to_the_old_identity(self) -> None:
        saved = self.save(top_level("owner", 101))
        saved["session"]["proc_start"] = "Thu Oct 29 20:00:00 2026"
        def start(current: shutdown_record.SessionShutdown) -> None:
            current["session"]["proc_start"] = saved["session"]["proc_start"]
        saved = shutdown_record.update_session(saved["login"], "owner", start)
        observation = self.darwin_tables(
            "101 1 101 S Thu Oct 29 20:00:00 2026 claude\n",
            "101 1 999 S Thu Oct 29 20:00:01 2026 unrelated\n")
        with (patch.object(sys, "platform", "darwin"), redirect_stdout(self.output)):
            result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(self.signals, [])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_darwin_owned_build_visible_only_in_the_later_table_prevents_false_success(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        observation = self.darwin_tables("", "301 1 301 S Thu Oct 29 20:00:00 2026 verify.sh CLAUDE_CODE_SESSION_ID=owner\n")
        with (patch.object(sys, "platform", "darwin"), redirect_stdout(self.output)):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_darwin_collectors_and_unrelated_churn_do_not_block_a_successful_stop(self) -> None:
        saved = self.save(top_level("owner", 101))
        _ = self.processes.pop(101)
        observation = self.darwin_tables(
            "901 101 101 R Thu Oct 29 20:00:00 2026 ps\n777 1 777 S Thu Oct 29 20:00:00 2026 unrelated\n",
            "902 101 101 R Thu Oct 29 20:00:00 2026 ps\n778 1 778 S Thu Oct 29 20:00:00 2026 unrelated-new\n",
            collectors=(901, 902))
        with (patch.object(sys, "platform", "darwin"), redirect_stdout(self.output)):
            result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC, observe=lambda _budget: observation, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertNotIn(901, observation.processes)
        self.assertNotIn(902, observation.processes)

    def test_darwin_later_zombie_and_zombie_to_absent_prove_the_exact_identity_gone(self) -> None:
        for first, later in (("S", "Z"), ("Z", "")):
            with self.subTest(first=first, later=later):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                def start(current: shutdown_record.SessionShutdown) -> None:
                    current["session"]["proc_start"] = "Thu Oct 29 20:00:00 2026"
                saved = shutdown_record.update_session(saved["login"], "owner", start)
                observation = self.darwin_tables(
                    f"101 1 101 {first} Thu Oct 29 20:00:00 2026 claude\n",
                    f"101 1 101 {later} Thu Oct 29 20:00:00 2026 <defunct>\n" if later else "")
                with (patch.object(sys, "platform", "darwin"), redirect_stdout(self.output)):
                    result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                        monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                        observe=lambda _budget: observation, kill=self.kill)
                self.assertEqual(result[0]["progress"]["kind"], "stopped")
                self.assertEqual(self.signals, [])

    def test_darwin_pid_reuse_after_a_zombie_keeps_the_new_identity_distinct(self) -> None:
        observation = self.darwin_tables(
            "101 1 101 Z Thu Oct 29 20:00:00 2026 <defunct>\n",
            "101 1 999 S Thu Oct 29 20:00:01 2026 unrelated\n")
        self.assertEqual(observation.processes[101].started, "Thu Oct 29 20:00:01 2026")
        self.assertIn(101, observation.uncertain)
        self.assertNotIn((101, "Thu Oct 29 20:00:01 2026"), observation.terminated)

    def test_darwin_supervisor_keep_uses_its_already_validated_start_identity(self) -> None:
        supervisor = inventory.SupervisorIdentity(kind="supervisor", pid=902, proc_start="supervisor-start")
        saved = self.save(top_level("owner", 101,
            host=inventory.TerminalHost(kind="terminal", supervisor=supervisor)))
        self.process(902, 101, started="supervisor-start")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(stop, "live_supervisor", return_value=supervisor),
              patch.object(relaunch, "write_hold") as hold):
            result = self.run_stop([saved], grace=0)
        hold.assert_called_once_with(902)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {101})
        self.assertIn(902, self.processes)

    def test_unreadable_build_ownership_record_is_not_evidence_that_no_builds_remain(self) -> None:
        run = self.root / "unreadable-run"
        (run / "mesh_runs").mkdir(parents=True)
        path = run / "mesh_runs" / "build.json"
        _ = path.write_text(json.dumps({"status": "running", "pid": 301}))
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        self.process(301, arguments=("python3", "mesh_runs.py", "supervise", str(path)))
        self.natural_exits[101] = 3
        read = Path.read_text
        def unreadable(current: Path, *args: object, **kwargs: object) -> str:
            del args, kwargs
            if current == path:
                raise PermissionError("build record denied")
            return read(current)
        with patch.object(Path, "read_text", unreadable):
            result = self.evidence_stop(saved)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn(str(path), str(result[0]["progress"]))
        self.assertIn(301, self.processes)
        self.assertLessEqual(self.elapsed, 66)

    def test_requester_helper_does_not_treat_unknown_command_identity_as_exit(self) -> None:
        saved = self.save(top_level("caller", 101))
        with (patch.object(stop, "shutdown_identity", side_effect=[inventory.ProcessUnknown("ps timeout"),
              inventory.ProcessLive("command-start"), inventory.ProcessGone()]),
              patch.object(time, "sleep") as sleep,
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", return_value=[saved]) as bounded):
            self.assertEqual(stop.main(["finish-requester", saved["login"], "caller", "999", "command-start"]), 0)
        self.assertEqual(sleep.call_count, 2)
        bounded.assert_called_once()

    def test_now_keeps_postmaster_successfully_and_preserves_a_pending_restore_timer(self) -> None:
        for restoring in (False, True):
            with self.subTest(restoring=restoring):
                owner = top_level("postmaster", 101)
                saved = self.save(owner)
                if restoring:
                    def begin_restore(current: shutdown_record.SessionShutdown) -> None:
                        current["progress"] = {"kind": "restarting", "at": saved["requested_at"],
                            "prior": {"kind": "stopped", "at": saved["requested_at"]}}
                        current["timers"] = [{"instance": "postmaster", "was_enabled": True}]
                    saved = shutdown_record.update_session(saved["login"], "postmaster", begin_restore)
                with (patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
                      patch.object(settle, "_launch_conductor") as conductor, redirect_stdout(self.output)):
                    self.assertEqual(settle.now(Account("claude", saved["login"], "claude2"), here=True, only=["postmaster"]), 0)
                conductor.assert_not_called()
                current = shutdown_record.find_session(saved["login"], "postmaster")
                if restoring:
                    self.assertEqual(current, saved)
                else:
                    self.assertIsNone(current)
                self.assertEqual(self.signals, [])
                self.assertIn(101, self.processes)

    def test_down_all_leaves_a_kept_directors_restore_claim_and_timer_to_its_confirmer(self) -> None:
        owner = top_level("postmaster", 101)
        saved = self.save(owner)
        def begin_restore(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarting", "at": saved["requested_at"],
                "prior": {"kind": "stopped", "at": saved["requested_at"]}}
            current["timers"] = [{"instance": "postmaster", "was_enabled": True}]
        saved = shutdown_record.update_session(saved["login"], "postmaster", begin_restore)
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([owner])]),
              patch.object(settle, "_send_settle_message") as tell, redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        self.assertEqual(shutdown_record.find_session(saved["login"], "postmaster"), saved)
        tell.assert_not_called()
        self.assertEqual(self.signals, [])

    def test_darwin_codex_assignment_argument_keeps_app_server_protected_until_pause(self) -> None:
        run = self.root / "codex-run"
        run.mkdir()
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401}))
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        self.natural_exits[101] = 3
        argv = ("401 1 401 S Thu Oct 29 20:00:00 2026 codex -c thread_unload_delay_secs=0 "
                "-c model_auto_compact_token_limit=200000 -c background_terminal_max_timeout=300000 "
                "app-server --listen ws://127.0.0.1:9999")
        def ps(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            text = argv + (" CLAUDE_CODE_SESSION_ID=owner" if "eww" in arguments else "")
            return subprocess.CompletedProcess(arguments, 0, text + "\n", "")
        with (patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=ps)):
            self.processes.update(inventory.shutdown_processes().processes)
        seen: list[set[int]] = []
        pause = self.pause_workers
        def preserve(arguments: list[str]) -> int:
            seen.append({pid for _, pid, _ in self.signals})
            return pause(arguments)
        with patch.object(self, "pause_workers", side_effect=preserve):
            result = self.run_stop([saved])
        self.assertEqual(seen, [set()])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_darwin_ten_owners_and_two_hundred_children_use_only_two_tables_per_pass(self) -> None:
        records: list[shutdown_record.SessionShutdown] = []
        for number in range(10):
            owner = top_level(f"owner-{number}", 1000 + number)
            owner["proc_start"] = "Thu Oct 29 20:00:00 2026"
            records.append(self.save(owner))
            for child in range(20):
                self.process(2000 + number * 20 + child, owner["pid"], started=owner["proc_start"])
        calls: list[tuple[str, ...]] = []
        def ps(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(arguments))
            self.advance(0.01)
            rows = [f"{pid} {process.parent} {pid} S {process.started} build" for pid, process in self.processes.items()]
            return subprocess.CompletedProcess(arguments, 0, "\n".join(rows), "")
        with (patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=ps),
              redirect_stdout(self.output)):
            result = stop.bounded_stop(records, started=0, grace_seconds=0,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=inventory.shutdown_processes,
                kill=self.kill, codex_mesh=lambda _arguments, _budget: 0)
        self.assertEqual(self.processes, {})
        self.assertTrue(all(saved["progress"]["kind"] == "stopped" for saved in result))
        self.assertLessEqual(len(calls), 6)
        self.assertTrue(all("-p" not in arguments for arguments in calls))

    def evidence_stop(self, saved: shutdown_record.SessionShutdown, *, errors: tuple[str, ...] = (),
                      observation_cost: float = 0.02, identity_cost: float = 0.01,
                      pause_cost: float = 0.1, unknown: bool = False,
                      spawn: bool = False) -> list[shutdown_record.SessionShutdown]:
        def observe(budget: float) -> inventory.ProcessObservation:
            self.advance(min(observation_cost, budget))
            if spawn and self.elapsed > 3:
                self.process(1000 + len(self.signals), session_id="owner", arguments=("verify.sh",))
            for pid in list(self.processes):
                _ = self.identity(pid)
            return inventory.ProcessObservation(self.processes.copy() if not unknown else {}, errors)
        def identity(pid: int, budget: float) -> inventory.ProcessIdentity:
            self.advance(min(identity_cost, budget))
            return inventory.ProcessUnknown(f"pid {pid}: unreadable stat") if unknown else self.probe(pid, budget)
        def pause(arguments: list[str], budget: float) -> int:
            self.advance(min(pause_cost, budget))
            return self.pause_workers(arguments) if pause_cost <= budget else 1
        with redirect_stdout(self.output):
            return stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC + timedelta(seconds=self.elapsed),
                observe=observe, identity=identity, kill=self.kill, codex_mesh=pause)

    def test_unrelated_partial_observation_does_not_prevent_proving_the_owner_stopped(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        self.process(777, started="10", arguments=("systemd", "--user"))
        shown = self.linux_table({101: "/user/app-agents.slice/run-owner.scope",
            777: "/user/background.slice/unrelated.service"}, {777})
        self.processes = shown.processes.copy()
        result = self.evidence_stop(saved, errors=("pid 777: environment unread",))
        self.assertEqual(set(self.processes), {777})
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertLess(self.elapsed, 66)
        self.assertIn("stopped  owner", self.output.getvalue())

    def test_every_table_and_identity_probe_failing_retains_unresolved_owner_evidence(self) -> None:
        saved = self.save(top_level("owner", 101))
        result = self.evidence_stop(saved, errors=("ps timed out",), unknown=True)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("101", str(result[0]["progress"]))
        self.assertIn("ps timed out", str(result[0]["progress"]))
        self.assertIn(101, self.processes)
        self.assertEqual(self.elapsed, 66)

    def test_slow_observations_probes_and_pause_share_one_absolute_66_second_budget(self) -> None:
        run = self.root / "slow-pause"
        run.mkdir()
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        result = self.evidence_stop(saved, observation_cost=2, identity_cost=0.5, pause_cost=30)
        self.assertLessEqual(self.elapsed, 66)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("pause", str(result[0]["progress"]))
        self.assertNotIn("stopped  owner", self.output.getvalue())

    def test_continuous_owned_spawning_never_extends_the_deadline(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        result = self.evidence_stop(saved, spawn=True)
        self.assertEqual(self.elapsed, 66)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("unresolved pids", str(result[0]["progress"]))
        self.assertTrue(self.processes)

    def test_costed_complete_observation_after_cleanup_gives_success(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        self.process(301, 101, session_id="owner", arguments=("verify.sh",))
        result = self.evidence_stop(saved)
        self.assertEqual(self.processes, {})
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertIn("all 1 stopped", self.output.getvalue())
        self.assertLess(self.elapsed, 66)

    def test_forced_kill_late_birth_cannot_be_counted_stopped_without_final_cleanup(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.stubborn.add(101)
        kill = self.kill
        def spawn(pid: int, signum: int) -> None:
            if pid == 101 and signum == signal.SIGKILL:
                self.process(301, session_id="owner", arguments=("verify.sh",))
            kill(pid, signum)
        with patch.object(self, "kill", side_effect=spawn):
            result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertLessEqual(self.elapsed, 66)
        self.assertNotIn("all 1 stopped", self.output.getvalue())

    def test_linux_unreadable_process_entry_is_an_incomplete_observation_and_unknown_identity(self) -> None:
        proc = self.root / "proc"
        (proc / "101").mkdir(parents=True)
        read = Path.read_bytes
        text = Path.read_text
        def denied_bytes(path: Path) -> bytes:
            if path.name == "stat":
                raise PermissionError("stat denied")
            return read(path)
        def denied_text(path: Path, *args: object, **kwargs: object) -> str:
            del args, kwargs
            if path.name == "stat":
                raise PermissionError("stat denied")
            return text(path)
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(Path, "read_bytes", denied_bytes), patch.object(Path, "read_text", denied_text)):
            observation = inventory.shutdown_processes()
            state = inventory.shutdown_identity(101)
        self.assertTrue(observation.errors)
        self.assertIn("101", str(observation.errors))
        self.assertIsInstance(state, inventory.ProcessUnknown)

    def test_darwin_mismatched_environment_prefix_grants_no_ownership_and_records_error(self) -> None:
        argv = "301 1 301 S Thu Oct 29 20:00:00 2026 python3 original.py\n"
        env = "301 1 301 S Thu Oct 29 20:00:00 2026 python3 replacement.py CLAUDE_CODE_SESSION_ID=owner\n"
        with (patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=[
              subprocess.CompletedProcess(["ps"], 0, argv, ""), subprocess.CompletedProcess(["ps"], 0, env, "")]) as ps):
            observation = inventory.shutdown_processes()
        self.assertEqual(ps.call_count, 2)
        self.assertEqual(observation.processes[301].session_id, "")
        self.assertTrue(observation.errors)

    def test_build_born_between_observation_and_kill_requires_a_new_complete_observation(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.stubborn.add(101)
        def ready(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "ready", "at": saved["requested_at"]}
        saved = shutdown_record.update_session(saved["login"], "owner", ready)
        kill = self.kill
        def spawn(pid: int, signum: int) -> None:
            if pid == 101 and signum == signal.SIGKILL:
                self.process(301, session_id="owner", arguments=("bash", "verify.sh", "late"))
            kill(pid, signum)
        with patch.object(self, "kill", side_effect=spawn):
            result = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertIn(301, {pid for _, pid, _ in self.signals})
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_child_born_during_pause_is_captured_before_its_surviving_parent_is_signalled(self) -> None:
        run = self.root / "pause-run"
        run.mkdir()
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401}))
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        self.process(401, arguments=("codex", "app-server"))
        self.process(402, 401, arguments=("codex-code-mode-host",))
        self.natural_exits[101] = 3
        def pause(arguments: list[str]) -> int:
            del arguments
            self.advance(0.2)
            self.process(403, 402, arguments=("build",))
            _ = self.processes.pop(401, None)
            return 0
        with patch.object(self, "pause_workers", side_effect=pause):
            result = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertIn(403, {pid for _, pid, _ in self.signals})
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_owned_pid_replacement_gets_its_own_term_and_kill_clock(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.process(301, session_id="owner", arguments=("verify.sh",))
        self.stubborn.add(301)
        self.natural_exits[101] = 3
        kill = self.kill
        replaced = False
        def reuse(pid: int, signum: int) -> None:
            nonlocal replaced
            kill(pid, signum)
            if pid == 301 and signum == signal.SIGKILL and not replaced:
                replaced = True
                self.process(301, started="replacement", session_id="owner", arguments=("verify.sh",))
        with patch.object(self, "kill", side_effect=reuse):
            result = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        terms = [at for at, pid, sig in self.signals if pid == 301 and sig == signal.SIGTERM]
        kills = [at for at, pid, sig in self.signals if pid == 301 and sig == signal.SIGKILL]
        self.assertEqual(len(terms), 2)
        self.assertEqual(len(kills), 2)
        self.assertTrue(all(kill_at - term_at >= 5 for term_at, kill_at in zip(terms, kills)))
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_unrelated_pid_replacement_does_not_inherit_the_old_targets_signal(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.process(301, session_id="owner", arguments=("verify.sh",))
        self.natural_exits[101] = 3
        kill = self.kill
        def reuse(pid: int, signum: int) -> None:
            kill(pid, signum)
            if pid == 301:
                self.process(301, started="unrelated", arguments=("unrelated",))
        with patch.object(self, "kill", side_effect=reuse):
            result = self.run_stop([saved])
        self.assertEqual(self.processes[301].started, "unrelated")
        self.assertEqual([sig for _, pid, sig in self.signals if pid == 301], [signal.SIGTERM])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")

    def test_failed_pause_never_writes_a_stopped_record(self) -> None:
        run = self.root / "pause-run"
        run.mkdir()
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        self.natural_exits[101] = 3
        with patch.object(self, "pause_workers", return_value=1), patch.object(stop, "now_utc", return_value=NOW_UTC):
            result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("pause", str(result[0]["progress"]))
        self.assertNotIn("stopped  owner", self.output.getvalue())

    def test_force_note_reports_inaccessible_git_search_and_shutdown_continues(self) -> None:
        saved = self.save(top_level("owner", 101))
        exists = Path.exists
        def inaccessible(path: Path) -> bool:
            if path.name == ".git":
                raise PermissionError("git directory denied")
            return exists(path)
        with patch.object(Path, "exists", inaccessible):
            result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertIn("could not inspect git files", result[0].get("force_stop_note", ""))

    def test_darwin_argv_assignment_never_grants_environment_ownership(self) -> None:
        argv = "301 1 301 S Thu Oct 29 20:00:00 2026 python3 tool.py CLAUDE_CODE_SESSION_ID=owner"
        shown = subprocess.CompletedProcess(["ps"], 0, argv + "\n", "")
        with (patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", return_value=shown)):
            observed = inventory.shutdown_processes().processes
        self.assertEqual(observed[301].session_id, "")
        self.assertIn("CLAUDE_CODE_SESSION_ID=owner", observed[301].arguments)

    def test_build_detached_by_the_directors_term_handler_is_stopped_before_success(self) -> None:
        saved = self.save(top_level("owner", 101))
        kill = self.kill
        def detach(pid: int, signum: int) -> None:
            kill(pid, signum)
            if pid == 101 and signum == signal.SIGTERM:
                self.process(301, session_id="owner", arguments=("bash", "verify.sh", "test"))
        with patch.object(self, "kill", side_effect=detach):
            stopped = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertIn(301, {pid for _, pid, _ in self.signals})
        self.assertEqual(stopped[0]["progress"]["kind"], "stopped")

    def test_script_basenames_never_exempt_an_owned_process_without_a_keep_record(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        for pid, name in enumerate(("postmaster.py", "notifier.sh", "ups", "postgres"), 301):
            self.process(pid, 101, arguments=("python3", f"/tmp/{name}", "test"))
        _ = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertEqual({pid for _, pid, _ in self.signals}, {301, 302, 303, 304})

    def test_worker_build_survives_until_pause_then_is_reaped_before_completion(self) -> None:
        run = self.root / "worker-run"
        run.mkdir()
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401, "port": 9999}))
        saved = self.save(showrunner("owner", 101, run_dirs=[str(run)]))
        self.process(401, arguments=("codex", "app-server"))
        self.process(402, 401, arguments=("codex-code-mode-host",))
        self.process(403, 402, arguments=("bash", "verify.sh", "test"))
        self.process(404, 403, arguments=("test-binary",))
        self.stubborn.add(403)
        self.natural_exits[101] = 3
        pause = self.pause_workers
        before_pause: list[set[int]] = []
        def preserve(arguments: list[str]) -> int:
            before_pause.append({pid for _, pid, _ in self.signals})
            return pause(arguments)
        with patch.object(self, "pause_workers", side_effect=preserve):
            stopped = self.run_stop([saved])
        self.assertEqual(before_pause, [set()])
        self.assertEqual(self.processes, {})
        self.assertIn((403, signal.SIGKILL), {(pid, sig) for _, pid, sig in self.signals})
        self.assertEqual(stopped[0]["progress"]["kind"], "stopped")
        self.assertIn("stopped build", self.output.getvalue())

    def test_kept_director_receives_no_pause_leaves_no_record_or_hold_and_is_success(self) -> None:
        owner = top_level("postmaster", 101)
        old = self.save(owner)
        real_bounded = stop.bounded_stop
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            with patch.object(stop, "bounded_stop", real_bounded):
                return self.run_stop(records, request_exit=request_exit)
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([owner])]),
              patch.object(settle, "_send_settle_message") as tell,
              patch.object(stop, "bounded_stop", side_effect=bounded), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        tell.assert_not_called()
        self.assertIsNone(shutdown_record.find_session(old["login"], "postmaster"))
        self.assertIsNone(shutdown_record.account_hold(old["login"]))
        self.assertIn(101, self.processes)
        self.assertNotIn("stopped  postmaster", self.output.getvalue())

    def test_adopting_a_restoring_process_revokes_its_pending_confirmation_before_stop(self) -> None:
        import restart
        old = self.save(top_level("restoring", 101))
        def restarting(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarting", "at": old["requested_at"],
                "prior": {"kind": "stopped", "at": old["requested_at"]}}
        claim = shutdown_record.update_session(old["login"], "restoring", restarting)
        current = top_level("restoring", 202)
        self.process(202, started=current["proc_start"])
        accepted: list[bool] = []
        real_bounded = stop.bounded_stop
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            with (patch.object(restart, "_prove_move", return_value=True),
                  patch.object(restart, "_clear_live_outage"),
                  patch.object(restart, "_send_worker_resume"),
                  patch.object(restart, "_restore_timers", return_value=[]),
                  patch.object(restart, "_tell_source_fallback")):
                restart.confirm_entry(claim, old["requested_at"], True, observed_at=0)
            accepted.append(claim["progress"]["kind"] == "restarted")
            if accepted[-1]:
                shutdown_record.archive_session(old["login"], "restoring")
            with patch.object(stop, "bounded_stop", real_bounded):
                return self.run_stop(records, request_exit=request_exit)
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([current])]),
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", side_effect=bounded), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        self.assertEqual(accepted, [False])
        self.assertNotIn(202, self.processes)
        self.assertIn(202, {pid for _, pid, _ in self.signals})

    def test_darwin_observation_uses_two_tables_for_all_identities_and_ownership(self) -> None:
        rows = "301 1 301 S Thu Oct 29 20:00:00 2026 bash verify.sh test CLAUDE_CODE_SESSION_ID=owner UNIT_RUN_DIR=/tmp/run\n302 301 301 S Thu Oct 29 20:00:01 2026 test-binary CLAUDE_CODE_SESSION_ID=owner\n"
        shown = subprocess.CompletedProcess(["ps"], 0, rows, "")
        with (patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess(["ps"], 0, rows.split(" CLAUDE_CODE_SESSION_ID=")[0] + "\n302 301 301 S Thu Oct 29 20:00:01 2026 test-binary\n", ""), shown]) as ps,
              patch.object(process_identity, "process_start", side_effect=AssertionError("per-process ps")),
              patch.object(inventory, "shutdown_identity", side_effect=AssertionError("per-process identity")),
              patch.object(herdr, "darwin_process_environment", side_effect=AssertionError("per-process environment")),
              patch.object(os, "getsid", side_effect=AssertionError("per-process session"))):
            observation = inventory.shutdown_processes()
            observed = observation.processes
        self.assertEqual(ps.call_count, 2)
        self.assertEqual(observed[301].session_id, "")
        self.assertEqual(observed[301].run_dir, "")
        self.assertIn(301, observation.uncertain)
        self.assertEqual(observed[302].process_session, 301)
        self.assertEqual(observed[301].started, "Thu Oct 29 20:00:00 2026")
        self.assertEqual(observed[301].arguments, ("bash", "verify.sh", "test"))

    def test_a_timed_out_darwin_observation_does_not_abort_shutdown(self) -> None:
        with (patch.object(sys, "platform", "darwin"),
              patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 5))):
            self.assertTrue(inventory.shutdown_processes().errors)

    def test_failed_initial_observation_still_stops_the_exact_recorded_director(self) -> None:
        saved = self.save(top_level("owner", 101))
        first = True
        def observe(budget: float) -> inventory.ProcessObservation:
            nonlocal first
            if first:
                first = False
                return inventory.ProcessObservation({}, ("initial table failed",))
            return self.observe(budget)
        with redirect_stdout(self.output):
            stopped = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                sleep=self.advance, clock=lambda: NOW_UTC + timedelta(seconds=self.elapsed),
                observe=observe, identity=self.probe, kill=self.kill, codex_mesh=lambda arguments, _budget: self.pause_workers(arguments))
        self.assertEqual(self.processes, {})
        self.assertEqual(stopped[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {101})

    def test_worker_build_found_at_director_exit_is_preserved_through_pause_then_stopped(self) -> None:
        run = self.root / "late-worker-run"
        run.mkdir()
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401, "port": 9999}))
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        self.process(401, arguments=("codex", "app-server"))
        self.process(402, 401, arguments=("codex-code-mode-host",))
        kill, pause = self.kill, self.pause_workers
        before_pause: list[set[int]] = []
        def detach(pid: int, signum: int) -> None:
            kill(pid, signum)
            if pid == 101 and signum == signal.SIGTERM:
                self.process(403, 402, arguments=("bash", "verify.sh", "test"))
        def preserve(arguments: list[str]) -> int:
            before_pause.append({pid for _, pid, _ in self.signals if pid in {401, 402, 403}})
            return pause(arguments)
        with (patch.object(self, "kill", side_effect=detach), patch.object(self, "pause_workers", side_effect=preserve)):
            _ = self.run_stop([saved])
        self.assertEqual(before_pause, [set()])
        self.assertEqual(self.processes, {})
        self.assertIn(403, {pid for _, pid, _ in self.signals})

    def test_worker_descendant_that_survives_kill_prevents_a_successful_stop_count(self) -> None:
        run = self.root / "stubborn-worker-run"
        run.mkdir()
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401, "port": 9999}))
        saved = self.save(top_level("owner", 101, run_dirs=[str(run)]))
        self.process(401, arguments=("codex", "app-server"))
        self.process(402, 401, arguments=("codex-code-mode-host",))
        self.process(403, 402, arguments=("bash", "verify.sh", "test"))
        self.natural_exits[101] = 3
        kill = self.kill
        def refuse(pid: int, signum: int) -> None:
            if pid == 403:
                self.signals.append((self.elapsed, pid, signum))
            else:
                kill(pid, signum)
        with patch.object(self, "kill", side_effect=refuse):
            stopped = self.run_stop([saved])
        self.assertEqual(stopped[0]["progress"]["kind"], "stop failed")
        self.assertIn(403, self.processes)
        self.assertNotIn("all 1 stopped", self.output.getvalue())
        self.assertIn("0 of 1 stopped", self.output.getvalue())

    def test_missing_stop_record_never_counts_a_still_live_director_as_stopped(self) -> None:
        saved = self.save(top_level("owner", 101))
        advance = self.advance
        removed = False
        def withdraw(seconds: float) -> None:
            nonlocal removed
            advance(seconds)
            if not removed:
                removed = True
                shutdown_record.archive_session(saved["login"], "owner")
        with patch.object(self, "advance", side_effect=withdraw):
            stopped = self.run_stop([saved])
        self.assertEqual(stopped[0]["progress"]["kind"], "stop failed")
        self.assertIn(101, self.processes)
        self.assertIn("0 of 1 stopped", self.output.getvalue())

    def test_fresh_kept_infrastructure_and_an_ordinary_owner_share_a_successful_shutdown(self) -> None:
        kept, owner = top_level("ups", 101), top_level("owner", 202)
        self.process(101, started=kept["proc_start"])
        self.process(202, started=owner["proc_start"])
        self.natural_exits[202] = 3
        real_bounded = stop.bounded_stop
        told: list[str] = []
        def tell(saved: shutdown_record.SessionShutdown) -> None:
            told.append(saved["session"]["name"])
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            with patch.object(stop, "bounded_stop", real_bounded):
                return self.run_stop(records, request_exit=request_exit)
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([kept, owner])]),
              patch.object(settle, "_send_settle_message", side_effect=tell),
              patch.object(stop, "bounded_stop", side_effect=bounded), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        self.assertTrue(set(told) <= {"owner"})
        self.assertEqual(set(self.processes), {101})
        self.assertEqual([saved["session"]["name"] for saved in shutdown_record.session_records("owner@example.com")], ["owner"])
        self.assertIsNone(shutdown_record.account_hold("owner@example.com"))

    def test_force_note_finds_git_files_when_current_directory_is_a_subfolder(self) -> None:
        checkout = self.root / "checkout"
        gitdir = checkout / ".git"
        gitdir.mkdir(parents=True)
        nested = checkout / "src" / "deep"
        nested.mkdir(parents=True)
        merge, lock = gitdir / "MERGE_HEAD", gitdir / "index.lock"
        _ = merge.write_text("merge in progress")
        _ = lock.write_text("locked")
        owner = top_level("owner", 101)
        owner["cwd"] = str(nested)
        stopped = self.run_stop([self.save(owner)])[0]
        note = stopped.get("force_stop_note", "")
        self.assertIn(str(merge), note)
        self.assertIn(str(lock), note)
        self.assertEqual(lock.read_text(), "locked")

    def test_one_deadline_for_all_accounts_and_natural_exit_printed_before_forced_exit(self) -> None:
        fast = self.save(top_level("fast", 101))
        slow = self.save(unit("slow", 201))
        slow["login"], slow["label"] = "second@example.com", "claude4"
        shutdown_record.archive_session(fast["login"], "slow")
        self.assertTrue(shutdown_record.create_session(slow))
        self.natural_exits[101] = 3
        self.stubborn.add(201)
        result = self.run_stop([slow, fast])
        self.assertEqual([(when, sig) for when, pid, sig in self.signals if pid == 201],
                         [(60.0, signal.SIGTERM), (65.0, signal.SIGKILL)])
        self.assertFalse(any(pid == 101 for _, pid, _ in self.signals))
        self.assertEqual([saved["progress"]["kind"] for saved in result], ["stopped", "stopped"])
        lines = self.output.getvalue().splitlines()
        self.assertIn("17:49:13  stopped  fast (by itself, 3.0 s)", lines[0])
        self.assertIn("stopped  slow (forced at 60 s)", lines[1])
        self.assertEqual(lines[-1], "all 2 stopped")
        self.assertTrue(any("stopped  fast" in flushed and "stopped  slow" not in flushed for flushed in self.output.flushed))

    def test_kills_owned_descendants_detached_build_scope_and_servers_and_preserves_unrelated_pids(self) -> None:
        run = self.root / "run"
        (run / "mesh_runs").mkdir(parents=True)
        path = run / "mesh_runs" / "build.json"
        _ = path.write_text(json.dumps({"status": "running", "pid": 301, "label": "unit tests"}), encoding="utf-8")
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401, "port": 9999}), encoding="utf-8")
        saved = self.save(showrunner("owner", 101, run_dirs=[str(run)]))
        self.process(102, 101)
        self.process(103, 102)
        self.process(301, arguments=("python3", "mesh_runs.py", "supervise", str(path)))
        self.process(302, 301, build_unit="run-owned.scope")
        self.process(303, build_unit="run-owned.scope")
        self.process(401, arguments=("codex", "app-server", "--listen", "ws://127.0.0.1:9999"))
        self.process(402, 401)
        self.process(901, build_unit="unrelated.scope")
        self.process(902, arguments=("python3", "mesh_runs.py", "supervise", "/other/build.json"))
        self.natural_exits[101] = 3
        _ = self.run_stop([saved])
        self.assertEqual(set(self.processes), {901, 902})
        self.assertEqual({pid for _, pid, _ in self.signals}, {102, 103, 301, 302, 303})
        self.assertIn("stopped build  unit tests (owner)", self.output.getvalue())
        self.assertIn("run-owned.scope", self.output.getvalue())

    def test_releases_only_owned_build_claims_and_keeps_infrastructure_and_remote_job_records(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        self.process(301, session_id="owner", arguments=("bash", "verify.sh"))
        self.process(302, 301, arguments=("ups",))
        self.process(303, 301, arguments=("postgres", "postmaster"))
        self.process(304, 301, arguments=("bash", "notifier.sh"))
        self.process(305, 301, arguments=("timer", "timer-owned"))
        self.process(306, 301, arguments=("user-kept-build",))
        def timer(current: shutdown_record.SessionShutdown) -> None:
            current["timers"] = [{"instance": "timer-owned", "was_enabled": True}]
        saved = shutdown_record.update_session(saved["login"], "owner", timer)
        holds = self.root / "holds"
        holds.mkdir()
        _ = (holds / "owner").write_text(json.dumps({"keep": [{"pid": pid, "start_time": f"start-{pid}"} for pid in range(302, 307)]}))
        _ = (holds / "unrelated").write_text(json.dumps({"holder_session": "another-session"}))
        queue = self.root / "buildlog" / "admission"
        queue.mkdir(parents=True)
        mine = queue / "301-start-301.waiting"
        other = queue / "901-start-901.waiting"
        _ = mine.write_text(json.dumps({"pid": 301, "start_time": "start-301"}))
        _ = other.write_text(json.dumps({"pid": 901, "start_time": "start-901"}))
        steps = self.root / "steps"
        steps.mkdir()
        step = steps / "owned.json"
        _ = step.write_text(json.dumps({"pid": 301, "proc_start": "start-301", "state": "on_mac"}))
        mac = self.root / "mac"
        (mac / "claims").mkdir(parents=True)
        remote = mac / "claims" / "remote.json"
        local = mac / "claims" / "local.json"
        _ = remote.write_text(json.dumps({"pid": 777, "holder_session": "owner", "remote_host": "mac"}))
        _ = local.write_text(json.dumps({"pid": 301, "proc_start": "start-301"}))
        _ = self.run_stop([saved])
        self.assertEqual(set(self.processes), {302, 303, 304, 305, 306})
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.assertFalse(step.exists())
        self.assertFalse(local.exists())
        self.assertTrue(remote.exists())
        self.assertFalse((holds / "owner").exists())
        self.assertTrue((holds / "unrelated").exists())
        self.assertIn("left running build  mac", self.output.getvalue())
        self.assertIn("Mac offload", self.output.getvalue())

    def test_pid_reuse_during_grace_is_never_signalled(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.process(102, 101)
        self.natural_exits[101] = 3
        original = self.advance
        def replace(seconds: float) -> None:
            original(seconds)
            if self.elapsed == 1:
                self.process(102, started="a-new-unrelated-process")
        with patch.object(self, "advance", side_effect=replace):
            _ = self.run_stop([saved])
        self.assertFalse(any(pid == 102 for _, pid, _ in self.signals))
        self.assertEqual(self.processes[102].started, "a-new-unrelated-process")

    def test_owned_mac_block_releases_through_unblock_so_saved_ci_state_is_restored(self) -> None:
        saved = self.save(top_level("director", 101))
        self.natural_exits[101] = 3
        block = self.root / "mac/block.json"
        block.parent.mkdir()
        _ = block.write_text(json.dumps({"holder": "Director", "session": "director", "ci": "off_by_us"}))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "Mac unblocked", "")) as release:
            _ = self.run_stop([saved])
        release.assert_called_once()
        arguments = cast(list[str], release.call_args.args[0])
        self.assertEqual(arguments[2:], ["unblock", "--holder", "Director"])
        self.assertTrue(arguments[1].endswith("/mac_test/mac_test.py"))
        self.assertTrue(block.exists(), "only the unblock protocol may delete a block and its saved CI state")

    def test_detached_verify_run_argument_alone_grants_no_ownership(self) -> None:
        directory = self.root / "run"
        directory.mkdir()
        saved = self.save(top_level("director", 101, run_dirs=[str(directory)]))
        self.natural_exits[101] = 3
        self.process(301, arguments=("bash", "verify.sh", "--session-dir", str(directory), "test", "package"))
        self.process(302, 301, arguments=("test-binary",))
        self.process(901, arguments=("bash", "verify.sh", "--session-dir", str(directory) + "-other", "test", "package"))
        _ = self.run_stop([saved])
        self.assertEqual(self.signals, [])
        self.assertEqual(set(self.processes), {301, 302, 901})
        self.assertNotIn("stopped build", self.output.getvalue())

    def assert_late_detached_build_stops(self, *, mesh: bool) -> None:
        run = self.root / "late-run"
        (run / "mesh_runs").mkdir(parents=True)
        saved = self.save(showrunner("owner", 101, run_dirs=[str(run)]))
        self.natural_exits[101] = 3
        self.stubborn.add(301)
        self.process(999, arguments=("bash", "verify.sh", "--session-dir", str(run) + "-other"))
        claims = self.root / "steps"
        claims.mkdir()
        claim = claims / "late.json"
        advance = self.advance
        spawned = False

        def spawn(seconds: float) -> None:
            nonlocal spawned
            advance(seconds)
            if not spawned and self.elapsed >= 1:
                spawned = True
                if mesh:
                    path = run / "mesh_runs" / "late.json"
                    _ = path.write_text(json.dumps({"status": "running", "pid": 301, "label": "late build"}))
                    self.process(301, arguments=("python3", "mesh_runs.py", "supervise", str(path)))
                else:
                    self.process(301, arguments=("bash", "verify.sh", "--session-dir", str(run)), run_dir=str(run))
                self.process(302, 301)
                _ = claim.write_text(json.dumps({"pid": 301, "proc_start": "start-301"}))

        with patch.object(self, "advance", side_effect=spawn):
            stopped = self.run_stop([saved])
        self.assertEqual(set(self.processes), {999})
        signals = [(when, sig) for when, pid, sig in self.signals if pid == 301]
        self.assertEqual(signals[0], (1.0, signal.SIGTERM))
        self.assertEqual(signals[-1], (6.0, signal.SIGKILL))
        self.assertEqual(stopped[0]["progress"]["kind"], "stopped")
        self.assertIn("stopped build", self.output.getvalue())
        self.assertFalse(claim.exists(), "the build claim must be released after its SIGKILL")

    def test_detached_verify_started_during_grace_is_discovered_and_stopped(self) -> None:
        self.assert_late_detached_build_stops(mesh=False)

    def test_detached_build_started_just_before_owner_exit_is_discovered_before_completion(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 0.2
        advance = self.advance
        spawned = False
        def spawn(seconds: float) -> None:
            nonlocal spawned
            advance(seconds)
            if not spawned:
                spawned = True
                self.process(301, arguments=("bash", "verify.sh", "test"), session_id="owner")
        with patch.object(self, "advance", side_effect=spawn):
            _ = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertIn(301, {pid for _, pid, _ in self.signals})

    def test_mesh_supervisor_record_created_during_grace_is_discovered_and_stopped(self) -> None:
        self.assert_late_detached_build_stops(mesh=True)

    def test_build_arguments_named_like_infrastructure_do_not_protect_the_build(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.natural_exits[101] = 3
        self.process(301, 101, session_id="owner", arguments=(
            "bash", "verify.sh", "test", "postmaster", "ups", "notifier.sh", "timer-owned"))
        self.process(302, 301)
        def timers(current: shutdown_record.SessionShutdown) -> None:
            current["timers"] = [{"instance": "timer-owned", "was_enabled": True}]
        saved = shutdown_record.update_session(saved["login"], "owner", timers)
        _ = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertEqual({pid for _, pid, _ in self.signals}, {301, 302})
        self.assertIn("stopped build", self.output.getvalue())

    def test_recorded_infrastructure_director_is_kept_and_never_reported_stopped(self) -> None:
        saved = self.save(top_level("postmaster", 101))
        self.process(101, started=saved["session"]["proc_start"], arguments=(
            "claude", "--remote-control", "postmaster", "-n", "postmaster"))
        self.process(102, 101)
        kept = self.run_stop([saved])
        self.assertEqual(set(self.processes), {101, 102})
        self.assertEqual(self.signals, [])
        self.assertEqual(kept, [])
        self.assertIsNone(shutdown_record.find_session(saved["login"], "postmaster"))
        self.assertIn("kept  postmaster", self.output.getvalue())
        self.assertNotIn("stopped  postmaster", self.output.getvalue())
        self.assertIn("all 0 stopped", self.output.getvalue())

    def test_app_server_code_mode_child_is_preserved_until_protocol_pause(self) -> None:
        run = self.root / "worker-run"
        run.mkdir()
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401, "port": 9999}))
        saved = self.save(showrunner("owner", 101, run_dirs=[str(run)]))
        self.process(401, arguments=("codex", "app-server"))
        self.process(402, 401, arguments=("codex-code-mode-host",), session_id="owner", run_dir=str(run))
        self.natural_exits[101] = 3
        pause = self.pause_workers
        observed: list[tuple[bool, set[int]]] = []
        def preserved(arguments: list[str]) -> int:
            observed.append((402 in self.processes, {pid for _, pid, _ in self.signals if pid == 402}))
            return pause(arguments)
        with patch.object(self, "pause_workers", side_effect=preserved):
            stopped = self.run_stop([saved])
        self.assertEqual(stopped[0]["progress"]["kind"], "stopped")
        self.assertEqual(observed, [(True, set())])
        self.assertEqual(self.processes, {})

    def test_darwin_detached_build_ownership_is_read_from_its_process_environment(self) -> None:
        saved = self.save(top_level("owner", 101))
        shown = subprocess.CompletedProcess(["ps"], 0, "301 1 301 S Thu Oct 29 20:00:00 2026 bash verify.sh test CLAUDE_CODE_SESSION_ID=owner\n", "")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess(["ps"], 0, "301 1 301 S Thu Oct 29 20:00:00 2026 bash verify.sh test\n", ""), shown]),
              patch.object(process_identity, "process_start", return_value=process_identity.ProcessStartDate("Thu Oct 29 20:00:00 2026")),
              patch.object(herdr, "darwin_process_environment", return_value={"CLAUDE_CODE_SESSION_ID": "owner"}),
              patch.object(os, "getsid", return_value=301)):
            self.processes.update(inventory.shutdown_processes().processes)
        self.natural_exits[101] = 3
        _ = self.run_stop([saved])
        self.assertEqual(self.processes, {})
        self.assertIn(301, {pid for _, pid, _ in self.signals})

    def test_requester_helper_waits_for_the_command_then_offers_a_full_checkpoint_window(self) -> None:
        saved = self.save(top_level("caller", 101))
        delivered = threading.Event()
        def tell_owner(current: shutdown_record.SessionShutdown) -> None:
            del current
            delivered.set()
        def checkpoint(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            for saved in records:
                request_exit(saved)
            self.assertTrue(delivered.wait(timeout=2), "requester must receive a pause request")
            return records
        with (patch.object(stop, "shutdown_identity", side_effect=[inventory.ProcessLive("command-start"), inventory.ProcessGone()]),
              patch.object(time, "sleep") as sleep,
              patch.object(settle, "_send_settle_message", side_effect=tell_owner) as tell,
              patch.object(stop, "bounded_stop", side_effect=checkpoint) as bounded):
            self.assertEqual(stop.main(["finish-requester", saved["login"], "caller", "999", "command-start"]), 0)
        sleep.assert_called_once_with(0.05)
        tell.assert_called_once_with(saved)
        bounded.assert_called_once()
        self.assertEqual(bounded.call_args.args[0], [saved])
        self.assertEqual(bounded.call_args.kwargs.get("grace_seconds", stop.SHUTDOWN_GRACE_SECONDS), 60)

    def test_force_warning_names_existing_git_files_in_a_linked_worktree(self) -> None:
        checkout = self.root / "checkout"
        gitdir = self.root / "git-dir"
        checkout.mkdir()
        gitdir.mkdir()
        _ = (checkout / ".git").write_text(f"gitdir: {gitdir}\n")
        merge = gitdir / "MERGE_HEAD"
        lock = gitdir / "index.lock"
        _ = merge.write_text("merge in progress")
        _ = lock.write_text("locked")
        owner = top_level("owner", 101)
        owner["cwd"] = str(checkout)
        saved = self.save(owner)
        stopped = self.run_stop([saved])[0]
        note = stopped.get("force_stop_note", "")
        self.assertIn(str(merge), note)
        self.assertIn(str(lock), note)
        self.assertEqual(lock.read_text(), "locked")

    def test_records_live_codex_seats_and_resume_prompts_before_stopping_workers(self) -> None:
        run = self.root / "mesh"
        run.mkdir()
        roster = run / "mesh_roster.json"
        _ = roster.write_text(json.dumps({"impl": {"status": "running", "thread_id": "thread-1", "role": "impl"},
                                         "test": {"status": "done", "thread_id": "thread-2", "role": "test"}}), encoding="utf-8")
        saved = self.save(top_level("director", 101, run_dirs=[str(run)]))
        self.natural_exits[101] = 3
        stopped = self.run_stop([saved])[0]
        self.assertEqual([(seat["seat"], seat["thread_id"], seat["turn_live"]) for seat in stopped.get("codex_seats", [])],
                         [("impl", "thread-1", True), ("test", "thread-2", False)])
        self.assertIn("Continue the task", (run / "impl.resume-prompt.md").read_text())

    def test_force_stop_pauses_worker_and_preserves_server_queue_only_after_director_dies(self) -> None:
        from scripts.agents import codex_mesh
        from scripts.agents.test_codex_mesh import StubAppServer

        run = self.root / "queued-worker"
        run.mkdir()
        queued: list[dict[str, object]] = [{"id": "message-1", "input": [{"type": "text", "text": "finish the regression"}]}]
        interrupted: list[str] = []
        def respond(request: dict[str, object]) -> list[dict[str, object]]:
            result: dict[str, object] = {}
            method = request.get("method")
            if method == "thread/loaded/list":
                result = {"data": ["thread-1"], "nextCursor": None}
            elif method == "thread/queue/list":
                result = {"data": queued.copy(), "nextCursor": None}
            elif method == "thread/queue/delete":
                # The disk copy must exist before the server drops its message.
                self.assertIn("finish the regression", (run / "impl.pending.json").read_text())
                queued.clear()
                result = {"deleted": True}
            elif method == "thread/read":
                result = {"thread": {"id": "thread-1", "status": {"type": "active"}}}
            elif method == "thread/turns/list":
                result = {"data": [{"id": "turn-1", "status": "inProgress"}], "nextCursor": None}
            elif method == "turn/interrupt":
                interrupted.append("turn-1")
            return [{"id": request["id"], "result": result}]
        server = StubAppServer(respond)
        self.addCleanup(server.close)
        _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 401, "port": server.port}))
        _ = (run / "mesh_roster.json").write_text(json.dumps({"impl": {"thread_id": "thread-1",
            "status": "running", "role": "impl", "turn_id": "turn-1", "port": server.port}}))
        saved = self.save(top_level("director", 101, run_dirs=[str(run)]))
        self.process(401, arguments=("codex", "app-server"))
        def reap(pid: int) -> bool:
            self.assertIsNone(self.identity(101))
            self.assertEqual(queued, [])
            _ = self.processes.pop(pid, None)
            return True
        def pause(arguments: list[str]) -> int:
            self.assertIsNone(self.identity(101), "the director must die before protocol pause")
            self.assertEqual(arguments, ["stop", "--pause", "--session-dir", str(run)])
            return codex_mesh.command_stop(argparse.Namespace(session_dir=str(run), pause=True))
        def alive(pid: int) -> bool:
            return self.identity(pid) is not None
        def director_alive(_run: Path) -> bool:
            return self.identity(101) is not None
        with (patch.object(codex_mesh, "_pid_alive", side_effect=alive),
              patch.object(codex_mesh, "_reap", side_effect=reap),
              patch.object(codex_mesh.unit_workers, "run_has_live_director", side_effect=director_alive),
              patch.object(self, "pause_workers", side_effect=pause)):
            stopped = self.run_stop([saved])[0]
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        self.assertEqual(roster["impl"]["status"], "paused")
        self.assertIs(roster["impl"]["paused_turn"], True)
        self.assertEqual(roster["impl"]["paused_from"], "running")
        self.assertIn("finish the regression", (run / "impl.pending.json").read_text())
        self.assertEqual(interrupted, ["turn-1"])
        self.assertTrue(stopped.get("codex_seats", [])[0]["turn_live"])
        self.assertFalse(any(pid == 401 for _, pid, _ in self.signals))

    def test_finished_record_is_archived_and_live_restore_details_replace_it(self) -> None:
        for progress in ("stopped", "restarted", "restart failed"):
            with self.subTest(progress=progress):
                session_id = "owner-" + progress.replace(" ", "-")
                old = self.save(top_level(session_id, 101))
                def finish(current: shutdown_record.SessionShutdown) -> None:
                    if progress == "restart failed":
                        current["progress"] = {"kind": "restart failed", "at": old["requested_at"], "reason": "old folder"}
                    elif progress == "stopped":
                        current["progress"] = {"kind": "stopped", "at": old["requested_at"]}
                    else:
                        current["progress"] = {"kind": "restarted", "at": old["requested_at"]}
                _ = shutdown_record.update_session(old["login"], session_id, finish)
                fresh = entry(top_level(session_id, 202), "waiting")
                fresh["session"]["cwd"] = "/new/current-folder"
                fresh["request_id"] = "new-request"
                self.assertTrue(shutdown_record.create_session(fresh))
                current = shutdown_record.find_session(old["login"], session_id)
                self.assertIsNotNone(current)
                if current is not None:
                    self.assertEqual(current["session"]["cwd"], "/new/current-folder")
                    self.assertEqual(current["session"]["pid"], 202)
                history = list((self.root / "state" / old["login"] / "history").glob("*.json"))
                self.assertTrue(any('/tmp/' + session_id in path.read_text() for path in history))

    def test_in_flight_restore_and_move_records_survive_a_new_pid(self) -> None:
        for moving in (False, True):
            with self.subTest(moving=moving):
                identity = "moving" if moving else "restoring"
                old = self.save(top_level(identity, 101))
                def in_flight(current: shutdown_record.SessionShutdown) -> None:
                    if moving:
                        current["target"] = "claude2"
                    else:
                        current["progress"] = {"kind": "restarting", "at": old["requested_at"],
                            "prior": {"kind": "stopped", "at": old["requested_at"]}}
                _ = shutdown_record.update_session(old["login"], identity, in_flight)
                fresh = entry(top_level(identity, 202), "waiting")
                self.assertFalse(shutdown_record.create_session(fresh))
                retained = shutdown_record.find_session(old["login"], identity)
                self.assertIsNotNone(retained)
                if retained is not None:
                    self.assertEqual(retained["session"]["pid"], 101)
                    self.assertEqual(retained["request_id"], old["request_id"])
                self.assertEqual(list((self.root / "state" / old["login"] / "history").glob("*.json")), [])

    def test_forced_stop_saves_tree_check_warning_without_removing_index_lock(self) -> None:
        saved = self.save(top_level("forced", 101))
        checkout = self.root / "checkout" / ".git"
        checkout.mkdir(parents=True)
        lock = checkout / "index.lock"
        _ = lock.write_text("in-progress index")
        stopped = self.run_stop([saved])[0]
        self.assertIn("stopped by force at 60 s, no ready line", stopped.get("force_stop_note", ""))
        self.assertIn("MERGE_HEAD", stopped.get("force_stop_note", ""))
        self.assertEqual(lock.read_text(), "in-progress index")

    def test_a_move_cannot_take_over_an_open_stop_from_a_different_live_process(self) -> None:
        old = self.save(top_level("owner", 101))
        owner = top_level("owner", 202)
        with redirect_stdout(self.output):
            added = settle.begin(old["login"], request_id="new-move", target="claude2", report=machine_inventory([owner]))
        self.assertEqual(added, [])
        self.assertEqual(shutdown_record.find_session(old["login"], "owner"), old)
        self.assertIn("in flight for another process", self.output.getvalue())

    def test_account_down_refreshes_finished_record_but_refuses_an_open_moves_new_pid(self) -> None:
        for moving in (False, True):
            with self.subTest(moving=moving):
                identity = "moving" if moving else "finished"
                old = self.save(top_level(identity, 101))
                def previous(saved: shutdown_record.SessionShutdown) -> None:
                    if moving:
                        saved["target"] = "claude2"
                    else:
                        saved["progress"] = {"kind": "stopped", "at": old["requested_at"]}
                _ = shutdown_record.update_session(old["login"], identity, previous)
                live = top_level(identity, 202)
                live["cwd"] = "/current-checkout"
                with (patch.object(settle, "_warm", return_value=True),
                      patch.object(settle, "_launch_conductor") as launch,
                      patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "requester"}), redirect_stdout(self.output)):
                    result = settle.down(Account("claude", old["login"], old["label"]), here=True,
                                         only=[identity], report=machine_inventory([live]))
                retained = shutdown_record.find_session(old["login"], identity)
                self.assertIsNotNone(retained)
                if retained is not None:
                    self.assertEqual(retained["session"]["pid"], 101 if moving else 202)
                    if not moving:
                        self.assertEqual(retained["session"]["cwd"], "/current-checkout")
                        self.assertNotIn("stop_deadline", retained)
                self.assertEqual(result, 1 if moving else 0)
                self.assertEqual(launch.call_count, 0 if moving else 1)

    def assert_account_conflict_writes_nothing(self, target: str) -> None:
        old = self.save(top_level("restoring", 101))
        def restarting(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarting", "at": old["requested_at"],
                "prior": {"kind": "stopped", "at": old["requested_at"]}}
        previous = shutdown_record.update_session(old["login"], "restoring", restarting)
        report = machine_inventory([top_level("restoring", 202), top_level("healthy", 303)])
        with (patch.object(settle, "_warm", return_value=True),
              patch.object(settle, "_launch_conductor") as launch,
              patch.object(settle, "_revoke_launches") as revoke,
              patch.object(settle, "_carry_trust") as trust, redirect_stdout(self.output)):
            self.assertEqual(settle.down(Account("claude", old["login"], old["label"]),
                here=True, target=target, report=report), 1)
        self.assertEqual(shutdown_record.find_session(old["login"], "restoring"), previous)
        self.assertIsNone(shutdown_record.find_session(old["login"], "healthy"))
        self.assertIsNone(shutdown_record.account_hold(old["login"]))
        launch.assert_not_called()
        revoke.assert_not_called()
        trust.assert_not_called()

    def test_account_down_refuses_a_conflict_before_writing_other_sessions_or_a_hold(self) -> None:
        self.assert_account_conflict_writes_nothing("")

    def test_account_move_refuses_a_conflict_before_writing_other_sessions_or_a_hold(self) -> None:
        self.assert_account_conflict_writes_nothing("claude2")

    def test_account_down_rescans_after_revocation_so_a_completed_launch_is_not_missed(self) -> None:
        initial = machine_inventory([])
        final = machine_inventory([top_level("arrived", 202)])
        reads = 0
        def scan(login: str, ids: tuple[str, ...]) -> inventory.Inventory:
            nonlocal reads
            del login, ids
            reads += 1
            if reads == 1:
                return initial
            self.assertIsNotNone(shutdown_record.account_hold(final["login"]))
            return final
        with (patch.object(settle, "_warm", return_value=True),
              patch.object(settle, "run_inventory", side_effect=scan),
              patch.object(settle, "_launch_conductor") as launch,
              patch.object(settle, "_revoke_launches"), redirect_stdout(self.output)):
            self.assertEqual(settle.down(Account("claude", final["login"], final["label"]), here=True), 0)
        arrived = shutdown_record.find_session(final["login"], "arrived")
        self.assertIsNotNone(arrived)
        if arrived is not None:
            self.assertEqual(arrived["session"]["pid"], 202)
        launch.assert_called_once_with(final["login"], final["label"])

    def test_now_replaces_stopped_record_from_old_folder_before_forcing_live_session(self) -> None:
        old = self.save(top_level("owner", 101))
        _ = shutdown_record.update_session(old["login"], "owner", lambda current: current.update(progress={"kind": "stopped", "at": old["requested_at"]}))
        current = top_level("owner", 202, status="busy")
        current["cwd"] = "/current-folder"
        seen: list[shutdown_record.SessionShutdown] = []
        def stop_now(records: list[shutdown_record.SessionShutdown], *, started: float, grace_seconds: float) -> list[shutdown_record.SessionShutdown]:
            del started
            self.assertEqual(grace_seconds, 0)
            seen.extend(copy.deepcopy(records))
            for saved in records:
                saved["progress"] = {"kind": "stopped", "at": saved["requested_at"]}
            return records
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([current])),
              patch.object(stop, "bounded_stop", side_effect=stop_now), redirect_stdout(self.output)):
            self.assertEqual(settle.now(Account("claude", old["login"], old["label"]), True, ["owner"]), 0)
        self.assertEqual(seen[0]["session"]["cwd"], "/current-folder")
        self.assertEqual(seen[0]["session"]["pid"], 202)
        self.assertNotIn("nothing to stop", self.output.getvalue())

    def test_now_returns_failure_when_the_bounded_stop_or_worker_pause_fails(self) -> None:
        saved = self.save(top_level("owner", 101))
        for failed in (True, False):
            with self.subTest(stop_failed=failed):
                def stop_now(records: list[shutdown_record.SessionShutdown], *, started: float, grace_seconds: float) -> list[shutdown_record.SessionShutdown]:
                    del started, grace_seconds
                    for current in records:
                        if failed:
                            current["progress"] = {"kind": "stop failed", "at": current["requested_at"], "reason": "still alive"}
                        else:
                            current["progress"] = {"kind": "stopped", "at": current["requested_at"]}
                            current["stop_issues"] = [{"kind": "codex server left running", "at": current["requested_at"],
                                "run_dir": "/owned/run", "cause": "stop not confirmed"}]
                    return records
                with (patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
                      patch.object(stop, "bounded_stop", side_effect=stop_now), redirect_stdout(self.output)):
                    self.assertEqual(settle.now(Account("claude", saved["login"], saved["label"]), True, ["owner"]), 1)

    def test_down_all_revokes_launch_admitted_before_its_inventory_and_closes_only_its_new_pane(self) -> None:
        first = self.save(top_level("seed", 101))
        report = machine_inventory([])
        with (patch.object(inventory, "all_live_accounts", return_value=[]),
              patch.object(settle, "run_inventory", return_value=report),
              patch.object(stop, "bounded_stop", return_value=[{**first, "progress": {"kind": "stopped", "at": first["requested_at"]}}]),
              patch.object(herdr, "close_agent") as close,
              redirect_stdout(self.output)):
            with self.assertRaisesRegex(OSError, "revoked"):
                with launch_permission.admitted_launch(first["login"], "late", "late", lambda: None):
                    self.assertEqual(settle.down_all(), 0)
                    launch_permission.claim_launch_pane("agents", "pane-new")
        close.assert_called_once_with("agents", "pane-new")

    def test_down_all_stops_an_account_seen_only_by_the_final_inventory(self) -> None:
        report = machine_inventory([top_level("late-owner", 202)])
        report["login"], report["label"] = "late@example.com", "claude4"
        selected: list[shutdown_record.SessionShutdown] = []
        def stopped(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            selected.extend(copy.deepcopy(records))
            self.assertIsNotNone(shutdown_record.account_hold(report["login"]))
            self.assertIsNotNone(shutdown_record.find_session(report["login"], "late-owner"))
            for saved in records:
                saved["progress"] = {"kind": "stopped", "at": saved["requested_at"]}
            return records
        with (patch.object(inventory, "all_live_accounts", side_effect=[[], [report]]),
              patch.object(launch_permission, "pending_launches", return_value=()),
              patch.object(settle, "_revoke_launches") as revoke,
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", side_effect=stopped), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        self.assertEqual([saved["session"]["session_id"] for saved in selected], ["late-owner"])
        self.assertEqual(selected[0]["login"], report["login"])
        revoke.assert_called_once_with(report["login"])

    def test_down_all_blocks_a_recorded_restore_admitted_after_account_revocation(self) -> None:
        previous = self.save(top_level("late-restore", 101))
        def finished(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "stopped", "at": previous["requested_at"]}
        _ = shutdown_record.update_session(previous["login"], "late-restore", finished)
        reads = 0
        def scan() -> list[inventory.Inventory]:
            nonlocal reads
            reads += 1
            if reads == 2:
                # A recorded restore's admission callback can allow the stopped account hold.
                # The machine fence must refuse before that callback runs.
                with launch_permission.admitted_launch(previous["login"], "late-restore", "late-restore", lambda: None):
                    self.fail("restore admitted during bounded shutdown")
            return [machine_inventory([])]
        with (patch.object(inventory, "all_live_accounts", side_effect=scan),
              patch.object(stop, "bounded_stop", return_value=[]), redirect_stdout(self.output)):
            with self.assertRaisesRegex(launch_permission.LaunchAdmissionRefused, "shutdown in progress"):
                _ = settle.down_all()
        # The process-owned fence releases on an exception; later restores are allowed.
        with launch_permission.admitted_launch(previous["login"], "late-restore", "late-restore", lambda: None):
            self.assertEqual(len(launch_permission.pending_launches()), 1)

    def test_down_all_defers_requester_until_other_accounts_have_stopped(self) -> None:
        caller = top_level("caller", 101)
        peer = machine_inventory([top_level("other", 202)])
        peer["login"], peer["label"] = "other@example.com", "claude4"
        events: list[str] = []
        def stopped(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            events.extend(saved["session"]["session_id"] for saved in records)
            self.assertNotIn("caller", events)
            return records
        def defer(saved: shutdown_record.SessionShutdown) -> None:
            events.append("defer " + saved["session"]["session_id"])
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "caller"}),
              patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([caller]), peer]),
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", side_effect=stopped),
              patch.object(stop, "defer_requester", side_effect=defer), redirect_stdout(self.output)):
            _ = settle.down_all()
        self.assertEqual(events, ["other", "defer caller"])

    def test_down_all_uses_the_open_restore_request_to_stop_its_current_process_without_archiving_it(self) -> None:
        old = self.save(top_level("restoring", 101))
        def restarting(saved: shutdown_record.SessionShutdown) -> None:
            saved["progress"] = {"kind": "restarting", "at": old["requested_at"],
                                 "prior": {"kind": "stopped", "at": old["requested_at"]}}
        _ = shutdown_record.update_session(old["login"], "restoring", restarting)
        selected: list[shutdown_record.SessionShutdown] = []
        def stop_current(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            selected.extend(records)
            return records
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([top_level("restoring", 202)])]),
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", side_effect=stop_current), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 1)
        self.assertEqual([saved["session"]["pid"] for saved in selected], [202])
        retained = shutdown_record.find_session(old["login"], "restoring")
        self.assertIsNotNone(retained)
        if retained is not None:
            self.assertEqual(retained["progress"]["kind"], "waiting")
            self.assertEqual(retained["session"]["pid"], 202)
            self.assertEqual(retained["request_id"], old["request_id"])
        self.assertIn("stopping current process under its existing shutdown request", self.output.getvalue())

    def test_down_all_stops_a_new_live_process_without_archiving_its_open_restore_request(self) -> None:
        old = self.save(top_level("restoring", 101))
        def restarting(current: shutdown_record.SessionShutdown) -> None:
            current["target"] = "claude2"
            current["progress"] = {"kind": "restarting", "at": old["requested_at"],
                "prior": {"kind": "stopped", "at": old["requested_at"]}}
        previous = shutdown_record.update_session(old["login"], "restoring", restarting)
        current = top_level("restoring", 202)
        current["cwd"] = "/current-worktree"
        self.process(202, started=current["proc_start"])
        self.natural_exits[202] = 3
        bounded_stop = stop.bounded_stop
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            return bounded_stop(records, started=0, machine_wide=machine_wide, keep=keep, request_exit=request_exit, monotonic=lambda: self.elapsed, sleep=self.advance,
                clock=lambda: NOW_UTC + timedelta(seconds=self.elapsed),
                observe=self.observe, identity=self.probe,
                kill=self.kill, codex_mesh=lambda arguments, _budget: self.pause_workers(arguments))
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([current])]),
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", side_effect=bounded), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        stopped = shutdown_record.find_session(old["login"], "restoring")
        self.assertIsNotNone(stopped)
        if stopped is not None:
            self.assertEqual(stopped["request_id"], previous["request_id"])
            self.assertEqual(stopped["requested_at"], previous["requested_at"])
            self.assertEqual(stopped.get("target"), "claude2")
            self.assertEqual(stopped["session"], current)
            self.assertEqual(stopped["progress"]["kind"], "stopped")
        self.assertEqual(list((self.root / "state" / old["login"] / "history").glob("*.json")), [])

    def test_down_all_is_explicitly_local_even_when_a_session_env_is_set(self) -> None:
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "caller"}),
              patch.object(settle, "down_all", return_value=0) as down):
            self.assertEqual(shutdown.main(["down", "--all"]), 0)
        down.assert_called_once_with()

    def test_down_all_refreshes_current_checkout_and_production_doc_on_every_live_account(self) -> None:
        first = showrunner("runner", 101)
        old = self.save(first)
        def finished(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "stopped", "at": old["requested_at"]}
        _ = shutdown_record.update_session(old["login"], "runner", finished)
        current = copy.deepcopy(first)
        current["cwd"], current["doc"] = "/current/runner", "/current/production.md"
        current["checkout"] = inventory.GitCheckout(kind="git", path="/current/runner",
            head=inventory.OnBranch(kind="branch", name="current-branch"),
            upstream=inventory.NoUpstream(kind="no upstream"), dirty=["draft.md"])
        first_report = machine_inventory([current])
        second_report = machine_inventory([unit("unit", 201), top_level("top", 202)])
        second_report["login"], second_report["label"] = "other@example.com", "claude4"
        selected: list[shutdown_record.SessionShutdown] = []
        def stopped(records: list[shutdown_record.SessionShutdown], *, started: float, machine_wide: bool = False, keep: tuple[inventory.Session, ...] = (),
                            request_exit: Callable[[shutdown_record.SessionShutdown], None] = lambda _saved: None) -> list[shutdown_record.SessionShutdown]:
            _ = machine_wide, keep, request_exit
            del started
            selected.extend(copy.deepcopy(records))
            for saved in records:
                _ = shutdown_record.update_session(saved["login"], saved["session"]["session_id"], finished)
                saved["progress"] = {"kind": "stopped", "at": old["requested_at"]}
            return records
        with (patch.object(inventory, "all_live_accounts", return_value=[first_report, second_report]),
              patch.object(settle, "_send_settle_message"),
              patch.object(stop, "bounded_stop", side_effect=stopped), redirect_stdout(self.output)):
            self.assertEqual(settle.down_all(), 0)
        self.assertEqual({saved["session"]["name"] for saved in selected}, {"runner", "unit", "top"})
        self.assertEqual({saved["login"] for saved in selected}, {old["login"], "other@example.com"})
        refreshed = next(saved["session"] for saved in selected if saved["session"]["session_id"] == "runner")
        self.assertEqual(refreshed["cwd"], "/current/runner")
        self.assertEqual(refreshed["checkout"], current["checkout"])
        self.assertEqual(refreshed["host"], current["host"])
        self.assertEqual(refreshed.get("doc"), "/current/production.md")

    def test_down_all_stops_saved_workers_after_the_director_leaves_inventory(self) -> None:
        for account_visible in (True, False):
            with self.subTest(account_visible=account_visible):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                worker = worker_session("child", 201, owner="owner")
                saved = shutdown_record.update_session(saved["login"], "owner", lambda current: current.update(workers=[worker]))
                self.processes = {}
                self.process(201, started=worker["proc_start"])
                self.stubborn.add(201)
                real_bounded = stop.bounded_stop
                def bounded(records: list[shutdown_record.SessionShutdown], **_options: object) -> list[shutdown_record.SessionShutdown]:
                    with patch.object(stop, "bounded_stop", real_bounded):
                        return self.run_stop(records, grace=0)
                reports = [machine_inventory([worker])] if account_visible else []
                with (patch.object(inventory, "all_live_accounts", return_value=reports),
                      patch.object(settle, "run_inventory", return_value=machine_inventory([])),
                      patch.object(stop, "bounded_stop", side_effect=bounded),
                      patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), redirect_stdout(self.output)):
                    self.assertEqual(settle.down_all(), 0, self.output.getvalue())
                self.assertEqual({pid for _, pid, _ in self.signals}, {201})
                self.assertGreaterEqual(self.elapsed, 5)
                self.assertNotIn(201, self.processes)
                final = shutdown_record.find_session(saved["login"], "owner")
                assert final is not None
                self.assertEqual(final["progress"]["kind"], "stopped")

    def pane_stop_record(self, *, director_gone: bool = False, failed_reason: str = "") -> shutdown_record.SessionShutdown:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        saved = self.save(director)
        scope = "/user/app.slice/app-agents.slice/pane.scope"
        self.process(70, started="5")
        self.process(80, 70, started="10")
        self.process(101, 80, started="100")
        self.processes = self.linux_table({70: "/system.slice/panes.service", 80: scope, 101: scope}, set()).processes.copy()
        def retain(current: shutdown_record.SessionShutdown) -> None:
            current["owner_pane_scopes"] = [{"pid": 101, "proc_start": "100", "cgroups": [scope]}]
            if failed_reason:
                current["progress"] = {"kind": "stop failed", "at": NOW_UTC.isoformat(), "reason": failed_reason}
        saved = shutdown_record.update_session(saved["login"], "owner", retain)
        self.stubborn.update({70, 80})
        if director_gone:
            del self.processes[101]
        return saved

    def test_interactive_pane_ancestors_are_kept_when_director_exits_on_request(self) -> None:
        saved = self.pane_stop_record()
        def exits(_saved: shutdown_record.SessionShutdown) -> None:
            del self.processes[101]
        result = self.run_stop([saved], request_exit=exits)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertEqual(set(self.processes), {70, 80})
        self.assertLess(self.elapsed, 1, "an exited director must not wait out the stop deadline")

    def test_retained_pane_scope_keeps_older_root_when_director_is_already_gone(self) -> None:
        saved = self.pane_stop_record(director_gone=True)
        result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertEqual(set(self.processes), {70, 80})
        self.assertLess(self.elapsed, 1)

    def test_keeping_pane_ancestors_still_stops_a_child_in_the_same_scope(self) -> None:
        saved = self.pane_stop_record()
        self.process(301, 101, started="110")
        groups = {pid: next(iter(process.cgroups), "/user/app.slice/app-agents.slice/pane.scope")
                  for pid, process in self.processes.items()}
        self.processes = self.linux_table(groups, set()).processes.copy()
        self.natural_exits[101] = 0.2
        result = self.run_stop([saved], grace=0)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {101, 301})
        self.assertEqual(set(self.processes), {70, 80})
        self.assertLess(self.elapsed, 1)

    def test_recorded_workers_owned_launcher_is_not_a_host_ancestor(self) -> None:
        saved = self.pane_stop_record()
        worker = worker_session("worker", 401, owner="owner")
        worker["proc_start"] = "120"
        saved = shutdown_record.update_session(saved["login"], "owner",
            lambda current: current.update(workers=[worker]))
        self.process(301, 101, started="110", session_id="owner")
        self.process(401, 301, started="120", session_id="worker")
        result = self.run_stop([saved], grace=0)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {101, 301, 401})
        self.assertEqual(set(self.processes), {70, 80})

    def test_gone_directors_run_marked_launcher_is_owned_before_ancestor_exemptions(self) -> None:
        saved = self.pane_stop_record(director_gone=True)
        run = str(self.root / "run")
        worker = worker_session("worker", 401, owner="owner")
        worker["proc_start"] = "120"
        def retain(current: shutdown_record.SessionShutdown) -> None:
            current["session"]["run_dirs"] = [run]
            current["workers"] = [worker]
        saved = shutdown_record.update_session(saved["login"], "owner", retain)
        self.process(301, 80, started="110", run_dir=run)
        self.process(401, 301, started="120", session_id="worker")
        with patch.object(self, "pause_workers", return_value=0):
            result = self.run_stop([saved], grace=0)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {301, 401})
        self.assertEqual(set(self.processes), {70, 80})

    def test_resumed_session_between_admission_and_first_capture_is_never_adopted(self) -> None:
        self.check_resumed_session_is_never_adopted()

    def test_other_account_resume_blocks_the_old_accounts_stop_retry(self) -> None:
        self.check_resumed_session_is_never_adopted("receiving@example.com", before_capture=False)

    def test_other_account_resume_before_first_capture_is_never_adopted(self) -> None:
        self.check_resumed_session_is_never_adopted("receiving@example.com")

    def test_socketless_other_account_resume_between_admission_and_capture_is_never_adopted(self) -> None:
        self.check_resumed_session_is_never_adopted("receiving@example.com", socket_available=False)

    def test_resumed_generation_exiting_after_capture_does_not_authorize_its_children(self) -> None:
        self.check_resumed_session_is_never_adopted("receiving@example.com",
            socket_available=False, exits_before_probe=True)

    def test_unreadable_generation_evidence_refuses_adoption_and_effects(self) -> None:
        for evidence in ("registry", "identity", "missing start"):
            with self.subTest(evidence=evidence):
                self.setUp()
                self.check_resumed_session_is_never_adopted("receiving@example.com",
                    socket_available=False, unreadable=evidence)

    def check_resumed_session_is_never_adopted(
        self, resumed_login: str | None = None, *, before_capture: bool = True,
        socket_available: bool = True, unreadable: str = "", exits_before_probe: bool = False,
    ) -> None:
        linux_process_fixture(self)
        saved = self.pane_stop_record(director_gone=True)
        run = str(self.root / "run")
        def failed(current: shutdown_record.SessionShutdown) -> None:
            current["session"]["run_dirs"] = [run]
            if resumed_login is not None:
                current["progress"] = {"kind": "stop failed", "at": NOW_UTC.isoformat(),
                                       "reason": "previous stop timed out"}
        saved = shutdown_record.update_session(saved["login"], "owner", failed)
        login = resumed_login or saved["login"]
        config = self.root / ".claude-receiving"
        config.mkdir()
        _ = (config / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))
        proc = self.root / "linux-proc"
        # pane_stop_record captured this old director before removing it.
        shutil.rmtree(proc / "101")
        socket_path = self.root / "resumed.sock"
        if socket_available:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
                endpoint.bind(str(socket_path))
        captured = False
        def publish() -> None:
            nonlocal captured
            self.process(501, 80, started="200", session_id="owner", run_dir=run)
            if not socket_available:
                self.process(502, 501, started="201", session_id="owner", run_dir=run)
            groups = {pid: next(iter(process.cgroups), "/user/app.slice/app-agents.slice/resumed.scope")
                      for pid, process in self.processes.items()}
            _ = self.linux_table(groups, set())
            for pid, process in self.processes.items():
                environment = {"CLAUDE_CODE_SESSION_ID": process.session_id, "UNIT_RUN_DIR": process.run_dir}
                if pid == 501:
                    environment["CLAUDE_CONFIG_DIR"] = str(config)
                _ = (proc / str(pid) / "environ").write_bytes(b"\0".join(
                    f"{key}={value}".encode() for key, value in environment.items()) + b"\0")
            _ = (self.root / "registry/501.json").write_text(json.dumps({
                "pid": 501, "procStart": "200", "sessionId": "owner", "name": "resumed",
                "messagingSocketPath": str(socket_path), "updatedAt": 1, "tmux": "",
                "cwd": str(self.root), "status": "idle", "kind": "interactive"}))
            if unreadable == "registry":
                _ = (self.root / "registry/501.json").write_text("{")
            elif unreadable == "missing start":
                registered = self.root / "registry/501.json"
                raw = cast(dict[str, object], json.loads(registered.read_text()))
                del raw["procStart"]
                _ = registered.write_text(json.dumps(raw))
            captured = True
        def capture(budget: float) -> inventory.ProcessObservation:
            if not captured:
                publish()
            return inventory.shutdown_processes(budget)
        def process_exists(pid: int, signum: int) -> None:
            self.assertEqual(signum, 0, "registry filtering must never send a real signal")
            if pid not in self.processes:
                raise ProcessLookupError(pid)
        def environment(pid: int) -> dict[str, str]:
            return inventory._snapshot_environment(proc / str(pid))  # pyright: ignore[reportPrivateUsage]
        probe = self.probe
        def identity(pid: int, budget: float) -> inventory.ProcessIdentity:
            if exits_before_probe and pid == 501:
                _ = self.processes.pop(pid, None)
            if unreadable == "identity" and pid == 501:
                return inventory.ProcessUnknown("fixture identity unreadable")
            return probe(pid, budget)
        read_bytes = Path.read_bytes
        def kernel_bytes(path: Path) -> bytes:
            if path.parent.parent == Path("/proc"):
                path = proc / path.parent.name / path.name
            return read_bytes(path)
        with (patch.dict(os.environ, {"HOME": str(self.root), "HERDR_PROC": str(proc),
                                     "NOTIFIER_STATE_DIR": str(self.root / "notifier")}),
              patch.object(Path, "read_bytes", kernel_bytes),
              patch.object(stop, "_fresh_inventory", _REAL_FRESH_INVENTORY),
              patch.object(process_identity, "process_start", side_effect=stored_start_probe(lambda pid: self.identity(pid) or "")),
              patch.object(os, "kill", side_effect=process_exists),
              patch.object(inventory, "_process_environment", environment),
              patch.object(sessions, "read_registry", wraps=sessions.read_registry) as registry,
              patch.object(self, "observe", side_effect=capture),
              patch.object(self, "probe", side_effect=identity),
              patch.object(self, "pause_workers") as paused):
            if not before_capture:
                publish()
            result = self.run_stop([saved], grace=0)
            self.assertEqual(self.signals, [])
            self.assertEqual(len(sessions.live_sessions()), int(socket_available))
            self.assertEqual([item.login for item in inventory.listing()], [login] if socket_available else [])
            if not socket_available:
                self.assertEqual(inventory.inventory(saved["login"], refresh_desktops=False)["sessions"], [])
                self.assertEqual(inventory.inventory(login, refresh_desktops=False)["sessions"], [])
            self.assertGreater(registry.call_count, 1)
        self.assertNotEqual(result[0]["progress"]["kind"], "stopped")
        self.assertIn("session identity unreadable" if unreadable else "session identity lost",
                      str(result[0]["progress"]))
        self.assertEqual(shutdown_record.find_session(saved["login"], "owner"), saved)
        expected_pids = {70, 80, 501} if socket_available else {70, 80, 501, 502}
        if exits_before_probe:
            expected_pids.remove(501)
        self.assertEqual(set(self.processes), expected_pids)
        paused.assert_not_called()

    def test_expired_automatic_retry_holds_a_resumed_session_before_scheduling(self) -> None:
        saved = self.pane_stop_record(director_gone=True)
        saved["stop_deadline"] = (NOW_UTC - timedelta(seconds=10)).isoformat()
        for progress in (shutdown_record.SessionWaiting(kind="waiting"),
                         shutdown_record.SessionStopping(kind="stopping", at=NOW_UTC.isoformat())):
            with self.subTest(progress=progress["kind"]):
                saved["progress"] = progress
                self.assertIsInstance(settle.next_step(saved,
                    stop.SessionIdentityLost(kind="session identity lost"), [], NOW_UTC), settle.Hold)

    def test_bounded_stop_admission_rechecks_a_resumed_session_after_scheduling(self) -> None:
        saved = self.pane_stop_record(director_gone=True)
        resumed = copy.deepcopy(saved["session"])
        resumed.update(pid=501, proc_start="200")
        self.process(501, 80, started="200", session_id="owner")
        original = stop.bounded_stop
        def bounded(records: list[shutdown_record.SessionShutdown], *, started: float,
                    grace_seconds: float) -> list[shutdown_record.SessionShutdown]:
            del started
            return original(records, started=0, grace_seconds=grace_seconds,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=self.observe, identity=self.probe, kill=self.kill)
        with (patch.object(stop, "_fresh_inventory", return_value=machine_inventory([resumed])),
              patch.object(stop, "bounded_stop", side_effect=bounded), redirect_stdout(self.output)):
            result = settle._stop_after_telling(saved, settle.StopSession())  # pyright: ignore[reportPrivateUsage]
        self.assertNotEqual(result["progress"]["kind"], "stopped")
        self.assertEqual(shutdown_record.find_session(saved["login"], "owner"), saved)
        self.assertEqual(self.signals, [])
        self.assertEqual(set(self.processes), {70, 80, 501})

    def test_discovery_and_retained_scopes_use_the_shared_cgroup_reader(self) -> None:
        saved = self.pane_stop_record()
        scope = "/user/app.slice/app-agents.slice/shared-reader.scope"
        def membership(_process: Path) -> list[tuple[str, Path]]:
            return [("0", Path(scope))]
        with (patch.object(herdr, "systemd_cgroups", side_effect=membership, create=True) as reader,
              patch.dict(os.environ, {"HERDR_PROC": str(self.root / "linux-proc")}),
              patch.object(stop, "shutdown_identity", return_value=inventory.ProcessLive("100")),
              patch.object(inventory, "_snapshot_environment", return_value={})):
            observation = inventory.shutdown_processes()
            scopes = stop.owner_pane_scopes(saved["session"], [])
            groups = herdr._systemd_cgroups(self.root / "linux-proc/101")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(observation.processes[101].cgroups, frozenset({scope}))
        self.assertEqual(scopes, [{"pid": 101, "proc_start": "100", "cgroups": [scope]}])
        self.assertEqual(groups, [("0", Path(scope))])
        self.assertEqual(reader.call_count, len(observation.processes) + 2)

    def test_failed_stop_total_reports_selected_count_and_every_failure_reason(self) -> None:
        saved = self.save(top_level("owner", 101))
        def refuses(_pid: int, _signum: int) -> None:
            raise PermissionError("fixture signal refused")
        with patch.object(self, "kill", side_effect=refuses):
            result = self.run_stop([saved], grace=0)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        text = self.output.getvalue()
        self.assertNotIn("all 0 stopped", text)
        self.assertIn("0 of 1 stopped", text)
        self.assertIn("owner: ", text)
        progress = result[0]["progress"]
        assert progress["kind"] == "stop failed"
        self.assertIn(progress["reason"], text)

    def test_now_reruns_failed_stop_when_only_its_interactive_pane_ancestors_remain(self) -> None:
        saved = self.pane_stop_record(director_gone=True, failed_reason="unresolved pids: 80/10")
        bounded = stop.bounded_stop
        def retry(records: list[shutdown_record.SessionShutdown], **_options: object) -> list[shutdown_record.SessionShutdown]:
            with patch.object(stop, "bounded_stop", bounded):
                return self.run_stop(records, grace=0)
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([])),
              patch.object(settle, "unrecorded_local_sessions", return_value={}),
              patch.object(settle, "_launch_conductor") as conductor,
              patch.object(stop, "bounded_stop", side_effect=retry), redirect_stdout(self.output)):
            self.assertEqual(settle.now(Account("claude", saved["login"], saved["label"]), True, ("owner",)), 0)
        conductor.assert_not_called()
        current = shutdown_record.find_session(saved["login"], "owner")
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "stopped")
        self.assertEqual(self.signals, [])
        self.assertEqual(set(self.processes), {70, 80})
        self.assertLess(self.elapsed, 1)

    def test_cancel_failed_stop_with_resumed_session_archives_without_signals(self) -> None:
        saved = self.pane_stop_record()
        observe = self.observe
        def unresolved(budget: float) -> inventory.ProcessObservation:
            return inventory.ProcessObservation(observe(budget).processes, ("fixture incomplete stop",))
        def exits(_saved: shutdown_record.SessionShutdown) -> None:
            del self.processes[101]
        with patch.object(self, "observe", side_effect=unresolved):
            failed = self.run_stop([saved], grace=0, request_exit=exits)
        self.assertEqual(failed[0]["progress"]["kind"], "stop failed")
        self.assertNotIn(101, self.processes)
        # Cancellation starts from a published failure. An exhausted attempt
        # may return that failure without budget left to publish it itself.
        saved = shutdown_record.update_session(saved["login"], "owner",
            lambda current: current.update(progress=failed[0]["progress"]))
        resumed = copy.deepcopy(saved["session"])
        resumed.update(pid=501, proc_start="200", name="resumed under another title")
        self.process(501, 80, started="200")
        with (patch.object(settle, "inventory", return_value=machine_inventory([resumed])) as observed,
              patch.object(settle, "_restore_record"), patch.object(os, "kill") as signal_sent,
              redirect_stdout(self.output)):
            self.assertEqual(settle.cancel(Account("claude", saved["login"], saved["label"]), True, ("owner",)), 0)
        observed.assert_called_once_with(saved["login"], frozenset({"owner"}))
        signal_sent.assert_not_called()
        self.assertIn("owner: taken out, still running", self.output.getvalue())
        self.assertIsNone(shutdown_record.find_session(saved["login"], "owner"))
        history = list((self.root / "state").glob("**/history/*.json"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text())["progress"]["kind"], "stop failed")
        self.assertIn(501, self.processes)
        self.assertEqual(self.signals, [])

    def test_cancel_failed_stop_without_live_session_keeps_record_and_names_finishing_command(self) -> None:
        saved = self.pane_stop_record(director_gone=True, failed_reason="old deadline")
        with (patch.object(settle, "inventory", return_value=machine_inventory([])),
              patch.object(settle, "_restore_record") as restore, redirect_stdout(self.output)):
            self.assertEqual(settle.cancel(Account("claude", saved["login"], saved["label"]), True, ("owner",)), 1)
        restore.assert_not_called()
        self.assertEqual(shutdown_record.find_session(saved["login"], "owner"), saved)
        self.assertIn(settle.shutdown_command("now", saved["login"], "--here", "--only", "owner"), self.output.getvalue())
        self.assertIn("finishes the stop", self.output.getvalue())
        self.assertEqual(self.signals, [])

    def test_now_does_not_adopt_new_process_of_failed_requests_session(self) -> None:
        saved = self.pane_stop_record(director_gone=True, failed_reason="old deadline")
        resumed = copy.deepcopy(saved["session"])
        resumed.update(pid=501, proc_start="200")
        self.process(501, 80, started="200")
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([resumed])),
              patch.object(settle, "unrecorded_local_sessions", return_value={}),
              patch.object(stop, "bounded_stop") as bounded, patch.object(os, "kill") as signal_sent,
              redirect_stdout(self.output)):
            self.assertEqual(settle.now(Account("claude", saved["login"], saved["label"]), True, ("owner",)), 1)
        bounded.assert_not_called()
        signal_sent.assert_not_called()
        self.assertEqual(shutdown_record.find_session(saved["login"], "owner"), saved)
        self.assertIn("another process; retained its record", self.output.getvalue())
        self.assertIn(501, self.processes)

    def test_detached_child_in_a_new_scope_requires_identity_backed_ancestry(self) -> None:
        for discovery in ("ancestry", "after first scan", "before first scan"):
            with self.subTest(discovery=discovery):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                scope = "/user/app.slice/app-agents.slice/run-owner.scope"
                other_scope = "/user/app.slice/app-agents.slice/run-detached.scope"
                self.processes[101] = inventory.ShutdownProcess(101, 1, saved["session"]["proc_start"], cgroups=frozenset({scope}))
                if discovery == "ancestry":
                    self.processes[301] = inventory.ShutdownProcess(301, 101, "child-start", cgroups=frozenset({scope}))
                if discovery == "before first scan":
                    self.processes[301] = inventory.ShutdownProcess(301, 900, "child-start", cgroups=frozenset({other_scope}))
                self.natural_exits[101] = 3
                moved = False
                def observe(budget: float) -> inventory.ProcessObservation:
                    nonlocal moved
                    if self.elapsed >= 3 and not moved:
                        moved = True
                        self.processes[301] = inventory.ShutdownProcess(301, 900, "child-start", cgroups=frozenset({other_scope}))
                    return self.observe(budget)
                with redirect_stdout(self.output):
                    result = stop.bounded_stop([saved], started=0, monotonic=lambda: self.elapsed,
                        sleep=self.advance, clock=lambda: NOW_UTC, observe=observe, identity=self.probe, kill=self.kill)
                owned = discovery == "ancestry"
                self.assertEqual(result[0]["progress"]["kind"], "stopped" if owned else "stop failed")
                self.assertEqual({pid for _, pid, _ in self.signals}, {301} if owned else set())
                if not owned:
                    self.assertIn("301", str(result[0]["progress"]))
                    self.assertIn(301, self.processes)

    def test_linux_pidfd_is_opened_before_identity_check_and_binds_the_signal(self) -> None:
        for reuse in ("before check", "after check", "none"):
            with self.subTest(reuse=reuse):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                events: list[str] = []
                kill = self.kill
                opened = False
                def open_pidfd(pid: int, flags: int = 0) -> int:
                    nonlocal opened
                    self.assertEqual((pid, flags), (101, 0))
                    opened = True
                    events.append("open")
                    if reuse == "before check":
                        self.process(101, started="replacement")
                    return 999
                def probe(pid: int, budget: float) -> inventory.ProcessIdentity:
                    checked = self.probe(pid, budget)
                    if opened and pid == 101:
                        events.append("check")
                        if reuse == "after check":
                            self.process(101, started="replacement")
                    return checked
                def send(descriptor: int, signum: int) -> None:
                    self.assertEqual((descriptor, signum), (999, signal.SIGTERM))
                    events.append("send")
                    if reuse == "none":
                        _ = self.processes.pop(101)
                real_close = os.close
                def close(descriptor: int) -> None:
                    if descriptor == 999:
                        events.append("close")
                    else:
                        real_close(descriptor)
                with (patch.object(sys, "platform", "linux"),
                      patch.object(stop, "_SYSTEM_KILL", kill, create=True),
                      patch.object(os, "pidfd_open", side_effect=open_pidfd, create=True),
                      patch.object(os, "close", side_effect=close),
                      patch.object(signal, "pidfd_send_signal", side_effect=send, create=True),
                      redirect_stdout(self.output)):
                    result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                        monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                        observe=self.observe, identity=probe, kill=kill)
                self.assertEqual(events, ["open", "check", "close"] if reuse == "before check" else ["open", "check", "send", "close"])
                self.assertEqual(self.signals, [])
                self.assertEqual(result[0]["progress"]["kind"], "stopped")
                if reuse != "none":
                    self.assertEqual(self.processes[101].started, "replacement")

    def test_pause_retains_scopes_when_every_recorded_process_is_gone_before_stop(self) -> None:
        director = top_level("owner", 101)
        director["proc_start"] = "100"
        worker = worker_session("child", 201, owner="owner")
        worker["proc_start"] = "105"
        self.process(101, started="100")
        self.process(201, started="105")
        scope = "/user/app.slice/app-agents.slice/run-owner.scope"
        _ = self.linux_table({101: scope, 201: scope}, set())
        with (patch.dict(os.environ, {"HERDR_PROC": str(self.root / "linux-proc")}), redirect_stdout(self.output)):
            added = settle.begin("owner@example.com", request_id="scopes", report=machine_inventory([director, worker]))
        self.assertEqual(len(added), 1)
        saved = shutdown_record.find_session("owner@example.com", "owner")
        assert saved is not None
        self.processes = {
            301: inventory.ShutdownProcess(301, 900, "110", cgroups=frozenset({scope})),
            302: inventory.ShutdownProcess(302, 900, "90", cgroups=frozenset({"/user/app.slice/app-agents.slice/run-idle.scope"})),
        }
        self.stubborn.add(301)
        result = self.run_stop([saved], grace=0)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertEqual({pid for _, pid, _ in self.signals}, {301})
        self.assertGreaterEqual(self.elapsed, 5)
        self.assertEqual(set(self.processes), {302})

    def test_saved_scopes_partition_orphans_after_all_directors_exit(self) -> None:
        records: list[shutdown_record.SessionShutdown] = []
        for pid in (101, 201):
            saved = self.save(top_level(f"owner-{pid}", pid))
            scopes = [shutdown_record.OwnerPaneScope(pid=pid, proc_start=saved["session"]["proc_start"],
                cgroups=[f"/user/app.slice/app-agents.slice/run-{pid}.scope"])]
            saved = shutdown_record.update_session(saved["login"], saved["session"]["session_id"],
                lambda current: current.update(owner_pane_scopes=scopes))
            records.append(saved)
        self.processes = {pid: inventory.ShutdownProcess(pid, 900, f"child-{pid}",
            cgroups=frozenset({f"/user/app.slice/app-agents.slice/run-{pid - 200}.scope"})) for pid in (301, 401)}
        result = self.run_stop(records, grace=0)
        self.assertEqual([saved["progress"]["kind"] for saved in result], ["stopped", "stopped"])
        self.assertEqual({pid for _, pid, _ in self.signals}, {301, 401})
        self.assertEqual(self.processes, {})

    def test_old_record_without_scope_evidence_cannot_authorize_an_orphan(self) -> None:
        saved = self.save(top_level("owner", 101))
        worker = worker_session("child", 201, owner="owner")
        saved = shutdown_record.update_session(saved["login"], "owner", lambda current: current.update(workers=[worker]))
        self.processes = {301: inventory.ShutdownProcess(301, 900, "child-start",
            cgroups=frozenset({"/user/app.slice/app-agents.slice/run-lost.scope"}))}
        result = self.run_stop([saved], grace=0)
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("301", str(result[0]["progress"]))
        self.assertEqual(self.signals, [])

    def test_pidfd_permission_errors_do_not_fall_back_to_pid_signals(self) -> None:
        for error in (PermissionError(1, "denied"), OSError(38, "unsupported")):
            with self.subTest(error=error):
                self.setUp()
                saved = self.save(top_level("owner", 101))
                kill = self.kill
                with (patch.object(sys, "platform", "linux"),
                      patch.object(stop, "_SYSTEM_KILL", kill),
                      patch.object(os, "pidfd_open", side_effect=error),
                      patch.object(signal, "pidfd_send_signal") as send,
                      redirect_stdout(self.output)):
                    result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                        monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                        observe=self.observe, identity=self.probe, kill=kill)
                send.assert_not_called()
                denied = isinstance(error, PermissionError)
                self.assertEqual(result[0]["progress"]["kind"], "stop failed" if denied else "stopped")
                self.assertEqual({pid for _, pid, _ in self.signals}, set() if denied else {101})

    def test_shared_session_configuration_is_read_once_per_stop_scan(self) -> None:
        saved = self.save(top_level("owner", 101))
        self.processes[900] = inventory.ShutdownProcess(900, 1, "server-start",
            cgroups=frozenset({"/user/app.slice/herdr-agents.service"}))
        for pid in range(301, 321):
            self.processes[pid] = inventory.ShutdownProcess(pid, 1, "90",
                cgroups=frozenset({"/user/background.slice/desktop.service"}))
        config = self.root / "config" / "herdr" / "shared-session"
        config.parent.mkdir(parents=True)
        _ = config.write_text("agents\n")
        reads = 0
        scans = 0
        original_read = Path.read_text
        def read(path: Path, *_args: object, **_kwargs: object) -> str:
            nonlocal reads
            if path == config:
                reads += 1
            return original_read(path)  # These fixture files are UTF-8.
        def observe(budget: float) -> inventory.ProcessObservation:
            nonlocal scans
            scans += 1
            return self.observe(budget)
        with (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config.parent.parent)}),
              patch.object(Path, "read_text", read),
              patch.object(inventory, "shutdown_server_identities", return_value=frozenset({(900, "server-start")})),
              redirect_stdout(self.output)):
            result = stop.bounded_stop([saved], started=0, grace_seconds=0,
                monotonic=lambda: self.elapsed, sleep=self.advance, clock=lambda: NOW_UTC,
                observe=observe, identity=self.probe, kill=self.kill)
        self.assertEqual(result[0]["progress"]["kind"], "stopped")
        self.assertGreater(scans, 1)
        self.assertEqual(reads, scans)


if __name__ == "__main__":
    _ = unittest.main()
