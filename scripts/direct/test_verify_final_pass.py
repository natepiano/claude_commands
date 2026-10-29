"""A full gate proves one clean tree; a read-only query checks that proof."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import cast, final, override
import unittest

from scripts.direct.test_verify_untested_examples import ExampleWorkspaceFixture, VERIFY


@final
class FinalPassTests(ExampleWorkspaceFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        fixture = self.root
        git = fixture / "stubs/git"
        git.unlink()
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required")
        _ = git.write_text(f'#!/bin/sh\nexec "{real_git}" "$@"\n')
        git.chmod(0o755)
        self.root = fixture / "repo"
        _ = self.package()
        with (self.root / "Cargo.toml").open("a") as stream:
            _ = stream.write('\n[workspace.metadata.dev]\nfeatures = ["extra", "extra"]\n')
        self.environment.update({"PACKAGE_ROOT": str(self.root), "IN_NIX_SHELL": "1",
                                 "VERIFY_STATE_DIR": str(fixture / "verify-state"),
                                 "UNIT_SESSION_DIR": "", "NEXTEST_TEST_THREADS": "2"})
        _ = self.git("init", "--quiet")
        _ = self.git("config", "user.name", "Test Worker")
        _ = self.git("config", "user.email", "worker@example.invalid")
        _ = self.git("add", ".")
        _ = self.git("commit", "--quiet", "-m", "initial")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.environment,
                                       text=True, stderr=subprocess.PIPE).strip()

    def pass_files(self) -> list[Path]:
        return list((Path(self.environment["VERIFY_STATE_DIR"]) / "final_pass").glob("*.json"))

    def test_clean_final_records_the_tree_and_query_runs_no_cargo_or_admission(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tree = self.git("write-tree")
        self.assertEqual([path.name for path in self.pass_files()], [f"{tree}.json"])
        row = cast(dict[str, object], json.loads(self.pass_files()[0].read_text()))
        self.assertEqual(row["tree"], tree)
        self.assertEqual(row["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(row["repo"], str(self.root))
        self.assertEqual(row["verify_sha256"], hashlib.sha256(VERIFY.read_bytes()).hexdigest())
        self.assertEqual(row["features"], "extra")
        self.assertEqual(row["status"], 0)
        calls = self.calls_made()
        metadata_count = self.metadata_count()
        self.environment.update({"BUILD_TARGETS_SCRIPT": "/missing-checker", "IN_NIX_SHELL": "",
                                 "BUILD_HOLD_DIR": "/missing-hold-root"})
        for args in (("final-pass",), ("final-pass", tree)):
            with self.subTest(args=args):
                answer = self.verify(*args)
                self.assertEqual(answer.returncode, 0, answer.stderr)
                self.assertEqual(json.loads(answer.stdout), row)
        self.assertEqual(self.calls_made(), calls)
        self.assertEqual(self.metadata_count(), metadata_count)
        self.environment["IN_NIX_SHELL"] = "1"
        self.environment["BUILD_TARGETS_SCRIPT"] = str(VERIFY.parent.parent / "build_targets/build_targets.py")
        again = self.verify("final")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.calls_made(), calls + calls)

    def test_query_refuses_a_changed_compiler(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stderr)
        compiler = Path(self.environment["HOME"]) / "stubs/rustc"
        _ = compiler.write_text('#!/bin/sh\nprintf "different compiler\\n"\n')
        answer = self.verify("final-pass")
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertIn("rustc differs", answer.stderr)

    def test_query_refuses_a_changed_verify_script(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = Path(self.environment["HOME"]) / "changed/direct"
        folder.mkdir(parents=True)
        (folder.parent / "lib").symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
        _ = shutil.copyfile(VERIFY.parent / "final_pass.py", folder / "final_pass.py")
        changed = folder / "verify.sh"
        _ = changed.write_text(VERIFY.read_text() + "\n# Test script change.\n")
        answer = subprocess.run(["bash", str(changed), "final-pass"], cwd=self.root,
                                env=self.environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertIn("verify.sh differs", answer.stderr)

    def test_dirty_final_writes_no_record_and_default_query_refuses(self) -> None:
        source = self.root / "src/lib.rs"
        for kind in ("unstaged", "staged", "untracked"):
            with self.subTest(kind=kind):
                _ = self.git("reset", "--hard", "HEAD")
                untracked = self.root / "note"
                untracked.unlink(missing_ok=True)
                if kind == "untracked":
                    _ = untracked.write_text("unsaved\n")
                else:
                    _ = source.write_text("pub fn value() -> u8 { 2 }\n")
                    if kind == "staged":
                        _ = self.git("add", "src/lib.rs")
                result = self.verify("final")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_files(), [])
                self.assertIn("no final pass record was written: the tree is dirty", result.stdout)
                answer = self.verify("final-pass")
                self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
                self.assertIn("the tree is dirty", answer.stderr)

    def test_failed_final_writes_no_record_and_keeps_tool_status(self) -> None:
        self.environment["FAKE_CHECK_EXIT"] = "42"
        result = self.verify("final")
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("no final pass record was written: final failed (exit 42)", result.stdout)

    def test_edit_during_final_writes_no_record(self) -> None:
        self.environment["EDIT_DURING_TEST"] = "1"
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("no final pass record was written: the tree is dirty", result.stdout)

    def test_committed_tree_change_during_final_writes_no_record(self) -> None:
        self.environment["EDIT_DURING_TEST"] = "1"
        cargo = Path(self.environment["HOME"]) / "stubs/cargo"
        text = cargo.read_text().replace(
            'printf "// changed during the test run\\n" >> "$PACKAGE_ROOT/${EDIT_DURING_TEST_PATH:-src/lib.rs}"',
            'printf "// changed during the test run\\n" >> "$PACKAGE_ROOT/${EDIT_DURING_TEST_PATH:-src/lib.rs}"\n'
            + '    git add src/lib.rs\n    git commit --quiet -m changed',
        )
        _ = cargo.write_text(text)
        before = self.git("rev-parse", "HEAD^{tree}")
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.git("rev-parse", "HEAD^{tree}"), before)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("the tree changed while final ran", result.stdout)

    def test_write_prunes_old_records_and_query_does_not_write(self) -> None:
        folder = Path(self.environment["VERIFY_STATE_DIR"]) / "final_pass"
        folder.mkdir(parents=True)
        old = folder / ("a" * 40 + ".json")
        _ = old.write_text("{}")
        expired = time.time() - 15 * 24 * 60 * 60
        os.utime(old, (expired, expired))
        result = self.verify("final-pass")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no final pass record", result.stderr)
        self.assertTrue(old.exists())
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertEqual(len(self.pass_files()), 1)


if __name__ == "__main__":
    _ = unittest.main()
