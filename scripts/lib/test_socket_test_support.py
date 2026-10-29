"""A long temporary root cannot lengthen a fixture's Unix socket path."""
from __future__ import annotations

import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.socket_test_support import short_socket_directory


class ShortSocketDirectoryTests(unittest.TestCase):
    def test_socket_binds_with_long_tmpdir_and_private_folder_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            long_tmpdir = Path(root) / ("long-session-" * 8)
            long_tmpdir.mkdir()
            self.assertGreater(len(os.fsencode(long_tmpdir)), 108)
            with patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}), patch.object(tempfile, "tempdir", None):
                self.assertEqual(tempfile.gettempdir(), str(long_tmpdir))
                with short_socket_directory() as directory:
                    folder = Path(directory)
                    self.assertEqual(folder.parent, Path("/tmp"))
                    self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                    path = folder / "s"
                    self.assertLess(len(os.fsencode(path)), 108)
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(str(path))
                        self.assertTrue(path.is_socket())
                self.assertFalse(folder.exists())


if __name__ == "__main__":
    _ = unittest.main()
