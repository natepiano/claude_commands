#!/usr/bin/env python3
"""Tests for auto_silence.py: the idle length, the check, the return and the Mac's ssh path.

Every push goes to a recording stand-in, and no notifier, systemd or ssh command runs.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from typing import cast, override
from unittest import mock

import auto_silence
import silence

PUSH: silence.KeptPush = {"source": "build", "priority": "0", "title": "Build done",
                          "message": "The build passed.", "detail": "", "key": ""}
START = 1_000_000.0


class AutoSilenceTests(unittest.TestCase):
    commands: list[list[str]]  # pyright: ignore[reportUninitializedInstanceVariable]
    pushes: list[list[str]]  # pyright: ignore[reportUninitializedInstanceVariable]
    posted: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    now: float  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        folder = self.enterContext(tempfile.TemporaryDirectory())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": folder})))
        self.commands, self.pushes, self.posted, self.now = [], [], [], START

    def runner(self, argv: Sequence[str]) -> int:
        self.commands.append(list(argv))
        return 0

    def sender(self, argv: Sequence[str]) -> int:
        self.pushes.append(list(argv))
        return 0

    def deliver(self, push: silence.KeptPush) -> int:
        self.posted.append(push["title"])
        return 0

    def main(self, *argv: str, code: int = 0) -> str:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(auto_silence.main(list(argv), self.runner, "Linux", sender=self.sender,
                                               deliver=self.deliver, clock=lambda: self.now), code)
        return output.getvalue()

    def push_values(self, index: int) -> tuple[str, list[str]]:
        argv = self.pushes[index]
        values = [argv[position + 1] for position, word in enumerate(argv) if word == "--value"]
        return argv[argv.index("--text-id") + 1], values

    def test_off_until_set_and_a_check_does_nothing(self) -> None:
        self.assertEqual(self.main(), "auto silence off\n")
        self.now += 86400
        _ = self.main("check")
        self.assertEqual((self.pushes, silence.read_state()), ([], None))

    def test_a_length_makes_the_minute_job_and_shows(self) -> None:
        self.assertEqual(self.main("45min"), "auto silence after 45 min idle\n")
        job = self.commands[0]
        self.assertEqual(job[2:6], ["new", "auto-silence", "--every", "1"])
        self.assertTrue(job[-1].endswith("auto_silence.py check"))
        self.now += 600
        self.assertIn("auto silence after 45 min idle; last prompt ", self.main())
        self.assertIn("10 min ago", self.main())
        _ = self.main("soon", code=2)

    def test_on_sets_the_two_hour_default(self) -> None:
        self.assertEqual(auto_silence.DEFAULT_IDLE, 2 * 3600)
        self.assertEqual(self.main("on"), "auto silence after 120 min idle\n")
        self.assertEqual(self.commands[0][2:4], ["new", "auto-silence"])
        self.assertEqual(auto_silence.read_config()["idle_seconds"], auto_silence.DEFAULT_IDLE)
        self.now += 119 * 60
        _ = self.main("check")
        self.assertEqual(self.pushes, [])
        self.now += 60
        _ = self.main("check")
        self.assertEqual(self.push_values(0), ("silence.auto_on", ["minutes=120", "sender=natedev"]))

    def test_idle_tells_the_user_first_then_silences_once(self) -> None:
        _ = self.main("45min")
        self.now += 44 * 60
        _ = self.main("check")
        self.assertEqual(self.pushes, [])

        def sender(argv: Sequence[str]) -> int:
            self.assertIsNone(silence.read_state(), "the push must go out before silence starts")
            return self.sender(argv)

        self.now += 60
        with redirect_stdout(io.StringIO()):
            self.assertEqual(auto_silence.check(lambda: self.now, sender), 0)
        self.assertEqual(self.push_values(0), ("silence.auto_on", ["minutes=45", "sender=natedev"]))
        record = silence.read_state()
        self.assertIsNotNone(record)
        self.assertTrue(record and record["auto"])
        self.assertEqual(silence.keep(PUSH, "Linux"), silence.Kept("until you are back"))
        self.now += 3600
        _ = self.main("check")
        self.assertEqual(len(self.pushes), 1)

    def test_the_next_prompt_ends_an_auto_silence_and_says_what_comes_before_it_delivers_what_was_kept(self) -> None:
        _ = self.main("45min")
        for count, held in ((0, "No pushes were held while you were away."),
                            (1, "The push held while you were away comes next."),
                            (3, "The 3 pushes held while you were away come next.")):
            with self.subTest(count=count):
                self.pushes.clear()
                events: list[str] = []

                def sender(argv: Sequence[str]) -> int:
                    events.append(argv[argv.index("--text-id") + 1])
                    return self.sender(argv)

                def deliver(push: silence.KeptPush) -> int:
                    events.append(push["title"])
                    return self.deliver(push)

                self.now += 50 * 60
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(auto_silence.check(lambda: self.now, sender), 0)
                for index in range(count):
                    _ = silence.keep({**PUSH, "title": f"Build {index} done"}, "Linux")
                events.clear()
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(auto_silence.active(lambda: self.now, sender, self.runner, deliver), 0)
                self.assertIsNone(silence.read_state())
                # The off push goes first: it says how many pushes come next, then they come.
                self.assertEqual(events, ["silence.auto_off", *(f"Build {index} done" for index in range(count))])
                self.assertEqual(self.push_values(-1), ("silence.auto_off", [f"held={held}", "sender=natedev"]))
        self.pushes.clear()
        _ = self.main("active")
        self.assertEqual(self.pushes, [])

    def test_a_silence_set_by_hand_is_never_ended_or_replaced(self) -> None:
        _ = self.main("45min")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(silence.on(None, self.runner), 0)
        self.now += 50 * 60
        _ = self.main("check")
        _ = self.main("active")
        record = silence.read_state()
        self.assertTrue(record is not None and not record["auto"])
        self.assertEqual(self.pushes, [])

    def test_on_by_hand_turns_an_auto_silence_into_a_hand_silence(self) -> None:
        _ = self.main("45min")
        self.now += 50 * 60
        _ = self.main("check")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(silence.on(None, self.runner), 0)
        _ = self.main("active")
        record = silence.read_state()
        self.assertTrue(record is not None and not record["auto"])

    def test_a_prompt_during_the_on_push_cancels_the_silence_and_says_so(self) -> None:
        _ = self.main("45min")
        self.now += 50 * 60

        def sender(argv: Sequence[str]) -> int:
            if not self.pushes:
                with redirect_stdout(io.StringIO()):
                    _ = auto_silence.active(lambda: self.now + 5, self.sender, self.runner, self.deliver)
            return self.sender(argv)

        with redirect_stdout(io.StringIO()):
            self.assertEqual(auto_silence.check(lambda: self.now, sender), 0)
        self.assertIsNone(silence.read_state())
        self.assertEqual([self.push_values(index)[0] for index in range(2)], ["silence.auto_on", "silence.auto_off"])
        self.assertEqual(self.push_values(1)[1], ["held=No pushes were held while you were away.", "sender=natedev"])

    def test_off_removes_the_job_and_ends_an_auto_silence(self) -> None:
        _ = self.main("45min")
        self.now += 50 * 60
        _ = self.main("check")
        self.commands.clear()
        self.assertIn("auto silence off", self.main("off"))
        self.assertEqual(self.commands[0][2:], ["remove", "auto-silence"])
        self.assertIsNone(silence.read_state())
        self.assertEqual(self.push_values(1)[0], "silence.auto_off")
        self.now += 86400
        _ = self.main("check")
        self.assertEqual(len(self.pushes), 2)

    def test_the_mac_asks_natedev_over_ssh(self) -> None:
        calls: list[list[str]] = []

        def remote(argv: Sequence[str]) -> int:
            calls.append(list(argv))
            return 0

        with mock.patch.dict(os.environ, {}, clear=False):
            del os.environ["NOTIFY_STATE_DIR"]
            self.assertEqual(auto_silence.main(["active"], self.runner, "Darwin", remote, self.sender), 0)
        self.assertEqual(calls[0][:-1], silence.SSH)
        self.assertTrue(calls[0][-1].endswith('auto_silence.py" active'))
        self.assertEqual((self.commands, self.pushes), ([], []))


if __name__ == "__main__":
    _ = unittest.main()
