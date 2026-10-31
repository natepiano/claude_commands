"""Mac usage credentials belong to the exact folder selected at launch."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import Message
from threading import Barrier
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import final, override
from unittest.mock import patch

from scripts.whoami import agent_accounts, usage_store


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

def folder_service(folder: Path) -> str:
    """Independent vector from the service-name contract supplied by the director."""
    value = unicodedata.normalize("NFC", str(folder)).encode("utf-8")
    return "Claude Code-credentials-" + hashlib.sha256(value).hexdigest()[:8]


class ServiceKeychain:
    """A synthetic keychain that rejects every selector except a service name."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.after_delete: Callable[[], None] = lambda: None

    def run(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        argv = tuple(arguments)
        if Path(argv[0]).name == "ps":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if Path(argv[0]).name != "security":
            raise AssertionError("unexpected external command in isolated keychain test")
        if len(argv) < 4 or argv[2] != "-s":
            raise AssertionError("keychain item must be selected by service name only")
        action, service = argv[1], argv[3]
        if (action == "find-generic-password" and argv[4:] not in ((), ("-w",))
                or action == "delete-generic-password" and argv[4:]
                or action not in ("find-generic-password", "delete-generic-password")):
            raise AssertionError("unexpected keychain action or extra item selector")
        if action == "delete-generic-password" or not argv[4:]:
            if (_options.get("stdout") != subprocess.DEVNULL
                    or _options.get("stderr") != subprocess.DEVNULL):
                raise AssertionError("keychain mutations and presence probes must discard both streams")
        self.calls.append(argv)
        if service not in self.entries:
            return subprocess.CompletedProcess(argv, 44, "", "synthetic private diagnostic")
        if action == "find-generic-password":
            return subprocess.CompletedProcess(argv, 0, self.entries[service] if "-w" in argv else "", "")
        del self.entries[service]
        self.after_delete()
        return subprocess.CompletedProcess(argv, 0, "", "")


class NamedKeychainCredentialsTests(unittest.TestCase):
    home: Path = Path()
    keychain: ServiceKeychain = ServiceKeychain()
    output: io.StringIO = io.StringIO()
    errors: io.StringIO = io.StringIO()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        _ = self.enterContext(patch.object(sys, "platform", "darwin"))
        self.keychain = ServiceKeychain()
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.keychain.run))
        self.output, self.errors = io.StringIO(), io.StringIO()
        _ = self.enterContext(redirect_stdout(self.output))
        _ = self.enterContext(redirect_stderr(self.errors))
        self.addCleanup(self.assert_private_output)

    def assert_private_output(self) -> None:
        output = self.output.getvalue() + self.errors.getvalue()
        self.assertFalse(any(value in output for value in (
            "synthetic-named-token", "synthetic-home-token", "synthetic private diagnostic")))

    def test_named_folder_reads_only_its_hashed_service_with_unicode_normalization(self) -> None:
        # Decomposed Unicode distinguishes NFC hashing from hashing raw path bytes.
        folder = self.home / "cafe\u0301" / ".claude2"
        service = folder_service(folder)
        self.keychain.entries[service] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-named-token"}})
        self.keychain.entries["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-home-token"}})
        credentials = agent_accounts.claude_credentials(folder)
        self.assertTrue(credentials.get("claudeAiOauth", {}).get("accessToken") == "synthetic-named-token")
        self.assertEqual([call[3] for call in self.keychain.calls], [service])

    def test_missing_named_entry_raises_without_reading_legacy_credentials(self) -> None:
        folder = self.home / ".claude2"
        self.keychain.entries["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-home-token"}})
        with self.assertRaisesRegex(RuntimeError, "usage credentials unavailable"):
            _ = agent_accounts.claude_credentials(folder)
        self.assertEqual([call[3] for call in self.keychain.calls], [folder_service(folder)])

    def test_empty_config_setting_reads_the_legacy_keychain_service(self) -> None:
        self.keychain.entries["Claude Code-credentials"] = json.dumps({
            "claudeAiOauth": {"accessToken": "synthetic-home-token"}})
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            credentials = agent_accounts.claude_credentials()
        self.assertTrue(credentials.get("claudeAiOauth", {}).get("accessToken") == "synthetic-home-token")
        self.assertEqual([call[3] for call in self.keychain.calls], ["Claude Code-credentials"])

    def test_implicit_folder_uses_the_callers_exact_config_setting(self) -> None:
        folder = self.home / ".claude3"
        service = folder_service(folder)
        self.keychain.entries[service] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-named-token"}})
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(folder)}):
            credentials = agent_accounts.claude_credentials()
        self.assertTrue(credentials.get("claudeAiOauth", {}).get("accessToken") == "synthetic-named-token")
        self.assertEqual([call[3] for call in self.keychain.calls], [service])

    def test_keychain_entry_without_a_login_token_never_sends_a_usage_request(self) -> None:
        folder = self.home / ".claude2"
        folder.mkdir()
        _ = (folder / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "idle@example.invalid"}}))
        entries = (agent_accounts.ClaudeOauth(subscriptionType="max", expiresAt=1),
                   agent_accounts.ClaudeOauth(subscriptionType="max", expiresAt=1, accessToken=""),
                   agent_accounts.ClaudeOauth(subscriptionType="max"))
        for oauth in entries:
            with self.subTest(oauth=oauth):
                self.keychain.entries[folder_service(folder)] = json.dumps({"claudeAiOauth": oauth})
                with (patch.object(urllib.request, "urlopen", side_effect=AssertionError("missing token sent")) as http,
                      patch.object(usage_store, "claim_read") as claim):
                    report = agent_accounts.claude_live(folder, use_disk_identity=True)
                self.assertEqual(report.usage_read_outcome, agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN)
                self.assertIn("no login token", report.quota_problem or "")
                self.assertEqual(report.quotas, [])
                http.assert_not_called()
                claim.assert_not_called()
                usage_store.save([report], datetime.now(timezone.utc))
                self.assertEqual(usage_store.latest_read_outcome(usage_store.load()["idle@example.invalid"]),
                                 agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN)


