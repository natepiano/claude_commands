"""Remote permission, interruption, and stop evidence protect AWS work."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
import time
from pathlib import Path
import signal
import subprocess
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import config as settings_config, ledger, remote
from scripts.aws_target.control_test_support import ControlFixture
from scripts.lib import conf, process_start as process_identity
from scripts.mac_test import linux_runner, mac_test, offload, claim_state


class AdmissionEnvelopeTests(ControlFixture):
    generation: str = "fixture-generation"

    def allowed_record(self) -> dict[str, object]:
        book = ledger.CostLedger(self.state)
        result = book.guard(self.settings, "admission", self.clock.now, lambda _event: True)
        self.assertIsInstance(result, ledger.SpendingAllowed)
        document = cast(dict[str, object], json.loads(book.path.read_text()))
        return {"version": 1, "target": "aws", "purpose": "admission", "generation": self.generation,
                "decision_at": self.clock.now, "expires_at": self.clock.now + 60,
                "decision": document["decision"]}

    def parse(self, record: object) -> object:
        return remote.parse_admission(record, target="aws", generation=self.generation, now=self.clock.now)

    def test_current_generation_permission_accepts_a_durable_allowed_decision(self) -> None:
        answer = self.parse(self.allowed_record())
        self.assertIsInstance(answer, remote.AdmissionEnvelope)
        assert isinstance(answer, remote.AdmissionEnvelope)
        self.assertIsInstance(answer.permission, ledger.PermissionAllowed)

    def test_envelope_rejects_each_wrong_identity_time_and_version_field(self) -> None:
        valid = self.allowed_record()
        changes: tuple[tuple[str, object], ...] = (
            ("version", 2), ("target", "spare"), ("purpose", "start"),
            ("generation", "older-generation"), ("decision_at", self.clock.now + 1),
            ("expires_at", self.clock.now), ("expires_at", "tomorrow"),
            ("decision", {"status": "allowed"}),
        )
        for key, value in changes:
            with self.subTest(field=key, value=value):
                self.assertIsInstance(self.parse({**valid, key: value}), ledger.PermissionUnavailable)
        for key in valid:
            with self.subTest(missing=key):
                self.assertIsInstance(self.parse({k: v for k, v in valid.items() if k != key}),
                                      ledger.PermissionUnavailable)

    def test_refused_and_unavailable_decisions_never_become_allowed_permissions(self) -> None:
        valid = self.allowed_record()
        book = ledger.CostLedger(self.state)
        self.replace("daily_limit_usd", "3")
        refused = book.guard(self.settings, "admission", self.clock.now, lambda _event: True)
        self.assertIsInstance(refused, ledger.SpendingRefused)
        document = cast(dict[str, object], json.loads(book.path.read_text()))
        parsed = self.parse({**valid, "decision": document["decision"]})
        self.assertIsInstance(parsed, remote.AdmissionEnvelope)
        assert isinstance(parsed, remote.AdmissionEnvelope)
        self.assertIsInstance(parsed.permission, ledger.PermissionRefused)
        self.remove("daily_limit_usd")
        unavailable = book.guard(self.settings, "admission", self.clock.now, lambda _event: True)
        self.assertIsInstance(unavailable, ledger.SpendingUnavailable)
        document = cast(dict[str, object], json.loads(book.path.read_text()))
        parsed = self.parse({**valid, "decision": document["decision"]})
        self.assertIsInstance(parsed, remote.AdmissionEnvelope)
        assert isinstance(parsed, remote.AdmissionEnvelope)
        self.assertIsInstance(parsed.permission, ledger.PermissionUnavailable)


class RemoteAgentTests(ControlFixture):
    remote_directory: Path = Path()
    groups: list[int] = []
    signals: list[tuple[int, int]] = []
    stops: list[int] = []
    polls: list[int] = []
    interrupted: bool = False

    @override
    def setUp(self) -> None:
        super().setUp()
        self.remote_directory = self.root / "remote"
        self.groups, self.signals, self.stops, self.polls = [], [], [], []
        self.interrupted = False
        self.publish_permission(self.remote_directory, self.admission_permission(), lifetime=86400)

    def imds(self) -> bool:
        self.polls.append(self.clock.now)
        return self.interrupted

    def signal_group(self, group: int, signum: int) -> None:
        self.assertTrue((self.remote_directory / "draining.json").exists())
        self.assertEqual(remote.interruption_generation(self.remote_directory,
            target="aws", generation="generation-one"), "generation-one")
        self.signals.append((group, signum))

    def agent(self, process_groups: Callable[[], Sequence[int]] | None = None) -> remote.RemoteAgent:
        return remote.RemoteAgent(self.settings, self.remote_directory, clock=self.clock, imds=self.imds,
            process_groups=process_groups if process_groups is not None else lambda: tuple(self.groups),
            signal_group=self.signal_group,
            shutdown=lambda: self.stops.append(self.clock.now), boot=lambda: "fixture-boot")

    def test_imds_failure_cannot_prevent_the_idle_stop(self) -> None:
        self.replace("idle_stop_minutes", "1")
        _ = self.agent().tick()
        self.clock.advance(60)
        agent = self.agent()
        agent.imds = mock.Mock(side_effect=TimeoutError("IMDS unavailable"))
        self.assertEqual(agent.tick(), "stopping")
        self.assertEqual(self.stops, [self.clock.now])

    def test_uncertain_process_evidence_cannot_prevent_the_idle_stop(self) -> None:
        self.replace("idle_stop_minutes", "1")
        _ = self.agent().tick()
        self.clock.advance(60)
        for error in (ValueError("slot process identity changed"),
                      ValueError("slot boot identity is missing"), PermissionError("unreadable environ")):
            with self.subTest(error=str(error)):
                agent = self.agent(process_groups=mock.Mock(side_effect=error))
                before = len(self.stops)
                self.assertEqual(agent.tick(), "stopping")
                self.assertEqual(len(self.stops), before + 1)
        self.assertEqual(self.signals, [])

    def test_a_failed_group_signal_does_not_skip_later_groups_or_the_saved_interruption(self) -> None:
        self.groups = [321, 654]
        self.interrupted = True
        attempts: list[int] = []

        def send(group: int, _signum: int) -> None:
            attempts.append(group)
            if group == 321:
                raise ProcessLookupError("group exited")
            if attempts.count(group) == 1:
                raise PermissionError("temporary signal failure")

        agent = self.agent()
        agent.signal_group = send
        _ = agent.tick()
        self.assertEqual(attempts, [321, 654])
        self.interrupted = False
        self.clock.advance(1)
        _ = agent.tick()
        self.assertEqual(attempts.count(654), 2)
        self.assertEqual(remote.interruption_generation(self.remote_directory,
            target="aws", generation="generation-one"), "generation-one")

    def test_real_process_reader_errors_cannot_disable_the_idle_deadline(self) -> None:
        self.replace("idle_stop_minutes", "1")
        _ = self.agent().tick()
        self.clock.advance(60)
        mirror = self.root / ".local/state/build-target/mirror/repository/1"
        mirror.mkdir(parents=True)
        marker = mirror / ".linux-run.json"
        proc = self.root / "proc"
        process = proc / "321"
        process.mkdir(parents=True)
        fields = ["S", "1", "321", *(["0"] * 16), "999"]
        _ = (process / "stat").write_text("321 (new cargo) " + " ".join(fields))
        _ = (process / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
        environment = process / "environ"
        _ = environment.write_bytes(b"BUILD_TARGET_RUN_TOKEN=new-run\0")
        read_bytes = Path.read_bytes

        def fields_for(pid: int, proc_root: Path = proc) -> list[str]:
            selected = proc_root
            return (selected / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()

        for failure in ("reused-pid", "missing-boot", "unreadable-environ"):
            with self.subTest(failure=failure):
                row: dict[str, object] = {"pid": 321, "start_time": 123, "process_group": 321,
                                          "token": "old-run", "boot_id": "fixture-boot"}
                if failure == "missing-boot":
                    del row["boot_id"]
                _ = marker.write_text(json.dumps(row))
                before = marker.read_bytes()

                def read(path: Path) -> bytes:
                    if failure == "unreadable-environ" and path == environment:
                        raise PermissionError("unreadable environ")
                    return read_bytes(path)

                with (
                    mock.patch.object(Path, "home", return_value=self.root),
                    mock.patch.object(linux_runner, "PROC_ROOT", proc),
                    mock.patch.object(linux_runner, "process_fields", side_effect=fields_for),
                    mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"),
                    mock.patch.object(Path, "read_bytes", read),
                ):
                    self.assertEqual(self.agent(process_groups=remote.active_process_groups).tick(), "stopping")
                self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(self.stops, [self.clock.now] * 3)
        self.assertEqual(self.signals, [])

    def test_active_groups_read_old_and_new_mirrors_with_each_process_read_once(self) -> None:
        proc = self.root / "proc"
        process = proc / "808"
        process.mkdir(parents=True)
        fields = ["S", "1", "808", *(["0"] * 16), "123"]
        _ = (process / "stat").write_text("808 (cargo check) " + " ".join(fields))
        environment = process / "environ"
        _ = environment.write_bytes(b"BUILD_TARGET_RUN_TOKEN=first-run\0")
        for slot, token, relative in ((1, "first-run", "repository/1"),
                                      (2, "second-run", "repository-key/aws-s2")):
            mirror = self.root / ".local/state/build-target/mirror" / relative
            mirror.mkdir(parents=True)
            _ = (mirror / ".linux-run.json").write_text(json.dumps({
                "pid": 320 + slot, "start_time": 123, "process_group": 320 + slot,
                "token": token, "boot_id": "fixture-boot"}))
        reads: list[Path] = []
        read_bytes = Path.read_bytes

        def read(path: Path) -> bytes:
            reads.append(path)
            return read_bytes(path)

        def fields_for(pid: int, proc_root: Path = proc) -> list[str]:
            selected = proc_root
            return (selected / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()

        with (
            mock.patch.object(Path, "home", return_value=self.root),
            mock.patch.object(linux_runner, "PROC_ROOT", proc),
            mock.patch.object(linux_runner, "process_fields", side_effect=fields_for),
            mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"),
            mock.patch.object(Path, "read_bytes", read),
        ):
            groups = remote.active_process_groups()
        self.assertEqual(groups, (808,))
        self.assertEqual(reads.count(environment), 1)

    def test_default_imds_client_uses_current_settings_without_a_second_config_read(self) -> None:
        self.replace("imds_timeout_s", "4")
        agent = remote.RemoteAgent(self.settings, self.remote_directory, clock=self.clock,
            process_groups=lambda: (), shutdown=lambda: self.stops.append(self.clock.now),
            boot=lambda: "fixture-boot")
        with (
            mock.patch.object(remote, "ImdsClient", return_value=lambda: False) as client,
            mock.patch.object(remote, "read_control_settings", wraps=settings_config.read_control_settings) as read,
        ):
            self.assertEqual(agent.tick(), "running")
        client.assert_called_once_with(4)
        self.assertEqual(read.call_count, 1)

    def test_interruption_records_draining_before_it_signals_each_active_group(self) -> None:
        self.groups = [321, 654]
        self.interrupted = True
        self.assertEqual(self.agent().tick(), "interrupted")
        self.assertEqual(self.signals, [(321, signal.SIGTERM), (654, signal.SIGTERM)])
        self.assertEqual(self.stops, [])
        self.assertIsInstance(remote.execution_permission(self.remote_directory, "aws",
            now=self.clock.now, boot_id="fixture-boot"), ledger.PermissionUnavailable)

    def test_interruption_evidence_from_another_generation_does_not_refuse_current_work(self) -> None:
        remote.write_interruption(self.remote_directory, target="aws", generation="older-generation", at=self.clock.now)
        self.assertEqual(remote.interruption_generation(self.remote_directory,
            target="aws", generation="generation-one"), "")
        self.assertIsInstance(remote.execution_permission(self.remote_directory, "aws",
            now=self.clock.now, boot_id="fixture-boot"), ledger.PermissionAllowed)

    def test_idle_stop_uses_the_configured_interval_and_resets_after_active_work(self) -> None:
        self.replace("idle_stop_minutes", "1")
        self.assertEqual(self.agent().tick(), "running")
        self.clock.advance(59)
        self.assertEqual(self.agent().tick(), "running")
        self.groups = [321]
        self.clock.advance(1)
        self.assertEqual(self.agent().tick(), "running")
        self.groups = []
        self.clock.advance(59)
        self.assertEqual(self.agent().tick(), "running")
        self.clock.advance(1)
        self.assertEqual(self.agent().tick(), "stopping")
        self.assertEqual(self.stops, [self.clock.now])

    def test_imds_checks_use_the_configured_poll_interval_across_restart(self) -> None:
        _ = self.agent().tick()
        self.clock.advance(4)
        _ = self.agent().tick()
        self.assertEqual(len(self.polls), 1)
        self.clock.advance(1)
        _ = self.agent().tick()
        self.assertEqual(len(self.polls), 2)

    def test_disabled_remote_agent_starts_no_imds_check_signal_or_shutdown(self) -> None:
        self.replace("offload", "off")
        self.interrupted = True
        self.groups = [321]
        self.assertEqual(self.agent().tick(), "off")
        self.assertEqual((self.polls, self.signals, self.stops), ([], [], []))

    def test_idle_checks_refuse_a_retained_marker_without_boot_identity(self) -> None:
        mirror = self.root / ".local/state/build-target/mirror/repository/1"
        mirror.mkdir(parents=True)
        marker = mirror / ".linux-run.json"
        _ = marker.write_text(json.dumps({"pid": 321, "start_time": 123, "process_group": 321, "token": "run"}))
        before = marker.read_bytes()
        self.replace("idle_stop_minutes", "1")
        agent = self.agent(process_groups=remote.active_process_groups)
        values = {**conf.read(self.settings).values(), "selected_target": "aws",
                  "selected_settings": str(self.settings), "remote_state_directory": str(self.remote_directory)}
        with (
            mock.patch.object(Path, "home", return_value=self.root),
            mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"),
            mock.patch.object(time, "time", side_effect=self.clock),
            mock.patch.object(linux_runner, "prepare_shell", side_effect=AssertionError("Uncertain slot cannot run")),
        ):
            self.assertEqual(agent.tick(), "running")
            self.assertEqual(self.stops, [])
            for entry in ("probe", "run"):
                with self.subTest(entry=entry), redirect_stdout(io.StringIO()) as output:
                    if entry == "probe":
                        status = linux_runner.probe_shell(mirror, values, "hana")
                    else:
                        status = linux_runner.run(mirror, values, "hana", "check", (), "call", "tree")
                self.assertEqual(status, 75)
                self.assertIn("linux_startup=unresolved", output.getvalue())
            self.clock.advance(60)
            self.assertEqual(agent.tick(), "stopping")
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(self.stops, [self.clock.now])
        self.assertEqual(self.signals, [])

    def test_idle_stop_remains_available_before_the_first_generation_publication(self) -> None:
        (self.remote_directory / "generation.json").unlink()
        self.replace("idle_stop_minutes", "1")
        self.assertIsInstance(remote.execution_permission(self.remote_directory, "aws",
            now=self.clock.now, boot_id="fixture-boot"), ledger.PermissionUnavailable)
        self.assertEqual(self.agent().tick(), "running")
        self.clock.advance(59)
        self.assertEqual(self.agent().tick(), "running")
        self.assertEqual(self.stops, [])
        self.clock.advance(1)
        self.assertEqual(self.agent().tick(), "stopping")
        self.assertEqual(self.stops, [self.clock.now])
        self.assertFalse((self.remote_directory / "generation.json").exists())
        self.assertIsInstance(remote.execution_permission(self.remote_directory, "aws",
            now=self.clock.now, boot_id="fixture-boot"), ledger.PermissionUnavailable)

    def test_a_new_boot_cannot_reuse_the_previous_permission(self) -> None:
        self.assertIsInstance(remote.execution_permission(self.remote_directory, "aws",
            now=self.clock.now, boot_id="restarted-boot"), ledger.PermissionUnavailable)


class RunnerPermissionTests(ControlFixture):
    mirror: Path = Path()
    remote_directory: Path = Path()
    values: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.mirror = self.root / "mirror"
        self.mirror.mkdir()
        self.remote_directory = self.root / "remote"
        self.values = {**conf.read(self.settings).values(), "selected_target": "aws",
                       "selected_settings": str(self.settings), "remote_state_directory": str(self.remote_directory)}
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(time, "time", return_value=self.clock.now))
        _ = self.enterContext(mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"))

    def publish_allowed(self) -> None:
        self.publish(self.admission_permission())

    def publish(self, permission: ledger.AdmissionPermission) -> None:
        self.publish_permission(self.remote_directory, permission)

    def call(self, entry: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            if entry == "probe":
                status = linux_runner.probe_shell(self.mirror, self.values, "hana")
            else:
                status = linux_runner.run(self.mirror, self.values, "hana", "check", (), "call", "tree")
        return status, output.getvalue()

    def assert_refused_before_shell(self) -> None:
        for entry in ("probe", "run"):
            with self.subTest(entry=entry), \
                 mock.patch.object(linux_runner, "prepare_shell", side_effect=AssertionError("Shell evaluation started")), \
                 mock.patch.object(linux_runner, "execute_slot", side_effect=AssertionError("Work started")), \
                 mock.patch.object(subprocess, "run", side_effect=AssertionError("Shell hook started")):
                status, output = self.call(entry)
            self.assertEqual(status, 75)
            self.assertIn("aws_permission=refused", output)
            self.assertNotIn("mac_exit=", output)
            self.assertNotIn("Traceback", output)
            self.assertFalse((self.mirror / ".linux-run.json").exists())

    def test_permission_expiry_during_shell_evaluation_starts_neither_hook_nor_work(self) -> None:
        for entry in ("probe", "run"):
            with self.subTest(entry=entry):
                self.publish_allowed()
                initial = self.clock.now
                shell = linux_runner.ShellEnvironment("fixture", self.root / "exports", self.root / "inputs")

                def evaluate(*_args: object, **_options: object) -> linux_runner.ShellEnvironment:
                    self.clock.advance(61)
                    return shell

                with (
                    mock.patch.object(time, "time", side_effect=self.clock),
                    mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()),
                    mock.patch.object(linux_runner, "prepare_shell", side_effect=evaluate),
                    mock.patch.object(linux_runner, "trim_build_folder", return_value=0),
                    mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as execute,
                ):
                    status, output = self.call(entry)
                self.assertEqual(self.clock.now, initial + 61)
                self.assertEqual(status, 75)
                self.assertIn("aws_permission=refused", output)
                execute.assert_not_called()

    def test_missing_permission_refuses_both_entry_points_before_shell_evaluation(self) -> None:
        self.assert_refused_before_shell()

    def test_expired_permission_refuses_both_entry_points_before_shell_evaluation(self) -> None:
        self.publish_allowed()
        _ = self.enterContext(mock.patch.object(time, "time", return_value=self.clock.now + 60))
        self.assert_refused_before_shell()

    def test_malformed_permission_refuses_both_entry_points_before_shell_evaluation(self) -> None:
        self.publish_allowed()
        _ = (self.remote_directory / "admission.json").write_text("{broken")
        self.assert_refused_before_shell()

    def test_another_generation_permission_refuses_both_entry_points(self) -> None:
        self.publish_allowed()
        document = cast(dict[str, object], json.loads((self.remote_directory / "admission.json").read_text()))
        document["generation"] = "another-generation"
        _ = (self.remote_directory / "admission.json").write_text(json.dumps(document))
        self.assert_refused_before_shell()

    def test_guard_refusal_after_reservation_prevents_probe_and_execution(self) -> None:
        paths = mac_test.state_paths("aws")
        arguments = mac_test.CliArguments()
        arguments.target, arguments.worktree = "aws", str(self.root)
        arguments.pid, arguments.what, arguments.remote = 700, "check hana", "aws-build"
        scope = mac_test.RepositoryScope("hana", "https://example.invalid/team/hana", 1)
        with mock.patch.object(mac_test, "target_claim_scope", return_value=scope), \
             mock.patch.object(process_identity, "process_start", return_value=process_identity.Started("fixture-process")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(arguments, paths, watch_claim=False), 0)
        claim = mac_test.claim_path(paths, scope)
        before = claim.read_bytes()
        self.replace("daily_limit_usd", "3")
        book = ledger.CostLedger(self.state)
        decision = book.guard(self.settings, "admission", self.clock.now, lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingRefused)
        document = cast(dict[str, object], json.loads(book.path.read_text()))
        self.publish(permission=ledger.decode_permission(document["decision"]))
        self.assert_refused_before_shell()
        self.assertEqual(claim.read_bytes(), before)

    def test_failed_ledger_write_cannot_publish_permission_for_either_entry_point(self) -> None:
        self.publish_allowed()
        book = ledger.CostLedger(self.state)
        with mock.patch.object(ledger, "write_text_atomic", side_effect=OSError("decision write failed")):
            decision = book.guard(self.settings, "admission", self.clock.now, lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingUnavailable)
        assert isinstance(decision, ledger.SpendingUnavailable)
        self.publish(permission=book.permission_for_decision(decision))
        self.assert_refused_before_shell()

    def test_current_permission_permits_the_probe_and_work(self) -> None:
        self.publish_allowed()
        shell = linux_runner.ShellEnvironment("fixture", self.root / "exports", self.root / "inputs")
        with mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
             mock.patch.object(linux_runner, "prepare_shell", return_value=shell) as prepare, \
             mock.patch.object(linux_runner, "execute_slot", return_value=0) as execute, \
             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(self.call("probe")[0], 0)
            self.assertEqual(self.call("run")[0], 0)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(execute.call_count, 1)

    def test_interruption_during_execution_emits_the_current_generation_at_both_entry_points(self) -> None:
        for entry in ("probe", "run"):
            with self.subTest(entry=entry):
                (self.remote_directory / "interruption.json").unlink(missing_ok=True)
                self.publish_allowed()
                shell = linux_runner.ShellEnvironment("fixture", self.root / "exports", self.root / "inputs")

                def interrupted_probe(*_args: object, **_options: object) -> subprocess.CompletedProcess[str]:
                    remote.write_interruption(self.remote_directory, target="aws", generation="generation-one",
                                              at=self.clock.now)
                    return subprocess.CompletedProcess([], 143)

                def interrupted_run(*_args: object, **_options: object) -> int:
                    remote.write_interruption(self.remote_directory, target="aws", generation="generation-one",
                                              at=self.clock.now)
                    return 143

                with mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
                     mock.patch.object(linux_runner, "prepare_shell", return_value=shell), \
                     mock.patch.object(linux_runner, "execute_slot", side_effect=interrupted_run), \
                     mock.patch.object(subprocess, "run", side_effect=interrupted_probe):
                    status, output = self.call(entry)
                self.assertEqual(status, 143)
                self.assertIn("aws_interrupted=generation-one", output)
                self.assertNotIn("linux_startup=unresolved", output)

    def test_generic_linux_and_loopback_work_do_not_require_aws_permission(self) -> None:
        for target in ("spare", "loopback"):
            with self.subTest(target=target):
                self.values.update(selected_target=target, selected_settings="linux.conf")
                shell = linux_runner.ShellEnvironment("fixture", self.root / "exports", self.root / "inputs")
                with mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
                     mock.patch.object(linux_runner, "prepare_shell", return_value=shell), \
                     mock.patch.object(linux_runner, "execute_slot", return_value=0), \
                     mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                    self.assertEqual(self.call("probe")[0], 0)
                    self.assertEqual(self.call("run")[0], 0)


class ClaimReleaseTests(ControlFixture):
    paths: mac_test.StatePaths = mac_test.StatePaths(Path())

    @override
    def setUp(self) -> None:
        super().setUp()
        self.paths = mac_test.StatePaths(self.state, "aws", registry_selected=True, platform="linux")

    def test_confirmed_stop_releases_only_the_captured_aws_claims_and_is_idempotent(self) -> None:
        for prepared_fence in (False, True):
            with self.subTest(prepared_fence=prepared_fence):
                kept = self.claim(self.paths)
                record = mac_test.read_run(kept)
                assert not isinstance(record, mac_test.NoRun)
                identity = claim_state.claim_identity(record)
                other = mac_test.StatePaths(self.root / "other", "spare", platform="linux")
                unrelated = self.claim(other)
                pending = offload.pending_start_path("aws", identity)
                fence_directory = self.root / offload.start_fence_relative(identity, "aws", scope_key=kept.stem).parent
                if prepared_fence:
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    _ = pending.write_text("pending")
                    prepared = subprocess.run(["bash", "-c", offload.prepare_start_fence_command(
                        identity, "aws", scope_key=kept.stem)], env=dict(os.environ), cwd=self.root,
                        capture_output=True, text=True, check=False, timeout=5)
                    self.assertEqual(prepared.returncode, 0, prepared.stderr)
                evidence = mac_test.capture_release_evidence(self.paths, "generation-one", "stopped", self.clock.now)
                real_run = subprocess.run

                def release_transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    del options
                    self.assertEqual((arguments[0], arguments[-2]), ("ssh", "aws-build"))
                    return real_run(["bash", "-c", arguments[-1]], cwd=self.root, env=dict(os.environ),
                                    capture_output=True, text=True, check=False, timeout=5)

                with mock.patch.object(subprocess, "run", side_effect=release_transport):
                    _ = mac_test.release_stopped_claims(self.paths, evidence)
                    _ = mac_test.release_stopped_claims(self.paths, evidence)
                self.assertFalse(kept.exists())
                self.assertTrue(unrelated.exists())
                proof = cast(dict[str, object], json.loads((self.state / "release-evidence" / kept.name).read_text()))
                self.assertEqual((proof["target"], proof["generation"], proof["state"]),
                                 ("aws", "generation-one", "stopped"))
                self.assertEqual(cast(dict[str, object], proof["claim"])["remote_host"], "aws-build")
                if prepared_fence:
                    self.assertFalse(pending.exists())
                    self.assertEqual(list(fence_directory.iterdir()), [])

    def test_stop_evidence_cannot_release_a_replacement_claim_after_restart(self) -> None:
        kept = self.claim(self.paths)
        evidence = mac_test.capture_release_evidence(self.paths, "generation-one", "stopped", self.clock.now)
        replacement = self.claim(self.paths, at=self.clock.now + 1, pid=2147483001)
        self.assertEqual(kept, replacement)
        before = replacement.read_bytes()
        _ = mac_test.release_stopped_claims(self.paths, evidence)
        self.assertEqual(replacement.read_bytes(), before)

    def test_claims_created_after_the_stop_observation_remain_owned(self) -> None:
        old = self.claim(self.paths)
        later = self.claim(self.paths, slot=2, at=self.clock.now + 1)
        evidence = mac_test.capture_release_evidence(self.paths, "generation-one", "stopped", self.clock.now)
        _ = mac_test.release_stopped_claims(self.paths, evidence)
        self.assertFalse(old.exists())
        self.assertTrue(later.exists())

    def test_stop_evidence_cannot_release_another_targets_claims(self) -> None:
        _ = self.claim(self.paths)
        other = mac_test.StatePaths(self.root / "other", "spare", platform="linux")
        unrelated = self.claim(other)
        evidence = mac_test.capture_release_evidence(self.paths, "generation-one", "stopped", self.clock.now)
        before = unrelated.read_bytes()
        with self.assertRaises(mac_test.StateReadError):
            _ = mac_test.release_stopped_claims(other, evidence)
        self.assertEqual(unrelated.read_bytes(), before)

    def test_stop_evidence_preserves_claims_for_another_host_or_without_a_generation(self) -> None:
        for mismatch in ("host", "legacy"):
            with self.subTest(mismatch=mismatch):
                path = self.claim(self.paths)
                row = cast(dict[str, object], json.loads(path.read_text()))
                row["remote_host"] = "previous-host" if mismatch == "host" else "aws-build"
                if mismatch == "legacy":
                    del row["instance_generation"]
                _ = path.write_text(json.dumps(row))
                before = path.read_bytes()
                evidence = mac_test.capture_release_evidence(self.paths, "generation-one", "stopped", self.clock.now)
                self.assertEqual(mac_test.release_stopped_claims(self.paths, evidence), 0)
                self.assertEqual(path.read_bytes(), before)

    def test_running_unknown_and_unreachable_states_keep_every_claim(self) -> None:
        path = self.claim(self.paths)
        before = path.read_bytes()
        for state in ("running", "unknown", "unreachable"):
            with self.subTest(state=state):
                try:
                    evidence = mac_test.capture_release_evidence(self.paths, "generation-one", state, self.clock.now)
                except ValueError:
                    pass
                else:
                    _ = mac_test.release_stopped_claims(self.paths, evidence)
                self.assertEqual(path.read_bytes(), before)


class RetainedMarkerTests(ControlFixture):
    def marker_and_process(self, *, boot: str = "new-boot") -> tuple[Path, Path]:
        mirror = self.root / "mirror/1"
        mirror.mkdir(parents=True)
        proc = self.root / "proc"
        process = proc / "321"
        process.mkdir(parents=True)
        boot_file = proc / "sys/kernel/random/boot_id"
        boot_file.parent.mkdir(parents=True)
        _ = boot_file.write_text(boot + "\n")
        fields = ["S", "1", "321", *(["0"] * 16), "123"]
        _ = (process / "stat").write_text("321 (new cargo) " + " ".join(fields))
        _ = (process / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
        _ = (process / "cmdline").write_bytes(b"cargo\0check\0")
        _ = (process / "environ").write_bytes(b"BUILD_TARGET_RUN_TOKEN=new-run\0")
        (process / "cwd").symlink_to(mirror)
        _ = (mirror / ".linux-run.json").write_text(json.dumps({
            "pid": 321, "start_time": 123, "process_group": 321,
            "token": "old-run", "boot_id": "old-boot",
        }))
        return mirror, proc

    def test_a_new_boot_recovers_the_old_marker_without_selecting_a_reused_pid(self) -> None:
        mirror, proc = self.marker_and_process()
        with mock.patch.object(os, "kill", side_effect=AssertionError("Never kill the new process")):
            selected = linux_runner.slot_processes(mirror, proc)
        self.assertEqual(selected, ())
        self.assertFalse((mirror / ".linux-run.json").exists())
        self.assertTrue((proc / "321/stat").exists())

    def test_malformed_boot_identity_never_releases_a_live_marker(self) -> None:
        mirror, proc = self.marker_and_process()
        marker = mirror / ".linux-run.json"
        original = cast(dict[str, object], json.loads(marker.read_text()))
        malformed_boots: tuple[object, ...] = (None, 0, False, list[object](), dict[str, object]())
        for boot in malformed_boots:
            with self.subTest(boot=boot):
                _ = marker.write_text(json.dumps({**original, "boot_id": boot}))
                before = marker.read_bytes()
                with mock.patch.object(os, "kill", side_effect=AssertionError("Never kill uncertain work")):
                    try:
                        _ = linux_runner.slot_processes(mirror, proc)
                    except (ValueError, linux_runner.SlotProcessesUnresolved):
                        pass
                self.assertTrue(marker.exists())
                self.assertEqual(marker.read_bytes(), before)
                self.assertTrue((proc / "321/stat").exists())

    def test_legacy_marker_on_a_generic_linux_target_uses_process_evidence(self) -> None:
        mirror, proc = self.marker_and_process()
        marker = mirror / ".linux-run.json"
        row = cast(dict[str, object], json.loads(marker.read_text()))
        del row["boot_id"]
        _ = marker.write_text(json.dumps(row))
        _ = (proc / "321/environ").write_bytes(b"BUILD_TARGET_RUN_TOKEN=old-run\0")
        with mock.patch.object(os, "kill", side_effect=AssertionError("Selection must not kill")):
            selected = linux_runner.slot_processes(mirror, proc)
        self.assertEqual(selected, (321,))
        self.assertTrue(marker.exists())
        (proc / "321/stat").unlink()
        (proc / "321/status").unlink()
        (proc / "321/environ").unlink()
        (proc / "321/cmdline").unlink()
        (proc / "321/cwd").unlink()
        (proc / "321").rmdir()
        self.assertEqual(linux_runner.slot_processes(mirror, proc), ())
        self.assertFalse(marker.exists())

    def test_missing_boot_evidence_keeps_the_old_marker_and_new_process(self) -> None:
        mirror, proc = self.marker_and_process()
        (proc / "sys/kernel/random/boot_id").unlink()
        before = (mirror / ".linux-run.json").read_bytes()
        with mock.patch.object(os, "kill", side_effect=AssertionError("Uncertain boot identity")):
            try:
                selected = linux_runner.slot_processes(mirror, proc)
            except (linux_runner.SlotProcessesUnresolved, OSError, ValueError):
                selected = ()
        self.assertEqual(selected, ())
        self.assertEqual((mirror / ".linux-run.json").read_bytes(), before)
        self.assertTrue((proc / "321/stat").exists())


if __name__ == "__main__":
    _ = unittest.main()
