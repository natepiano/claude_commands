"""Prove shutdown tests cannot reach a real message sender."""

from __future__ import annotations

import subprocess
import unittest
from collections.abc import Callable
from typing import Never, cast, final
from unittest.mock import patch

from scripts.shutdown import remote
from scripts.shutdown import remote_control
from scripts.shutdown import restart_support
from scripts.shutdown import shutdown
from scripts.shutdown.test_support import ShutdownSendLeaf, ShutdownTestCase


LOGIN = "owner@example.com"
LABEL = "claude2"
NOW = "2026-10-10T21:00:00+00:00"


@final
class SendGuardTests(ShutdownTestCase):
    def run_without_subprocesses(self, case: ShutdownTestCase) -> unittest.TestResult:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def record_call(*args: object, **kwargs: object) -> Never:
            calls.append((args, kwargs))
            raise AssertionError("the send guard allowed a subprocess call")

        result = unittest.TestResult()
        with (
            patch.object(subprocess, "run", new=record_call),
            patch.object(subprocess, "Popen", new=record_call),
        ):
            _ = case.run(result)
        self.assertEqual(calls, [], "a guarded case reached subprocess.run or Popen")
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [])
        return result

    def assert_blocked(
        self, case: ShutdownTestCase, leaf: ShutdownSendLeaf, recipient: str
    ) -> None:
        result = self.run_without_subprocesses(case)
        self.assertFalse(result.wasSuccessful())
        failures = "\n".join(text for _, text in result.failures)
        self.assertIn(
            f"shutdown test reached real send leaf {leaf.value} for {recipient}; "
            + "fake the step above it, or "
            + f"allow_leaf(ShutdownSendLeaf.{leaf.name}) and fake the subprocess beneath it",
            failures,
        )


    def test_session_message_fails_for_local_and_remote_delivery(self) -> None:
        for machine in ("", "mac"):
            with self.subTest(machine=machine):
                class UnfakedSessionMessage(ShutdownTestCase):
                    def test_message(self) -> None:
                        _ = restart_support.send_message(
                            "session:x", "summary", "text", machine=machine
                        )

                self.assert_blocked(
                    UnfakedSessionMessage("test_message"),
                    ShutdownSendLeaf.DELIVER_MESSAGE,
                    "session:x",
                )


    def test_remote_bindings_fail_before_ssh(self) -> None:
        for module in (remote, shutdown):
            with self.subTest(binding=f"{module.__name__}.run_remote"):
                class UnfakedRemote(ShutdownTestCase):
                    def test_remote(self) -> None:
                        run_remote = cast(
                            Callable[[list[str]], tuple[int, str]],
                            getattr(module, "run_remote"),
                        )
                        _ = run_remote(["status", LOGIN])

                self.assert_blocked(
                    UnfakedRemote("test_remote"),
                    ShutdownSendLeaf.RUN_REMOTE,
                    f"{remote.other_machine()}: shutdown.py status {LOGIN}",
                )


    def test_allowing_remote_restores_bindings_under_fake_ssh(self) -> None:
        commands: list[list[str]] = []

        def fake_ssh(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "fixture output\nrc=0\n", "")

        class AllowedRemote(ShutdownTestCase):
            def test_remote(self) -> None:
                self.allow_leaf(ShutdownSendLeaf.RUN_REMOTE)
                with patch.object(subprocess, "run", new=fake_ssh):
                    for module in (remote, shutdown):
                        run_remote = cast(
                            Callable[[list[str]], tuple[int, str]],
                            getattr(module, "run_remote"),
                        )
                        self.assertEqual(
                            run_remote(["status", LOGIN]), (0, "fixture output")
                        )

        result = self.run_without_subprocesses(AllowedRemote("test_remote"))
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertEqual(command[0], "ssh")
            self.assertEqual(command[-2], remote.other_machine())

    def test_remote_control_fixer_launch_is_blocked(self) -> None:
        class UnfakedFixer(ShutdownTestCase):
            def test_launch(self) -> None:
                remote_control.launch_fixer("owner", 321, "original", "Owner", "Owner")
        self.assert_blocked(UnfakedFixer("test_launch"),
            ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER, "session:owner")


if __name__ == "__main__":
    _ = unittest.main()
