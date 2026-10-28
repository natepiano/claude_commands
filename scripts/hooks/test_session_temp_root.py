#!/usr/bin/env python3
"""Tests for session-start-temp-root.py, run as Claude Code runs it: JSON on stdin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "session-start-temp-root.py"


class SessionTempRootTests(unittest.TestCase):
    def run_hook(self, base: Path, payload: object, env_file: Path | None) -> None:
        environment = {**os.environ, "SESSION_TEMP_ROOT": str(base / "session")}
        _ = environment.pop("CLAUDE_ENV_FILE", None)
        if env_file is not None:
            environment["CLAUDE_ENV_FILE"] = str(env_file)
        _ = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True, env=environment,
                           check=True)

    def test_the_root_is_made_and_tmpdir_is_exported_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            env_file = base / "env.sh"
            for _ in range(2):
                self.run_hook(base, {"session_id": "abc-123"}, env_file)
            root = base / "session" / "abc-123"
            self.assertTrue(root.is_dir())
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env_file.read_text(), f"export TMPDIR={root}\n")
            shown = subprocess.run(["sh", "-c", f". {env_file}; printf %s \"$TMPDIR\""], capture_output=True,
                                   text=True, check=True).stdout
            self.assertEqual(shown, str(root))

    def test_a_bad_session_id_or_no_env_file_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            self.run_hook(base, {"session_id": "../x"}, base / "env.sh")
            self.run_hook(base, {"session_id": "abc"}, None)
            self.assertFalse((base / "session").exists())
            self.assertFalse((base / "env.sh").exists())


if __name__ == "__main__":
    _ = unittest.main()
