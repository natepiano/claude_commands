"""Claude launcher accounts integration coverage."""

from __future__ import annotations

import unittest
import json
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture
from scripts.accounts import accounts
import os
from unittest.mock import patch


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
    def test_copied_claude_launcher_keeps_legacy_home_then_uses_showrunners_after_cutover(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            self.assertEqual(accounts.setup("claude2"), [])
            _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": "worker@example.invalid"},
            }))
            groups = accounts.groups_file()
            groups.parent.mkdir(parents=True, exist_ok=True)
            _ = groups.write_text(json.dumps({"showrunners": "claude2"}))
        for cutover in (False, True):
            with self.subTest(cutover=cutover):
                if cutover:
                    _ = (self.root / ".claude.json").write_text("{}")
                session_dir = self.start_phase(f"copied-account-{cutover}")

                result = self.claude_launch(session_dir, "sonnet:low")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((self.root / "launch_environment.json").read_text()), {
                    "CLAUDE_CONFIG_DIR": str(self.root / ".claude2") if cutover else "",
                    "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.root / ".claude") if cutover else "",
                })


if __name__ == "__main__":
    _ = unittest.main()
