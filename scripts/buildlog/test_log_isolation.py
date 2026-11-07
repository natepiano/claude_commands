"""Launch-recording tests must isolate their log even without a caller's override."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.buildlog.test_index import use_test_log

use_test_log()


REPOSITORY = Path(__file__).resolve().parents[2]


class TestLogIsolationTests(unittest.TestCase):
    def test_launch_recording_tests_never_write_to_an_unconfigured_home(self) -> None:
        candidates = (
            ("discover", "-s", "scripts/shutdown", "-p", "test_pane_record.py"),
            ("discover", "-s", "scripts/shutdown", "-p", "test_pane_restore.py"),
            ("discover", "-s", "scripts/production", "-p", "test_unit_operation_panes.py"),
            ("discover", "-s", "scripts/lib", "-p", "test_nix_env_launches.py"),
            ("scripts.hana_shot.test_hana_shot.LaunchBuildWarningTests",
             "scripts.hana_shot.test_hana_shot.LaunchRecordRecoveryTests"),
        )
        for arguments in candidates:
            module = " ".join(arguments)
            with self.subTest(module=module), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                tools = home / "bin"
                tools.mkdir()
                herdr = tools / "herdr"
                _ = herdr.write_text(f"#!{sys.executable}\nraise SystemExit('unexpected fixture herdr command')\n")
                herdr.chmod(0o755)
                environment = {key: value for key, value in os.environ.items()
                               if key not in ("BUILDLOG_DIR", "STAFF_OPERATION_FD", "PYTHONUNBUFFERED",
                                              "REFUSAL_WATCH_INVENTORY")}
                environment.update(HOME=str(home), HERDR_BIN=str(herdr),
                                   PATH=str(tools) + os.pathsep + os.environ.get("PATH", os.defpath))
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", *arguments],
                    cwd=REPOSITORY, env=environment, text=True, capture_output=True,
                    check=False, timeout=300,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((home / ".local/state/buildlog/errors.log").exists(),
                                 f"{module} leaked recorder errors into its caller's HOME")


if __name__ == "__main__":
    _ = unittest.main()
