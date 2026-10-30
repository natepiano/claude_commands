"""An interrupted publication keeps the last complete text available."""
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib.atomic_file import write_text_atomic


class AtomicFileTests(unittest.TestCase):
    def test_next_write_removes_dead_process_temporary_files_and_keeps_live_ones(self) -> None:
        for permission_denied in (False, True):
            with self.subTest(permission_denied=permission_denied), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "change-review - demo.md"
                live_pid = os.getpid()
                dead_pid = live_pid + 1000000
                live = path.with_name(f".{path.name}.{live_pid}.live1234.tmp")
                dead = path.with_name(f".{path.name}.{dead_pid}.dead1234.tmp")
                unrelated = path.with_name(f".change-review - demoXmd.{dead_pid}.other123.tmp")
                for file in (live, dead, unrelated):
                    _ = file.write_text("unfinished")
                real_kill = os.kill

                def process_exists(pid: int, signal: int) -> None:
                    if pid == dead_pid:
                        raise ProcessLookupError(pid)
                    if permission_denied:
                        raise PermissionError(pid)
                    real_kill(pid, signal)

                with patch("scripts.lib.atomic_file.os.kill", side_effect=process_exists):
                    write_text_atomic(path, "Reviewed\n")
                self.assertEqual(path.read_text(), "Reviewed\n")
                self.assertFalse(dead.exists())
                self.assertEqual(live.read_text(), "unfinished")
                self.assertEqual(unrelated.read_text(), "unfinished")

    def test_write_creates_a_missing_parent_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "nested" / "state.json"
            write_text_atomic(path, '["question"]\n')
            self.assertEqual(path.read_text(), '["question"]\n')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_write_can_require_an_existing_parent_before_temporary_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "retired" / "wait"
            with patch("scripts.lib.atomic_file._remove_dead_temporary_files") as sweep:
                with self.assertRaises(FileNotFoundError):
                    write_text_atomic(path, "wait-id\n", create_parent=False)
                sweep.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])
            path.parent.mkdir()
            write_text_atomic(path, "wait-id\n", create_parent=False)
            self.assertEqual(path.read_text(), "wait-id\n")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_replacement_syncs_parent_folder_after_publishing_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            _ = path.write_text("old state", encoding="utf-8")
            sync = os.fsync
            synced_versions: list[tuple[bool, str]] = []

            def record_sync(descriptor: int) -> None:
                synced_versions.append((stat.S_ISDIR(os.fstat(descriptor).st_mode),
                                        path.read_text(encoding="utf-8")))
                sync(descriptor)

            with patch("scripts.lib.atomic_file.os.fsync", side_effect=record_sync):
                write_text_atomic(path, "new complete state")
            self.assertEqual(synced_versions, [(False, "old state"), (True, "new complete state")])

    def test_durable_text_write_preserves_previous_file_when_content_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            write_text_atomic(path, "old text\n")
            before = path.read_bytes()
            with patch("scripts.lib.atomic_file.os.fsync", side_effect=OSError("injected content sync failure")):
                with self.assertRaisesRegex(OSError, "injected content sync failure"):
                    write_text_atomic(path, "replacement \N{SNOWMAN}\n")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(".note.txt.*")), [])
            write_text_atomic(path, "replacement \N{SNOWMAN}\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement \N{SNOWMAN}\n")
            self.assertEqual(list(path.parent.glob(".note.txt.*")), [])

    def test_new_file_after_interrupted_publish_uses_the_default_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report"
            previous = os.umask(0o027)
            try:
                with patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("stopped")):
                    with self.assertRaises(OSError):
                        write_text_atomic(path, "complete report")
                self.assertFalse(path.exists())
                write_text_atomic(path, "complete report")
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                self.assertEqual(path.read_text(), "complete report")
            finally:
                _ = os.umask(previous)

    def test_replacing_a_file_keeps_its_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared-report"
            _ = path.write_text("old report")
            path.chmod(0o664)
            previous = os.umask(0o077)
            try:
                write_text_atomic(path, "new report")
            finally:
                _ = os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o664)
            self.assertEqual(path.read_text(), "new report")

    def test_failed_publish_keeps_old_file_and_rerun_publishes_full_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            _ = path.write_text("old state", encoding="utf-8")
            with patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("stopped")):
                with self.assertRaises(OSError):
                    write_text_atomic(path, "new complete state")
            self.assertEqual(path.read_text(), "old state")
            write_text_atomic(path, "new complete state")
            self.assertEqual(path.read_text(), "new complete state")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_first_publish_leaves_no_partial_file_for_a_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan"
            with patch("scripts.lib.atomic_file.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_text_atomic(path, "## Source\n\ncomplete brief\n")
            self.assertFalse(path.exists())
            write_text_atomic(path, "## Source\n\ncomplete brief\n")
            self.assertEqual(path.read_text(), "## Source\n\ncomplete brief\n")
