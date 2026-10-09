#!/usr/bin/env python3
"""Exercise workspace platform auditing with disposable repositories."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import final, override


SCRIPT = Path(__file__).with_name("audit.py")


@final
class AuditCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.workspace = Path()
        self.config_path = Path()
        self.state_directory = Path()
        self.bin_directory = Path()
        self.command_log = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "workspace"
        self.config_path = self.root / "mac_test.conf"
        self.state_directory = self.root / "state"
        self.bin_directory = self.root / "bin"
        self.command_log = self.root / "commands"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        self.state_directory.mkdir()
        self.bin_directory.mkdir()
        self.write_workspace()
        self.write_config("linux-target,compound-review")
        self.write_command_stands()
        self.environment = {
            **os.environ,
            "MAC_TEST_CONFIG": str(self.config_path),
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_COMMAND_LOG": str(self.command_log),
            "PATH": str(self.bin_directory),
        }

    def write_workspace(self) -> None:
        self.write_file(
            self.workspace / "Cargo.toml",
            """\
[workspace]
resolver = "2"
members = ["crates/*"]
exclude = ["crates/excluded"]
""",
        )
        self.write_member(
            "linux-target",
            """\
[target.'cfg(target_os = "linux")'.dependencies]
cc = "1"
libc = "0.2"
""",
        )
        self.write_file(
            self.workspace / "crates/linux-target/build.rs",
            """\
#[cfg(target_family = "unix")]
fn unix_build() {}

fn main() {}
""",
        )
        self.write_member("linux-test")
        self.write_file(
            self.workspace / "crates/linux-test/tests/linux.rs",
            """\
#[cfg(not(target_os = "macos"))]
#[test]
fn linux_behavior() {}
""",
        )
        self.write_member(
            "mac-dependency",
            """\
[target.'cfg(target_os = "macos")'.dev-dependencies]
objc2 = "0.5"
""",
        )
        self.write_file(
            self.workspace / "crates/mac-dependency/src/windows.rs",
            """\
#[cfg(not(unix))]
pub fn windows_api() {}
""",
        )
        self.write_member("compound-review")
        self.write_file(
            self.workspace / "crates/compound-review/src/lib.rs",
            """\
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn platform() {}
""",
        )
        self.write_member("ordinary")
        self.write_member("excluded")
        self.write_file(
            self.workspace / "crates/excluded/src/lib.rs",
            """\
#[cfg(target_os = "linux")]
pub fn excluded_platform() {}
""",
        )

    def write_member(self, directory_name: str, extra_manifest: str = "") -> None:
        member = self.workspace / "crates" / directory_name
        self.write_file(
            member / "Cargo.toml",
            f"""\
[package]
name = "{directory_name}"
version = "0.1.0"
edition = "2021"

{extra_manifest}""",
        )
        self.write_file(member / "src/lib.rs", "pub fn member() {}\n")

    def write_config(self, packages: str) -> None:
        _ = self.config_path.write_text(
            f"linux_only.{self.workspace.name}={packages}\n", encoding="utf-8"
        )

    def write_command_stands(self) -> None:
        stand = self.bin_directory / "command-stand"
        _ = stand.write_text(
            """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$MAC_TEST_COMMAND_LOG"
