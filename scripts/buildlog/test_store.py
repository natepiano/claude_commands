#!/usr/bin/env python3
"""Tests for the permanent sample file's placement beside step records."""

from __future__ import annotations

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


if __name__ == "__main__":
    _ = unittest.main()
