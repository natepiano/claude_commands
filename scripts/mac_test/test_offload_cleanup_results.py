"""Offload cleanup results contracts."""

from __future__ import annotations

import unittest
from offload_cleanup_test_support import SeededCleanupFixture


class OffloadCommandTests(SeededCleanupFixture):
    def test_stream_order_and_pass_status(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "first marker"},
            {"stream": "stderr", "text": "second marker"},
            {"stream": "stdout", "text": "third marker"},
        ]
        self.sync_fixture()
        result = self.command("--workspace")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(
            result.stdout.startswith(
                "mac_test: running on the Mac (macOS, host Mac); tree copied in "
            ),
            result.stdout,
        )
        positions = [
            result.stdout.index(marker)
            for marker in ("first marker", "second marker", "third marker")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("mac_exit=", result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse(self.claim_file.exists())

    def test_copy_claims_as_a_copy_and_names_the_copy(self) -> None:
        result = self.command("--workspace", copy=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stdout.startswith(
            "mac_test: running a copy on the Mac while natedev compiles (host Mac); tree copied in "), result.stdout)
        self.assertNotIn("mac_test: running on the Mac", result.stdout)
        self.assert_wire("passed", "")
        held = [event for event in self.events() if event["command"] in ("ssh", "rsync")]
        self.assertTrue(held)
        self.assertTrue(all(event.get("copy_claim") is True for event in held), held)
        self.assertFalse(self.claim_file.exists())
        self.clear_events()
        self.assertEqual(self.command("--workspace").returncode, 0)
        self.assertTrue(all(event.get("copy_claim") is False for event in self.events_for("rsync")))

    def test_failed_run_returns_remote_status_and_notice(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stderr", "text": "nextest failed"}
        ]
        self.fixture["run_status_line"] = "mac_exit=42"
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 42, result.stdout)
        self.assertIn("nextest failed\n", result.stdout)
        self.assertIn("this ran on the Mac (macOS)", result.stdout)
        self.assertIn(
            "only a test that must run on Linux may rerun with --local",
            result.stdout,
        )
        self.assertNotIn("unrelated to your change", result.stdout)
        self.assertNotIn("mac_exit=42", result.stdout)
        self.assert_wire("failed", "")
        self.assertFalse(self.claim_file.exists())

    def test_no_test_status_runs_on_natedev_instead(self) -> None:
        self.fixture["run_status_line"] = "mac_exit=4"
        self.sync_fixture()

        result = self.command()

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead\n",
            result.stdout,
        )
        self.assertNotIn("mac_exit=4", result.stdout)
        self.assert_wire("declined", "no_tests")
        self.assertFalse(self.claim_file.exists())

    def test_nextest_failure_status_remains_a_mac_failure(self) -> None:
        self.fixture["run_status_line"] = "mac_exit=1"
        self.sync_fixture()

        result = self.command()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assert_wire("failed", "")

    def test_cleanup_success_keeps_the_reported_test_result_and_hides_markers(self) -> None:
        for status in (0, 1, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertNotIn("mac_exit=", result.stdout)
                self.assertNotIn("mac_cleanup=", result.stdout)
                self.assertNotIn("Mac build output", result.stdout)

    def test_cleanup_failure_reports_once_without_changing_test_result(self) -> None:
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.fixture["run_cleanup_line"] = "mac_cleanup=7"
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(result.stdout.count("Mac build output not removed: 7\n"), 1)
                self.assertNotIn("mac_cleanup=", result.stdout)

    def test_kept_build_output_releases_claim_without_leftover_check_or_notice(self) -> None:
        self.fixture["run_cleanup_line"] = "mac_cleanup=kept"
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(self.leftover_events(), [])
                self.assertFalse(self.claim_file.exists())
                self.assertNotIn("Mac build output", result.stdout)
                self.assertNotIn("mac_cleanup=", result.stdout)

    def test_other_target_dir_reports_kept_output_without_changing_test_result(self) -> None:
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.fixture["run_cleanup_line"] = "mac_cleanup=skipped"
                self.sync_fixture()
                result = self.command("--target-dir", "other target")
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(result.stdout.count("Mac build output kept: the run used another target dir\n"), 1)
                self.assertNotIn("mac_cleanup=", result.stdout)

    def test_disconnect_after_test_status_reports_unconfirmed_cleanup_and_keeps_result(self) -> None:
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.fixture["run_cleanup_line"] = ""
                self.fixture["run_ssh_status"] = 255
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(result.stdout.count("Mac build output cleanup unconfirmed\n"), 1)
                self.assertNotIn("mac_exit=", result.stdout)



if __name__ == "__main__":
    _ = unittest.main()
