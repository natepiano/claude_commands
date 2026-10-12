"""Codex home discovery, live read isolation, and account display."""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import MagicMock, call, patch

import agent_accounts
import note_file
import whoami
from agent_accounts import (
    CheckedNamedClaudeAccount,
    ClaudeAccountSelection,
    CodexAccountResult,
    CodexHomeSource,
    CodexRateLimits,
    KnownClaudeAccount,
    Report,
    UnreadableClaudeAccount,
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
        _ = self.enterContext(patch.object(note_file, "AGENTS_DIR", self.home))

    def legacy_selection(self) -> ClaudeAccountSelection:
        return ClaudeAccountSelection(UnreadableClaudeAccount(), UnreadableClaudeAccount(), False, ())

    def live_fixture(self, home: Path) -> Report:
        return Report("Codex", email=f"{home.name}@example.invalid", source=CodexHomeSource(home))

    def test_discovery_includes_default_even_when_no_codex_home_exists(self) -> None:
        self.assertEqual(agent_accounts.codex_homes(), [self.home / ".codex"])
        self.assertEqual(agent_accounts.codex_home(), self.home / ".codex")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_discovery_orders_existing_numbered_directories_numerically(self) -> None:
        for name in (".codex10", ".codex2", ".codex1", ".codex-other"):
            (self.home / name).mkdir()
        _ = (self.home / ".codex3").write_text("a file")

        self.assertEqual(agent_accounts.codex_homes(), [
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
                self.assertEqual(agent_accounts.codex_homes(), expected)

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
        default = KnownClaudeAccount("main@example.invalid", "claude 1")
        named = CheckedNamedClaudeAccount("claude 2", self.home / ".claude2",
            KnownClaudeAccount("unit@example.invalid", "claude 2"))
        selections = (
            self.legacy_selection(),
            ClaudeAccountSelection(default, named.identity, True, (named,)),
        )
        homes = [self.home / name for name in (".codex", ".codex2", ".codex10")]
        for selection in selections:
            with (
                self.subTest(multiple=selection.multiple_accounts),
                patch.object(agent_accounts, "claude_live", return_value=Report("Claude")) as claude,
                patch.object(agent_accounts, "codex_live", side_effect=self.live_fixture) as codex,
            ):
                reports = asyncio.run(agent_accounts.live_reports(selection))
            self.assertEqual([report.source for report in reports if report.tool == "Codex"],
                [CodexHomeSource(home) for home in homes])
            self.assertCountEqual(codex.await_args_list, [call(home) for home in homes])
            if selection.multiple_accounts:
                self.assertCountEqual(claude.call_args_list, [call(self.home / ".claude"), call(named.directory)])
            else:
                claude.assert_called_once_with()

    def test_whoami_prints_each_codex_account_and_marks_only_the_selected_home(self) -> None:
        default = self.home / ".codex"
        selected = self.home / ".codex2"
        selected.mkdir()
        for label, login in (("codex 1", "default@example.invalid"), ("codex 2", "selected@example.invalid")):
            _ = (self.home / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n")
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
        self.assertIn("Account: codex 1 — default@example.invalid", blocks[0])
        self.assertNotIn("(seats)", blocks[0])
        self.assertIn("Account: codex 2 (seats) — selected@example.invalid", blocks[1])

    def test_whoami_with_one_codex_home_keeps_the_original_output(self) -> None:
        _ = (self.home / "codex 1.md").write_text("---\nlogin: seats@example.invalid\n---\n")
        report = Report("Codex", email="seats@example.invalid", source=CodexHomeSource(self.home / ".codex"))
        self.assertEqual(whoami.render(report, self.legacy_selection()), [
            "Codex", "  Account: codex 1 — seats@example.invalid", "  Plan: unavailable",
            "  Limit resets available: unavailable",
        ])

    def test_whoami_marks_the_selected_block_when_its_live_read_fails(self) -> None:
        default = self.home / ".codex"
        selected = self.home / ".codex2"
        selected.mkdir()
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
            "Codex (~/.codex2, seats)\n  selected unavailable",
        ])

    def test_whoami_names_the_logged_out_spare_and_selected_codex_homes(self) -> None:
        default = self.home / ".codex"
        spare = self.home / ".codex1"
        spare.mkdir()
        reports = [
            Report("Codex", problem="Not logged in", source=CodexHomeSource(default)),
            Report("Codex", problem="Not logged in", source=CodexHomeSource(spare)),
        ]
        output = io.StringIO()
        with patch.object(whoami, "live_reports", return_value=reports), redirect_stdout(output):
            asyncio.run(whoami.main())
        self.assertEqual(output.getvalue().strip().split("\n\n"), [
            "Codex (~/.codex, seats)\n  Not logged in",
            "Codex (~/.codex1)\n  Not logged in",
        ])

    def test_whoami_names_a_home_when_the_account_email_is_unavailable(self) -> None:
        spare = self.home / ".codex1"
        spare.mkdir()
        report = Report("Codex", source=CodexHomeSource(spare))
        self.assertEqual(whoami.render(report, self.legacy_selection()), [
            "Codex (~/.codex1)", "  Account: email unavailable", "  Plan: unavailable",
            "  Limit resets available: unavailable",
        ])

    def test_whoami_uses_the_full_path_for_an_accountless_home_outside_user_home(self) -> None:
        selected = self.home.parent / f"{self.home.name}-codex"
        report = Report("Codex", problem="Not logged in", source=CodexHomeSource(selected))
        with patch.dict(os.environ, {"CODEX_HOME": str(selected)}):
            self.assertEqual(whoami.render(report, self.legacy_selection()), [
                f"Codex ({selected}, seats)", "  Not logged in",
            ])

    def test_whoami_with_one_failed_codex_home_keeps_the_original_output(self) -> None:
        for problem in ("unavailable", "Not logged in"):
            with self.subTest(problem=problem):
                report = Report("Codex", problem=problem, source=CodexHomeSource(self.home / ".codex"))
                self.assertEqual(whoami.render(report, self.legacy_selection()), ["Codex", f"  {problem}"])


if __name__ == "__main__":
    _ = unittest.main()
