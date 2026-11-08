"""Real verify.sh Mac pressure with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_pressure_reading


@final
class VerifyMacPressureTests(VerifyMacOffloadFixture):
    def test_private_memory_pressure_blocks_local_work_until_its_release_event(self) -> None:
        pressure = Path(self.environment["BUILDLOG_MEMORY_PRESSURE"])
        write_pressure_reading(pressure, full_avg10=20.0)
        process = self.start_verify("check", "sample")
        try:
            self.wait_for_memory_wait(process)
            self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["check"]],
                             [], "pressure did not block local execution")
            ready = pressure.with_suffix(".ready")
            write_pressure_reading(ready)
            _ = ready.replace(pressure)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output + errors)
            self.assertIn("memory pressure: full avg10 20.0", output + errors)
            self.assertEqual(len([event for event in self.cargo_events()
                                  if event["args"][:1] == ["check"]]), 1)
        finally:
            if process.poll() is None:
                self.stop_verify(process)


if __name__ == "__main__":
    _ = unittest.main()
