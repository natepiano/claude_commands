"""Claude repair worker identity integration coverage."""

from __future__ import annotations

import unittest
import json
from typing import cast
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
    def test_claude_repair_in_the_impl_slot_keeps_a_distinct_worker_identity(self) -> None:
        run = self.start_phase("claude-repair-identity")
        first = self.claude_launch(run, "sonnet:low")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = (run / "impl_worker_impl.json").read_bytes()
        self.open_repair_round(run)
        repair = self.claude_launch(run, "sonnet:low", kind="fix")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual((run / "impl_worker_impl.json").read_bytes(), original)
        identity = cast(dict[str, object], json.loads((run / "impl_worker_fix.json").read_text()))
        original_identity = cast(dict[str, object], json.loads(original))
        self.assertNotEqual(identity["name"], original_identity["name"])
        self.assertEqual(identity["family"], "claude")
        self.assertFalse((run / "mesh_roster.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
