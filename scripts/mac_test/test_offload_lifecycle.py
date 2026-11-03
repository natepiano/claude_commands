"""Offload claim, copy and signal lifetimes."""

from __future__ import annotations

from typing import TYPE_CHECKING
import select
import signal
import unittest
from offload_test_support import OffloadCommandFixture
# The fixture loads offload's sibling script paths before the tree-key import.
import treekey
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard


class OffloadCommandTests(OffloadCommandFixture):
    def test_mac_run_line_waits_for_claim_and_tree_copy(self) -> None:
        self.fixture["hold_copy"] = True
        self.sync_fixture()
        process = self.start_command(script=self.claim_stub_runner())
        try:
            self.wait_for_path(self.stub_directory / "claim-started")
            output = process.stdout
            assert output is not None
            self.assertEqual(select.select([output], [], [], 0)[0], [])
            self.assertEqual(self.events(), [])
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            self.wait_for_path(self.stub_directory / "copy-started")
            self.assertEqual(select.select([output], [], [], 0)[0], [])
            self.assertEqual([event["command"] for event in self.events()], ["ssh", "rsync"])
            self.assert_claimed_commands()
            _ = (self.stub_directory / "continue-copy").write_text("continue\n")
            text, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, text)
        self.assertEqual(text.count("mac_test: running on the Mac (macOS, host Mac); tree copied in "), 1)
        self.assert_wire("passed", "")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_time_out(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("timeout")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_fail(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("ssh_error")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_omit_status(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("missing_status")

    def test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_check_fails(self) -> None:
        self.assert_takeover_signal_checks_reply_host("ssh_error")

    def test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_leftovers_remain(self) -> None:
        self.assert_takeover_signal_checks_reply_host("remaining")

    def test_signal_during_takeover_checks_reply_host_and_releases_claim_when_check_is_clean(self) -> None:
        self.assert_takeover_signal_checks_reply_host("clean")

    def test_signal_during_fresh_claim_from_process_group_preserves_reply_and_releases_without_ssh(self) -> None:
        self.set_leftover_answer("ssh_error")
        output = self.interrupt_held_claim(signal_group=True, signal_number=signal.SIGINT)
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(),
                         ["claim", "release"], output)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events_for("ssh"), [])
        self.assertFalse((self.stub_directory / "cleanup-called").exists())

    def test_signal_during_second_fresh_claim_releases_without_another_leftover_check(self) -> None:
        output = self.interrupt_held_claim(after_takeover=True)
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(),
                         ["claim", "release", "claim", "release"], output)
        self.assertFalse(self.claim_file.exists())
        ssh = self.events_for("ssh")
        self.assertEqual(len(ssh), 1)
        self.assertEqual(ssh[0]["args"][-2], "previous-mac")

    def test_signal_during_takeover_with_no_reply_host_keeps_claim_without_ssh(self) -> None:
        output = self.interrupt_held_claim(takeover=True, reply_host="")
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(), ["claim"])
        self.assert_claim_is_kept("previous-mac")
        self.assert_leftover_diagnostic(output, "missing_status")
        self.assertIn("the kept claim's host could not be read", output)
        self.assertEqual(self.events_for("ssh"), [])
        self.assertFalse((self.stub_directory / "cleanup-called").exists())

    def test_unchanged_copy_sends_its_known_tree_key_beside_the_call_id(self) -> None:
        key = self.commit_sent_tree()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(treekey.tree_key(str(self.repository)), key)
        words = self.sent_command_words()
        call_index = words.index("BUILDLOG_CALL_ID=call-17")
        self.assertEqual(words[call_index + 1], f"BUILDLOG_SENT_TREE_KEY={key}")

    def test_copy_with_different_before_and_after_tree_keys_sends_no_key(self) -> None:
        before = self.commit_sent_tree()

        def change() -> None:
            _ = (self.repository / "during-copy.rs").write_text("changed during copy\n")

        self.run_with_copy_change(change)
        after = treekey.tree_key(str(self.repository))
        self.assertIsInstance(after, str)
        self.assertNotEqual(before, after)
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_copy_with_unavailable_before_key_and_known_after_key_sends_no_key(self) -> None:
        self.assertIsNone(treekey.tree_key(str(self.repository)))

        def change() -> None:
            _ = self.commit_sent_tree()

        self.run_with_copy_change(change)
        self.assertIsInstance(treekey.tree_key(str(self.repository)), str)
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_copy_with_known_before_key_and_unavailable_after_key_sends_no_key(self) -> None:
        _ = self.commit_sent_tree()
        head_path = self.repository / ".git" / "HEAD"
        head = head_path.read_text()

        def change() -> None:
            (self.repository / ".git" / "HEAD").unlink()

        self.run_with_copy_change(change)
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        _ = head_path.write_text(head)
        self.assert_stable_copy_sends_key()

    def test_copy_with_both_tree_keys_unavailable_sends_no_key(self) -> None:
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_first_remote_line_is_printed_before_the_next_line(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "streamed now"}
        ]
        self.fixture["hold_after_first_line"] = True
        self.sync_fixture()
        process = self.start_command()
        try:
            output_before_gate = self.read_until_output(
                process, "streamed now\n"
            )
            _ = (self.stub_directory / "continue-run").write_text(
                "continue\n", encoding="utf-8"
            )
            remaining_output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        except BaseException:
            process.send_signal(signal.SIGTERM)
            _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            raise
        output = output_before_gate + remaining_output
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("streamed now\n", output_before_gate)
        self.assert_wire("passed", "")

    def test_signal_keeps_claim_when_leftover_check_does_not_prove_completion(self) -> None:
        self.fixture["hold_run"] = True
        for outcome in ("remaining", "missing_status", "timeout", "ssh_error"):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                self.set_leftover_answer(outcome)
                process = self.start_command("--workspace")
                try:
                    self.wait_for_path(self.stub_directory / "run-started")
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                self.assert_leftover_diagnostic(output, outcome)
                self.assert_claim_is_kept()
                self.assertEqual(len(self.leftover_events()), 1)

    def test_signal_before_remote_command_starts_releases_without_leftover_check(self) -> None:
        for stage in ("probe", "copy"):
            with self.subTest(stage=stage):
                self.reset_leftover_case()
                (self.stub_directory / f"{stage}-started").unlink(missing_ok=True)
                self.fixture["hold_probe"] = stage == "probe"
                self.fixture["hold_copy"] = stage == "copy"
                self.sync_fixture()
                process = self.start_command()
                try:
                    self.wait_for_path(self.stub_directory / f"{stage}-started")
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                self.assertFalse(self.claim_file.exists())
                self.assertEqual(self.leftover_events(), [])

    def test_run_signals_request_remote_cleanup_and_release_claim(self) -> None:
        self.fixture["hold_run"] = True
        self.sync_fixture()
        for signal_number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_events()
                (self.stub_directory / "run-started").unlink(missing_ok=True)
                (self.stub_directory / "cleanup-called").unlink(missing_ok=True)
                process = self.start_command("--workspace")
                try:
                    self.wait_for_path(self.stub_directory / "run-started")
                    process.send_signal(signal_number)
                    output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                except BaseException:
                    process.kill()
                    _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    raise
                self.assertEqual(process.returncode, 128 + signal_number, output)
                self.assertTrue(
                    (self.stub_directory / "cleanup-called").exists()
                )
                self.assertFalse(self.claim_file.exists())
                ssh_events = self.events_for("ssh")
                self.assertEqual(len(ssh_events), 3)
                cleanup_arguments = ssh_events[-1]["args"]
                self.assertEqual(
                    cleanup_arguments[:-1],
                    [
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=6",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=3",
                        "mac",
                    ],
                )
                cleanup = cleanup_arguments[-1]
                self.assert_leftover_selectors(cleanup)
                self.assert_claimed_commands()

    def test_second_signal_during_cleanup_keeps_first_status_and_releases(self) -> None:
        self.fixture["hold_run"] = True
        self.fixture["hold_cleanup"] = True
        self.sync_fixture()
        process = self.start_command("--workspace")
        try:
            self.wait_for_path(self.stub_directory / "run-started")
            process.send_signal(signal.SIGTERM)
            self.wait_for_path(self.stub_directory / "cleanup-started")
            process.send_signal(signal.SIGHUP)
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        except BaseException:
            process.kill()
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            raise

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
        self.assertTrue((self.stub_directory / "cleanup-called").exists())
        self.assertFalse(self.claim_file.exists())

    def test_ignored_interrupt_becomes_default_before_claim(self) -> None:
        startup = self.root / "interrupt-startup"
        startup.mkdir()
        marker = self.stub_directory / "interrupt-is-default"
        gate = self.stub_directory / "continue-after-interrupt-default"
        _ = (startup / "sitecustomize.py").write_text(
            "from __future__ import annotations\n"
            + "import os\n"
            + "from pathlib import Path\n"
            + "import signal\n"
            + "import time\n"
            + "original_signal = signal.signal\n"
            + "def observed_signal(signal_number: int, handler: object) -> object:\n"
            + "    previous = original_signal(signal_number, handler)\n"
            + "    if signal_number == signal.SIGINT and handler == signal.SIG_DFL:\n"
            + "        Path(os.environ['TEST_INTERRUPT_DEFAULT_MARKER']).write_text(\n"
            + "            'ready\\n', encoding='utf-8'\n"
            + "        )\n"
            + "        gate = Path(os.environ['TEST_INTERRUPT_DEFAULT_GATE'])\n"
            + "        while not gate.exists():\n"
            + "            time.sleep(0.01)\n"
            + "    return previous\n"
            + "signal.signal = observed_signal\n",
            encoding="utf-8",
        )
        process = self.start_command(
            extra={
                "PYTHONPATH": str(startup),
                "TEST_INTERRUPT_DEFAULT_MARKER": str(marker),
                "TEST_INTERRUPT_DEFAULT_GATE": str(gate),
            },
            ignore_interrupt=True,
        )
        try:
            self.wait_for_path(marker)
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        except BaseException:
            process.kill()
            _ = gate.write_text("continue\n", encoding="utf-8")
            _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            raise

        self.assertEqual(process.returncode, -signal.SIGINT, output)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events(), [])
        self.assertFalse(self.result_path.exists())


if __name__ == "__main__":
    _ = unittest.main()
