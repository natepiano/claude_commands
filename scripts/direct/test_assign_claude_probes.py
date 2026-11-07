"""Test the default probe window before worker identity becomes available."""
from __future__ import annotations

import subprocess
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture


class AssignClaudeLauncherProbeTests(AssignClaudeLauncherFixture):
    def test_empty_probe_settings_use_defaults_and_complete_the_launch(self) -> None:
        session = self.start_phase("empty-probes")
        environment = self.launch_environment(session)
        environment.update(UNIT_WORKER_WAIT_ATTEMPTS="", UNIT_WORKER_POLL_SECS="")
        result = subprocess.run(self.assign_command(session), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.open_passes(session)["impl"]["status"], "completed")
        self.assertEqual((session / "impl_status_impl").read_text(), "implemented\n")

    def test_new_worker_gets_thirty_one_second_identity_probes(self) -> None:
        session = self.start_phase("default-probes")
        launcher = self.root / "scripts/direct/assign_claude.py"
        _ = launcher.write_text(launcher.read_text().replace("time.sleep(float(", "record_sleep(float(") +
            '\ndef record_sleep(seconds: float) -> None:\n' +
            '    with Path(os.environ["TEST_PROBES"]).open("a") as output:\n' +
            '        output.write(f"{seconds:g}\\n")\n')
        agent = self.root / "scripts/agents/agent_bg.sh"
        _ = agent.write_text('#!/bin/bash\nexec python3 "$TEST_WAIT_HELPER" "$TEST_RELEASE"\n')
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text('#!/bin/bash\n: > "$TEST_RELEASE"\n')
        environment = self.launch_environment(session)
        _ = environment.pop("UNIT_WORKER_POLL_SECS")
        _ = environment.pop("UNIT_WORKER_WAIT_ATTEMPTS")
        environment.update({"TEST_PROBES": str(session / "probes"), "TEST_RELEASE": str(session / "release")})
        result = subprocess.run(self.assign_command(session), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "probes").read_text().splitlines(), ["1"] * 30)
        self.assertEqual(self.open_passes(session)["impl"]["status"], "completed")
