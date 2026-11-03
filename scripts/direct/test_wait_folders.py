"""Verification recovery removes folders only after it proves stale ownership."""
from __future__ import annotations

import os
import fcntl
from pathlib import Path
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from unittest.mock import patch

from scripts.direct import progress_history, wait_folders
from scripts.lib.process_start_test_support import write_stat


class WaitFolderTests(unittest.TestCase):
    def test_probe_recovery_preserves_the_folder_and_its_wait_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            folder = session / "verify-hold.43.probe"
            folder.mkdir()
            marker = folder / "phase-wait.saved.hold"
            _ = marker.write_text("saved-wait\n")
            arguments = ["progress_history.py", "recover-wait-folder", "--session-dir", str(session),
                         "--directory", str(folder)]
            with patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "1"}), patch.object(sys, "argv", arguments):
                progress_history.main()
            self.assertEqual(marker.read_bytes(), b"saved-wait\n")
            self.assertFalse((session / ".progress_history.lock").exists())

    def test_a_publisher_cannot_add_a_wait_after_recovery_discovers_an_empty_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            folder = session / "verify-hold.43.race"
            folder.mkdir()
            published: list[str] = []
            closed: list[str] = []
            iterdir: Callable[[Path], Iterator[Path]] = Path.iterdir

            def publish_after_snapshot(path: Path) -> Iterator[Path]:
                snapshot = list(iterdir(path))
                if path == folder:
                    with (session / ".progress_history.lock").open("a+") as publisher:
                        try:
                            fcntl.flock(publisher.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            _ = (folder / "phase-wait.late.hold").write_text("late-wait\n")
                            published.append("late-wait")
                return iter(snapshot)

            def close_wait(session_dir: Path, wait_id: str) -> None:
                self.assertEqual(session_dir, session)
                closed.append(wait_id)

            with patch.object(Path, "iterdir", publish_after_snapshot), \
                 patch.object(progress_history, "close_wait_id", side_effect=close_wait):
                wait_folders.recover(session, folder)
            self.assertEqual(published, closed, "recovery deleted a published wait without closing it")
            self.assertFalse(folder.exists())

    def test_the_recorder_recovery_command_returns_to_its_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            folder = session / "verify-hold.43.empty"
            folder.mkdir()
            arguments = ["progress_history.py", "recover-wait-folder", "--session-dir", str(session),
                         "--directory", str(folder)]
            with patch.object(sys, "argv", arguments), patch.object(
                os, "execvpe", side_effect=AssertionError("recovery replaced its caller")
            ):
                progress_history.main()
            self.assertFalse(folder.exists())

    def test_an_ownerless_staging_folder_is_removed_only_when_its_pid_is_proven_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, worktree, proc = root / "session", root / "tree", root / "proc"
            proc.mkdir()
            folders = wait_folders.recovery_root(session, worktree)
            folders.mkdir(parents=True)
            for pid, state in ((41, "live"), (42, "unknown"), (43, "gone")):
                (folders / f".verify-hold-new.{pid}.{state}").mkdir()
            _ = write_stat(proc, 41)
            torn = write_stat(proc, 42)
            _ = torn.write_text("torn stat")
            with patch.dict(os.environ, {"VERIFY_PROC_ROOT": str(proc)}):
                wait_folders.sweep_wait_folders(session, worktree, None)
            self.assertTrue((folders / ".verify-hold-new.41.live").is_dir())
            self.assertTrue((folders / ".verify-hold-new.42.unknown").is_dir())
            self.assertFalse((folders / ".verify-hold-new.43.gone").exists())

    def test_recovery_without_markers_does_not_read_the_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "verify-hold.43.empty"
            folder.mkdir()
            with patch("scripts.direct.progress_history._read_state", side_effect=AssertionError("history was read")):
                wait_folders.recover(root, folder)
            self.assertFalse(folder.exists())
