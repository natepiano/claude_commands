"""send_later builds one transient timer that runs send.py, here or on natedev over ssh."""
from __future__ import annotations

import unittest
from collections.abc import Sequence

import send_later

SEND_ARGS = ["--to", "user", "--value", "description=it is ready"]


class SendLaterTests(unittest.TestCase):
    def capture(self, argv: list[str], system: str) -> list[str]:
        seen: list[list[str]] = []

        def runner(command: Sequence[str]) -> int:
            seen.append(list(command))
            return 0

        self.assertEqual(send_later.main(argv, runner, system), 0)
        return seen[0]

    def test_a_span_runs_send_once_after_it_on_linux(self) -> None:
        command = self.capture(["--in", "10min", "--", *SEND_ARGS], "Linux")
        self.assertEqual(command[:3], ["systemd-run", "--user", "--collect"])
        self.assertIn("--on-active=10min", command)
        self.assertEqual(command[-len(SEND_ARGS):], SEND_ARGS)
        self.assertEqual(command[-len(SEND_ARGS) - 1], str(send_later.SEND))

    def test_a_time_uses_a_calendar_timer(self) -> None:
        command = self.capture(["--at", "2026-10-20 15:30", "--", *SEND_ARGS], "Linux")
        self.assertIn("--on-calendar=2026-10-20 15:30", command)

    def test_the_mac_makes_the_timer_on_natedev(self) -> None:
        command = self.capture(["--in", "1h", "--", *SEND_ARGS], "Darwin")
        self.assertEqual(command[:2], ["ssh", "natedev"])
        self.assertIn("$HOME/.claude/scripts/message/send.py", command[2])
        self.assertIn("'description=it is ready'", command[2])

    def test_run_silence_starts_silence_later(self) -> None:
        silence_args = ["on", "--until", "2026-10-21 07:00"]
        command = self.capture(["--at", "2026-10-20 22:00", "--run", "silence", "--", *silence_args], "Linux")
        self.assertIn("--on-calendar=2026-10-20 22:00", command)
        self.assertTrue(any(part.startswith("--unit=silence-later-") for part in command))
        self.assertEqual(command[-len(silence_args) - 1:], [str(send_later.SCRIPTS["silence"]), *silence_args])
        command = self.capture(["--in", "1h", "--run", "silence", "--", "on"], "Darwin")
        self.assertIn("$HOME/.claude/scripts/notify/silence.py on", command[2])

    def test_no_send_arguments_refuses(self) -> None:
        with self.assertRaises(SystemExit):
            _ = send_later.main(["--in", "1h"], lambda _command: 0, "Linux")


if __name__ == "__main__":
    _ = unittest.main()
