"""Check saved results and test environments through the real verify command."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
from typing import cast, override

from scripts.copies.copies_test_support import copies_environment
from scripts.direct.verify_mac_offload_test_support import (
    CARGO_STUB, RUNNER_STUB, VERIFY, VerifyMacOffloadFixture,
)
from scripts.lint.memory_test_support import write_config_fixture


FIXED_ENVIRONMENT = {
    "TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "verify",
    "COLUMNS": "80", "LINES": "24",
}


class VerifyResultEnvironmentTests(VerifyMacOffloadFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment.update({name: value for name, value in copies_environment(self.root).items()
                                 if name != "HOME"})
        for name in ("NO_COLOR", "CLICOLOR"):
            _ = self.environment.pop(name, None)
        cargo = CARGO_STUB.replace(
            'if arguments[:1] == ["metadata"]:',
            'if arguments[:1] == ["check"]:\n'
            + '    Path(os.environ["TEST_RUNNER_STATE"], "check-environment.json").write_text(json.dumps(dict(os.environ)))\n'
            + 'if arguments[:1] == ["metadata"]:',
        ).replace(
            'if arguments[:2] == ["nextest", "run"]:\n    raise',
            'if arguments[:2] == ["nextest", "run"]:\n'
            + '    Path(os.environ["TEST_RUNNER_STATE"], "test-environment.json").write_text(json.dumps(dict(os.environ)))\n'
            + '    raise',
        )
        self.write_executable(self.bin_directory / "cargo", cargo)
        observe = f'''sys.path.insert(0, {str(VERIFY.parents[2])!r})
sys.path.insert(0, {str(VERIFY.parent.parent / "mac_test")!r})
from scripts.mac_test import offload
parsed = offload.parser().parse_args(namespace=offload.CliArguments())
request = offload.request_from_arguments(parsed)
config = offload.read_config(Path(parsed.settings))
command = offload.remote_test_command(config, request, offload.KnownRepository("hana"))
Path(os.environ["TEST_RUNNER_STATE"], "remote-command").write_text(command)
'''
        self.write_executable(self.runner, RUNNER_STUB.replace(
            'arguments = sys.argv[1:]', observe + '\narguments = sys.argv[1:]', 1))

    def test_result_values_distinguish_unset_empty_and_changed_values_in_saved_passes(self) -> None:
        for name in ("NO_COLOR", "CLICOLOR"):
            with self.subTest(name=name):
                arguments = ("test", "sample", "--filter", name, "--local", "Linux test")
                self.clear_observations()
                first = self.verify(*arguments, extra={name: "1"})
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.clear_observations()
                repeat = self.verify(*arguments, extra={name: "1"})
                self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
                self.assertIn("PASS (recorded)", repeat.stdout)
                self.assertEqual(self.local_runs(), [])
                self.clear_observations()
                empty = self.verify(*arguments, extra={name: ""})
                self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.clear_observations()
                absent = self.verify(*arguments)
                self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
                self.assertEqual(len(self.local_runs()), 1)

    def test_empty_environment_policy_reaches_cargo_and_passes(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "direct.conf",
            "VERIFY_RESULT_ENV_NAMES=\nVERIFY_RESULT_ENV_PREFIXES=\nVERIFY_TEST_FIXED_ENV=\n")
        result = self.verify("test", "sample", "--local", "Linux test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)

    def test_terminal_values_are_fixed_at_local_cargo_and_do_not_invalidate_a_pass(self) -> None:
        first_values = {name: "caller-one" for name in FIXED_ENVIRONMENT}
        first = self.verify("test", "sample", "--local", "Linux test", extra=first_values)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        observed = cast(dict[str, str], json.loads((self.runner_state / "test-environment.json").read_text()))
        self.assertEqual({name: observed[name] for name in FIXED_ENVIRONMENT}, FIXED_ENVIRONMENT)
        self.clear_observations()
        second = self.verify("test", "sample", "--local", "Linux test",
                             extra={name: "caller-two" for name in FIXED_ENVIRONMENT})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("PASS (recorded)", second.stdout)
        self.assertEqual(self.local_runs(), [])

    def test_changed_forwarded_value_reaches_the_mac_and_runs_again(self) -> None:
        for value in ("one", "two"):
            with self.subTest(value=value):
                self.clear_observations()
                result = self.verify("test", "sample", "--mac", "Mac test", "--env", "SNAPSHOT_MODE", extra={
                    "SNAPSHOT_MODE": value, "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.runner_events()), 1)
                words = shlex.split((self.runner_state / "remote-command").read_text())
                self.assertIn("SNAPSHOT_MODE=" + value, words)

    def test_mac_routes_carry_result_values_and_fixed_terminal_values(self) -> None:
        for routing in ((), ("--mac", "Mac test"), ("--target", "mac")):
            with self.subTest(routing=routing):
                self.clear_observations()
                result = self.verify("test", "sample", *routing, "--no-cache", extra={
                    "NO_COLOR": "1", "RUST_LOG": "sample=trace",
                    **{name: "caller" for name in FIXED_ENVIRONMENT},
                    "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.runner_events()), 1)
                words = shlex.split((self.runner_state / "remote-command").read_text())
                for name, value in {"NO_COLOR": "1", "RUST_LOG": "sample=trace", **FIXED_ENVIRONMENT}.items():
                    self.assertIn(name + "=" + value, words)
                    self.assertNotIn(name + "=caller", words)

    def test_ssh_route_carries_result_values_and_fixed_terminal_values(self) -> None:
        settings = self.root / "linux.conf"
        _ = write_config_fixture(settings, "offload=on\nhost=fake-linux\nslots=1\n", template="aws_target")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=peer,natedev\nkind.peer=ssh\nplatform.peer=linux\nsettings.peer=linux.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n")
        result = self.verify("test", "sample", "--target", "peer", extra={
            "NO_COLOR": "1", **{name: "caller" for name in FIXED_ENVIRONMENT},
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        words = shlex.split((self.runner_state / "remote-command").read_text())
        for name, value in {"NO_COLOR": "1", **FIXED_ENVIRONMENT}.items():
            self.assertIn(name + "=" + value, words)
            self.assertNotIn(name + "=caller", words)

    def test_final_sets_terminal_values_only_for_its_test_step(self) -> None:
        result = self.verify("final", "sample", extra={name: "caller" for name in FIXED_ENVIRONMENT})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checked = cast(dict[str, str], json.loads((self.runner_state / "check-environment.json").read_text()))
        self.assertEqual({name: checked[name] for name in FIXED_ENVIRONMENT},
                         {name: "caller" for name in FIXED_ENVIRONMENT})
        observed = cast(dict[str, str], json.loads((self.runner_state / "test-environment.json").read_text()))
        self.assertEqual({name: observed[name] for name in FIXED_ENVIRONMENT}, FIXED_ENVIRONMENT)
