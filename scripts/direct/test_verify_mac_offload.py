"""Real verify.sh Mac offload with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, HANG_GUARD_S as HANG_GUARD_S, VERIFY as VERIFY, stub_build_tool_path as stub_build_tool_path, CARGO_STUB as CARGO_STUB, RUSTC_STUB as RUSTC_STUB, CallRecord as CallRecord


@final
class VerifyMacOptionValidationTests(VerifyMacOffloadFixture):
    def test_local_flag_requires_a_non_option_reason_before_any_work(self) -> None:
        expected = (
            'verify.sh: --local needs a reason: --local "<why this test cannot pass on '
            'macOS>". Leave it off to run on the Mac.\n'
        )
        cases = (
            ("test", "sample", "--local"),
            ("test", "sample", "--local", ""),
            ("test", "sample", "--local", " \t "),
            ("test", "sample", "--local", "--filter", "alpha"),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, expected)
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_local_flag_refuses_a_mac_reservation_before_any_work(self) -> None:
        reasons = (
            "Mac reserved for demo",
            "RESERVATION: demo has the Mac",
            "showrunner ruling: no Mac claim",
            "Mac held by demo",
            "the Mac is blocked",
            "Mac busy, hold off",
        )

        for reason in reasons:
            with self.subTest(reason=reason):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify("test", "sample", "--local", reason)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr,
                    "verify.sh: --local is only for a test that cannot pass on macOS, not to keep "
                    + f'tests off the Mac ("{reason}"). To reserve the Mac, its holder runs '
                    + "/build:hold hold <why> target mac: tests then skip the Mac by themselves, "
                    + "/build:status shows the hold, and it ends at its expiry. Leave --local off.\n",
                )
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_missing_runner_skips_offload(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"VERIFY_MAC_RUNNER": str(self.root / "absent-runner.py")},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)


# Compatibility for fixtures that use the shared shell/tool contract.
VerifyMacOffloadTests = VerifyMacOffloadFixture


if __name__ == "__main__":
    _ = unittest.main()
