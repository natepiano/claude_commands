"""Claude launcher effort integration coverage."""

from __future__ import annotations

import unittest
import json
from scripts.direct.implement_launcher_claude_test_support import ClaudeLauncherFixture
from typing import cast


class ImplementLauncherWorkerTests(ClaudeLauncherFixture):
    def test_new_claude_worker_uses_registry_effort(self) -> None:
        for row, expected_effort in (("sonnet:low", "low"), ("sonnet", "")):
            with self.subTest(row=row):
                session_dir = self.start_phase(f"effort-{row.replace(':', '-')}")
                result = self.claude_launch(
                    session_dir, row, inherited_effort="xhigh"
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
                    (session_dir / "impl_bg_id_impl").read_text(encoding="utf-8"),
                    "abc12345\n",
                )
                self.assertIn(
                    "abc12345\t", (session_dir / "workers").read_text(encoding="utf-8")
                )


if __name__ == "__main__":
    _ = unittest.main()
