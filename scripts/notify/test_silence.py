#!/usr/bin/env python3
"""Tests for silence.py: on, off, status, the end timer and the Mac's ssh path, with no systemd or ssh."""

from __future__ import annotations

from scripts.lib.checkout_test_support import python_tool_source

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.notify import push_texts, silence
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.user_zone import user_zone

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
        self.assertEqual(silence.keep_here(PUSH), silence.Kept("until off"))
        self.assertIn("1 push kept", self.main("status"))

    def test_off_clears_silence_and_posts_the_kept_pushes_oldest_first(self) -> None:
        _ = self.main("on")
        second: silence.KeptPush = {**PUSH, "title": "Tests done"}
        for push in (PUSH, second):
            _ = silence.keep_here(push)
        posted: list[str] = []

        def deliver(push: silence.KeptPush) -> int:
            posted.append(push["title"])
            return 0

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(silence.off(self.runner, deliver), 0)
        self.assertEqual(output.getvalue(), "silence off; 2 kept pushes delivered\n")
        self.assertEqual(posted, ["Build done", "Tests done"])
        self.assertEqual(silence.keep_here(PUSH), silence.NotSilent())
        self.assertEqual(self.main("status"), "silence off\n")

    def test_nightly_result_without_herdr_keeps_only_the_newest_result_until_silence_ends(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = Path(__file__).resolve().parents[2]
        _ = shutil.copytree(checkout / "config", root / "config")
        keys = root / "keys"
        _ = keys.write_text("PUSHOVER_USER=fixture\nPUSHOVER_TOKEN=fixture\n")
        posted = root / "posted.jsonl"
        channel = root / "channel.py"
        _ = channel.write_text(python_tool_source('''import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs
from scripts.notify import pushover, push_texts, silence

def post(url, data):
    with Path(os.environ["FIXTURE_POSTED"]).open("a") as output:
        output.write(json.dumps(parse_qs(data.decode())) + "\\n")
    return 200, b'{"status":1}'

keys = Path(os.environ["FIXTURE_KEYS"])
push_texts.approval_date = lambda text_id: "2026-11-06"
if sys.argv[1:] == ["release"]:
    raise SystemExit(silence.off(lambda command: 0, lambda push: pushover.post_kept(push, keys, post)))
raise SystemExit(pushover.send(sys.argv[1:], keys, post))
'''))
        environment = {
            "HOME": str(root), "PATH": os.environ["PATH"], "HERDR_BIN": "/nonexistent/herdr",
            "PYTHONPATH": str(checkout / "scripts/lib/python_startup"),
            "CONF_DIR": str(root / "config"), "NOTIFY_STATE_DIR": str(root / "notify"),
            "XDG_STATE_HOME": str(root / "state"), "USER_ZONE": "America/New_York",
            "FIXTURE_KEYS": str(keys), "FIXTURE_POSTED": str(posted),
        }
        bootstrap = ("import sys; from pathlib import Path; from scripts.message import send; "
                     "send.USER_CHANNEL = Path(sys.argv[1]); raise SystemExit(send.main(sys.argv[2:]))")
        with mock.patch.dict(os.environ, environment, clear=True):
            _ = self.main("on")
            results = tuple(push_texts.render("nightly.result", branch="main", time="03:53 EST",
                outcome=outcome, types="passed", tests="passed" if outcome == "passed" else "failed in message/test_send.py")
                for outcome in ("passed", "failed"))
            for rendered in results:
                command = push_texts.user_send_argv(rendered, key="nightly-result")
                result = subprocess.run([command[0], "-c", bootstrap, str(channel), *command[2:]],
                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("KEPT:", result.stdout)
                self.assertFalse(posted.exists())
                kept, = silence.read_kept()
                self.assertEqual((kept["title"], kept["message"], kept["key"]),
                                 (rendered.title, rendered.message, "nightly-result"))
            released = subprocess.run([sys.executable, str(channel), "release"], capture_output=True,
                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
            delivery, = [cast(dict[str, list[str]], json.loads(line)) for line in posted.read_text().splitlines()]
            self.assertEqual(delivery["title"], [results[-1].title])
            self.assertTrue(delivery["message"][0].startswith(results[-1].message + "\n"))
            self.assertEqual(silence.read_kept(), [])
            self.assertIsNone(silence.read_state())

    def test_a_failed_post_is_counted(self) -> None:
        _ = self.main("on")
        _ = silence.keep_here(PUSH)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(silence.off(self.runner, lambda _push: 1), 1)
        self.assertIn("0 kept pushes delivered; 1 failed", output.getvalue())

    def test_manual_off_logs_an_automatic_end_before_releasing_pushes(self) -> None:
        record: silence.SilenceRecord = {"since": "2026-11-03T06:00:00+00:00", "until": None,
                                         "timer": None, "auto": True}
        with silence.locked():
            silence.write_state(record)
        _ = silence.keep_here(PUSH)
        posted: list[silence.KeptPush] = []

        def deliver(push: silence.KeptPush) -> int:
            path = silence.state_dir() / "auto-silence-events.jsonl"
            event = cast(dict[str, object], json.loads(path.read_text().splitlines()[0]))
            self.assertEqual((event["event"], event["cause"], event["silence_id"]),
                             ("end", "manual_off", record["since"]))
            self.assertIsNone(silence.read_state())
            posted.append(push)
            return 0

        with mock.patch.object(silence, "post_kept", side_effect=deliver):
            _ = self.main("off")
        self.assertEqual(posted, [PUSH])
        self.assertEqual(silence.read_kept(), [])

    def test_manual_off_keeps_automatic_silence_when_the_end_log_fails(self) -> None:
        from scripts.notify import auto_silence

        record: silence.SilenceRecord = {"since": "2026-11-03T06:00:00+00:00", "until": None,
                                         "timer": None, "auto": True}
        with silence.locked():
            silence.write_state(record)
        _ = silence.keep_here(PUSH)
        state, kept = silence.state_file().read_bytes(), silence.kept_file().read_bytes()
        with (mock.patch.object(auto_silence, "append_event", side_effect=OSError("disk full")),
              mock.patch.object(silence, "post_kept") as deliver,
              redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
            self.assertEqual(silence.main(["off"], self.runner, "Linux"), 1)
        deliver.assert_not_called()
        self.assertEqual(silence.state_file().read_bytes(), state)
        self.assertEqual(silence.kept_file().read_bytes(), kept)

    def test_manual_on_preserves_automatic_silence_when_its_end_log_fails(self) -> None:
        from scripts.notify import auto_silence

        record: silence.SilenceRecord = {"since": "2026-11-03T06:00:00+00:00", "until": None,
                                         "timer": None, "auto": True}
        with silence.locked():
            silence.write_state(record)
        with (mock.patch.object(auto_silence, "append_event", side_effect=OSError("disk full")),
              redirect_stderr(io.StringIO())):
            self.assertEqual(silence.on(None, self.runner), 1)
        self.assertEqual(silence.read_state(), record)

    def test_off_retries_failed_and_interrupted_posts_without_reposting_delivered_pushes(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                _ = self.main("on")
                second: silence.KeptPush = {**PUSH, "title": "Tests done"}
                for push in (PUSH, second):
                    _ = silence.keep_here(push)
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
                _ = silence.keep_here(PUSH)
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
            _ = silence.keep_here({**PUSH, "title": title, "key": "other" if title == "Other" else "subject"})
        self.assertEqual(self.kept_titles(), ["Other", "New note"])

    def test_an_empty_key_or_another_source_never_replaces(self) -> None:
        _ = self.main("on")
        pushes: list[silence.KeptPush] = [{**PUSH, "title": "First"}, {**PUSH, "title": "Second"},
                                          {**PUSH, "title": "Build", "key": "subject"},
                                          {**PUSH, "title": "Tests", "source": "tests", "key": "subject"}]
        for push in pushes:
            _ = silence.keep_here(push)
        self.assertEqual(self.kept_titles(), ["First", "Second", "Build", "Tests"])

    def test_drop_removes_the_kept_pushes_with_its_source_and_key_and_says_how_many(self) -> None:
        _ = self.main("on")
        pushes: list[silence.KeptPush] = [{**PUSH, "title": "Alert", "source": "stall-watch", "key": "prompt"},
                                          {**PUSH, "title": "Other source", "key": "prompt"},
                                          {**PUSH, "title": "No key"}, {**PUSH, "title": "Other key", "source": "stall-watch",
                                                                        "key": "prompt-2"}]
        for push in pushes:
            _ = silence.keep_here(push)
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
        _ = silence.keep_here({**PUSH, "text_id": "nixos.ups_battery"})
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
        _ = silence.keep_here(PUSH)
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


if __name__ == "__main__":
    _ = unittest.main()
