"""Real verification hold waits: events."""

from __future__ import annotations

import ctypes
import errno
import os
import time
import unittest
from unittest import mock

from scripts.direct.verify_hold_wait_test_support import (
    TEST_HANG_GUARD_SECS,
    VerifyHoldWaitFixture,
    grant_call,
    read_cycle_file,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_local_admission_waits_for_release_notifications_instead_of_sleeping(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.direct.local_release_wait import LocalReleaseEvents

        self.hold()
        def grant_on_notification(_seconds: float) -> None:
            grant_call(self.cycle_file)

        with mock.patch.dict(os.environ, self.fixture.environment), \
             mock.patch.object(time, "sleep", side_effect=AssertionError("local admission must wake on release")), \
             mock.patch.object(LocalReleaseEvents, "wait", side_effect=grant_on_notification) as waiting:
            self.assertEqual(build_targets.command_wait_local("notification-call"), 0)
        waiting.assert_called_once_with(0.01)

    def test_local_admission_without_inotify_polls_until_release(self) -> None:
        from scripts.build_targets import build_targets

        self.hold()
        def release_on_poll(_seconds: float) -> None:
            grant_call(self.cycle_file)

        with mock.patch.dict(os.environ, self.fixture.environment):
            with mock.patch.object(ctypes, "CDLL", return_value=object()), \
                 mock.patch.object(time, "sleep", side_effect=release_on_poll) as polling:
                self.assertEqual(build_targets.command_wait_local("polling-call"), 0)
            polling.assert_called_once_with(0.01)
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])

    def test_local_release_waiter_keeps_linux_notifications(self) -> None:
        from scripts.direct import local_release_wait

        holders = self.fixture.root / "event-holders"
        release = self.fixture.root / "event-release"
        holders.mkdir()
        release.mkdir()
        with mock.patch.object(time, "sleep", side_effect=AssertionError("Linux must use notifications")), \
             local_release_wait.local_release_waiter(holders, release) as waiter:
            self.assertIsInstance(waiter, local_release_wait.LocalReleaseEvents)
            waiter.arm()
            _ = (holders / "owner").write_text("released")
            waiter.wait(TEST_HANG_GUARD_SECS)

    def test_local_release_waiter_polls_when_kernel_lacks_inotify(self) -> None:
        from scripts.direct import local_release_wait

        for unavailable in (errno.ENOSYS, errno.ENOTSUP):
            with self.subTest(errno=unavailable), \
                 mock.patch.object(local_release_wait, "LocalReleaseEvents", side_effect=OSError(unavailable, "unavailable")), \
                 mock.patch.object(time, "sleep") as polling:
                with local_release_wait.local_release_waiter(self.fixture.root, self.fixture.root) as waiter:
                    self.assertIsInstance(waiter, local_release_wait.IntervalReleasePolling)
                    waiter.arm()
                    waiter.wait(15)
                polling.assert_called_once_with(15)

    def test_local_release_waiter_does_not_hide_resource_errors(self) -> None:
        from scripts.direct import local_release_wait

        with mock.patch.object(local_release_wait, "LocalReleaseEvents", side_effect=OSError(errno.EMFILE, "exhausted")), \
             self.assertRaises(OSError) as failed:
            _ = local_release_wait.local_release_waiter(self.fixture.root, self.fixture.root)
        self.assertEqual(failed.exception.errno, errno.EMFILE)


if __name__ == "__main__":
    _ = unittest.main()
