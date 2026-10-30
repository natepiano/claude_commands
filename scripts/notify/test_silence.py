#!/usr/bin/env python3
"""Tests for silence.py: on, off, status, the end timer and the Mac's ssh path, with no systemd or ssh."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock

import silence
from user_zone import user_zone

PUSH: silence.KeptPush = {"source": "build", "priority": "0", "title": "Build done",
                          "message": "No action needed.\nThe build passed.", "detail": "", "key": "", "text_id": ""}


class SilenceTests(unittest.TestCase):
    commands: list[list[str]]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        folder = self.enterContext(tempfile.TemporaryDirectory())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": folder})))
        self.commands = []

    def runner(self, argv: Sequence[str]) -> int:
        self.commands.append(list(argv))
        return 0

    def main(self, *argv: str) -> str:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(silence.main(list(argv), self.runner, "Linux"), 0)
        return output.getvalue()

    def test_on_without_an_end_lasts_until_off_and_makes_no_timer(self) -> None:
        self.assertIn("until off", self.main("on"))
        self.assertEqual(self.commands, [])
        self.assertIn("silence on from", self.main("status"))
        self.assertEqual(silence.keep(PUSH, "Linux"), silence.Kept("until off"))
        self.assertIn("1 push kept", self.main("status"))

    def test_off_clears_silence_and_posts_the_kept_pushes_oldest_first(self) -> None:
        _ = self.main("on")
        second: silence.KeptPush = {**PUSH, "title": "Tests done"}
        for push in (PUSH, second):
            _ = silence.keep(push, "Linux")
        posted: list[str] = []

        def deliver(push: silence.KeptPush) -> int:
            posted.append(push["title"])
            return 0

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(silence.off(self.runner, deliver), 0)
        self.assertEqual(output.getvalue(), "silence off; 2 kept pushes delivered\n")
        self.assertEqual(posted, ["Build done", "Tests done"])
        self.assertEqual(silence.keep(PUSH, "Linux"), silence.NotSilent())
        self.assertEqual(self.main("status"), "silence off\n")

    def test_a_failed_post_is_counted(self) -> None:
        _ = self.main("on")
        _ = silence.keep(PUSH, "Linux")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(silence.off(self.runner, lambda _push: 1), 1)
        self.assertIn("1 kept push delivered; 1 failed", output.getvalue())

    def test_off_retries_failed_and_interrupted_posts_without_reposting_delivered_pushes(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                _ = self.main("on")
                second: silence.KeptPush = {**PUSH, "title": "Tests done"}
                for push in (PUSH, second):
                    _ = silence.keep(push, "Linux")
                posted: list[str] = []

                def fail_second(push: silence.KeptPush) -> int:
                    if push == second:
                        if interrupted:
                            raise RuntimeError("stopped during release")
                        return 1
                    posted.append(push["title"])
                    return 0

                def deliver(push: silence.KeptPush) -> int:
                    posted.append(push["title"])
                    return 0

                with redirect_stdout(io.StringIO()):
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError, "stopped during release"):
                            _ = silence.off(self.runner, fail_second)
                    else:
                        self.assertEqual(silence.off(self.runner, fail_second), 1)
                    self.assertEqual(silence.read_kept(), [second])
                    self.assertEqual(silence.off(self.runner, deliver), 0)
                    self.assertEqual(silence.off(self.runner, deliver), 0)
                self.assertEqual(posted, [PUSH["title"], second["title"]])
                self.assertEqual(silence.read_kept(), [])

    def test_timer_off_retries_failed_and_interrupted_delivery_after_the_silence_record_is_gone(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                _ = self.main("on", "--for", "45min")
                record = silence.read_state()
                assert record is not None and record["timer"] is not None
                command = ["off", "--timer", record["timer"]]
                _ = silence.keep(PUSH, "Linux")
                posted: list[silence.KeptPush] = []

                def fail_delivery(push: silence.KeptPush) -> int:
                    self.assertEqual(push, PUSH)
                    if interrupted:
                        raise RuntimeError("stopped during timer release")
                    return 1

                def deliver(push: silence.KeptPush) -> int:
                    posted.append(push)
                    return 0

                with mock.patch.object(silence, "post_kept", side_effect=fail_delivery):
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError, "stopped during timer release"):
                            _ = self.main(*command)
                    else:
                        with redirect_stdout(io.StringIO()) as output:
                            self.assertEqual(silence.main(command, self.runner, "Linux"), 1)
                        self.assertIn("1 failed", output.getvalue())
                self.assertIsNone(silence.read_state())
                self.assertEqual(silence.read_kept(), [PUSH])
                with mock.patch.object(silence, "post_kept", side_effect=deliver):
                    self.assertIn("1 kept push delivered", self.main(*command))
                    self.assertIn("0 kept pushes delivered", self.main(*command))
                self.assertEqual(posted, [PUSH])
                self.assertEqual(silence.read_kept(), [])

    def kept_titles(self) -> list[str]:
        return [push["title"] for push in silence.read_kept()]

    def test_a_kept_push_replaces_the_older_one_with_its_source_and_key(self) -> None:
        _ = self.main("on")
        for title in ("Old note", "Other", "New note"):
            _ = silence.keep({**PUSH, "title": title, "key": "other" if title == "Other" else "subject"}, "Linux")
        self.assertEqual(self.kept_titles(), ["Other", "New note"])

    def test_an_empty_key_or_another_source_never_replaces(self) -> None:
        _ = self.main("on")
        pushes: list[silence.KeptPush] = [{**PUSH, "title": "First"}, {**PUSH, "title": "Second"},
                                          {**PUSH, "title": "Build", "key": "subject"},
                                          {**PUSH, "title": "Tests", "source": "tests", "key": "subject"}]
        for push in pushes:
            _ = silence.keep(push, "Linux")
        self.assertEqual(self.kept_titles(), ["First", "Second", "Build", "Tests"])

    def test_drop_removes_the_kept_pushes_with_its_source_and_key_and_says_how_many(self) -> None:
        _ = self.main("on")
        pushes: list[silence.KeptPush] = [{**PUSH, "title": "Alert", "source": "stall-watch", "key": "prompt"},
                                          {**PUSH, "title": "Other source", "key": "prompt"},
                                          {**PUSH, "title": "No key"}, {**PUSH, "title": "Other key", "source": "stall-watch",
                                                                        "key": "prompt-2"}]
        for push in pushes:
            _ = silence.keep(push, "Linux")
        self.assertEqual(silence.drop("stall-watch", "prompt", "Linux"), 1)
        self.assertEqual(self.kept_titles(), ["Other source", "No key", "Other key"])
        # An empty key drops nothing, as an empty key never replaces; a second drop finds nothing.
        self.assertEqual(silence.drop("build", "", "Linux"), 0)
        self.assertEqual(self.main("drop", "--source", "stall-watch", "--key", "prompt"), "0\n")
        self.assertEqual(self.main("drop", "--source", "stall-watch", "--key", "prompt-2"), "1\n")
        self.assertEqual(self.kept_titles(), ["Other source", "No key"])
        # Drop works with silence off too, and leaves silence as it was.
        self.assertIn("silence on", self.main("status"))

    def test_a_line_kept_before_pushes_had_a_key_or_text_id_reads_as_free_text_with_no_key(self) -> None:
        old = {name: value for name, value in PUSH.items() if name not in ("key", "text_id")}
        self.assertEqual(silence.parse_push(json.dumps(old)), PUSH)

    def test_a_kept_push_keeps_its_text_id(self) -> None:
        _ = self.main("on")
        _ = silence.keep({**PUSH, "text_id": "nixos.ups_battery"}, "Linux")
        self.assertEqual([push["text_id"] for push in silence.read_kept()], ["nixos.ups_battery"])

    def test_until_makes_a_timer_that_runs_off_and_a_new_end_stops_the_old_timer(self) -> None:
        end = (datetime.now() + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
        _ = self.main("on", "--until", f"{end:%Y-%m-%d %H:%M}")
        timer = self.commands[0]
        self.assertEqual(timer[:3], ["systemd-run", "--user", "--collect"])
        self.assertIn(f"--on-calendar={end.replace(tzinfo=user_zone()).astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC", timer)
        unit = timer[3].removeprefix("--unit=")
        self.assertEqual(timer[-4:], [str(silence.SILENCE), "off", "--timer", unit])
        self.assertTrue(unit.startswith("silence-off-"))
        output = self.main("on", "--for", "1h30min")
        self.assertIn("until", output)
        self.assertEqual(self.commands[-1], ["systemctl", "--user", "stop", f"{unit}.timer"])
        record = silence.read_state()
        assert record is not None and record["until"] is not None
        left = datetime.fromisoformat(record["until"]) - datetime.now().astimezone()
        self.assertAlmostEqual(left.total_seconds(), 5400, delta=60)

    def test_off_stops_the_end_timer(self) -> None:
        _ = self.main("on", "--for", "45min")
        unit = self.commands[0][3].removeprefix("--unit=")
        self.assertIn("0 kept pushes delivered", self.main("off"))
        self.assertEqual(self.commands[-1], ["systemctl", "--user", "stop", f"{unit}.timer"])

    def test_extending_silence_retries_stopping_the_old_timer_before_replacing_its_record(self) -> None:
        _ = self.main("on", "--for", "45min")
        old = silence.read_state()
        assert old is not None
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                with mock.patch.object(silence, "stop_timer", side_effect=(
                        RuntimeError("stopped before timer stop") if interrupted else None),
                        return_value=1), redirect_stderr(io.StringIO()) as errors:
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError, "stopped before timer stop"):
                            _ = self.main("on", "--for", "90min")
                    else:
                        self.assertEqual(silence.main(["on", "--for", "90min"], self.runner, "Linux"), 1)
                        self.assertIn("old end timer could not be stopped", errors.getvalue())
                self.assertEqual(silence.read_state(), old)
        _ = self.main("on", "--for", "90min")
        self.assertEqual(self.commands[-1], ["systemctl", "--user", "stop", f"{old['timer']}.timer"])
        newer = silence.read_state()
        assert newer is not None
        self.assertNotEqual(newer["timer"], old["timer"])

    def test_an_interrupted_extension_leaves_a_timer_that_cannot_end_the_replacement_silence(self) -> None:
        _ = self.main("on", "--for", "45min")
        old = silence.read_state()
        _ = silence.keep(PUSH, "Linux")
        with mock.patch.object(silence, "stop_timer", side_effect=RuntimeError("stopped at old timer stop")):
            with self.assertRaisesRegex(RuntimeError, "stopped at old timer stop"):
                _ = self.main("on", "--for", "90min")
        self.assertEqual(silence.read_state(), old)
        stray = self.commands[-1]
        _ = self.main("on", "--for", "90min")
        current = silence.read_state()
        assert current is not None
        timer = next(command for command in reversed(self.commands) if command[0] == "systemd-run")
        self.assertNotEqual(stray, timer)
        posted: list[silence.KeptPush] = []

        def deliver(push: silence.KeptPush) -> int:
            posted.append(push)
            return 0

        with mock.patch.object(silence, "post_kept", side_effect=deliver):
            commands_before = len(self.commands)
            self.assertEqual(self.main(*stray[stray.index(str(silence.SILENCE)) + 1:]), "")
            self.assertEqual(len(self.commands), commands_before)
            self.assertEqual(silence.read_state(), current)
            self.assertEqual(silence.read_kept(), [PUSH])
            self.assertEqual(posted, [])
            self.assertIn("1 kept push delivered", self.main(*timer[timer.index(str(silence.SILENCE)) + 1:]))
        self.assertIsNone(silence.read_state())
        self.assertEqual(silence.read_kept(), [])
        self.assertEqual(posted, [PUSH])

    def test_a_past_end_or_a_bad_span_is_refused(self) -> None:
        for argv in (["on", "--until", "2020-01-01 07:00"], ["on", "--for", "soon"], ["on", "--for", "1x"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _ = silence.main(argv, self.runner, "Linux")
        self.assertIsNone(silence.read_state())

    def test_a_failed_timer_leaves_silence_unchanged(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(silence.main(["on", "--for", "1h"], lambda _argv: 1, "Linux"), 1)
        self.assertIsNone(silence.read_state())

    def end_passed(self, count: int) -> None:
        past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
        _ = silence.state_file().write_text(json.dumps({"since": past, "until": past, "timer": None}))
        _ = silence.kept_file().write_text("".join(json.dumps({**PUSH, "title": f"push {index}"}) + "\n" for index in range(count)))

    def test_any_call_after_a_missed_end_ends_silence_and_posts_the_kept_pushes(self) -> None:
        self.end_passed(2)
        posted: list[str] = []

        def deliver(push: silence.KeptPush) -> int:
            posted.append(push["title"])
            return 0

        with mock.patch.object(silence, "post_kept", side_effect=deliver), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.main("status"), "silence off\n")
        self.assertEqual(posted, ["push 0", "push 1"])
        self.assertIn("silence ended at its end time; 2 kept pushes delivered", errors.getvalue())

    def test_two_callers_at_once_post_each_kept_push_once(self) -> None:
        self.end_passed(20)
        posted: list[str] = []
        start = threading.Barrier(4)

        def deliver(push: silence.KeptPush) -> int:
            posted.append(push["title"])
            return 0

        def end() -> None:
            _ = start.wait()
            _ = silence.end_if_past(self.runner, deliver)

        callers = [threading.Thread(target=end) for _ in range(4)]
        with redirect_stderr(io.StringIO()):
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()
        self.assertEqual(posted, [f"push {index}" for index in range(20)])

    def test_span_seconds(self) -> None:
        self.assertEqual(silence.span_seconds("1h30min"), 5400)
        self.assertEqual(silence.span_seconds("45 min"), 2700)
        self.assertEqual(silence.span_seconds("2 hours"), 7200)
        self.assertIsNone(silence.span_seconds("an hour"))


class MacTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "NOTIFY_STATE_DIR"}
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        _ = self.enterContext(mock.patch.object(silence, "state_dir", side_effect=AssertionError("Mac read local state")))

    def test_every_subcommand_runs_on_natedev(self) -> None:
        seen: list[list[str]] = []

        def remote(argv: Sequence[str]) -> int:
            seen.append(list(argv))
            return 0

        argv = ["on", "--until", "2026-10-21 07:00"]
        self.assertEqual(silence.main(argv, lambda _argv: 1, "Darwin", remote), 0)
        self.assertEqual(seen[0][:2], ["ssh", "-o"])
        self.assertEqual(seen[0][-2], "natedev")
        self.assertEqual(seen[0][-1], silence.REMOTE + " on --until '2026-10-21 07:00'")

    def test_a_push_asks_natedev_and_sends_when_natedev_cannot_answer(self) -> None:
        asked: list[tuple[list[str], str]] = []

        def asker(answer: tuple[int, str]) -> silence.Ask:
            def ask(argv: Sequence[str], text: str) -> tuple[int, str]:
                asked.append((list(argv), text))
                return answer
            return ask

        self.assertEqual(silence.keep(PUSH, "Darwin", asker((0, "until 07:00 EDT\n"))), silence.Kept("until 07:00 EDT"))
        self.assertEqual(asked[0][0][-1], silence.REMOTE + " keep")
        self.assertIn('"title": "Build done"', asked[0][1])
        _ = silence.keep({**PUSH, "key": "subject"}, "Darwin", asker((0, "until off\n")))
        self.assertIn('"key": "subject"', asked[1][1])
        self.assertEqual(silence.keep(PUSH, "Darwin", asker((1, ""))), silence.NotSilent())
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(silence.keep(PUSH, "Darwin", asker((255, ""))), silence.NotSilent())
        self.assertIn("natedev could not be asked", errors.getvalue())

    def test_a_drop_asks_natedev_and_drops_nothing_when_natedev_cannot_answer(self) -> None:
        asked: list[tuple[list[str], str]] = []

        def asker(answer: tuple[int, str]) -> silence.Ask:
            def ask(argv: Sequence[str], text: str) -> tuple[int, str]:
                asked.append((list(argv), text))
                return answer
            return ask

        self.assertEqual(silence.drop("stall-watch", "stall-watch:prompt:x y", "Darwin", asker((0, "2\n"))), 2)
        self.assertEqual(asked[0], ([*silence.SSH, silence.REMOTE + " drop --source stall-watch --key 'stall-watch:prompt:x y'"], ""))
        for answer in ((255, ""), (0, "not a number\n")):
            with self.subTest(answer=answer), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(silence.drop("stall-watch", "k", "Darwin", asker(answer)), 0)
            self.assertIn("natedev could not be asked to drop k", errors.getvalue())

    def test_natedev_keep_reads_the_push_on_stdin(self) -> None:
        with (tempfile.TemporaryDirectory() as folder, mock.patch.object(silence, "state_dir", return_value=Path(folder)),
              mock.patch("sys.stdin", io.StringIO(json.dumps(PUSH)))):
            self.assertEqual(silence.main(["keep"], lambda _argv: 0, "Linux"), 1)
            _ = (Path(folder) / "silence.json").write_text('{"since": "2026-10-20T22:00:00-04:00", "until": null, "timer": null}')
            with mock.patch("sys.stdin", io.StringIO('{"source": "b", "priority": "0", "title": "t", "message": "m", "detail": ""}')), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(silence.main(["keep"], lambda _argv: 0, "Linux"), 0)
            self.assertEqual(output.getvalue(), "until off\n")
            self.assertEqual(len(silence.read_kept()), 1)
            for title in ("old", "new"):
                with (mock.patch("sys.stdin", io.StringIO(json.dumps({**PUSH, "title": title, "key": "subject"}))),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(silence.main(["keep"], lambda _argv: 0, "Linux"), 0)
            self.assertEqual([push["title"] for push in silence.read_kept()], ["t", "new"])


if __name__ == "__main__":
    _ = unittest.main()
