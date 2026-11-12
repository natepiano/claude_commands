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
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast, override
from unittest import mock

from scripts.build_hold.owning_session import OwningSession
from scripts.build_hold import build_hold, hold_enforcer
from scripts.agents import detached_runs
from scripts.build_targets import target_config
from scripts.mac_test import claim_state
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production import staff

from scripts.build_hold.override_guard import require_checkout

require_checkout(Path(__file__))


from scripts.direct import step_record


EMPTY_PROCESS_ENVIRONMENT: Mapping[str, str] = MappingProxyType({})


class HoldEnforcerTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    holders: Path = Path()
    uid: int = 0
    now: float = 0
    claimed: frozenset[build_hold.ProcessIdentity] = frozenset()
    claim_reader: Callable[[Path], build_hold.LiveClaims | build_hold.ClaimsUnreadable] = staticmethod(hold_enforcer.claimed_processes)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc, self.holders = self.root / "proc", self.root / "holders"
        self.proc.mkdir()
        self.holders.mkdir()
        self.uid, self.now = os.getuid(), 0
        self.claimed = frozenset()
        self.claim_reader = hold_enforcer.claimed_processes
        _ = self.enterContext(mock.patch.object(hold_enforcer, "claimed_processes", side_effect=self.read_claims))
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
            "STAFF_FILE": str(self.root / "staff.json"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "",

            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
        }))
        self.write_holder()
        # Registering a stopped session must use this temporary cycle.
        with build_hold.release_lock():
            cycle = build_hold.open_cycle(datetime.fromtimestamp(100, timezone.utc))
            cycle["holders"]["holder"] = {"since": datetime.fromtimestamp(100, timezone.utc).isoformat(), "released_at": ""}
            build_hold.save_cycle(cycle)

    def read_claims(self, proc_root: Path) -> build_hold.LiveClaims:
        self.assertEqual(proc_root, self.proc)
        return build_hold.LiveClaims(self.claimed)

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
        fields = ["0"] * 20
        fields[0], fields[1], fields[4], fields[19] = "S", str(parent), str(tty), str(start)
        _ = (directory / "stat").write_text(f"{pid} ({command}) " + " ".join(fields))
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
                                          self.uid, enforcer_pid, self.claimed)

    def rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def write_step(self, call_id: str, state: step_record.StepState, pid: int) -> None:
        step_record.write(step_record.StepRecord(
            call_id, pid, step_record.process_start(pid), self.root, "check", step_record.WorkspacePackages(),
            step_record.AnyTarget(("mac", "natedev"), ("mac",)), state, "2026-10-28T12:00:00+00:00",
        ))

    def test_drain_stops_waiting_run_waits_for_working_run_and_releases_owner_once(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        member = fixture_member("producer", name="holder")
        _ = (self.holders / "holder").rename(self.holders / member.id)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="waiting-owner")
        _ = self.process(20, "bash", argv=("bash", "verify.sh", "test"), session="working-owner")
        _ = self.process(21, "cargo", parent=20, argv=("cargo", "test"))
        signals: list[tuple[int, int]] = []
        notices: list[str] = []
        pauses: list[float] = []
        stop = hold_enforcer.stop_waiting_run
        drain = hold_enforcer.drain

        def private_drain(holder: build_hold.Holder, proc_root: Path) -> int:
            self.assertIn(build_hold.ProcessIdentity(20, "11000"), holder.keep)
            self.assertNotIn(build_hold.ProcessIdentity(10, "11000"), holder.keep)
            return drain(holder, proc_root, sleep=advance, clock=lambda: self.now)

        def signal_process(pid: int, signum: int) -> None:
            self.assertTrue(build_hold.natedev_holds(self.holders).in_force)
            signals.append((pid, signum))

        def private_stop(run: hold_enforcer.WaitingRun, holder: build_hold.Holder,
                         processes: Mapping[int, build_hold.AuditProcess], proc_root: Path
                         ) -> hold_enforcer.StoppedRun | hold_enforcer.RunIdentityChanged:
            return stop(run, holder, processes, proc_root, signal_process=signal_process,
                        signal_group=lambda pid, signum: self.fail(f"unexpected group signal {pid} {signum}"))

        def advance(seconds: float) -> None:
            self.assertEqual(signals, [(10, signal.SIGTERM)])
            pauses.append(seconds)
            self.now += 60
            if len(pauses) == 2:
                shutil.rmtree(self.proc / "10")
                shutil.rmtree(self.proc / "21")
                shutil.rmtree(self.proc / "20")

        with mock.patch.object(hold_enforcer, "admitted_processes", return_value=frozenset()), \
             mock.patch.object(hold_enforcer, "claimed_processes", return_value=build_hold.LiveClaims(frozenset())), \
             mock.patch.object(hold_enforcer, "stop_waiting_run", side_effect=private_stop), \
             mock.patch.object(hold_enforcer, "drain", side_effect=private_drain), \
             mock.patch.object(hold_enforcer, "start"), \
             redirect_stdout(io.StringIO()) as output:
            status = build_hold.main(["hold", "--drain", "--holder", "holder", "--for", "timed test",
                                      "--proc-root", str(self.proc)])
        self.assertEqual(status, 0)
        self.assertEqual(signals, [(10, signal.SIGTERM)])
        self.assertGreaterEqual(len(pauses), 2)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[-1], "drained")
        self.assertGreaterEqual(sum("working-owner" in line and "20" in line for line in lines), 2)
        self.assertTrue(any("waiting-owner" in line and "10" in line and "age" in line for line in lines))
        self.assertRegex(output.getvalue(), r"age|[0-9]+s|[0-9]+ min")
        row, = [row for row in self.rows() if row["action"] == "stopped"]
        self.assertEqual(row["stopped_run"], {"pid": 10, "proc_start": "11000"})
        self.assertEqual(row["session"], "waiting-owner")

        def notify(entry: build_hold.ReleaseEntry, socket: str, _notice: str) -> build_hold.ReleaseDelivered:
            self.assertEqual(socket, "/private/socket")
            notices.append(entry["session_id"])
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/private/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=notify):
            _ = build_hold.release_cycle(self.holders, member.id)
            _ = build_hold.release_cycle(self.holders, member.id)
        self.assertEqual(notices, ["waiting-owner"])

        with self.subTest(claim_read="unreadable at hold start"):
            _ = self.process(30, "bash", argv=("bash", "verify.sh", "test"), session="working-owner")
            _ = self.process(31, "cargo", parent=30, start=11_001, argv=("cargo", "test"))
            _ = self.process(40, "python3", argv=("python3", "targeted_test.py"), session="waiting-owner")
            _ = self.process(41, "bash", parent=40, start=11_002, argv=("bash", "verify.sh", "check"))
            signals.clear()
            pauses.clear()
            reads = 0

            def recover_claims(proc_root: Path) -> build_hold.LiveClaims | build_hold.ClaimsUnreadable:
                nonlocal reads
                self.assertEqual(proc_root, self.proc)
                reads += 1
                if reads == 1:
                    return build_hold.ClaimsUnreadable("private claims are damaged")
                return build_hold.LiveClaims(frozenset())

            def finish_drain(seconds: float) -> None:
                self.assertEqual(signals, [(40, signal.SIGTERM), (41, signal.SIGTERM)])
                pauses.append(seconds)
                if len(pauses) == 1:
                    shutil.rmtree(self.proc / "41")
                    shutil.rmtree(self.proc / "40")
                else:
                    shutil.rmtree(self.proc / "31")
                    shutil.rmtree(self.proc / "30")

            def recovered_drain(holder: build_hold.Holder, proc_root: Path) -> int:
                self.assertCountEqual(holder.keep, (build_hold.ProcessIdentity(30, "11000"),
                                                   build_hold.ProcessIdentity(40, "11000")))
                path = self.holders / member.id
                saved = self.root / "saved-holder"

                def end_enforcer_pass(seconds: float) -> None:
                    self.assertEqual(seconds, 1)
                    self.assertEqual(signals, [])
                    _ = path.rename(saved)

                stopper = hold_enforcer.StopBuildsAndTests(proc_root, signal_process=signal_process)
                self.assertEqual(hold_enforcer.run(
                    proc_root, self.holders, self.root / "enforcer", uid=self.uid, enforcer_pid=999,
                    stopper=stopper, sleep=end_enforcer_pass, cgroup_root=self.root / "cgroup"), 0)
                _ = saved.rename(path)
                self.assertEqual(signals, [])
                return drain(holder, proc_root, sleep=finish_drain, clock=lambda: self.now)

            errors = io.StringIO()
            with mock.patch.object(hold_enforcer, "admitted_processes", return_value=frozenset()), \
                 mock.patch.object(hold_enforcer, "claimed_processes", side_effect=recover_claims), \
                 mock.patch.object(hold_enforcer, "stop_waiting_run", side_effect=private_stop), \
                 mock.patch.object(hold_enforcer, "drain", side_effect=recovered_drain), \
                 mock.patch.object(hold_enforcer, "start"), \
                 redirect_stdout(io.StringIO()) as output, redirect_stderr(errors):
                self.assertEqual(build_hold.main([
                    "hold", "--drain", "--holder", "holder", "--for", "timed test", "--proc-root", str(self.proc)]), 0)
            self.assertEqual(signals, [(40, signal.SIGTERM), (41, signal.SIGTERM)])
            self.assertEqual(output.getvalue().splitlines()[-1], "drained")
            self.assertEqual(errors.getvalue().splitlines(), ["claims unreadable: private claims are damaged"])
            self.assertGreater(reads, 1)

    def test_drain_classifies_gate_entry_points_and_work_evidence(self) -> None:
        entries = (("bash", "verify.sh", "check"), ("python3", "merge_gate.py", "--tests-only"),
                   ("python3", "targeted_test.py"))
        for argv in entries:
            with self.subTest(entry=argv):
                _ = self.process(10, argv[0], argv=argv, session="gate-owner")
                table = self.table()
                identity = table[10].identity
                errors = io.StringIO()
                with mock.patch.object(hold_enforcer, "claimed_processes",
                                       return_value=build_hold.ClaimsUnreadable("private claims are damaged")), \
                     mock.patch.object(hold_enforcer, "admitted_processes", return_value=frozenset()), \
                     redirect_stderr(errors):
                    self.assertEqual(hold_enforcer.working_drain_identities(self.proc), (identity,))
                self.assertEqual(errors.getvalue().splitlines(), ["claims unreadable: private claims are damaged"])
                for admitted, claimed, expected in (
                    (frozenset[build_hold.ProcessIdentity](), frozenset[build_hold.ProcessIdentity](),
                     hold_enforcer.WaitingRun),
                    (frozenset({identity}), frozenset[build_hold.ProcessIdentity](), hold_enforcer.WorkingRun),
                    (frozenset[build_hold.ProcessIdentity](), frozenset({identity}), hold_enforcer.WorkingRun),
                ):
                    with self.subTest(admitted=admitted, claimed=claimed):
                        classified = hold_enforcer.classify_drain_runs(table, admitted, build_hold.LiveClaims(claimed))
                        assert not isinstance(classified, build_hold.ClaimsUnreadable)
                        run, = classified
                        self.assertIsInstance(run, expected)
                        self.assertEqual(run.owner, OwningSession("gate-owner"))
                _ = self.process(20, argv[0], parent=10, start=11_001, argv=argv)
                _ = self.process(21, "runner", parent=10, start=11_002)
                _ = self.process(30, "cargo", parent=21, start=11_003, argv=("cargo", "test"))
                try:
                    runs = hold_enforcer.classify_drain_runs(self.table(), frozenset(), build_hold.LiveClaims(frozenset()))
                    assert not isinstance(runs, build_hold.ClaimsUnreadable)
                    self.assertEqual([run.process.identity.pid for run in runs], [10])
                    self.assertTrue(all(isinstance(run, hold_enforcer.WorkingRun) for run in runs))
                finally:
                    for pid in (20, 21, 30):
                        shutil.rmtree(self.proc / str(pid))

    def test_drain_records_stop_and_owner_before_signalling_and_cancels_a_changed_identity(self) -> None:
        record = self.root / "detached-run.json"
        _ = self.process(100, "python3", start=10_000,
                         argv=("python3", "detached_runs.py", "supervise", str(record)))
        holder, = build_hold.natedev_holds(self.holders).in_force
        record_build = hold_enforcer.record_build
        for outcome in ("record failure", "identity change", "stopped"):
            with self.subTest(outcome=outcome):
                _ = record.write_text(json.dumps({"pid": 100, "status": "running"}))
                _ = self.process(10, "bash", parent=100, argv=("bash", "verify.sh", "check"), session="gate-owner")
                table = self.table()
                classified = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
                assert not isinstance(classified, build_hold.ClaimsUnreadable)
                run, = classified
                assert isinstance(run, hold_enforcer.WaitingRun)
                events: list[str] = []
                signals: list[tuple[int, int]] = []

                def save_stop(build: build_hold.AuditedBuild, holder: build_hold.Holder, proc_root: Path,
                              action: Literal["audit", "stopped"], *,
                              stopped_run: build_hold.ProcessIdentity | hold_enforcer.NoStoppedRun) -> None:
                    events.append("record")
                    if outcome == "record failure":
                        raise OSError("private stop record is full")
                    record_build(build, holder, proc_root, action, stopped_run=stopped_run)

                def save_reason(path: Path, text: str) -> None:
                    write_text_atomic(path, text)
                    if path == record and "stop_reason" in text and outcome == "identity change":
                        _ = self.process(10, "editor", start=12_000)

                def signal_group(pid: int, signum: int) -> None:
                    signals.append((pid, signum))
                    self.assertEqual(events, ["record"])
                    self.assertEqual([row["stopped_run"] for row in self.rows() if row["action"] == "stopped"],
                                     [{"pid": 10, "proc_start": "11000"}])
                    cycle = build_hold.read_cycle()
                    assert cycle is not None
                    self.assertIn("gate-owner", cycle["recipients"])

                with mock.patch.object(hold_enforcer, "record_build", side_effect=save_stop), \
                     mock.patch.object(detached_runs, "write_text_atomic", side_effect=save_reason):
                    if outcome == "record failure":
                        with self.assertRaisesRegex(OSError, "private stop record is full"):
                            _ = hold_enforcer.stop_waiting_run(
                                run, holder, table, self.proc, signal_group=signal_group,
                                signal_process=lambda pid, signum: self.fail(f"unexpected signal {pid} {signum}"),
                                supervisor_args=lambda pid, proc_root: ("python3", "detached_runs.py", "supervise", str(record)),
                            )
                    else:
                        result = hold_enforcer.stop_waiting_run(
                            run, holder, table, self.proc, signal_group=signal_group,
                            signal_process=lambda pid, signum: self.fail(f"unexpected signal {pid} {signum}"),
                            supervisor_args=lambda pid, proc_root: ("python3", "detached_runs.py", "supervise", str(record)),
                        )
                        expected = hold_enforcer.RunIdentityChanged if outcome == "identity change" else hold_enforcer.StoppedRun
                        self.assertIsInstance(result, expected)
                if outcome == "stopped":
                    self.assertEqual(signals, [(100, signal.SIGTERM)])
                else:
                    self.assertEqual(signals, [])
                    self.assertEqual([row for row in self.rows() if row["action"] == "stopped"], [])
                    saved = cast(dict[str, object], json.loads(record.read_text()))
                    self.assertNotIn("stop_reason", saved)

    def test_drain_skips_a_reused_pid_before_a_stop(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="gate-owner")
        table = self.table()
        classified = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
        assert not isinstance(classified, build_hold.ClaimsUnreadable)
        run, = classified
        assert isinstance(run, hold_enforcer.WaitingRun)
        _ = self.process(10, "editor", start=12_000)
        holder, = build_hold.natedev_holds(self.holders).in_force
        signals: list[tuple[int, int]] = []
        result = hold_enforcer.stop_waiting_run(
            run, holder, table, self.proc,
            signal_process=lambda pid, signum: signals.append((pid, signum)),
            signal_group=lambda pid, signum: signals.append((pid, signum)),
        )
        self.assertIsInstance(result, hold_enforcer.RunIdentityChanged)
        self.assertEqual(signals, [])
        self.assertEqual(self.rows(), [])

    def test_drain_interrupt_keeps_the_hold(self) -> None:
        _ = self.process(20, "bash", argv=("bash", "verify.sh", "test"), session="working-owner")
        _ = self.process(21, "cargo", parent=20, argv=("cargo", "test"))
        holder, = build_hold.natedev_holds(self.holders).in_force

        def interrupt(seconds: float) -> None:
            self.assertGreater(seconds, 0)
            raise KeyboardInterrupt

        with mock.patch.object(hold_enforcer, "admitted_processes", return_value=frozenset()), \
             mock.patch.object(hold_enforcer, "claimed_processes", return_value=build_hold.LiveClaims(frozenset())), \
             redirect_stdout(io.StringIO()) as output:
            status = hold_enforcer.drain(holder, self.proc, sleep=interrupt, clock=lambda: self.now)
        self.assertEqual(status, 130)
        self.assertTrue(build_hold.natedev_holds(self.holders).in_force)
        self.assertNotIn("drained", output.getvalue().splitlines())

        stop_waiting = hold_enforcer.stop_waiting_run
        for reading in ("unreadable", "claim after table"):
            with self.subTest(claim_read=reading):
                _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="waiting-owner")
                reads = 0
                pauses = 0
                signals: list[int] = []
                events: list[str] = []
                read_table = build_hold.read_audit_processes

                def table_before_claims(proc_root: Path) -> dict[int, build_hold.AuditProcess]:
                    table = read_table(proc_root)
                    events.append("table")
                    return table

                def read_after_table(proc_root: Path) -> build_hold.LiveClaims | build_hold.ClaimsUnreadable:
                    nonlocal reads
                    self.assertEqual(proc_root, self.proc)
                    events.append("claims")
                    self.assertEqual(events[-2:], ["table", "claims"])
                    reads += 1
                    if reading == "unreadable" and reads <= 2:
                        return build_hold.ClaimsUnreadable("private claims are damaged")
                    identities = frozenset({build_hold.ProcessIdentity(10, "11000")}) if reading == "claim after table" else frozenset[build_hold.ProcessIdentity]()
                    return build_hold.LiveClaims(identities)

                def private_stop(
                    waiting: hold_enforcer.WaitingRun, holder: build_hold.Holder,
                    processes: Mapping[int, build_hold.AuditProcess], proc_root: Path,
                ) -> hold_enforcer.StoppedRun | hold_enforcer.RunIdentityChanged:
                    def send(pid: int, signum: int) -> None:
                        self.assertEqual(signum, signal.SIGTERM)
                        signals.append(pid)

                    return stop_waiting(
                        waiting, holder, processes, proc_root, signal_process=send)

                def finish(seconds: float) -> None:
                    nonlocal pauses
                    self.assertEqual(seconds, 1)
                    pauses += 1
                    self.assertEqual(signals, [10] if reading == "unreadable" and pauses == 3 else [])
                    if pauses == (3 if reading == "unreadable" else 1):
                        raise KeyboardInterrupt

                errors = io.StringIO()
                with mock.patch.object(build_hold, "read_audit_processes", side_effect=table_before_claims), \
                     mock.patch.object(hold_enforcer, "claimed_processes", side_effect=read_after_table), \
                     mock.patch.object(hold_enforcer, "admitted_processes", return_value=frozenset()), \
                     mock.patch.object(hold_enforcer, "stop_waiting_run", side_effect=private_stop), \
                     redirect_stdout(io.StringIO()), redirect_stderr(errors):
                    self.assertEqual(hold_enforcer.drain(holder, self.proc, sleep=finish, clock=lambda: self.now), 130)
                self.assertEqual(errors.getvalue().splitlines(),
                                 ["claims unreadable: private claims are damaged"] if reading == "unreadable" else [])
                shutil.rmtree(self.proc / "10")

    def test_drain_checks_detached_supervisor_before_group_stop_and_records_reason(self) -> None:
        record = self.root / "detached-run.json"
        _ = self.process(100, "python3", start=10_000,
                         argv=("python3", "detached_runs.py", "supervise", str(record)))
        _ = self.process(10, "bash", parent=100, argv=("bash", "verify.sh", "check"), session="gate-owner")
        table = self.table()
        classified = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
        assert not isinstance(classified, build_hold.ClaimsUnreadable)
        run, = classified
        assert isinstance(run, hold_enforcer.WaitingRun)
        holder, = build_hold.natedev_holds(self.holders).in_force
        read_supervisor_args = hold_enforcer.supervisor_args
        for argument, current, prefix in ((str(record), False, ("python3",)), (str(record), True, ("python3",)),
                                           (record.name, True, ("python3",)),
                                           (str(record), False, ("python3", "unrelated.py"))):
            with self.subTest(record_argument=argument, supervisor_current=current, supervisor_prefix=prefix):
                _ = self.process(100, "python3", start=10_000,
                                 argv=("python3", "detached_runs.py", "supervise", argument))
                table = self.table()
                _ = record.write_text(json.dumps({"pid": 100, "status": "running"}))
                signals: list[tuple[int, int]] = []
                inspected: list[int] = []

                def supervisor_args(pid: int, proc_root: Path) -> tuple[str, ...]:
                    inspected.append(pid)
                    self.assertEqual(proc_root, self.proc)
                    argv = ((*prefix, "detached_runs.py", "supervise", argument)
                            if current or len(prefix) == 2 else ("editor", "notes.txt"))
                    _ = (proc_root / str(pid) / "cmdline").write_bytes(
                        b"\0".join(value.encode() for value in argv) + b"\0")
                    return read_supervisor_args(pid, proc_root)

                def signal_group(pid: int, signum: int) -> None:
                    saved = cast(dict[str, object], json.loads(record.read_text()))
                    self.assertEqual(saved["stop_reason"], "stopped for timed test; run again after the release")
                    signals.append((pid, signum))

                result = hold_enforcer.stop_waiting_run(
                    run, holder, table, self.proc, supervisor_args=supervisor_args,
                    signal_process=lambda pid, signum: self.fail(f"unexpected process signal {pid} {signum}"),
                    signal_group=signal_group,
                )
                self.assertIn(100, inspected)
                if current:
                    self.assertIsInstance(result, hold_enforcer.StoppedRun)
                    self.assertEqual(signals, [(100, signal.SIGTERM)])
                else:
                    self.assertIsInstance(result, hold_enforcer.RunIdentityChanged)
                    self.assertEqual(signals, [])

    def test_only_confirmed_hold_stops_are_consumed(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        consumed = self.root / "consumed-stops"
        for pid, (status, expected) in enumerate((("requested", False), ("not_stopped", False),
                                                 ("stopped", True), ("legacy", True)), 10):
            with self.subTest(stop_status=status):
                _ = self.process(pid, "cargo", session="gate-owner",
                                 environment={"BUILDLOG_CALL_ID": "drain-call"})
                process = self.table()[pid]
                build = build_hold.AuditedBuild(process, OwningSession("gate-owner"), "other_session", ())
                if status == "legacy":
                    hold_enforcer.record_build(build, holder, self.proc, "stopped")
                else:
                    hold_enforcer.record_build(build, holder, self.proc, "stopped", stopped_run=process.identity)
                    if status == "not_stopped":
                        hold_enforcer.set_stop_status(holder, process.identity, "not_stopped")
                    elif status == "stopped":
                        hold_enforcer.set_stop_status(holder, process.identity, "stopped")
                self.assertEqual(step_record.consume_stopped_build("drain-call", consumed), expected)

    def test_verify_controller_survives_every_live_state_while_local_compile_child_stops(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other")
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(20, "cargo", parent=10, argv=("cargo", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(21, "python3", parent=10, start=11_001,
                         argv=("python3", "offload.py", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        for state in step_record.StepState:
            with self.subTest(state=state):
                self.write_step("verify-call", state, 10)
                decisions = {item.process.identity.pid: item for item in self.decisions()}
                self.assertIsInstance(decisions[10], hold_enforcer.AllowedBuild)
                expected = (hold_enforcer.WouldStopBuild if state == step_record.StepState.ON_NATEDEV
                            else hold_enforcer.AllowedBuild)
                self.assertIsInstance(decisions[20], expected)
                self.assertIsInstance(decisions[21], expected)
                if state in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_MAC):
                    sent: list[int] = []
                    stopper = hold_enforcer.StopBuildsAndTests(
                        self.proc, signal_process=lambda pid, _signum: sent.append(pid))
                    for decision in decisions.values():
                        if isinstance(decision, hold_enforcer.WouldStopBuild):
                            stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force,
                                         999, self.claimed)
                    self.assertEqual(sent, [])

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
                                                    self.uid, 999, self.claimed)
            self.assertEqual({item.process.identity.pid for item in decisions}, {10, 11})
            sent: list[int] = []
            stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
            for decision in decisions:
                if isinstance(decision, hold_enforcer.WouldStopBuild):
                    stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
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
                    stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
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
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.write_step("verify-call", step_record.StepState.WAITING_MAC, 90)
        _ = self.process(12, "worker", parent=90, start=11_002, session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        self.now = 3
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
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
        self.assertEqual(decision.owner, OwningSession("holder"))
        member = staff.StaffRow(staff.StaffId("fixed-holder"), "producer", "renamed-holder", staff.StaffRoot(),
                                current_session=staff.StaffId("holder"))
        _ = (self.holders / "holder").rename(self.holders / member.id)
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,))):
            decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        self.assertEqual(decision.owner, OwningSession("holder"))

    def test_stopped_run_writes_reason_and_log_registers_notice_then_terms_root_first_and_kills_survivor(self) -> None:
        root = self.process(30, "cargo", argv=("cargo", "check", "--workspace"), session="other")
        _ = (root / "fd/2").write_text("TREE ok\nFMT ok\n")
        _ = self.process(20, "rustc", parent=30, start=11_001)
        _ = self.process(10, "linker", parent=20, start=11_002)
        decision = next(item for item in self.decisions() if item.process.identity.pid == 30)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []

        def send(pid: int, signum: int) -> None:
            self.assertEqual((root / "fd/2").read_text(),
                             "TREE ok\nFMT ok\nnatedev builds and tests are held by builder for timed test; this run was stopped. "
                             + "You get a notice at the release; run it again then.\n")
            self.assertEqual(self.rows()[0]["action"], "stopped")
            cycle = build_hold.read_cycle()
            assert cycle is not None
            self.assertIn("other", cycle["recipients"])
            sent.append((pid, signum))
            if signum == signal.SIGTERM and pid != 30:
                (self.proc / str(pid) / "stat").unlink()

        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=send, clock=lambda: self.now)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(30, signal.SIGTERM), (20, signal.SIGTERM), (10, signal.SIGTERM)])
        row, = self.rows()
        self.assertEqual((row["pid"], row["proc_start"], row["session"], row["command"], row["hold_key"], row["cwd"]),
                         (30, "11000", "other", "cargo check --workspace", "holder", str(self.root)))
        self.now = 1.99
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(len(sent), 3)
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent[-1], (30, signal.SIGKILL))
        self.assertEqual(len(sent), 4)

    def test_codex_mac_idle_cargo_and_claude_unittest_and_reparented_worker_test_are_stopped(self) -> None:
        _ = self.process(10, "codex", start=100, environment={"CODEX_THREAD_ID": "codex-worker"})
        _ = self.process(20, "python3", parent=10, argv=("python3", "-m", "unittest", "scripts.direct.test_mac_idle"))
        cargo = self.process(21, "cargo", parent=20, start=11_001, argv=("/nix/store/rust/bin/cargo", "check", "--workspace"))
        (cargo / "cwd").unlink()
        (cargo / "cwd").symlink_to("/dev/shm/mac-idle-suite-123/tmp456")
        _ = self.process(30, "claude", start=100, session="claude-session")
        _ = self.process(31, "python3", parent=30, argv=("python3", "-m", "unittest", "scripts.agents.codex_test_support"))
        _ = self.process(40, "python3", argv=("python3", "-m", "unittest", "scripts.production.test_merge_gate"),
                         environment={"CLAUDE_CODE_SESSION_ID": "detached-session"})
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertEqual(set(decisions), {20, 21, 31, 40})
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in decisions.values()))
        self.assertEqual(decisions[21].owner, OwningSession("codex-worker"))
        self.assertEqual(decisions[21].process.cwd, "/dev/shm/mac-idle-suite-123/tmp456")
        self.assertEqual(decisions[40].owner, OwningSession("detached-session"))

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
                stopper.stop(item, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
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
        self.assertEqual(decision.owner, OwningSession("rename"))
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
        decisions = hold_enforcer.decide_builds(table, holders, self.uid, 999, self.claimed)
        self.assertEqual({item.process.identity.pid for item in decisions}, {30, 31})
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
        )
        for decision in decisions:
            self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
            if isinstance(decision, hold_enforcer.WouldStopBuild):
                stopper.stop(decision, table, holders, 999, self.claimed)
        self.assertEqual(sent, [(30, signal.SIGTERM), (31, signal.SIGTERM)])
        self.assertEqual([row["pid"] for row in self.rows()], [30, 31])

        for condition in ("live", "stale record", "ended", "unrelated", "reused pid"):
            with self.subTest(claim=condition):
                step_record.record_path("claimed-call").unlink(missing_ok=True)
                _ = self.process(30, "python3", session="other", argv=("python3", "-m", "unittest", "suite"),
                                 environment={"BUILDLOG_CALL_ID": "claimed-call"})
                if condition == "stale record":
                    self.write_step("claimed-call", step_record.StepState.ON_MAC, 30)
                    _ = self.process(30, "python3", start=12_000, session="other",
                                     argv=("python3", "-m", "unittest", "suite"),
                                     environment={"BUILDLOG_CALL_ID": "claimed-call"})
                _ = self.process(32, "python3", parent=30, start=12_001,
                                 argv=("python3", "offload.py", "check"),
                                 environment={"BUILDLOG_CALL_ID": "claimed-call"})
                _ = self.process(33, "ssh", parent=32, start=12_002,
                                 environment={"BUILDLOG_CALL_ID": "claimed-call"})
                table = self.table()
                before = hold_enforcer.decide_builds(table, holders, self.uid, 999, frozenset())
                stopped = next(item for item in before if item.process.identity.pid == 30)
                assert isinstance(stopped, hold_enforcer.WouldStopBuild)
                claimed = table[32].identity
                if condition == "unrelated":
                    claimed = table[31].identity
                elif condition == "reused pid":
                    claimed = build_hold.ProcessIdentity(32, "12000")
                self.claimed = frozenset() if condition == "ended" else frozenset({claimed})
                decisions = {item.process.identity.pid: item for item in
                             hold_enforcer.decide_builds(table, holders, self.uid, 999, self.claimed)}
                allowed = condition in ("live", "stale record")
                for pid in (30, 32, 33):
                    self.assertIsInstance(decisions[pid], hold_enforcer.AllowedBuild if allowed
                                          else hold_enforcer.WouldStopBuild)
                    if allowed:
                        self.assertEqual(decisions[pid].reason, "holds a live Mac or AWS claim")
                sent = []
                stopper = hold_enforcer.StopBuildsAndTests(
                    self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)))
                stopper.stop(stopped, table, holders, 999, self.claimed)
                self.assertEqual(sent, [] if allowed else [(pid, signal.SIGTERM) for pid in (30, 32, 33)])

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
        decisions = hold_enforcer.decide_builds(table, build_hold.natedev_holds(self.holders).in_force, self.uid, 20, self.claimed)
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
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual((sent, self.rows()), ([], []))
        decision, = self.decisions()
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        _ = self.process(10, "cargo", start=13_000, argv=("cargo", "build"))
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM)])

    def test_keep_added_during_term_grace_prevents_kill_of_root_and_children(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"))
        _ = self.process(20, "rustc", parent=10, start=11_001)
        decision = next(item for item in self.decisions() if item.process.identity.pid == 10)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
                                                 clock=lambda: self.now)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.write_holder((decision.process.identity,))
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM), (20, signal.SIGTERM)])

        self.write_holder()
        _ = self.process(30, "worker", parent=20, start=11_002)
        table = self.table()
        sent.clear()
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)), clock=lambda: self.now)
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.claimed = frozenset({table[20].identity})
        self.now = 5
        stopper.kill_survivors(table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM), (20, signal.SIGTERM), (30, signal.SIGTERM)])
        self.assertEqual(stopper.terminating, {})

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

        self.write_holder()
        _ = self.process(40, "python3", parent=20, start=11_002, argv=("python3", "offload.py", "check"))
        self.claimed = frozenset({self.table()[40].identity})
        sent.clear()
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
        passes = 0

        def finish_claim(_seconds: float) -> None:
            nonlocal passes
            passes += 1
            if passes == 1:
                self.assertEqual(sent, [])
                self.claimed = frozenset()
            else:
                self.assertEqual(sent, [30, 20, 40])
                (self.holders / "holder").unlink()

        with mock.patch.object(hold_enforcer, "claimed_processes", side_effect=self.read_claims) as claims:
            self.assertEqual(hold_enforcer.run(
                self.proc, self.holders, self.root / "enforcer", uid=self.uid,
                enforcer_pid=999, stopper=stopper, sleep=finish_claim, cgroup_root=self.root / "cgroup"), 0)
        self.assertEqual(claims.call_args_list, [mock.call(self.proc), mock.call(self.proc)])
        self.assertEqual(passes, 2)

        for failure in ("claim file", "target config", "claim after table"):
            with self.subTest(claim_read=failure):
                self.write_holder()
                self.claimed = frozenset()
                paths = claim_state.StatePaths(self.root / "claims-state")
                paths.directory.mkdir(exist_ok=True)
                _ = paths.run.write_text("{broken")
                events: list[str] = []
                signals: list[tuple[int, int]] = []
                passes = 0
                stopper = hold_enforcer.StopBuildsAndTests(
                    self.proc, signal_process=lambda pid, signum: signals.append((pid, signum)), clock=lambda: 5)
                if failure != "claim after table":
                    stopper.terminating[build_hold.ProcessIdentity(20, "11001")] = 0
                read_table = build_hold.read_audit_processes

                def table_before_claims(
                    proc_root: Path, selection: build_hold.HoldBuildCandidates, cgroup_root: Path,
                ) -> dict[int, build_hold.AuditProcess]:
                    table = read_table(proc_root, selection, cgroup_root)
                    events.append("table")
                    if failure == "claim after table":
                        self.claimed = frozenset({table[40].identity})
                    return table

                def targets() -> tuple[target_config.Target, ...]:
                    if failure == "target config" and passes < 2:
                        raise target_config.ConfigError("private target config is damaged")
                    return (target_config.SshTarget("mac", "macos", self.root / "mac.conf"),)

                def read_after_table(proc_root: Path) -> build_hold.LiveClaims | build_hold.ClaimsUnreadable:
                    events.append("claims")
                    self.assertEqual(events[-2:], ["table", "claims"])
                    if failure == "claim after table":
                        return self.read_claims(proc_root)
                    return self.claim_reader(proc_root)

                def recover(seconds: float) -> None:
                    nonlocal passes
                    self.assertEqual(seconds, 1)
                    passes += 1
                    if failure == "claim after table":
                        self.assertEqual(signals, [])
                        (self.holders / "holder").unlink()
                    elif passes < 3:
                        self.assertEqual(signals, [])
                        if passes == 2:
                            paths.run.unlink()
                    else:
                        self.assertIn((20, signal.SIGKILL), signals)
                        self.assertIn((30, signal.SIGTERM), signals)
                        (self.holders / "holder").unlink()

                errors = io.StringIO()
                with mock.patch.object(build_hold, "read_audit_processes", side_effect=table_before_claims), \
                     mock.patch.object(hold_enforcer, "claimed_processes", side_effect=read_after_table), \
                     mock.patch.object(target_config, "read_targets", side_effect=targets), \
                     mock.patch.object(claim_state, "state_paths", return_value=paths), redirect_stderr(errors):
                    self.assertEqual(hold_enforcer.run(
                        self.proc, self.holders, self.root / "enforcer", uid=self.uid,
                        enforcer_pid=999, stopper=stopper, sleep=recover, cgroup_root=self.root / "cgroup"), 0)
                if failure == "claim after table":
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertEqual(len(errors.getvalue().splitlines()), 2)
                    self.assertIn("enforcement waits", errors.getvalue())
                    self.assertIn("claims readable; enforcement resumes", errors.getvalue())
                    self.assertIn(str(paths.run) if failure == "claim file" else "private target config", errors.getvalue())

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

        for reader in ("scan", "report"):
            with self.subTest(claim_reader=reader):
                output = io.StringIO()
                with mock.patch.object(hold_enforcer, "claimed_processes",
                                       return_value=build_hold.ClaimsUnreadable("private claims are damaged")), \
                     redirect_stdout(output):
                    if reader == "scan":
                        hold_enforcer.scan(self.proc, self.holders, self.uid, 999, self.root / "cgroup")
                        text = output.getvalue()
                    else:
                        text = "\n".join(hold_enforcer.report(self.proc, self.holders))
                self.assertEqual(text.count("claims unreadable: private claims are damaged"), 1)
                self.assertIn("would stop: no session, pid 30", text)

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
