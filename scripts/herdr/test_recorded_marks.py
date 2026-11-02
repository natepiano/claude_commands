"""Saved identity belongs only to the live conversation occupying its recorded pane."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import herdr
import sessions
from scripts.shutdown import pane_record
import pane_launch


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
        self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.registry),
            "HERDR_PANE_RECORD_DIR": str(self.root / "panes")}))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))

    def test_saved_marks_require_the_live_process_conversation_and_exact_pane(self) -> None:
        pid = os.getpid()
        place = herdr.HerdrPlace("agents", "w1:p1")
        identity = {"STAFF_ID": "s123456789ab", "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}
        entry = pane_record.launch(place.pane, "conversation", "/fixture", identity, ["claude"])
        pane_record.write(place.session, entry)
        registry = {"pid": pid, "sessionId": "conversation", "name": "alpha", "updatedAt": 1,
                    "messagingSocketPath": "", "procStart": sessions.process_start(pid)}
        path = self.registry / f"{pid}.json"
        row = herdr.HerdrProcess(pid, place, {}, "claude", "/fixture")
        with patch.object(herdr, "_scan_herdr_processes", return_value=[row]):
            for case in ("match", "wrong-session", "wrong-pane", "dead"):
                with self.subTest(case=case):
                    recorded = dict(registry)
                    if case == "wrong-session":
                        recorded["sessionId"] = "different-conversation"
                    if case == "dead":
                        recorded["procStart"] = "different-process-start"
                    _ = path.write_text(json.dumps(recorded))
                    target = entry.copy()
                    if case == "wrong-pane":
                        target["pane_id"] = "another-pane"
                    # Deliberately corrupt the embedded pane id while keeping the store key.
                    _ = pane_record.record_path("agents").write_text(json.dumps({place.pane: target}))
                    result = herdr.herdr_processes()
                    self.assertEqual(result[0].marks, identity if case == "match" else {})

    def test_discovery_reads_one_pane_file_and_only_missing_sessions(self) -> None:
        pid = os.getpid()
        labels = {"STAFF_ID": "s123456789ab", "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}
        places = [herdr.HerdrPlace("agents", pane) for pane in ("w1:p1", "w2:p1", "w3:p1")]
        for place in places:
            pane_record.write(place.session, pane_record.launch(place.pane, "conversation", "/fixture", labels, ["claude"]))
        _ = (self.registry / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": "conversation", "name": "alpha", "updatedAt": 1,
            "messagingSocketPath": "", "procStart": sessions.process_start(pid)}))
        rows = [herdr.HerdrProcess(pid, place, labels if index == 0 else {}, "claude", "/fixture")
                for index, place in enumerate(places)]
        with (patch.object(herdr, "_scan_herdr_processes", return_value=rows),
              patch.object(pane_launch, "read", wraps=pane_launch.read) as files,
              patch.object(sessions, "read_session", wraps=sessions.read_session) as registry):
            self.assertEqual([row.marks for row in herdr.herdr_processes()], [labels] * len(rows))
            files.assert_called_once_with(self.root / "panes/agents.json")
            self.assertEqual(registry.call_args_list, [call(self.registry / f"{pid}.json")] * 2)

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


if __name__ == "__main__":
    _ = unittest.main()
