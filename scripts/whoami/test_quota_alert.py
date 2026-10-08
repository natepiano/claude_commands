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
from typing import cast, override
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

REAL_RECIPIENTS = quota_alert.recipients


def configured_recipients(config: quota_alert.Config, here: str | None = None) -> list[str]:
    del config
    return [name for name in ("natedev", "boss of bosses") if name != here]


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
        config = self.root / "showrunners.json"
        _ = config.write_text(json.dumps({"threshold_percent": 1, "repeat_minutes": 30,
                                          "stall_minutes": 5, "faults_to": "natedev", "always": ["natedev"],
                                          "showrunners": [
                                              {"session": "natedev", "zone": "America/Los_Angeles", "units": []},
                                              {"session": "boss of bosses", "zone": "America/Los_Angeles",
                                               "units": []},
                                          ]}))
        registry = self.root / "agents.conf"
        _ = registry.write_text(REGISTRY)
        for name, value in (("CONFIG", config), ("STATE", self.root / "state.json"), ("relay", self.relay),
                            ("REGISTRY", registry)):
            patcher = mock.patch.object(quota_alert, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)
        recipients_patch = mock.patch.object(quota_alert, "recipients", configured_recipients)
        _ = recipients_patch.start()
        self.addCleanup(recipients_patch.stop)
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

    def test_quota_config_uses_shared_showrunner_settings(self) -> None:
        self.assertEqual(quota_alert.Config.__module__, "scripts.production.showrunners")
        self.assertEqual(quota_alert.load_settings_from.__module__, "scripts.production.showrunners")

    def test_load_config_reads_the_named_object_layout_registry(self) -> None:
        named_config = self.root / "named-showrunners.json"
        _ = named_config.write_text(json.dumps({
            "threshold_percent": 7,
            "repeat_minutes": 45,
            "stall_minutes": 9,
            "faults_to": "fault-handler",
            "always": ["always-there"],
            "showrunners": [{
                "session": "named-director",
                "zone": "America/New_York",
                "units": [{"session": "named-unit", "status": "run-finished"}],
            }],
        }), encoding="utf-8")

        with mock.patch.object(quota_alert, "CONFIG", named_config):
            config = quota_alert.load_config()

        self.assertEqual(config["threshold_percent"], 7)
        self.assertEqual(config["always"], ["always-there"])
        self.assertEqual(config["showrunners"][0]["session"], "named-director")
        self.assertEqual([(unit.session, type(unit).__name__)
                          for unit in config["showrunners"][0]["units"]],
                         [("named-unit", "RunFinishedUnitDirector")])

    def test_repeats_to_every_recipient_until_acknowledged(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1")]
        self.assertEqual(len(quota_alert.alert(notes, self.now)), 2)  # two sends, and no switch to claude
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
        self.assertEqual(log, ["quota restored codex 1 -> natedev: sent", "quota restored codex 1 -> boss of bosses: sent"])
        text = self.sent[0][1]
        self.assertTrue(text.startswith("Quota restored: delegation on Codex can resume; the codex 1 alert is closed."))
        self.assertIn("codex 1, the active Codex account, has 100% of its weekly usage left", text)
        self.assertNotIn("claude 2", text)
        _ = self.recipients()
        self.assertEqual(quota_alert.alert(notes, self.now + timedelta(hours=1)), [])

    def test_always_then_showrunners_receive_alert_restored_and_echo_once_by_name(self) -> None:
        config = quota_alert.CONFIG
        content = cast(dict[str, object], json.loads(config.read_text()))
        runners = cast(list[dict[str, object]], content["showrunners"])
        runners.append({"session": "new director", "zone": "America/Los_Angeles", "units": []})
        _ = config.write_text(json.dumps(content))
        with mock.patch.object(quota_alert, "recipients", REAL_RECIPIENTS):
            low_note = self.note("claude 2.md", "active", "0")
            expected = ["natedev", "boss of bosses", "new director"]
            self.assertEqual(quota_alert.recipients(quota_alert.load_config()), expected)
            _ = quota_alert.alert([low_note], self.now)
            self.assertCountEqual([recipient for recipient, _ in self.sent], expected)
            _ = self.recipients()
            _ = quota_alert.alert([self.note("claude 2.md", "active", "100")], self.now + timedelta(minutes=2))
            self.assertCountEqual([recipient for recipient, _ in self.sent], expected)
            _ = self.recipients()
            _ = quota_alert.tell_others("user action", "natedev")
            self.assertCountEqual([recipient for recipient, _ in self.sent], ["boss of bosses", "new director"])

    def test_always_recipient_survives_removal_of_its_showrunner_entry(self) -> None:
        config = quota_alert.CONFIG
        content = cast(dict[str, object], json.loads(config.read_text()))
        runners = cast(list[dict[str, object]], content["showrunners"])
        content["showrunners"] = [runner for runner in runners if runner["session"] != "natedev"]
        _ = config.write_text(json.dumps(content))
        with mock.patch.object(quota_alert, "recipients", REAL_RECIPIENTS):
            self.assertEqual(quota_alert.recipients(quota_alert.load_config()), ["natedev", "boss of bosses"])
            _ = quota_alert.alert([self.note("claude 2.md", "active", "0")], self.now)
            self.assertCountEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
            _ = self.recipients()
            _ = quota_alert.alert([self.note("claude 2.md", "active", "100")],
                                  self.now + timedelta(minutes=2))
            self.assertCountEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
            _ = self.recipients()
            _ = quota_alert.tell_others("user action", "boss of bosses")
            self.assertEqual([recipient for recipient, _ in self.sent], ["natedev"])

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
        parent = os.getppid()
        grandparent = parent + 1000
        with mock.patch.object(quota_alert, "SESSIONS", sessions), mock.patch.object(
            subprocess, "run", side_effect=[
                subprocess.CompletedProcess(["ps"], 0, str(grandparent), ""),
                subprocess.CompletedProcess(["ps"], 0, "1", ""),
                subprocess.CompletedProcess(["ps"], 0, str(grandparent), ""),
            ]
        ):
            self.assertIsNone(quota_alert.current_session())
            # Two levels up, so the walk past a parent with no session file is exercised.
            _ = (sessions / f"{grandparent}.json").write_text(json.dumps({"name": "boss of bosses"}))
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

    def refuse(self) -> str:
        """Codex refused work for quota while codex 1 is out of its week; the first log line."""
        return quota_alert.blocked([self.note("codex 1.md", "active", "0")], self.now)[0]

    def test_codex_reaching_the_threshold_switches_nothing_and_says_to_keep_delegating(self) -> None:
        log = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        self.assertEqual([line for line in log if "switch" in line], [])
        self.assertEqual(self.assignments(),
                         {"delegate": "codex", "cli": "claude", "fix": "codex", "ask_a_friend": "caller"})
        text = self.sent[0][1]
        self.assertIn("nothing was switched to Claude: keep delegating Codex work", text)
        self.assertIn("if Codex refuses work for quota, bring that to them at once", text)
        self.assertNotIn("start no new", text)
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_a_quota_refusal_moves_every_codex_function_to_claude_once(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = quota_alert.alert(notes, self.now)
        self.assertEqual(self.refuse(), "switched to claude for codex 1: delegate, fix")
        self.assertEqual(self.assignments(),
                         {"delegate": "claude", "cli": "claude", "fix": "claude", "ask_a_friend": "caller"})
        self.sent.clear()
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=30))
        text = self.sent[0][1]
        stamp = self.now.isoformat(timespec="seconds")
        self.assertIn(f"Every function that ran on Codex (delegate, fix) was moved to Claude at {stamp}", text)
        self.assertIn("Codex work already in flight may fail and should be run again", text)
        self.assertNotIn("start no new", text)
        # The user puts fix back on codex mid-episode; the next run does not move it again.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["fix"], "codex")

    def test_a_refusal_tells_every_recipient_at_once_and_a_second_one_moves_nothing(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = quota_alert.alert(notes, self.now)
        self.sent.clear()
        later = self.now + timedelta(minutes=5)
        log = quota_alert.blocked(notes, later)
        self.assertEqual(log, ["switched to claude for codex 1: delegate, fix",
                               "quota alert codex 1 -> natedev: sent", "quota alert codex 1 -> boss of bosses: sent"])
        self.assertEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
        self.assertIn("Every function that ran on Codex (delegate, fix) was moved to Claude", self.sent[0][1])
        self.sent.clear()
        stamp = later.isoformat(timespec="seconds")
        self.assertEqual(quota_alert.blocked(notes, later + timedelta(minutes=1)),
                         [f"codex refused work for quota; its functions were already moved to claude at {stamp}"])
        self.assertEqual(self.sent, [])

    def test_a_refusal_above_the_threshold_moves_nothing(self) -> None:
        log = quota_alert.blocked([self.note("codex 1.md", "active", "24")], self.now)
        self.assertEqual(log, ["codex refused work for quota, but no active codex account is at or under the "
                               + "threshold; nothing switched"])
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertEqual(self.sent, [])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_a_refusal_before_the_first_alert_or_after_an_acknowledgement_still_switches(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        self.assertEqual(quota_alert.blocked(notes, self.now)[0], "switched to claude for codex 1: delegate, fix")
        self.assertEqual(len(self.sent), 2)
        _ = quota_alert.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.assignments()["delegate"], "codex")
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=4))
        _ = quota_alert.acknowledge("", "natedev", self.now + timedelta(minutes=5))
        self.sent.clear()
        log = quota_alert.blocked(notes, self.now + timedelta(minutes=6))
        self.assertEqual(log[0], "switched to claude for codex 1: delegate, fix")
        self.assertEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])

    def test_a_codex_alert_shows_the_credit_balance_or_says_none_is_reported(self) -> None:
        note = self.note("codex 1.md", "active", "0")
        _ = quota_alert.alert([note], self.now)
        self.assertEqual(self.sent[0][1].splitlines()[1], "No credit balance is reported for codex 1.")
        for value, line in (("60498", "Credits left on codex 1: 60,498."), ("0", "Credits left on codex 1: 0."),
                            ("unlimited", "Credits on codex 1: unlimited."),
                            ("null", "No credit balance is reported for codex 1.")):
            _ = note.path.write_text(note.path.read_text().replace("---\n", f"---\ncredit_balance: {value}\n", 1))
            read = read_note(note.path)
            assert read is not None
            self.assertEqual(quota_alert.credit_line(read), line)
            _ = note.path.write_text(note.path.read_text().replace(f"credit_balance: {value}\n", "", 1))
        self.sent.clear()
        _ = quota_alert.alert([self.note("claude 2.md", "active", "0")], self.now)
        self.assertNotIn("redit", self.sent[0][1])

    def test_claude_running_out_switches_nothing(self) -> None:
        _ = quota_alert.alert([self.note("claude 2.md", "active", "0")], self.now)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertIn("start no new Claude work", self.sent[0][1])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_recovery_moves_back_only_what_is_still_on_claude(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
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
        _ = quota_alert.alert(notes, self.now)
        self.assertTrue(self.refuse().startswith(
            "switch to claude for codex 1 failed: ERROR: [delegate.impl] effort 'ultra'"))
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.sent.clear()
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=30))
        text = self.sent[0][1]
        self.assertIn("The automatic switch from Codex to Claude failed: ERROR: [delegate.impl] effort 'ultra'", text)
        self.assertIn("start no new Codex work", text)
        # A registry fixed mid-episode is not switched by the next run.
        _ = registry.write_text(REGISTRY)
        _ = quota_alert.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["delegate"], "codex")

    def test_a_stopped_switch_leaves_the_registry_alone(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        with mock.patch.object(quota_alert, "EDIT_TIMEOUT", 0.01):
            line = self.refuse()
        self.assertEqual(line, "switch to claude for codex 1 failed: agents_set_all_assignments took longer "
                         + "than 0.01 s and was stopped")
        # The editor's process group died with it: nothing it started lands later.
        time.sleep(2)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertEqual([path.name for path in self.root.glob("agents.conf.*")], [])

    def test_another_low_codex_account_keeps_the_switch(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        log = quota_alert.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "1")],
                                self.now + timedelta(minutes=2))
        self.assertEqual([line for line in log if "switch" in line], [])
        self.assertEqual(self.assignments()["delegate"], "claude")
        self.assertIn("Every function that ran on Codex (delegate, fix) was moved to Claude", self.sent[0][1])
        _ = quota_alert.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "90")],
                              self.now + timedelta(minutes=4))
        self.assertEqual(self.assignments()["delegate"], "codex")

    def test_refresh_moves_back_and_says_so(self) -> None:
        _ = quota_alert.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
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
        _ = self.refuse()
        self.assertIsNone(quota_alert.notice(lambda: "boss of bosses"))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith(f"Codex refused work for quota (codex 1 at 0%, resets {self.resets}): delegate, "
                                        + "fix moved to Claude at "))
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = quota_alert.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(hours=1))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith("Codex recovered at "))
        self.assertTrue(text.endswith(": moved back from Claude to Codex: delegate, fix."))


if __name__ == "__main__":
    _ = unittest.main()
