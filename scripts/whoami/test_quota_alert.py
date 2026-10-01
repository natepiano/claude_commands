"""Repeat-until-acknowledged delivery and message content of the low-quota alert."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import override
from unittest import mock

import quota_alert
from agent_notes import local_reset, read_note

Notes = list[quota_alert.AgentNote]

REGISTRY = """\
[assignments]
delegate=codex
cli=claude
fix=codex   # a trailing comment
ask_a_friend=caller

[delegate.codex]
impl=gpt-6-sol:high
[delegate.claude]
impl=opus:high
[cli.codex]
commit_prep=gpt-6-sol:high
[cli.claude]
commit_prep=sonnet:high
[fix.codex]
style_fix=gpt-6-sol:xhigh
[fix.claude]
style_fix=opus:max
[ask_a_friend.codex]
consultation=gpt-6-sol:max
[ask_a_friend.claude]
consultation=opus:max

[codex.agents]
gpt-6-sol=low,medium,high,xhigh,max
[claude.agents]
opus=low,medium,high,xhigh,max
sonnet=low,medium,high
"""


class QuotaAlertTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.now: datetime = datetime.now(timezone.utc).replace(microsecond=0)
        self.resets: str = local_reset(self.now + timedelta(days=2))
        self.failing: set[str] = set()
        self.sent: list[tuple[str, str]] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = self.root / "quota_alert.json"
        _ = config.write_text(json.dumps({"threshold_percent": 1, "repeat_minutes": 30,
                                          "notify": ["natedev", "boss of bosses"]}))
        registry = self.root / "agents.conf"
        _ = registry.write_text(REGISTRY)
        for name, value in (("CONFIG", config), ("STATE", self.root / "state.json"), ("relay", self.relay),
                            ("REGISTRY", registry)):
            patcher = mock.patch.object(quota_alert, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)
        # A fresh sync stamp and no Codex config keep agents_config.sh from syncing the catalog into the fixture.
        synced = self.root / "catalog synced"
        synced.touch()
        for key, value in (("CODEX_CATALOG_SYNC_STATE_FILE", synced), ("CODEX_CONFIG_FILE", self.root / "absent"),
                           ("CODEX_MODELS_CACHE_FILE", self.root / "absent")):
            self.addCleanup(self.restore_environment, key, os.environ.get(key))
            os.environ[key] = str(value)

    @staticmethod
    def restore_environment(key: str, value: str | None) -> None:
        if value is None:
            _ = os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def relay(self, recipient: str, text: str, key: str | None) -> str | None:
        del key
        self.sent.append((recipient, text))
        return "not reachable" if recipient in self.failing else None

    def recipients(self) -> list[str]:
        found = sorted(recipient for recipient, _ in self.sent)
        self.sent.clear()
        return found

    def note(self, name: str, state: str, left: str, resets: str | None = None) -> quota_alert.AgentNote:
        path = self.root / name
        _ = path.write_text(f"---\nlogin: someone@example.com\nresets: {resets or self.resets}\n"
                            + f"state: {state}\nweekly_remaining_usage: {left}\n---\n")
        note = read_note(path)
        assert note is not None
        return note

    def test_repeats_to_every_recipient_until_acknowledged(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1")]
        self.assertEqual(len(quota_alert.alert(notes, self.now)), 3)  # the switch to claude, then two sends
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=10))
        self.assertEqual(self.recipients(), [])
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])
        _ = quota_alert.acknowledge(here="natedev")
        _ = self.recipients()
        _ = quota_alert.alert(notes, self.now + timedelta(hours=5))
        self.assertEqual(self.recipients(), [])

    def test_failed_delivery_retries_on_the_same_cadence(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1")]
        self.failing.add("boss of bosses")
        log = quota_alert.alert(notes, self.now)
        self.assertIn("quota alert codex 1 -> boss of bosses: not delivered: not reachable", log)
        _ = self.recipients()
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])

    def test_recovery_rearms_but_an_unknown_reading_does_not(self) -> None:
        _ = quota_alert.alert([self.note("claude 2.md", "active", "0")], self.now)
        _ = quota_alert.acknowledge("claude 2")
        _ = self.recipients()
        _ = quota_alert.alert([self.note("claude 2.md", "active", "null")], self.now)
        _ = quota_alert.alert([self.note("claude 2.md", "active", "1")], self.now + timedelta(hours=1))
        self.assertEqual(self.recipients(), [])
        _ = quota_alert.alert([self.note("claude 2.md", "active", "100")], self.now + timedelta(hours=2))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])
        _ = quota_alert.alert([self.note("claude 2.md", "active", "1")], self.now + timedelta(hours=3))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])

    def test_recovery_sends_restored_to_everyone_once(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "1"), self.note("claude 2.md", "active", "64")],
                              self.now)
        _ = self.recipients()
        notes: Notes = [self.note("codex 1.md", "active", "100"), self.note("claude 2.md", "active", "64")]
        log = quota_alert.alert(notes, self.now + timedelta(minutes=2))
        self.assertEqual(log, ["switch to claude undone: moved back from Claude to Codex: delegate, fix",
                               "quota restored codex 1 -> natedev: sent", "quota restored codex 1 -> boss of bosses: sent"])
        text = self.sent[0][1]
        self.assertTrue(text.startswith("Quota restored: delegation on Codex can resume; the codex 1 alert is closed."))
        self.assertIn("codex 1, the active Codex account, has 100% of its weekly usage left", text)
        self.assertNotIn("claude 2", text)
        _ = self.recipients()
        self.assertEqual(quota_alert.alert(notes, self.now + timedelta(hours=1)), [])

    def test_switch_to_a_low_account_is_not_a_restore(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.recipients()
        _ = quota_alert.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "1")],
                              self.now + timedelta(minutes=2))
        self.assertTrue(all(text.startswith("Quota alert: codex 2") for _, text in self.sent))

    def test_refresh_announces_to_the_others_even_after_the_timer_closed_it(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "1")], self.now)
        notes: Notes = [self.note("codex 1.md", "active", "100")]
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=2))
        _ = self.recipients()
        lines = quota_alert.refresh(notes, "natedev", self.now + timedelta(minutes=3))
        self.assertEqual(lines[-2:], [f"codex 1: 100% left, resets {self.resets}", "told boss of bosses"])
        self.assertEqual([recipient for recipient, _ in self.sent], ["boss of bosses"])
        self.assertTrue(self.sent[0][1].startswith("Quota restored: delegation on Codex can resume; the user reported"))

    def test_refresh_with_every_account_still_low_announces_nothing(self) -> None:
        lines = quota_alert.refresh([self.note("codex 1.md", "active", "1")], "natedev", self.now)
        self.assertEqual(lines[-1], "no active account is above the threshold; nothing announced")
        self.assertTrue(all(text.startswith("Quota alert:") for _, text in self.sent))

    def test_acknowledgement_during_delivery_is_kept(self) -> None:
        def acknowledging_relay(recipient: str, text: str, key: str | None) -> str | None:
            _ = quota_alert.acknowledge()
            return self.relay(recipient, text, key)

        notes: Notes = [self.note("codex 1.md", "active", "1")]
        with mock.patch.object(quota_alert, "relay", acknowledging_relay):
            _ = quota_alert.alert(notes, self.now)
        _ = self.recipients()
        _ = quota_alert.alert(notes, self.now + timedelta(hours=1))
        self.assertEqual(self.recipients(), [])

    def test_acknowledge_names_one_episode_or_reports_none(self) -> None:
        self.assertEqual(quota_alert.acknowledge(), ["no quota alert is open"])
        _ = quota_alert.alert([self.note("codex 1.md", "active", "1"), self.note("claude 2.md", "active", "0")],
                              self.now)
        self.assertEqual(quota_alert.acknowledge("codex 2"), ["no open quota alert named codex 2"])
        _ = quota_alert.acknowledge("codex 1", now=self.now)
        self.assertEqual(quota_alert.acknowledge(here="natedev"), [
            "codex 1: already acknowledged at " + self.now.isoformat(timespec="seconds"),
            "claude 2: acknowledged; quiet until it is back above the threshold",
            "told boss of bosses"])

    def test_acknowledgement_is_echoed_to_every_other_configured_session(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "1")], self.now)
        _ = self.recipients()
        self.failing.add("boss of bosses")
        self.assertEqual(quota_alert.acknowledge(here="natedev")[-1], "could not tell boss of bosses: not reachable")
        recipient, text = self.sent.pop()
        self.assertEqual((recipient, self.sent), ("boss of bosses", []))
        self.assertTrue(text.startswith('Quota alert acknowledged: the user silenced codex 1 in session "natedev" at '))
        self.assertEqual(quota_alert.acknowledge(here="natedev"), ["codex 1: already acknowledged at "
                                                                   + text.split(" at ", 1)[1].split(". ", 1)[0]])
        self.assertEqual(self.sent, [])

    def test_acknowledgement_outside_a_session_tells_everyone(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "1")], self.now)
        _ = self.recipients()
        _ = quota_alert.acknowledge()
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])

    def test_current_session_is_found_up_the_process_tree(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        with mock.patch.object(quota_alert, "SESSIONS", sessions):
            self.assertIsNone(quota_alert.current_session())
            # Two levels up, so the walk past a parent with no session file is exercised.
            found = subprocess.run(["ps", "-o", "ppid=", "-p", str(os.getppid())], capture_output=True, text=True,
                                   check=True)
            _ = (sessions / f"{int(found.stdout)}.json").write_text(json.dumps({"name": "boss of bosses"}))
            self.assertEqual(quota_alert.current_session(), "boss of bosses")

    def test_inactive_above_threshold_unknown_or_reset_notes_are_quiet(self) -> None:
        past = local_reset(self.now - timedelta(hours=1))
        notes: Notes = [self.note("codex 2.md", "inactive", "0"), self.note("claude 1.md", "active", "2"),
                        self.note("claude 2.md", "active", "null"), self.note("codex 1.md", "active", "0", past)]
        self.assertEqual(quota_alert.alert(notes, self.now), [])
        self.assertEqual(self.sent, [])

    def test_message_names_the_note_and_its_siblings_but_no_login(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1"), self.note("codex 2.md", "inactive", "40"),
                        self.note("claude 1.md", "inactive", "3")]
        _ = quota_alert.alert(notes, self.now)
        text = self.sent[0][1]
        self.assertTrue(text.startswith("Quota alert: codex 1, the active Codex account, has 1% "))
        self.assertIn("codex 2: inactive, 40% weekly usage left", text)
        self.assertIn("every 30 minutes until the user acknowledges it by typing /quota_ack", text)
        self.assertNotIn("claude 1", text)
        self.assertNotIn("@", text)

    def assignments(self) -> dict[str, str]:
        return quota_alert.registry_assignments()

    def test_codex_running_out_moves_every_codex_function_to_claude_once(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        log = quota_alert.alert(notes, self.now)
        self.assertIn("switched to claude for codex 1: delegate, fix", log)
        self.assertEqual(self.assignments(),
                         {"delegate": "claude", "cli": "claude", "fix": "claude", "ask_a_friend": "caller"})
        text = self.sent[0][1]
        stamp = self.now.isoformat(timespec="seconds")
        self.assertIn(f"Every function that ran on Codex (delegate, fix) was moved to Claude at {stamp}", text)
        self.assertIn("Codex work already in flight may fail and should be run again", text)
        self.assertNotIn("start no new", text)
        # The user puts fix back on codex mid-episode; the next run does not move it again.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.assignments()["fix"], "codex")

    def test_claude_running_out_switches_nothing(self) -> None:
        _ = quota_alert.alert([self.note("claude 2.md", "active", "0")], self.now)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertIn("start no new Claude work", self.sent[0][1])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_recovery_moves_back_only_what_is_still_on_claude(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        self.sent.clear()
        log = quota_alert.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(hours=1))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: delegate; left alone, "
                      + "re-assigned since the switch: fix", log)
        self.assertEqual(self.assignments(),
                         {"delegate": "codex", "cli": "claude", "fix": "codex", "ask_a_friend": "caller"})
        self.assertIn("The automatic switch is undone: moved back from Claude to Codex: delegate;", self.sent[0][1])

    def test_a_refused_switch_still_alerts_and_says_why(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        log = quota_alert.alert(notes, self.now)
        self.assertTrue(log[0].startswith("switch to claude for codex 1 failed: ERROR: [delegate.impl] effort 'ultra'"))
        self.assertEqual(self.assignments()["delegate"], "codex")
        text = self.sent[0][1]
        self.assertIn("The automatic switch from Codex to Claude failed: ERROR: [delegate.impl] effort 'ultra'", text)
        self.assertIn("start no new Codex work", text)
        # Once per episode: a registry fixed mid-episode is not switched by the next run.
        _ = registry.write_text(REGISTRY)
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.assignments()["delegate"], "codex")

    def test_a_stopped_switch_leaves_the_registry_alone(self) -> None:
        with mock.patch.object(quota_alert, "EDIT_TIMEOUT", 0.01):
            log = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        self.assertEqual(log[0], "switch to claude for codex 1 failed: agents_set_all_assignments took longer "
                         + "than 0.01 s and was stopped")
        # The editor's process group died with it: nothing it started lands later.
        time.sleep(2)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertEqual([path.name for path in self.root.glob("agents.conf.*")], [])

    def test_another_low_codex_account_keeps_the_switch(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        self.sent.clear()
        log = quota_alert.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "1")],
                                self.now + timedelta(minutes=2))
        self.assertIn("switch to claude for codex 2: nothing was on codex", log)
        self.assertEqual(self.assignments()["delegate"], "claude")
        self.assertIn("Every function that ran on Codex (delegate, fix) was moved to Claude", self.sent[0][1])
        _ = quota_alert.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "90")],
                              self.now + timedelta(minutes=4))
        self.assertEqual(self.assignments()["delegate"], "codex")

    def test_refresh_moves_back_and_says_so(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        self.sent.clear()
        lines = quota_alert.refresh([self.note("codex 1.md", "active", "100")], "natedev",
                                    self.now + timedelta(minutes=3))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: delegate, fix", lines)
        self.assertEqual(self.assignments()["fix"], "codex")
        self.assertEqual([recipient for recipient, _ in self.sent], ["boss of bosses"])
        self.assertIn("The automatic switch is undone: moved back from Claude to Codex: delegate, fix.", self.sent[0][1])

    def test_notice_shows_each_switch_once_and_only_in_the_owner_session(self) -> None:
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        self.assertIsNone(quota_alert.notice(lambda: "boss of bosses"))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith(f"Codex ran out of quota (codex 1 at 0%, resets {self.resets}): delegate, "
                                        + "fix moved to Claude at "))
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = quota_alert.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(hours=1))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith("Codex recovered at "))
        self.assertTrue(text.endswith(": moved back from Claude to Codex: delegate, fix."))


if __name__ == "__main__":
    _ = unittest.main()