@final
class SharedUsageReadTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = Path()
        self.now = datetime(2026, 10, 19, 12, 11, tzinfo=timezone.utc)
        self.claim = usage_store.claim_read

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        self.now = datetime(2026, 10, 19, 12, 11, tzinfo=timezone.utc)
        _ = self.enterContext(patch.object(agent_accounts, "command_json", return_value={
            "loggedIn": True, "email": "shared@example.invalid", "authMethod": "claude.ai"}))
        _ = self.enterContext(patch.object(agent_accounts, "claude_credentials", return_value={
            "claudeAiOauth": {"accessToken": "fixture"}}))
        self.claim = usage_store.claim_read
        _ = self.enterContext(patch.object(usage_store, "claim_read", side_effect=self.claim_now))

    def claim_now(self, login: str, _now: datetime) -> agent_accounts.UsageReadClaim | agent_accounts.UsageReadHeld:
        return self.claim(login, self.now)

    def read(self) -> agent_accounts.Report:
        return agent_accounts.claude_live(self.home / ".claude2")

    def response(self, _request: urllib.request.Request, *, timeout: int) -> io.BytesIO:
        self.assertEqual(timeout, 15)
        return io.BytesIO(b'{"seven_day":{"utilization":25,"resets_at":"2026-10-25T12:00:00Z"}}')

    def test_the_http_call_does_not_hold_the_store_lock(self) -> None:
        def unlocked_response(request: urllib.request.Request, *, timeout: int) -> io.BytesIO:
            with usage_store.store_file().with_name("usage.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            return self.response(request, timeout=timeout)

        with patch.object(urllib.request, "urlopen", side_effect=unlocked_response):
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.OK)

    def test_named_live_read_uses_disk_identity_without_starting_claude(self) -> None:
        folder = self.home / ".claude2"
        folder.mkdir()
        _ = (folder / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "shared@example.invalid"}}))
        with patch.object(agent_accounts, "claude_credentials", return_value={
                "claudeAiOauth": {"accessToken": "fixture", "subscriptionType": "max"}}), \
             patch.object(agent_accounts, "command_json", side_effect=AssertionError("CLI started")) as cli, \
             patch.object(urllib.request, "urlopen", side_effect=self.response):
            report = agent_accounts.claude_live(folder, use_disk_identity=True)
        cli.assert_not_called()
        self.assertEqual(report.email, "shared@example.invalid")
        self.assertEqual(report.plan, "max")
        self.assertEqual(report.weekly_remaining, 75)

    def test_missing_disk_subscription_keeps_cli_plan_label(self) -> None:
        folder = self.home / ".claude2"
        folder.mkdir()
        _ = (folder / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "shared@example.invalid"}}))
        with (patch.object(agent_accounts, "command_json", return_value={
                "loggedIn": True, "email": "shared@example.invalid", "authMethod": "claude.ai",
                "subscriptionType": "max"}) as cli,
              patch.object(urllib.request, "urlopen", side_effect=self.response)):
            report = agent_accounts.claude_live(folder, use_disk_identity=True)
        cli.assert_called_once()
        self.assertEqual(report.plan, "max")
        self.assertEqual(report.weekly_remaining, 75)

    def test_unreadable_disk_credentials_keeps_cli_plan_label(self) -> None:
        folder = self.home / ".claude2"
        folder.mkdir()
        _ = (folder / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "shared@example.invalid"}}))
        with (patch.object(agent_accounts, "claude_credentials", side_effect=OSError("fixture")),
              patch.object(agent_accounts, "command_json", return_value={
                "loggedIn": True, "email": "shared@example.invalid", "authMethod": "claude.ai",
                "subscriptionType": "max"}) as cli):
            report = agent_accounts.claude_live(folder, use_disk_identity=True)
        cli.assert_called_once()
        self.assertEqual(report.plan, "max")

    def test_named_disk_subscription_reads_credentials_once(self) -> None:
        folder = self.home / ".claude2"
        folder.mkdir()
        _ = (folder / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "shared@example.invalid"}}))
        with (patch.object(agent_accounts, "claude_credentials", return_value={
                "claudeAiOauth": {"accessToken": "fixture", "subscriptionType": "max"}}) as credentials,
              patch.object(urllib.request, "urlopen", side_effect=self.response)):
            report = agent_accounts.claude_live(folder, use_disk_identity=True)
        credentials.assert_called_once_with(folder)
        self.assertEqual(report.plan, "max")
        self.assertEqual(report.weekly_remaining, 75)

    def test_threads_share_one_read_then_a_read_after_ninety_seconds_runs(self) -> None:
        start = Barrier(2)

        def read_together() -> agent_accounts.Report:
            _ = start.wait(timeout=TEST_HANG_GUARD_SECS)
            return self.read()

        with patch.object(urllib.request, "urlopen", side_effect=self.response) as http:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(read_together), pool.submit(read_together)
                reports = [first.result(timeout=TEST_HANG_GUARD_SECS), second.result(timeout=TEST_HANG_GUARD_SECS)]
            self.assertCountEqual([report.usage_read_outcome for report in reports],
                [agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD])
            held = next(report for report in reports if report.usage_read_outcome == agent_accounts.UsageReadOutcome.HELD)
            self.assertEqual(held.quotas, [])
            self.assertIsNone(held.quota_problem)
            http.assert_called_once()
            self.now += timedelta(seconds=91)
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.OK)
            self.assertEqual(http.call_count, 2)
        stored = usage_store.load()["shared@example.invalid"]
        self.assertEqual(stored.get("attempted_at"), self.now.isoformat())
        self.assertEqual(stored.get("attempt_outcome"), agent_accounts.UsageReadOutcome.OK)

    def test_a_second_process_holds_a_claim_made_by_this_reader(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=self.response):
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.OK)
        script = """
from pathlib import Path
from unittest.mock import patch
from scripts.whoami import agent_accounts
with (patch.object(agent_accounts, 'command_json', return_value={
        'loggedIn': True, 'email': 'shared@example.invalid', 'authMethod': 'claude.ai'}),
      patch.object(agent_accounts, 'claude_credentials', return_value={
        'claudeAiOauth': {'accessToken': 'fixture'}}),
      patch('urllib.request.urlopen', side_effect=AssertionError('held read reached HTTP'))):
    report = agent_accounts.claude_live(Path.home() / '.claude3')
    assert report.usage_read_outcome == agent_accounts.UsageReadOutcome.HELD
    assert not report.quotas and report.quota_problem is None
"""
        # The child uses the same synthetic time as the parent's claim.
        script = ("from datetime import datetime, timezone\nfrom scripts.whoami import usage_store\n"
                  + "claim = usage_store.claim_read\n"
                  + f"usage_store.claim_read = lambda login, now: claim(login, datetime.fromisoformat({self.now.isoformat()!r}))\n"
                  + script)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rate_limit_holds_for_ten_minutes_even_when_the_caller_does_not_save(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                "https://fixture.invalid", 429, "fixture", Message(), None)) as http:
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.RATE_LIMITED_429)
            self.now += timedelta(minutes=9)
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.HELD)
            http.assert_called_once()
        with patch.object(urllib.request, "urlopen", side_effect=self.response) as http:
            self.now += timedelta(minutes=1)
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.OK)
            http.assert_called_once()

    def test_http_errors_are_kept_without_a_later_save(self) -> None:
        for code, outcome in ((401, agent_accounts.UsageReadOutcome.REFUSED_401),
                              (503, agent_accounts.UsageReadOutcome.ERROR)):
            with self.subTest(code=code):
                self.now += timedelta(minutes=11)
                with patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                        "https://fixture.invalid", code, "fixture", Message(), None)):
                    _ = self.read()
                stored = usage_store.load()["shared@example.invalid"]
                self.assertEqual(stored.get("attempt_outcome"), outcome)
                self.assertEqual(stored.get("attempted_at"), self.now.isoformat())

    def test_a_late_save_keeps_a_newer_failure_from_live_reads(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=self.response):
            good = self.read()
        self.now += timedelta(seconds=91)
        with patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                "https://fixture.invalid", 429, "fixture", Message(), None)):
            _ = self.read()
        before = usage_store.store_file().read_bytes()
        usage_store.save([good], self.now, {"claude2": "shared@example.invalid"})
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        self.assertEqual(usage_store.load()["shared@example.invalid"].get("read_outcome"),
                         agent_accounts.UsageReadOutcome.RATE_LIMITED_429)
        self.assertTrue(usage_store.label_file().exists())

    def test_a_claim_store_error_still_returns_the_live_read(self) -> None:
        for error in (OSError("disk full"), ValueError("bad store")):
            with (self.subTest(error=type(error).__name__),
                  patch.object(usage_store, "claim_read", side_effect=error),
                  patch.object(usage_store, "finish_read") as finish,
                  patch.object(urllib.request, "urlopen", side_effect=self.response) as http,
                  redirect_stderr(io.StringIO()) as errors):
                report = self.read()
            self.assertEqual(report.usage_read_outcome, agent_accounts.UsageReadOutcome.OK)
            self.assertEqual(report.quotas[0].remaining_percent, 75)
            self.assertIsNone(report.quota_problem)
            http.assert_called_once()
            finish.assert_not_called()
            self.assertEqual(errors.getvalue().splitlines(),
                [f"usage store: could not claim the read; {error}; read without a hold"])

    def test_a_finish_store_error_keeps_the_good_live_report(self) -> None:
        for error in (OSError("disk full"), ValueError("bad store")):
            self.now += timedelta(minutes=2)
            with (self.subTest(error=type(error).__name__),
                  patch.object(usage_store, "finish_read", side_effect=error),
                  patch.object(urllib.request, "urlopen", side_effect=self.response) as http,
                  redirect_stderr(io.StringIO()) as errors):
                report = self.read()
            self.assertEqual(report.usage_read_outcome, agent_accounts.UsageReadOutcome.OK)
            self.assertEqual(report.quotas[0].remaining_percent, 75)
            self.assertIsNone(report.quota_problem)
            http.assert_called_once()
            self.assertEqual(errors.getvalue().splitlines(),
                [f"usage store: could not save the read; {error}; keep the live report"])

    def test_stale_token_makes_no_attempt(self) -> None:
        with (patch.object(agent_accounts, "claude_credentials", return_value={
                "claudeAiOauth": {"accessToken": "fixture", "expiresAt": 1}}),
              patch.object(urllib.request, "urlopen", side_effect=AssertionError("stale token sent"))):
            self.assertEqual(self.read().usage_read_outcome, agent_accounts.UsageReadOutcome.STALE_TOKEN)
        self.assertFalse(usage_store.store_file().exists())


if __name__ == "__main__":
    _ = unittest.main()


class CodexAllowanceRoomTests(unittest.TestCase):
    def test_positive_weekly_usage_without_a_reset_can_serve_work(self) -> None:
        now = datetime.now(timezone.utc)
        for left in (0, 100):
            with self.subTest(left=left):
                report = agent_accounts.Report("Codex", quotas=[
                    agent_accounts.Quota("Weekly", 100 - left, None)])
                result = agent_accounts.codex_can_work(report, now, agent_accounts.CreditsOff())
                self.assertIsInstance(result, agent_accounts.CodexWorkAvailable if left else
                                      agent_accounts.CodexWorkExhausted)
