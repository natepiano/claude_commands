#!/usr/bin/env python3
"""Tests for auto_silence.py: the idle length, the check, the return and the Mac's ssh path.

Every push goes to a recording stand-in, and no notifier, systemd or ssh command runs.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import os
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from typing import cast, override
from unittest import mock

from scripts.notify import auto_silence
from scripts.notify import silence

from scripts.lib.prompt_activity import UserTypedPrompt, PeerPrompt, ScriptPrompt, HarnessNotice, UnknownPrompt
from scripts.lib import relaunch
from scripts.lib import resume_message
from scripts.production import session_staff, staff
from scripts.shutdown import pane_record
from scripts.hooks import conversation_pause

PUSH: silence.KeptPush = {"source": "build", "priority": "0", "title": "Build done",
                          "message": "The build passed.", "detail": "", "key": "", "text_id": ""}
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
        registry = Path(folder) / "sessions"
        registry.mkdir()
        _ = (registry / "typed.json").write_text(json.dumps({"sessionId": "typed", "name": "typed-session"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)})))

    def runner(self, argv: Sequence[str]) -> int:
        self.commands.append(list(argv))
        return 0

    def test_historical_relaunch_sessions_keep_silence_and_send_no_return_notice(self) -> None:
        _ = self.main("on")
        self.now += auto_silence.DEFAULT_IDLE
        _ = self.main("check")
        before = silence.state_file().read_bytes()

        def historical_resume(cause: str, *,
                              move: resume_message.NoAccountMove | resume_message.AccountMove = resume_message.NO_ACCOUNT_MOVE,
                              workers: resume_message.ResumeWorkers = resume_message.NO_WORKERS,
                              context: str = "",
                              own_pass: resume_message.BusyPass | resume_message.NoOwnPass = resume_message.NO_OWN_PASS) -> str:
            self.assertEqual(cause, "you were stopped and resumed")
            return resume_message.resume_message("the herdr server restarted", move=move,
                                                  workers=workers, context=context, own_pass=own_pass)

        for name, identity in (("devtools", "33c5eac0-86d3-4b40-907c-8b24fc8831bd"),
                               ("models", "acae33e1-1a40-4d4e-af62-1697c067c1fc")):
            with self.subTest(session=name):
                entry = pane_record.launch("w1:p1", identity, "/fixture", {}, ["claude"])
                environment = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}
                with (mock.patch.object(pane_record, "matching_launch", return_value=entry),
                      mock.patch.object(session_staff, "record_membership", return_value=entry),
                      mock.patch.object(relaunch, "resume_message", historical_resume),
                      mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
                      mock.patch.dict(os.environ, {"CONVERSATION_PAUSE_STATE_DIR": str(silence.state_dir() / "prompts")}),
                      redirect_stderr(io.StringIO()) as errors):
                    arguments, _ = relaunch.restored_launch(["claude", "--resume", identity], environment)
                self.assertEqual(errors.getvalue(), "")
                prompt = arguments[-1]
                self.assertIn("You were stopped and resumed because the herdr server restarted.", prompt)
                source = conversation_pause.prompt_source(prompt, lambda: frozenset(), lambda: ())
                self.assertIs(source, conversation_pause.PromptSource.SCHEDULED)
                self.assertEqual(auto_silence.active(lambda: self.now, self.sender, self.runner,
                                                   self.deliver, ScriptPrompt(identity)), 0)
                self.assertEqual(silence.state_file().read_bytes(), before)
                self.assertFalse(any("silence.auto_off" in push for push in self.pushes))

    def test_bare_activity_has_unknown_source_and_keeps_silence(self) -> None:
        _ = self.main("on")
        self.now += auto_silence.DEFAULT_IDLE
        _ = self.main("check")
        before = auto_silence.config_file().read_bytes()
        self.assertEqual(auto_silence.main(["active"], self.runner, "Linux", sender=self.sender,
            deliver=self.deliver, clock=lambda: self.now), 0)
        self.assertIsNotNone(silence.read_state())
        self.assertEqual(auto_silence.config_file().read_bytes(), before)
        self.assertEqual(self.events()[-1]["prompt_source"], "UnknownPrompt")
        self.assertEqual(len(self.pushes), 1)

    def sender(self, argv: Sequence[str]) -> int:
        self.pushes.append(list(argv))
        return 0

    def deliver(self, push: silence.KeptPush) -> int:
        self.posted.append(push["title"])
        return 0

    def main(self, *argv: str, code: int = 0) -> str:
        if argv == ("active",):
            argv = ("active", "--source", "UserTypedPrompt", "--session-id", "typed")
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
                    self.assertEqual(auto_silence.active(lambda: self.now, sender, self.runner, deliver, UserTypedPrompt("typed")), 0)
                self.assertIsNone(silence.read_state())
                # The off push goes first: it says how many pushes come next, then they come.
                self.assertEqual(events, ["silence.auto_off", *(f"Build {index} done" for index in range(count))])
                self.assertEqual(self.push_values(-1), ("silence.auto_off", [f"held={held}", "sender=typed-session"]))
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
                    _ = auto_silence.active(lambda: self.now + 5, self.sender, self.runner, self.deliver, UserTypedPrompt("typed"))
            return self.sender(argv)

        with redirect_stdout(io.StringIO()):
            self.assertEqual(auto_silence.check(lambda: self.now, sender), 0)
        self.assertIsNone(silence.read_state())
        self.assertEqual([self.push_values(index)[0] for index in range(2)], ["silence.auto_on", "silence.auto_off"])
        self.assertEqual(self.push_values(1)[1], ["held=No pushes were held while you were away.", "sender=typed-session"])
        self.assertEqual(self.events()[-1]["event"], "cancelled_start")
        self.assertEqual(self.events()[-1]["cause"], "start_cancelled_by_user_prompt")

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

    def events(self) -> list[dict[str, object]]:
        path = silence.state_dir() / "auto-silence-events.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]

    def test_non_user_activity_keeps_silence_and_last_active(self) -> None:
        _ = self.main("45min")
        self.now += 3000
        _ = self.main("check")
        _ = silence.keep(PUSH, "Linux")
        before = auto_silence.config_file().read_bytes()
        for source in (PeerPrompt, ScriptPrompt, HarnessNotice, UnknownPrompt):
            with self.subTest(source=source.__name__):
                self.assertEqual(auto_silence.active(lambda: self.now, self.sender, self.runner,
                    self.deliver, source("typed")), 0)
                self.assertIsNotNone(silence.read_state())
                self.assertEqual(auto_silence.config_file().read_bytes(), before)
                self.assertEqual(len(self.pushes), 1)
                self.assertEqual(self.posted, [])
        self.assertEqual([event["event"] for event in self.events()], ["start", *["ignored_activity"] * 4])

    def test_typed_end_appends_its_session_before_the_notice_and_releases_once(self) -> None:
        _ = self.main("45min")
        self.now += 3000
        _ = self.main("check")
        start = self.events()[0]
        _ = silence.keep(PUSH, "Linux")

        def sender(argv: Sequence[str]) -> int:
            event = self.events()[-1]
            self.assertEqual((event["event"], event["cause"], event["session_id"]), ("end", "user_prompt", "typed"))
            self.assertEqual(event["silence_id"], start["silence_id"])
            return self.sender(argv)

        with redirect_stdout(io.StringIO()):
            for _ in range(2):
                self.assertEqual(auto_silence.active(lambda: self.now, sender, self.runner,
                    self.deliver, UserTypedPrompt("typed")), 0)
        self.assertEqual(self.posted, ["Build done"])
        self.assertEqual(sum(event["event"] == "end" for event in self.events()), 1)
        self.assertIn("sender=typed-session", self.push_values(-1)[1])

    def test_failed_event_append_keeps_silence_and_held_pushes(self) -> None:
        _ = self.main("45min")
        self.now += 3000
        _ = self.main("check")
        _ = silence.keep(PUSH, "Linux")
        state, kept = silence.state_file().read_bytes(), silence.kept_file().read_bytes()
        with (mock.patch.object(auto_silence, "append_event", side_effect=OSError("disk full")),
              redirect_stderr(io.StringIO())):
            self.assertEqual(auto_silence.active(lambda: self.now, self.sender, self.runner,
                self.deliver, UserTypedPrompt("typed")), 1)
        self.assertEqual(silence.state_file().read_bytes(), state)
        self.assertEqual(silence.kept_file().read_bytes(), kept)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.posted, [])

    def test_failed_start_event_append_still_preserves_silence(self) -> None:
        _ = self.main("45min")
        self.now += 3000
        with (mock.patch.object(auto_silence, "append_event", side_effect=OSError("disk full")),
              redirect_stderr(io.StringIO())):
            self.assertEqual(auto_silence.check(lambda: self.now, self.sender), 1)
        self.assertIsNotNone(silence.read_state())

    def test_failed_release_records_the_result_and_keeps_the_failed_push(self) -> None:
        _ = self.main("45min")
        self.now += 3000
        _ = self.main("check")
        _ = silence.keep(PUSH, "Linux")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(auto_silence.active(lambda: self.now, self.sender, self.runner,
                lambda _push: 1, UserTypedPrompt("typed")), 1)
        self.assertIsNone(silence.read_state())
        self.assertEqual(silence.read_kept(), [PUSH])
        event = self.events()[-1]
        self.assertEqual((event["event"], event["result"], event["cause"]),
                         ("release_failed", "release_failed", "held_push_release"))
        self.assertEqual(event["silence_id"], self.events()[0]["silence_id"])

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
