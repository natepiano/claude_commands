"""Claude launcher effort integration coverage."""

from __future__ import annotations

import unittest
import json
from scripts.direct.assign_claude_native_test_support import ClaudeLauncherFixture
from typing import cast


class AssignClaudeLauncherWorkerTests(ClaudeLauncherFixture):
    def test_new_claude_worker_uses_registry_effort(self) -> None:
        for row, expected_effort in (("sonnet:low", "low"), ("sonnet", "")):
            with self.subTest(row=row):
                session_dir = self.start_phase(f"effort-{row.replace(':', '-')}")
                result = self.claude_launch(
                    session_dir, row
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = cast("list[str]", json.loads(
                    (self.root / "launch_args.json").read_text(encoding="utf-8")
                ))
                self.assertEqual(arguments[arguments.index("--model") + 1], "sonnet")
                if expected_effort:
                    self.assertEqual(
                        arguments[arguments.index("--effort") + 1], expected_effort
                    )
                else:
                    self.assertNotIn("--effort", arguments)
                self.assertEqual(
                    self.recorded_worker(session_dir).current_session,
                    json.loads(next((self.root / "sessions").glob("*.json")).read_text())["sessionId"],
                )


if __name__ == "__main__":
    _ = unittest.main()
