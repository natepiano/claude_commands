"""An interrupted publication keeps the last complete text available."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib.atomic_file import write_text_atomic


class AtomicFileTests(unittest.TestCase):
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
