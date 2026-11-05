"""Prove parent file removal in temporary directories."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.production import remove_showrunner_parents


class RemoveShowrunnerParentsTests(unittest.TestCase):
    def test_removal_preserves_notices_and_wrap_capture_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for child in ("top", "leaf"):
                directory = root / child
                directory.mkdir()
                _ = (directory / "parent").write_text("former\n")
                _ = (directory / "parent_notice.json").write_text("pending\n")
                _ = (directory / "wrap_release.json").write_text("capture\n")
            removed = remove_showrunner_parents.remove_parent_files(root)
            self.assertEqual(removed, (root / "leaf/parent", root / "top/parent"))
            self.assertEqual(remove_showrunner_parents.remove_parent_files(root), ())
            for child in ("top", "leaf"):
                self.assertFalse((root / child / "parent").exists())
                self.assertEqual((root / child / "parent_notice.json").read_text(), "pending\n")
                self.assertEqual((root / child / "wrap_release.json").read_text(), "capture\n")

    def test_removal_resumes_after_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for child in ("one", "two"):
                (root / child).mkdir()
                _ = (root / child / "parent").write_text("former\n")
            unlink = Path.unlink

            def interrupt(path: Path) -> None:
                unlink(path)
                raise OSError("stopped after removal")

            with mock.patch.object(Path, "unlink", interrupt), self.assertRaisesRegex(OSError, "stopped after removal"):
                _ = remove_showrunner_parents.remove_parent_files(root)
            self.assertEqual(remove_showrunner_parents.remove_parent_files(root), (root / "two/parent",))
            self.assertEqual(remove_showrunner_parents.remove_parent_files(root), ())