exit 97
""",
            encoding="utf-8",
        )
        stand.chmod(0o755)
        for name in ("cargo", "git", "gh", "systemd-run", "ssh", "rsync", "scp"):
            (self.bin_directory / name).symlink_to(stand)

    @staticmethod
    def write_file(path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(contents, encoding="utf-8")

    def run_audit(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.workspace), *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if self.command_log.exists():
            self.fail(self.command_log.read_text(encoding="utf-8"))
        return result

    def table(self, output: str) -> tuple[list[str], dict[str, list[str]]]:
        table_lines = [line for line in output.splitlines() if line.startswith("|")]
        self.assertGreaterEqual(len(table_lines), 3, output)
        headings = self.cells(table_lines[0])
        rows = {
            cells[0]: cells
            for line in table_lines[2:]
            if (cells := self.cells(line))
        }
        return headings, rows

    @staticmethod
    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    def test_reports_members_predicates_listing_and_notes(self) -> None:
        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        headings, rows = self.table(result.stdout)
        self.assertEqual(
            headings,
            [
                "Package",
                "Gated sites (src)",
                "Gated sites (tests)",
                "Target dependencies",
                "Listed",
                "Suggestion",
            ],
        )
        self.assertEqual(
            set(rows),
            {
                "linux-target",
                "linux-test",
                "mac-dependency",
                "compound-review",
                "ordinary",
            },
        )
        self.assertNotIn("excluded", result.stdout)

        self.assertEqual(rows["linux-target"][1:3], ["1", "0"])
        self.assertIn("cc", rows["linux-target"][3])
        self.assertIn("libc", rows["linux-target"][3])
        self.assertEqual(rows["linux-target"][4:], ["yes", "keep on natedev"])

        self.assertEqual(rows["linux-test"][1:3], ["0", "1"])
        self.assertEqual(rows["linux-test"][4:], ["no", "can go to the Mac"])

        self.assertEqual(rows["mac-dependency"][1:3], ["1", "0"])
        self.assertIn("objc2", rows["mac-dependency"][3])
        self.assertEqual(rows["mac-dependency"][4:], ["no", "can go to the Mac"])

        self.assertEqual(rows["compound-review"][1:3], ["1", "0"])
        self.assertEqual(rows["compound-review"][4:], ["yes", "needs review"])
        self.assertEqual(rows["ordinary"][1:3], ["0", "0"])
        self.assertEqual(rows["ordinary"][4:], ["no", "can go to the Mac"])

        package_order = list(rows)
        self.assertEqual(package_order[0], "linux-target")
        self.assertEqual(package_order[-1], "ordinary")

        notes = [
            line
            for line in result.stdout.splitlines()
            if line and not line.startswith("|")
        ]
        self.assertEqual(len(notes), 3, result.stdout)
        self.assertIn("Listing disagreements: none.", notes)
        review = next(line for line in notes if "any(" in line)
        self.assertIn("compound-review", review)
        self.assertIn('target_os = "linux"', review)
        self.assertIn('target_os = "macos"', review)
        self.assertIn(
            "Not built on the Mac: linux-test "
            + "(crates/linux-test/tests/linux.rs:1).",
            notes,
        )

    def test_check_fails_for_unlisted_linux_target_dependency(self) -> None:
        self.write_config("compound-review")

        result = self.run_audit("--check")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Listing disagreements: linux-target.", result.stdout)

    def test_check_allows_source_linux_only_and_unclassified_members(self) -> None:
        result = self.run_audit("--check")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("linux-test", result.stdout)
        self.assertIn("compound-review", result.stdout)
        self.assertIn("needs review", result.stdout)

    def test_reads_multiline_cfg_attr_cfg_macro_and_raw_os_lines(self) -> None:
        self.write_member("cfg-attr")
        self.write_file(
            self.workspace / "crates/cfg-attr/src/lib.rs",
            """\
#[cfg_attr(target_os = "linux", path = "linux.rs")]
pub fn platform() {}
""",
        )
        self.write_member("multiline")
        self.write_file(
            self.workspace / "crates/multiline/src/lib.rs",
            """\
#[cfg(
    target_os = "linux"
)]
pub fn platform() {}
""",
        )
        self.write_member("cfg-macro")
        self.write_file(
            self.workspace / "crates/cfg-macro/src/lib.rs",
            """\
pub const ENABLED: bool = cfg!(target_os = "linux");
""",
        )
        self.write_member("raw-os-line")
        self.write_file(
            self.workspace / "crates/raw-os-line/build.rs",
            """\
if target_os == "linux" {
}
""",
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _, rows = self.table(result.stdout)
        for package in ("cfg-attr", "multiline", "cfg-macro"):
            self.assertEqual(rows[package][1], "1")
            self.assertEqual(rows[package][5], "can go to the Mac")
        self.assertEqual(rows["raw-os-line"][1], "1")
        self.assertEqual(rows["raw-os-line"][5], "needs review")
        self.assertIn('raw-os-line (if target_os == "linux" {)', result.stdout)
        self.assertIn(
            "Not built on the Mac: cfg-attr (crates/cfg-attr/src/lib.rs:1); "
            + "cfg-macro (crates/cfg-macro/src/lib.rs:1); "
            + "linux-test (crates/linux-test/tests/linux.rs:1); "
            + "multiline (crates/multiline/src/lib.rs:1).",
            result.stdout,
        )

    def test_reports_when_no_linux_only_code_is_omitted(self) -> None:
        self.write_file(
            self.workspace / "Cargo.toml",
            """\
[workspace]
resolver = "2"
members = ["crates/ordinary"]
""",
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Not built on the Mac: none.", result.stdout)

    def test_unknown_repository_reports_unknown_and_exits_two(self) -> None:
        (self.workspace / ".git").rmdir()

        result = self.run_audit("--check")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        _, rows = self.table(result.stdout)
        self.assertTrue(rows)
        self.assertTrue(all(row[4] == "unknown" for row in rows.values()))
        repository_note = next(
            line
            for line in result.stdout.splitlines()
            if line and not line.startswith("|") and "git repository" in line
        )
        self.assertIn(str(self.workspace), repository_note)


if __name__ == "__main__":
    _ = unittest.main()
