"""Test the default probe window before worker identity becomes available."""
from __future__ import annotations

import os
import subprocess
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.implement_launcher_test_support import ImplementLauncherFixture


class ImplementLauncherProbeTests(ImplementLauncherFixture):
    def test_new_worker_gets_thirty_one_second_identity_probes(self) -> None:
        session = self.start_phase("default-probes")
        binaries = session / "bin"
        binaries.mkdir()
        sleep = binaries / "sleep"
        _ = sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_PROBES"\n')
        sleep.chmod(0o755)
        agent = self.root / "scripts/agents/agent_bg.sh"
        _ = agent.write_text('#!/bin/bash\nexec python3 "$TEST_WAIT_HELPER" "$TEST_RELEASE"\n')
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text('#!/bin/bash\n: > "$TEST_RELEASE"\n')
        environment = self.launch_environment(session)
        _ = environment.pop("UNIT_WORKER_POLL_SECS")
        _ = environment.pop("UNIT_WORKER_WAIT_ATTEMPTS")
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "TEST_PROBES": str(session / "probes"), "TEST_RELEASE": str(session / "release")})
        result = subprocess.run(self.implement_command(session), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "probes").read_text().splitlines(), ["1"] * 30)
        self.assertEqual(self.open_passes(session)["impl"]["status"], "completed")
