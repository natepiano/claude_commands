"""Real verify.sh Mac result paths with disposable tools."""
from __future__ import annotations

import unittest
import subprocess
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, VERIFY as VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacResultPathsTests(VerifyMacOffloadFixture):
    def test_runner_result_uses_fixture_temp_directory(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "declined", "TEST_RUNNER_STATUS": "75"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runner = self.runner_events()[0]
        result_index = runner["args"].index("--result") + 1
        self.assertTrue(
            Path(runner["args"][result_index]).is_relative_to(self.root / "tmp"),
            runner,
        )

    def test_missing_temp_root_is_created_before_verify_runs(self) -> None:
        missing = self.root / "new-session" / "tmp"
        result = self.verify("test", "sample", "--no-cache", extra={"TMPDIR": str(missing)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(missing.is_dir())
        self.assertEqual(len(self.local_runs()), 1)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(Path(arguments[arguments.index("--result") + 1]).parent, missing)

    def test_lint_entry_points_create_missing_temp_roots(self) -> None:
        reader = self.root / "lint-config.sh"
        _ = reader.write_text("lint_config_enabled() { return 0; }\n")
        lint = VERIFY.parent.parent / "lint"
        for entry in ("invoke.sh", "lint"):
            with self.subTest(entry=entry):
                missing = self.root / entry / "tmp"
                arguments = (["bash", "-c", 'set -euo pipefail; source "$1"; run_once printf "step ran\\n"',
                              "temp-root-test", str(lint / entry)] if entry == "invoke.sh" else
                             ["bash", str(lint / entry), "fmt", "--all", "--check"])
                result = subprocess.run(arguments, cwd=self.root,
                    env={**self.environment, "TMPDIR": str(missing)}, capture_output=True,
                    text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(missing.is_dir())


if __name__ == "__main__":
    _ = unittest.main()
