"""Record-driven restart delivery and read-only status behavior."""

from __future__ import annotations

import unit_staff_test_support
from scripts.production import staff_sources
import io
import os
import subprocess
import tempfile
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast, final, override
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_support import ShutdownSendLeaf, ShutdownTestCase, empty_session_registry
import herdr
import shutdown_record
import sessions
from user_action import ActionRequired
from push_texts import RenderedPush, render, catalog
import restart as restart_work
import restart_support as settle
if TYPE_CHECKING:
    from ..notify import review
else:
    import review
# The module settle imports by its own path entry: a separate object from the package import below.
import showrunners as settle_showrunners
from scripts.production import showrunners


# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300

LOGIN = "owner@example.com"
LABEL = "claude2"
NOW = "2026-10-09T21:49:10+00:00"
NOW_UTC = datetime(2026, 10, 9, 21, 49, 10, tzinfo=timezone.utc)


def setUpModule() -> None:
    _ = unittest.enterModuleContext(patch.object(herdr, "security_session", return_value=herdr.NotMacSession()))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))
    _ = unittest.enterModuleContext(patch.object(settle_showrunners, "NOTIFIER_STATE_DIR", root))


class SentCall(TypedDict):
    args: list[str]
    text: str


from test_restart import top_level, unit, entry as session_shutdown


