"""The rerun lens reads changed production operations without touching state."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SCRIPT = Path(__file__).with_name("rerun_lens.py")


def diff_file(path: str, lines: list[str], start: int = 1) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            + f"@@ -0,0 +{start},{len(lines)} @@\n"
            + "".join(f"+{line}\n" for line in lines))


class RerunLensTests(unittest.TestCase):
    def scan(self, diff: str) -> list[str]:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as directory:
            path = Path(directory) / "phase.diff"
            _ = path.write_text(diff)
            result = subprocess.run([sys.executable, str(SCRIPT), "arm", "--diff", str(path)],
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_text(), diff)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            return result.stdout.splitlines()

    def test_python_state_writes_moves_and_locks_name_the_changed_lines(self) -> None:
        lines = self.scan(diff_file("scripts/restore.py", [
            'state.write_text(json.dumps(record))', 'os.replace(source, destination)',
            'fcntl.flock(handle, fcntl.LOCK_EX)', 'json.dump(record, output)',
            'with open("state.jsonl", "a") as output:', '    output.write(body)',
            'shutil.move(record, history)', 'record.unlink()', 'shutil.rmtree(history)',
        ], 40))
        self.assertEqual(lines[0], "armed")
        for number, kind in ((40, "file write or append"), (41, "file move or delete"),
                             (42, "lock or claim"), (43, "JSON state write"),
                             (44, "file write or append"), (45, "file write or append"),
                             (46, "file move or delete"), (47, "file move or delete"),
                             (48, "file move or delete")):
            self.assertIn(f"scripts/restore.py:{number} — {kind}", lines)

    def test_shell_state_and_fail_fast_account_loop_arm_the_lens(self) -> None:
        lines = self.scan(diff_file("herdr-up.sh", [
            'set -e', 'for account in claude1 claude3; do', '  restart "$account"', 'done',
            'mv pending history/', 'rm stopped.json', 'echo pending > state.json',
            'mkdir "$claim_lock"',
        ]))
        self.assertEqual(lines[0], "armed")
        for number, kind in ((1, "fail-fast list"), (2, "fail-fast list"),
                             (5, "file move or delete"), (6, "file move or delete"),
                             (7, "file write or append"), (8, "lock or claim")):
            self.assertIn(f"herdr-up.sh:{number} — {kind}", lines)

    def test_rust_reservations_moves_locks_and_append_builders_arm_the_lens(self) -> None:
        lines = self.scan(diff_file("src/reservations.rs", [
            'std::fs::write(path, reservation)?;', 'fs::rename(path, claim)?;',
            'let lock = fd_lock::RwLock::new(file);', 'let file = File::create(path)?;',
            'let log = OpenOptions::new()', '    .append(true)', '    .open(path)?;',
            'fs::remove_file(path)?;', 'fs::remove_dir_all(root)?;',
        ]))
        self.assertEqual(lines[0], "armed")
        for number, kind in ((1, "file write or append"), (2, "file move or delete"),
                             (3, "lock or claim"), (4, "file write or append"),
                             (6, "file write or append"), (8, "file move or delete"),
                             (9, "file move or delete")):
            self.assertIn(f"src/reservations.rs:{number} — {kind}", lines)
        self.assertIn("Cargo.toml:1 — lock or claim", self.scan(diff_file("Cargo.toml", ['fd-lock = "4"'])))

    def test_docs_tests_fixtures_and_pure_computation_do_not_arm_the_lens(self) -> None:
        for path in ("docs/restore.md", "README.md", "tests/restore.rs", "scripts/test_restore.py",
                     "fixtures/restore.py", "src/restore.test.ts", "src/tests.rs", "src/restore_tests.rs"):
            with self.subTest(path=path):
                self.assertEqual(self.scan(diff_file(path, ['state.write_text(body)', 'os.replace(a, b)'])),
                                 ["not armed"])
        self.assertEqual(self.scan(diff_file("src/math.py", ['result = sum(values)', 'return json.dumps(result)'])),
                         ["not armed"])
        self.assertEqual(self.scan(diff_file("check.sh", ['set -e', 'echo ready > /dev/null'])), ["not armed"])

    def test_multiline_file_modes_and_extensionless_shell_writes_arm_the_lens(self) -> None:
        lines = self.scan(diff_file("restore.py", [
            'with state.open(', '    "w",', '    encoding="utf-8",', ') as output:', '    pass',
        ]))
        self.assertIn("restore.py:2 — file write or append", lines)
        lines = self.scan(diff_file("bin/restore", ['#!/usr/bin/env bash', 'mv record history/']))
        self.assertIn("bin/restore:2 — file move or delete", lines)

    def test_string_replacement_and_console_output_do_not_arm_the_lens(self) -> None:
        self.assertEqual(self.scan(diff_file("format.py", [
            'return name.replace("old", "new")', 'sys.stdout.write(message)',
        ])), ["not armed"])

    def test_existing_writes_do_not_arm_an_unrelated_changed_calculation(self) -> None:
        diff = ('--- a/restore.py\n+++ b/restore.py\n@@ -8,3 +8,3 @@\n'
                + ' state.write_text(body)\n-amount = 1\n+amount = 2\n return amount\n')
        self.assertEqual(self.scan(diff), ["not armed"])

    def test_changed_command_in_existing_fail_fast_loop_arms_the_lens(self) -> None:
        diff = ('--- a/up.sh\n+++ b/up.sh\n@@ -1,4 +1,4 @@\n set -e\n'
                + ' for account in one two; do\n-  old_restart "$account"\n'
                + '+  restart "$account"\n done\n')
        self.assertIn("up.sh:3 — fail-fast list", self.scan(diff))

    def test_added_loop_checks_fail_fast_context_across_hunks(self) -> None:
        diff = ('--- a/up.sh\n+++ b/up.sh\n@@ -1,2 +1,2 @@\n set -e\n-# old\n+# new\n'
                + '@@ -30,0 +30,3 @@\n+for account in one two; do\n+  restart "$account"\n+done\n')
        self.assertIn("up.sh:30 — fail-fast list", self.scan(diff))


    def test_path_replacement_uses_path_context_but_text_replacement_does_not(self) -> None:
        for operation in ("replace", "rename"):
            with self.subTest(operation=operation):
                diff = ('--- a/claim.py\n+++ b/claim.py\n@@ -1,3 +1,4 @@\n'
                        + ' def claim(path: Path) -> None:\n'
                        + '     sending = path.with_suffix(".sending")\n'
                        + f'+    path.{operation}(sending)\n'
                        + '     return\n')
                self.assertIn("claim.py:3 — file move or delete", self.scan(diff))
        self.assertEqual(self.scan(diff_file("format.py", [
            'text: str = "old"', 'text = text.replace("old", "new")',
        ])), ["not armed"])
        self.assertIn("claim.py:2 — file move or delete", self.scan(diff_file("claim.py", [
            'scratch = Path("claim.tmp")', 'scratch.replace(destination)',
        ])))

    def test_rust_lock_acquisition_uses_unchanged_imports(self) -> None:
        for crate, methods in (("fs2::FileExt", ("lock_exclusive", "lock_shared", "try_lock_exclusive", "try_lock_shared")),
                               ("fd_lock::RwLock", ("write", "try_write", "read", "try_read"))):
            for method in methods:
                with self.subTest(crate=crate, method=method):
                    diff = ('--- a/src/claim.rs\n+++ b/src/claim.rs\n@@ -1,1 +1,2 @@\n'
                            + f' use {crate};\n+let guard = lock.{method}()?;\n')
                    self.assertIn("src/claim.rs:2 — lock or claim", self.scan(diff))

    def test_test_support_and_rust_test_modules_do_not_arm_the_lens(self) -> None:
        for path in ("scripts/direct/review_launcher_test_support.py", "conftest.py"):
            with self.subTest(path=path):
                self.assertEqual(self.scan(diff_file(path, ['state.write_text(body)'])), ["not armed"])
        test_module = ['#[cfg(test)]', 'mod tests {', '    #[test]', '    fn saves() {',
                       '        fs::write(path, body)?;', '    }', '}']
        self.assertEqual(self.scan(diff_file("src/lib.rs", test_module)), ["not armed"])
        lines = self.scan(diff_file("src/lib.rs", test_module + ['fs::write(path, body)?;']))
        self.assertEqual(lines, ["armed", "src/lib.rs:8 — file write or append"])

    def test_binary_console_output_does_not_arm_the_lens(self) -> None:
        self.assertEqual(self.scan(diff_file("format.py", [
            'sys.stdout.buffer.write(body)', 'sys.stderr.buffer.write(body)',
        ])), ["not armed"])

    def test_operation_names_in_literals_are_not_operations(self) -> None:
        for path, source in (("scanner.py", [
                'pattern = r"fs2|fd-lock"', 'suffix = ".lock"',
                'pattern = r"open(.*write_text("',
                'example = "os.replace(source, target)"',
                'example = "with open(\\"state.json\\", \\"w\\")"',
            ]), ("src/scanner.rs", ['let pattern = r#"fd_lock fs::write(path, body) .lock"#;'])):
            with self.subTest(path=path):
                self.assertEqual(self.scan(diff_file(path, source)), ["not armed"])
        self.assertIn("claim.py:1 — lock or claim", self.scan(diff_file("claim.py", [
            'with open("owner.lock", "w") as output:', '    pass',
        ])))

    def test_fail_fast_report_names_only_changed_loop_steps_and_errexit(self) -> None:
        diff = ('--- a/up.sh\n+++ b/up.sh\n@@ -1,6 +1,8 @@\n'
                + ' set -e\n+echo ready\n for account in one two; do\n'
                + '   old_restart "$account"\n+  restart "$account"\n done\n'
                + ' echo complete\n # end\n')
        self.assertEqual(self.scan(diff), ["armed", "up.sh:1 — fail-fast list", "up.sh:5 — fail-fast list"])

if __name__ == "__main__":
    _ = unittest.main()
