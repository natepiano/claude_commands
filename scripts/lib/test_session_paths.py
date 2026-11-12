"""Session readers observe home and override changes without a reload."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib.session_paths import sessions_directory
from scripts.message import sessions
from scripts.production import showrunners, staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


class SessionPathTests(unittest.TestCase):
    def test_readers_observe_changed_home_and_override_without_a_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            root = Path(directory)
            owner = fixture_member("producer", name="initial")
            for name in ("first", "second"):
                home = root / name
                registry = home / ".claude/sessions"
                registry.mkdir(parents=True)
                _ = (registry / f"{os.getpid()}.json").write_text(
                    json.dumps({"sessionId": owner.current_session, "name": "stale registry label"}))
                with patch.dict(os.environ, {"HOME": str(home), "NOTIFIER_SESSIONS_DIR": ""}):
                    self.assertEqual(sessions_directory(), registry)
                    self.assertEqual(sessions.sessions_dir(), registry)
                    _ = staff.rename(owner.id, name)
                    self.assertEqual(showrunners.session_name(owner.current_session), name)
            for name in ("override-one", "override-two"):
                registry = root / name
                registry.mkdir()
                _ = (registry / f"{os.getpid()}.json").write_text(
                    json.dumps({"sessionId": owner.current_session, "name": "stale registry label"}))
                with patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}):
                    self.assertEqual(sessions.sessions_dir(), registry)
                    _ = staff.rename(owner.id, name)
                    self.assertEqual(showrunners.session_name(owner.current_session), name)
