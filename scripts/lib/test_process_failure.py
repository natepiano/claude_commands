"""Failed commands retain the cause from both captured output streams."""

import subprocess
import unittest

from scripts.lib.process_failure import failure_detail


class ProcessFailureTests(unittest.TestCase):
    def test_both_streams_are_labelled_with_stderr_before_stdout(self) -> None:
        result = subprocess.CompletedProcess(
            ["validate"], 7, stdout="\ncheck failed\nassertion details\n",
            stderr="  memory wait\nstill waiting\n",
        )
        self.assertEqual(
            failure_detail(result),
            "stderr: memory wait\nstill waiting\nstdout: check failed\nassertion details",
        )

    def test_an_empty_stream_is_omitted_without_losing_the_other_stream(self) -> None:
        for stdout, stderr, expected in (
            ("failed check\n", " \n", "stdout: failed check"),
            ("\n\t", "failed command\n", "stderr: failed command"),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                result = subprocess.CompletedProcess(["validate"], 3, stdout=stdout, stderr=stderr)
                self.assertEqual(failure_detail(result), expected)

    def test_no_output_reports_the_failed_exit_code(self) -> None:
        result = subprocess.CompletedProcess(["validate"], 17, stdout="\n", stderr=" ")
        self.assertEqual(failure_detail(result), "exit 17")

    def test_requested_tail_limits_each_stream_independently(self) -> None:
        result = subprocess.CompletedProcess(
            ["validate"], 1, stdout="old check\nfailed check\ncheck detail\n",
            stderr="old wait\nlatest wait\nwait detail\n",
        )
        self.assertEqual(
            failure_detail(result, lines=2),
            "stderr: latest wait\nwait detail\nstdout: failed check\ncheck detail",
        )

    def test_default_tail_retains_only_the_last_twenty_lines(self) -> None:
        output = "\n".join(f"check {number}" for number in range(25))
        result = subprocess.CompletedProcess(["validate"], 1, stdout=output, stderr="")
        self.assertEqual(
            failure_detail(result),
            "stdout: " + "\n".join(f"check {number}" for number in range(5, 25)),
        )


if __name__ == "__main__":
    _ = unittest.main()
