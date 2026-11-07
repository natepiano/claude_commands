"""Test default settings for the worker wait."""
from __future__ import annotations

import subprocess
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture


class AssignClaudeLauncherProbeTests(AssignClaudeLauncherFixture):
    def test_empty_probe_settings_use_defaults_and_complete_the_launch(self) -> None:
        session = self.start_phase("empty-probes")
        environment = self.launch_environment(session)
        environment.update(UNIT_WORKER_POLL_SECS="")
        result = subprocess.run(self.assign_command(session), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.open_passes(session)["impl"]["status"], "completed")
        self.assertEqual((session / "impl_status_impl").read_text(), "implemented\n")
