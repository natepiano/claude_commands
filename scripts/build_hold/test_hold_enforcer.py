"""Enforcement with temporary process, cgroup, holder and build log roots."""

from __future__ import annotations

import io
import json
import os
import signal
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast, override
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ..lib import process_start
    from ..lib.process_start_test_support import write_stat
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start
    from scripts.lib.process_start_test_support import write_stat

import build_hold
import hold_enforcer

if TYPE_CHECKING:
    from ..direct import step_record
else:
    import step_record


EMPTY_PROCESS_ENVIRONMENT: Mapping[str, str] = MappingProxyType({})


class HoldEnforcerTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    holders: Path = Path()
    uid: int = 0
    now: float = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc, self.holders = self.root / "proc", self.root / "holders"
        self.proc.mkdir()
        self.holders.mkdir()
        self.uid, self.now = os.getuid(), 0
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        _ = boot.write_text("test-boot\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "BUILD_HOLD_DIR": str(self.holders),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "BUILD_HOLD_ENFORCER_DIR": str(self.root / "enforcer"),
            "VERIFY_STEPS_DIR": str(self.root / "steps"),
            "VERIFY_PROC_ROOT": str(self.proc),
            "BUILD_HOLD_CGROUP_ROOT": str(self.root / "cgroup"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "",
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "active"),
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
        }))
        self.write_holder()
        # Registering a stopped session must use this temporary cycle.
        with build_hold.release_lock():
            cycle = build_hold.open_cycle(datetime.fromtimestamp(100, timezone.utc))
            cycle["holders"]["holder"] = {"since": datetime.fromtimestamp(100, timezone.utc).isoformat(), "released_at": ""}
            build_hold.save_cycle(cycle)

    def write_holder(self, keep: tuple[build_hold.ProcessIdentity, ...] = ()) -> None:
        _ = (self.holders / "holder").write_text(json.dumps({
            "holder": "builder", "since": datetime.fromtimestamp(100, timezone.utc).isoformat(),
            "for": "timed test", "release_eta": "unknown",
            "keep": [{"pid": item.pid, "start_time": item.start_time} for item in keep],
        }))

    def process(self, pid: int, command: str, *, parent: int = 1, start: int = 11_000,
                argv: tuple[str, ...] = (), session: str = "", tty: int = 0,
                environment: Mapping[str, str] = EMPTY_PROCESS_ENVIRONMENT, executable: str = "") -> Path:
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        _ = write_stat(directory.parent, pid, command=command, ppid=parent, tty_nr=tty, start_ticks=start)
        _ = (directory / "comm").write_text(command + "\n")
        _ = (directory / "cmdline").write_bytes(b"\0".join(argument.encode() for argument in (argv or (command,))) + b"\0")
        values = dict(environment)
        if session:
            values["CLAUDE_CODE_SESSION_ID"] = session
        _ = (directory / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in values.items()))
        for name, target in (("cwd", str(self.root)), ("exe", executable or f"/tools/{command}")):
            path = directory / name
            path.unlink(missing_ok=True)
            path.symlink_to(target)
        (directory / "fd").mkdir(exist_ok=True)
        _ = (directory / "fd/2").write_text("")
        return directory

    def table(self) -> dict[int, build_hold.AuditProcess]:
        return hold_enforcer.read_processes(self.proc, self.uid, self.root / "cgroup")

    def decisions(self, enforcer_pid: int = 999) -> tuple[hold_enforcer.BuildDecision, ...]:
        return hold_enforcer.decide_builds(self.table(), build_hold.natedev_holds(self.holders).in_force,
                                          self.uid, enforcer_pid)

    def rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def write_step(self, call_id: str, state: step_record.StepState, pid: int) -> None:
        step_record.write(step_record.StepRecord(
            call_id, pid, cast(process_start.ProcessStart, process_start.process_start(pid, self.proc)).text, self.root, "check", None,
            step_record.MacAllowed(), state, "2026-10-28T12:00:00+00:00",
        ))

    def test_signals_require_a_matching_owner_and_keep_unknown_identity(self) -> None:
        identity = build_hold.ProcessIdentity(77, "123")
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)))
        stat = write_stat(self.proc, 77, start_ticks=123)
        read_stat = process_start.read_stat
        with mock.patch.object(process_start, "read_stat", side_effect=process_start.ProcessReadError("denied")):
            self.assertFalse(stopper.signal_if_current(identity, signal.SIGTERM))
        _ = write_stat(self.proc, 77, start_ticks=456)
        self.assertFalse(stopper.signal_if_current(identity, signal.SIGTERM))
        stat.unlink()
        self.assertFalse(stopper.signal_if_current(identity, signal.SIGTERM))
        _ = write_stat(self.proc, 77, start_ticks=123)
        with mock.patch.object(process_start, "read_stat", wraps=read_stat):
            self.assertTrue(stopper.signal_if_current(identity, signal.SIGTERM))
        self.assertEqual(sent, [(77, signal.SIGTERM)])

    def test_verify_controller_survives_every_live_state_while_local_compile_child_stops(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other")
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(20, "cargo", parent=10, argv=("cargo", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        for state in step_record.StepState:
            with self.subTest(state=state):
                self.write_step("verify-call", state, 10)
                decisions = {item.process.identity.pid: item for item in self.decisions()}
                self.assertIsInstance(decisions[10], hold_enforcer.AllowedBuild)
                expected = (hold_enforcer.WouldStopBuild if state == step_record.StepState.ON_NATEDEV
                            else hold_enforcer.AllowedBuild)
                self.assertIsInstance(decisions[20], expected)

    def test_call_record_decides_unknown_commands_and_cargo_without_name_rules(self) -> None:
        _ = self.process(90, "coordinator")
        for pid, (command, argv) in enumerate((("worker", ("worker", "compile")),
                                             ("cargo", ("cargo", "check"))), 10):
            _ = self.process(pid, command, argv=argv, session="other",
                             environment={"BUILDLOG_CALL_ID": f"call-{pid}"})
        for state in step_record.StepState:
            for pid in (10, 11):
                self.write_step(f"call-{pid}", state, 90)
            table = self.table()
            decisions = hold_enforcer.decide_builds(table, build_hold.natedev_holds(self.holders).in_force,
                                                    self.uid, 999)
            self.assertEqual({item.process.identity.pid for item in decisions}, {10, 11})
            sent: list[int] = []
            stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
            for decision in decisions:
                if isinstance(decision, hold_enforcer.WouldStopBuild):
                    stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999)
            self.assertEqual(sent, [10, 11] if state == step_record.StepState.ON_NATEDEV else [])
        self.assertEqual({(row["pid"], row["call_id"], row["action"]) for row in self.rows()},
                         {(10, "call-10", "stopped"), (11, "call-11", "stopped")})

    def test_missing_stale_and_unreadable_call_records_stop_a_verify_process(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        for condition in ("missing", "stale", "unreadable"):
            with self.subTest(condition=condition):
                if condition == "stale":
                    self.write_step("verify-call", step_record.StepState.ON_MAC, 10)
                    _ = self.process(10, "bash", start=12_000, argv=("bash", "verify.sh", "check"), session="other",
                                     environment={"BUILDLOG_CALL_ID": "verify-call"})
                elif condition == "unreadable":
                    _ = step_record.record_path("verify-call").write_text("{broken")
                table = self.table()
                decision, = self.decisions()
                self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
                sent: list[int] = []
                if isinstance(decision, hold_enforcer.WouldStopBuild):
                    stopper = hold_enforcer.StopBuildsAndTests(
                        self.proc, signal_process=lambda pid, _signum: sent.append(pid))
                    stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999)
                self.assertEqual(sent, [10])

    def test_reroute_keeps_kill_for_old_compiler_and_protects_new_mac_children(self) -> None:
        _ = self.process(90, "coordinator")
        _ = self.process(10, "cargo", argv=("cargo", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(11, "worker", parent=10, start=11_001, session="other",
                         environment={"BUILDLOG_CALL_ID": "mac-call"})
        self.write_step("verify-call", step_record.StepState.ON_NATEDEV, 90)
        self.write_step("mac-call", step_record.StepState.ON_MAC, 90)
        table = self.table()
        decision = next(item for item in self.decisions() if item.process.identity.pid == 10)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)), clock=lambda: self.now)
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999)
        self.write_step("verify-call", step_record.StepState.WAITING_MAC, 90)
        _ = self.process(12, "worker", parent=90, start=11_002, session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        self.now = 3
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(sent, [(10, signal.SIGTERM), (10, signal.SIGKILL)])
        self.assertEqual(stopper.terminating, {})
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertIsInstance(decisions[12], hold_enforcer.AllowedBuild)

    def test_sole_holder_keeps_its_on_natedev_record_process(self) -> None:
        _ = self.process(90, "coordinator")
        _ = self.process(10, "worker", session="holder", environment={"BUILDLOG_CALL_ID": "holder-call"})
        self.write_step("holder-call", step_record.StepState.ON_NATEDEV, 90)
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        self.assertEqual(decision.owner, build_hold.OwningSession("holder"))

    def test_stopped_run_writes_reason_and_log_registers_notice_then_terms_root_first_and_kills_survivor(self) -> None:
        root = self.process(30, "cargo", argv=("cargo", "check", "--workspace"), session="other")
        _ = self.process(20, "rustc", parent=30, start=11_001)
        _ = self.process(10, "linker", parent=20, start=11_002)
        decision = next(item for item in self.decisions() if item.process.identity.pid == 30)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []

        def send(pid: int, signum: int) -> None:
            self.assertEqual((root / "fd/2").read_text(),
                             "natedev builds and tests are held by builder for timed test; this run was stopped. "
                             + "You get a notice at the release; run it again then.\n")
            self.assertEqual(self.rows()[0]["action"], "stopped")
            cycle = build_hold.read_cycle()
            assert cycle is not None
            self.assertIn("other", cycle["recipients"])
            sent.append((pid, signum))
            if signum == signal.SIGTERM and pid != 30:
                (self.proc / str(pid) / "stat").unlink()

        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=send, clock=lambda: self.now)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(sent, [(30, signal.SIGTERM), (20, signal.SIGTERM), (10, signal.SIGTERM)])
        row, = self.rows()
        self.assertEqual((row["pid"], row["proc_start"], row["session"], row["command"], row["hold_key"], row["cwd"]),
                         (30, "11000", "other", "cargo check --workspace", "holder", str(self.root)))
        self.now = 1.99
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(len(sent), 3)
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(sent[-1], (30, signal.SIGKILL))
        self.assertEqual(len(sent), 4)

    def test_codex_mac_idle_cargo_and_claude_unittest_and_reparented_mesh_test_are_stopped(self) -> None:
        _ = self.process(10, "codex", start=100, environment={"CODEX_THREAD_ID": "codex-worker"})
        _ = self.process(20, "python3", parent=10, argv=("python3", "-m", "unittest", "scripts.direct.test_mac_idle"))
        cargo = self.process(21, "cargo", parent=20, start=11_001, argv=("/nix/store/rust/bin/cargo", "check", "--workspace"))
        (cargo / "cwd").unlink()
        (cargo / "cwd").symlink_to("/dev/shm/mac-idle-suite-123/tmp456")
        _ = self.process(30, "claude", start=100, session="claude-session")
        _ = self.process(31, "python3", parent=30, argv=("python3", "-m", "unittest", "scripts.agents.test_codex_mesh"))
        _ = self.process(40, "python3", argv=("python3", "-m", "unittest", "scripts.production.test_merge_gate"),
                         environment={"CLAUDE_CODE_SESSION_ID": "detached-session"})
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertEqual(set(decisions), {20, 21, 31, 40})
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in decisions.values()))
        self.assertEqual(decisions[21].owner, build_hold.OwningSession("codex-worker"))
        self.assertEqual(decisions[21].process.cwd, "/dev/shm/mac-idle-suite-123/tmp456")
        self.assertEqual(decisions[40].owner, build_hold.OwningSession("detached-session"))

    def test_waiting_merge_gate_and_verify_survive_while_their_build_children_stop(self) -> None:
        _ = self.process(30, "python3", argv=("python3", "merge_gate.py", "--checkout", "/tree", "--tests-only"), session="gate")
        _ = self.process(20, "python3", parent=30, start=11_001, argv=("python3", "-m", "unittest", "suite"))
        _ = self.process(10, "rustc", parent=20, start=11_002)
        _ = self.process(21, "basedpyright", parent=30, start=11_001, argv=("basedpyright", "src"))
        _ = self.process(40, "bash", argv=("bash", "verify.sh", "test"), session="verify")
        _ = self.process(41, "cargo", parent=40, start=11_001,
                         argv=("/home/user/.rustup/toolchains/stable/bin/cargo", "test"))
        sent: list[int] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
        decisions = self.decisions()
        for item in decisions:
            if isinstance(item, hold_enforcer.WouldStopBuild):
                stopper.stop(item, self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(sent, [20, 10, 21, 41])
        self.assertIsInstance(next(item for item in decisions if item.process.identity.pid == 30), hold_enforcer.AllowedBuild)
        self.assertIsInstance(next(item for item in decisions if item.process.identity.pid == 40), hold_enforcer.AllowedBuild)
        self.assertEqual({row["pid"] for row in self.rows()}, {20, 21, 41})
        self.assertTrue(all(row["action"] == "stopped" for row in self.rows()))

    def test_merge_gate_entry_points_wait_but_mentions_do_not_protect_tests(self) -> None:
        for argv in (
            ("/tree/merge_gate.py", "--tests-only"),
            ("python3", "-u", "/tree/merge_gate.py", "--promote-only"),
            ("python3", "-m", "scripts.production.merge_gate", "--tests-only"),
        ):
            with self.subTest(argv=argv):
                _ = self.process(30, "python3", argv=argv, session="other")
                decision, = self.decisions()
                self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        for argv in (
            ("python3", "-m", "unittest", "merge_gate.py"),
            ("python3", "/tree/test_driver.py", "merge_gate.py"),
        ):
            with self.subTest(argv=argv):
                _ = self.process(30, "python3", argv=argv, session="other")
                decision, = self.decisions()
                self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)

    def test_old_nextest_stops_keep_covers_children_and_reused_keep_pid_does_not(self) -> None:
        _ = self.process(10, "cargo-nextest", start=1000, session="other")
        _ = self.process(20, "rustc", parent=10, start=2000)
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in self.decisions()))
        self.write_holder((build_hold.ProcessIdentity(10, "1000"),))
        self.assertTrue(all(isinstance(item, hold_enforcer.KeptBuild) for item in self.decisions()))
        _ = self.process(10, "cargo-nextest", start=1500, session="other")
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in self.decisions()))

    def test_session_owned_headless_rust_analyzer_stops_after_twelve_hour_parent(self) -> None:
        _ = self.process(10, "python3", start=100, argv=("python3", "scripts/style/bulk_rename.py", "--all"), session="rename")
        _ = self.process(20, "rust-analyzer", parent=10)
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
        self.assertEqual(decision.owner, build_hold.OwningSession("rename"))
        self.assertEqual(decision.process.identity.pid, 20)

    def test_holder_user_terminal_desktop_and_shared_server_pass_but_service_and_marked_terminal_stop(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "test"), session="holder")
        _ = self.process(20, "bash", tty=22)
        _ = self.process(21, "cargo", parent=20, argv=("cargo", "test"))
        _ = self.process(30, "sccache", argv=("sccache", "--server"))
        _ = self.process(31, "rustc", parent=30)
        _ = self.process(40, "sccache", environment={"SCCACHE_START_SERVER": "1"})
        _ = self.process(41, "rustc", parent=40)
        _ = self.process(50, "basedpyright", argv=("/venv/bin/basedpyright", "src"), session="agent")
        _ = self.process(60, "zed", start=100)
        _ = self.process(61, "basedpyright-langserver", parent=60)
        _ = self.process(62, "cargo", parent=60, argv=("cargo", "check"))
        _ = self.process(70, "cargo", argv=("cargo", "build"))
        _ = self.process(80, "cargo", tty=33, argv=("cargo", "build"), environment={"CODEX_THREAD_ID": "detached"})
        app = self.root / f"cgroup/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice"
        desktop = app / "app-zed@one.service"
        desktop.mkdir(parents=True)
        _ = (desktop / "cgroup.procs").write_text("60\n61\n")
        service = app / "run-daemon.service"
        service.mkdir()
        _ = (service / "cgroup.procs").write_text("70\n")
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        for pid in (10, 21, 31, 41, 62):
            self.assertIsInstance(decisions[pid], hold_enforcer.AllowedBuild)
        for pid in (50, 70, 80):
            self.assertIsInstance(decisions[pid], hold_enforcer.WouldStopBuild)
        self.assertNotIn(61, decisions)

    def test_all_test_entry_points_and_executable_path_match_without_treating_lookup_or_language_server_as_builds(self) -> None:
        cases = (
            ("python3", ("python3", "-m", "pytest")), ("py.test", ("/venv/bin/py.test",)),
            ("python3", ("python3", "/tree/test_driver.py")),
            ("bash", ("bash", "-e", "/tree/test_build.sh")), ("clippy-driver", ("clippy-driver",)),
            ("node", ("node", "/tools/basedpyright.js", "src")),
        )
        expected: set[int] = set()
        for pid, (command, argv) in enumerate(cases, 10):
            _ = self.process(pid, command, argv=argv)
            expected.add(pid)
        _ = self.process(30, "hidden", argv=("renamed", "check"), executable="/toolchain/bin/cargo")
        expected.add(30)
        _ = self.process(40, "cargo", argv=("cargo", "+nightly", "--config", "build", "metadata"))
        _ = self.process(41, "python3", argv=("python3", "helper.py", "-m", "unittest"))
        _ = self.process(42, "python3", argv=("python3", "-c", "print('pytest')"))
        _ = self.process(43, "basedpyright-langserver")
        _ = self.process(44, "rust-analyzer")
        _ = self.process(45, "python3", argv=("python3", "/tree/driver_test.py"))
        decisions = self.decisions()
        self.assertEqual({item.process.identity.pid for item in decisions}, expected)
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in decisions))

    def test_hold_stops_natedev_tests_but_spares_mac_coordination(self) -> None:
        for pid, verb in enumerate(("watch-claim", "free-now", "claim", "release"), 10):
            _ = self.process(pid, "python3", session="other", argv=(
                "python3", "-u", "/tree/scripts/mac_test/mac_test.py", verb,
            ))
        _ = self.process(20, "python3", session="other", argv=(
            "python3", "-m", "scripts.mac_test.mac_test", "watch-claim",
        ))
        _ = self.process(21, "mac_test.py", session="other", executable="/tools/python3", argv=(
            "/tree/scripts/mac_test/mac_test.py", "release",
        ))
        _ = self.process(30, "python3", session="other", argv=("python3", "-m", "unittest", "suite"))
        _ = self.process(31, "cargo", session="other", argv=("cargo", "nextest", "run"))
        table = self.table()
        holders = build_hold.natedev_holds(self.holders).in_force
        decisions = hold_enforcer.decide_builds(table, holders, self.uid, 999)
        self.assertEqual({item.process.identity.pid for item in decisions}, {30, 31})
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
        )
        for decision in decisions:
            self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
            if isinstance(decision, hold_enforcer.WouldStopBuild):
                stopper.stop(decision, table, holders, 999)
        self.assertEqual(sent, [(30, signal.SIGTERM), (31, signal.SIGTERM)])
        self.assertEqual([row["pid"] for row in self.rows()], [30, 31])

    def test_verify_version_and_metadata_probes_are_not_builds_during_hold(self) -> None:
        _ = self.process(5, "bash", session="verify", argv=("bash", "verify.sh", "test"))
        probes = (
            ("rustc", "-vV"), ("rustc", "--version"), ("rustc", "-V"),
            ("rustc", "+nightly", "-vV"), ("cargo", "--version"),
            ("cargo", "metadata"), ("cargo", "+nightly", "metadata", "--format-version", "1"),
        )
        for pid, argv in enumerate(probes, 10):
            _ = self.process(pid, argv[0], parent=5, argv=argv)
        _ = self.process(30, "rustc", parent=5, argv=("rustc", "/tree/lib.rs"))
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertEqual(set(decisions), {5, 30})
        self.assertIsInstance(decisions[5], hold_enforcer.AllowedBuild)
        self.assertIsInstance(decisions[30], hold_enforcer.WouldStopBuild)

    def test_reader_checks_other_environments_but_limits_arguments_to_candidates_and_records(self) -> None:
        _ = self.process(10, "claude", start=1000, session="other")
        _ = self.process(20, "cargo", parent=10, argv=("cargo", "build"))
        _ = self.process(30, "cargo-port")
        read_bytes = Path.read_bytes
        detailed: set[tuple[int, str]] = set()

        def read(path: Path) -> bytes:
            if path.name != "stat":
                detailed.add((int(path.parent.name), path.name))
            return read_bytes(path)

        with mock.patch.object(Path, "read_bytes", read):
            table = self.table()
        self.assertEqual(detailed, {(10, "cmdline"), (10, "environ"), (20, "cmdline"),
                                    (20, "environ"), (30, "environ")})
        self.assertEqual(table[30].environment, {})

    def test_other_uid_and_enforcer_ancestors_are_never_stopped(self) -> None:
        _ = self.process(10, "python3", argv=("python3", "-m", "unittest"))
        _ = self.process(20, "python3", parent=10, argv=("python3", "hold_enforcer.py", "run"), start=11_001)
        _ = self.process(30, "cargo", argv=("cargo", "build"))
        table = self.table()
        table[30] = replace(table[30], uid=self.uid + 1)
        decisions = hold_enforcer.decide_builds(table, build_hold.natedev_holds(self.holders).in_force, self.uid, 20)
        decision, = decisions
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        self.assertEqual(decision.process.identity.pid, 10)

    def test_pid_reuse_before_term_or_kill_gets_no_signal(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"))
        decision, = self.decisions()
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        table = self.table()
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
                                                 clock=lambda: self.now)
        _ = self.process(10, "cargo", start=12_000, argv=("cargo", "build"))
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual((sent, self.rows()), ([], []))
        decision, = self.decisions()
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        _ = self.process(10, "cargo", start=13_000, argv=("cargo", "build"))
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(sent, [(10, signal.SIGTERM)])

    def test_keep_added_during_term_grace_prevents_kill_of_root_and_children(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"))
        _ = self.process(20, "rustc", parent=10, start=11_001)
        decision = next(item for item in self.decisions() if item.process.identity.pid == 10)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
                                                 clock=lambda: self.now)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.write_holder((decision.process.identity,))
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999)
        self.assertEqual(sent, [(10, signal.SIGTERM), (20, signal.SIGTERM)])

    def test_run_stops_descendants_once_then_exits_on_file_removal_without_reading_another_table(self) -> None:
        _ = self.process(30, "cargo", argv=("cargo", "check"))
        _ = self.process(20, "rustc", parent=30, start=11_001)
        sent: list[int] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))

        def release(_seconds: float) -> None:
            (self.holders / "holder").unlink()

        def run() -> int:
            return hold_enforcer.run(self.proc, self.holders, self.root / "enforcer", uid=self.uid,
                                     enforcer_pid=999, stopper=stopper, sleep=release, cgroup_root=self.root / "cgroup")

        self.assertEqual(run(), 0)
        self.assertEqual(sent, [30, 20])
        self.assertEqual([row["pid"] for row in self.rows()], [30])
        with mock.patch.object(build_hold, "read_audit_processes", side_effect=AssertionError("empty hold reads no table")):
            self.assertEqual(run(), 0)

    def test_rehold_keep_added_after_table_read_is_checked_before_any_signal(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "check"))
        table = self.table()
        sent: list[int] = []

        def changed_hold(_root: Path, _selection: build_hold.HoldBuildCandidates, _cgroups: Path) -> dict[int, build_hold.AuditProcess]:
            self.write_holder((table[10].identity,))
            return table

        def release(_seconds: float) -> None:
            (self.holders / "holder").unlink()

        with mock.patch.object(build_hold, "read_audit_processes", side_effect=changed_hold) as read:
            result = hold_enforcer.run(self.proc, self.holders, self.root / "enforcer", uid=self.uid, enforcer_pid=999,
                stopper=hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid)),
                sleep=release, cgroup_root=self.root / "cgroup")
        self.assertEqual(result, 0)
        read.assert_called_once()
        self.assertEqual((sent, self.rows()), ([], []))

    def test_scan_reports_stop_allowed_and_kept_without_changes_or_signals(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "check"), start=9000)
        _ = self.process(20, "cargo", argv=("cargo", "build"), session="holder")
        _ = self.process(30, "python3", argv=("python3", "-m", "unittest"))
        self.write_holder((build_hold.ProcessIdentity(10, "9000"),))
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with mock.patch.object(os, "kill", side_effect=AssertionError("scan sends no signals")), redirect_stdout(output):
            hold_enforcer.scan(self.proc, self.holders, self.uid, 999, self.root / "cgroup")
        text = output.getvalue()
        self.assertIn("kept: no session, pid 10", text)
        self.assertIn("allowed: holder, pid 20", text)
        self.assertIn("would stop: no session, pid 30", text)
        self.assertIn("--keep 30", text)
        self.assertRegex(text, r"pass: [0-9.]+ s; 3 processes")
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_audit_and_stopped_rows_share_writer_and_deduplicate_each_action(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"), session="other")
        decision, = self.decisions()
        build = build_hold.AuditedBuild(decision.process, decision.owner, "other_session", ())
        holder, = build_hold.natedev_holds(self.holders).in_force
        actions: tuple[Literal["audit", "stopped"], ...] = ("audit", "stopped", "audit", "stopped")
        for action in actions:
            hold_enforcer.record_build(build, holder, self.proc, action)
        self.assertEqual([row["action"] for row in self.rows()], ["audit", "stopped"])

    def test_start_keeps_active_unit_and_uses_absolute_paths_or_detached_fallback(self) -> None:
        with mock.patch.object(shutil, "which", return_value="/tools/systemctl"), \
             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "active\n", "")) as run, \
             mock.patch.object(subprocess, "Popen") as launch:
            hold_enforcer.start()
            self.assertEqual(run.call_count, 1)
            launch.assert_not_called()
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "test-bus"}), \
             mock.patch.object(shutil, "which", return_value="/tools/tool"), \
             mock.patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess([], 3, "inactive\n", ""),
                                                              subprocess.CompletedProcess([], 0, "", "")]) as run:
            hold_enforcer.start()
            command = cast(list[str], run.call_args.args[0])
            self.assertIn("--unit=build-hold-enforcer", command)
            self.assertEqual(command[-1], "run")
            self.assertTrue(Path(command[-2]).is_absolute())
            self.assertTrue(Path(command[-3]).is_absolute())
        with mock.patch.object(shutil, "which", return_value=None), \
             mock.patch.object(subprocess, "Popen") as launch:
            hold_enforcer.start()
            self.assertTrue(launch.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    _ = unittest.main()
