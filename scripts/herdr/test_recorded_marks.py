"""Retained outside callers read launch records without changing them."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from scripts.herdr import pane_launch
from scripts.agents import pane_record

@final
class RecordedMarksTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.registry = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = self.root / "sessions"
        self.registry.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.registry),
            "HERDR_PANE_RECORD_DIR": str(self.root / "panes")})))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))



    def test_strict_reader_accepts_identity_only_launches_without_rewriting_them(self) -> None:
        path = self.root / "launch-history.json"
        entry = {"session_id": "conversation", "written_at": "2026-10-01T00:00:00Z",
                 "environment": {"STAFF_ID": "s123456789ab"}}
        _ = path.write_text(json.dumps({"legacy": entry}))
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(pane_launch.read_strict(path), {"legacy": entry})
        # Account attribution additionally needs a folder, not restore flags or hints.
        self.assertEqual(pane_launch.read_valid_entries(path), {})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_readout_skips_damaged_launch_fields_but_strict_repairs_refuse_them(self) -> None:
        entry = pane_record.launch("valid", "conversation", "/fixture", {"STAFF_ID": "s123456789ab"}, ["claude"])
        path = self.root / "launch-history.json"
        corruptions: tuple[tuple[str, object], ...] = (
            ("session_id", []), ("written_at", None), ("environment", {"STAFF_ID": 7}),
            ("config_dir", []), ("ended", "false"), ("flags_read", 1), ("cwd", {}),
        )
        for field, value in corruptions:
            with self.subTest(field=field):
                damaged: dict[str, object] = dict(entry)
                damaged[field] = value
                _ = path.write_text(json.dumps({"valid": entry, "damaged": damaged}))
                before = path.read_bytes(), path.stat().st_mtime_ns
                self.assertEqual(pane_launch.read_valid_entries(path), {"valid": entry})
                with self.assertRaises(ValueError):
                    _ = pane_launch.read_strict(path)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
