"""Keyed delivery survives process stops without recording an unaccepted send."""
from __future__ import annotations

import os
import select
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable, Generator
from unittest import mock

from scripts.message import send
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase, private_sender_acceptance


@contextmanager
def stopped_sender(run: Callable[[Callable[[], None]], None]) -> Generator[None]:
    """Synchronize a child at the chosen boundary, then kill it without Python cleanup."""
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        def checkpoint() -> None:
            _ = os.write(writer, b"ready")
            signal.pause()
        try:
            run(checkpoint)
        finally:
            os._exit(91)
    os.close(writer)
    try:
        ready, _, _ = select.select([reader], [], [], 10)
        if not ready or os.read(reader, 5) != b"ready":
            raise AssertionError("sender child did not reach checkpoint")
        yield
    finally:
        os.kill(child, signal.SIGKILL)
        _, status = os.waitpid(child, 0)
        os.close(reader)
        if not os.WIFSIGNALED(status) or os.WTERMSIG(status) != signal.SIGKILL:
            raise AssertionError(f"sender child exited without SIGKILL: {status}")


class KeyedDeliveryStopTests(NotificationGuardedTestCase):
    def check_stop(self, boundary: str) -> None:
        for channel in ("message", "push"):
            with self.subTest(channel=channel, boundary=boundary), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                def child(checkpoint: Callable[[], None]) -> None:
                    before = checkpoint if boundary == "before_accept" else lambda: None
                    after = checkpoint if boundary == "after_accept" else lambda: None
                    with private_sender_acceptance(state, before_accept=before, after_accept=after) as fixture:
                        if boundary == "after_record":
                            def log(*_args: object, **_kwargs: object) -> None:
                                checkpoint()
                                # The child is killed before returning; the real log is used by the retry.
                            with mock.patch.object(send, "log", side_effect=log):
                                _ = send.send(fixture.options(channel))
                        else:
                            _ = send.send(fixture.options(channel))
                with stopped_sender(child):
                    pass
                with private_sender_acceptance(state) as fixture:
                    initial = 0 if boundary == "before_accept" else 1
                    self.assertEqual(fixture.count(), initial)
                    result = send.send(fixture.options(channel))
                    expected = 2 if boundary == "after_accept" else 1
                    self.assertEqual(fixture.count(), expected)
                    self.assertEqual(result.outcome, "skipped" if boundary == "after_record" else "sent")
                    self.assertEqual(send.send(fixture.options(channel)).outcome, "skipped")
                    self.assertEqual(fixture.count(), expected)

    def test_stop_after_lock_before_acceptance_retries(self) -> None:
        self.check_stop("before_accept")

    def test_stop_after_acceptance_before_record_allows_one_duplicate(self) -> None:
        self.check_stop("after_accept")

    def test_stop_after_record_before_watcher_mark_does_not_redeliver(self) -> None:
        self.check_stop("after_record")

    def test_contender_is_retryable_and_does_not_claim_acceptance(self) -> None:
        for channel in ("message", "push"):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                def child(checkpoint: Callable[[], None]) -> None:
                    with private_sender_acceptance(state, before_accept=checkpoint) as fixture:
                        _ = send.send(fixture.options(channel))
                with stopped_sender(child), private_sender_acceptance(state) as fixture:
                    result = send.send(fixture.options(channel))
                    self.assertEqual(result.outcome, "retry")
                    self.assertNotIn(send.EXIT[result.outcome], (0, 1))
                    self.assertEqual(fixture.count(), 0)
                with private_sender_acceptance(state) as fixture:
                    self.assertEqual(send.send(fixture.options(channel)).outcome, "sent")
                    self.assertEqual(fixture.count(), 1)

    def test_logical_recipient_survives_socket_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory, private_sender_acceptance(Path(directory)) as fixture:
            first = fixture.options("message")._replace(recipient_identity=send.LogicalRecipientIdentity("showrunner:builds"))
            second = fixture.options("message", address="uds:/fixture/restarted.sock")._replace(recipient_identity=send.LogicalRecipientIdentity("showrunner:builds"))
            self.assertEqual(send.send(first).outcome, "sent")
            self.assertEqual(send.send(second).outcome, "skipped")
            self.assertEqual(fixture.count(), 1)
