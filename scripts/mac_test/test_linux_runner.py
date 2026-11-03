"""Check Linux shell caching, execution, and retained claim cleanup."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Literal, NamedTuple, TypedDict, cast, override
import unittest
from unittest import mock

if TYPE_CHECKING:
    from ..aws_target.runner_test_support import seed_runner_permission
    from . import linux_runner, mac_test, offload, test_target_slots
    from ..lint import sweep
    from ..lint.memory_test_support import write_config_fixture
else:
    from scripts.aws_target.runner_test_support import seed_runner_permission
    import linux_runner
    import mac_test
    import offload
    import test_target_slots
    import sweep
    from scripts.lint.memory_test_support import write_config_fixture


class DiskReading(NamedTuple):
    total: int
    used: int
    free: int


MISSING_PROCESS_CWD = Path()


class ToolEvent(TypedDict):
    tool: str
    args: list[str]
    cwd: str
    inputs: list[str]
    environment: dict[str, str]
    warm: bool


TOOL_STUB = r'''
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote

tool = Path(sys.argv[0]).name
arguments = sys.argv[1:]
inputs = []
if tool == "nix":
    for argument in arguments:
        if "#" in argument:
            folder = Path(unquote(argument.split("#", 1)[0].removeprefix("path:")))
            if folder.is_dir():
                inputs = sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())
event = {
    "tool": tool, "args": arguments, "cwd": str(Path.cwd()), "inputs": inputs,
    "environment": {key: value for key, value in os.environ.items()
                    if key.startswith(("BUILDLOG_", "LINT_", "TEST_SHELL_"))},
    "warm": (Path.cwd() / "target/warm-proof").exists(),
}
with Path(os.environ["TOOL_EVENTS"]).open("a") as output:
    output.write(json.dumps(event) + "\n")
if tool == "nix":
    if "print-dev-env" not in arguments:
        raise SystemExit("Unexpected nix command")
    print("export IN_NIX_SHELL=1\nexport TEST_SHELL_PROOF=loaded")
    print(os.environ.get("FAKE_NIX_EXPORTS", ""))
    raise SystemExit(int(os.environ.get("FAKE_NIX_STATUS", "0")))
if tool == "lint":
    target = Path(os.environ.get("CARGO_TARGET_DIR", "target"))
    target.mkdir(parents=True, exist_ok=True)
    (target / "warm-proof").write_text("warm\n")
    raise SystemExit(int(os.environ.get("FAKE_LINT_STATUS", "0")))
if tool == "df":
    if "-g" in arguments:
        raise SystemExit("Linux must not use df -g")
    free = int(os.environ.get("FAKE_FREE_GIB", "100"))
    if any("G" in argument for argument in arguments):
        available = str(free) + "G"
    elif "-B1" in arguments:
        available = str(free * 1024 ** 3)
    else:
        available = str(free * 1024 ** 2)
    print("Filesystem Blocks Used Available Use% Mounted on")
    print("fixture 999999999 0 " + available + " 1% /")
    raise SystemExit(0)
if tool == "rustc":
    assert arguments == ["-V"]
    print("rustc " + os.environ.get("TEST_SHELL_TOOL_VERSION", "fixture"))
    raise SystemExit(0)
if tool == "cargo":
    assert arguments in (["nextest", "--version"], ["mend", "--version"])
    print(arguments[0] + " " + os.environ.get("TEST_SHELL_TOOL_VERSION", "fixture"))
    raise SystemExit(0)
if tool == "hostname":
    print("fake-linux")
    raise SystemExit(0)
if tool == "rm":
    if os.environ.get("FAKE_RM_FAIL") == "1" and any(argument.endswith("/target") for argument in arguments):
        raise SystemExit(9)
    os.execv("/bin/rm", ["rm", *arguments])
raise SystemExit("Unexpected fake tool: " + tool)
'''


class LinuxShellTests(unittest.TestCase):
    permission_at: int = int(time.time())
    root: Path = Path()
    source: Path = Path()
    mirror: Path = Path()
    settings: Path = Path()
    event_path: Path = Path()
    environment: dict[str, str] = {}
    use_real_sweep: bool = False
    identity: offload.KnownRepository = offload.KnownRepository("phase-copy", "https://example.com/team/hana")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linux-runner-")))
        self.source = self.root / "rust/phase-copy"
        self.source.mkdir(parents=True)
        self.settings = self.root / "aws.conf"
        _ = write_config_fixture(self.settings,
            "offload=on\nhost=fake-linux\nkinds=test,check,doc\nslots=2\n"
            + "free_floor_gib=10\nbuild_budget_gib=32\ntarget_budget_gib=24\n"
            + "target_budget_gib.hana=31\ntarget_budget_gib.phase-copy=2\n"
            + "devshell.hana=ci\ndevshell.phase-copy=wrong\nskip.hana=test(gpu)\n"
            + "devshell=default\nshell_cache=.local/state/build-target/shells\nshell_timeout_s=2\n",
            template="mac_test",
        )
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(
            "targets=aws,mac\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws.conf\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n"
        )
        _ = (self.root / "mac.conf").write_text("offload=on\nhost=fake-mac\nlinux_only.phase-copy=\n")
        _ = write_config_fixture(self.root / "lint.conf", "recent_minutes=60\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "BUILD_TARGETS_CONFIG": str(registry), "CONF_DIR": str(self.root),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        self.mirror = self.root / offload.remote_mirror(self.identity, "aws", 2)
        self.mirror.mkdir(parents=True)
        self.write_input("flake.nix", "{ outputs = _: {}; }\n")
        self.write_input("flake.lock", "{}\n")
        self.write_input("nix/shell.nix", "{}\n")
        self.write_input("src/main.rs", "fn main() {}\n")
        self.write_input(".env", "private fixture\n")
        self.write_input(".git/config", "private remote fixture\n")
        self.event_path = self.root / "events.jsonl"
        tools = self.root / "bin"
        tools.mkdir()
        for tool in ("nix", "df", "rm"):
            self.write_tool(tools / tool)
        self.write_tool(self.root / ".claude/scripts/lint/lint")
        seed_runner_permission(self.root, self.settings, self.permission_at, "fixture-boot")
        _ = self.enterContext(mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"))
        self.environment = {
            "HOME": str(self.root), "PATH": f"{tools}:/run/current-system/sw/bin:/usr/bin:/bin",
            "TOOL_EVENTS": str(self.event_path), "BUILD_TARGETS_CONFIG": str(registry),
            "CONF_DIR": str(self.root),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "LINT_SWEEP_STATE_DIR": str(self.root / "sweep-state"),
        }

    def build_output(self, folder: str, name: str, used: float, size: int) -> Path:
        tree = self.mirror / folder / "debug"
        digest = hashlib.sha256(name.encode()).hexdigest()[:16]
        fingerprint = tree / ".fingerprint" / f"{name}-{digest}"
        fingerprint.mkdir(parents=True)
        stamp = fingerprint / f"bin-{name}.json"
        _ = stamp.write_text("{}")
        os.utime(stamp, (used, used))
        os.utime(fingerprint, (used, used))
        output = tree / "deps" / f"{name}-{digest}"
        output.parent.mkdir(parents=True, exist_ok=True)
        _ = output.write_bytes(b"x" * size)
        return output

    def write_tool(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"#!{sys.executable}\n" + TOOL_STUB)
        path.chmod(0o755)

    def write_input(self, name: str, contents: str) -> None:
        for root in (self.source, self.mirror):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(contents)

    def request(self, verb: offload.MacVerb = "nextest", words: tuple[str, ...] = ()) -> offload.OffloadRequest:
        return offload.OffloadRequest(
            self.source, "hana", "call with spaces", offload.NO_RESULT_FILE, words,
            verb=verb, target="aws", slot=2,
        )

    def command(self, request: offload.OffloadRequest) -> str:
        return offload.remote_test_command(
            offload.read_config(self.settings), request, self.identity, offload.KnownTreeKey("sent-tree-key"),
        )

    def execute(self, request: offload.OffloadRequest) -> subprocess.CompletedProcess[str]:
        arguments = shlex.split(self.command(request))
        self.assertEqual(arguments[:3], ["python3", ".claude/scripts/mac_test/linux_runner.py", "run"])
        output, errors = io.StringIO(), io.StringIO()
        free = int(self.environment.get("FAKE_FREE_GIB", "100")) * 1024 ** 3
        real_run = subprocess.run
        real_uses = sweep.process_uses
        proc = self.root / "proc"
        proc.mkdir(exist_ok=True)

        def private_uses(targets: Sequence[str]) -> dict[str, sweep.TargetUse]:
            return real_uses(targets, str(proc))

        def child(argv: list[str], *, cwd: Path | str = "", check: bool = False,
                  capture_output: bool = False, text: bool = False, timeout: float = 5,
                  env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = text
            if argv[:2] == [sys.executable, "-c"] and "sweep.sweep_workspace" in argv[2]:
                if not self.use_real_sweep:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                with mock.patch.dict(os.environ, env or {}), \
                        mock.patch.object(sys, "argv", ["-c", argv[3]]), \
                        mock.patch.object(sweep, "process_uses", side_effect=private_uses), \
                        mock.patch.object(time, "time", return_value=1_793_570_400.0):
                    try:
                        exec(argv[2], {"__name__": "__main__"})
                    except SystemExit as stopped:
                        status = stopped.code if isinstance(stopped.code, int) else 1
                    else:
                        status = 0
                return subprocess.CompletedProcess(argv, status, "", "")
            return real_run(argv, cwd=cwd or self.root, check=check, capture_output=capture_output,
                            text=True, timeout=timeout, env=env)

        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(sys, "argv", arguments[1:]), \
                mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
                mock.patch.object(subprocess, "run", side_effect=child), \
                mock.patch("shutil.disk_usage", return_value=DiskReading(1000 * 1024 ** 3, 0, free)), \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                status = linux_runner.main()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(str(error), file=errors)
                status = 1
        return subprocess.CompletedProcess(arguments, status, output.getvalue(), errors.getvalue())

    def events(self, tool: str) -> list[ToolEvent]:
        if not self.event_path.exists():
            return []
        events = [cast(ToolEvent, json.loads(line)) for line in self.event_path.read_text().splitlines()]
        return [event for event in events if event["tool"] == tool]

    def assert_ran(self, request: offload.OffloadRequest) -> ToolEvent:
        result = self.execute(request)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mac_exit=0", result.stdout)
        events = self.events("lint")
        self.assertTrue(events, result.stdout + result.stderr)
        return events[-1]

    def test_shell_inputs_exclude_source_build_files_and_private_files(self) -> None:
        self.write_input("target/large-build", "build fixture\n")

        event = self.assert_ran(self.request())

        evaluations = self.events("nix")
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["inputs"], ["flake.lock", "flake.nix", "nix/shell.nix"])
        self.assertTrue(any(argument.endswith("#ci") for argument in evaluations[0]["args"]))
        self.assertEqual(event["environment"].get("TEST_SHELL_PROOF"), "loaded")
        self.assertEqual(event["cwd"], str(self.mirror))

    def test_unchanged_inputs_reuse_exports_and_the_slot_build_folder(self) -> None:
        _ = self.assert_ran(self.request())
        self.write_input("src/main.rs", "fn main() { println!(\"changed\"); }\n")

        second = self.assert_ran(self.request())

        self.assertEqual(len(self.events("nix")), 1)
        self.assertTrue(second["warm"])
        self.assertEqual(second["cwd"], str(self.mirror))
        self.assertTrue((self.mirror / "target/warm-proof").is_file())

    def test_another_slot_keeps_a_separate_warm_build_folder(self) -> None:
        _ = self.assert_ran(self.request())
        another = self.root / offload.remote_mirror(self.identity, "aws", 1)
        for name in ("flake.nix", "flake.lock", "nix/shell.nix"):
            path = another / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text((self.source / name).read_text())

        first_in_slot = self.assert_ran(replace(self.request(), slot=1))
        again = self.assert_ran(self.request())

        self.assertFalse(first_in_slot["warm"])
        self.assertEqual(first_in_slot["cwd"], str(another))
        self.assertTrue((another / "target/warm-proof").exists())
        self.assertTrue((self.mirror / "target/warm-proof").exists())
        self.assertTrue(again["warm"])

    def test_repository_budget_above_the_build_budget_refuses_execution(self) -> None:
        _ = self.settings.write_text(self.settings.read_text().replace("target_budget_gib.hana=31", "target_budget_gib.hana=33"))

        result = self.execute(self.request())

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.events("lint"))

    def test_shell_content_and_path_changes_invalidate_exports(self) -> None:
        _ = self.assert_ran(self.request())
        self.write_input("nix/shell.nix", "{ changed = true; }\n")
        _ = self.assert_ran(self.request())
        for root in (self.source, self.mirror):
            _ = (root / "nix/shell.nix").rename(root / "nix/renamed.nix")
        _ = self.assert_ran(self.request())

        self.assertEqual(len(self.events("nix")), 3)

    def test_shell_selector_change_invalidates_exports(self) -> None:
        _ = self.assert_ran(self.request())
        _ = self.settings.write_text(self.settings.read_text().replace("devshell.hana=ci", "devshell.hana=other"))

        _ = self.assert_ran(self.request())

        evaluations = self.events("nix")
        self.assertEqual(len(evaluations), 2)
        self.assertTrue(any(argument.endswith("#other") for argument in evaluations[-1]["args"]))

    def test_each_top_level_flake_input_change_invalidates_exports(self) -> None:
        _ = self.assert_ran(self.request())
        for count, name in enumerate(("flake.nix", "flake.lock"), start=2):
            with self.subTest(name=name):
                self.write_input(name, (self.source / name).read_text() + "\n")
                _ = self.assert_ran(self.request())
                self.assertEqual(len(self.events("nix")), count)

    def test_partial_cache_directory_requires_a_new_evaluation(self) -> None:
        key = linux_runner.shell_key(self.mirror, "ci")
        partial = self.root / ".local/state/build-target/shells" / key / "inputs"
        for name in ("flake.nix", "flake.lock", "nix/shell.nix"):
            path = partial / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text((self.source / name).read_text())

        event = self.assert_ran(self.request())

        self.assertEqual(len(self.events("nix")), 1)
        self.assertEqual(event["environment"].get("TEST_SHELL_PROOF"), "loaded")

    def test_missing_store_path_in_cached_exports_requires_a_new_evaluation(self) -> None:
        _ = self.assert_ran(self.request())
        exports, = (self.root / ".local/state/build-target/shells").rglob("exports.sh")
        missing = "/nix/store/00000000000000000000000000000000-fixture-collected-shell/bin"
        self.assertFalse(Path(missing).exists())
        _ = exports.write_text(f"export PATH={shlex.quote(missing)}:$PATH\nexport TEST_SHELL_PROOF=stale\n")

        event = self.assert_ran(self.request())

        self.assertEqual(len(self.events("nix")), 2)
        self.assertEqual(event["environment"].get("TEST_SHELL_PROOF"), "loaded")

    def test_failed_evaluation_never_publishes_usable_exports(self) -> None:
        self.environment["FAKE_NIX_STATUS"] = "1"
        first = self.execute(self.request())
        self.assertFalse(self.events("lint"), first.stdout + first.stderr)
        self.assertNotIn("mac_exit=0\n", first.stdout)
        self.assertEqual(list((self.root / ".local/state/build-target/shells").rglob("exports.sh")), [])
        del self.environment["FAKE_NIX_STATUS"]

        _ = self.assert_ran(self.request())

        self.assertEqual(len(self.events("nix")), 2)

    def test_origin_policy_budgets_arguments_and_tree_key_reach_the_linux_gate(self) -> None:
        words = ("-p", "hana", "-E", "test(a) | test(b)", "--", "argument with spaces", "$(touch INJECTION)")

        event = self.assert_ran(self.request(words=words))

        self.assertEqual(event["args"][:4], ["nextest", "-p", "hana", "-E"])
        self.assertIn("not (test(gpu))", event["args"][4])
        self.assertEqual(event["args"][5:], list(words[4:]))
        self.assertEqual(event["environment"].get("BUILDLOG_BUILD_BUDGET_GIB"), "32")
        self.assertEqual(event["environment"].get("LINT_SWEEP_BUDGET_GIB"), "31")
        self.assertEqual(event["environment"].get("BUILDLOG_SENT_TREE_KEY"), "sent-tree-key")
        self.assertEqual(event["environment"].get("BUILDLOG_CALL_ID"), "call with spaces")
        self.assertFalse((self.mirror / "INJECTION").exists())

    def test_other_clone_names_keep_the_origin_shell_budget_and_skip_settings(self) -> None:
        words = ("-p", "hana", "-E", "test(a)")
        first = self.assert_ran(self.request(words=words))
        self.identity = offload.KnownRepository("temporary-clone", "https://example.com/team/hana")

        second = self.assert_ran(replace(self.request(words=words), repo_root=self.root / "rust/temporary-clone"))

        self.assertEqual(first["args"], second["args"])
        self.assertEqual(first["environment"]["LINT_SWEEP_BUDGET_GIB"], "31")
        self.assertEqual(second["environment"]["LINT_SWEEP_BUDGET_GIB"], "31")
        self.assertEqual(len(self.events("nix")), 1)

    def test_all_nextest_filter_forms_combine_the_skip_as_a_valid_expression(self) -> None:
        expression = "test(a) | test(b)"
        expected = f"({expression}) & not (test(gpu))"
        forms = (
            (("-E", expression), ("-E", expected)),
            (("--filter-expr", expression), ("--filter-expr", expected)),
            (("--filterset", expression), ("--filterset", expected)),
            ((f"-E{expression}",), (f"-E{expected}",)),
            ((f"-E={expression}",), (f"-E={expected}",)),
            ((f"--filter-expr={expression}",), (f"--filter-expr={expected}",)),
            ((f"--filterset={expression}",), (f"--filterset={expected}",)),
        )
        for words, combined in forms:
            with self.subTest(words=words):
                event = self.assert_ran(self.request(words=(*words, "--", "literal -E=test(z)")))

                self.assertEqual(event["args"], ["nextest", *combined, "--", "literal -E=test(z)"])

    def test_linux_skip_inserts_a_filter_before_the_test_argument_boundary(self) -> None:
        words = ("-p", "hana", "--", "-E=test(argument)")

        event = self.assert_ran(self.request(words=words))

        self.assertEqual(event["args"], [
            "nextest", "-p", "hana", "-E", "not (test(gpu))", "--", "-E=test(argument)",
        ])

    def test_budget_and_disk_floor_changes_apply_to_the_next_run(self) -> None:
        _ = self.assert_ran(self.request())
        _ = self.settings.write_text(self.settings.read_text().replace("build_budget_gib=32", "build_budget_gib=40")
                                    .replace("target_budget_gib.hana=31", "target_budget_gib.hana=35")
                                    .replace("free_floor_gib=10", "free_floor_gib=200"))

        result = self.execute(self.request())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mac_cleanup=0", result.stdout)
        self.assertFalse((self.mirror / "target").exists())
        event = self.events("lint")[-1]
        self.assertEqual(event["environment"]["BUILDLOG_BUILD_BUDGET_GIB"], "40")
        self.assertEqual(event["environment"]["LINT_SWEEP_BUDGET_GIB"], "35")

    def test_slot_budget_removes_only_the_selected_build_folder(self) -> None:
        self.use_real_sweep = True
        _ = self.settings.write_text(self.settings.read_text().replace("target_budget_gib.hana=31", "target_budget_gib.hana=0.0001"))
        old = self.build_output("target", "old", 1, 196608)
        recent = self.build_output("target", "recent", 1_793_570_400, 8192)
        other = self.mirror.parent / "1/target/retained"
        other.parent.mkdir(parents=True)
        _ = other.write_text("another slot\n")

        result = self.execute(self.request())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mac_cleanup=kept", result.stdout)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual((self.mirror / "target/warm-proof").read_text(), "warm\n")
        self.assertEqual(other.read_text(), "another slot\n")

    def test_budget_trims_old_output_and_keeps_recent_output_until_the_disk_floor(self) -> None:
        self.use_real_sweep = True
        _ = self.settings.write_text(self.settings.read_text().replace(
            "target_budget_gib.hana=31", "target_budget_gib.hana=0.0001"))
        old = self.build_output("target", "old", 1, 196608)
        recent = self.build_output("target", "recent", 1_793_570_400, 8192)
        neighbor = self.mirror.parent / "1/target/recent-output"
        neighbor.parent.mkdir(parents=True)
        _ = neighbor.write_text("another slot\n")

        result = self.execute(self.request())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual((self.mirror / "target/warm-proof").read_text(), "warm\n")
        used = sum(path.stat().st_size for path in (self.mirror / "target").rglob("*") if path.is_file())
        self.assertLessEqual(used, 0.0001 * 1024 ** 3)
        self.assertEqual(neighbor.read_text(), "another slot\n")

        self.environment["FAKE_FREE_GIB"] = "1"
        low_disk = self.execute(self.request())

        self.assertEqual(low_disk.returncode, 0, low_disk.stderr)
        self.assertIn("mac_cleanup=0", low_disk.stdout)
        self.assertFalse((self.mirror / "target").exists())
        self.assertEqual(neighbor.read_text(), "another slot\n")

    def test_explicit_build_folder_budget_keeps_other_slot_files(self) -> None:
        self.use_real_sweep = True
        _ = self.settings.write_text(self.settings.read_text().replace("target_budget_gib.hana=31", "target_budget_gib.hana=0.0001"))
        old = self.build_output("custom-build", "old", 1, 196608)
        recent = self.build_output("custom-build", "recent", 1_793_570_400, 8192)
        self.write_input("target/retained", "default folder\n")

        result = self.execute(self.request(words=("--target-dir", "custom-build")))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual((self.mirror / "custom-build/warm-proof").read_text(), "warm\n")
        self.assertEqual((self.mirror / "target/retained").read_text(), "default folder\n")

    def test_explicit_build_folder_cannot_escape_the_selected_slot(self) -> None:
        neighbor = self.mirror.parent / "1/target/retained"
        neighbor.parent.mkdir(parents=True)
        _ = neighbor.write_text("another slot\n")
        for words in (("--target-dir", "../1/target"), (f"--target-dir={neighbor.parent}",)):
            with self.subTest(words=words):
                result = self.execute(self.request(words=words))

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.events("lint"))
                self.assertEqual(neighbor.read_text(), "another slot\n")

    def test_selected_target_probe_reads_tools_inside_the_cached_shell(self) -> None:
        for tool in ("rustc", "cargo", "hostname"):
            self.write_tool(self.root / "bin" / tool)
        _ = (self.root / ".claude/.build-target-script-commit").write_text("fixture-commit\n")
        self.environment["FAKE_NIX_EXPORTS"] = "export TEST_SHELL_TOOL_VERSION=shell"
        arguments = shlex.split(linux_runner.probe_command_script(
            str(self.mirror), offload.read_config(self.settings).values, "hana",
        ))
        output = io.StringIO()
        real_run = subprocess.run

        def capture(argv: list[str], *, cwd: Path | str = "", check: bool = False,
                    capture_output: bool = False, text: bool = False, timeout: float = 5) -> subprocess.CompletedProcess[str]:
            _ = text
            result = real_run(argv, cwd=cwd or self.root, check=check, capture_output=True, text=True, timeout=timeout)
            if not capture_output:
                print(result.stdout, end="")
            return result

        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(sys, "argv", arguments[1:]), \
                mock.patch.object(linux_runner, "slot_run", return_value=nullcontext()), \
                mock.patch.object(subprocess, "run", side_effect=capture), redirect_stdout(output):
            status = linux_runner.main()

        self.assertEqual(status, 0)
        values = dict(line.split("=", 1) for line in output.getvalue().splitlines())
        self.assertEqual(values["rustc"], "rustc shell")
        self.assertEqual(values["nextest"], "nextest shell")
        self.assertEqual(values["mend"], "mend shell")
        self.assertEqual(values["scripts_commit"], "fixture-commit")
        self.assertEqual(len(self.events("nix")), 1)

    def test_probe_uses_linux_disk_units_and_reports_all_parity_fields(self) -> None:
        for tool in ("rustc", "cargo", "hostname"):
            self.write_tool(self.root / "bin" / tool)
        stamp = self.root / ".claude/.build-target-script-commit"
        _ = stamp.write_text("fixture-commit\n")

        result = subprocess.run(
            ["bash", "-c", offload.probe_script(self.identity, "aws", 2)], cwd=self.root,
            env=self.environment, text=True, capture_output=True, check=False, timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(float(values["free_gib"]), 100.0)
        self.assertEqual(values["rustc"], "rustc fixture")
        self.assertEqual(values["nextest"], "nextest fixture")
        self.assertEqual(values["mend"], "mend fixture")
        self.assertEqual(values["scripts_commit"], "fixture-commit")
        self.assertNotIn("-g", self.events("df")[-1]["args"])

    def test_matching_shell_tools_run_when_the_base_environment_versions_differ(self) -> None:
        for tool in ("rustc", "cargo"):
            self.write_tool(self.root / "bin" / tool)
        _ = self.settings.write_text(self.settings.read_text()
                                    + "parity.rustc=rustc shell\nparity.nextest=nextest shell\nparity.mend=mend shell\n")
        self.environment["FAKE_NIX_EXPORTS"] = "export TEST_SHELL_TOOL_VERSION=shell"

        event = self.assert_ran(self.request())

        self.assertEqual(event["environment"].get("TEST_SHELL_TOOL_VERSION"), "shell")
        self.assertEqual(len(self.events("nix")), 1)

    def test_shell_tool_mismatch_never_reports_a_test_failure_or_executes_work(self) -> None:
        for tool in ("rustc", "cargo"):
            self.write_tool(self.root / "bin" / tool)
        _ = self.settings.write_text(self.settings.read_text()
                                    + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\nparity.mend=mend fixture\n")
        self.environment["FAKE_NIX_EXPORTS"] = "export TEST_SHELL_TOOL_VERSION=different"

        result = self.execute(self.request())

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("mac_exit=", result.stdout)
        self.assertIn("mac_cleanup=", result.stdout)
        self.assertFalse(self.events("lint"))

    def test_check_and_staged_doc_keep_their_arguments_and_document_policy(self) -> None:
        check = self.assert_ran(self.request("check", ("--workspace", "--all-features")))
        self.assertEqual(check["args"], ["check", "--workspace", "--all-features"])
        staged = self.root / "staged snapshot"
        staged.mkdir()
        request = replace(self.request("doc", ("--workspace", "--no-deps")), source=offload.StagedDocSnapshot(staged))

        doc = self.assert_ran(request)

        self.assertEqual(doc["args"], ["doc", "--workspace", "--no-deps"])
        self.assertEqual(doc["environment"].get("LINT_CONFIG_FORCE"), "1")
        self.assertEqual(doc["environment"].get("BUILDLOG_SENT_TREE_KEY"), "sent-tree-key")

    def test_failure_status_and_linux_disk_cleanup_evidence_remain_distinct(self) -> None:
        _ = self.assert_ran(self.request())
        self.environment.update({"FAKE_LINT_STATUS": "75", "FAKE_FREE_GIB": "1"})
        remove_tree = shutil.rmtree

        def remove(path: str | Path, **_options: object) -> None:
            if Path(path) == self.mirror / "target":
                raise PermissionError("fixture denies cleanup")
            remove_tree(path)

        with mock.patch("shutil.rmtree", side_effect=remove):
            result = self.execute(self.request("check"))

        self.assertEqual(result.returncode, 75)
        self.assertIn("mac_exit=75", result.stdout, result.stderr)
        self.assertIn("mac_cleanup=1", result.stdout, result.stderr)
        self.assertTrue((self.mirror / "target/warm-proof").exists())


class LinuxProcessTests(unittest.TestCase):
    root: Path = Path()
    mirror: Path = Path()
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linux-process-")))
        self.mirror = self.root / "mirror/1"
        self.mirror.mkdir(parents=True)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        boot_file = self.proc / "sys/kernel/random/boot_id"
        boot_file.parent.mkdir(parents=True)
        _ = boot_file.write_text("fixture-boot\n")
        _ = self.enterContext(mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"))
        _ = self.enterContext(mock.patch.object(os, "getpid", return_value=900))
        _ = self.enterContext(mock.patch.object(os, "getpgrp", return_value=900))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))

    def process(self, pid: int, *, state: str = "S", parent: int = 1, group: int = 100, start: int = 123,
                command: tuple[str, ...] = ("nix", "print-dev-env"), token: str = "",
                cwd: Path = MISSING_PROCESS_CWD) -> None:
        process = self.proc / str(pid)
        process.mkdir()
        fields = [state, str(parent), str(group), *(["0"] * 16), str(start)]
        _ = (process / "stat").write_text(f"{pid} (fixture process) " + " ".join(fields))
        _ = (process / "cmdline").write_bytes(b"\0".join(word.encode() for word in command) + b"\0")
        _ = (process / "environ").write_bytes(f"BUILD_TARGET_RUN_TOKEN={token}\0".encode() if token else b"")
        if cwd != MISSING_PROCESS_CWD:
            (process / "cwd").symlink_to(cwd)

    def marker(self) -> Path:
        path = self.mirror / ".linux-run.json"
        lease: linux_runner.SlotRun = {"pid": 100, "start_time": 123, "process_group": 100, "token": "selected-run", "boot_id": "fixture-boot"}
        _ = path.write_text(json.dumps(lease))
        return path

    def deny_reads(self, *, environ: tuple[int, ...] = (), cwd: tuple[int, ...] = (),
                   cmdline: tuple[int, ...] = ()) -> None:
        read_bytes = Path.read_bytes
        resolve = Path.resolve
        denied_bytes = {self.proc / str(pid) / "environ" for pid in environ}
        denied_bytes.update(self.proc / str(pid) / "cmdline" for pid in cmdline)
        denied_cwd = {self.proc / str(pid) / "cwd" for pid in cwd}

        def read(path: Path) -> bytes:
            if path in denied_bytes:
                raise PermissionError(f"fixture denies {path.name}")
            return read_bytes(path)

        def resolve_path(path: Path, strict: bool = False) -> Path:
            if path in denied_cwd:
                raise PermissionError("fixture denies cwd")
            return resolve(path, strict=strict)

        _ = self.enterContext(mock.patch.object(Path, "read_bytes", read))
        _ = self.enterContext(mock.patch.object(Path, "resolve", resolve_path))

    def use_private_processes(self) -> None:
        fields = linux_runner.process_fields
        processes = linux_runner.slot_processes

        def private_fields(pid: int, proc_root: Path = linux_runner.PROC_ROOT) -> list[str]:
            _ = proc_root
            return fields(pid, self.proc)

        def private_processes(mirror: Path, proc_root: Path = linux_runner.PROC_ROOT, *,
                              require_boot_identity: bool = False) -> tuple[int, ...]:
            _ = proc_root
            return processes(mirror, self.proc, require_boot_identity=require_boot_identity)

        _ = self.enterContext(mock.patch.object(linux_runner, "process_fields", side_effect=private_fields))
        _ = self.enterContext(mock.patch.object(linux_runner, "slot_processes", side_effect=private_processes))

    def selector_output(self) -> subprocess.CompletedProcess[str]:
        script = linux_runner.mirror_selector_script(str(self.mirror))
        command = next(shlex.split(line) for line in script.splitlines() if line.strip().startswith("python3 "))
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", str(self.mirror)]), \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                exec(command[command.index("-c") + 1], {"__name__": "__main__"})
            except SystemExit as stopped:
                status = stopped.code if isinstance(stopped.code, int) else 1
            else:
                status = 0
        return subprocess.CompletedProcess(command, status, output.getvalue(), errors.getvalue())

    def test_unreadable_process_before_the_lease_allows_confirmed_cleanup(self) -> None:
        marker = self.marker()
        self.process(102, start=100, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,), cmdline=(102,))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, ())
        self.assertFalse(selected.unresolved)
        self.assertEqual(linux_runner.slot_processes(self.mirror, self.proc), ())
        self.assertFalse(marker.exists())
        _ = self.marker()
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(selector.stdout.strip(), "")
        self.process(900, group=900, start=123, cwd=self.root)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with linux_runner.slot_run(self.mirror):
                print("mac_exit=0\nmac_cleanup=kept")
        self.assertFalse(marker.exists())
        self.assertIn("mac_cleanup=kept", output.getvalue())
        self.assertNotIn("mac_cleanup=unconfirmed", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue() + selector.stderr)

    def test_unreadable_leased_process_and_parent_chain_keep_confirmed_ownership(self) -> None:
        marker = self.marker()
        self.process(100, cwd=self.root)
        self.process(101, parent=100, start=124, cwd=self.root)
        self.process(102, parent=101, group=102, start=125, cwd=self.root)
        self.deny_reads(environ=(100, 101, 102), cwd=(100, 101, 102), cmdline=(100, 101, 102))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(set(selected.pids), {100, 101, 102})
        self.assertFalse(selected.unresolved)
        self.assertTrue(marker.exists())
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(set(selector.stdout.split()), {"100", "101", "102"})
        self.assertNotIn("Traceback", selector.stdout + selector.stderr)

    def test_denied_environment_preserves_readable_slot_directory_evidence(self) -> None:
        marker = self.marker()
        self.process(101, start=124, cwd=self.mirror)
        self.deny_reads(environ=(101,))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, (101,))
        self.assertFalse(selected.unresolved)
        self.assertTrue(marker.exists())
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(selector.stdout.split(), ["101"])
        self.assertNotIn("Traceback", selector.stdout + selector.stderr)

    def test_denied_directory_preserves_readable_run_token_evidence(self) -> None:
        marker = self.marker()
        self.process(101, start=124, token="selected-run", cwd=self.root)
        self.deny_reads(cwd=(101,))
        self.use_private_processes()

        selected = linux_runner.select_slot_processes(self.mirror, self.proc)

        self.assertEqual(selected.pids, (101,))
        self.assertFalse(selected.unresolved)
        self.assertTrue(marker.exists())
        selector = self.selector_output()
        self.assertEqual(selector.returncode, 0, selector.stderr)
        self.assertEqual(selector.stdout.split(), ["101"])
        self.assertNotIn("Traceback", selector.stdout + selector.stderr)

    def test_unattributed_unreadable_process_retains_claim_through_completion_and_takeover(self) -> None:
        run_command = subprocess.run
        open_process = subprocess.Popen
        dispatch = LinuxDispatchTests()
        self.addCleanup(dispatch.doCleanups)
        dispatch.setUp()
        _ = dispatch.settings.write_text(dispatch.settings.read_text().replace("slots.hana=4", "slots.hana=1"))
        identity = offload.repository_identity(dispatch.source)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The fixture must have a repository identity")
        self.mirror = dispatch.fixture.root / offload.remote_mirror(identity, "aws", 1)
        self.mirror.mkdir(parents=True)
        self.process(900, group=900, start=123, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,))
        self.use_private_processes()
        kill_log = self.root / "kill-list"
        cleanups: list[subprocess.CompletedProcess[str]] = []

        def cleanup(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if argv[0] != "ssh":
                return dispatch.command_reply(argv, **options)
            dispatch.commands.append(argv)
            dispatch.actions.append("cleanup")
            selector = self.selector_output()
            stubs = (
                "python3() { printf '%s' " + shlex.quote(selector.stdout)
                + "; printf '%s' " + shlex.quote(selector.stderr)
                + " >&2; return " + str(selector.returncode) + "; }\n"
                + "kill() { printf '%s\\n' \"$*\" >> " + shlex.quote(str(kill_log)) + "; }\n"
                + "sleep() { :; }\n"
            )
            with mock.patch.object(subprocess, "Popen", open_process):
                result = run_command(["bash", "-c", stubs + argv[-1]], text=True,
                                     capture_output=True, check=False, timeout=5,
                                     env={"HOME": str(dispatch.fixture.root), "PATH": "/usr/bin:/bin"})
            cleanups.append(result)
            return result

        def stream(arguments: Sequence[str]) -> offload.RemoteRunStatus:
            dispatch.commands.append(list(arguments))
            dispatch.actions.append("run")
            output = io.StringIO()
            with redirect_stdout(output):
                with linux_runner.slot_run(self.mirror):
                    self.process(102, group=102, start=124, cwd=self.root)
                    print("mac_exit=0\nmac_cleanup=kept")
            self.assertIn("mac_cleanup=unconfirmed", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            shutil.rmtree(self.proc / "900")

            class CompletedRemoteStep:
                stdout: io.StringIO = io.StringIO(output.getvalue())

                def wait(self) -> int:
                    return 0

            with mock.patch.object(subprocess, "Popen", return_value=CompletedRemoteStep()):
                return offload.ProcessController.stream_mac_run(dispatch.controller, arguments)

        with mock.patch.object(subprocess, "run", side_effect=cleanup), \
                mock.patch.object(dispatch.controller, "stream_mac_run", side_effect=stream):
            completed, output, errors = dispatch.run_request(dispatch.request())
            self.assertIsInstance(completed, offload.PassedOnTarget)
            self.assertNotIn("Traceback", output + errors)
            self.assertIn(900, dispatch.fixture.claims("aws"))
            self.assertNotIn("release", dispatch.actions)
            self.assertTrue((self.mirror / ".linux-run.json").exists())
            selected = linux_runner.select_slot_processes(self.mirror, self.proc)
            self.assertEqual(selected.pids, ())
            self.assertTrue(selected.unresolved)
            with self.assertRaises(linux_runner.SlotProcessesUnresolved):
                _ = linux_runner.slot_processes(self.mirror, self.proc)
            self.assertEqual(dispatch.fixture.claim("aws", dispatch.source, 103, remote=True)[0], 11)
            dispatch.fixture.dead.add(900)
            dispatch.actions.clear()
            with mock.patch.object(os, "getpid", return_value=999):
                takeover, output, errors = dispatch.run_request(dispatch.request())

        self.assertIsInstance(takeover, offload.BusyMacDecline)
        if isinstance(takeover, offload.BusyMacDecline):
            self.assertIsInstance(takeover.holder, offload.LostRunLeftovers)
        self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.NoBackoff)
        self.assertIn(999, dispatch.fixture.claims("aws"))
        self.assertNotIn("release", dispatch.actions)
        self.assertNotIn("run", dispatch.actions)
        self.assertTrue((self.mirror / ".linux-run.json").exists())
        self.assertEqual(len(cleanups), 2)
        for result in cleanups:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout.strip(), "")
            self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", output + errors)
        self.assertFalse(kill_log.exists())

    def test_selector_distinguishes_unresolved_ownership_from_failed_inspection(self) -> None:
        marker = self.marker()
        self.process(102, start=124, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,))
        self.use_private_processes()

        unresolved = self.selector_output()

        self.assertEqual(unresolved.returncode, 3)
        self.assertEqual(unresolved.stdout, "")
        self.assertNotIn("Traceback", unresolved.stderr)
        self.assertTrue(marker.exists())
        self.process(100, start=456, cwd=self.root)

        failed = self.selector_output()

        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertNotIn("Traceback", failed.stderr)
        self.assertTrue(marker.exists())

    def assert_unresolved_startup_stays_busy(self, stage: Literal["run", "probe", "after-probe"]) -> None:
        dispatch = LinuxDispatchTests()
        self.addCleanup(dispatch.doCleanups)
        dispatch.setUp()
        identity = offload.repository_identity(dispatch.source)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The fixture must have a repository identity")
        self.mirror = dispatch.fixture.root / offload.remote_mirror(identity, "aws", 1)
        self.mirror.mkdir(parents=True)
        self.process(900, group=900, cwd=self.root)
        if stage != "after-probe":
            _ = self.marker()
            self.process(102, group=102, start=124, cwd=self.root)
        self.deny_reads(environ=(102,), cwd=(102,))
        self.use_private_processes()
        replies: list[subprocess.CompletedProcess[str]] = []
        probe_replies: list[subprocess.CompletedProcess[str]] = []
        cleanup_replies: list[subprocess.CompletedProcess[str]] = []

        def runner(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
            argv = shlex.split(arguments[-1])
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", argv[1:]), \
                    redirect_stdout(output), redirect_stderr(errors):
                status = linux_runner.main()
            reply = subprocess.CompletedProcess(argv, status, output.getvalue(), errors.getvalue())
            replies.append(reply)
            return reply

        def capture(arguments: Sequence[str],
                    deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CapturedCommandResult:
            if (stage != "run" and arguments[0] == "ssh"
                    and "probe" in shlex.split(arguments[-1]) and "--mirror" in arguments[-1]):
                dispatch.commands.append(list(arguments))
                dispatch.actions.append("probe")
                reply = runner(arguments)
                probe_replies.append(reply)
                return offload.CommandOutput(reply.returncode, reply.stdout, reply.stderr)
            return dispatch.capture(arguments, deadline)

        def child(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if argv[0] == "bash":
                self.assertEqual(stage, "after-probe")
                self.process(102, group=102, start=124, cwd=self.root)
                print(dispatch.probe_wire, end="")
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[0] == "ssh":
                dispatch.commands.append(argv)
                dispatch.actions.append("cleanup")
                reply = self.selector_output()
                cleanup_replies.append(reply)
                return reply
            return dispatch.command_reply(argv, **options)

        def stream(arguments: Sequence[str]) -> offload.RemoteRunStatus:
            dispatch.commands.append(list(arguments))
            dispatch.actions.append("run")
            reply = runner(arguments)

            class RemoteStartup:
                stdout: io.StringIO = io.StringIO(reply.stdout + reply.stderr)

                def wait(self) -> int:
                    return reply.returncode

            with mock.patch.object(subprocess, "Popen", return_value=RemoteStartup()):
                return offload.ProcessController.stream_mac_run(dispatch.controller, arguments)

        shell = linux_runner.ShellEnvironment("fixture-shell", self.root / "exports", self.root / "inputs")
        with mock.patch.object(dispatch.controller, "capture", side_effect=capture), \
                mock.patch.object(dispatch.controller, "stream_mac_run", side_effect=stream), \
                mock.patch.object(subprocess, "run", side_effect=child), \
                mock.patch.object(linux_runner, "prepare_shell", return_value=shell) as evaluate, \
                mock.patch.object(linux_runner, "execute_slot", side_effect=AssertionError("Work must not start")) as execute:
            result, output, errors = dispatch.run_request(dispatch.request())

        self.assertIsInstance(result, offload.BusyMacDecline)
        if isinstance(result, offload.BusyMacDecline):
            self.assertIsInstance(result.holder, offload.LostRunLeftovers)
        self.assertIn(900, dispatch.fixture.claims("aws"))
        self.assertTrue((self.mirror / ".linux-run.json").exists())
        self.assertNotIn("release", dispatch.actions)
        self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.NoBackoff)
        self.assertFalse(offload.backoff_path("aws").exists())
        execute.assert_not_called()
        self.assertEqual(evaluate.call_count, 1 if stage == "after-probe" else 0)
        self.assertTrue(replies)
        refused = replies[-1]
        self.assertEqual(refused.returncode, 75)
        self.assertIn("linux_startup=unresolved", refused.stdout)
        self.assertNotIn("mac_exit=", output + errors + "".join(reply.stdout + reply.stderr for reply in replies))
        self.assertNotIn("Traceback", output + errors + "".join(reply.stdout + reply.stderr for reply in replies))
        self.assertTrue(cleanup_replies)
        for reply in cleanup_replies:
            self.assertEqual(reply.returncode, 3)
            self.assertEqual(reply.stdout, "")
        if stage == "probe":
            self.assertNotIn("run", dispatch.actions)
        else:
            self.assertIn("run", dispatch.actions)
        if stage == "after-probe":
            self.assertEqual(len(probe_replies), 1)
            self.assertEqual(probe_replies[0].returncode, 0)
            self.assertIn("rc=0", probe_replies[0].stdout)
            self.assertIn("mac_cleanup=unconfirmed", probe_replies[0].stdout)

    def test_unresolved_run_startup_keeps_the_claim_without_work_or_backoff(self) -> None:
        self.assert_unresolved_startup_stays_busy("run")

    def test_unresolved_probe_startup_keeps_the_claim_without_work_or_backoff(self) -> None:
        self.assert_unresolved_startup_stays_busy("probe")

    def test_successful_probe_with_unresolved_cleanup_blocks_run_startup(self) -> None:
        self.assert_unresolved_startup_stays_busy("after-probe")

    def test_shell_evaluation_and_runner_remain_owned_outside_the_mirror(self) -> None:
        marker = self.marker()
        self.process(100, cwd=self.root)
        self.process(101, token="selected-run", cwd=self.root)
        self.process(102, token="another-run", cwd=self.root)
        self.process(103, group=103, token="selected-run", cwd=self.root)
        other_slot = self.mirror.parent / "2"
        other_slot.mkdir()
        self.process(104, group=104, cwd=other_slot)

        selected = linux_runner.slot_processes(self.mirror, proc_root=self.proc)

        self.assertEqual(set(selected), {100, 101, 103})
        self.assertTrue(marker.exists())
        fields = linux_runner.process_fields(100, proc_root=self.proc)
        self.assertEqual((fields[0], fields[2], fields[19]), ("S", "100", "123"))

    def test_runner_before_marker_publication_selects_only_its_exact_slot(self) -> None:
        self.process(100, command=("python3", ".claude/scripts/mac_test/linux_runner.py", "run", "--mirror", "mirror/1"), cwd=self.root)
        self.process(101, command=("python3", ".claude/scripts/mac_test/linux_runner.py", "run", "--mirror", "mirror/2"), cwd=self.root)

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), (100,))

    def test_detached_child_keeps_ownership_after_the_original_runner_ends(self) -> None:
        marker = self.marker()
        self.process(101, token="selected-run", cwd=self.root)

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), (101,))
        self.assertTrue(marker.exists())

    def test_reused_runner_pid_cannot_confirm_cleanup(self) -> None:
        marker = self.marker()
        self.process(100, start=456, cwd=self.root)

        with self.assertRaisesRegex(ValueError, "identity changed"):
            _ = linux_runner.slot_processes(self.mirror, proc_root=self.proc)
        self.assertTrue(marker.exists())

    def test_zombies_without_cwd_do_not_block_live_slot_selection(self) -> None:
        self.process(100, state="Z")
        self.process(101, state="X")
        self.process(102, cwd=self.mirror)

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), (102,))

    def test_unreadable_live_process_cannot_confirm_cleanup(self) -> None:
        marker = self.marker()
        self.process(102, group=102)

        with self.assertRaises(linux_runner.SlotProcessesUnresolved):
            _ = linux_runner.slot_processes(self.mirror, proc_root=self.proc)
        self.assertTrue(marker.exists())

    def test_ended_processes_remove_the_marker_before_a_fresh_run(self) -> None:
        marker = self.marker()
        self.process(100, state="Z")

        self.assertEqual(linux_runner.slot_processes(self.mirror, proc_root=self.proc), ())
        self.assertFalse(marker.exists())

    def test_runner_publishes_ownership_before_shell_evaluation(self) -> None:
        fields = ["S", "1", "900", *(["0"] * 16), "123"]
        marker = self.mirror / ".linux-run.json"

        def fail_shell(*_arguments: object) -> linux_runner.ShellEnvironment:
            lease = linux_runner.read_slot_run(marker)
            self.assertNotIsInstance(lease, linux_runner.UnmarkedSlot)
            if not isinstance(lease, linux_runner.UnmarkedSlot):
                self.assertEqual((lease["pid"], lease["start_time"], lease["process_group"]), (900, 123, 900))
                self.assertEqual(os.environ.get("BUILD_TARGET_RUN_TOKEN"), lease["token"])
            raise ValueError("fixture shell failure")

        values = {"shell_cache": "cache", "devshell": "ci", "shell_timeout_s": "2", "kinds": "test,check,doc"}
        with mock.patch.object(linux_runner, "process_fields", return_value=fields), \
                mock.patch.object(linux_runner, "slot_processes", return_value=()), \
                mock.patch.object(linux_runner, "prepare_shell", side_effect=fail_shell):
            with self.assertRaisesRegex(ValueError, "fixture shell failure"):
                _ = linux_runner.run(self.mirror, values, "hana", "check", (), "call", "tree")
        self.assertFalse(marker.exists())

    def test_detached_children_keep_the_marker_and_emit_uncertain_cleanup(self) -> None:
        fields = ["S", "1", "900", *(["0"] * 16), "123"]
        output = io.StringIO()
        with mock.patch.object(linux_runner, "process_fields", return_value=fields), \
                mock.patch.object(linux_runner, "slot_processes", return_value=(101,)), redirect_stdout(output):
            with linux_runner.slot_run(self.mirror):
                self.assertTrue((self.mirror / ".linux-run.json").exists())

        self.assertIn("mac_cleanup=unconfirmed", output.getvalue())
        self.assertTrue((self.mirror / ".linux-run.json").exists())


class LinuxCopyTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    destination: Path = Path()
    commands: list[list[str]] = []
    stamps: list[str] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linux-copy-")))
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "received"
        self.destination.mkdir()
        self.commands, self.stamps = [], []
        _ = self.git("init", "-q")
        _ = self.git("config", "user.name", "Fixture")
        _ = self.git("config", "user.email", "fixture@example.invalid")
        registry = self.root / "build_targets.conf"
        _ = registry.write_text("targets=aws\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws.conf\n")
        _ = write_config_fixture(self.root / "aws.conf", "host=fake-linux\noffload=on\n", template="mac_test")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_TARGETS_CONFIG": str(registry), "CONF_DIR": str(self.root),
        })))

    def git(self, *words: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.source), *words], text=True, capture_output=True, check=True, timeout=5,
        )
        return result.stdout.strip()

    def write_source(self, name: str, contents: str) -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(contents)
        return path

    def capture(self, arguments: Sequence[str],
                _deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
        argv = list(arguments)
        self.commands.append(argv)
        if argv[0] == "git":
            result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=5)
        elif argv[0] == "rsync":
            local = argv[:-1]
            if "-e" in local:
                index = local.index("-e")
                del local[index:index + 2]
            result = subprocess.run(
                [*local, f"{self.destination}/"], text=True, capture_output=True, check=False, timeout=5,
            )
        elif argv[0] == "ssh":
            self.stamps.append(argv[-1])
            return offload.CommandOutput(0, "", "")
        else:
            self.fail(f"Unexpected external command: {argv}")
        return offload.CommandOutput(result.returncode, result.stdout, result.stderr)

    def config(self) -> offload.OffloadConfig:
        return offload.read_config(self.root / "aws.conf")

    def test_second_copy_removes_deleted_files_sends_new_files_and_excludes_ignored_files(self) -> None:
        _ = self.write_source(".gitignore", "private/\n.env\n")
        dropped = self.write_source("tests/b.rs", "old test\n")
        _ = self.write_source("src/lib.rs", "original source\n")
        _ = self.write_source("private/key", "private fixture\n")
        _ = self.write_source(".env", "private environment\n")
        _ = self.write_source("target/local-build", "local build\n")
        _ = self.git("add", ".gitignore", "tests/b.rs", "src/lib.rs")
        _ = self.git("commit", "-qm", "Fixture source")
        controller = offload.ProcessController()
        request = offload.OffloadRequest(self.source, "hana", "copy", offload.NO_RESULT_FILE, (), target="aws")
        identity = offload.KnownRepository("source", "https://example.invalid/team/hana")
        with mock.patch.object(controller, "capture", side_effect=self.capture):
            self.assertTrue(offload.copy_tree(controller, self.config(), request, identity))
            self.assertEqual((self.destination / "tests/b.rs").read_text(), "old test\n")
            (self.destination / "target").mkdir()
            _ = (self.destination / "target/warm").write_text("retained build\n")
            dropped.unlink()
            _ = self.write_source("tests/new test.rs", "untracked test\n")
            _ = self.write_source("src/lib.rs", "changed source\n")

            self.assertTrue(offload.copy_tree(controller, self.config(), request, identity))

        received = {str(path.relative_to(self.destination)) for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(received, {".gitignore", "src/lib.rs", "tests/new test.rs", "target/warm"})
        self.assertEqual((self.destination / "tests/new test.rs").read_text(), "untracked test\n")
        self.assertEqual((self.destination / "src/lib.rs").read_text(), "changed source\n")
        self.assertEqual((self.destination / "target/warm").read_text(), "retained build\n")

    def test_script_stamp_never_labels_dirty_worktree_bytes_as_the_head_commit(self) -> None:
        runner = self.write_source("scripts/mac_test/linux_runner.py", "committed runner\n")
        _ = self.write_source("config/aws_target.conf", "host=fake-linux\n")
        _ = self.git("add", "scripts", "config")
        _ = self.git("commit", "-qm", "Fixture scripts")
        commit = self.git("rev-parse", "HEAD")
        values = self.root / "aws.conf"
        _ = values.write_text(values.read_text() + f"parity.scripts_commit={commit}\n"
                              + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\nparity.mend=mend fixture\n")
        controller = offload.ProcessController()
        with mock.patch.object(offload, "__file__", str(self.source / "scripts/mac_test/offload.py")), \
                mock.patch.object(controller, "capture", side_effect=self.capture):
            self.assertTrue(offload.copy_tracked_scripts(controller, self.config()))
            self.assertEqual((self.destination / "scripts/mac_test/linux_runner.py").read_text(), "committed runner\n")
            self.assertTrue(self.stamps)
            self.assertIn(commit, self.stamps[-1])
            shutil.rmtree(self.destination)
            self.destination.mkdir()
            self.stamps.clear()
            _ = runner.write_text("dirty runner\n")

            copied = offload.copy_tracked_scripts(controller, self.config())

        if copied:
            self.assertEqual((self.destination / "scripts/mac_test/linux_runner.py").read_text(), "committed runner\n")
            self.assertTrue(self.stamps)
            self.assertIn(commit, self.stamps[-1])
        else:
            self.assertFalse(self.stamps)
            self.assertFalse((self.destination / "scripts/mac_test/linux_runner.py").exists())

    def test_script_changes_during_transfer_never_publish_a_commit_stamp(self) -> None:
        runner = self.write_source("scripts/mac_test/linux_runner.py", "committed runner\n")
        _ = self.write_source("config/aws_target.conf", "host=fake-linux\n")
        _ = self.git("add", "scripts", "config")
        _ = self.git("commit", "-qm", "Fixture scripts")
        commit = self.git("rev-parse", "HEAD")
        values = self.root / "aws.conf"
        _ = values.write_text(values.read_text() + f"parity.scripts_commit={commit}\n"
                              + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\nparity.mend=mend fixture\n")
        controller = offload.ProcessController()
        for change in ("worktree", "commit"):
            with self.subTest(change=change):
                _ = runner.write_text("committed runner\n")
                self.stamps.clear()

                def capture(arguments: Sequence[str],
                            deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
                    result = self.capture(arguments, deadline)
                    if arguments[0] == "rsync":
                        if change == "worktree":
                            _ = runner.write_text("changed during transfer\n")
                        else:
                            _ = self.git("commit", "--allow-empty", "-qm", "Commit during transfer")
                    return result

                with mock.patch.object(offload, "__file__", str(self.source / "scripts/mac_test/offload.py")), \
                        mock.patch.object(controller, "capture", side_effect=capture):
                    copied = offload.copy_tracked_scripts(controller, self.config())

                self.assertFalse(copied)
                self.assertFalse(self.stamps)
                self.assertEqual((self.destination / "scripts/mac_test/linux_runner.py").read_text(), "committed runner\n")


class LinuxDispatchTests(unittest.TestCase):
    source: Path = Path()
    settings: Path = Path()
    commands: list[list[str]] = []
    actions: list[str] = []
    filters: list[str] = []
    manifests: list[list[str]] = []
    stream_status: offload.RemoteRunStatus = offload.ReportedMacStatus(0, offload.CleanupKept())
    cleanup_reply: subprocess.CompletedProcess[str] = subprocess.CompletedProcess([], 0, "leftovers=0\nrc=0\n", "")
    probe_wire: str = ""
    script_commit: str = "fixture-commit"

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: test_target_slots.RepositorySlotTests = test_target_slots.RepositorySlotTests()
        self.controller: offload.ProcessController = offload.ProcessController()

    @override
    def setUp(self) -> None:
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.source = self.fixture.repository("rust/phase-copy", "https://example.com/team/hana.git")
        for name in ("flake.nix", "flake.lock", "nix/shell.nix", "src/main.rs"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text("fixture\n")
        self.settings = self.fixture.root / "aws.conf"
        _ = self.settings.write_text(self.settings.read_text()
                                    + "kinds=test,check,doc\ndevshell=default\ndevshell.hana=ci\n"
                                    + "shell_cache=.local/state/build-target/shells\nshell_timeout_s=2\n"
                                    + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\n"
                                    + "parity.mend=mend fixture\nparity.scripts_commit=fixture-commit\n")
        self.commands, self.actions, self.filters, self.manifests = [], [], [], []
        self.probe_wire = (
            "free_gib=200\nhost=fake-linux\nrustc=rustc fixture\nnextest=nextest fixture\n"
            + "mend=mend fixture\nscripts_commit=fixture-commit\nrc=0\n"
        )
        self.controller = offload.ProcessController()
        seed_runner_permission(self.fixture.root, self.settings, 1_793_570_400, "fixture-boot")
        _ = self.enterContext(mock.patch.object(linux_runner, "boot_identity", return_value="fixture-boot"))
        _ = self.enterContext(mock.patch.object(time, "time", return_value=1_793_570_400.0))
        _ = self.enterContext(mock.patch.object(self.controller, "complete_claim", side_effect=self.complete_claim))
        _ = self.enterContext(mock.patch.object(self.controller, "capture", side_effect=self.capture))
        _ = self.enterContext(mock.patch.object(self.controller, "stream_mac_run", side_effect=self.stream))
        _ = self.enterContext(mock.patch.object(offload, "read_tree_key", return_value=offload.KnownTreeKey("source-key")))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.command_reply))

    def request(self, verb: offload.MacVerb = "nextest") -> offload.OffloadRequest:
        return offload.OffloadRequest(self.source, "hana", "call", offload.NO_RESULT_FILE,
                                     ("-p", "hana"), verb=verb, target="aws")

    def state_arguments(self, arguments: Sequence[str]) -> mac_test.CliArguments:
        return mac_test.parser().parse_args(list(arguments[2:]), namespace=mac_test.CliArguments())

    def complete_claim(self, arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
        self.commands.append(list(arguments))
        args = self.state_arguments(arguments)
        output = io.StringIO()
        with redirect_stdout(output):
            status = mac_test.command_claim(args, mac_test.state_paths(args.target))
        self.actions.append(f"claim:{status}")
        attempt.answer = offload.read_claim_answer(offload.CommandOutput(status, output.getvalue(), ""))

    def capture(self, arguments: Sequence[str],
                _deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
        argv = list(arguments)
        self.commands.append(argv)
        if len(argv) > 2 and argv[2] == "release":
            args = self.state_arguments(argv)
            self.actions.append("release")
            return offload.CommandOutput(mac_test.command_release(args, mac_test.state_paths(args.target)), "", "")
        if argv[0] == "git":
            if argv[-2:] == ["rev-parse", "HEAD"]:
                return offload.CommandOutput(0, self.script_commit + "\n", "")
            if "diff" in argv:
                self.assertEqual(argv[3:7], ["diff", "HEAD", "--quiet", "--"])
                return offload.CommandOutput(0, "", "")
            self.assertIn("ls-files", argv)
            if "scripts" in argv:
                return offload.CommandOutput(0, "scripts/mac_test/linux_runner.py\0config/aws_target.conf\0", "")
            return offload.CommandOutput(0, "private/\0.env\0", "")
        if argv[0] == "rsync":
            for argument in argv:
                if argument.startswith("--files-from="):
                    text = Path(argument.split("=", 1)[1]).read_text()
                    self.manifests.append(text.strip("\0").split("\0") if "--from0" in argv else text.splitlines())
                    self.actions.append("source-copy" if "--from0" in argv else "scripts-copy")
                if argument.startswith("--exclude-from="):
                    self.filters.append(Path(argument.split("=", 1)[1]).read_text())
                    self.actions.append("source-copy")
            return offload.CommandOutput(0, "", "")
        if argv[0] == "ssh":
            if ".build-target-script-commit" in argv[-1] and "printf" in argv[-1] and "rustc" not in argv[-1]:
                return offload.CommandOutput(0, "", "")
            self.actions.append("probe")
            return offload.CommandOutput(0, self.probe_wire, "")
        self.fail(f"Unexpected command: {argv}")

    def stream(self, arguments: Sequence[str]) -> offload.RemoteRunStatus:
        self.commands.append(list(arguments))
        self.actions.append("run")
        return self.stream_status

    def command_reply(self, argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            return self.fixture.git_reply(argv, **options)
        if argv[0] == "direnv":
            self.commands.append(argv)
            self.assertEqual(argv, ["direnv", "export", "json"])
            return subprocess.CompletedProcess(argv, 1, "", "environment blocked")
        self.assertEqual(argv[0], "ssh", f"Unexpected external command: {argv}")
        self.commands.append(argv)
        self.actions.append("cleanup")
        return self.cleanup_reply

    def run_request(self, request: offload.OffloadRequest) -> tuple[offload.MacOffloadResult, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = offload.run_offload(self.controller, offload.read_config(self.settings), request, 100.0)
        return result, output.getvalue(), errors.getvalue()

    def test_probe_interruption_keeps_generation_evidence_without_unreachable_backoff(self) -> None:
        state = offload.state_directory("aws")
        state.mkdir(parents=True, exist_ok=True)
        _ = (state / "controller.json").write_text(json.dumps({"generation": "fixture-generation"}))
        self.probe_wire = "aws_interrupted=fixture-generation\n"
        result, _, _ = self.run_request(self.request())
        self.assertIsInstance(result, offload.InterruptedTargetRun)
        assert isinstance(result, offload.InterruptedTargetRun)
        self.assertEqual(result.generation, "fixture-generation")
        self.assertNotIn("run", self.actions)
        self.assertIn("cleanup", self.actions)
        self.assertFalse(self.fixture.claims("aws"))
        self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.NoBackoff)

    def test_linux_executes_without_mac_settings_during_mac_backoff(self) -> None:
        offload.write_backoff(time.time() + 60, "mac")

        result, output, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertIn("running on aws (Linux, host fake-linux)", output)
        self.assertNotIn("running on the Mac", output)
        self.assertIsInstance(offload.read_backoff(time.time(), "mac"), offload.ActiveBackoff)
        self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.NoBackoff)
        self.assertLess(self.actions.index("scripts-copy"), self.actions.index("run"))
        self.assertFalse(self.fixture.claims("aws"))
        self.assertFalse(self.fixture.claims("mac"))
        state_commands = [argv for argv in self.commands if len(argv) > 2 and argv[2] in ("claim", "release")]
        self.assertTrue(state_commands)
        for argv in state_commands:
            self.assertEqual(argv[argv.index("--target") + 1], "aws")

    def test_linux_rejects_lint_even_when_the_kind_setting_contains_it(self) -> None:
        _ = self.settings.write_text(self.settings.read_text().replace("kinds=test,check,doc", "kinds=test,check,doc,clippy,mend-check"))
        for verb in ("clippy", "mend-check"):
            with self.subTest(verb=verb):
                result, _, _ = self.run_request(self.request(verb))
                self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertFalse(self.actions)
        self.assertFalse(self.fixture.claims("aws"))

    def test_each_parity_mismatch_blocks_source_execution(self) -> None:
        matching = self.probe_wire
        for field in ("rustc", "nextest", "mend", "scripts_commit"):
            with self.subTest(field=field):
                self.probe_wire = "\n".join(field + "=wrong" if line.startswith(field + "=") else line
                                             for line in matching.splitlines()) + "\n"
                identity = offload.repository_identity(self.source)
                if not isinstance(identity, offload.KnownRepository):
                    self.fail("The fixture must have a repository identity")
                with redirect_stderr(io.StringIO()):
                    probe = offload.probe_mac(self.controller, offload.read_config(self.settings), identity, "aws", 2)
                self.assertIsInstance(probe, offload.UnavailableProbe)
        self.assertNotIn("source-copy", self.actions)
        self.assertNotIn("run", self.actions)

    def test_script_commit_mismatch_blocks_transfer_and_execution(self) -> None:
        self.script_commit = "different-local-commit"

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertNotIn("scripts-copy", self.actions)
        self.assertNotIn("source-copy", self.actions)
        self.assertNotIn("run", self.actions)
        self.assertFalse(self.fixture.claims("aws"))

    def test_linux_not_ready_status_declines_after_confirmed_cleanup(self) -> None:
        self.stream_status = offload.ReportedMacStatus(75, offload.CleanupKept())

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertNotIsInstance(result, offload.FailedOnMac)
        self.assertFalse(self.fixture.claims("aws"))

    def test_runtime_parity_failure_without_a_test_result_never_reports_failed_work(self) -> None:
        self.stream_status = offload.MISSING_MAC_STATUS

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.LostMacRun)
        self.assertNotIsInstance(result, offload.FailedOnMac)
        self.assertFalse(self.fixture.claims("aws"))

    def test_linux_not_ready_status_retains_ownership_when_cleanup_is_unknown(self) -> None:
        self.stream_status = offload.ReportedMacStatus(75, offload.CleanupUnconfirmed())
        self.cleanup_reply = subprocess.CompletedProcess([], 255, "", "fixture cannot reach Linux")

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertIn(os.getpid(), self.fixture.claims("aws"))
        self.assertNotIn("release", self.actions)

    def test_copy_excludes_private_files_and_preserves_the_received_slot(self) -> None:
        self.assertEqual(self.fixture.claim("aws", self.source, 101, remote=True)[0], 0)
        first = self.fixture.claims("aws")[101]

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertEqual(self.manifests, [
            ["scripts/mac_test/linux_runner.py", "config/aws_target.conf"],
        ])
        source_copy = next(argv for argv in self.commands if argv[0] == "rsync" and any(word.startswith("--exclude-from=") for word in argv))
        self.assertIn("--delete", source_copy)
        self.assertEqual(self.filters, ["/.git\n/target/\n/private/\n/.env\n"])
        self.assertNotIn("--copy-links", source_copy)
        identity = offload.repository_identity(self.source)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The fixture must have a repository identity")
        command = next(argv[-1] for argv in self.commands if argv[0] == "ssh" and "--mirror" in argv[-1])
        words = shlex.split(command)
        self.assertEqual(words[words.index("--mirror") + 1], offload.remote_mirror(identity, "aws", 2))
        self.assertEqual(self.fixture.claims("aws"), {101: first})

    def test_lost_run_keeps_ownership_until_confirmed_cleanup_and_a_fresh_claim(self) -> None:
        _ = self.settings.write_text(self.settings.read_text().replace("slots.hana=4", "slots.hana=1"))
        self.stream_status = offload.MISSING_MAC_STATUS
        self.cleanup_reply = subprocess.CompletedProcess([], 255, "", "fixture cannot reach Linux")

        result, _, errors = self.run_request(self.request())

        self.assertIsInstance(result, offload.LostMacRun)
        retained = self.fixture.claims("aws")
        self.assertIn(os.getpid(), retained)
        self.assertNotIn("release", self.actions)
        self.assertIn("aws stays busy", errors)
        self.assertNotIn("Mac stays busy", errors)
        self.stream_status = offload.ReportedMacStatus(0, offload.CleanupKept())
        self.cleanup_reply = subprocess.CompletedProcess([], 0, "leftovers=0\nrc=0\n", "")
        self.actions.clear()
        self.fixture.dead.add(os.getpid())

        with mock.patch.object(os, "getpid", return_value=999):
            retry, _, _ = self.run_request(self.request())

        self.assertIsInstance(retry, offload.PassedOnTarget)
        self.assertEqual(self.actions[:4], ["claim:13", "cleanup", "release", "claim:0"])
        self.assertFalse(self.fixture.claims("aws"))

    def test_live_slot_children_block_new_work_until_cleanup_confirms_they_ended(self) -> None:
        _ = self.settings.write_text(self.settings.read_text().replace("slots.hana=4", "slots.hana=1"))
        identity = offload.repository_identity(self.source)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The fixture must have a repository identity")
        mirror = self.fixture.root / offload.remote_mirror(identity, "aws", 1)
        mirror.mkdir(parents=True)
        fields = ["S", "1", "100", *(["0"] * 16), "123"]
        wire = io.StringIO()
        with mock.patch.object(linux_runner, "process_fields", return_value=fields), \
                mock.patch.object(linux_runner, "slot_processes", return_value=(101,)), redirect_stdout(wire):
            with linux_runner.slot_run(mirror):
                print("mac_exit=0\nmac_cleanup=kept")
        self.assertTrue((mirror / ".linux-run.json").exists())

        class CompletedRemoteStep:
            stdout: io.StringIO = io.StringIO(wire.getvalue())

            def wait(self) -> int:
                return 0

        def stream_child_run(arguments: Sequence[str]) -> offload.RemoteRunStatus:
            self.commands.append(list(arguments))
            self.actions.append("run")
            with mock.patch.object(subprocess, "Popen", return_value=CompletedRemoteStep()):
                return offload.ProcessController.stream_mac_run(self.controller, arguments)

        self.cleanup_reply = subprocess.CompletedProcess([], 0, "leftovers=1\nrc=0\n", "")
        with mock.patch.object(self.controller, "stream_mac_run", side_effect=stream_child_run):
            result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertIn("cleanup", self.actions)
        self.assertNotIn("release", self.actions)
        self.assertIn(os.getpid(), self.fixture.claims("aws"))
        self.assertEqual(self.fixture.claim("aws", self.source, 102, remote=True)[0], 11)
        self.fixture.dead.add(os.getpid())
        self.actions.clear()
        with mock.patch.object(os, "getpid", return_value=999):
            blocked, _, _ = self.run_request(self.request())
        self.assertIsInstance(blocked, offload.BusyMacDecline)
        if isinstance(blocked, offload.BusyMacDecline):
            self.assertIsInstance(blocked.holder, offload.LostRunLeftovers)
        self.assertNotIn("run", self.actions)
        self.assertNotIn("release", self.actions)
        self.assertIn(999, self.fixture.claims("aws"))

        self.cleanup_reply = subprocess.CompletedProcess([], 0, "leftovers=0\nrc=0\n", "")
        self.fixture.dead.add(999)
        self.actions.clear()
        with mock.patch.object(os, "getpid", return_value=1000):
            fresh, _, _ = self.run_request(self.request())
        self.assertIsInstance(fresh, offload.PassedOnTarget)
        self.assertEqual(self.actions[:4], ["claim:13", "cleanup", "release", "claim:0"])
        self.assertFalse(self.fixture.claims("aws"))

    def test_deleted_worktree_cleanup_selects_only_the_retained_slot(self) -> None:
        old = self.fixture.repository("old-worktree", "https://example.com/team/hana.git", self.source)
        self.assertEqual(self.fixture.claim("aws", old, 101, remote=True)[0], 0)
        self.assertEqual(self.fixture.claim("aws", self.source, 102, remote=True)[0], 0)
        before = self.fixture.claims("aws")
        _ = self.settings.write_text(self.settings.read_text().replace("slots.hana=4", "slots.hana=2"))
        (old / ".git").unlink()
        old.rmdir()
        self.fixture.dead.add(101)

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertEqual(self.actions[:4], ["claim:13", "cleanup", "release", "claim:0"])
        cleanup = next(argv for argv in self.commands if argv[0] == "ssh" and "select_leftovers" in argv[-1])
        identity = offload.repository_identity(self.source)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The fixture must have a repository identity")
        self.assertIn(offload.remote_mirror(identity, "aws", 1), cleanup[-1])
        self.assertNotIn(offload.remote_mirror(identity, "aws", 2), cleanup[-1])
        self.assertNotIn("lsof", cleanup[-1])
        self.assertNotIn("pgrep", cleanup[-1])
        self.assertEqual(self.fixture.claims("aws"), {102: before[102]})

    def test_unrelated_environment_does_not_gain_automatic_shell_approval(self) -> None:
        unrelated = self.fixture.repository("unrelated/project", "https://example.com/team/hana.git")
        _ = (unrelated / ".envrc").write_text("use flake\n")

        result, _, _ = self.run_request(replace(self.request(), repo_root=unrelated))

        self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertFalse(self.actions)
        self.assertFalse(self.fixture.claims("aws"))

    def test_failed_linux_probe_changes_only_the_linux_backoff(self) -> None:
        self.probe_wire = "free_gib=200\nhost=fake-linux\nrc=1\n"

        result, _, _ = self.run_request(self.request())

        self.assertIsInstance(result, offload.DeclinedMacRun)
        self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.ActiveBackoff)
        self.assertIsInstance(offload.read_backoff(time.time(), "mac"), offload.NoBackoff)
        self.assertLess(self.actions.index("source-copy"), self.actions.index("probe"))
        self.assertNotIn("run", self.actions)
        self.assertFalse(self.fixture.claims("aws"))

    def test_cold_shell_uses_its_timeout_while_unreachable_hosts_use_the_probe_timeout(self) -> None:
        cold_seconds = 1.5
        shell_limits: list[float] = []
        failed_limits: list[float] = []
        unreachable = False

        def timed_capture(arguments: Sequence[str],
                          deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CapturedCommandResult:
            if arguments[0] == "ssh" and isinstance(deadline, offload.CommandTimeLimit):
                if unreachable:
                    failed_limits.append(deadline.seconds)
                    return offload.COMMAND_TIMED_OUT
                if "--mirror" in arguments[-1] and "probe" in shlex.split(arguments[-1]):
                    shell_limits.append(deadline.seconds)
                    if deadline.seconds < cold_seconds:
                        return offload.COMMAND_TIMED_OUT
            return self.capture(arguments, deadline)

        config = offload.read_config(self.settings)
        self.assertLess(config.probe_timeout_s, cold_seconds)
        self.assertLess(cold_seconds, float(config.values["shell_timeout_s"]))
        with mock.patch.object(self.controller, "capture", side_effect=timed_capture):
            cold, _, _ = self.run_request(self.request())

            self.assertIsInstance(cold, offload.PassedOnTarget)
            self.assertTrue(shell_limits)
            self.assertTrue(all(limit >= cold_seconds for limit in shell_limits))
            self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.NoBackoff)
            self.actions.clear()
            unreachable = True
            failed, _, _ = self.run_request(self.request())

        self.assertIsInstance(failed, offload.DeclinedMacRun)
        if isinstance(failed, offload.DeclinedMacRun):
            self.assertEqual(failed.reason, "unreachable")
        self.assertEqual(failed_limits, [config.probe_timeout_s])
        self.assertIsInstance(offload.read_backoff(time.time(), "aws"), offload.ActiveBackoff)
        self.assertNotIn("run", self.actions)
        self.assertFalse(self.fixture.claims("aws"))

    def test_real_linux_failure_status_survives_cleanup(self) -> None:
        self.stream_status = offload.ReportedMacStatus(101, offload.CleanupKept())

        result, _, _ = self.run_request(self.request("check"))

        self.assertIsInstance(result, offload.FailedOnTarget)
        if isinstance(result, offload.FailedOnTarget):
            self.assertEqual(result.status, 101)
        self.assertFalse(self.fixture.claims("aws"))

    def test_staged_doc_copies_the_snapshot_and_checks_its_tree_key(self) -> None:
        snapshot = self.fixture.root / "staged snapshot"
        snapshot.mkdir()
        for name in ("flake.nix", "flake.lock", "nix/shell.nix", "src/main.rs"):
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text((self.source / name).read_text())
        roots: list[Path] = []

        def tree_key(root: Path) -> offload.KnownTreeKey:
            roots.append(root)
            return offload.KnownTreeKey("staged-doc-key")

        request = replace(self.request("doc"), source=offload.StagedDocSnapshot(snapshot))
        with mock.patch.object(offload, "read_tree_key", side_effect=tree_key):
            result, _, _ = self.run_request(request)

        self.assertIsInstance(result, offload.PassedOnTarget)
        self.assertEqual(roots, [snapshot, snapshot])
        source_copy = next(argv for argv in self.commands if argv[0] == "rsync" and any(word.startswith("--exclude-from=") for word in argv))
        self.assertEqual(source_copy[-2], f"{snapshot}/")
        remote = next(argv[-1] for argv in self.commands if argv[0] == "ssh" and "--tree-key" in argv[-1])
        words = shlex.split(remote)
        self.assertEqual(words[words.index("--tree-key") + 1], "staged-doc-key")


if __name__ == "__main__":
    _ = unittest.main()
