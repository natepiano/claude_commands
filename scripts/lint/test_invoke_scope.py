#!/usr/bin/env python3
"""Build-step admission and behavior inside each step's systemd scope."""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast

import sys

if TYPE_CHECKING:
    from ..lib import process_start
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

INVOKE = Path(__file__).with_name("invoke.sh")
OOM_SCORE_ADJ = "/proc/self/oom_score_adj"


def scope_command() -> str:
    source = INVOKE.read_text(encoding="utf-8")
    match = re.search(r"^BUILDLOG_SCOPE_SH='([^'\n]*)'$", source, re.MULTILINE)
    if match is None:
        raise AssertionError("BUILDLOG_SCOPE_SH is missing or no longer a single-quoted string")
    return match.group(1)


def require_score_below_500(test: unittest.TestCase) -> None:
    if not Path(OOM_SCORE_ADJ).exists():
        test.skipTest(f"{OOM_SCORE_ADJ} is unavailable")
    if int(Path(OOM_SCORE_ADJ).read_text(encoding="utf-8")) >= 500:
        test.skipTest("this process already runs at oom_score_adj 500 or more, so a 500 in the step proves nothing")


class BuildScopeTests(unittest.TestCase):
    def run_scope(
        self, step: list[str], command: str | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "peak"
            return subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    'exec 3>&2; exec /bin/sh -c "$@"',
                    "scope-test",
                    command if command is not None else scope_command(),
                    str(marker),
                    *step,
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

    def test_step_has_oom_score_adj_500(self) -> None:
        require_score_below_500(self)
        result = self.run_scope(["sh", "-c", f"cat {OOM_SCORE_ADJ}"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "500\n")

    def test_child_of_step_inherits_oom_score_adj_500(self) -> None:
        require_score_below_500(self)
        result = self.run_scope(["sh", "-c", f"sh -c 'cat {OOM_SCORE_ADJ}'"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "500\n")

    def test_step_exit_status_passes_through(self) -> None:
        result = self.run_scope(["sh", "-c", "exit 7"])
        self.assertEqual(result.returncode, 7)

    def test_scope_writes_cgroup_sidecar_before_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "fake-cgroup"
            scope.mkdir()
            _ = (scope / "memory.peak").write_text("123\n")
            _ = (scope / "memory.pressure").write_text("some avg10=0 total=0\n")
            marker = root / "peak"
            command = scope_command().replace(
                '"/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"', f'"{scope}"'
            )
            result = subprocess.run(
                ["/bin/sh", "-c", 'exec 3>&2; exec /bin/sh -c "$@"',
                 "scope-test", command, str(marker), "sh", "-c",
                 'test -f "$1.cgroup" && cat "$1.cgroup"', "step", str(marker)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), str(scope))
            self.assertEqual((root / "peak.cgroup").read_text().strip(), str(scope))

    def test_failed_score_write_is_silent_and_step_runs_under_exported_errexit(self) -> None:
        readonly_score = Path("/proc/self/status")
        if not readonly_score.exists():
            self.skipTest(f"{readonly_score} is unavailable")
        command = scope_command()
        self.assertIn(OOM_SCORE_ADJ, command)
        result = self.run_scope(
            ["sh", "-c", "printf 'ran\\n'"],
            command.replace(OOM_SCORE_ADJ, str(readonly_score)),
            {**os.environ, "SHELLOPTS": "errexit"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ran\n")
        self.assertEqual(result.stderr, "")

    def test_sampler_records_near_peak_use_with_rate_limit_and_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "scope"
            scope.mkdir()
            sidecar = root / "scope-path"
            _ = sidecar.write_text(str(scope) + "\n")
            maximum = root / "step.max"
            near_peak = root / "step.wave"
            _ = near_peak.write_text("900\n")
            program = INVOKE.read_text().split("buildlog_sample_anon() {", 1)[1].split("\n}\n", 1)[0]
            program = "buildlog_sample_anon() {" + program + '''
}
times=(1000 1059 1060 1120 1121 1122 1182 1183)
values=(100 50 50 49 50 200 99 100)
index=0
date() { printf "%s\\n" "${times[index]}"; }
sleep() {
    printf "%s:%s\\n" "$(cat "$maximum_path")" "$(cat "$near_peak_path")"
    index=$((index + 1))
    (( index < ${#times[@]} )) || exit 0
    printf "anon %s\\n" "${values[index]}" > "$scope_path/memory.stat"
}
maximum_path=$2
near_peak_path=${2%.max}.wave
scope_path=$3
printf "anon %s\\n" "${values[index]}" > "$scope_path/memory.stat"
buildlog_sample_anon "$1" "$2" "$$"
'''
            with near_peak.open() as old_snapshot:
                result = subprocess.run(["bash", "-c", program, "sampler-test", str(sidecar),
                                         str(maximum), str(scope)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [
                    "100:100 1000", "100:100 1000", "100:100 1060", "100:100 1060", "100:100 1121",
                    "200:200 1122", "200:200 1122", "200:200 1183"])
                self.assertEqual(old_snapshot.read(), "900\n")

    def test_sampler_keeps_new_peak_when_wave_publish_and_cleanup_fail(self) -> None:
        for fail_first_peak in (False, True):
            with self.subTest(fail_first_peak=fail_first_peak), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                scope = root / "scope"
                scope.mkdir()
                sidecar = root / "scope-path"
                _ = sidecar.write_text(str(scope) + "\n")
                maximum = root / "step.max"
                _ = maximum.write_text("100\n")
                _ = maximum.with_suffix(".wave").write_text("100 1\n")
                program = INVOKE.read_text().split("buildlog_sample_anon() {", 1)[1].split("\n}\n", 1)[0]
                program = "set -e\nbuildlog_sample_anon() {" + program + r'''
}
values=(100 200 200 0)
index=0
date() { if (( index == 0 )); then printf '1\n'; else printf '3000\n'; fi; }
mv() {
    if [[ "$3" == *.wave && "$index" != 0 ]]; then return 1; fi
    if [[ "$3" == *.max && "$index" == 1 && "$fail_first_peak" == 1 ]]; then return 1; fi
    command mv "$@"
    if [[ "$index" == 2 ]]; then
        printf 'between:%s:%s\n' "$(cat "$maximum_path")" "$(cat "${maximum_path%.max}.wave")"
    fi
}
rm() { return 1; }
sleep() {
    printf '%s\n' "$(cat "$maximum_path")"
    index=$((index + 1))
    (( index < ${#values[@]} )) || exit 0
    printf 'anon %s\n' "${values[index]}" > "$scope_path/memory.stat"
}
maximum_path=$2
scope_path=$3
fail_first_peak=$4
if [[ "$fail_first_peak" == 0 ]]; then values=(100 200 0); fi
printf 'anon 100\n' > "$scope_path/memory.stat"
buildlog_sample_anon "$1" "$2" "$$"
'''
                result = subprocess.run(["bash", "-c", program, "sampler-test", str(sidecar),
                                         str(maximum), str(scope), str(int(fail_first_peak))],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = (["100", "100", "between:200:100 1", "200", "200"] if fail_first_peak
                            else ["100", "200", "200"])
                self.assertEqual(result.stdout.splitlines(), expected)
                self.assertEqual(maximum.read_text(), "200\n")
                self.assertEqual(maximum.with_suffix(".wave").read_text(), "100 1\n")

    def test_sampler_exits_when_its_parent_shell_dies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shell: subprocess.Popen[str] = subprocess.Popen(
                ["bash", "-c", 'source "$1"; parent=$BASHPID; buildlog_sample_anon "$2" "$3" "$parent" & printf "%s\\n" "$!"; wait',
                 "sampler-test", str(INVOKE), str(root / "scope.cgroup"), str(root / "maximum")],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                env={**os.environ,
                     "BUILD_HOLD_DIR": str(root / "holders"),
                     "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                     "UNIT_ACTIVE_DIR": str(root / "active"),
                     "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                     "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                     "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": directory, "LINT_CONFIG_READER": str(root / "missing-config")},
            )
            sampler_pid = 0
            try:
                assert shell.stdout is not None
                sampler_pid = int(cast(str, shell.stdout.readline()).strip())
                os.kill(shell.pid, signal.SIGKILL)
                _ = shell.wait(timeout=TEST_HANG_GUARD_SECS)
                deadline = time.monotonic() + TEST_HANG_GUARD_SECS
                while time.monotonic() < deadline and self.process_running(sampler_pid):
                    time.sleep(0.05)
                self.assertFalse(self.process_running(sampler_pid), "sampler survived its parent shell")
            finally:
                if shell.poll() is None:
                    shell.kill()
                    _ = shell.wait(timeout=TEST_HANG_GUARD_SECS)
                if sampler_pid and self.process_running(sampler_pid):
                    os.kill(sampler_pid, signal.SIGKILL)
                if shell.stdout is not None:
                    shell.stdout.close()

    @staticmethod
    def process_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            return process_start.process_state(pid) is not process_start.ProcessRunState.GONE
        except process_start.ProcessReadError:
            return True

    def test_unreserved_scoped_step_removes_cgroup_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "fake-cgroup"
            scope.mkdir()
            _ = (scope / "memory.peak").write_text("123\n")
            _ = (scope / "memory.pressure").write_text("some avg10=0 total=0\n")
            command = scope_command().replace(
                '"/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"', f'"{scope}"'
            )
            shell = (
                'source "$1"; BUILDLOG_SCOPE_SH=$2; '
                'buildlog_begin() { BUILDLOG_PEAK="$TMPDIR/buildlog.test.peak"; BUILDLOG_START=""; }; '
                'sweep_after_step() { :; }; '
                'systemd-run() { while [[ "$1" != -- ]]; do shift; done; shift; "$@"; }; '
                'run_once sh -c \'test -f "$1.cgroup"\' step "$TMPDIR/buildlog.test.peak" "$TMPDIR/sweep.py"'
            )
            result = subprocess.run(
                ["bash", "-c", shell, "sidecar-test", str(INVOKE), command],
                capture_output=True, text=True, check=False,
                env={**os.environ,
                     "BUILD_HOLD_DIR": str(root / "holders"),
                     "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                     "UNIT_ACTIVE_DIR": str(root / "active"),
                     "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                     "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                     "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": directory, "TMPDIR": directory,
                     "LINT_CONFIG_READER": str(root / "missing-config")},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(root.glob("*.peak.cgroup")), [])


class SweepStartupTests(unittest.TestCase):
    def sweep_events(self, switch: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reader, writer = os.pipe()
            process = subprocess.Popen(
                ["bash", "-c", 'source "$1"; invoke_sweep() { printf "started\\n" >&"$SWEEP_TEST_FD"; }; sweep_after_step test-step',
                 "sweep-test", str(INVOKE)],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                pass_fds=(writer,),
                env={**os.environ, "HOME": directory, "TMPDIR": directory,
                     "XDG_RUNTIME_DIR": str(root / "runtime"), "LINT_INVOKE_SOURCED": "",
                     "LINT_CONFIG_READER": str(root / "missing-config"), "LINT_SKIP_SWEEP": switch,
                     "SWEEP_TEST_FD": str(writer)},
            )
            os.close(writer)
            try:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, output + errors)
                events = b""
                deadline = time.monotonic() + TEST_HANG_GUARD_SECS
                while True:
                    ready, _, _ = select.select([reader], [], [], max(0, deadline - time.monotonic()))
                    self.assertTrue(ready, "sweep stand-in still holds its pipe")
                    chunk = os.read(reader, 4096)
                    if not chunk:
                        break
                    events += chunk
                if switch == "1":
                    self.assertFalse((root / "runtime" / "lint-sweep").exists())
                return events.decode()
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                os.close(reader)

    def test_offload_switch_starts_no_detached_sweep(self) -> None:
        self.assertEqual(self.sweep_events("1"), "")

    def test_without_offload_switch_the_detached_sweep_still_starts(self) -> None:
        self.assertEqual(self.sweep_events(""), "started\n")

    def test_switch_set_to_zero_keeps_detached_sweep_enabled(self) -> None:
        self.assertEqual(self.sweep_events("0"), "started\n")


class BuildAdmissionTests(unittest.TestCase):
    def run_invoke(
        self, root: Path, shell: str, arguments: list[str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", 'set -euo pipefail; source "$1"; shift; ' + shell,
             "admission-test", str(INVOKE), *arguments],
            capture_output=True, text=True, check=False,
            env={**os.environ,
                 "HOME": str(root), "TMPDIR": str(root),
                 "BUILDLOG_DIR": str(root / "buildlog"),
                 "BUILD_HOLD_DIR": str(root / "holders"),
                 "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                 "UNIT_ACTIVE_DIR": str(root / "active"),
                 "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                 "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                 "MAC_TEST_STATE_DIR": str(root / "mac-state"),
                 "LINT_CONFIG_READER": str(root / "missing-config")},
        )

    def test_taplo_does_not_compile_and_existing_command_classification_is_preserved(self) -> None:
        cases = [
            (["taplo", "fmt", "--check"], 1),
            (["cargo", "clippy", "--workspace", "--all-targets", "--all-features"], 0),
            (["env", "RUSTC_WRAPPER=", "cargo", "mend", "--workspace"], 0),
            (["cargo", "+nightly", "fmt", "--all", "--check"], 1),
            (["python3", "/tmp/sweep.py", "--check"], 1),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for arguments, expected_status in cases:
                with self.subTest(arguments=arguments):
                    result = self.run_invoke(Path(directory), 'buildlog_step_compiles "$@"', arguments)
                    self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)

    def test_taplo_skips_build_hold_and_memory_admission_but_records_its_status(self) -> None:
        shell = r'''
events="$TMPDIR/events"
build_hold_wait_local() { echo hold >> "$events"; }
buildlog_wait_for_memory() {
    echo memory >> "$events"
    BUILDLOG_MEM_WAIT_S=0 BUILDLOG_MEM_OUTCOME=Granted
}
build_hold_check_local() { return 0; }
buildlog_begin() {
    printf 'begin %s\n' "$*" >> "$events"
    BUILDLOG_START="" BUILDLOG_PEAK="" BUILDLOG_MEM_RESERVATION=""
    BUILDLOG_MEM_WAIT_S=0
}
buildlog_end() {
    local recorded_status=$1
    shift 3
    printf 'end %s %s\n' "$recorded_status" "$*" >> "$events"
}
buildlog_release_memory() { :; }
sweep_after_step() { :; }
step_status=$1
taplo() { printf 'checked Cargo.toml\n'; return "$step_status"; }
run_once taplo fmt --check
'''
        for status in (0, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = self.run_invoke(root, shell, [str(status)])
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertIn("checked Cargo.toml", result.stdout)
                self.assertEqual(
                    (root / "events").read_text().splitlines(),
                    ["begin taplo fmt --check", f"end {status} taplo fmt --check"],
                )


if __name__ == "__main__":
    _ = unittest.main()
