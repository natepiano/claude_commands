"""Real verify.sh Mac pressure with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacPressureTests(VerifyMacOffloadFixture):
    def test_private_memory_pressure_blocks_local_work_until_its_release_event(self) -> None:
        pressure = Path(self.environment["BUILDLOG_MEMORY_PRESSURE"])
        _ = pressure.write_text("full avg10=20.00 avg60=0.00 avg300=0.00 total=0\n")
        waiting = self.runner_state / "pressure-wait"
        release = self.runner_state / "pressure-release"
        self.write_executable(self.bin_directory / "sleep", '''#!/bin/sh
: > "$TEST_PRESSURE_WAIT"
exec "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_PRESSURE_RELEASE"
''')
        process = self.start_verify("check", "sample", extra={
            "TEST_PRESSURE_WAIT": str(waiting), "TEST_PRESSURE_RELEASE": str(release),
        })
        try:
            self.wait_for_path(waiting, (process,))
            self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["check"]],
                             [], "pressure did not block local execution")
            ready = pressure.with_suffix(".ready")
            _ = ready.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
            _ = ready.replace(pressure)
            release.touch()
            output, errors = process.communicate(timeout=HANG_GUARD_S)
            self.assertEqual(process.returncode, 0, output + errors)
            self.assertIn("memory pressure: full avg10 20.0", output + errors)
            self.assertEqual(len([event for event in self.cargo_events()
                                  if event["args"][:1] == ["check"]]), 1)
        finally:
            if process.poll() is None:
                self.stop_verify(process)


if __name__ == "__main__":
    _ = unittest.main()
