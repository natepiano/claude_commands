"""Exercise compaction markers with temporary state and configuration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).with_name("pre-compact-marker.py")


class PreCompactMarkerTests(unittest.TestCase):
    root: Path = Path()
    folder: Path = Path()
    config: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.folder = self.root / ".local/state/compaction"
        self.config = self.root / "config"
        self.config.mkdir()
        source = HOOK.resolve().parents[2] / "config/compaction_marker.conf"
        _ = (self.config / source.name).write_text(source.read_text())

    def run_hook(self, family: str, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOK), "--family", family], input=payload,
            env={**os.environ, "HOME": str(self.root), "CONF_DIR": str(self.config),
                 "XDG_STATE_HOME": str(self.root / ".local/state")},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def test_content_and_replacement_for_both_families(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.folder.mkdir(parents=True, exist_ok=True)
                marker = self.folder / "session-123.json"
                _ = marker.write_text('{"old": true}')
                before = datetime.now(UTC)
                result = self.run_hook(family, json.dumps({"hook_event_name": "PreCompact",
                    "session_id": "session-123", "trigger": "auto"}))
                after = datetime.now(UTC)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                start = cast(dict[str, str], json.loads(marker.read_text()))
                self.assertEqual(set(start), {"session_id", "family", "started_at"})
                self.assertEqual((start["session_id"], start["family"]), ("session-123", family))
                started = datetime.fromisoformat(start["started_at"])
                self.assertIsNotNone(started.utcoffset())
                self.assertLessEqual(before, started)
                self.assertLessEqual(started, after)
                self.assertEqual(list(self.folder.glob("*.json")), [marker])

    def test_each_run_removes_markers_older_than_twenty_four_hours(self) -> None:
        self.folder.mkdir(parents=True)
        old = self.folder / "old.json"
        recent = self.folder / "recent.json"
        for marker, age in ((old, timedelta(hours=25)), (recent, timedelta(hours=23))):
            _ = marker.write_text("{}")
            modified = (datetime.now(UTC) - age).timestamp()
            os.utime(marker, (modified, modified))
        result = self.run_hook("codex", '{"hook_event_name":"PreCompact","session_id":"thr_123"}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue((self.folder / "thr_123.json").is_file())

    def test_a_write_error_exits_zero_without_a_new_marker(self) -> None:
        _ = (self.root / ".local").write_text("a file prevents the state directory")
        result = self.run_hook("claude", '{"hook_event_name":"PreCompact","session_id":"session-123"}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(self.folder.exists())

    def test_clear_deletes_only_the_input_sessions_marker(self) -> None:
        self.folder.mkdir(parents=True)
        own = self.folder / "session-123.json"
        other = self.folder / "another-session.json"
        _ = own.write_text('{"session_id":"session-123"}')
        other_content = '{"session_id":"another-session"}'
        _ = other.write_text(other_content)
        result = subprocess.run([sys.executable, str(HOOK.with_name("compaction-marker-clear.py"))],
            input='{"session_id":"session-123"}', env={**os.environ, "HOME": str(self.root),
                "XDG_STATE_HOME": str(self.root / ".local/state")},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual({path.name: path.read_text() for path in self.folder.glob("*.json")},
                         {other.name: other_content})


if __name__ == "__main__":
    _ = unittest.main()
