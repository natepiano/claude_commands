"""Tests for held news: when it is sent to the user, when typing in a terminal stops it, and that it is sent once."""
from __future__ import annotations

import os
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest.mock import patch

import escalate
import send
import sessions
if TYPE_CHECKING:
    from ..whoami.agent_accounts import KnownCodexAccount, UnknownCodexAccount
else:
    from scripts.whoami.agent_accounts import KnownCodexAccount, UnknownCodexAccount
from user_action import ActionRequired, NoActionRequired

SCRIPT = Path(__file__).with_name("escalate.py")
NOTIFIER_STUB = 'print -r -- "notifier $*" >> "$LOG"\n'
SEND_STUB = """import os, sys
with open(os.environ["LOG"], "a") as log:
    _ = log.write("send " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("REFUSED"):
    print("FAILED: the user was not reached: exit 1")
    raise SystemExit(3)
"""


class EscalateTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.root / "notifier.sh").write_text(NOTIFIER_STUB, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        _ = (self.root / "log").write_text("", encoding="utf-8")
        self.environment = {
            **os.environ, "ESCALATE_STATE_DIR": str(self.root / "state"), "LOG": str(self.root / "log"),
            "ESCALATE_SEND": str(self.root / "send.py"), "ESCALATE_NOTIFIER": str(self.root / "notifier.sh"),
        }

    def run_script(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env={**self.environment, **environment},
                              capture_output=True, text=True, check=False)

    def hold(self, minutes: str = "0") -> subprocess.CompletedProcess[str]:
        return self.run_script("hold", "disk", "--summary", "Disk", "--text", "Disk is full.",
                               "--action", "free disk space", "--minutes", minutes)

    def log(self) -> str:
        return (self.root / "log").read_text(encoding="utf-8")

    def test_a_hold_starts_the_minute_tick_and_a_second_hold_changes_nothing(self) -> None:
        self.assertEqual(self.hold("15").stdout,
                         "disk: held; sent to the user in 15 min unless they type in a terminal\n")
        self.assertEqual(self.hold("15").stdout, "disk: already held\n")
        self.assertEqual(self.log().count("notifier new escalate --every 1 --run "), 1)
        self.assertIn(f"{SCRIPT.resolve()} due\n", self.log())
        self.assertEqual(self.run_script("list").stdout, "disk - waiting, sent in 15 min - Disk\n")

    def test_nothing_is_sent_before_its_time(self) -> None:
        _ = self.hold("15")
        done = self.run_script("due")
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertNotIn("send ", self.log())

    def test_unanswered_news_is_sent_to_the_user_once(self) -> None:
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")
        self.assertEqual(self.run_script("due").stdout, "")
        self.assertEqual(self.log().count(
            "send --to user --from escalate --key escalate-disk --summary Disk --need decision --action free disk space "
            + "--text Disk is full.\n"), 1)
        self.assertEqual(self.run_script("list").stdout, "disk - sent - Disk\n")

    def test_closing_a_sent_blocked_push_stops_its_repeats_and_other_closes_do_not(self) -> None:
        _ = self.run_script("hold", "power", "--summary", "Power", "--text", "Power is out.",
                            "--action", "save your work", "--minutes", "0", "--need", "blocked")
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\npower: sent to the user\n")
        self.assertEqual(self.run_script("close", "disk").stdout, "disk: closed\n")
        self.assertNotIn("send retire", self.log())
        closed = self.run_script("close", "power")
        self.assertEqual((closed.stdout, closed.stderr), ("power: closed\n", ""))
        self.assertEqual(self.log().count("send retire --to user --from escalate --key escalate-power\n"), 1)

    def test_typing_in_a_terminal_after_the_hold_stops_it(self) -> None:
        _ = self.hold()
        _ = self.run_script("typed")
        self.assertEqual(self.run_script("due").stdout, "disk: the user typed in a terminal; not sent\n")
        self.assertNotIn("send ", self.log())
        self.assertEqual(self.run_script("due").stdout, "")

    def test_close_retries_a_failed_emergency_cancellation_before_forgetting_the_hold(self) -> None:
        _ = self.run_script("hold", "power", "--summary", "Power", "--text", "Power is out.",
                            "--action", "save your work", "--minutes", "0", "--need", "blocked")
        _ = self.run_script("due")
        failed = self.run_script("close", "power", REFUSED="1")
        self.assertIn("FAILED: the user was not reached", failed.stderr)
        path = self.root / "state/held/power.json"
        self.assertTrue(path.exists())
        self.assertEqual(self.run_script("close", "power").stdout, "power: closed\n")
        self.assertFalse(path.exists())
        self.assertEqual(self.log().count("send retire --to user --from escalate --key escalate-power\n"), 2)

    def test_typing_before_the_hold_does_not_count(self) -> None:
        _ = self.run_script("typed")
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_a_send_that_fails_is_tried_again(self) -> None:
        _ = self.hold()
        done = self.run_script("due", REFUSED="1")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "disk: NOT sent: FAILED: the user was not reached: exit 1\n")
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_obsolete_script_hold_is_logged_once_closed_and_never_delivered(self) -> None:
        path = self.root / "state/held/usage.json"
        path.parent.mkdir(parents=True)
        record = {"summary": "Usage low", "text": "Old phone text", "need": "note",
                  "held_at": 1.0, "minutes": 0, "outcome": "waiting", "action": {"kind": "none"},
                  "text_id": "five_hour.removed", "values": {"remaining": "0%"}}
        for route in ("due", "close", "hold", "reconcile"):
            with self.subTest(route=route):
                _ = path.write_text(json.dumps(record))
                with (patch.object(escalate, "STATE", path.parents[1]), patch.object(escalate, "schedule"),
                      patch.object(escalate, "codex_account", return_value=UnknownCodexAccount(self.root, "offline")),
                      patch.object(subprocess, "run", side_effect=AssertionError("obsolete hold delivered"))):
                    if route == "due":
                        self.assertIn("closed script hold five_hour.removed", escalate.due(10**12)[0].line)
                        self.assertEqual(escalate.due(10**12), [])
                    elif route == "hold":
                        self.assertTrue(escalate.hold("usage", escalate.SessionPush("Choose build", "Retry?", ActionRequired("Choose retry."))))
                    elif route == "reconcile":
                        escalate.reconcile_codex_holds(self.root, "new@example.invalid")
                    else:
                        self.assertTrue(escalate.close("usage"))
                    if route != "hold":
                        self.assertIsInstance(escalate.read(path), escalate.NoHeld)
                    else:
                        self.assertIsInstance(escalate.read(path), escalate.HeldMessage)
                    _ = escalate.close("usage")
                    self.assertFalse(escalate.close("usage"))
        entries = [cast(escalate.RetiredHoldRecord, json.loads(line)) for line in (path.parents[1] / "log.jsonl").read_text().splitlines()]
        self.assertEqual(len(entries), 4)
        self.assertTrue(all(entry["text_id"] == "five_hour.removed" for entry in entries))
        self.assertNotIn("send ", self.log())

    def test_session_hold_written_before_registry_metadata_still_sends_and_closes(self) -> None:
        path = self.root / "state/held/session.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps({"summary": "Build choice", "text": "Choose retry.", "need": "decision",
            "held_at": 1.0, "minutes": 0, "outcome": "waiting", "action": {"kind": "required", "text": "Choose retry."}}))
        self.assertEqual(self.run_script("due").stdout, "session: sent to the user\n")
        self.assertEqual(self.run_script("due").stdout, "")
        self.assertIn("--summary Build choice --need decision --action Choose retry. --text Choose retry.", self.log())
        self.assertEqual(self.run_script("close", "session").stdout, "session: closed\n")

    def test_review_held_push_is_terminal_and_returns_to_the_relayed_session(self) -> None:
        with (patch.object(escalate, "STATE", self.root / "held review"),
              patch.object(escalate, "schedule"), patch.object(escalate, "typed_at", return_value=0),
              patch.object(escalate, "codex_account", return_value=UnknownCodexAccount(self.root, "offline")),
              patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess([], 4, "HELD: not sent, failed review: reader-starts-cold\n", ""),
                  subprocess.CompletedProcess([], 0, "SENT: to session\n", "")]) as run):
            relay = escalate.HeldRelay(recipient="session:writer", key="original-question")
            _ = escalate.hold("question", escalate.SessionPush("Build choice", "Choose a build", ActionRequired("Choose retry"), "decision"), minutes=0, relays=(relay,))
            settled = escalate.due(10**12)
            self.assertEqual(escalate.due(10**12), [])
        self.assertEqual(run.call_count, 2)
        self.assertIn("HELD: not sent", settled[0].line)
        self.assertFalse(settled[0].failed)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--to") + 1], "session:writer")
        self.assertIn("reader-starts-cold", command[command.index("--text") + 1])
        self.assertIn("Revise and resend", command[command.index("--text") + 1])

    def test_cli_question_origin_survives_until_a_held_review_returns_to_it(self) -> None:
        sessions_dir = self.root / ".claude/sessions"
        sessions_dir.mkdir(parents=True)
        _ = (sessions_dir / "123.json").write_text(json.dumps({"sessionId": "origin-session"}))
        with (patch.dict(os.environ, {"HOME": str(self.root), "CLAUDE_PID": "123"}),
              patch.object(escalate, "STATE", self.root / "origin holds"), patch.object(escalate, "schedule"),
              patch.object(escalate, "typed_at", return_value=0),
              patch.object(escalate, "codex_account", return_value=UnknownCodexAccount(self.root, "offline")),
              patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess([], 4, "HELD: failed review\n", ""),
                  subprocess.CompletedProcess([], 0, "SENT\n", "")]) as run):
            self.assertEqual(escalate.main(["hold", "question", "--summary", "Choose build", "--text", "Choose retry",
                                           "--action", "Choose retry", "--minutes", "0"]), 0)
            _ = escalate.due(10**12)
            self.assertEqual(escalate.due(10**12), [])
        self.assertEqual(run.call_count, 2)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--to") + 1], "session:origin-session")

    def test_due_retries_a_failed_review_result_relay_without_retrying_the_user_push(self) -> None:
        reason = "HELD: not sent, failed review: reader-starts-cold"
        with (patch.object(escalate, "STATE", self.root / "held review"),
              patch.object(escalate, "schedule"), patch.object(escalate, "typed_at", return_value=0) as typed,
              patch.object(escalate, "codex_account", return_value=UnknownCodexAccount(self.root, "offline")),
              patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess([], 4, reason + "\n", ""),
                  subprocess.CompletedProcess([], 3, "FAILED: relay unavailable\n", ""),
                  subprocess.CompletedProcess([], 0, "SENT: to session\n", "")]) as run):
            relay = escalate.HeldRelay(recipient="session:writer", key="original-question")
            _ = escalate.hold("question", escalate.SessionPush("Build choice", "Choose a build",
                              ActionRequired("Choose retry")), minutes=0, relays=(relay,))
            settled = escalate.due(10**12)
            self.assertTrue(settled[-1].failed)
            self.assertIn("held result relay failed", settled[-1].line)
            pending = escalate.read(escalate.held_path("question"))
            assert isinstance(pending, escalate.HeldMessage)
            self.assertEqual((pending.outcome, pending.review_reason), ("review_held", reason))
            # Typing after the refusal must not discard the pending result.
            typed.return_value = 10**12
            self.assertEqual(escalate.due(10**12), [])
            self.assertEqual(escalate.due(10**12), [])
            finished = escalate.read(escalate.held_path("question"))
            assert isinstance(finished, escalate.HeldMessage)
            self.assertEqual((finished.outcome, finished.review_reason), ("review_held", ""))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1], run.call_args_list[2])
        command = cast(list[str], run.call_args.args[0])
        self.assertIn(reason, command[command.index("--text") + 1])

    def test_close_forgets_the_key_so_the_next_hold_waits_anew(self) -> None:
        _ = self.hold()
        _ = self.run_script("due")
        self.assertEqual(self.run_script("close", "disk").stdout, "disk: closed\n")
        self.assertEqual(self.run_script("close", "disk").stdout, "disk: not held\n")
        self.assertEqual(self.run_script("list").stdout, "nothing held\n")
        _ = self.hold()
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_no_action_is_only_valid_for_a_note(self) -> None:
        with self.assertRaises(ValueError):
            _ = escalate.hold("disk", escalate.SessionPush("Disk", "full", NoActionRequired(), "decision"))
        held = self.run_script("hold", "news", "--summary", "News", "--text", "Finished.",
                               "--no-action", "--need", "note", "--minutes", "0")
        self.assertEqual(held.returncode, 0)
        self.assertEqual(self.run_script("due").stdout, "news: sent to the user\n")
        self.assertIn("--need note --no-action --text Finished.", self.log())

    def test_legacy_hold_is_not_delivered_and_the_next_hold_replaces_it(self) -> None:
        path = self.root / "state" / "held" / "disk.json"
        path.parent.mkdir(parents=True)
        legacy = {"summary": "Old disk", "text": "Old text", "need": "decision", "held_at": 0.0,
                  "minutes": 0, "outcome": "waiting"}
        _ = path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(self.run_script("due").stdout, "")
        self.assertNotIn("send ", self.log())
        self.assertEqual(self.run_script("list").stdout,
                         "disk - waiting for a hold that states its action\n")

        self.assertIn("disk: held", self.hold().stdout)
        record = cast(dict[str, object], cast(object, json.loads(path.read_text(encoding="utf-8"))))
        self.assertEqual(record["action"], {"kind": "required", "text": "free disk space"})
        self.assertEqual(self.run_script("due").stdout, "disk: sent to the user\n")

    def test_current_hold_is_not_replaced(self) -> None:
        _ = self.hold("15")
        path = self.root / "state" / "held" / "disk.json"
        before = path.read_text(encoding="utf-8")
        second = self.run_script("hold", "disk", "--summary", "Changed", "--text", "Changed.",
                                 "--action", "do something else", "--minutes", "15")
        self.assertEqual(second.stdout, "disk: already held\n")
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_written_action_reads_back_as_the_domain_type(self) -> None:
        _ = self.hold("15")
        held = escalate.read(self.root / "state" / "held" / "disk.json")
        self.assertIsInstance(held, escalate.HeldMessage)
        assert isinstance(held, escalate.HeldMessage)
        self.assertEqual(held.action, ActionRequired("free disk space"))

    def test_missing_or_wrong_action_explains_the_fix(self) -> None:
        for action in ([], ["--action", "none"], ["--no-action"]):
            done = self.run_script("hold", "disk", "--summary", "a", "--text", "b", *action)
            self.assertEqual(done.returncode, 2)
            self.assertIn("Say what the user does", done.stderr)

    def select_codex(self, number: int) -> KnownCodexAccount:
        home = self.root / f".codex{number}"
        home.mkdir(exist_ok=True)
        login = f"account{number}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"x.{claims}.y"}}))
        _ = (self.root / f"codex{number}.md").write_text(f"---\nlogin: {login}\n---\n")
        script = SCRIPT.parent.parent / "accounts" / "accounts.py"

        selected = subprocess.run([sys.executable, str(script), "set-role", "workers", f"codex{number}"],
            env={**self.environment, "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)}, capture_output=True, text=True, check=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        return KnownCodexAccount(home, login, f"codex{number}")

    def test_due_after_swap_cancels_outgoing_account_without_waiting_for_updater(self) -> None:
        with (patch.dict(os.environ, {"HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)}),
              patch.object(escalate, "STATE", self.root / "owned holds"),
              patch.object(escalate, "schedule"), patch.object(escalate, "deliver", return_value="") as deliver):
            a = self.select_codex(1)
            _ = escalate.hold("a-window", escalate.SessionPush("codex1", "A low", NoActionRequired(), "note"), minutes=0, owner=a)
            b = self.select_codex(2)
            _ = escalate.hold("b-window", escalate.SessionPush("codex2", "B low", NoActionRequired(), "note"), minutes=0, owner=b)
            _ = escalate.hold("other-tool", escalate.SessionPush("Claude", "Claude low", NoActionRequired(), "note"), minutes=0)
            _ = escalate.due(10**12)
            held = [cast(escalate.HeldMessage, call.args[0]) for call in deliver.call_args_list]
            self.assertEqual({entry.summary for entry in held}, {"codex2", "Claude"})
            self.assertIsInstance(escalate.read(escalate.held_path("a-window")), escalate.NoHeld)

    def test_due_with_unknown_selected_login_preserves_owned_hold_without_delivery(self) -> None:
        with (patch.dict(os.environ, {"HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)}),
              patch.object(escalate, "STATE", self.root / "owned holds"),
              patch.object(escalate, "schedule"), patch.object(escalate, "deliver", return_value="") as deliver):
            a = self.select_codex(1)
            _ = escalate.hold("a-window", escalate.SessionPush("codex1", "A low", NoActionRequired(), "note"), minutes=0, owner=a)
            _ = (a.home / "auth.json").write_text("unreadable")
            before = escalate.held_path("a-window").read_text()
            _ = escalate.due(10**12)
            deliver.assert_not_called()
            self.assertEqual(escalate.held_path("a-window").read_text(), before)

    def test_due_with_signed_in_account_without_a_note_preserves_codex_holds(self) -> None:
        with (patch.dict(os.environ, {"HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)}),
              patch.object(escalate, "STATE", self.root / "owned holds"),
              patch.object(escalate, "schedule"), patch.object(escalate, "deliver", return_value="") as deliver):
            a = self.select_codex(1)
            _ = escalate.hold("a-window", escalate.SessionPush("codex1", "A low", NoActionRequired(), "note"), minutes=0, owner=a)
            _ = escalate.hold("five-hour-codex", escalate.SessionPush("legacy Codex", "old low", NoActionRequired(), "note"), minutes=0)
            _ = (self.root / "codex1.md").unlink()
            before = {path.name: path.read_text() for path in (escalate.STATE / "held").glob("*.json")}
            _ = escalate.due(10**12)
            deliver.assert_not_called()
            self.assertEqual({path.name: path.read_text() for path in (escalate.STATE / "held").glob("*.json")}, before)

    def test_due_retires_outgoing_and_legacy_relays_for_an_offline_recipient(self) -> None:
        with (patch.dict(os.environ, {"HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)}),
              patch.object(escalate, "STATE", self.root / "owned holds"),
              patch.object(send, "STATE", self.root / "messages"),
              patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(escalate, "schedule"), patch.object(escalate, "deliver", return_value="") as deliver):
            a = self.select_codex(1)
            for key in ("5-hour codex1", "5-hour Codex", "5-hour Claude"):
                send.enqueue(send.Message("offline", "quota_alert", "", "low window", key), "offline", datetime.now(timezone.utc))
            relay = escalate.HeldRelay(recipient="offline", key="5-hour codex1")
            _ = escalate.hold("a-window", escalate.SessionPush("codex1", "A low", NoActionRequired(), "note"), owner=a, relays=(relay,))
            _ = escalate.hold("five-hour-codex", escalate.SessionPush("legacy", "old low", NoActionRequired(), "note"))
            _ = self.select_codex(2)
            def retire(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                self.assertIn("retire", command)
                _ = send.retire(command[command.index("--to") + 1], "quota_alert", command[command.index("--key") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")
            with (patch.object(subprocess, "run", side_effect=retire),
                  patch.object(escalate, "load_settings", return_value={"always": ["offline"]}),
                  patch.object(escalate, "registered_showrunners", return_value=[])):
                _ = escalate.due(10**12)
            deliver.assert_not_called()
            self.assertEqual([item["key"] for item in send.read_queue(send.queue_path("offline"))], ["5-hour Claude"])
            self.assertEqual(list((escalate.STATE / "held").glob("*.json")), [])

    def test_any_other_arguments_print_the_usage(self) -> None:
        for arguments in ([], ["hold", "disk"],
                          ["hold", "disk", "--summary", "a", "--text", "b", "--action", "fix it", "--need", "loud"],
                          ["hold", "bad key", "--summary", "a", "--text", "b"], ["close"], ["due", "now"]):
            done = self.run_script(*arguments)
            self.assertEqual(done.returncode, 2, arguments)
            self.assertIn("usage: escalate.py", done.stderr)


if __name__ == "__main__":
    _ = unittest.main()
