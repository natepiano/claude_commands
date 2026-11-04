"""Exercise the watcher's scope with actual Git trees and Cargo workspace inputs."""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from typing import final

from scripts.direct import tree_watch
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable

from scripts.direct.tree_watch_test_support import (
    EventWatcher,
    TreeWatchFixture,
    WATCH_DEADLINE,
    write_workspace as write_workspace,
)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class TreeWatchTests(TreeWatchFixture):
    def test_nested_workspace_build_identity_uses_its_own_output_roots(self) -> None:
        workspace = self.nested_workspace()
        output = self.root / "build-inputs"
        outer = CargoOutputRoots(self.root, self.root / "target", self.root / "build")
        nested = CargoOutputRoots(workspace, workspace / "target", workspace / "build")

        def resolve(checkout: Path) -> CargoOutputRoots:
            return nested if checkout == workspace else outer

        with mock.patch.dict(os.environ, {"VERIFY_COMPUTE_TEST_BUILD": "1",
                                         "VERIFY_TEST_BUILD_INPUTS_FILE": str(output)}), \
                mock.patch.object(tree_watch, "read_cargo_output_roots", side_effect=resolve):
            _ = tree_watch.TreeInputs(self.key_arguments(), json.dumps(self.metadata)).keys()
        self.assertEqual(output.read_text().splitlines()[1:],
                         [str(nested.target_directory), str(nested.build_directory)])

    def test_build_identity_saves_both_resolved_output_roots(self) -> None:
        output = self.root / "build-inputs"
        target = self.root / "target"
        build = self.root / "intermediates"
        with mock.patch.dict(os.environ, {"VERIFY_COMPUTE_TEST_BUILD": "1",
                                         "VERIFY_TEST_BUILD_INPUTS_FILE": str(output)}):
            def calculate(roots: CargoOutputRoots | CargoOutputUnavailable) -> list[str]:
                with mock.patch.object(tree_watch, "read_cargo_output_roots", return_value=roots):
                    _ = tree_watch.TreeInputs(self.key_arguments(), json.dumps(self.metadata)).keys()
                return output.read_text().splitlines()

            split = calculate(CargoOutputRoots(self.root, target, build))
            self.assertEqual(split[1:], [str(target), str(build)])
            same = calculate(CargoOutputRoots(self.root, target, target))
            self.assertEqual(same[1:], [str(target), str(target)])
            self.assertNotEqual(split[0], same[0])
            output.unlink()
            with mock.patch.object(tree_watch, "read_cargo_output_roots",
                                   return_value=CargoOutputUnavailable("metadata failed")):
                _ = tree_watch.TreeInputs(self.key_arguments(), json.dumps(self.metadata)).keys()
            self.assertFalse(output.exists())

    def test_lint_versions_reuse_the_call_reading_and_keep_failed_reads_disabled(self) -> None:
        saved = self.root / "lint-versions"
        formatter = self.root / "rustfmt-version"
        with mock.patch.dict(os.environ, {"VERIFY_LINT_VERSIONS_FILE": str(saved),
                                         "VERIFY_LINT_RUSTFMT_VERSION_FILE": str(formatter)}):
            with mock.patch.object(subprocess, "check_output", return_value=b"tool version\n") as probe:
                first = tree_watch.lint_tool_versions()
                self.assertEqual(tree_watch.lint_tool_versions(), first)
                self.assertEqual(probe.call_count, 4)
                self.assertEqual(formatter.read_bytes(), b"tool version")
            saved.unlink()
            with mock.patch.object(subprocess, "check_output", side_effect=OSError("tool missing")) as probe:
                with self.assertRaises(OSError):
                    _ = tree_watch.lint_tool_versions()
                with self.assertRaises(ValueError):
                    _ = tree_watch.lint_tool_versions()
                probe.assert_called_once()

    def test_lint_keys_without_result_caching_do_not_probe_versions(self) -> None:
        import json

        with mock.patch.object(tree_watch, "lint_tool_versions", side_effect=AssertionError("unexpected probe")):
            key, watched = tree_watch.TreeInputs(["--", "lint"], json.dumps(self.metadata)).keys(lint_versions=False)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertRegex(watched, r"^[0-9a-f]{64}$")

    def test_fired_path_names_the_changed_watched_file(self) -> None:
        process, fired, before = self.start_watcher(os.getpid())
        relative = "sample/src/lib.rs"
        _ = (self.root / relative).write_text("// changed input\n")
        self.assert_fires(process, fired, before)
        self.assertEqual(fired.with_name(fired.name + ".path").read_text(), relative + "\n")

    def test_in_scope_edit_table_fires(self) -> None:
        for relative in ("sample/src/lib.rs", "dep/src/lib.rs", "sample/fixtures/input.bin",
                         "Cargo.toml", "Cargo.lock", ".cargo/config.toml",
                         "rust-toolchain", "rust-toolchain.toml", "unrelated/Cargo.toml"):
            with self.subTest(path=relative):
                process, fired, before = self.start_watcher(os.getpid())
                path = self.root / relative
                _ = path.write_text(path.read_text() + "\n# changed\n")
                self.assert_fires(process, fired, before)

    def test_out_of_scope_edit_table_does_not_fire(self) -> None:
        for relative in ("docs/design.md", "plans/work.md", "scripts/helper.py",
                         "unrelated/src/lib.rs", "session/board.log"):
            with self.subTest(path=relative):
                process, fired, before = self.start_watcher(os.getpid())
                path = self.root / relative
                _ = path.write_text(path.read_text() + "changed\n")
                self.assert_observes_without_firing(process)
                self.assertFalse(fired.exists(), "out-of-scope edit cancelled the run")
                self.assertEqual(self.key(), before)
                # Prove the watcher was alive and observing, rather than silently broken.
                source = self.root / "sample/src/lib.rs"
                _ = source.write_text(source.read_text() + "// in scope\n")
                self.assert_fires(process, fired, before)

    def test_added_deleted_and_staged_member_inputs_fire(self) -> None:
        for change in ("untracked", "deleted", "staged", "executable"):
            with self.subTest(change=change):
                process, fired, before = self.start_watcher(os.getpid())
                source = self.root / "sample/src/lib.rs"
                if change == "untracked":
                    _ = (source.parent / "new.rs").write_text("// new input\n")
                elif change == "deleted":
                    (source.parent / "removed.rs").unlink()
                elif change == "executable":
                    source.chmod(0o755)
                else:
                    _ = source.write_text(source.read_text() + "// staged input\n")
                    _ = subprocess.run(["git", "add", "sample/src/lib.rs"], cwd=self.root,
                                       check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
                self.assert_fires(process, fired, before)

    def test_workspace_watch_includes_every_member_but_excludes_external_docs(self) -> None:
        before = self.key("--workspace")
        _ = (self.root / "docs/design.md").write_text("new external documentation\n")
        self.assertEqual(self.key("--workspace"), before)
        _ = (self.root / "unrelated/src/lib.rs").write_text("// workspace member input\n")
        self.assertNotEqual(self.key("--workspace"), before)

    def test_parent_exit_stops_watcher_without_firing(self) -> None:
        parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                  stdin=subprocess.PIPE)
        try:
            watcher, fired, _ = self.start_watcher(parent.pid, real_process=True)
            parent.terminate()
            _ = parent.communicate(timeout=TEST_HANG_GUARD_SECS)
            output, errors = watcher.communicate(timeout=WATCH_DEADLINE)
            self.assertEqual(watcher.returncode, 3, output + errors)
            self.assertFalse(fired.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
            _ = parent.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_unavailable_git_snapshot_keeps_running_until_a_readable_change(self) -> None:
        git = shutil.which("git")
        assert git is not None
        fault = self.root / "git-read-fault"
        observed = self.root / "git-read-failed"
        wrapper = self.root / "tools/git"
        _ = wrapper.write_text(f"#!{sys.executable}\n" +
            "import os, sys\nfrom pathlib import Path\n" +
            f"fault = Path({str(fault)!r})\nobserved = Path({str(observed)!r})\n" +
            "if 'ls-tree' in sys.argv and fault.exists():\n" +
            "    observed.touch()\n    raise SystemExit(128)\n" +
            f"os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        # Workspace mode used to turn a failed ls-tree into an empty key.
        process, fired, before = self.start_watcher(os.getpid(), "--workspace")
        fault.touch()
        self.assert_observes_without_firing(process)
        self.assertTrue(observed.exists(), "fault injection never reached Git")
        self.assert_observes_without_firing(process)
        self.assertFalse(fired.exists(), "an uncertain snapshot cancelled an unchanged tree")
        source = self.root / "sample/src/lib.rs"
        _ = source.write_text(source.read_text() + "// edit during unreadable snapshot\n")
        self.assert_observes_without_firing(process)
        self.assertFalse(fired.exists(), "an unreadable edit was treated as proven stale")
        fault.unlink()
        assert isinstance(process, EventWatcher)
        process.advance()
        output, errors = process.communicate(timeout=WATCH_DEADLINE)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotEqual(fired.read_text().strip(), before)
        self.assertEqual(fired.read_text().strip(), self.key("--workspace"))

    def test_nested_cargo_workspace_watches_its_own_root_inputs(self) -> None:
        cargo_root = self.nested_workspace(committed=True)
        for relative in ("Cargo.toml", "Cargo.lock", ".cargo/config.toml",
                         "rust-toolchain", "rust-toolchain.toml"):
            with self.subTest(path=relative):
                process, fired, before = self.start_watcher(os.getpid())
                path = cargo_root / relative
                _ = path.write_text(path.read_text() + "\n# changed nested root input\n")
                self.assert_fires(process, fired, before)

    def test_git_root_inputs_outside_nested_cargo_workspace_do_not_cancel(self) -> None:
        cargo_root = self.nested_workspace()
        process, fired, before = self.start_watcher(os.getpid())
        for relative in ("Cargo.toml", "Cargo.lock", ".cargo/config.toml",
                         "rust-toolchain", "rust-toolchain.toml"):
            path = self.root / relative
            _ = path.write_text(path.read_text() + "\n# outside Cargo workspace\n")
        self.assert_observes_without_firing(process)
        self.assertFalse(fired.exists(), "Git root inputs were confused with Cargo root inputs")
        _ = (cargo_root / "Cargo.lock").write_text("# changed actual Cargo root input\n")
        self.assert_fires(process, fired, before)


if __name__ == "__main__":
    _ = unittest.main()
