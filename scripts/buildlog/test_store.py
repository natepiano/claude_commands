#!/usr/bin/env python3
"""Tests for the permanent sample file's placement beside step records."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override
from unittest import mock

import store
from test_index import use_test_log

use_test_log()


class StoreTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"

    def test_sample_record_lands_in_separate_host_month_file(self) -> None:
        epoch = datetime(2026, 10, 2, 12, tzinfo=UTC).timestamp()
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root)}):
            path = store.sample_file("natedev", epoch)
            self.assertEqual(path, self.root / "natedev" / "samples-2026-10.jsonl")
            self.assertNotEqual(path, store.host_file("natedev", epoch))
            record: dict[str, object] = {"kind": "sample", "host": "natedev", "at": store.utc_iso(epoch)}
            store.append_line(path, record)
            self.assertEqual(cast(dict[str, object], json.loads(path.read_text())), record)
            self.assertFalse(store.host_file("natedev", epoch).exists())

    def test_append_separates_an_interrupted_tail_before_the_next_row(self) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True)
        for tail in (b'{"id":"cut"', b'{"id":"complete"}'):
            with self.subTest(tail=tail):
                _ = path.write_bytes(tail)
                store.append_line(path, {"id": "next"})
                self.assertEqual(path.read_bytes(), tail + b'\n{"id":"next"}\n')

    def test_append_reads_only_the_tail_byte_and_keeps_the_lock_through_fsync(self) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_bytes(b'{"id":"old"}\n')
        read, write, fsync = os.read, os.write, os.fsync
        read_sizes: list[int] = []
        writes: list[bytes] = []
        synced: list[int] = []

        def assert_locked() -> None:
            with path.open("rb") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def read_tail(fd: int, size: int) -> bytes:
            assert_locked()
            read_sizes.append(size)
            return read(fd, size)

        def short_write(fd: int, data: bytes | memoryview) -> int:
            assert_locked()
            fragment = bytes(data)[:3]
            writes.append(fragment)
            return write(fd, fragment)

        def sync_locked(fd: int) -> None:
            assert_locked()
            synced.append(fd)
            fsync(fd)

        with mock.patch.object(os, "read", side_effect=read_tail), \
             mock.patch.object(os, "write", side_effect=short_write), \
             mock.patch.object(os, "fsync", side_effect=sync_locked):
            store.append_line(path, {"id": "next"})

        self.assertEqual(read_sizes, [1])
        self.assertGreater(len(writes), 1)
        self.assertEqual(len(synced), 1)
        self.assertEqual(path.read_bytes(), b'{"id":"old"}\n{"id":"next"}\n')


if __name__ == "__main__":
    _ = unittest.main()
