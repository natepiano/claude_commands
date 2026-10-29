"""Real verification hold waits: workers."""

from __future__ import annotations

import unittest

from scripts.direct.verify_hold_wait_test_support import VerifyHoldWaitFixture


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_verification_thread_overrides_the_servers_impl_slot(self) -> None:
        self.assert_wait_follows_worker_thread("fixture-test")

    def test_fix_named_worker_uses_its_recorded_test_slot(self) -> None:
        self.assert_wait_follows_worker_thread("fixture-fix")


if __name__ == "__main__":
    _ = unittest.main()
