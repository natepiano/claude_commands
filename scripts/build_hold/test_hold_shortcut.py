"""Count hold checks with temporary state and a shell command stand-in."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import override

import test_build_hold


MEMORY_GATE = Path(__file__).resolve().parent.parent / "lint" / "memory_gate.sh"
BASH = shutil.which("bash") or "/bin/bash"
EMPTY_ENVIRONMENT: Mapping[str, str] = MappingProxyType({})


class HoldShortcutTests(test_build_hold.IsolatedBuildHoldTest):
    holders: Path = Path()
    starts: Path = Path()
    wait_file: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.holders = self.scratch / "holders"
        self.holders.mkdir()
        self.starts = self.scratch / "starts"
        self.wait_file = self.scratch / "wait-seconds"
        bin_dir = self.scratch / "bin"
        bin_dir.mkdir()
        python = bin_dir / "python3"
        _ = python.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_STARTS"\nexit "$TEST_STATUS"\n')
        python.chmod(0o755)
        self.environment = {
            **os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BUILDLOG_CALL_ID": "c1", "BUILDLOG_CALLER": "verify",
            "BUILDLOG_HOLD_PID": "12345", "BUILDLOG_HOLD_WAIT_FILE": str(self.wait_file),
            "TEST_STARTS": str(self.starts), "TEST_STATUS": "0", "GLOBIGNORE": "",
        }

    def run_checks(self, setting: str = "", *, extra: Mapping[str, str] = EMPTY_ENVIRONMENT) -> subprocess.CompletedProcess[str]:
        self.starts.unlink(missing_ok=True)
        script = '''set -euo pipefail
source "$1"
'''+ setting + '''
shortcut=0; build_hold_nothing_held || shortcut=$?
wait_status=0; build_hold_wait_local || wait_status=$?
admission=0; build_hold_check_local || admission=$?
printf '%s %s %s\\n' "$shortcut" "$wait_status" "$admission"
'''
        return subprocess.run([BASH, "-c", script, "bash", str(MEMORY_GATE)],
                              cwd=self.scratch, env={**self.environment, **extra},
                              capture_output=True, text=True, check=False, timeout=5)

    def assert_checks(self, *, shortcut: int, status: int = 0, setting: str = "",
                      extra: Mapping[str, str] = EMPTY_ENVIRONMENT, starts: bool = True) -> None:
        result = self.run_checks(setting, extra={"TEST_STATUS": str(status), **extra})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{shortcut} {status} {status}")
        self.assertEqual(result.stderr, "")
        commands = self.starts.read_text().splitlines() if self.starts.exists() else []
        script = MEMORY_GATE.parent.parent / "build_targets" / "build_targets.py"
        self.assertEqual(commands, [f"{script} wait-local --call-id c1 --pid 12345",
                                    f"{script} check-local --caller verify"] if starts else [])

    def release_record(self) -> None:
        release = self.scratch / "release"
        cycle_id = "c" * 32
        (release / cycle_id).mkdir(parents=True, exist_ok=True)
        _ = (release / "current").write_text(cycle_id)
        _ = (release / cycle_id / "cycle.json").write_text('{"release_started_at":"2026-10-13T00:00:00+00:00"}')

    def test_empty_holder_directory_starts_no_hold_check(self) -> None:
        self.assert_checks(shortcut=0, starts=False)
        self.assertFalse(self.wait_file.exists())

    def test_holder_file_starts_the_same_commands_as_before(self) -> None:
        _ = (self.holders / "holder").write_text("held")
        for status in (0, 10, 12):
            with self.subTest(status=status):
                self.assert_checks(shortcut=1, status=status)

    def test_open_release_starts_the_same_commands(self) -> None:
        self.release_record()
        _ = (self.holders / "holder").write_text("held")
        self.assert_checks(shortcut=1)

    def test_finished_release_record_alone_starts_no_check(self) -> None:
        self.release_record()
        self.assert_checks(shortcut=0, starts=False)

    def test_any_doubt_starts_the_check(self) -> None:
        for kind in ("subdirectory", "dot", "two dots", "link", "missing", "file", "no read", "no search", "empty variable", "holder"):
            with self.subTest(kind=kind):
                if kind in ("no read", "no search") and os.getuid() == 0:
                    self.skipTest("Root can read directories without permission")
                folder = self.scratch / kind
                extra = {"BUILD_HOLD_DIR": str(folder)}
                if kind == "file":
                    _ = folder.write_text("file")
                elif kind != "missing":
                    folder.mkdir()
                    if kind == "subdirectory":
                        (folder / "child").mkdir()
                    elif kind in ("dot", "two dots", "holder"):
                        _ = (folder / {"dot": ".holder", "two dots": "..holder", "holder": "holder"}[kind]).write_text("held")
                    elif kind == "link":
                        (folder / "dangling").symlink_to(folder / "absent")
                    elif kind == "empty variable":
                        extra["BUILD_HOLD_DIR"] = ""
                    elif kind == "no read":
                        folder.chmod(0o300)
                    elif kind == "no search":
                        folder.chmod(0o400)
                try:
                    self.assert_checks(shortcut=1, extra=extra)
                finally:
                    if kind in ("no read", "no search"):
                        folder.chmod(0o700)

    def test_shell_settings_never_hide_an_entry(self) -> None:
        for setting in ("set -f", "GLOBIGNORE='*'", "shopt -s failglob", "shopt -s nullglob", "shopt -s dotglob"):
            for kind in ("holder", ".holder", "release", "empty"):
                with self.subTest(setting=setting, kind=kind):
                    for path in self.holders.iterdir():
                        path.unlink()
                    if kind != "empty":
                        _ = (self.holders / (".holder" if kind == ".holder" else "holder")).write_text("held")
                    if kind == "release":
                        self.release_record()
                    empty = kind == "empty" and setting in ("shopt -s nullglob", "shopt -s dotglob")
                    self.assert_checks(shortcut=0 if empty else 1, setting=setting, starts=not empty)

    def test_mac_runs_are_unchanged(self) -> None:
        _ = (self.holders / "holder").write_text("held")
        for setting, extra in (("", {"BUILDLOG_CALLER": "verify-mac"}),
                               ("OSTYPE=darwin23", {"BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "cargo-port"})):
            with self.subTest(setting=setting, extra=extra):
                self.assert_checks(shortcut=1, setting=setting, extra=extra, starts=False)

    def test_caller_without_a_call_id_waits_under_its_own_id(self) -> None:
        _ = (self.holders / "holder").write_text("held")
        script = MEMORY_GATE.parent.parent / "build_targets" / "build_targets.py"
        for caller, name in (("cargo-port", "cargo-port"), ("", "lint")):
            with self.subTest(caller=caller):
                result = self.run_checks(extra={"BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": caller})
                self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, "1 0 0", ""))
                wait, admission = self.starts.read_text().splitlines()
                call_id = wait.split()[3]
                self.assertRegex(call_id, r"^v[0-9]{8}T[0-9]{6}-[0-9]+-[0-9]+$")
                self.assertEqual(wait, f"{script} wait-local --call-id {call_id} --pid 12345 --caller {name}")
                self.assertEqual(admission, f"{script} check-local --caller {name}")

    def test_caller_without_a_holder_directory_starts_no_check(self) -> None:
        extra = {"BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "cargo-port", "BUILD_HOLD_DIR": str(self.scratch / "missing")}
        self.assert_checks(shortcut=1, extra=extra, starts=False)

    def test_nothing_held_starts_no_process(self) -> None:
        empty_path = self.scratch / "empty-path"
        empty_path.mkdir()
        forks = self.scratch / "subshells"
        result = subprocess.run([BASH, "-c", '''set -euo pipefail
source "$1"
PATH="$2"
# Trace inside command substitutions and (...) as well as function calls.
exec 3>"$3"
set -T
trap 'if (( BASH_SUBSHELL > 0 )); then printf "%s\\n" "$BASH_SUBSHELL" >&3; fi' DEBUG
for ((n=0; n<2000; n++)); do build_hold_nothing_held; done
[[ "$n" -eq 2000 ]]
''', "bash", str(MEMORY_GATE), str(empty_path), str(forks)], cwd=self.scratch,
                                env=self.environment, capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(forks.read_text(), "", "the shortcut must not fork subshells")
