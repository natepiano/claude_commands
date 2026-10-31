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

    def test_append_discards_an_unfinished_tail_before_the_next_row(self) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True)
        for tail in (b'{"id":"cut"', b'{"id":"complete"}'):
            with self.subTest(tail=tail):
                _ = path.write_bytes(tail)
                store.append_line(path, {"id": "next"})
                self.assertEqual(path.read_bytes(), b'{"id":"next"}\n')

    def test_append_discards_a_failed_short_write_and_keeps_every_line_parseable(self) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        old_rows = [{"id": "old-one"}, {"id": "old-two"}]
        for row in old_rows:
            store.append_line(path, row)
        prefix = path.read_bytes()
        write = os.write
        wrote_fragment = False

        def fail_after_fragment(fd: int, data: bytes | memoryview) -> int:
            nonlocal wrote_fragment
            if wrote_fragment:
                raise OSError("fixture disk full after a short write")
            wrote_fragment = True
            return write(fd, bytes(data)[:7])

        with mock.patch.object(os, "write", side_effect=fail_after_fragment):
            with self.assertRaisesRegex(OSError, "fixture disk full"):
                store.append_line(path, {"id": "interrupted"})
        self.assertEqual(path.read_bytes(), prefix + b'{"id":"')

        store.append_line(path, {"id": "next"})
        contents = path.read_bytes()
        self.assertEqual(contents, prefix + b'{"id":"next"}\n')
        self.assertEqual([cast(dict[str, object], json.loads(line)) for line in contents.splitlines()],
                         [*old_rows, {"id": "next"}])

    def test_whole_rows_skip_damaged_nonobject_and_partial_rows_from_the_offset(self) -> None:
        path = self.root / "history.jsonl"
        path.parent.mkdir(parents=True)
        first = b'{"id":"first"}\n'
        fragment = b'broken {"id":"fragment"}\n'
        _ = path.write_bytes(first + fragment + b'{"id":"\xff"}\n'
                             + b'[{"id":"list"}]\n' + b'{"id":"last"}\n' + b'{"id":"cut"}')
        cases = ((0, [{"id": "first"}, {"id": "last"}]),
                 (len(first) + len(b"broken "), [{"id": "last"}]),
                 (len(first) + len(fragment), [{"id": "last"}]))
        for start, expected in cases:
            with self.subTest(start=start), path.open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                self.assertEqual(list(store.whole_rows(handle, start)), expected)

    def test_append_keeps_whole_rows_before_a_long_unfinished_tail(self) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        store.append_line(path, {"id": "old"})
        prefix = path.read_bytes()
        with path.open("ab") as handle:
            tail = b'{"id":"cut","padding":"' + b"x" * (1024 * 1024 + 7)
            self.assertEqual(handle.write(tail), len(tail))

        store.append_line(path, {"id": "next"})

        self.assertEqual(path.read_bytes(), prefix + b'{"id":"next"}\n')

    def test_short_reads_during_tail_repair_preserve_whole_rows(self) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True)
        read = os.read
        for empty_tail_read in (False, True):
            with self.subTest(empty_tail_read=empty_tail_read):
                _ = path.write_bytes(b'{"id":"kept"}\n{"id":"cut')
                read_sizes: list[int] = []

                def short_read(fd: int, size: int) -> bytes:
                    read_sizes.append(size)
                    return read(fd, 0 if empty_tail_read and size == 1 else min(size, 4))

                with mock.patch.object(os, "read", side_effect=short_read):
                    store.append_line(path, {"id": "next"})

                self.assertTrue(read_sizes, "tail repair must inspect the existing file")
                self.assertEqual(path.read_bytes(), b'{"id":"kept"}\n{"id":"next"}\n')

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
