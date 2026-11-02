"""Real verify.sh Mac signals with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
import signal
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacSignalsTests(VerifyMacOffloadFixture):
    def test_runner_receives_each_forwarded_signal_before_local_work(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_observations()
                process = self.start_verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_HOLD": "1"},
                )
                try:
                    self.wait_for_path(self.runner_state / "runner-signals-ready")
                    process.send_signal(signal_number)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                except BaseException:
                    self.stop_verify(process)
                    raise
                self.assertEqual(
                    process.returncode, 128 + signal_number, output + errors
                )
                self.assertEqual(
                    (self.runner_state / "runner-signal").read_text(
                        encoding="utf-8"
                    ),
                    str(signal_number),
                )
                self.assertEqual(self.local_runs(), [])
                runners = self.runner_events()
                self.assertEqual(len(runners), 1)
                self.assertFalse(runners[0]["token_held"])


if __name__ == "__main__":
    _ = unittest.main()
