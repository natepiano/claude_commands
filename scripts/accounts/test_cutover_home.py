"""A configuration-only home retains seed data while identity moves by rename."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.message import sessions
import account


class HomeCutoverTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home),
            "AGENT_NOTES_DIR": str(self.home / "notes")}, clear=True))
        (self.home / ".claude").mkdir()
        (self.home / "notes").mkdir()
        for label, login in (("claude2", "old@example.invalid"), ("claude3", "next@example.invalid")):
            _ = (self.home / "notes" / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n")
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "old@example.invalid"},
            "mcpServers": {"fixture": {}}, "projects": {"/project": {"hasTrustDialogAccepted": True}}}))
        self.assertEqual(accounts.setup("claude2"), [])
        _ = self.enterContext(patch.object(sessions, "live_sessions", return_value=[]))
        _ = self.enterContext(patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")))
        _ = self.enterContext(patch.object(Path, "read_bytes", return_value=b"claude\0--resume\0probe\0"))

    def test_cutover_moves_credentials_by_rename_preserves_seed_and_undo_restores_identity(self) -> None:
        source = self.home / ".claude/.credentials.json"
        _ = source.write_bytes(b"fixture only")
        inode = source.stat().st_ino
        accounts.cutover_home("claude2")
        target = self.home / ".claude2/.credentials.json"
        self.assertFalse(source.exists())
        self.assertEqual(target.stat().st_ino, inode)
        self.assertIsNone(account.claude_account(self.home / ".claude"))
        identity = account.claude_account(self.home / ".claude2")
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.label, "claude2")
        self.assertEqual(accounts.claude_account_dir("claude2"), self.home / ".claude2")
        self.assertNotIn("~/.claude", [row.folder for row in accounts.account_statuses()])
        seed = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
        self.assertNotIn("oauthAccount", seed)
        self.assertIn("projects", seed)
        self.assertIn("mcpServers", seed)
        numbered = cast(dict[str, object], json.loads((self.home / ".claude2/.claude.json").read_text()))
        self.assertEqual(numbered["projects"], seed["projects"])
        self.assertEqual(numbered["mcpServers"], seed["mcpServers"])
        accounts.cutover_home(undo=True)
        self.assertEqual(source.stat().st_ino, inode)
        self.assertFalse(target.exists())
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))
        self.assertIsNone(account.claude_account(self.home / ".claude2"))

    def test_absent_process_setting_loses_its_account_only_after_cutover(self) -> None:
        with patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()):
            before = account.account_of(123)
            self.assertIsNotNone(before)
            assert before is not None
            self.assertEqual(before.label, "claude2")
            accounts.cutover_home("claude2")
            self.assertIsNone(account.account_of(123))

    def test_home_login_never_substitutes_for_a_missing_numbered_folder(self) -> None:
        numbered = self.home / ".claude2"
        for item in numbered.iterdir():
            item.unlink()
        numbered.rmdir()
        self.assertEqual(accounts.claude_account_dir("claude2"), numbered)
        statuses = accounts.account_statuses()
        self.assertNotIn("~/.claude", [status.folder for status in statuses])
        status = next(status for status in statuses if status.label == "claude2")
        self.assertFalse(status.is_set_up)
        self.assertIsInstance(accounts.prepared_account("claude2", resume=True), accounts.AccountRefused)
        self.assertTrue(any("missing" in problem for problem in accounts.check("claude2")))

    def test_destination_identity_refuses_before_any_journal_or_move(self) -> None:
        target = self.home / ".claude2/.claude.json"
        _ = target.write_text('{"oauthAccount": {}}')
        with self.assertRaisesRegex(accounts.AccountsError, "already holds"):
            accounts.cutover_home("claude2")
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_socketless_claude_process_prevents_forward_cutover(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "321 claude claude --resume probe\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()) as environment):
            with self.assertRaisesRegex(accounts.AccountsError, "live session"):
                accounts.cutover_home("claude2")
        environment.assert_called_once_with(321)
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_socketless_claude_process_prevents_undo_on_its_numbered_folder(self) -> None:
        accounts.cutover_home("claude2")
        listing = subprocess.CompletedProcess([], 0, "321 claude claude --resume probe\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.home / ".claude2"))):
            with self.assertRaisesRegex(accounts.AccountsError, "live session"):
                accounts.cutover_home(undo=True)
        self.assertIsNone(account.claude_account(self.home / ".claude"))
        self.assertIsNotNone(account.claude_account(self.home / ".claude2"))

    def test_unreadable_socketless_claude_environment_prevents_cutover(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "321 claude claude\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(account, "process_config_dir", return_value=account.ProcessEnvironmentUnread("denied"))):
            with self.assertRaisesRegex(accounts.AccountsError, "cannot establish"):
                accounts.cutover_home("claude2")
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_node_claude_with_apostrophe_in_prompt_prevents_forward_cutover(self) -> None:
        listing = subprocess.CompletedProcess([], 0,
            "321 node node /opt/@anthropic-ai/claude-code/cli.js -p user's prompt\n", "")
        arguments = b"node\0/opt/@anthropic-ai/claude-code/cli.js\0-p\0user's prompt\0"
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(Path, "read_bytes", return_value=arguments),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()) as environment):
            with self.assertRaisesRegex(accounts.AccountsError, "live session"):
                accounts.cutover_home("claude2")
        environment.assert_called_once_with(321)
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_node_claude_with_apostrophe_in_prompt_prevents_undo(self) -> None:
        accounts.cutover_home("claude2")
        listing = subprocess.CompletedProcess([], 0,
            "321 node node /opt/@anthropic-ai/claude-code/cli.js -p user's prompt\n", "")
        arguments = b"node\0/opt/@anthropic-ai/claude-code/cli.js\0-p\0user's prompt\0"
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(Path, "read_bytes", return_value=arguments),
              patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.home / ".claude2"))):
            with self.assertRaisesRegex(accounts.AccountsError, "live session"):
                accounts.cutover_home(undo=True)
        self.assertIsNone(account.claude_account(self.home / ".claude"))
        self.assertIsNotNone(account.claude_account(self.home / ".claude2"))

    def test_unreadable_node_arguments_prevent_cutover(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "321 node node worker.js\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(Path, "read_bytes", side_effect=PermissionError("denied"))):
            with self.assertRaisesRegex(accounts.AccountsError, "cannot read.*arguments"):
                accounts.cutover_home("claude2")
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_empty_node_arguments_prevent_cutover(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "321 node node worker.js\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(Path, "read_bytes", return_value=b"")):
            with self.assertRaisesRegex(accounts.AccountsError, "cannot read.*arguments"):
                accounts.cutover_home("claude2")
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_readable_non_claude_node_process_does_not_prevent_cutover(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "321 node node worker.js\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(Path, "read_bytes", return_value=b"node\0/opt/worker.js\0"),
              patch.object(account, "process_config_dir") as environment):
            accounts.cutover_home("claude2")
        environment.assert_not_called()
        self.assertIsNone(account.claude_account(self.home / ".claude"))
        self.assertIsNotNone(account.claude_account(self.home / ".claude2"))

    def test_mac_cutover_refuses_both_directions_before_identity_changes(self) -> None:
        for undo in (False, True):
            with self.subTest(undo=undo), patch.object(sys, "platform", "darwin"):
                with self.assertRaisesRegex(accounts.AccountsError, r"Mac.*one /login.*~/.claude2.*never /logout"):
                    accounts.cutover_home("claude2", undo=undo)
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())
        self.assertIsNotNone(account.claude_account(self.home / ".claude"))

    def test_mac_status_names_its_login_step_instead_of_cutover_command(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            report = accounts.status_report()
        self.assertIn("one /login in ~/.claude2", report)
        self.assertIn("never /logout", report)
        self.assertNotIn("run accounts.py cutover-home", report)

    def test_unscoped_launch_keeps_legacy_home_until_cutover_then_uses_showrunners(self) -> None:
        before = accounts.account_for_unscoped_launch({})
        self.assertIsInstance(before, accounts.LegacyHomeLaunch)
        accounts.cutover_home("claude2")
        _ = accounts.set_group_account("showrunners", "claude2", why="fixture", now=datetime.now(timezone.utc))
        after = accounts.account_for_unscoped_launch({})
        self.assertEqual(after, accounts.NamedAccount("claude2", self.home / ".claude2"))

    def test_interrupted_cutover_finishes_without_overwriting_or_copying_credentials(self) -> None:
        source = self.home / ".claude/.credentials.json"
        _ = source.write_bytes(b"fixture only")
        inode = source.stat().st_ino
        rename = Path.rename
        def interrupted(path: Path, destination: Path) -> Path:
            if path == source:
                raise OSError("interrupted")
            return rename(path, destination)
        with patch.object(Path, "rename", interrupted):
            with self.assertRaisesRegex(OSError, "interrupted"):
                accounts.cutover_home("claude2")
        accounts.cutover_home("claude2")
        self.assertFalse(source.exists())
        self.assertEqual((self.home / ".claude2/.credentials.json").stat().st_ino, inode)
        self.assertIsNone(account.claude_account(self.home / ".claude"))
        history = cast(dict[str, object], json.loads(accounts.accounts_config_file().read_text()))
        self.assertEqual(len(cast(list[object], history["home_cutovers"])), 1)

    def test_history_uses_each_record_login_or_its_dated_cutover_not_todays_login(self) -> None:
        accounts.cutover_home("claude2")
        old = accounts.resolve_history_label("default", "2026-01-01T00:00:00Z")
        self.assertEqual(old, accounts.ResolvedHistoryLabel("claude2"))
        _ = (self.home / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"next@example.invalid"}}')
        self.assertEqual(accounts.resolve_history_label("~/.claude", "2026-01-01T00:00:00Z", login="next@example.invalid"),
                         accounts.ResolvedHistoryLabel("claude3"))
        self.assertEqual(accounts.resolve_history_label("default", "2026-01-01T00:00:00Z"), old)
        self.assertIsInstance(accounts.resolve_history_label("default", "2999-01-01T00:00:00Z"),
                              accounts.HistoricalAccountUnresolved)

    def test_exec_showrunners_always_sets_the_groups_numbered_folder(self) -> None:
        accounts.cutover_home("claude2")
        _ = accounts.set_group_account("showrunners", "claude2", why="fixture", now=datetime.now(timezone.utc))
        with (patch.object(os, "execvpe") as execute,
              patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "wrong", "CLAUDE_CODE_OAUTH_TOKEN": "fixture"}),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(accounts.main(["exec", "showrunners", "--version"]), 0)
        environment = cast(dict[str, str], execute.call_args.args[2])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.home / ".claude2"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)

    def test_two_home_cutovers_resolve_records_on_each_side_from_their_own_era(self) -> None:
        accounts.cutover_home("claude2")
        config = cast(dict[str, object], json.loads(accounts.accounts_config_file().read_text()))
        first = cast(list[dict[str, str]], config["home_cutovers"])[0]["at"]
        self.assertEqual(accounts.setup("claude3"), [])
        seed_path = self.home / ".claude.json"
        seed = cast(dict[str, object], json.loads(seed_path.read_text()))
        seed["oauthAccount"] = {"emailAddress": "next@example.invalid"}
        _ = seed_path.write_text(json.dumps(seed))
        accounts.cutover_home("claude3")
        self.assertEqual(accounts.resolve_history_label("default", "2026-01-01T00:00:00Z"),
                         account.ResolvedHistoryLabel("claude2"))
        self.assertEqual(accounts.resolve_history_label("~/.claude", first), account.ResolvedHistoryLabel("claude3"))
        self.assertIsNone(account.claude_account(self.home / ".claude"))
