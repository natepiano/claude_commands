"""Codex home discovery, live read isolation, and account display."""

from __future__ import annotations

import sys
import asyncio
import base64
import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import cast, override
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.whoami.note_file import note_name
from scripts.accounts import accounts, role_map
from scripts.production import showrunners

from scripts.whoami import agent_accounts
from scripts.whoami import note_file
import whoami
from scripts.whoami.agent_accounts import (
    CheckedNamedClaudeAccount,
    ClaudeAccountsInUse,
    CodexAccountResult,
    CodexHomeSource,
    CodexRateLimits,
    NumberedAccount,
    Report,
    UnreadableIdentity,
)


class CodexHomesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment_context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home), "PATH": os.defpath,
        }, clear=True)))
        del environment_context
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.home))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.home / "notifier"))

    def test_home_role_label_uses_the_login_note_number_or_the_unnoted_folder(self) -> None:
        home = self.select_workers("codex2")
        _ = home.rename(self.home / ".codex")
        self.assertEqual(agent_accounts.codex_home_role_label(self.home / ".codex"), "codex2")
        (self.home / "codex2.md").unlink()
        self.assertEqual(agent_accounts.codex_home_role_label(self.home / ".codex"), "~/.codex")

    def test_numbered_account_resolves_the_home_holding_its_login_and_a_setup_home_otherwise(self) -> None:
        home = self.select_workers("codex2")
        _ = home.rename(self.home / ".codex")
        resolved = agent_accounts.codex_account_home("codex2")
        self.assertIsInstance(resolved, agent_accounts.CodexHomeHoldingLogin)
        assert isinstance(resolved, agent_accounts.CodexHomeHoldingLogin)
        self.assertEqual((resolved.label, resolved.home, resolved.login),
                         ("codex2", self.home / ".codex", "selected@example.invalid"))
        self.assertEqual(agent_accounts.workers_home(), self.home / ".codex")
        _ = (self.home / "codex1.md").write_text("---\nlogin: next@example.invalid\n---\n")
        missing = agent_accounts.codex_account_home("codex1")
        self.assertIsInstance(missing, agent_accounts.CodexSetupHome)
        self.assertEqual((missing.label, missing.home), ("codex1", self.home / ".codex1"))

    def test_home_matches_its_numbered_account_but_refuses_the_default_alias(self) -> None:
        """Numbered identities identify the home; new input cannot use the old alias."""
        home = self.select_workers("codex2")
        _ = home.rename(self.home / ".codex")
        current = self.home / ".codex"
        self.assertFalse(agent_accounts.codex_home_matches_label(current, "default"))
        self.assertTrue(agent_accounts.codex_home_matches_label(current, "codex2"))
        other = self.select_workers("codex1")
        self.assertFalse(agent_accounts.codex_home_matches_label(other, "default"))
        self.assertFalse(agent_accounts.codex_home_matches_label(other, "codex2"))
        self.assertFalse(agent_accounts.codex_home_matches_label(current, "codex1"))

    def test_credit_setting_reads_on_off_and_defaults_to_on(self) -> None:
        path = self.home / "codex1.md"
        for field, expected in (("use_credits: off\n", agent_accounts.CreditsOff),
                                ("use_credits: on\n", agent_accounts.CreditsOn),
                                ("", agent_accounts.CreditsOn),
                                ("use_credits: sometimes\n", agent_accounts.CreditsOn)):
            with self.subTest(field=field):
                _ = path.write_text("---\nlogin: next@example.invalid\n" + field + "---\n")
                self.assertIsInstance(agent_accounts.codex_credits_setting("codex1"), expected)

    def test_credit_setting_off_disallows_paid_room_in_either_exhausted_window(self) -> None:
        now = datetime.now(timezone.utc)
        for weekly_used, five_used in ((100, 10), (10, 100), (100, 100)):
            for balance in ("6370", "unlimited"):
                with self.subTest(weekly=weekly_used, five=five_used, balance=balance):
                    report = Report("Codex", quotas=[agent_accounts.Quota("Weekly", weekly_used, now + timedelta(days=1))],
                        five_hour=agent_accounts.Quota("5-hour", five_used, now + timedelta(hours=1)), credit_balance=balance)
                    self.assertIsInstance(agent_accounts.codex_can_work(report, now, agent_accounts.CreditsOff()),
                                          agent_accounts.CodexWorkExhausted)
                    self.assertIsInstance(agent_accounts.codex_can_work(report, now, agent_accounts.CreditsOn()),
                                          agent_accounts.CodexWorkAvailable)

    def select_workers(self, label: str) -> Path:
        home = self.home / ("." + note_name(label).removesuffix(".md"))
        home.mkdir(exist_ok=True)
        login = "selected@example.invalid" if label == "codex2" else "next@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (self.home / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "workers", label]), 0)
        return home

    def test_legacy_codex_note_alone_keeps_login_resolution_and_the_user_credit_setting(self) -> None:
        home = self.select_workers("codex2")
        numbered = self.home / "codex2.md"
        legacy = self.home / "codex 2.md"
        _ = numbered.rename(legacy)
        _ = legacy.write_text("---\nlogin: selected@example.invalid\nuse_credits: off\n---\n")
        identity = agent_accounts.codex_account(home)
        self.assertIsInstance(identity, agent_accounts.KnownCodexAccount)
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        self.assertEqual(identity.label, "codex2")
        self.assertEqual(agent_accounts.codex_account_home("codex2").home, home)
        self.assertIsInstance(agent_accounts.codex_credits_setting("codex2"), agent_accounts.CreditsOff)
        self.assertFalse(numbered.exists(), "readers must not migrate files")

    def legacy_selection(self) -> ClaudeAccountsInUse:
        return ClaudeAccountsInUse(UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset())

    def live_fixture(self, home: Path) -> Report:
        return Report("Codex", email=f"{home.name}@example.invalid", source=CodexHomeSource(home))

    def test_shared_user_zone_uses_production_or_the_users_zone_setting(self) -> None:
        runner = showrunners.Showrunner(session="fixture", socket="", slug="fixture", zone="Asia/Tokyo", doc="")
        with patch.object(showrunners, "registered_showrunners", return_value=[runner]):
            self.assertEqual(agent_accounts.user_zone(), ZoneInfo("Asia/Tokyo"))
        with patch.dict(os.environ, {"USER_ZONE": "Europe/Paris"}):
            with patch.object(showrunners, "registered_showrunners", return_value=[]):
                self.assertEqual(agent_accounts.user_zone(), ZoneInfo("Europe/Paris"))
            with patch.object(showrunners, "registered_showrunners", side_effect=OSError("records unreadable")):
                self.assertEqual(agent_accounts.user_zone(), ZoneInfo("Europe/Paris"))

    def test_shared_user_zone_uses_the_setting_when_production_cannot_import(self) -> None:
        with patch.dict("sys.modules", {"scripts.production": None}), patch.dict(os.environ, {"USER_ZONE": "Europe/Paris"}):
            self.assertEqual(agent_accounts.user_zone(), ZoneInfo("Europe/Paris"))

    def test_discovery_includes_default_even_when_no_codex_home_exists(self) -> None:
        self.assertEqual(agent_accounts.codex_homes(self.home / ".codex"), [self.home / ".codex"])
        self.assertEqual(agent_accounts.codex_home(), self.home / ".codex")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_discovery_orders_existing_numbered_directories_numerically(self) -> None:
        for name in (".codex10", ".codex2", ".codex1", ".codex-other"):
            (self.home / name).mkdir()
        _ = (self.home / ".codex3").write_text("a file")

        self.assertEqual(agent_accounts.codex_homes(self.home / ".codex"), [
            self.home / name for name in (".codex", ".codex1", ".codex2", ".codex10")
        ])

    def test_discovery_includes_the_home_in_use_once(self) -> None:
        numbered = self.home / ".codex2"
        numbered.mkdir()
        for selected, expected in (
            (numbered, [self.home / ".codex", numbered]),
            (self.home / "custom codex", [self.home / ".codex", numbered, self.home / "custom codex"]),
        ):
            with self.subTest(selected=selected), patch.dict(os.environ, {"CODEX_HOME": str(selected)}):
                self.assertEqual(agent_accounts.codex_home(), selected)
                self.assertEqual(agent_accounts.codex_homes(selected), expected)

    def test_live_read_sets_the_child_home_and_retains_report_source(self) -> None:
        selected = self.home / ".codex2"
        process = MagicMock(spec=asyncio.subprocess.Process)
        process.returncode = 0
        identity: CodexAccountResult = {
            "account": {"type": "chatgpt", "email": "spare@example.invalid", "planType": "pro"},
        }
        usage: CodexRateLimits = {"rateLimits": {
            "secondary": {"usedPercent": 12, "windowDurationMins": 10080},
            "primary": {"usedPercent": 80, "windowDurationMins": 300},
            "credits": {"balance": "23", "hasCredits": True},
        }}
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex"), "RETAIN_ENV": "fixture"}),
            patch.object(agent_accounts, "executable", return_value="fixture-codex"),
            patch.object(asyncio, "create_subprocess_exec", return_value=process) as launch,
            patch.object(agent_accounts.CodexServer, "request", side_effect=[{}, identity, usage]),
            patch.object(agent_accounts.CodexServer, "send"),
        ):
            before = dict(os.environ)
            report = asyncio.run(agent_accounts.codex_live(selected))
            self.assertEqual(dict(os.environ), before)

        self.assertEqual(launch.call_args.args, ("fixture-codex", "app-server", "--listen", "stdio://"))
        environment = cast(dict[str, str], launch.call_args.kwargs["env"])
        self.assertEqual(environment, {**before, "CODEX_HOME": str(selected)})
        self.assertEqual(report.source, CodexHomeSource(selected))
        self.assertEqual(report.email, "spare@example.invalid")
        self.assertEqual(report.weekly_remaining, 88)
        self.assertIsNotNone(report.five_hour)
        self.assertEqual(report.credit_balance, "23")

    def test_logged_out_live_report_still_names_its_home(self) -> None:
        selected = self.home / ".codex2"
        process = MagicMock(spec=asyncio.subprocess.Process)
        process.returncode = 0
        with (
            patch.object(agent_accounts, "executable", return_value="fixture-codex"),
            patch.object(asyncio, "create_subprocess_exec", return_value=process),
            patch.object(agent_accounts.CodexServer, "request", side_effect=[{}, {"account": None}]),
            patch.object(agent_accounts.CodexServer, "send"),
        ):
            report = asyncio.run(agent_accounts.codex_live(selected))
        self.assertEqual(report.source, CodexHomeSource(selected))
        self.assertEqual(report.problem, "Not logged in")

    def test_failed_live_report_still_names_its_home(self) -> None:
        selected = self.home / ".codex2"
        with (
            patch.object(agent_accounts, "executable", return_value="fixture-codex"),
            patch.object(asyncio, "create_subprocess_exec", side_effect=OSError("fixture")),
        ):
            report = asyncio.run(agent_accounts.codex_live(selected))
        self.assertEqual(report.source, CodexHomeSource(selected))
        self.assertIsNotNone(report.problem)

    def test_live_reports_reads_each_home_in_order_in_both_claude_branches(self) -> None:
        for name in (".codex10", ".codex2"):
            (self.home / name).mkdir()
        default = NumberedAccount("claude1", Path.home() / ".claude", "main@example.invalid")
        named = CheckedNamedClaudeAccount("claude2", self.home / ".claude2",
            NumberedAccount("claude2", self.home / ".claude2", "unit@example.invalid"))
        selections = (
            self.legacy_selection(),
            ClaudeAccountsInUse(default, True, (named,), frozenset((named.identity.login,))),
        )
        homes = [self.home / name for name in (".codex", ".codex2", ".codex10")]
        for selection in selections:
            with (
                self.subTest(multiple=selection.multiple_accounts),
                patch.object(agent_accounts, "claude_live", return_value=Report("Claude")) as claude,
                patch.object(agent_accounts, "codex_live", side_effect=self.live_fixture) as codex,
            ):
                reports = asyncio.run(agent_accounts.live_reports(selection, self.home / ".codex"))
            self.assertEqual([report.source for report in reports if report.tool == "Codex"],
                [CodexHomeSource(home) for home in homes])
            self.assertCountEqual(codex.await_args_list, [call(home) for home in homes])
            if selection.multiple_accounts:
                self.assertCountEqual(claude.call_args_list, [call(named.directory, use_disk_identity=True)])
            else:
                claude.assert_not_called()

    def test_whoami_prints_each_codex_account_and_marks_only_the_selected_home(self) -> None:
        default = self.home / ".codex"
        selected = self.home / ".codex2"
        _ = self.select_workers("codex2")
        for label, login in (("codex1", "default@example.invalid"), ("codex2", "selected@example.invalid")):
            _ = (self.home / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
        reports = [
            Report("Codex", email="default@example.invalid", source=CodexHomeSource(default)),
            Report("Codex", email="selected@example.invalid", source=CodexHomeSource(selected)),
        ]
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
            patch.object(whoami, "live_reports", return_value=reports),
            redirect_stdout(output),
        ):
            asyncio.run(whoami.main())
        blocks = output.getvalue().strip().split("\n\n")
        self.assertEqual(len(blocks), 2)
        self.assertIn("Account: codex1 — default@example.invalid", blocks[0])
        self.assertNotIn("(workers)", blocks[0])
        self.assertIn("Account: codex2 (workers) — selected@example.invalid", blocks[1])

    def test_whoami_with_one_codex_home_keeps_the_original_output(self) -> None:
        _ = (self.home / "codex1.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        report = Report("Codex", email="workers@example.invalid", source=CodexHomeSource(self.home / ".codex"))
        self.assertEqual(whoami.render(report, self.legacy_selection(), self.home / ".codex"), [
            "Codex", "  Account: codex1 — workers@example.invalid", "  Plan: unavailable",
            "  Limit resets available: unavailable",
        ])

    def test_whoami_marks_the_selected_block_when_its_live_read_fails(self) -> None:
        default = self.home / ".codex"
        selected = self.home / ".codex2"
        _ = self.select_workers("codex2")
        reports = [
            Report("Codex", problem="default unavailable", source=CodexHomeSource(default)),
            Report("Codex", problem="selected unavailable", source=CodexHomeSource(selected)),
        ]
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(selected)}),
            patch.object(whoami, "live_reports", return_value=reports),
            redirect_stdout(output),
        ):
            asyncio.run(whoami.main())
        blocks = output.getvalue().strip().split("\n\n")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks, [
            "Codex (~/.codex)\n  default unavailable",
            "Codex (~/.codex2, workers)\n  selected unavailable",
        ])

    def test_whoami_names_the_logged_out_spare_and_selected_codex_homes(self) -> None:
        default = self.home / ".codex"
        spare = self.home / ".codex1"
        spare.mkdir()
        reports = [
            Report("Codex", problem="Not logged in", source=CodexHomeSource(default)),
            Report("Codex", problem="Not logged in", source=CodexHomeSource(spare)),
        ]
        blocks = ["\n".join(whoami.render(report, self.legacy_selection(), default)) for report in reports]
        self.assertEqual(blocks, [
            "Codex (~/.codex, workers)\n  Not logged in",
            "Codex (~/.codex1)\n  Not logged in",
        ])

    def test_whoami_names_a_home_when_the_account_email_is_unavailable(self) -> None:
        spare = self.home / ".codex1"
        spare.mkdir()
        report = Report("Codex", source=CodexHomeSource(spare))
        self.assertEqual(whoami.render(report, self.legacy_selection(), self.home / ".codex"), [
            "Codex (~/.codex1)", "  Account: email unavailable", "  Plan: unavailable",
            "  Limit resets available: unavailable",
        ])

    def test_whoami_uses_the_full_path_for_an_accountless_home_outside_user_home(self) -> None:
        selected = self.home.parent / f"{self.home.name}-codex"
        report = Report("Codex", problem="Not logged in", source=CodexHomeSource(selected))
        with patch.dict(os.environ, {"CODEX_HOME": str(selected)}):
            self.assertEqual(whoami.render(report, self.legacy_selection(), selected), [
                f"Codex ({selected}, workers)", "  Not logged in",
            ])

    def test_whoami_with_one_failed_codex_home_keeps_the_original_output(self) -> None:
        for problem in ("unavailable", "Not logged in"):
            with self.subTest(problem=problem):
                report = Report("Codex", problem=problem, source=CodexHomeSource(self.home / ".codex"))
                self.assertEqual(whoami.render(report, self.legacy_selection(), self.home / ".codex"), ["Codex", f"  {problem}"])

    def test_whoami_uses_one_workers_role_when_it_changes_during_live_reads(self) -> None:
        selected = self.select_workers("codex2")
        reports = [Report("Codex", email="selected@example.invalid", source=CodexHomeSource(selected)),
            Report("Codex", problem="Not logged in", source=CodexHomeSource(self.home / ".codex"))]
        async def read(_selection: ClaudeAccountsInUse, home: Path) -> list[Report]:
            self.assertEqual(home, selected)
            _ = self.select_workers("codex3")
            return reports
        output = io.StringIO()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}), patch.object(whoami, "live_reports", side_effect=read), redirect_stdout(output):
            asyncio.run(whoami.main())
        blocks = output.getvalue().strip().split("\n\n")
        self.assertIn("(workers)", blocks[0])
        self.assertNotIn("workers", blocks[1])
        self.assertEqual(agent_accounts.workers_home(), self.home / ".codex3")

    def test_missing_selected_workers_home_refuses_resolution_but_discovery_still_lists_its_path(self) -> None:
        selected = self.select_workers("codex2")
        (selected / "auth.json").unlink()
        selected.rmdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}):
            with self.assertRaisesRegex(ValueError, "workers is on codex2, which is not set up on this machine"):
                _ = agent_accounts.workers_home()
            self.assertEqual(agent_accounts.codex_homes(selected), [self.home / ".codex", selected])

    def test_codex_work_readiness_reads_both_current_windows_and_credits(self) -> None:
        now = datetime.now(timezone.utc)
        future = now + timedelta(hours=1)
        for weekly, five_hour, credits, expected in (
            (20, 50, "0", agent_accounts.CodexWorkAvailable),
            (100, 20, "0", agent_accounts.CodexWorkExhausted),
            (20, 100, "0", agent_accounts.CodexWorkExhausted),
            (100, 100, "3", agent_accounts.CodexWorkAvailable),
            (100, 100, "unlimited", agent_accounts.CodexWorkAvailable),
            (100, 20, None, agent_accounts.CodexWorkUnknown),
        ):
            with self.subTest(weekly=weekly, five_hour=five_hour, credits=credits):
                report = Report("Codex", five_hour=agent_accounts.Quota("5-hour", five_hour, future),
                    quotas=[agent_accounts.Quota("Weekly", weekly, future)], credit_balance=credits)
                self.assertIsInstance(agent_accounts.codex_can_work(report, now, agent_accounts.CreditsOn()), expected)

    def test_codex_work_readiness_reads_a_missing_five_hour_window_as_unused(self) -> None:
        # Codex reports no 5-hour window for an account with no use in it (codex1, 2026-10-21).
        now = datetime.now(timezone.utc)
        week = now + timedelta(days=7)
        for weekly, credits, expected in (
            (0, "0", agent_accounts.CodexWorkAvailable),
            (100, "0", agent_accounts.CodexWorkExhausted),
            (100, "3", agent_accounts.CodexWorkAvailable),
        ):
            with self.subTest(weekly=weekly, credits=credits):
                report = Report("Codex", quotas=[agent_accounts.Quota("Weekly", weekly, week)], credit_balance=credits)
                self.assertIsInstance(agent_accounts.codex_can_work(report, now, agent_accounts.CreditsOn()), expected)

    def test_codex_work_readiness_does_not_use_expired_or_missing_allowance(self) -> None:
        now = datetime.now(timezone.utc)
        future = now + timedelta(hours=1)
        for report in (
            Report("Codex", credit_balance="0"),
            Report("Codex", five_hour=agent_accounts.Quota("5-hour", 0, now),
                quotas=[agent_accounts.Quota("Weekly", 0, future)], credit_balance="0"),
            Report("Codex", five_hour=agent_accounts.Quota("5-hour", 0, future),
                quotas=[agent_accounts.Quota("Weekly", 0, now - timedelta(seconds=1))], credit_balance="0"),
            Report("Codex", problem="usage failed", credit_balance="0"),
        ):
            with self.subTest(report=report):
                self.assertIsInstance(agent_accounts.codex_can_work(report, now, agent_accounts.CreditsOn()), agent_accounts.CodexWorkUnknown)

    def test_home_preparation_restarts_an_unanswered_server_then_finishes_initialize(self) -> None:
        home = self.home / ".codex1"
        process = MagicMock(spec=asyncio.subprocess.Process)
        process.returncode = 0
        with (
            patch.object(agent_accounts, "executable", return_value="fixture-codex"),
            patch.object(asyncio, "create_subprocess_exec", return_value=process) as launch,
            patch.object(agent_accounts.CodexServer, "request", side_effect=[agent_accounts.CodexServerClosed("provider exited before reply"), {}]) as request,
            patch.object(agent_accounts.CodexServer, "send") as send,
            patch.object(agent_accounts.CodexServer, "close") as close,
            patch.object(asyncio, "sleep"),
        ):
            result = asyncio.run(agent_accounts.prepare_codex_home(home))
        self.assertIsInstance(result, agent_accounts.CodexHomePrepared)
        self.assertEqual(launch.await_count, 2)
        self.assertEqual(request.await_count, 2)
        self.assertEqual(close.await_count, 2)
        self.assertGreater(agent_accounts.CODEX_HOME_PREPARATION_SECONDS, 925)
        first_wait = cast(float, request.call_args_list[0].kwargs["timeout_seconds"])
        self.assertGreater(first_wait, 925)
        _ = send.assert_awaited_once_with({"method": "initialized"})
        for invocation in launch.call_args_list:
            environment = cast(dict[str, str], invocation.kwargs["env"])
            self.assertEqual(environment["CODEX_HOME"], str(home))
        self.assertTrue(all(invocation.args[1] == "initialize" for invocation in request.call_args_list))

    def test_home_preparation_does_not_retry_an_error_that_mentions_closed(self) -> None:
        process = MagicMock(spec=asyncio.subprocess.Process)
        process.returncode = 0
        with patch.object(asyncio, "create_subprocess_exec", return_value=process) as launch, \
                patch.object(agent_accounts.CodexServer, "request", side_effect=[RuntimeError("request rejected: closed account"), {}]), \
                patch.object(agent_accounts.CodexServer, "send"), patch.object(asyncio, "sleep"), \
                patch.object(agent_accounts.CodexServer, "close"):
            result = asyncio.run(agent_accounts.prepare_codex_home(self.home / ".codex1"))
        self.assertIsInstance(result, agent_accounts.CodexHomePreparationFailed)
        _ = launch.assert_awaited_once()

    def test_server_output_eof_raises_the_named_closed_exception(self) -> None:
        async def request() -> None:
            process = MagicMock(spec=asyncio.subprocess.Process)
            output = asyncio.StreamReader()
            output.feed_eof()
            process.stdout = output
            process.stdin = MagicMock(spec=asyncio.StreamWriter)
            server = agent_accounts.CodexServer(process)
            _ = await server.request(1, "initialize", {})
        with self.assertRaises(agent_accounts.CodexServerClosed):
            asyncio.run(request())

    def test_home_preparation_stops_at_its_limit_with_the_home_and_reason(self) -> None:
        home = self.home / ".codex1"
        process = MagicMock(spec=asyncio.subprocess.Process)
        process.returncode = 0
        with (
            patch.object(agent_accounts, "executable", return_value="fixture-codex"),
            patch.object(agent_accounts, "CODEX_HOME_PREPARATION_SECONDS", 1),
            patch.object(asyncio, "create_subprocess_exec", return_value=process) as launch,
            patch.object(agent_accounts.CodexServer, "request", side_effect=asyncio.TimeoutError),
            patch.object(agent_accounts.CodexServer, "close") as close,
        ):
            result = asyncio.run(agent_accounts.prepare_codex_home(home))
        self.assertIsInstance(result, agent_accounts.CodexHomePreparationFailed)
        if not isinstance(result, agent_accounts.CodexHomePreparationFailed):
            self.fail("Preparation must return its failure reason")
        self.assertIn("~/.codex1", result.problem)
        self.assertIn("1", result.problem)
        self.assertIn("initialize", result.problem)
        _ = launch.assert_awaited_once()
        _ = close.assert_awaited_once()

    def test_numbered_home_read_without_initialize_names_the_setup_command(self) -> None:
        home = self.home / ".codex1"
        process = MagicMock(spec=asyncio.subprocess.Process)
        process.returncode = 0
        with (
            patch.object(agent_accounts, "executable", return_value="fixture-codex"),
            patch.object(asyncio, "create_subprocess_exec", return_value=process),
            patch.object(agent_accounts.CodexServer, "request", side_effect=asyncio.TimeoutError),
            patch.object(agent_accounts.CodexServer, "close"),
        ):
            report = asyncio.run(agent_accounts.codex_live(home))
        self.assertIsNotNone(report.problem)
        self.assertIn("~/.codex1", report.problem or "")
        self.assertIn('/showrunner:accounts setup codex1', report.problem or "")
        self.assertEqual(report.codex_startup, agent_accounts.CodexStartupFailure(
            home, '/showrunner:accounts setup codex1'))


if __name__ == "__main__":
    _ = unittest.main()
