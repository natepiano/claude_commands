"""Offload remote results, cleanup and shell protocols."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from typing import cast, override
from unittest import mock
from scripts.mac_test import offload, mac_test
from scripts.mac_test.offload_test_commands import ACTIVITY_SELECTOR_STUB, write_shell
from scripts.mac_test.offload_test_support import HANA, HANA_COMMAND_PATTERN, SelectorEvent
from scripts.lib import test_hang_guard
from scripts.lint.memory_test_support import config_environment


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
            ("mac_cleanup=0 32715571 92\n", 0, offload.CleanupDone(offload.MeasuredCleanupSize(32715571), 92)),
            ("mac_cleanup=0 - 92.5\n", 0, offload.CleanupDone(offload.CleanupSizeNotMeasured(), 92.5)),
            ("mac_cleanup=kept\n", 0, offload.CleanupKept()),
            ("mac_cleanup=7\n", 0, offload.CleanupFailed(7)),
            ("mac_cleanup=skipped\n", 0, offload.CleanupSkipped()),
            ("mac_trim=status=0 elapsed_seconds=2 over_budget=yes\nmac_cleanup=kept\n",
             0, offload.CleanupKept()),
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
                trim_output = "mac_trim=status=0 elapsed_seconds=2 over_budget=yes\n" if marker.startswith("mac_trim=") else ""
                self.assertEqual(output.getvalue(), "test output\n" + trim_output)

    def test_bare_success_confirms_cleanup_without_a_removal_line(self) -> None:
        output = io.StringIO()
        controller = offload.ProcessController()
        with redirect_stdout(output):
            answer = controller.stream_mac_run([
                "/bin/sh", "-c", 'printf "mac_exit=0\\nmac_cleanup=0\\n"',
            ])
            if not isinstance(answer, offload.ReportedMacStatus):
                self.fail("The remote command did not report its status.")
            self.assertEqual(answer, offload.ReportedMacStatus(0, offload.CleanupRemovedWithoutFacts()))
            offload.report_cleanup(answer.cleanup, "", path="unused", free_floor_gib=100)
        self.assertEqual(output.getvalue(), "")

    def test_cleanup_reports_one_removal_line_with_the_size_and_free_space(self) -> None:
        for size, free, text in (
            (offload.MeasuredCleanupSize(32715571), 92, "31.2 GiB; Mac free 92 GiB"),
            (offload.CleanupSizeNotMeasured(), 92.5, "size not measured; Mac free 92.5 GiB"),
        ):
            with self.subTest(size=size):
                output = io.StringIO()
                with redirect_stdout(output):
                    offload.report_cleanup(offload.CleanupDone(size, free), "",
                                           path="~/.local/state/mac-test/mirror/hana/target", free_floor_gib=100)
                self.assertEqual(output.getvalue(),
                                 "Mac build output removed: ~/.local/state/mac-test/mirror/hana/target, "
                                 + text + " under the 100 GiB floor\n")


class MacTargetCleanupShellTests(unittest.TestCase):
    root: Path = Path()
    home_directory: Path = Path()
    mirror: Path = Path()
    temporary_directory: Path = Path()
    bin_directory: Path = Path()
    configuration_environment: dict[str, str] = {}
    python: Path = Path()

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
        self.configuration_environment = config_environment(self.root / "configuration")
        # The trim step runs the lint sweep, about 0.5 CPU s a start. Tests of the trim call real_sweep();
        # the other tests check the shell text around it, so this stub reports a clean trim at once.
        self.python = self.home_directory / ".claude/scripts/lib/py"
        self.python.parent.mkdir(parents=True)
        write_shell(self.python, 'exit 0\n')
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

    def real_sweep(self) -> None:
        """Run the real lint sweep in the trim step."""
        write_shell(self.python, f'exec {shlex.quote(sys.executable)} "$@"\n')

    def run_remote(
        self, shell: list[str], status: int, words: tuple[str, ...] = (), *,
        disk_output: str = "Filesystem GiB Used Available Capacity Mounted\nfixture 500 401 99 80% /\n",
        disk_status: int = 0,
        free_floor_gib: float = 100,
        abandoned_session_minutes: int = 7,
        budget_gib: str = "24",
        budget_values: tuple[tuple[str, str], ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        request = offload.OffloadRequest(self.root, "hana", "cleanup-call", offload.NO_RESULT_FILE, words, owner="verify", holder_session="fixture-session")
        config = offload.OffloadConfig(
            enabled=True, connection=offload.SshConnectionSettings("unused-host", 6, 10), probe_timeout_s=1,
            unreachable_backoff_s=300, free_floor_gib=free_floor_gib,
            default_target_budget_gib=budget_gib,
            values={"abandoned_session_minutes": str(abandoned_session_minutes), **dict(budget_values)},
        )
        text = (
            '[ "$(command -v df)" = ' + shlex.quote(str(self.bin_directory / "df")) + ' ] || exit 99\n'
            + offload.remote_test_command(config, request, offload.KnownRepository("hana's project"))
        )
        return subprocess.run(
            [*shell, "-c", text], cwd=self.home_directory,
            env={**os.environ, **self.configuration_environment,
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib/python_startup"),
                 "HOME": str(self.home_directory), "TMPDIR": str(self.temporary_directory),
                 "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                 "CLEANUP_TEST_DIR": str(self.root), "CLEANUP_TEST_STATUS": str(status),
                 "CLEANUP_TEST_DF_OUTPUT": disk_output, "CLEANUP_TEST_DF_STATUS": str(disk_status)},
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def cleanup_lines(self, answer: subprocess.CompletedProcess[str]) -> list[str]:
        lines = answer.stdout.splitlines()
        if lines[-1] == "mac_cleanup=skipped":
            return lines
        self.assertRegex(lines[2], r"^mac_trim=status=0 elapsed_seconds=[0-9]+ over_budget=(yes|no|unknown)$")
        return lines[:2] + lines[3:]

    def test_remote_text_removes_only_old_abandoned_sessions_before_building(self) -> None:
        minutes = 3
        cases: tuple[tuple[str, int, bool, tuple[str, ...]], ...] = (
            ("debug/incremental/crate/s-old-working", minutes + 2, False, ()),
            ("aarch64-apple-darwin/debug/incremental/crate/s-old-working", minutes + 2, False, ()),
            ("debug/incremental/crate/s-new-working", 1, True, ()),
            ("aarch64-apple-darwin/debug/incremental/crate/s-new-working", 1, True, ()),
            ("debug/incremental/crate/s-finished", minutes + 2, True, ()),
            ("aarch64-apple-darwin/debug/incremental/crate/s-finished", minutes + 2, True, ()),
            *((f"debug/{name}/incremental/crate/s-old-working", minutes + 2, True, ())
              for name in ("deps", "build", ".fingerprint", "examples")),
            ("deep/triple/debug/incremental/crate/s-old-working", minutes + 2, True, ()),
            ("debug/incremental/crate/s-old-working", minutes + 2, True, ("--target-dir", "other-target")),
            ("debug/incremental/crate/s-old-working", minutes + 2, True, ("--target-dir=other-target",)),
            ("", 0, False, ()),
        )
        target = self.mirror / "target"
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for relative, age, keep, words in cases:
                for status in (0, 42):
                    with self.subTest(shell=shell, session=relative, words=words, status=status):
                        shutil.rmtree(target, ignore_errors=True)
                        session = target / relative
                        lock = session.with_name(session.name.removesuffix("-working") + ".lock")
                        if relative:
                            session.mkdir(parents=True)
                            _ = (session / "compiler-output").write_text("session data")
                            _ = lock.write_text("session lock")
                            modified = time.time() - age * 60
                            os.utime(session, (modified, modified))
                            os.utime(lock, (modified, modified))
                        answer = self.run_remote(shell, status, words, free_floor_gib=50,
                                                 abandoned_session_minutes=minutes)
                        self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                        cleanup = "skipped" if words else "kept"
                        self.assertEqual(self.cleanup_lines(answer),
                                         ["nextest finished", f"mac_exit={status}", f"mac_cleanup={cleanup}"])
                        build_target = self.mirror / "other-target" if words else target
                        self.assertEqual((build_target / "nextest-output").read_text(), "built\n")
                        if relative:
                            self.assertEqual(session.exists(), keep)
                            self.assertEqual(lock.exists(), keep)
                            if keep:
                                self.assertEqual((session / "compiler-output").read_text(), "session data")
                                self.assertEqual(lock.read_text(), "session lock")

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
                    lines = self.cleanup_lines(answer)
                    self.assertEqual(lines[:2], ["nextest finished", f"mac_exit={status}"])
                    self.assertEqual(len(lines), 3)
                    self.assertRegex(lines[2], r"^mac_cleanup=0 [0-9]+ 99$")
                    cleanup = offload.parse_cleanup(lines[2].removeprefix("mac_cleanup="))
                    if not isinstance(cleanup, offload.CleanupDone):
                        self.fail("The removal marker did not report a completed cleanup.")
                    if not isinstance(cleanup.size, offload.MeasuredCleanupSize):
                        self.fail("The target size was not measured.")
                    self.assertGreater(cleanup.size.kib, 0)
                    self.assertEqual(answer.stderr, "")
                    self.assertFalse(target.exists())
                    self.assertEqual((self.root / "sweep-switch").read_text(), "1")
                    self.assertEqual(
                        cast(list[str], json.loads((self.root / "df-arguments.json").read_text())),
                        ["-g", str(self.home_directory)],
                    )
                    for path in (sibling, temporary_file, other_target):
                        self.assertEqual(path.read_text(), "keep\n")

    def test_failed_size_measurement_still_removes_the_target_under_sh_and_zsh(self) -> None:
        write_shell(self.bin_directory / "du", r'''
json_args "$@" > "$CLEANUP_TEST_DIR/du-arguments.json"
printf '12\t%s\n' "$2"
exit 1
''')
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_remote(shell, 0)
                self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                self.assertEqual(self.cleanup_lines(answer),
                                 ["nextest finished", "mac_exit=0", "mac_cleanup=0 - 99"])
                self.assertEqual(answer.stderr, "")
                self.assertFalse((self.mirror / "target").exists())
                self.assertEqual(json.loads((self.root / "du-arguments.json").read_text()),
                                 ["-sk", str(self.mirror / "target")])

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
                        self.assertEqual(self.cleanup_lines(answer), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")

        self.real_sweep()
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for default_kib, repository_kib, trim in ((96, 0, True), (256, 96, True), (96, 256, False)):
                    with self.subTest(shell=shell, status=status, default_kib=default_kib,
                                      repository_kib=repository_kib):
                        shutil.rmtree(target)
                        old = target / "debug/incremental/old-session/output.o"
                        new = target / "debug/incremental/new-session/output.o"
                        for path, age in ((old, 7200), (new, 600)):
                            path.parent.mkdir(parents=True)
                            _ = path.write_bytes(os.urandom(64 * 1024))
                            modified = time.time() - age
                            os.utime(path, (modified, modified))
                            os.utime(path.parent, (modified, modified))
                        (target / "debug/.fingerprint").mkdir()
                        overrides = (("mac_budget_gib.hana's project", str(repository_kib / 1024 ** 2)),) if repository_kib else ()
                        answer = self.run_remote(
                            shell, status, free_floor_gib=100,
                            disk_output="Filesystem GiB Used Available Capacity Mounted\nfixture 500 300 200 60% /\n",
                            budget_gib=str(default_kib / 1024 ** 2), budget_values=overrides,
                        )
                        self.assertEqual((answer.returncode, answer.stderr), (0, ""), answer.stdout)
                        self.assertEqual(self.cleanup_lines(answer),
                                         ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(old.exists(), not trim)
                        self.assertEqual(len(new.read_bytes()), 64 * 1024)
                        remaining = sum(path.stat().st_blocks * 512 for path in target.rglob("*") if path.is_file())
                        self.assertLessEqual(remaining, (repository_kib or default_kib) * 1024)

        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for trim_fails in (False, True):
                with self.subTest(shell=shell, trim_fails=trim_fails):
                    shutil.rmtree(target, ignore_errors=True)
                    target.mkdir()
                    retained = target / "unmatched-output"
                    _ = retained.write_bytes(os.urandom(128 * 1024))
                    if trim_fails:
                        write_shell(self.python, 'exit 9\n')
                    else:
                        self.real_sweep()
                    answer = self.run_remote(
                        shell, 42, budget_gib=str(96 / 1024 ** 2),
                        disk_output=("Filesystem GiB Used Available Capacity Mounted\n"
                                     + f"fixture 500 300 {99 if trim_fails else 200} 60% /\n"),
                    )
                    self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                    self.assertEqual(retained.exists(), not trim_fails)
                    lines = answer.stdout.splitlines()
                    trim_lines = [line for line in lines if line.startswith("mac_trim=")]
                    self.assertEqual(len(trim_lines), 1, answer.stdout)
                    self.assertRegex(trim_lines[0],
                                     rf"^mac_trim=status={9 if trim_fails else 0} elapsed_seconds=[0-9]+ over_budget=yes$")
                    self.assertNotIn("lint sweep:", answer.stdout)
                    if trim_fails:
                        self.assertRegex(lines[-1], r"^mac_cleanup=0 [0-9]+ 99$")
                    else:
                        self.assertEqual(lines[-1], "mac_cleanup=kept")

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
                        self.assertEqual(self.cleanup_lines(answer), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
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
                        self.assertEqual(self.cleanup_lines(answer), ["nextest finished", f"mac_exit={status}", "mac_cleanup=7"])
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
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)

    def test_activity_text_has_zero_for_only_shells_in_clones_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p101\nn/Users/test/rust/hana\np102\nn/Users/test/rust/other\n"),
            "ps": (0, "101 501 /bin/zsh\n102 501 /bin/sh\n103 999 /opt/cargo\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=0\nwork=0\nrc=0\n", ""))

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
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=7\nwork=5\nrc=0\n", ""))
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
        self.assertEqual(selector.call_args_list, [mock.call(HANA, "mac", 1), mock.call(offload.ALL_MIRRORS)])

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
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)

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
                                  capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.splitlines(), matching)




if __name__ == "__main__":
    _ = unittest.main()
