"""Lifecycle actions require queue evidence and durable spending permission."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import sys
import subprocess
from pathlib import Path
from typing import cast, override
import unittest
from unittest import mock


from scripts.aws_target import aws_target, config as settings_config, ledger, remote
from scripts.aws_target.control_test_support import ControlFixture
from scripts.mac_test import mac_test
from scripts.lib.atomic_file import write_text_atomic
from scripts.direct import step_record
from scripts.mac_test import offload


class ControlTests(ControlFixture):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.publications: list[remote.AdmissionEnvelope] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = (self.state / 'deployment.json').write_text(json.dumps({
            'region': 'us-east-1', 'stack': 'fixture-build', 'outputs': {'InstanceId': 'i-fixture'}}))

    def test_controller_uses_saved_identity_and_ignores_the_old_config_identity(self) -> None:
        self.replace('instance_id', 'i-unrelated')
        self.assertEqual(self.controller().start().state, 'starting')
        instances = [words for words in self.aws.calls if 'describe-instances' in words]
        self.assertTrue(instances)
        self.assertTrue(all(words[words.index('--instance-ids') + 1] == 'i-fixture' for words in instances))

    def test_controller_refuses_a_region_change_before_an_aws_call(self) -> None:
        self.replace('region', 'us-east-2')
        self.assertEqual(self.controller().start().state, 'refused')
        self.assertEqual(self.aws.calls, [])

    def test_controller_refuses_a_missing_saved_identity_before_an_aws_call(self) -> None:
        (self.state / 'deployment.json').unlink()
        self.assertEqual(self.controller().start().state, 'refused')
        self.assertEqual(self.aws.calls, [])

    def publish(self, envelope: remote.AdmissionEnvelope) -> bool:
        self.publications.append(envelope)
        return True

    def reject_publication(self, envelope: remote.AdmissionEnvelope) -> bool:
        self.publications.append(envelope)
        return False

    def controller(self) -> aws_target.Controller:
        return aws_target.Controller(self.settings, self.state, self.queue_directory,
                                     execute=self.aws, clock=self.clock, notify=lambda _event: True,
                                     publish=self.publish)

    def demand(self) -> None:
        self.snapshot(self.waiter(101, step="check"), self.waiter(102, step="doc"))

    def pinned_step(self, target: str = "aws", kind: str = "test") -> None:
        self.enterContext(mock.patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(self.root / "steps")}))
        self.enterContext(mock.patch.object(offload, "repository_identity")).return_value = (
            offload.KnownRepository("hana", "https://example.invalid/team/hana"))
        pid = os.getpid()
        step_record.write(step_record.StepRecord("pinned", pid, step_record.process_start(pid), self.root,
            kind, step_record.NamedPackage("hana"), step_record.AnyTarget((target,)), step_record.StepState.WAITING_SSH,
            "2026-01-15T12:00:00Z", selection=step_record.Pinned(target)))

    def test_one_new_pinned_step_starts_capacity_without_local_queue_demand(self) -> None:
        self.pinned_step()
        self.assertEqual(self.controller().tick().state, "starting")
        self.assertEqual(self.aws.count("start-instances"), 1)
        decision = ledger.CostLedger(self.state).read_snapshot().decision
        self.assertEqual(decision["status"], "allowed")

    def test_pinned_final_starts_capacity_for_check_and_test(self) -> None:
        self.pinned_step(kind="final")
        self.assertEqual(self.controller().tick().state, "starting")
        self.assertEqual(self.aws.count("start-instances"), 1)

    def test_pinned_final_requires_both_remote_kinds(self) -> None:
        self.pinned_step(kind="final")
        for kinds in ("check", "test"):
            with self.subTest(kinds=kinds):
                self.replace("kinds", kinds)
                self.assertEqual(self.controller().tick().state, "stopped")
                self.assertEqual(self.aws.count("start-instances"), 0)

    def test_unpinned_build_waiters_do_not_start_capacity_with_default_kinds(self) -> None:
        self.snapshot(self.waiter(101, step="build"), self.waiter(102, step="build"))
        self.assertEqual(self.controller().tick().state, "stopped")
        self.assertEqual(self.aws.count("start-instances"), 0)

    def test_queue_repository_hold_controls_whether_a_stopped_box_starts(self) -> None:
        self.replace("start_after_wait_s", "1")
        self.snapshot(self.waiter(101, age=2), self.waiter(102, age=2))
        self.check_decline_demand()

    def test_pinned_repository_hold_controls_whether_a_stopped_box_starts(self) -> None:
        self.pinned_step()
        self.check_decline_demand()

    def test_queue_subdirectory_uses_the_repository_hold(self) -> None:
        folder = self.root / "src/subdirectory"
        folder.mkdir(parents=True)
        first, second = self.waiter(101), self.waiter(102)
        first["folder"] = second["folder"] = str(folder)
        self.snapshot(first, second)
        self.check_decline_demand()

    def test_pinned_poll_updates_keep_the_repository_hold(self) -> None:
        self.pinned_step()
        row = cast(dict[str, object], json.loads(step_record.record_path("pinned").read_text()))
        step_record.write_fields("pinned", {**row, "updated": "2026-01-15T12:00:01Z"})
        self.check_decline_demand()

    def check_decline_demand(self) -> None:
        self.replace("version_decline_hold_s", "60")
        controller = self.controller()
        path = offload.linux_version_decline_path("aws", "hana")
        other = offload.linux_version_decline_path("aws", "other")
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            mock.patch.object(subprocess, "run", side_effect=AssertionError("No subprocess expected")),
            mock.patch.object(subprocess, "Popen", side_effect=AssertionError("No subprocess expected")),
            mock.patch.object(offload, "expected_linux_parity", side_effect=AssertionError("No tool comparison expected")),
        ):
            for content, starts in ((str(self.clock.now - 10), False), (str(self.clock.now - 59), False),
                                    (str(self.clock.now - 60), True), (None, True), ("unreadable", True)):
                with self.subTest(content=content):
                    self.aws.state = "stopped"
                    self.aws.calls.clear()
                    path.unlink(missing_ok=True)
                    _ = other.write_text(str(self.clock.now))
                    if content is not None:
                        _ = path.write_text(content)
                    self.assertEqual(controller.tick().state, "starting" if starts else "stopped")
                    self.assertEqual(self.aws.count("start-instances"), int(starts))

    def test_pinned_step_cannot_start_capacity_when_either_spending_cap_refuses(self) -> None:
        self.pinned_step()
        for key in ("daily_limit_usd", "monthly_limit_usd"):
            with self.subTest(key=key):
                self.replace(key, "3")
                self.assertEqual(self.controller().tick().state, "refused")
                self.assertEqual(self.aws.count("start-instances"), 0)
                self.replace(key, "20" if key == "daily_limit_usd" else "400")

    def test_foreign_completed_and_disallowed_pins_do_not_start_aws(self) -> None:
        self.replace("kinds", "test,check,doc")
        self.pinned_step("spare")
        self.assertEqual(self.controller().tick().state, "stopped")
        self.pinned_step(kind="build")
        self.assertEqual(self.controller().tick().state, "stopped")
        self.pinned_step()
        _ = step_record.record_path("pinned").with_suffix(".state").write_text("on_ssh aws\n")
        self.assertEqual(self.controller().tick().state, "stopped")
        self.assertEqual(self.aws.count("start-instances"), 0)

    def retained_claim(self) -> Path:
        return self.claim(mac_test.state_paths("aws"), generation="i-fixture:" + self.aws.generation)

    def documents(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(path.read_text()))
                for path in self.state.glob("*.json") if path.is_file()]

    def test_interrupted_reconciliation_keeps_restart_protection_until_the_guard_finishes(self) -> None:
        self.aws.state = "running"
        _ = self.controller().tick()
        _ = self.retained_claim()
        self.clock.advance(1)
        self.aws.generation = "2026-01-15T12:00:01+00:00"
        with mock.patch.object(ledger, "spending_guard", side_effect=RuntimeError("controller stopped")):
            with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                _ = self.controller().tick()
        self.replace("daily_limit_usd", "3")
        result = self.controller().tick()
        self.assertEqual(result.state, "stopping")
        self.assertEqual(self.aws.count("stop-instances"), 1)

    def test_completed_drain_allows_a_later_start_when_spending_permission_returns(self) -> None:
        self.aws.state = "running"
        _ = self.controller().tick()
        self.clock.advance(1)
        self.replace("daily_limit_usd", "3")
        self.assertEqual(self.controller().tick().state, "stopping")
        self.aws.state = "stopped"
        self.clock.advance(1)
        self.replace("daily_limit_usd", "20")
        self.assertEqual(self.controller().start().state, "starting")
        self.assertEqual(self.aws.count("start-instances"), 1)

    def test_publication_timeout_cannot_delay_a_required_stop(self) -> None:
        self.publication_timeout_stops(unexpected=False)

    def test_unpublished_permission_stops_an_idle_instance_at_the_limit(self) -> None:
        self.aws.state = "running"
        self.replace("idle_stop_minutes", "1")
        controller = self.controller()
        controller.publish = self.reject_publication
        self.assertEqual(controller.tick().reason, "permission_unpublished")
        self.clock.advance(60)
        restarted = self.controller()
        restarted.publish = self.reject_publication
        result = restarted.tick()
        self.assertEqual((result.state, result.reason), ("stopping", "unpublished_idle"))
        self.assertEqual(self.aws.count("stop-instances"), 1)

    def test_unpublished_permission_keeps_an_idle_instance_before_the_limit(self) -> None:
        self.aws.state = "running"
        self.replace("idle_stop_minutes", "1")
        controller = self.controller()
        controller.publish = self.reject_publication
        self.assertEqual(controller.tick().reason, "permission_unpublished")
        self.clock.advance(59)
        result = controller.tick()
        self.assertEqual((result.state, result.reason), ("running", "permission_unpublished"))
        self.assertEqual(self.aws.count("stop-instances"), 0)

    def test_published_permission_resets_the_unpublished_idle_timer(self) -> None:
        self.aws.state = "running"
        self.replace("idle_stop_minutes", "1")
        controller = self.controller()
        controller.publish = self.reject_publication
        self.assertEqual(controller.tick().reason, "permission_unpublished")
        self.clock.advance(59)
        controller.publish = self.publish
        self.assertEqual(controller.tick().reason, "")
        self.clock.advance(1)
        controller.publish = self.reject_publication
        self.assertEqual(controller.tick().state, "running")
        self.clock.advance(59)
        self.assertEqual(controller.tick().state, "running")
        self.assertEqual(self.aws.count("stop-instances"), 0)
        self.clock.advance(1)
        self.assertEqual(controller.tick().reason, "unpublished_idle")

    def test_new_generation_resets_the_unpublished_idle_timer(self) -> None:
        self.aws.state = "running"
        self.replace("idle_stop_minutes", "1")
        controller = self.controller()
        controller.publish = self.reject_publication
        self.assertEqual(controller.tick().reason, "permission_unpublished")
        self.clock.advance(60)
        self.aws.generation = "2026-01-15T12:01:00+00:00"
        self.assertEqual(controller.tick().state, "running")
        self.clock.advance(59)
        self.assertEqual(controller.tick().state, "running")
        self.assertEqual(self.aws.count("stop-instances"), 0)
        self.clock.advance(1)
        self.assertEqual(controller.tick().reason, "unpublished_idle")

    def test_active_claim_prevents_the_unpublished_idle_stop(self) -> None:
        self.aws.state = "running"
        self.replace("idle_stop_minutes", "1")
        claim = self.retained_claim()
        controller = self.controller()
        controller.publish = self.reject_publication
        self.assertEqual(controller.tick().reason, "permission_unpublished")
        self.clock.advance(60)
        self.assertEqual(controller.tick().state, "running")
        self.assertEqual(self.aws.count("stop-instances"), 0)
        claim.unlink()
        self.clock.advance(1)
        self.assertEqual(controller.tick().reason, "unpublished_idle")
        self.assertEqual(self.aws.count("stop-instances"), 1)

    def test_publication_timeout_cannot_delay_an_unexpected_generation_stop(self) -> None:
        self.publication_timeout_stops(unexpected=True)

    def publication_timeout_stops(self, *, unexpected: bool) -> None:
        self.aws.state = "running"
        _ = self.controller().tick()
        _ = self.retained_claim()
        self.clock.advance(1)
        self.replace("daily_limit_usd", "3")
        if unexpected:
            self.aws.generation = "2026-01-15T13:00:00+00:00"
        else:
            self.replace("drain_max_minutes", "1")
            self.assertEqual(self.controller().tick().state, "draining")
            self.clock.advance(60)
        controller = self.controller()
        controller.publish = mock.Mock(side_effect=subprocess.TimeoutExpired("ssh", 1))
        before = self.aws.count("stop-instances")
        self.assertEqual(controller.tick().state, "stopping")
        self.assertEqual(self.aws.count("stop-instances"), before + 1)


    def test_manual_stop_survives_an_accounting_write_failure(self) -> None:
        self.aws.state = "running"
        book = ledger.CostLedger(self.state)
        before = book.path.read_bytes()
        with mock.patch.object(ledger, "write_text_atomic", side_effect=OSError("ledger disk full")):
            result = self.controller().stop()
        self.assertEqual(result.state, "stopping")
        self.assertEqual(self.aws.count("stop-instances"), 1)
        self.assertEqual(book.path.read_bytes(), before)

    def test_cli_help_imports_in_a_clean_interpreter_without_aws_commands(self) -> None:
        tools = self.root / "cli-bin"
        tools.mkdir()
        aws = tools / "aws"
        called = self.root / "aws-called"
        _ = aws.write_text('#!/bin/sh\nprintf called > "$CLI_AWS_CALLED"\nexit 99\n')
        aws.chmod(0o755)
        replica = self.root / "box"
        replica.mkdir()
        root = Path(__file__).resolve().parents[2]

        def transfer(arguments: Sequence[str], deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT
                     ) -> offload.CapturedCommandResult:
            if "ls-files" in arguments:
                timeout = deadline.seconds if isinstance(deadline, offload.CommandTimeLimit) else 10
                reply = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=timeout)
                return offload.CommandOutput(reply.returncode, reply.stdout, reply.stderr)
            if "rev-parse" in arguments:
                return offload.CommandOutput(0, "fixture-commit\n", "")
            if arguments[0] == "rsync":
                manifest, = [Path(word.removeprefix("--files-from=")) for word in arguments
                             if word.startswith("--files-from=")]
                for name in manifest.read_text().splitlines():
                    destination = replica / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _ = shutil.copyfile(root / name, destination)
            return offload.CommandOutput(0, "", "")

        controller = offload.ProcessController()
        parity: offload.PublicToolParity = {
            "rustc": "fixture", "nextest": "fixture", "mend": "fixture", "scripts_commit": "fixture-commit",
        }
        with mock.patch.object(controller, "capture", side_effect=transfer), \
                mock.patch.object(offload, "expected_linux_parity", return_value=parity):
            self.assertTrue(offload.copy_tracked_scripts(controller, offload.read_config(self.settings)))
        inactive = replica / "config"
        registry = inactive / "build_targets.conf"
        _ = registry.write_text("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        settings = inactive / "aws_target.conf"
        _ = settings.write_text("offload=off\n")
        environment = {**os.environ, "PATH": str(tools), "PYTHONPATH": "", "CONF_DIR": str(inactive),
                       "BUILD_TARGETS_CONFIG": str(registry), "CLI_AWS_CALLED": str(called)}
        for module in (replica / "scripts/aws_target/aws_target.py", replica / "scripts/aws_target/remote.py",
                       replica / "scripts/mac_test/linux_runner.py"):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-I", str(module), "--help"],
                    cwd=self.root, env=environment,
                    capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

        for command in ("tick", "start", "stop"):
            with self.subTest(disabled=command):
                result = subprocess.run([sys.executable, "-I", str(replica / "scripts/aws_target/aws_target.py"), command],
                    cwd=self.root, env=environment, capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cast(dict[str, object], json.loads(result.stdout))["state"], "off")
        self.assertFalse(called.exists())

    def test_generic_linux_probe_and_run_work_when_spending_modules_cannot_import(self) -> None:
        script = r"""
