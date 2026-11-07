"""The agents command shows only its public commands."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from scripts.shutdown import shutdown


class AgentsUsageTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = shutdown.main(arguments)
            except SystemExit as error:
                self.assertIsInstance(error.code, int)
                status = int(str(error.code))
        return status, output.getvalue(), errors.getvalue()

    def test_no_arguments_print_the_help_usage_and_exit_zero(self) -> None:
        empty = self.invoke([])
        self.assertEqual(empty[0], 0)
        self.assertEqual(empty, self.invoke(["help"]))
        self.assertTrue(empty[1].startswith("usage: agents"))
        self.assertEqual(empty[2], "")

    def test_help_lists_only_public_commands_with_their_arguments(self) -> None:
        status, output, errors = self.invoke(["help"])
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual([line.split()[1] for line in output.splitlines()[1:]],
                         ["help", "status", "resume", "restart", "stop"])
        self.assertIn("resume NAME", output)
        self.assertIn("stop SESSION", output)
        self.assertIn("[ACCOUNT]", output)
        self.assertIn("--dry-run", output)
        for command in ("records", "held-sessions", "remote-control"):
            self.assertNotIn(command, output)

    def test_help_option_prints_the_same_public_usage(self) -> None:
        option = self.invoke(["--help"])
        self.assertEqual(option[0], 0)
        self.assertTrue(option[1].startswith("usage: agents"))
        self.assertEqual(option, self.invoke(["help"]))

    def test_unknown_command_prints_usage_and_exits_two(self) -> None:
        status, output, errors = self.invoke(["unknown-command"])
        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        usage = self.invoke(["help"])[1]
        self.assertTrue(usage.startswith("usage: agents"))
        self.assertIn(usage, errors)


if __name__ == "__main__":
    _ = unittest.main()
