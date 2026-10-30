"""Quota alerts: delivery."""

from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from typing import cast
from unittest import mock
from scripts.whoami import quota_alert_test_support as support
import quota_alert
import review
from push_texts import render
import agent_notes
import note_file
from user_action import ActionRequired
from scripts.whoami.quota_alert_test_support import Notes, REAL_TELL_USER, REFUSAL_ACTION, REGISTRY as REGISTRY

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_internal_switch_failure_details_do_not_leak_into_the_phone_push(self) -> None:
        note = self.note("codex1.md", "active", "0")
        push = quota_alert.refusal_push(note, quota_alert.NoCodexQuotaFallback(),
                                        "Could not read /private/account/registry " + "unavailable " * 100, False)
        self.assertIsInstance(push, quota_alert.UserPush)
        self.assertNotIn("/private", push.text)
        self.assertNotIn("registry", push.text)
        self.assertIn("the work is paused", push.text)

    def test_actionable_quota_pushes_fit_phone_body_and_name_where_to_act(self) -> None:
        notes: Notes = [self.note("claude2.md", "active", "0", limit_reset_count="0"),
                        self.note("codex1.md", "active", "0", credit_balance="60498")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        for summary, text, ask in self.pushed:
            self.assertLess(len(summary), 45)
            self.assertLess(len("Action: " + ask.action + "\n" + text), 500)
            self.assertTrue("natedev" in ask.action or "chatgpt.com/codex/settings/usage" in ask.action)
            self.assertLessEqual(text.count("recommended"), 1)

    def test_every_user_push_states_its_action_as_send_py_requires(self) -> None:
        _ = write_registry(quota_alert.REGISTRY, {"direct.claude": {"impl": "opus:ultra"}}, base_text=REGISTRY)
        claude = self.note("claude2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex1.md", "active", "0")
        spare = self.note("claude1.md", "inactive", "80", login="spare@example.invalid")
        sends = self.user_sends(lambda: self.alert([claude, codex, spare], self.now))
        sends |= self.user_sends(lambda: self.block([codex], self.now))
        notes = [title for title, (need, _action) in sends.items() if need == "note"]
        self.assertEqual(len(notes), 1)
        for title in notes:
            _ = sends.pop(title)
        self.assertEqual(sends, {
            "quota_alert: codex1 now uses paid credits": ("decision", ActionRequired(
                "To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex1.md on natedev")),
            "Codex work stopped for lack of usage": ("decision", ActionRequired(REFUSAL_ACTION)),
        })

    def test_user_delivery_names_success_process_failure_and_launch_failure(self) -> None:
        push = render("quota.claude_next_move", account="claude2", remaining="0%")
        for code, line, expected in ((0, "SENT: to the user", quota_alert.PushSent()),
                                     (3, "FAILED: channel unavailable", quota_alert.PushFailed("FAILED: channel unavailable"))):
            with self.subTest(code=code), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, line, "")):
                self.assertEqual(REAL_TELL_USER(push), expected)
        with mock.patch.object(subprocess, "run", side_effect=OSError("sender unavailable")):
            self.assertEqual(REAL_TELL_USER(push), quota_alert.PushFailed("sender unavailable"))

    def test_user_send_uses_configured_review_limit_without_shortening_agent_relays(self) -> None:
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        done = subprocess.CompletedProcess(["send.py"], 0, "SENT: to the user", "")
        with (mock.patch.object(review, "NOTIFY_CONFIG", config),
              mock.patch.object(subprocess, "run", return_value=done) as run):
            self.assertIsInstance(REAL_TELL_USER(render("quota.claude_next_move", account="claude2", remaining="0%")), quota_alert.PushSent)
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(float(command[command.index("--timeout") + 1]), review.user_send_seconds())
            self.assertEqual(run.call_args.kwargs["timeout"], review.user_send_seconds() + 15)
            self.assertNotIn("input", run.call_args.kwargs)
        self.assertEqual(quota_alert.RELAY_TIMEOUT, 40)

    def test_failed_codex_reminder_retries_phone_delivery_without_session_alerts(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        note = self.note("codex1.md", "active", "2")
        self.push_errors.append(quota_alert.PushFailed("fixture offline"))
        log = self.alert([note], self.now)
        self.assertIn("quota push codex1 -> user: not delivered: fixture offline", log)
        _ = self.alert([note], self.now + timedelta(minutes=2))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_legacy_blocked_cli_finds_the_canonical_episode_and_saves_no_spaced_label(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "1")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(state["episodes"]["codex1"].get("refusal_push"), self.now.isoformat(timespec="seconds"))
            # Only the episode's delivery marker may suppress this retry.
            _ = state.get("refusal_notices", {})[str(self.root / ".codex")].pop("push", None)
        self.pushed.clear()
        self.sent.clear()
        readings = self.alert_readings(notes, self.now)
        output = io.StringIO()

        with (mock.patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "someone@example.com", "codex 1"]),
              mock.patch.object(note_file, "read_notes", return_value=notes),
              mock.patch.object(quota_alert, "resolve_codex_readings", return_value=readings),
              redirect_stdout(output)):
            agent_notes.main()

        self.assertEqual(self.pushed, [], "the canonical episode already records its refusal push")
        self.assertEqual(self.sent, [], "the canonical episode must suppress another refusal delivery")
        self.assertNotIn("codex 1", output.getvalue())
        saved = cast(dict[str, object], json.loads(quota_alert.STATE.read_text()))
        notices = cast(dict[str, dict[str, object]], saved["refusal_notices"])
        self.assertEqual(notices[str(self.root / ".codex")]["label"], "codex1")

    def test_legacy_low_quota_episode_does_not_send_a_second_opening_alert(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "1")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        original = quota_alert.STATE.read_text()
        self.assertIn('"codex1":', original)
        _ = quota_alert.STATE.write_text(original.replace('"codex1":', '"codex 1":'))
        self.sent.clear()
        self.pushed.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(self.noted), 1)
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            self.assertNotIn("codex 1", state["episodes"])

    def test_legacy_fallback_owner_and_refusal_notice_load_canonically(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        _ = self.refuse()
        original = quota_alert.STATE.read_text()
        self.assertIn('"label": "codex1"', original)
        _ = quota_alert.STATE.write_text(original.replace('"label": "codex1"', '"label": "codex 1"'))
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.owner.label, "codex1")
            notices = state.get("refusal_notices", {})
            self.assertTrue(notices)
            for notice in notices.values():
                assert notice["kind"] == "known"
                self.assertEqual(notice["label"], "codex1")


if __name__ == "__main__":
    _ = unittest.main()