import importlib.abc
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest import mock
import subprocess

class SpendingUnavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("scripts.aws_target"):
            raise ImportError("spending modules unavailable")
        return None

sys.meta_path.insert(0, SpendingUnavailable())
sys.path.insert(0, sys.argv[1])
from scripts.mac_test import linux_runner
mirror = Path(sys.argv[2])
values = {"selected_target": "spare", "selected_settings": "linux.conf", "shell_cache": "shells",
          "shell_timeout_s": "1", "devshell": "default"}
shell = linux_runner.ShellEnvironment("fixture", mirror / "exports", mirror / "inputs")
with mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
     mock.patch.object(linux_runner, "prepare_shell", return_value=shell), \
     mock.patch.object(linux_runner, "execute_slot", return_value=0), \
     mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
    assert linux_runner.probe_shell(mirror, values, "hana") == 0
    assert linux_runner.run(mirror, values, "hana", "check", (), "call", "tree") == 0
"""
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-I", "-c", script,
            str(Path(__file__).resolve().parents[2]), str(self.root)],
            cwd=self.root, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_queue_demand_respects_the_configured_remote_kinds(self) -> None:
        self.replace("kinds", "doc")
        self.snapshot(self.waiter(101, step="check"), self.waiter(102, step="test"))
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.snapshot(self.waiter(101, step="doc"), self.waiter(102, step="doc"))
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 1)

    def test_cli_reads_the_queue_from_the_configured_buildlog_directory(self) -> None:
        self.demand()
        output = self.root / "cli-output"
        controller_type = aws_target.Controller

        def controller(settings: Path, directory: Path, queue: Path) -> aws_target.Controller:
            return controller_type(settings, directory, queue, execute=self.aws, clock=self.clock,
                                   notify=lambda _event: True, publish=self.publish)

        with (
            mock.patch.object(sys, "argv", ["aws_target.py", "tick", "--settings", str(self.settings),
                                          "--directory", str(self.state)]),
            mock.patch.object(aws_target, "Controller", side_effect=controller),
            output.open("w") as stream,
            mock.patch.object(sys, "stdout", stream),
        ):
            self.assertEqual(aws_target.main(), 0)
        self.assertEqual(self.aws.count("start-instances"), 1)
        self.assertEqual(cast(dict[str, object], json.loads(output.read_text()))["state"], "starting")

    def test_publication_uses_shared_connection_options_and_one_settings_read(self) -> None:
        self.aws.state = "running"
        self.replace("connect_timeout_s", "7")
        controller = aws_target.Controller(self.settings, self.state, self.queue_directory,
            execute=self.aws, clock=self.clock, notify=lambda _event: True)
        with (
            mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as publish,
            mock.patch.object(aws_target, "read_control_settings", wraps=settings_config.read_control_settings) as read,
        ):
            self.assertEqual(controller.tick().state, "running")
        call = publish.call_args
        self.assertIsNotNone(call)
        assert call is not None
        arguments = cast(list[str], call.args[0])
        self.assertEqual(arguments[0], "ssh")
        self.assertIn("ConnectTimeout=7", arguments)
        self.assertIn("ServerAliveInterval=15", arguments)
        self.assertIn("ServerAliveCountMax=3", arguments)
        self.assertIn("aws-build", arguments)
        self.assertEqual(read.call_count, 1)

    def test_stopped_tick_without_claims_reads_claim_records_once(self) -> None:
        with mock.patch.object(mac_test, "read_claims", wraps=mac_test.read_claims) as read:
            self.assertEqual(self.controller().tick().state, "stopped")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.assertEqual(self.aws.count("stop-instances"), 0)

    def test_old_eligible_queue_rows_start_one_stopped_instance(self) -> None:
        self.demand()
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 1)
        self.assertGreaterEqual(self.aws.count("describe-instances"), 1)
        command = next(words for words in self.aws.calls if "start-instances" in words)
        self.assertIn("build-scaler", command)
        self.assertIn("us-east-1", command)
        self.assertIn("i-fixture", command)
        book = ledger.CostLedger(self.state)
        record = cast(dict[str, object], json.loads(book.path.read_text()))
        self.assertEqual(cast(dict[str, object], record["decision"])["status"], "allowed")

    def test_young_and_too_few_waiters_do_not_start_capacity(self) -> None:
        self.snapshot(self.waiter(101, age=119), self.waiter(102, age=119))
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.snapshot(self.waiter(101))
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)

    def test_disallowed_kinds_unknown_repositories_and_token_waiters_do_not_supply_demand(self) -> None:
        self.replace("kinds", "test,check,doc")
        token = self.waiter(104)
        token["token_left_at"] = self.clock.now - 300
        self.snapshot(self.waiter(101, step="build"), self.waiter(102, step="lint"),
                      self.waiter(103, step="final"), token, self.waiter(105, identity=""),
                      self.waiter(106, step="example"))
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)

    def test_missing_stale_and_partial_snapshots_refuse_start_without_live_queue_reads(self) -> None:
        with mock.patch("scripts.build_queue.admission_client.view", side_effect=AssertionError("live queue read")):
            _ = self.controller().tick()
            self.snapshot(self.waiter(101), self.waiter(102), age=86400)
            _ = self.controller().tick()
            _ = (self.queue_directory / "queue.json").write_text('{"v":4,"written_at":0}')
            _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)

    def test_disabled_configuration_makes_no_aws_call_and_keeps_the_ledger(self) -> None:
        self.demand()
        self.replace("offload", "off")
        book = ledger.CostLedger(self.state)
        before = book.path.read_bytes()
        controller = self.controller()
        _ = controller.tick()
        _ = controller.start()
        _ = controller.stop()
        self.assertEqual(self.aws.calls, [])
        self.assertEqual(book.path.read_bytes(), before)

    def test_capacity_failure_respects_its_saved_backoff_after_restart(self) -> None:
        self.demand()
        self.aws.capacity_miss = True
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 1)
        self.clock.advance(299)
        self.demand()
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 1)
        self.clock.advance(2)
        self.demand()
        self.aws.capacity_miss = False
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 2)

    def test_concurrent_tick_and_manual_start_serialize_the_instance_action(self) -> None:
        self.demand()
        operations: tuple[Callable[[], object], ...] = (self.controller().tick, self.controller().start)
        def perform(operation: Callable[[], object]) -> object:
            return operation()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(perform, operations))
        self.assertEqual(len(results), 2)
        self.assertEqual(self.aws.count("start-instances"), 1)

    def test_api_uncertainty_refuses_start_and_later_success_keeps_accounting_evidence(self) -> None:
        self.demand()
        book = ledger.CostLedger(self.state)
        before = cast(dict[str, object], json.loads(book.path.read_text()))
        self.aws.unavailable = True
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.aws.unavailable = False
        self.clock.advance(1)
        self.demand()
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 1)
        after = cast(dict[str, object], json.loads(book.path.read_text()))
        self.assertEqual(after["inventories"], before["inventories"])
        self.assertTrue(self.documents())

    def test_malformed_instance_replies_refuse_start_without_destroying_accounting_evidence(self) -> None:
        malformed: tuple[object, ...] = (
            None, [], {"Reservations": None}, {"Reservations": [None]},
            {"Reservations": [{"Instances": "broken"}]},
            {"Reservations": [{"Instances": [{"InstanceId": "i-fixture", "State": None,
                                               "LaunchTime": self.aws.generation}]}]},
        )
        book = ledger.CostLedger(self.state)
        before = book.path.read_bytes()
        for reply in malformed:
            with self.subTest(reply=reply):
                def execute(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if "describe-instances" in arguments:
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), "")
                    return self.aws(arguments, **options)

                controller = aws_target.Controller(self.settings, self.state, self.queue_directory,
                    execute=execute, clock=self.clock, notify=lambda _event: True, publish=self.publish)
                self.assertEqual(controller.start().state, "refused")
                self.assertEqual(self.aws.count("start-instances"), 0)
                self.assertEqual(book.path.read_bytes(), before)
                saved = cast(dict[str, object], json.loads((self.state / "controller.json").read_text()))
                failure = cast(dict[str, object], saved["api_failure"])
                self.assertEqual(failure["at"], self.clock.now)
                self.assertTrue(failure["reason"])

    def test_both_limits_refuse_manual_and_queued_starts(self) -> None:
        self.demand()
        for key in ("daily_limit_usd", "monthly_limit_usd"):
            with self.subTest(limit=key):
                self.replace(key, "3")
                _ = self.controller().start()
                _ = self.controller().tick()
                self.assertEqual(self.aws.count("start-instances"), 0)
                self.replace(key, "20" if key == "daily_limit_usd" else "400")

    def test_failed_decision_write_never_reuses_an_earlier_allowed_permission(self) -> None:
        self.demand()
        book = ledger.CostLedger(self.state)
        self.assertIsInstance(book.guard(self.settings, "start", self.clock.now, lambda _event: True),
                              ledger.SpendingAllowed)
        with mock.patch.object(ledger, "write_text_atomic", side_effect=OSError("decision disk full")):
            _ = self.controller().start()
            _ = self.controller().tick()
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.assertFalse(any(isinstance(item.permission, ledger.PermissionAllowed) for item in self.publications))

    def test_failed_current_decision_write_cannot_republish_an_earlier_allowed_running_decision(self) -> None:
        self.aws.state = "running"
        self.assertEqual(self.controller().tick().state, "running")
        _ = self.retained_claim()
        self.publications.clear()
        self.clock.advance(1)

        def fail_current_decision(path: Path, text: str) -> None:
            row = cast(dict[str, object], json.loads(text))
            decision = cast(dict[str, object], row.get("decision", {}))
            if decision.get("at") == self.clock.now:
                raise OSError("current decision write failed")
            write_text_atomic(path, text)

        with mock.patch.object(ledger, "write_text_atomic", side_effect=fail_current_decision):
            _ = self.controller().tick()
        self.assertTrue(self.publications)
        self.assertTrue(all(isinstance(envelope.permission, ledger.PermissionUnavailable)
                            for envelope in self.publications))
        self.assertEqual(self.aws.count("start-instances"), 0)
        saved = cast(dict[str, object], json.loads(ledger.CostLedger(self.state).path.read_text()))
        self.assertEqual(cast(dict[str, object], saved["decision"])["at"], self.clock.now - 1)
        self.assertTrue(saved["inventories"])

    def test_repeated_limit_ticks_preserve_the_original_controlled_drain_deadline(self) -> None:
        self.aws.state = "running"
        _ = self.controller().tick()
        claim = self.retained_claim()
        self.replace("drain_max_minutes", "1")
        self.replace("daily_limit_usd", "3")
        first = self.controller().tick()
        self.assertEqual(first.state, "draining")
        controller_path = self.state / "controller.json"
        deadline = cast(dict[str, object], json.loads(controller_path.read_text()))["drain_deadline"]
        self.clock.advance(30)
        self.assertEqual(self.controller().tick().state, "draining")
        self.assertEqual(cast(dict[str, object], json.loads(controller_path.read_text()))["drain_deadline"], deadline)
        self.clock.advance(29)
        self.assertEqual(self.controller().tick().state, "draining")
        self.assertTrue(claim.exists())
        self.assertEqual(self.aws.count("stop-instances"), 0)
        self.clock.advance(1)
        self.assertEqual(self.controller().tick().state, "stopping")
        self.assertEqual(self.aws.count("stop-instances"), 1)
        self.assertFalse(claim.exists())

    def test_unexpected_running_generation_stops_immediately_when_spending_is_refused(self) -> None:
        self.aws.state = "running"
        claim = self.retained_claim()
        self.replace("daily_limit_usd", "3")
        result = self.controller().tick()
        self.assertEqual(result.state, "stopping")
        self.assertEqual(self.aws.count("stop-instances"), 1)
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.assertFalse(claim.exists())

    def test_manual_stop_records_confirmed_instance_evidence_before_claim_release(self) -> None:
        self.aws.state = "running"
        claim = self.retained_claim()
        result = self.controller().stop()
        self.assertEqual(result.state, "stopping")
        self.assertFalse(claim.exists())
        proof_path = mac_test.state_paths("aws").directory / "release-evidence" / claim.name
        proof = cast(dict[str, object], json.loads(proof_path.read_text()))
        self.assertEqual((proof["target"], proof["state"]), ("aws", "stopping"))
        self.assertEqual(proof["generation"], "i-fixture:" + self.aws.generation)
        self.assertEqual(proof["observed_at"], self.clock.now)

    def test_running_new_generation_releases_only_ended_host_claims_with_proof(self) -> None:
        self.aws.state = "running"
        paths = mac_test.state_paths("aws")
        old = self.claim(paths, generation="previous-generation", at=self.clock.now)
        current = self.claim(paths, slot=2, generation="i-fixture:" + self.aws.generation)
        later = self.claim(paths, slot=3, generation="previous-generation", at=self.clock.now + 1)
        legacy = self.claim(paths, slot=4)
        foreign = self.claim(paths, slot=5, generation="previous-generation")
        row = cast(dict[str, object], json.loads(legacy.read_text()))
        del row['instance_generation']
        _ = legacy.write_text(json.dumps(row))
        row = cast(dict[str, object], json.loads(foreign.read_text()))
        row['remote_host'] = 'other-host'
        _ = foreign.write_text(json.dumps(row))
        before = {path: path.read_bytes() for path in (current, later, legacy, foreign)}

        self.assertEqual(self.controller().tick().state, "running")

        self.assertFalse(old.exists())
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)
        proofs = list((paths.directory / "release-evidence").iterdir())
        self.assertEqual([path.name for path in proofs], [old.name])
        proof = cast(dict[str, object], json.loads(proofs[0].read_text()))
        self.assertEqual(proof['state'], 'running')
        self.assertEqual(proof['generation'], 'i-fixture:' + self.aws.generation)
        self.assertEqual(proof['observed_at'], self.clock.now)
        self.assertEqual(proof['remote_host'], 'aws-build')
        claim = cast(dict[str, object], proof['claim'])
        self.assertEqual(claim['instance_generation'], 'previous-generation')
        self.assertEqual(self.aws.count('stop-instances'), 0)

    def test_a_running_restart_without_queue_demand_never_requests_another_start(self) -> None:
        self.snapshot()
        self.aws.state = "running"
        self.assertEqual(self.controller().tick().state, "running")
        self.assertEqual(self.aws.count("start-instances"), 0)
        self.assertTrue(self.publications)
        self.assertIsInstance(self.publications[-1].permission, ledger.PermissionAllowed)

    def test_temporary_api_failure_allows_new_permission_after_reconciliation(self) -> None:
        self.aws.state = "running"
        _ = self.controller().tick()
        before = ledger.CostLedger(self.state).path.read_bytes()
        self.publications.clear()
        self.aws.unavailable = True
        self.assertEqual(self.controller().tick().state, "refused")
        self.assertEqual(self.publications, [])
        self.assertEqual(ledger.CostLedger(self.state).path.read_bytes(), before)
        self.aws.unavailable = False
        self.clock.advance(1)
        self.assertEqual(self.controller().tick().state, "running")
        self.assertIsInstance(self.publications[-1].permission, ledger.PermissionAllowed)

    def test_routine_ticks_refresh_prices_only_after_the_configured_interval(self) -> None:
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("describe-spot-price-history"), 1)
        self.clock.advance(3599)
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("describe-spot-price-history"), 1)
        self.clock.advance(1)
        _ = self.controller().tick()
        self.assertEqual(self.aws.count("describe-spot-price-history"), 2)


if __name__ == "__main__":
    _ = unittest.main()
