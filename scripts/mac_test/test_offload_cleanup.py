"""Offload remote results, cleanup and shell protocols."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from typing import cast, override
from unittest import mock
import offload
import mac_test
from offload_test_commands import ACTIVITY_SELECTOR_STUB, write_shell
from offload_test_support import HANA, HANA_COMMAND_PATTERN, SelectorEvent

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


class MacCleanupProtocolTests(unittest.TestCase):
    def test_status_marker_followed_by_ordinary_output_is_printed_and_not_a_result(self) -> None:
        output = io.StringIO()
        controller = offload.ProcessController()
        text = "mac_exit=0\ntest still running\n"
        with redirect_stdout(output):
            answer = controller.stream_mac_run([
                "/bin/sh", "-c", 'printf "%s" "$1"; exit 255', "protocol", text,
            ])
        self.assertIsInstance(answer, offload.MissingMacStatus)
        self.assertEqual(output.getvalue(), text)

    def test_stream_returns_cleanup_outcome_beside_the_reported_test_status(self) -> None:
        cases = (
            ("mac_cleanup=0\n", 0, offload.CleanupDone()),
            ("mac_cleanup=kept\n", 0, offload.CleanupKept()),
            ("mac_cleanup=7\n", 0, offload.CleanupFailed(7)),
            ("mac_cleanup=skipped\n", 0, offload.CleanupSkipped()),
            ("", 255, offload.CleanupUnconfirmed()),
        )
        for marker, ssh_status, expected in cases:
            with self.subTest(marker=marker, ssh_status=ssh_status):
                output = io.StringIO()
                controller = offload.ProcessController()
                with redirect_stdout(output):
                    answer = controller.stream_mac_run([
                        "/bin/sh", "-c", 'printf "%s" "$1"; exit "$2"', "protocol",
                        "test output\nmac_exit=42\n" + marker, str(ssh_status),
                    ])
                self.assertEqual(answer, offload.ReportedMacStatus(42, expected))
                self.assertEqual(output.getvalue(), "test output\n")


class MacTargetCleanupShellTests(unittest.TestCase):
    root: Path = Path()
    home_directory: Path = Path()
    mirror: Path = Path()
    temporary_directory: Path = Path()
    bin_directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home_directory = self.root / "home with spaces"
        self.mirror = self.home_directory / offload.remote_mirror(offload.KnownRepository("hana's project"))
        self.mirror.mkdir(parents=True)
        self.temporary_directory = self.root / "tmp"
        self.temporary_directory.mkdir()
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        disk_space = self.bin_directory / "df"
        write_shell(disk_space, r'''
json_args "$@" > "$CLEANUP_TEST_DIR/df-arguments.json"
printf '%s' "$CLEANUP_TEST_DF_OUTPUT"
exit "$CLEANUP_TEST_DF_STATUS"
''')
        lint = self.home_directory / ".claude/scripts/lint/lint"
        lint.parent.mkdir(parents=True)
        write_shell(lint, r'''
json_args "$@" > "$CLEANUP_TEST_DIR/lint-arguments.json"
printf '%s' "${LINT_SKIP_SWEEP:-}" > "$CLEANUP_TEST_DIR/sweep-switch"
target=target
while (( $# )); do
    case $1 in
        --target-dir) target=$2; shift ;;
        --target-dir=*) target=${1#*=} ;;
    esac
    shift
done
mkdir -p -- "$target" || exit 99
printf 'built\n' > "$target/nextest-output"
printf 'nextest finished\n'
exit "$CLEANUP_TEST_STATUS"
''')

    def run_remote(
        self, shell: list[str], status: int, words: tuple[str, ...] = (), *,
        disk_output: str = "Filesystem GiB Used Available Capacity Mounted\nfixture 500 401 99 80% /\n",
        disk_status: int = 0,
        free_floor_gib: float = 100,
    ) -> subprocess.CompletedProcess[str]:
        request = offload.OffloadRequest(self.root, "hana", "cleanup-call", offload.NO_RESULT_FILE, words)
        config = offload.OffloadConfig(
            enabled=True, host="unused-host", connect_timeout_s=6, probe_timeout_s=1,
            unreachable_backoff_s=300, free_floor_gib=free_floor_gib,
            default_target_budget_gib="24", values={},
        )
        text = (
            '[ "$(command -v df)" = ' + shlex.quote(str(self.bin_directory / "df")) + ' ] || exit 99\n'
            + offload.remote_test_command(config, request, offload.KnownRepository("hana's project"))
        )
        return subprocess.run(
            [*shell, "-c", text], cwd=self.home_directory,
            env={**os.environ, "HOME": str(self.home_directory), "TMPDIR": str(self.temporary_directory),
                 "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                 "CLEANUP_TEST_DIR": str(self.root), "CLEANUP_TEST_STATUS": str(status),
                 "CLEANUP_TEST_DF_OUTPUT": disk_output, "CLEANUP_TEST_DF_STATUS": str(disk_status)},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def test_remote_text_below_space_floor_removes_only_repository_target_after_pass_and_failure(self) -> None:
        target = self.mirror / "target"
        sibling = self.mirror / "keep-source"
        temporary_file = self.temporary_directory / "keep-temporary"
        other_target = self.mirror.parent / "other-repository" / "target" / "keep-build"
        other_target.parent.mkdir(parents=True)
        for path in (sibling, temporary_file, other_target):
            _ = path.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                with self.subTest(shell=shell, status=status):
                    target.mkdir(exist_ok=True)
                    _ = (target / "previous-build").write_text("old\n")
                    answer = self.run_remote(shell, status)
                    self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                    self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=0"])
                    self.assertEqual(answer.stderr, "")
                    self.assertFalse(target.exists())
                    self.assertEqual((self.root / "sweep-switch").read_text(), "1")
                    self.assertEqual(
                        cast(list[str], json.loads((self.root / "df-arguments.json").read_text())),
                        ["-g", str(self.home_directory)],
                    )
                    for path in (sibling, temporary_file, other_target):
                        self.assertEqual(path.read_text(), "keep\n")

    def test_remote_text_at_or_above_space_floor_keeps_build_output(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for free_space, floor in ((100, 100), (101, 100), (50, 50)):
                    with self.subTest(shell=shell, status=status, free_space=free_space, floor=floor):
                        answer = self.run_remote(
                            shell, status, free_floor_gib=floor,
                            disk_output=f"Filesystem GiB Used Available Capacity Mounted\nfixture 500 400 {free_space} 80% /\n",
                        )
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")

    def test_remote_text_with_unreadable_space_keeps_build_output(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for disk_output, disk_status in (("", 0), ("header\nfixture 500 400 unavailable\n", 0), ("", 1)):
                    with self.subTest(shell=shell, status=status, disk_output=disk_output, disk_status=disk_status):
                        answer = self.run_remote(shell, status, disk_output=disk_output, disk_status=disk_status)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")

    def test_remote_text_with_another_target_dir_keeps_every_build_directory(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        other_target = self.temporary_directory / "other build"
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for words in (("--target-dir", str(other_target)), (f"--target-dir={other_target}",)):
                    with self.subTest(shell=shell, status=status, words=words):
                        answer = self.run_remote(shell, status, words)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=skipped"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((other_target / "nextest-output").read_text(), "built\n")
                        self.assertEqual(cast(list[str], json.loads((self.root / "lint-arguments.json").read_text())), ["nextest", *words])

    def test_remote_text_reports_delete_failure_after_the_test_status(self) -> None:
        remove = self.bin_directory / "rm"
        write_shell(remove, r'''
json_args "$@" > "$CLEANUP_TEST_DIR/remove-arguments.json"
exit 7
''')
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"]}):
            for shell in (["/bin/sh"], ["zsh", "-f"]):
                for status in (0, 42):
                    with self.subTest(shell=shell, status=status):
                        answer = self.run_remote(shell, status)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=7"])
                        self.assertTrue((self.mirror / "target" / "nextest-output").exists())
                        arguments = cast(list[str], json.loads((self.root / "remove-arguments.json").read_text()))
                        self.assertEqual([word for word in arguments if not word.startswith("-")], [str(self.mirror / "target")])


class MacActivityShellTests(unittest.TestCase):
    root: Path = Path()
    bin_directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        for name in ("pgrep", "lsof", "ps", "id"):
            path = self.bin_directory / name
            write_shell(path, ACTIVITY_SELECTOR_STUB)

    def run_activity(self, shell: list[str], responses: dict[str, tuple[int, str]]) -> subprocess.CompletedProcess[str]:
        for name, (status, answer) in responses.items():
            _ = (self.root / f"{name}-response.sh").write_text(
                f"status={status}\nanswer={shlex.quote(answer)}\n")
        (self.root / "commands.jsonl").unlink(missing_ok=True)
        environment = {**os.environ, "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                       "ACTIVITY_TEST_DIR": str(self.root)}
        checks = "\n".join(
            '[ "$(command -v ' + name + ')" = ' + shlex.quote(str(self.bin_directory / name)) + ' ] || exit 99'
            for name in ("pgrep", "lsof", "ps", "id")
        )
        text = "kill() { printf 'signal requested\\n' >&2; return 99; }\n" + checks + "\n" + mac_test.mac_activity_script()
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)

    def test_activity_text_has_zero_for_only_shells_in_clones_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p101\nn/Users/test/rust/hana\np102\nn/Users/test/rust/other\n"),
            "ps": (0, "101 501 /bin/zsh\n102 501 /bin/sh\n103 999 /opt/cargo\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=0\nrc=0\n", ""))

    def test_activity_text_counts_each_matching_user_pid_once_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (0, "101\n102\n"),
            "lsof": (0, "p102\nn/Users/test/.local/state/mac-test/mirror/hana\n"
                     + "p103\nn/Users/test/.local/state/mac-test/mirror\np104\nn/Users/test/rust/hana\n"),
            "ps": (0, "101 501 /opt/rust/bin/cargo\n105 501 /opt/rust/bin/rustc\n106 501 cargo-nextest\n"
                   + "107 501 /Users/test/rust/hana/target/debug/app\n108 999 /opt/cargo\n"
                   + "109 501 /bin/zsh\n110 501 /opt/cargo-helper\n112 501 /Users/test/my project/target/debug/app\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=7\nrc=0\n", ""))
                calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
                pgrep = [call for call in calls if call["command"] == "pgrep"]
                lsof = [call for call in calls if call["command"] == "lsof"]
                ps = [call for call in calls if call["command"] == "ps"]
                self.assertEqual(len(pgrep), 1)
                self.assertIn("-u", pgrep[0]["args"])
                self.assertIn("501", pgrep[0]["args"])
                self.assertIn("[.]local/state/mac-test/mirror/", pgrep[0]["args"])
                self.assertEqual(lsof[0]["args"], ["-a", "-u", "501", "-d", "cwd", "-Fpn"])
                self.assertEqual(len(ps), 1)

    def test_activity_selector_failure_prints_no_success_under_sh_and_zsh(self) -> None:
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for name, status in (("pgrep", 2), ("lsof", 1), ("ps", 1)):
                with self.subTest(shell=shell, selector=name):
                    responses = {"id": (0, "501\n"), "pgrep": (1, ""), "lsof": (0, ""), "ps": (0, "")}
                    responses[name] = (status, "")
                    answer = self.run_activity(shell, responses)
                    self.assertNotEqual(answer.returncode, 0)
                    self.assertNotIn("rc=0", answer.stdout)
                    self.assertNotIn("active=0", answer.stdout)
                    self.assertNotIn("signal requested", answer.stderr)

    def test_activity_and_leftover_texts_use_the_shared_mirror_selector(self) -> None:
        marker = "select_leftovers() { return 1; }\n"
        with mock.patch.object(offload, "mirror_selector_script", return_value=marker) as selector:
            self.assertTrue(offload.leftover_check_script(HANA).startswith(marker))
            self.assertTrue(mac_test.mac_activity_script().startswith(marker))
        self.assertEqual(selector.call_args_list, [mock.call(HANA), mock.call(offload.ALL_MIRRORS)])

    def run_selector(
        self, shell: list[str], scope: offload.MirrorScope, responses: dict[str, tuple[int, str]],
    ) -> subprocess.CompletedProcess[str]:
        for name, (status, answer) in responses.items():
            _ = (self.root / f"{name}-response.sh").write_text(
                f"status={status}\nanswer={shlex.quote(answer)}\n")
        (self.root / "commands.jsonl").unlink(missing_ok=True)
        environment = {**os.environ, "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                       "ACTIVITY_TEST_DIR": str(self.root)}
        text = offload.mirror_selector_script(scope) + "select_leftovers\n"
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)

    def test_repository_selector_stops_at_its_own_mirror_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p201\nn/Users/test/.local/state/mac-test/mirror/hana\n"
                     + "p202\nn/Users/test/.local/state/mac-test/mirror/hana/crates/hana\n"
                     + "p203\nn/Users/test/.local/state/mac-test/mirror/hana_other\n"
                     + "p204\nn/Users/test/.local/state/mac-test/mirror/cargo-liner\n"
                     + "p205\nn/Users/test/.local/state/mac-test/mirror\n"
                     + "p206\nn/Users/test/rust/hana\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_selector(shell, HANA, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "201\n202\n", ""))
                calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
                pgrep = [call for call in calls if call["command"] == "pgrep"]
                self.assertEqual(len(pgrep), 1)
                self.assertEqual(pgrep[0]["args"], ["-u", "501", "-f", HANA_COMMAND_PATTERN])

    def test_repository_command_pattern_matches_only_its_own_mirror(self) -> None:
        responses = {"id": (0, "501\n"), "pgrep": (1, ""), "lsof": (0, "")}
        answer = self.run_selector(["/bin/sh"], HANA, responses)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
        pattern = next(call["args"][-1] for call in calls if call["command"] == "pgrep")
        matching = [
            "sh -c cd .local/state/mac-test/mirror/hana && cargo nextest run",
            "cargo build --manifest-path /Users/test/.local/state/mac-test/mirror/hana/Cargo.toml",
            "sh -c cd '/Users/test/.local/state/mac-test/mirror/hana' && cargo test",
            "sh -c cd \"/Users/test/.local/state/mac-test/mirror/hana\"",
            "ls /Users/test/.local/state/mac-test/mirror/hana",
        ]
        other = [
            "sh -c cd .local/state/mac-test/mirror/hana_other && cargo nextest run",
            "sh -c cd .local/state/mac-test/mirror/cargo-liner && cargo nextest run",
            "sh -c cd .local/state/mac-test/mirror/hanabi && cargo test",
            "sh -c cd /Users/test/rust/hana && cargo test",
        ]
        selected = subprocess.run(["grep", "-E", "-e", pattern], input="\n".join(matching + other) + "\n",
                                  capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.splitlines(), matching)




if __name__ == "__main__":
    _ = unittest.main()
