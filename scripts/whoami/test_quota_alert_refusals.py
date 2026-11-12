"""Quota alerts: refusals."""

from __future__ import annotations

from scripts.lib.test_models_registry import write_registry

import os
import fcntl
import json
import io
import shutil
import socket
import subprocess
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from typing import cast
from unittest import mock
from scripts.whoami import quota_alert_test_support as support
from scripts.whoami import quota_alert
from scripts.whoami import agent_accounts
from scripts.accounts import refusals, role_map
from scripts.whoami.quota_alert_test_support import Notes, REFUSAL_ACTION, REGISTRY as REGISTRY

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_no_capacity_announces_once_after_a_zero_episode_without_holding_the_state_lock(self) -> None:
        for hostname, enabled in ((role_map.ROLE_MAP_HOST + ".local", True), ("other-host.local", False)):
            with self.subTest(hostname=hostname):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.seed, self.root)
                notes: Notes = [self.note("codex1.md", "active", "0")]
                _ = self.alert(notes, self.now)
                self.assertEqual(self.broadcasts, [])
                calls: list[list[str]] = []

                def record_broadcast(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                    del options
                    with quota_alert.STATE.with_name(f".{quota_alert.STATE.name}.lock").open() as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
                    self.assertTrue(saved.get("no_worker_mode", False))
                    self.assertIn("--no-block", arguments)
                    calls.append(arguments[arguments.index("--from"):])
                    return subprocess.CompletedProcess(arguments, 0, "", "")

                with mock.patch.object(socket, "gethostname", return_value=hostname), \
                     mock.patch("scripts.whoami.quota_alert.subprocess.run", side_effect=record_broadcast):
                    _ = self.block(notes, self.now)
                    _ = self.block(notes, self.now + timedelta(minutes=1))
                if enabled:
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0][2::2], ["--showrunners", "--units"])
                    self.assertIn("Send no assign.", calls[0][3])
                    self.assertEqual(calls[0][3], calls[0][5])
                    with quota_alert.state_file() as state:
                        self.assertTrue(state.get("no_worker_mode", False))
                    self.assertEqual(len(calls), 1)
                else:
                    self.assertEqual(calls, [])
                    with quota_alert.state_file() as state:
                        self.assertFalse(state.get("no_worker_mode", False))

    def test_failed_notice_start_logs_once_and_keeps_the_refusal_pass_running(self) -> None:
        for failure, detail in ((7, "exit 7"), (OSError("manager unavailable"), "manager unavailable")):
            with self.subTest(failure=detail):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.seed, self.root)
                notes: Notes = [self.note("codex1.md", "active", "0")]
                output = io.StringIO()

                def fail_start(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                    del options
                    if isinstance(failure, OSError):
                        raise failure
                    return subprocess.CompletedProcess(arguments, failure, "", "")

                with redirect_stdout(output), mock.patch("scripts.whoami.quota_alert.subprocess.run", side_effect=fail_start) as start:
                    log = self.block(notes, self.now)
                    _ = self.block(notes, self.now + timedelta(minutes=1))
                self.assertEqual(output.getvalue().splitlines(), [f"quota-alert: no-worker mode broadcast failed: {detail}"])
                self.assertEqual(start.call_count, 1)
                self.assertIn("quota status codex1: Codex refused work on codex1. No credit balance is reported for codex1. "
                              + "New work moved to Claude, the other coding assistant.", log)
                self.assertEqual(self.assignments()["direct"], "claude")
                with quota_alert.state_file() as state:
                    self.assertTrue(state.get("no_worker_mode", False))

    def test_explicit_refusal_time_is_recorded_without_replacing_it_with_pass_time(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        earlier = self.now - timedelta(minutes=1)
        _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
            refused_at=quota_alert.RefusedAt(earlier), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, earlier)

    def test_unknown_refusal_time_uses_pass_time_and_ignores_inherited_handoff_environment(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        with mock.patch.dict(os.environ, {"ACCOUNT_REFUSAL_AT": (self.now - timedelta(days=1)).isoformat()}):
            _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
                refused_at=quota_alert.RefusalTimeUnknown(), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, self.now)

    def test_codex_reaching_the_threshold_switches_nothing_and_only_reminds_the_phone(self) -> None:
        log = self.alert([self.note("codex1.md", "active", "1")], self.now)
        self.assertEqual([line for line in log if "switch" in line], [])
        self.assertEqual(self.assignments(),
                         {"direct": "codex", "cli": "claude", "design_roles": "codex"})
        self.assertEqual(self.sent, [])
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_failed_refusal_push_retries_on_the_next_refusal_then_stops(self) -> None:
        _ = write_registry(quota_alert.REGISTRY, {"direct.claude": {"impl": "opus:ultra"}}, base_text=REGISTRY)
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        self.push_errors.append(quota_alert.PushFailed("phone offline"))
        first = self.block(notes, self.now + timedelta(minutes=1))
        self.assertIn("quota refusal codex1 -> user: not delivered: phone offline", first)
        second = self.block(notes, self.now + timedelta(minutes=2))
        self.assertIn("quota refusal codex1 -> user: sent", second)
        third = self.block(notes, self.now + timedelta(minutes=3))
        self.assertFalse(any(line.endswith("-> user: sent") for line in third))
        self.assertEqual(len(self.pushed), 2)

    def test_a_refused_switch_still_alerts_and_says_why(self) -> None:
        registry = quota_alert.REGISTRY
        _ = write_registry(registry, {"direct.claude": {"impl": "opus:ultra"}}, base_text=REGISTRY)
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now)
        self.assertTrue(any(line.startswith("switch to claude for codex1 failed: ERROR: [direct.impl] effort 'ultra'") for line in log))
        self.assertIn("quota refusal codex1 -> user: sent", log)
        self.assertEqual(self.pushed[0][0::2], ("Codex work stopped for lack of usage", quota_alert.Ask("decision", REFUSAL_ACTION)))
        self.assertIn("Moving those jobs to Claude", self.pushed[0][1])
        second = self.block(notes, self.now)
        self.assertFalse(any(line.startswith("quota push") for line in second))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            failed = state.get("refusal_notices", {})[str(self.root / ".codex")].get("switch_failed")
        self.assertIsInstance(failed, str)
        # A registry fixed mid-episode is not switched by the next run.
        _ = write_registry(registry, base_text=REGISTRY)
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["direct"], "codex")
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(minutes=61))
        self.assertEqual(len(self.broadcasts), 2)
        self.assertIn("Use workers again.", self.broadcasts[1][3])
        with quota_alert.state_file() as state:
            self.assertFalse(state.get("no_worker_mode", False))


if __name__ == "__main__":
    _ = unittest.main()
