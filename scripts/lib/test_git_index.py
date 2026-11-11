"""Private index copies keep the timestamp of the opened file."""

import builtins
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, cast
import unittest
from unittest import mock

from scripts.lib import git_index


class GitIndexTests(unittest.TestCase):
    def test_copy_keeps_opened_index_bytes_and_timestamp_when_source_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "index"
            replacement = Path(directory) / "index.lock"
            destination = Path(directory) / "private-index"
            _ = source.write_bytes(b"first index")
            _ = replacement.write_bytes(b"later index")
            first_time = 1_600_000_000_000_000_000
            os.utime(source, ns=(first_time, first_time))
            later_time = first_time + 10_000_000_000
            os.utime(replacement, ns=(later_time, later_time))
            real_open = builtins.open

            def replace_before_read(path: str | Path, mode: str) -> BinaryIO:
                opened = cast(BinaryIO, real_open(path, mode))
                if Path(path) == source and mode == "rb":
                    _ = replacement.replace(source)
                return opened

            with mock.patch.object(builtins, "open", side_effect=replace_before_read):
                git_index.private_copy(source, destination)

            self.assertEqual(destination.read_bytes(), b"first index")
            self.assertEqual(destination.stat().st_mtime_ns, first_time)
            self.assertEqual(source.read_bytes(), b"later index")