@final
class RestartSupportTests(ShutdownTestCase):
    root: Path = Path()


    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(sessions, "remote_control", return_value="active"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        context = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.root / "state"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        del context
        empty_session_registry(self, self.root / "sessions")
        _ = self.enterContext(patch.object(settle, "now_utc", return_value=NOW_UTC))
        _ = self.enterContext(patch.object(settle, "merge_in_progress", return_value=False))
        _ = self.enterContext(patch.object(settle, "_showing_form", return_value=False))
        # Fixture folders do not exist; a restore whose folder is gone is dropped before launch.
        _ = self.enterContext(patch.object(restart_work, "_folder_exists", return_value=True))


    def save(self, saved: shutdown_record.SessionShutdown) -> None:
        self.assertTrue(shutdown_record.create_session(saved))


    def test_status_trail_prints_steps_in_user_zone_in_order(self) -> None:
        saved = session_shutdown(top_level("trail-owner", name="Trail owner"))
        saved["trail"] = [shutdown_record.ProgressStep(at=NOW, kind="waiting", detail="pause sent"),
            shutdown_record.ProgressStep(at="2026-10-09T21:50:10+00:00", kind="reaping", detail="weekly limit")]
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            lines = settle.status_record_lines(saved)
        self.assertEqual([line.strip() for line in lines[1:]],
            ["17:49:10 waiting pause sent", "17:50:10 reaping weekly limit"])


    def test_unit_screen_refuses_a_registry_record_with_a_reused_pid(self) -> None:
        owner = unit("original")
        registered = sessions.SessionRecord(pid=owner["pid"], sessionId="different", name="Different",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with patch.object(sessions, "read_session", return_value=registered), \
             patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("other", "w1:p1")) as place, \
             patch("session_pane.read_place", return_value="other conversation") as read:
            screen = settle._screen(owner)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(screen, settle.ScreenUnreadable)
            place.assert_not_called()
            read.assert_not_called()
            registered["sessionId"] = owner["session_id"]
            self.assertEqual(settle._screen(owner), settle.ScreenText("other conversation"))  # pyright: ignore[reportPrivateUsage]
            read.assert_called_once_with(herdr.HerdrPlace("other", "w1:p1"), 60)


    def unit_for_showrunner(self, then: shutdown_record.ShutdownContinuation = "restart") -> shutdown_record.SessionShutdown:
        owner = unit("unit")
        owner["name"] = "Unit"
        unit_staff_test_support.PARENTS[owner["host"]["unit"]] = staff_sources.UnitProduction("demo", "/tmp/demo-production.md")
        saved = session_shutdown(owner, state={"kind": "ready", "at": NOW}, then=then)
        saved["target"] = "claude3"
        saved["why"] = "claude2 is out of usage"
        self.save(saved)
        return saved


    @staticmethod
    def report_legacy_stopping(saved: shutdown_record.SessionShutdown) -> None:
        # Replay an old record's queued line through the surviving delivery owner.
        _ = settle.send_showrunner_line(saved, settle.SHOWRUNNER_TOLD_STOPPING,
            "fixture-runner", "Unit is stopping: claude2 is out of usage",
            "Unit is stopping: claude2 is out of usage")

    @staticmethod
    def sent(send: Mock) -> tuple[str, str, str]:
        return cast(tuple[str, str, str], send.call_args.args)


    def test_a_move_reported_after_a_later_stop_and_forget_does_not_say_the_unit_is_back(self) -> None:
        saved = self.unit_for_showrunner()
        def restarted(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarted", "at": NOW}
            current["showrunner_told"] = settle.SHOWRUNNER_TOLD_STOPPING
        back = shutdown_record.update_session(LOGIN, "unit", restarted)
        shutdown_record.archive_session(LOGIN, "unit")
        stop = session_shutdown(saved["session"], state={"kind": "stopped", "at": NOW}, request_id="request-two")
        stop["requested_at"] = (NOW_UTC + timedelta(minutes=10)).isoformat()
        self.save(stop)
        self.assertIsInstance(shutdown_record.forget_session(LOGIN, "unit", "natedev"), shutdown_record.SessionForgotten)
        with (
            patch.object(settle_showrunners, "current_name", return_value="runner"),
            patch.object(settle, "send_message", return_value={"kind": "sent"}) as send,
        ):
            settle.tell_showrunners_outcome([back])
        send.assert_not_called()


    def test_failed_restart_showrunner_outcome_checks_liveness_and_sends_once(self) -> None:
        saved = self.unit_for_showrunner()
        cases = (restart_work.SessionLive(), restart_work.SessionNotLive(),
                 restart_work.SessionLivenessUnknown("scratch registry unreadable"))
        for observed in cases:
            with self.subTest(liveness=observed):
                def failed(current: shutdown_record.SessionShutdown) -> None:
                    current["progress"] = {"kind": "restart failed", "at": NOW,
                                           "reason": "restart prompt not submitted"}
                    current["showrunner_told"] = settle.SHOWRUNNER_TOLD_STOPPING
                saved = shutdown_record.update_session(LOGIN, "unit", failed)
                with (patch.object(restart_work, "restart_failed_liveness", return_value=observed) as liveness,
                      patch.object(settle_showrunners, "current_name", return_value="runner"),
                      patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send):
                    settle.tell_showrunners_outcome([saved])
                    liveness.assert_called_once_with(saved)
                    current = shutdown_record.find_session(LOGIN, "unit")
                    assert current is not None
                    settle.tell_showrunners_outcome([current])
                    self.assertEqual(liveness.call_count, 2)
                send.assert_called_once()
                recipient, summary, text = self.sent(send)
                self.assertEqual(recipient, "runner")
                if isinstance(observed, restart_work.SessionLive):
                    self.assertEqual(summary,
                        "Unit is back on claude3; its restart prompt was not confirmed — check it, " +
                        "then close its record in a terminal on natedev: " + settle.shutdown_command("forget", "unit", "--account", LOGIN))
                    self.assertEqual(text, summary)
                    self.assertNotIn("shutdown.py restart", text)
                    self.assertIn(shutdown_record.ShowrunnerLineDelivered(kind="delivered",
                        line=settle.SHOWRUNNER_TOLD_BACK, text=text), current.get("showrunner_lines", []))
                else:
                    self.assertIn("Unit did not come back: restart prompt not submitted", summary)
                    self.assertIn(settle.shutdown_command("restart", LOGIN, "--here", "--only", "unit"), text)
                    self.assertIn(shutdown_record.ShowrunnerLineDelivered(kind="delivered",
                        line=settle.not_back_text(saved), text=text), current.get("showrunner_lines", []))
                    if isinstance(observed, restart_work.SessionLivenessUnknown):
                        self.assertIn("liveness unknown: scratch registry unreadable", summary)


    def test_stop_report_passes_live_failed_unit_to_showrunner_with_one_liveness_read(self) -> None:
        _ = self.unit_for_showrunner()
        def failed(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restart failed", "at": NOW,
                                   "reason": "restart prompt not submitted"}
            current["showrunner_told"] = settle.SHOWRUNNER_TOLD_STOPPING
        saved = shutdown_record.update_session(LOGIN, "unit", failed)
        with (patch.object(restart_work, "clear_running_on_target",
                           return_value=restart_work.TargetCheck([saved], [])),
              patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionLive()) as liveness,
              patch.object(settle_showrunners, "current_name", return_value="runner"),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send,
              patch.object(settle, "alert_user", return_value=True) as alert):
            settle.report_stop([saved])
        liveness.assert_called_once_with(saved)
        back = [call for call in send.call_args_list if call.args[0] == "runner"]
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].args[1],
                         "Unit is back on claude3; its restart prompt was not confirmed — check it, " +
                         "then close its record in a terminal on natedev: " + settle.shutdown_command("forget", "unit", "--account", LOGIN))
        self.assertNotIn("shutdown.py restart", cast(str, back[0].args[2]))
        current = shutdown_record.find_session(LOGIN, "unit")
        assert current is not None
        self.assertIn(shutdown_record.ShowrunnerLineDelivered(kind="delivered",
            line=settle.SHOWRUNNER_TOLD_BACK, text=cast(str, back[0].args[2])), current.get("showrunner_lines", []))
        alert.assert_not_called()


    def test_session_count_text_names_one_and_many_sessions(self) -> None:
        self.assertEqual(settle.stopped_sessions_text(1), "One session")
        self.assertEqual(settle.stopped_sessions_text(14), "14 sessions")


    def test_stop_report_shows_saved_submit_screen_for_live_and_absent_sessions(self) -> None:
        saved = session_shutdown(top_level("screen-owner", name="Screen owner"), then="restart",
            state={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
        saved["alerts"] = "none"
        self.save(saved)
        for live in (True, False):
            for screen in ("Claude fixture\n❯ unsent opening prompt", herdr.PromptScreenNotRead()):
                with self.subTest(live=live, screen=screen):
                    restart_work._save_prompt_outcome(saved, herdr.PromptNotSubmitted("not submitted", screen))  # pyright: ignore[reportPrivateUsage]
                    with (patch.object(restart_work, "clear_running_on_target", return_value=restart_work.TargetCheck(remaining=[saved], finished=[])),
                          patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionLive() if live else restart_work.SessionNotLive()),
                          patch.object(settle, "run_remote", return_value=(0, "[]")),
                          patch.object(settle, "tell_showrunners_outcome"),
                          patch.object(settle, "alert_user") as alert,
                          redirect_stdout(io.StringIO()) as output):
                        settle.report_stop([saved])
                    text = output.getvalue()
                    if isinstance(screen, str):
                        line = settle.running_restart_line(saved) if live else "Screen owner: restart prompt not submitted"
                        self.assertIn("  " + line + "\n  last screen of the failed prompt submit:\n    Claude fixture\n    ❯ unsent opening prompt\n", text)
                    else:
                        self.assertNotIn("last screen of the failed prompt submit:", text)
                    alert.assert_not_called()


    def test_stop_report_shows_saved_screen_when_local_machine_case_differs(self) -> None:
        saved = session_shutdown(top_level("screen-owner", name="Screen owner"), then="restart",
            state={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
        saved["machine"] = "Mac"
        saved["alerts"] = "none"
        self.save(saved)
        restart_work._save_prompt_outcome(saved, herdr.PromptNotSubmitted(  # pyright: ignore[reportPrivateUsage]
            "not submitted", "Claude fixture\n❯ unsent opening prompt"))
        for live in (True, False):
            with self.subTest(live=live):
                with (patch.object(settle, "local_machine", return_value="mac"),
                      patch.object(restart_work, "clear_running_on_target", return_value=restart_work.TargetCheck(remaining=[saved], finished=[])),
                      patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionLive() if live else restart_work.SessionNotLive()),
                      patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "tell_showrunners_outcome"),
                      patch.object(settle, "alert_user") as alert,
                      redirect_stdout(io.StringIO()) as output):
                    settle.report_stop([saved])
                line = settle.running_restart_line(saved) if live else "Screen owner: restart prompt not submitted"
                self.assertIn("  " + line + "\n  last screen of the failed prompt submit:\n    Claude fixture\n    ❯ unsent opening prompt\n", output.getvalue())
                alert.assert_not_called()


    def test_automatic_restart_with_an_unsent_prompt_pushes_the_failed_session_name(self) -> None:
        saved = session_shutdown(top_level("unsent-owner", name="Unsent owner"), then="restart",
            state={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
        self.save(saved)
        restart_work._save_prompt_outcome(saved, herdr.PromptNotSubmitted(  # pyright: ignore[reportPrivateUsage]
            "not submitted", "Claude fixture\n❯ unsent opening prompt"))
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message") as sender,
              patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO()) as output):
            settle.report_stop([saved])
        sender.assert_not_called()
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertIn("Unsent owner", push.title + "\n" + push.body)
        expected = restart_work.restart_push(LABEL, [restart_work.FailedSessionRestart(
            "Unsent owner", "restart prompt not submitted")], machine="natedev")
        self.assertEqual(push, expected[0])
        self.assertNotIn("Claude fixture", push.title + "\n" + push.body)
        self.assertIn("Unsent owner: restart prompt not submitted", output.getvalue())
        current = shutdown_record.find_session(LOGIN, "unsent-owner")
        assert current is not None
        self.assertEqual(current["progress"], saved["progress"])


    def test_untrusted_folder_restart_pushes_the_trust_step_and_never_a_retry(self) -> None:
        command = "cd /home/natepiano/worktrees/organon && CLAUDE_CONFIG_DIR=/home/natepiano/.claude2 claude"
        untrusted = session_shutdown(top_level("untrusted-owner", name="Untrusted owner"), then="restart",
            state={"kind": "restart failed", "at": NOW, "trust_command": command,
                      "reason": f"/home/natepiano/worktrees/organon: Claude has not trusted this folder. Run: {command}"})
        broken = session_shutdown(top_level("broken-owner", name="Broken owner"), then="restart",
            state={"kind": "restart failed", "at": NOW, "reason": "window failed to open"})
        records = [untrusted, broken]
        for saved in records:
            self.save(saved)
        for attempt in ("first", "unchanged"):
            with (self.subTest(attempt=attempt),
                  patch.object(sessions, "live_sessions", return_value=[]),
                  patch.object(settle, "run_remote", return_value=(0, "[]")),
                  patch.object(settle, "send_message"),
                  patch.object(settle, "alert_user", return_value=True) as alert,
                  redirect_stdout(io.StringIO())):
                settle.report_stop([current for saved in records
                                    if (current := shutdown_record.find_session(LOGIN, saved["session"]["session_id"]))])
            pushes = {cast(RenderedPush, call.args[0]).id: cast(RenderedPush, call.args[0]) for call in alert.call_args_list}
            if attempt == "unchanged":
                self.assertEqual(pushes, {})
                continue
            self.assertEqual(set(pushes), {"shutdown.restart_untrusted", "shutdown.restart_failed"})
            trust = pushes["shutdown.restart_untrusted"]
            self.assertEqual(trust.values, {"session": "Untrusted owner", "machine": "natedev",
                                            "account": '"claude2"', "command": command})
            retry = pushes["shutdown.restart_failed"]
            self.assertEqual((retry.values["stopped"], retry.values["cause"]),
                             ("One session (Broken owner)", "window failed to open"))
            self.assertNotIn("Untrusted owner", retry.message)


    def test_stop_report_observes_failed_restart_liveness_once_before_commands_and_pushes(self) -> None:
        cases = (
            ("live", restart_work.SessionLive()),
            ("not live", restart_work.SessionNotLive()),
            ("unknown", restart_work.SessionLivenessUnknown("scratch registry unreadable")),
        )
        for state, observed in cases:
            with self.subTest(liveness=state):
                failed = session_shutdown(top_level(f"failed-{state}", name="Slow owner"), then="restart",
                    requested_by="requester",
                    state={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
                sibling = session_shutdown(top_level(f"restarted-{state}", name="Restarted sibling"), then="restart",
                    requested_by="requester", state={"kind": "restarted", "at": NOW})
                self.save(failed)
                self.save(sibling)
                records = [failed, sibling]
                with (patch.object(restart_work, "clear_running_on_target",
                                   return_value=restart_work.TargetCheck(records, [])),
                      patch.object(restart_work, "restart_failed_liveness", return_value=observed) as liveness,
                      patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message") as sender,
                      patch.object(settle, "alert_user", return_value=True) as alert,
                      patch.object(restart_work, "restart_outage_pushes", wraps=restart_work.restart_outage_pushes) as pushes,
                      patch.object(settle, "mark_not_back_pushed", wraps=settle.mark_not_back_pushed) as mark):
                    settle.report_stop(records)
                liveness.assert_called_once_with(failed)
                sender.assert_called_once()
                summary = cast(str, sender.call_args.args[1])
                text = cast(str, sender.call_args.args[2])
                current = shutdown_record.find_session(LOGIN, failed["session"]["session_id"])
                assert current is not None
                if isinstance(observed, restart_work.SessionLive):
                    self.assertEqual(summary, f"{LABEL} is back")
                    self.assertIn("  Slow owner: restart prompt not confirmed; session is running — check it, " +
                                  "then close its record in a terminal on natedev: " + settle.shutdown_command("forget",
                                      failed["session"]["session_id"], "--account", LOGIN), text)
                    self.assertNotIn("  Slow owner: restart prompt not submitted", text)
                    self.assertNotIn("shutdown.py restart", text)
                    self.assertEqual(current, failed)
                    alert.assert_not_called()
                    pushes.assert_not_called()
                    mark.assert_not_called()
                else:
                    self.assertEqual(summary, f"{LABEL} is not fully back")
                    self.assertIn("  Slow owner: restart prompt not submitted", text)
                    command = settle.shutdown_command("restart", LOGIN, "--here", "--only",
                                                      failed["session"]["session_id"])
                    self.assertIn(f"in a terminal on natedev: {command}", text)
                    self.assertNotIn("session is running", text)
                    if isinstance(observed, restart_work.SessionLivenessUnknown):
                        self.assertIn(f"{command}; liveness unknown: {observed.reason}", text)
                    alert.assert_called_once()
                    pushes.assert_called_with(
                        LABEL, [restart_work.FailedSessionRestart("Slow owner", "restart prompt not submitted")],
                        machine="natedev")
                    mark.assert_called_once_with(failed)
                    self.assertEqual(current.get("restore_alerted"), settle.not_back_text(failed))


    def test_automatic_restart_sends_all_split_pushes_and_marks_every_named_failure(self) -> None:
        batches = [[f"Owner{index}-" + "x" * suffix_length for index in range(6)]
                   for suffix_length in (80, 40)]
        batches.append(["Short owner", "Oversize-" + "x" * 201, "Another owner"])
        for names in batches:
            with self.subTest(names=names):
                records = [session_shutdown(top_level(f"split-{len(names[0])}-{index}", name=name), then="restart",
                    state={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
                    for index, name in enumerate(names)]
                for saved in records:
                    self.save(saved)
                with (patch.object(sessions, "live_sessions", return_value=[]),
                      patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message"), patch.object(settle, "alert_user", return_value=True) as alert,
                      redirect_stdout(io.StringIO())):
                    settle.report_stop(records)
                    pushes = [cast(RenderedPush, call.args[0]) for call in alert.call_args_list]
                    self.assertGreater(len(pushes), 1)
                    for push in pushes:
                        self.assertEqual(push.id, "shutdown.restart_failed")
                        self.assertLessEqual(len(push.values["stopped"]), 400)
                        self.assertLess(len(push.message), 500)
                        self.assertLess(len(push.title), 45)
                        self.assertTrue(any(name in push.message for name in names)
                                        or "…" in push.values["stopped"])
                    for name in names:
                        if len(name) == 210:
                            shortened, = [push for push in pushes if "…" in push.values["stopped"]]
                            displayed = shortened.values["stopped"].removeprefix("One session (").removesuffix(")")
                            self.assertTrue(displayed.endswith("…"))
                            self.assertGreater(len(displayed), 1)
                            self.assertTrue(name.startswith(displayed[:-1]))
                            self.assertLess(len(displayed[:-1]), len(name))
                            self.assertTrue(len(shortened.message) == 499
                                            or len(shortened.values["stopped"]) == 400)
                            self.assertFalse(any(other in shortened.message for other in names if other != name))
                        else:
                            self.assertEqual(sum(push.message.count(name) for push in pushes), 1)
                    for saved in records:
                        current = shutdown_record.find_session(LOGIN, saved["session"]["session_id"])
                        assert current is not None
                        self.assertEqual(current.get("restore_alerted"), settle.not_back_text(current))
                    alert.reset_mock()
                    settle.report_stop(shutdown_record.session_records(LOGIN))
                    alert.assert_not_called()


    def test_pending_scheduled_jobs_remain_in_session_reports_without_a_phone_alert(self) -> None:
        saved = session_shutdown(top_level("owner", name="Owner"), state={"kind": "timers pending", "at": NOW, "timers": []}, requested_by="requester")
        with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop([saved])
        alert.assert_not_called()
        self.assertIn("timers pending", cast(str, sender.call_args.args[2]))


    def test_manual_restart_asks_for_manual_restore_instead_of_retrying_the_automatic_path(self) -> None:
        saved = session_shutdown(top_level("owner", name="Owner"), state={"kind": "manual restart", "command": "private resume command"}, requested_by="requester")
        with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop([saved])
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.manual_restart")
        self.assertNotIn("private resume command", push.body)
        self.assertIn('send: "Resume the stopped Claude session named Owner on natedev from its shutdown record."', cast(ActionRequired, push.action).text)
        self.assertIn("on the natedev computer", cast(ActionRequired, push.action).text)
        self.assertIn("Its terminal cannot restart on its own", push.body)
        self.assertIn("manual restart", cast(str, sender.call_args.args[2]))


    def test_reports_with_no_failed_sessions_never_ask_the_user_to_retry(self) -> None:
        for records in ([], [session_shutdown(top_level("owner", name="Owner"),
                state={"kind": "stopped", "at": NOW}, requested_by="requester")]):
            with (self.subTest(records=records),
                  patch.object(settle, "run_remote", return_value=(0, "[]")),
                  patch.object(settle, "send_message") as sender,
                  patch.object(settle, "alert_user", return_value=True) as alert):
                settle.report_stop(records)
            alert.assert_not_called()
            if records:
                self.assertIn("Owner: stopped", cast(str, sender.call_args.args[2]))
        for entry in catalog():
            if entry.id == "shutdown.restart_failed":
                self.assertTrue(all(example.values["stopped"] != "0 sessions" for example in entry.examples))


    def test_user_alert_wait_covers_configured_review_and_remote_transport(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        with patch.object(review, "NOTIFY_CONFIG", config):
            for machine, expected in (("", 220), ("natedev", 260)):
                with (self.subTest(machine=machine),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture\n", "")) as sender):
                    _ = settle.alert_user(render("nixos.ups_battery", charge="30", minutes="5"), machine=machine)
                self.assertEqual(sender.call_args.kwargs["timeout"], expected)
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture\n", "")) as sender:
                self.assertEqual(settle.send_message("session:owner", "Shutdown", "Stop work"), {"kind": "sent"})
            self.assertEqual(sender.call_args.kwargs["timeout"], 60)


    def test_user_alert_timeout_reports_failed_delivery_without_interrupting_shutdown(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        with (patch.object(review, "user_send_seconds", return_value=115),
              patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("fake send", 130)),
              redirect_stderr(io.StringIO()) as errors):
            _ = settle.alert_user(render("nixos.ups_battery", charge="30", minutes="5"))
        self.assertIn("shutdown: user alert failed: send to user timed out after 130 seconds", errors.getvalue())


    def test_ups_alert_ignores_account_hold_and_every_session_progress(self) -> None:
        progresses: list[shutdown_record.SessionProgress] = [{"kind": "waiting"}, {"kind": "ready", "at": NOW}, {"kind": "stopping", "at": NOW}, {"kind": "stopped", "at": NOW}, {"kind": "already gone", "at": NOW}, {"kind": "process identity lost", "at": NOW}, {"kind": "stop failed", "at": NOW, "reason": "failed"}, {"kind": "restarting", "at": NOW, "prior": {"kind": "stopped", "at": NOW}}, {"kind": "restarted", "at": NOW}, {"kind": "restart failed", "at": NOW, "reason": "failed"}, {"kind": "manual restart", "command": "resume"}, {"kind": "timers pending", "at": NOW, "timers": []}]
        held = shutdown_record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW, request_id="held", requested_by={"kind": "terminal"}, then="stop")
        shutdown_record.set_account_hold(held)
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        for index, progress in enumerate(progresses):
            saved = session_shutdown(top_level(str(index), name=str(index)), state=progress)
            self.save(saved)
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture\n", "")) as sender, patch.object(shutdown_record, "session_records", side_effect=AssertionError("alert consulted shutdown state")), patch.object(review, "user_send_seconds", return_value=85):
                _ = settle.alert_user(render("nixos.ups_battery", charge="30", minutes="5"))
            self.assertEqual(sender.call_args.kwargs["input"], "")
            self.assertIn("nixos.ups_battery", cast(list[str], sender.call_args.args[0]))
            self.assertEqual(sender.call_args.kwargs["timeout"], 100)
            self.assertIn("--to", cast(list[str], cast(object, sender.call_args.args[0])))



    def test_rerun_showrunner_report_retries_unsent_claim_once(self) -> None:
        class Killed(BaseException):
            pass

        saved = self.unit_for_showrunner()
        with (
            patch.object(settle_showrunners, "current_name", return_value="fixture-runner"),
        ):
            with patch.object(settle, "send_message", side_effect=Killed):
                with self.assertRaises(Killed):
                    self.report_legacy_stopping(saved)
            with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send:
                self.report_legacy_stopping(shutdown_record.session_records(LOGIN)[0])
                self.report_legacy_stopping(shutdown_record.session_records(LOGIN)[0])
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0], "fixture-runner")
                self.assertIn("Unit is stopping", cast(str, send.call_args.args[1]))
            def failed(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = {"kind": "restart failed", "at": NOW, "reason": "fixture launch failed"}
            failed_record = shutdown_record.update_session(LOGIN, "unit", failed)
            with (
                patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionNotLive()),
                patch.object(settle, "send_message", side_effect=RuntimeError("fixture send failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture send failed"):
                    settle.tell_showrunners_outcome([failed_record])
            with (
                patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionNotLive()),
                patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send,
            ):
                settle.tell_showrunners_outcome(shutdown_record.session_records(LOGIN))
                settle.tell_showrunners_outcome(shutdown_record.session_records(LOGIN))
                self.assertEqual(send.call_count, 1)
                self.assertIn("Unit did not come back: fixture launch failed", cast(str, send.call_args.args[1]))

    def test_archived_restart_reports_back_after_retrying_its_stopping_line(self) -> None:
        class Killed(BaseException):
            pass

        saved = self.unit_for_showrunner()
        with (
            patch.object(settle_showrunners, "current_name", return_value="fixture-runner"),
        ):
            with patch.object(settle, "send_message", side_effect=Killed):
                with self.assertRaises(Killed):
                    self.report_legacy_stopping(saved)
            def restarted(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = {"kind": "restarted", "at": NOW}
            _ = shutdown_record.update_session(LOGIN, "unit", restarted)
            shutdown_record.archive_session(LOGIN, "unit")
            with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send:
                settle.tell_showrunners_outcome(shutdown_record.restarted_history())
                self.assertEqual([item.args[1] for item in send.call_args_list], [
                    "Unit is stopping: claude2 is out of usage", "Unit is back on claude3"])
                settle.tell_showrunners_outcome(shutdown_record.restarted_history())
            self.assertEqual([item.args[1] for item in send.call_args_list], [
                "Unit is stopping: claude2 is out of usage", "Unit is back on claude3"])

    def test_delivered_showrunner_line_uses_a_repeat_key_and_skipped_retry_completes_it(self) -> None:
        class Killed(BaseException):
            pass

        saved = self.unit_for_showrunner()
        send_message = settle.send_message
        commands: list[list[str]] = []

        def deliver(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            commands.append(command)
            outcome = ("SKIPPED: key 'fixture' acknowledged at " + NOW + "\n"
                       if len(commands) == 2 else "SENT: fixture\n")
            return subprocess.CompletedProcess(command, 0, outcome, "")

        with (
            patch.object(settle_showrunners, "current_name", return_value="fixture-runner"),
            patch.object(settle, "send_message", side_effect=send_message),
            patch.object(subprocess, "run", side_effect=deliver),
        ):
            self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
            with patch.object(settle, "_complete_showrunner_send", side_effect=Killed):
                with self.assertRaises(Killed):
                    self.report_legacy_stopping(saved)
            self.report_legacy_stopping(shutdown_record.session_records(LOGIN)[0])
            self.report_legacy_stopping(shutdown_record.session_records(LOGIN)[0])
            self.assertEqual(len(commands), 2)
            def restarted(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = {"kind": "restarted", "at": NOW}
            current = shutdown_record.update_session(LOGIN, "unit", restarted)
            settle.tell_showrunners_outcome([current])
            new_stop = session_shutdown(saved["session"], request_id="another-stop", then="restart")
            self.save(new_stop)
            self.report_legacy_stopping(shutdown_record.session_records(LOGIN)[0])
        self.assertEqual(len(commands), 4)
        keys = [command[command.index("--key") + 1] for command in commands]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len({keys[0], keys[2], keys[3]}), 3)
        for command in commands:
            self.assertIn("--repeat-minutes", command)
            self.assertGreater(float(command[command.index("--repeat-minutes") + 1]), 0)

    def test_queued_showrunner_line_stays_queued_until_an_acknowledged_retry(self) -> None:
        saved = self.unit_for_showrunner()
        responses = iter((
            (1, "QUEUED: fixture recipient unavailable\n"),
            (0, "SKIPPED: key 'fixture' last sent to fixture-runner at " + NOW
             + ", inside its 5256000-minute window\n"),
            (0, "SKIPPED: key 'fixture' acknowledged at " + NOW + "; `send.py reopen` to resume\n"),
        ))

        def deliver(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            code, output = next(responses)
            return subprocess.CompletedProcess(command, code, output, "")

        with (
            patch.object(settle_showrunners, "current_name", return_value="fixture-runner"),
            patch.object(subprocess, "run", side_effect=deliver) as sender,
        ):
            self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
            self.report_legacy_stopping(saved)
            current = shutdown_record.session_records(LOGIN)[0]
            queued = current.get("showrunner_lines", [])
            self.assertEqual([line["kind"] for line in queued], ["queued"])
            self.assertEqual(queued[0].get("reason"), "fixture recipient unavailable")
            self.assertEqual(queued[0].get("at"), NOW)
            self.assertTrue(settle.showrunner_was_told(current))
            settle.tell_showrunners_outcome([current])
            current = shutdown_record.session_records(LOGIN)[0]
            self.assertEqual([line["kind"] for line in current.get("showrunner_lines", [])], ["queued"])
            self.assertEqual(current.get("showrunner_lines", []), queued)
            settle.tell_showrunners_outcome([current])
            current = shutdown_record.session_records(LOGIN)[0]
            self.assertEqual([line["kind"] for line in current.get("showrunner_lines", [])], ["delivered"])
            self.report_legacy_stopping(current)
            settle.tell_showrunners_outcome([current])
        self.assertEqual(sender.call_count, 3)
        commands = [cast(list[str], sent.args[0]) for sent in sender.call_args_list]
        keys = [command[command.index("--key") + 1] for command in commands]
        self.assertEqual(keys, [keys[0]] * 3)
        self.assertEqual([sent.kwargs["input"] for sent in sender.call_args_list],
                         [sender.call_args_list[0].kwargs["input"]] * 3)

    def test_failed_showrunner_outcome_does_not_block_other_units(self) -> None:
        class Killed(BaseException):
            pass

        first = self.unit_for_showrunner()
        second_owner = unit("second-unit")
        second_owner["name"] = "Second"
        unit_staff_test_support.PARENTS[second_owner["host"]["unit"]] = staff_sources.UnitProduction("demo", "/tmp/demo-production.md")
        second = session_shutdown(second_owner, then="restart")
        self.save(second)

        def records() -> list[shutdown_record.SessionShutdown]:
            return sorted(shutdown_record.session_records(LOGIN),
                key=lambda saved: saved["session"]["session_id"] != "unit")

        with (
            patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
            patch.object(settle_showrunners, "current_name", return_value="fixture-runner"),
            patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionNotLive()),
        ):
            for saved in (first, second):
                self.report_legacy_stopping(saved)
                def failed(current: shutdown_record.SessionShutdown) -> None:
                    current["progress"] = {"kind": "restart failed", "at": NOW, "reason": "fixture launch failed"}
                _ = shutdown_record.update_session(LOGIN, saved["session"]["session_id"], failed)
            with patch.object(settle, "send_message", side_effect=Killed):
                with self.assertRaises(Killed):
                    settle.tell_showrunners_outcome(records())
            notices: list[str] = []

            def send(recipient: str, summary: str, text: str, **options: object) -> settle.MessageSent:
                del recipient, text, options
                if summary.startswith("Unit "):
                    raise RuntimeError("fixture runner offline for Unit")
                notices.append(summary)
                return settle.MessageSent(kind="sent")

            errors = io.StringIO()
            reported_error = ""
            with patch.object(settle, "send_message", side_effect=send), redirect_stderr(errors):
                try:
                    settle.tell_showrunners_outcome(records())
                except RuntimeError as error:
                    reported_error = str(error)
            self.assertEqual(notices, ["Second did not come back: fixture launch failed"])
            self.assertIn("fixture runner offline for Unit", errors.getvalue() + reported_error)
            with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as retry:
                settle.tell_showrunners_outcome(records())
                settle.tell_showrunners_outcome(records())
            self.assertEqual([item.args[1] for item in retry.call_args_list], [
                "Unit did not come back: fixture launch failed"])


class PromptDetectionTests(unittest.TestCase):
    RULE: str = "─" * 40


    PERMISSION: str = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel"


    def test_words_that_contain_form_never_report_a_form(self) -> None:
        transcript = ("The information is in the right format.\nWe perform the platform check.\n"
                      "Fill in the form, or press esc to cancel the edit.\n")
        for bottom in ("", f"{self.RULE}\n❯ \n{self.RULE}\n  ? for shortcuts"):
            with self.subTest(input_box=bool(bottom)):
                self.assertFalse(settle.waits_on_prompt(transcript + bottom))


    def test_a_permission_prompt_waits_for_the_user(self) -> None:
        self.assertTrue(settle.waits_on_prompt(f"Earlier output\n{self.RULE}\n{self.PERMISSION}\n"))
        self.assertTrue(settle.waits_on_prompt(f"{self.RULE}\n❯ 1. Yes\n  2. No\nEnter to select"))


    def test_a_prompt_scrolled_above_the_input_box_no_longer_waits(self) -> None:
        self.assertFalse(settle.waits_on_prompt(f"{self.PERMISSION}\nDone.\n{self.RULE}\n❯ \n{self.RULE}"))


    def test_a_busy_session_never_reports_a_form(self) -> None:
        for status, expected in (("busy", False), ("idle", True)):
            with (self.subTest(status=status),
                  patch.object(settle, "_screen", return_value=settle.ScreenText(self.PERMISSION)) as screen):
                self.assertEqual(settle._showing_form(top_level("one", name="one", status=status)), expected)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(screen.called, status != "busy")
