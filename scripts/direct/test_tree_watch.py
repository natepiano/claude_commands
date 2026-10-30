"""Exercise the watcher's scope with actual Git trees and Cargo workspace inputs."""
from __future__ import annotations

import os
import io
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from scripts.direct import tree_watch
from scripts.lib import process_start
import shutil
import subprocess
import sys
import unittest
from typing import final

from scripts.direct.tree_watch_test_support import (
    EventWatcher, TreeWatchFixture, WATCH_DEADLINE, TEST_HANG_GUARD_SECS,
    write_workspace as write_workspace,
)


@final
class TreeWatchTests(TreeWatchFixture):
    def test_an_unreadable_parent_start_is_retried_before_watching(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            start, fired = root / "start", root / "fired"
            _ = start.write_text("before")
            with (patch.object(sys, "stdin", io.StringIO("{}")),
                  patch.object(tree_watch.TreeInputs, "__init__", return_value=None),
                  patch.object(tree_watch.TreeInputs, "keys", return_value=("", "changed")),
                  patch.object(process_start, "process_start", side_effect=[
                      process_start.ProcessReadError("denied"), process_start.ProcessStartTime(123)]),
                  patch.object(process_start, "check_owner", return_value=process_start.OwnerCheck.ALIVE),
                  patch.object(time, "sleep") as retry):
                self.assertEqual(tree_watch.main([str(start), str(fired), "42"]), 0)
            self.assertEqual(fired.read_text(), "changed\n")
            retry.assert_called_once()

    def test_unconfirmed_parent_is_kept_and_never_signalled(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            start, fired = root / "start", root / "fired"
            _ = start.write_text("before")
            with (patch.object(sys, "stdin", io.StringIO("{}")),
                  patch.object(tree_watch.TreeInputs, "__init__", return_value=None),
                  patch.object(tree_watch.TreeInputs, "keys", return_value=("", "changed")),
                  patch.object(process_start, "process_start", return_value=process_start.ProcessStartTime(123)),
                  patch.object(process_start, "check_owner", return_value=process_start.OwnerCheck.UNKNOWN),
                  patch.object(os, "kill") as signal):
                self.assertEqual(tree_watch.main([str(start), str(fired), "42", "--signal-parent"]), 0)
            self.assertEqual(fired.read_text(), "changed\n")
            signal.assert_not_called()

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
