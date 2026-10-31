"""Prove shutdown tests cannot reach a real message sender."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Never, Protocol, cast, final, override
from unittest.mock import patch

import shutdown_record
import remote
import remote_control
import restart
import restart_support
import shutdown
from test_support import ShutdownSendLeaf, ShutdownTestCase
from push_texts import render, user_send_argv


LOGIN = "owner@example.com"
LABEL = "claude2"
NOW = "2026-10-10T21:00:00+00:00"


class RetireShutdownSession(Protocol):
    def __call__(
        self,
        current: shutdown_record.SessionShutdown,
        *,
        dry_run: bool,
    ) -> None: ...


def restart_fixture() -> shutdown_record.SessionShutdown:
    from test_restart import entry, top_level

    return entry(top_level("x", name="fixture session"))


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

    def send_script(self) -> tuple[Path, Path]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = root / "record-send"
        log = root / "send-arguments"
        _ = script.write_text(
            f"#!{sys.executable}\n"
            + "from pathlib import Path\n"
            + "import sys\n"
            + f"Path({str(log)!r}).write_text(\n"
            + "    '\\n'.join(sys.argv[1:]) + '\\n\\n' + sys.stdin.read(),\n"
            + "    encoding='utf-8',\n"
            + ")\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script, log

    def assert_recorded_alert(self, log: Path) -> None:
        push = render("shutdown.restart_incomplete", account='"claude2"', machine="Mac")
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), [*user_send_argv(push)[2:], ""])

    def test_account_alert_without_a_fake_fails(self) -> None:
        class UnfakedAlert(ShutdownTestCase):
            def test_alert(self) -> None:
                _ = restart_support.alert_user(
                    render("shutdown.restart_incomplete", account='"claude2"', machine="Mac"),
                )

        self.assert_blocked(
            UnfakedAlert("test_alert"), ShutdownSendLeaf.DELIVER_MESSAGE, "user"
        )

    def test_catching_the_alert_error_still_fails_at_cleanup(self) -> None:
        class SwallowedAlert(ShutdownTestCase):
            def test_alert(self) -> None:
                try:
                    _ = restart_support.alert_user(
                            render("shutdown.restart_incomplete", account='"claude2"', machine="Mac"),
                    )
                except Exception:
                    pass

        self.assert_blocked(
            SwallowedAlert("test_alert"), ShutdownSendLeaf.DELIVER_MESSAGE, "user"
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

    def test_retiring_shutdown_messages_fails(self) -> None:
        class UnfakedRetirement(ShutdownTestCase):
            def test_retire(self) -> None:
                retire = cast(
                    Callable[[str, str], None],
                    getattr(restart_support, "retire_shutdown_messages"),
                )
                retire(LOGIN, "x")

        self.assert_blocked(
            UnfakedRetirement("test_retire"),
            ShutdownSendLeaf.RETIRE_SHUTDOWN_MESSAGES,
            "session:x",
        )

    def test_all_four_remote_bindings_fail_before_ssh(self) -> None:
        for module in (remote, restart_support, restart, shutdown):
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


    def test_notifier_start_and_stop_fail(self) -> None:
        for verb in ("start", "stop"):
            with self.subTest(verb=verb):
                class UnfakedNotifier(ShutdownTestCase):
                    def test_notifier(self) -> None:
                        restart_support.run_notifier(verb, "x")

                self.assert_blocked(
                    UnfakedNotifier("test_notifier"),
                    ShutdownSendLeaf.RUN_NOTIFIER,
                    "notifier x",
                )

    def test_restart_retirement_fails_when_it_would_send(self) -> None:
        current = restart_fixture()

        class UnfakedRestartRetirement(ShutdownTestCase):
            def test_retire(self) -> None:
                retire = cast(RetireShutdownSession, getattr(restart, "_retire"))
                retire(current, dry_run=False)

        self.assert_blocked(
            UnfakedRestartRetirement("test_retire"),
            ShutdownSendLeaf.RESTART_RETIRE,
            "session:x",
        )

    def test_restart_retirement_dry_run_prints_without_sending(self) -> None:
        current = restart_fixture()

        class DryRunRestartRetirement(ShutdownTestCase):
            def test_retire(self) -> None:
                retire = cast(RetireShutdownSession, getattr(restart, "_retire"))
                retire(current, dry_run=True)

        output = io.StringIO()
        with redirect_stdout(output):
            result = self.run_without_subprocesses(
                DryRunRestartRetirement("test_retire")
            )
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertIn("retire --to session:x", output.getvalue())

    def test_allowing_delivery_in_a_test_runs_only_the_fake_script(self) -> None:
        script, log = self.send_script()

        class AllowedDelivery(ShutdownTestCase):
            def test_alert(self) -> None:
                self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
                _ = restart_support.alert_user(
                    render("shutdown.restart_incomplete", account='"claude2"', machine="Mac"),
                )

        result = unittest.TestResult()
        with patch.dict(os.environ, {"SHUTDOWN_SEND": str(script)}):
            _ = AllowedDelivery("test_alert").run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assert_recorded_alert(log)

    def test_allowing_delivery_in_setup_runs_only_the_fake_script(self) -> None:
        script, log = self.send_script()

        class AllowedDelivery(ShutdownTestCase):
            @override
            def setUp(self) -> None:
                super().setUp()
                self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)

            def test_alert(self) -> None:
                _ = restart_support.alert_user(
                    render("shutdown.restart_incomplete", account='"claude2"', machine="Mac"),
                )

        result = unittest.TestResult()
        with patch.dict(os.environ, {"SHUTDOWN_SEND": str(script)}):
            _ = AllowedDelivery("test_alert").run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assert_recorded_alert(log)

    def test_patching_a_leaf_replaces_its_guard_without_allowing_it(self) -> None:
        calls: list[tuple[list[str], str, str]] = []

        def fake_delivery(
            command: list[str], recipient: str, text: str
        ) -> restart_support.MessageDelivery:
            calls.append((command, recipient, text))
            return restart_support.MessageSent(kind="sent")

        class PatchedDelivery(ShutdownTestCase):
            def test_alert(self) -> None:
                with patch.object(restart_support, "_deliver_message", new=fake_delivery):
                    _ = restart_support.alert_user(
                            render("shutdown.restart_incomplete", account='"claude2"', machine="Mac"),
                    )

        result = self.run_without_subprocesses(PatchedDelivery("test_alert"))
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ("user", ""))

    def test_allowing_remote_restores_all_four_bindings_under_fake_ssh(self) -> None:
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
                    for module in (remote, restart_support, restart, shutdown):
                        run_remote = cast(
                            Callable[[list[str]], tuple[int, str]],
                            getattr(module, "run_remote"),
                        )
                        self.assertEqual(
                            run_remote(["status", LOGIN]), (0, "fixture output")
                        )

        result = self.run_without_subprocesses(AllowedRemote("test_remote"))
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertEqual(command[0], "ssh")
            self.assertEqual(command[-2], remote.other_machine())

    def test_remote_control_fixer_launch_is_blocked(self) -> None:
        class UnfakedFixer(ShutdownTestCase):
            def test_launch(self) -> None:
                remote_control.launch_fixer("owner", 321, "original", "Owner", "Owner")
        self.assert_blocked(UnfakedFixer("test_launch"),
            ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER, "session:owner")

    def test_leaf_enum_has_the_specified_dotted_names(self) -> None:
        class LeafNames(ShutdownTestCase):
            def test_names(self) -> None:
                self.assertEqual(
                    {leaf.name: leaf.value for leaf in ShutdownSendLeaf},
                    {
                        "DELIVER_MESSAGE": "restart_support._deliver_message",
                        "RETIRE_SHUTDOWN_MESSAGES": "restart_support.retire_shutdown_messages",
                        "RESTART_RETIRE": "restart._retire",
                        "RESUME_CODEX_WORKERS": "restart._send_worker_resume",
                        "RUN_REMOTE": "remote.run_remote",
                        "LAUNCH_REMOTE_CONTROL_FIXER": "remote_control.launch_fixer",
                        "RUN_NOTIFIER": "restart_support.run_notifier",
                    },
                )

        result = self.run_without_subprocesses(LeafNames("test_names"))
        self.assertTrue(result.wasSuccessful(), result.failures)


if __name__ == "__main__":
    _ = unittest.main()
