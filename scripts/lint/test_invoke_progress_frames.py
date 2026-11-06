"""Keep Cargo frames in the capture log and stream readable output."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InvokeProgressFramesTests(unittest.TestCase):
    def test_cargo_frames_stay_in_the_log_and_preserve_the_command_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cargo = root / "cargo"
            _ = cargo.write_text("#!/usr/bin/env bash\nprintf '\\rBuilding [=>] 1/4: a\\r\\033[K   Compiling a\\nlast line'\nexit 23\n")
            cargo.chmod(0o700)
            config = root / "lint_config.sh"
            _ = config.write_text("lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n")
            environment = {key: value for key, value in os.environ.items() if not key.startswith("BUILDLOG_")}
            environment.update(HOME=str(root), TMPDIR=str(root), BUILDLOG_OFF="1", LINT_CONFIG_READER=str(config))
            command = '''source "$1"
buildlog_step_compiles() { return 1; }
buildlog_end() { cp -- "$2" "$saved_log"; }
sweep_after_step() { :; }
saved_log=$3
run_step_attempt "$2"
'''
            result = subprocess.run(["bash", "-c", command, "frames", str(Path(__file__).with_name("invoke.sh")),
                str(cargo), str(root / "saved.log")], env=environment, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertIn("   Compiling a\nlast line\n", result.stdout)
            self.assertNotIn("Building", result.stdout)
            self.assertNotIn("\r", result.stdout)
            self.assertIn(b"\rBuilding [=>] 1/4: a\r", (root / "saved.log").read_bytes())
