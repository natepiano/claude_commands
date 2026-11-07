"""Quota alerts: delivery."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from datetime import timedelta
from typing import cast, override
from unittest import mock

from scripts.lib.test_models_registry import write_registry
from scripts.accounts.home_test_support import scratch_accounts_config
from scripts.message import send_budget
from scripts.shutdown import remote
from scripts.whoami import quota_alert_test_support as support
from scripts.whoami import quota_alert
from scripts.notify import review
from scripts.notify.push_texts import render
from scripts.whoami import agent_notes
from scripts.whoami import note_file
from scripts.notify.user_action import ActionRequired
from scripts.whoami.quota_alert_test_support import Notes, REAL_TELL_USER, REFUSAL_ACTION, REGISTRY as REGISTRY

setUpModule = support.setUpModule
REAL_RELAY = quota_alert.relay


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch("socket.gethostname", return_value="worker.example"))

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
            self.assertTrue(remote.local_machine() in ask.action or "chatgpt.com/codex/settings/usage" in ask.action)
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
                f"To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex1.md on {remote.local_machine()}")),
            "Codex work stopped for lack of usage": ("decision", ActionRequired(REFUSAL_ACTION)),
        })

    def test_user_delivery_names_success_process_failure_and_launch_failure(self) -> None:
        push = render("quota.claude_next_move", account="claude2", remaining="0%")
        for code, line, expected in ((0, "SENT: to the user", quota_alert.PushSent()),
                                     (6, "RETRY: active", quota_alert.PushFailed("RETRY: active")),
                                     (3, "FAILED: channel unavailable", quota_alert.PushFailed("stdout: FAILED: channel unavailable"))):
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
            self.assertEqual(run.call_args.kwargs["timeout"], review.user_send_seconds() + 140)
            self.assertNotIn("input", run.call_args.kwargs)
            self.assertIsNone(REAL_RELAY("fixture", "message", None))
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(command[command.index("--timeout") + 1], "30")
            self.assertEqual(run.call_args.kwargs["timeout"], send_budget.seconds(30))

    def test_relay_reads_the_current_account_timeout(self) -> None:
        config = self.enterContext(scratch_accounts_config()) / "accounts.conf"
        for name in ("send", "postmaster", "codex_daemon"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / "config" / f"{name}.conf", config.with_name(f"{name}.conf"))
        original = config.read_text()
        done = subprocess.CompletedProcess([], 0, "SENT: fixture", "")
        for limit in (40, 55):
            _ = config.write_text(original.replace("quota_relay_timeout_seconds=40", f"quota_relay_timeout_seconds={limit}"))
            with self.subTest(limit=limit), mock.patch.object(subprocess, "run", return_value=done) as run:
                self.assertIsNone(REAL_RELAY("fixture", "message", None))
                command = cast(list[str], run.call_args.args[0])
                self.assertEqual(command[command.index("--timeout") + 1], str(limit - quota_alert.KILL_GRACE))
                self.assertEqual(run.call_args.kwargs["timeout"], send_budget.seconds(limit - quota_alert.KILL_GRACE))

    def test_failed_codex_reminder_retries_phone_delivery_without_session_alerts(self) -> None:
        _ = quota_alert.CONFIG.write_text(quota_alert.CONFIG.read_text().replace("threshold_percent=1", "threshold_percent=2"))
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


class RelayOutcomeTests(unittest.TestCase):
    def test_user_delivery_requires_a_confirmed_success_outcome(self) -> None:
        push = render("quota.claude_next_move", account="claude2", remaining="0%")
        cases = ((0, "SENT: fixture", True), (0, "SKIPPED: repeated", True),
                 (0, "KEPT: silence", True), (0, "REPLACED: revision", True),
                 (1, "QUEUED: offline", False), (3, "FAILED: unavailable", False),
                 (2, "REFUSED: invalid", False), (4, "HELD: decision", False), (5, "PENDING: active", False),
                 (0, "", False), (0, "QUEUED: inconsistent", False),
                 (1, "SENT: inconsistent", False), (0, "FAILED: inconsistent", False))
        for code, text, accepted in cases:
            with self.subTest(code=code, text=text), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, text, "")):
                result = REAL_TELL_USER(push)
                self.assertIsInstance(result, quota_alert.PushSent if accepted else quota_alert.PushFailed)
                if isinstance(result, quota_alert.PushFailed):
                    self.assertNotEqual(result.why, "")

    def test_confirmed_relay_outcomes_preserve_success_and_queue_policies(self) -> None:
        for code, header, expected in ((0, "SENT", None), (0, "SKIPPED", None),
                                      (0, "KEPT", None), (0, "REPLACED", None),
                                      (1, "QUEUED", "QUEUED: fixture detail"),
                                      (6, "RETRY", "RETRY: fixture detail"),
                                      (3, "FAILED", "stdout: FAILED: fixture detail"),
                                      (2, "REFUSED", "stdout: REFUSED: fixture detail"),
                                      (4, "HELD", "stdout: HELD: fixture detail"),
                                      (5, "PENDING", "stdout: PENDING: fixture detail")):
            with self.subTest(header=header), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, header + ": fixture detail", "")):
                self.assertEqual(quota_alert.relay("fixture", "message", "fixture-key"), expected)

    def test_inconsistent_and_missing_relay_results_report_failure(self) -> None:
        for code, output in ((0, "QUEUED: offline"), (0, "FAILED: unavailable"), (1, "SENT: fixture"),
                             (0, ""), (1, ""), (-15, "SENT: fixture")):
            with self.subTest(code=code, output=output), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")):
                failure = quota_alert.relay("fixture", "message", "fixture-key")
                self.assertIsNotNone(failure)
                self.assertNotEqual(failure, "")


if __name__ == "__main__":
    _ = unittest.main()
