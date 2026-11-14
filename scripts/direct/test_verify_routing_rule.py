"""Check the routing rule and the commands that remote runners receive."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import cast, override

from scripts.build_targets import build_targets, targets
from scripts.direct.verify_ssh_targets_test_support import VerifySshTargetsFixture
from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture
from scripts.direct.verify_mac_offload_test_support import (
    CARGO_STUB, RUNNER_STUB, VERIFY, VerifyMacOffloadFixture,
)
from scripts.direct.test_verify_result_environment import FIXED_ENVIRONMENT
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

from scripts.direct.step_selection import Pinned, ROUTED


class RoutingRuleTests(unittest.TestCase):
    def test_routing_flag_errors_describe_commands_and_configured_targets(self) -> None:
        parser = shell_functions(Path(__file__).with_name("verify.sh"), "parse_routing_arguments")
        cases = (
            ("--target", "--target needs one configured target, including natedev."),
            ("--mac", '--mac needs a reason: --mac "<why this command must run on macOS>".'),
            ("--local", '--local needs a reason: --local "<why this command must run on natedev>".'),
        )
        with tempfile.TemporaryDirectory() as directory:
            for flag, message in cases:
                with self.subTest(flag=flag):
                    result = subprocess.run(["bash", "-c", parser + '\nCMD=build\nparse_routing_arguments "$@"',
                        "verify-parser", flag], cwd=directory, capture_output=True, text=True,
                        check=False, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(message, result.stderr)

    def test_each_command_uses_the_same_rule_and_accepts_each_pin(self) -> None:
        commands: tuple[build_targets.StepKind, ...] = ("check", "test", "doc", "lint", "example", "build", "final")
        for command in commands:
            for pin in ("", "--target", "--mac", "--local"):
                with self.subTest(command=command, pin=pin):
                    routing = targets.LocalRequired("local files") if pin == "--local" else targets.ANY_ELIGIBLE_TARGET
                    selection = Pinned("mac" if pin == "--mac" else "linux") if pin in ("--mac", "--target") else ROUTED
                    answer = build_targets.step_routing_rule(command, routing, selection)
                    local = pin == "--local" or command in ("build", "final") and not pin
                    self.assertIsInstance(answer, build_targets.NatedevOnly if local else build_targets.AnyTarget)
                    writing = build_targets.step_routing_rule(command, routing, selection, writes_tree=True)
                    self.assertIsInstance(writing, build_targets.NatedevOnly)

        parser = shell_functions(Path(__file__).with_name("verify.sh"), "parse_routing_arguments") + r'''
VERIFY_CALL_ENVIRONMENT=()
for CMD in check test doc lint example build final; do
    for flag in --target --mac --local; do
        value=linux
        [[ "$flag" != --mac ]] || value='macOS API'
        [[ "$flag" != --local ]] || value='local files'
        parse_routing_arguments sample "$flag" "$value"
        printf '%s %s %s %s %s\n' "$CMD" "$flag" "$PINNED_TARGET" "$MAC_ONLY" "$LOCAL_ONLY"
    done
done
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(["bash", "--noprofile", "--norc", "-c", parser], cwd=directory,
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"{command} {flag} {target} {mac} {local}"
            for command in commands
            for flag, target, mac, local in (("--target", "linux", 0, 0), ("--mac", "", 1, 0), ("--local", "", 0, 1))
        ])


class RemoteCommandTests(VerifySshTargetsFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        source = self.runner.read_text().replace('request = offload.request_from_arguments(args)',
            'request = offload.request_from_arguments(args)\n'
            + 'import sys\n'
            + 'with (Path(os.environ["TEST_RUNNER_STATE"]) / "received-arguments").open("a") as output:\n'
            + '    output.write(json.dumps(sys.argv[1:]) + "\\n")')
        _ = self.runner.write_text(source)
        _ = self.runner.write_text(self.runner.read_text().replace(
            '    remote_output, remote_status = wire, status',
            '    if request.verb == "build":\n        wire = "build_target_directory=target\\n" + wire\n'
            + '    remote_output, remote_status = wire, status'))

    def received_arguments(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line))
                for line in (self.runner_state / "received-arguments").read_text().splitlines()]

    def test_check_uses_the_first_eligible_linux_target(self) -> None:
        self.configure("linux,natedev")
        result = self.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["target"] for event in self.ssh_events()], ["linux"])
        self.assertIn("run", self.ssh_events()[0]["actions"])
        words, = self.received_arguments()
        self.assertEqual(words[words.index("--verb") + 1], "check")
        self.assertEqual(words[words.index("--kind") + 1], "check")
        self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["check"]], [])

    def test_example_uses_linux_when_its_private_settings_permit_examples(self) -> None:
        self.configure("linux,natedev")
        settings = self.root / "linux_target.conf"
        _ = settings.write_text(settings.read_text().replace("kinds=test,check,doc", "kinds=test,check,doc,example"))
        reader = Path(self.environment["LINT_CONFIG_READER"])
        _ = reader.write_text(reader.read_text() + '\nlint_config_enabled() { [[ "$1" == clippy ]]; }\n')
        result = self.verify("example", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["target"] for event in self.ssh_events()], ["linux"])
        self.assertIn("run", self.ssh_events()[0]["actions"])
        words, = self.received_arguments()
        self.assertEqual(words[words.index("--verb") + 1], "clippy")
        self.assertEqual(words[words.index("--kind") + 1], "example")
        self.assertIn("--examples", words[words.index("--") + 1:])
        self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["clippy"]], [])

    def test_pinned_final_sends_both_steps_to_the_target_without_a_saved_pass(self) -> None:
        self.configure("linux,natedev")
        git = self.bin_directory / "git"
        _ = git.write_text(git.read_text().replace("wanted=ls-files", "wanted=HEAD\n"
            + "if has \"$@\"; then printf 'fixture-commit\\n'; exit 0; fi\nwanted=ls-files"))
        result = self.verify("final", "sample", "--target", "linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.received_arguments()
        self.assertEqual(len(arguments), 2)
        self.assertEqual([words[words.index("--kind") + 1] for words in arguments], ["check", "test"])
        self.assertEqual(arguments[0][arguments[0].index("--verb") + 1], "check")
        self.assertNotIn("--verb", arguments[1])
        self.assertEqual([event for event in self.cargo_events()
                          if event["args"][:1] == ["check"] or event["args"][:2] == ["nextest", "run"]], [])
        self.assertEqual([event["args"] for event in self.cargo_events()
                          if event["args"][:1] == ["nextest"]], [["nextest", "--version"]])
        state = Path(self.environment.get("VERIFY_STATE_DIR", str(Path(self.environment["HOME"]) / ".local/state/verify")))
        self.assertEqual(list((state / "final_pass").glob("*.json")), [])

    def test_pinned_build_reports_the_remote_binary(self) -> None:
        self.configure("linux,natedev")
        settings = self.root / "linux_target.conf"
        _ = settings.write_text(settings.read_text().replace("kinds=test,check,doc", "kinds=test,check,doc,build"))
        metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
        metadata["target_directory"] = str(self.root / "target")
        packages = cast(list[dict[str, object]], metadata["packages"])
        targets = cast(list[dict[str, object]], packages[0]["targets"])
        targets.append({"name": "sample-cli", "kind": ["bin"], "src_path": str(self.root / "src/main.rs")})
        self.environment["TEST_METADATA"] = json.dumps(metadata)
        result = self.verify("build", "sample", "--target", "linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["target"] for event in self.ssh_events()], ["linux"])
        words, = self.received_arguments()
        self.assertEqual(words[words.index("--verb") + 1], "build")
        self.assertEqual(words[words.index("--kind") + 1], "build")
        self.assertIn("binary: linux:target/debug/sample-cli", result.stdout)
        self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["build"]], [])

    def test_pinned_build_uses_the_runner_target_directory(self) -> None:
        self.configure("linux,natedev")
        settings = self.root / "linux_target.conf"
        _ = settings.write_text(settings.read_text().replace("kinds=test,check,doc", "kinds=test,check,doc,build"))
        metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
        metadata["target_directory"] = str(self.root / "local-target")
        packages = cast(list[dict[str, object]], metadata["packages"])
        targets = cast(list[dict[str, object]], packages[0]["targets"])
        targets.append({"name": "sample-cli", "kind": ["bin"], "src_path": str(self.root / "src/main.rs")})
        self.environment["TEST_METADATA"] = json.dumps(metadata)
        _ = self.runner.write_text(self.runner.read_text().replace(
            'build_target_directory=target', 'build_target_directory=/remote/cargo output'))
        result = self.verify("build", "sample", "--target", "linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("binary: linux:/remote/cargo output/debug/sample-cli", result.stdout)
        self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["build"]], [])


class FinalRemoteEnvironmentTests(VerifyMacOffloadFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        observe = f'''with open({str(VERIFY.parents[2] / "scripts/lib/package_root.py")!r}) as package_root: exec(package_root.read(), {{"__file__": package_root.name}})
from scripts.mac_test import eligibility, offload
parsed = offload.parser().parse_args(namespace=offload.CliArguments())
request = offload.request_from_arguments(parsed)
config = eligibility.read_config(Path(parsed.settings))
command = offload.remote_test_command(config, request, eligibility.KnownRepository("hana"))
with Path(os.environ["TEST_RUNNER_STATE"], "remote-commands").open("a") as output:
    output.write(json.dumps({{"verb": request.verb, "environment": dict(request.environment),
                             "command": command, "copy": parsed.copy}}) + "\\n")
'''
        self.write_executable(self.runner, RUNNER_STUB.replace(
            'arguments = sys.argv[1:]', observe + '\narguments = sys.argv[1:]', 1))
        git = self.bin_directory / "git"
        _ = git.write_text(git.read_text().replace("wanted=ls-files", "wanted=HEAD\n"
            + "if has \"$@\"; then printf 'fixture-commit\\n'; exit 0; fi\nwanted=ls-files"))
        self.environment.update({"NO_COLOR": "1", "RUST_LOG": "sample=trace",
                                 **{name: "caller" for name in FIXED_ENVIRONMENT},
                                 "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        _ = write_config_fixture(self.root / "linux.conf", "offload=on\nhost=fake-linux\nslots=1\nkinds=test,check,doc\n",
                                 template="mac_test")
        targets_file = self.root / "build_targets.conf"
        _ = targets_file.write_text(targets_file.read_text().replace("targets=mac,natedev", "targets=mac,natedev,peer")
            + "kind.peer=ssh\nplatform.peer=linux\nsettings.peer=linux.conf\n")

    def assert_remote_environment(self, *, copied: bool) -> None:
        rows = [cast(dict[str, object], json.loads(line))
                for line in (self.runner_state / "remote-commands").read_text().splitlines()]
        self.assertEqual([row["verb"] for row in rows], ["nextest"] if copied else ["check", "nextest"])
        if not copied:
            self.assertEqual(rows[0]["environment"], {})
        self.assertEqual(rows[-1]["copy"], copied)
        environment = cast(dict[str, str], rows[-1]["environment"])
        words = shlex.split(cast(str, rows[-1]["command"]))
        for name, value in {"NO_COLOR": "1", "RUST_LOG": "sample=trace", **FIXED_ENVIRONMENT}.items():
            self.assertEqual(environment[name], value)
            self.assertIn(name + "=" + value, words)
            self.assertNotIn(name + "=caller", words)
            if not copied:
                self.assertNotIn(name + "=" + value, shlex.split(cast(str, rows[0]["command"])))

    def test_pinned_final_forwards_the_test_environment_only_to_remote_nextest(self) -> None:
        for target in ("mac", "peer"):
            with self.subTest(target=target):
                (self.runner_state / "remote-commands").unlink(missing_ok=True)
                result = self.verify("final", "sample", "--target", target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_remote_environment(copied=False)

    def test_unpinned_final_runner_calls_forward_the_environment_only_to_nextest(self) -> None:
        values = {"NO_COLOR": "1", "RUST_LOG": "sample=trace", **FIXED_ENVIRONMENT}
        entries = " ".join(shlex.quote(name + "=" + value) for name, value in values.items())
        for target, platform, settings in (("mac", "macos", self.environment["MAC_TEST_CONFIG"]),
                                           ("peer", "linux", str(self.root / "linux.conf"))):
            with self.subTest(target=target):
                (self.runner_state / "remote-commands").unlink(missing_ok=True)
                # Unpinned final stays local. Call its remote boundary without changing that rule.
                for verb in ("check", "nextest"):
                    assigned = self.assigned_remote(target, verb, "final")
                    result = self.call_mac_functions('CMD=final MAC_FINISH_CALL=0 CACHE_DIR=""\n'
                        + f'VERIFY_TEST_ENVIRONMENT=({entries})\n'
                        + 'PLACEMENT_ASSIGNMENT_FILE="$TEST_DIRECT_ASSIGNMENT"\n'
                        + f'STEP_ASSIGNED_TARGET={shlex.quote(target)} STEP_ASSIGNED_PLATFORM={platform}\n'
                        + f'MAC_VERB={verb}\ntry_mac_test "$MAC_TEST_CONFIG"\n',
                        extra={"MAC_TEST_CONFIG": settings, "TEST_DIRECT_ASSIGNMENT": str(assigned)})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_remote_environment(copied=False)

    def test_final_exports_the_test_environment_to_a_copy_runner(self) -> None:
        # The Cargo consumer starts a real copy runner with the final step's environment.
        copy = f'''    with open({str(VERIFY.parents[2] / "scripts/lib/package_root.py")!r}) as package_root: exec(package_root.read(), {{"__file__": package_root.name}})
    from scripts.mac_test import copy_watch
    request = copy_watch.WatchRequest(
        info=root / "info", result=root / "copy-result",
        runner=Path(os.environ["VERIFY_MAC_RUNNER"]),
        settings=Path(os.environ["MAC_TEST_CONFIG"]),
        repo_root=Path(os.environ["PACKAGE_ROOT"]), package="sample",
        call_id=os.environ["BUILDLOG_CALL_ID"], call_started=0,
        filter_run=False, nextest_words=tuple(arguments[2:]))
    running = copy_watch.start_copy(request, root / "copy-result", Path(os.environ["TEST_COPY_ASSIGNMENT"]))
    raise SystemExit(running.process.wait())
'''
        source = CARGO_STUB.replace('if arguments[:1] == ["metadata"]:',
            'root = Path(os.environ["TEST_RUNNER_STATE"])\n'
            + 'if arguments[:1] == ["check"]:\n'
            + '    (root / "check-environment").write_text(json.dumps(dict(os.environ)))\n'
            + 'if arguments[:1] == ["metadata"]:', 1)
        source = source.replace('if arguments[:2] == ["nextest", "run"]:\n',
                                'if arguments[:2] == ["nextest", "run"]:\n' + copy, 1)
        self.write_executable(self.bin_directory / "cargo", source)
        for flags in ((), ("--target", "natedev")):
            with self.subTest(flags=flags):
                (self.runner_state / "remote-commands").unlink(missing_ok=True)
                assigned = self.assigned_remote("mac", "nextest")
                result = self.verify("final", "sample", *flags,
                    extra={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0", "TEST_COPY_ASSIGNMENT": str(assigned)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_remote_environment(copied=True)
                checked = cast(dict[str, str], json.loads((self.runner_state / "check-environment").read_text()))
                self.assertEqual({name: checked[name] for name in FIXED_ENVIRONMENT},
                                 {name: "caller" for name in FIXED_ENVIRONMENT})


class PinnedLintTests(VerifyStepRoutesFixture):
    def test_natedev_pin_waits_for_a_hold_and_runs_only_locally(self) -> None:
        self.fixture.environment.update({"TEST_MAC_ALLOWED": "0", "TEST_ASSERT_LOCAL_HELD": "1"})
        result = self.run_verify("test", "sample", "--target", "natedev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [cast(dict[str, object], json.loads(line))["command"]
                    for line in self.events.read_text().splitlines()]
        self.assertNotIn("wait-local", commands)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertEqual(self.mac_rows(), [])

    def test_mac_lint_refusal_does_not_deny_the_local_repair(self) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        runner = Path(self.fixture.environment["VERIFY_MAC_RUNNER"])
        _ = runner.write_text(runner.read_text().replace(
            'mac=passed\\nseconds=0\\n', 'mac=declined\\nreason=unreachable\\nseconds=0\\n'))
        result = self.run_verify("lint", "sample", "--mac", "macOS API")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(any("mend" in event["args"] and "--fix" in event["args"]
                            for event in self.fixture.cargo_events()))
        self.assertIn("lint --mac refused", result.stderr)
        self.assertNotIn("nothing ran on natedev", result.stderr)

    def test_mac_pin_keeps_repairs_on_natedev_and_judges_on_the_mac(self) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        result = self.run_verify("lint", "sample", "--mac", "macOS API")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        local = [event["args"] for event in self.fixture.cargo_events()]
        self.assertTrue(any("mend" in words and "--fix" in words for words in local), local)
        verbs: list[str] = []
        for row in self.mac_rows():
            words = cast(list[str], row["args"])
            self.assertNotIn("--fix", words)
            verbs.append(words[words.index("--verb") + 1])
        self.assertEqual(verbs, ["clippy"])
        self.assert_removed()


if __name__ == "__main__":
    _ = unittest.main()
