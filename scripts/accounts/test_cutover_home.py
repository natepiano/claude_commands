"""A configuration-only home retains seed data while identity moves by rename."""
from __future__ import annotations

import ctypes
import base64
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.accounts import accounts, role_map
from scripts.accounts.nix_env_fake import fake_nix_loader, imports_with_nix_loader
from scripts.message import sessions
from scripts.whoami import agent_accounts
from scripts.whoami.test_agent_accounts import ServiceKeychain, folder_service
import account


def file_state(path: Path) -> tuple[int, int, int, int, int, int]:
    """Return the file's identity and change times, without the access time a read can move."""
    status = path.stat()
    return (status.st_dev, status.st_ino, status.st_mode, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


class HomeCutoverTests(unittest.TestCase):
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.object(accounts, "import_module", side_effect=imports_with_nix_loader(fake_nix_loader()))))
        _ = self.enterContext(patch.object(sys, "platform", "linux"))
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
        program = self.home / ".local/bin/claude"
        program.parent.mkdir(parents=True)
        _ = program.write_text("#!/bin/sh\nexit 99\n")
        program.chmod(0o700)
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


class CodexHomeCutoverTests(unittest.TestCase):
    """Only a rename transfers the scratch login, with both directions recoverable."""

    home: Path = Path()
    source: Path = Path()
    target: Path = Path()
    journal: Path = Path()
    auth_inode: int = 0
    output: io.StringIO = io.StringIO()
    errors: io.StringIO = io.StringIO()
    process_lister: Callable[[], list[accounts.CodexHomeProcess]] = staticmethod(accounts.list_codex_home_processes)

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home / "notes")}, clear=True)))
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        self.process_lister = accounts.list_codex_home_processes
        _ = self.enterContext(patch.object(accounts, "list_codex_home_processes", return_value=[]))
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home",
                                           return_value=agent_accounts.CodexHomePrepared()))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("no real process or keychain calls")))
        self.source = self.home / ".codex"
        self.target = self.home / ".codex2"
        self.source.mkdir()
        (self.home / "notes").mkdir()
        _ = (self.home / "notes/codex2.md").write_text("---\nlogin: codex-cutover@example.invalid\n---\n")
        _ = (self.home / "notes/codex3.md").write_text("---\nlogin: other@example.invalid\n---\n")
        claims = base64.urlsafe_b64encode(json.dumps({"email": "codex-cutover@example.invalid"}).encode()).decode().rstrip("=")
        # Synthetic identity only; assertions inspect its inode, never credential bytes.
        _ = (self.source / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        self.auth_inode = (self.source / "auth.json").stat().st_ino
        _ = (self.source / "config.toml").write_text('model = "fixture"\n')
        (self.source / "sessions").mkdir()
        _ = (self.source / "sessions/kept.jsonl").write_text("synthetic session seed\n")
        self.journal = self.home / ".local/state/accounts/cutover-codex-home.json"
        self.output, self.errors = io.StringIO(), io.StringIO()
        _ = self.enterContext(redirect_stdout(self.output))
        _ = self.enterContext(redirect_stderr(self.errors))

    def run_cutover(self, *, undo: bool = False) -> int:
        return accounts.main(["cutover-home", "codex2", *(["--undo"] if undo else [])])

    def journal_record(self) -> dict[str, str]:
        return cast(dict[str, str], json.loads(self.journal.read_text()))

    def history(self) -> list[dict[str, str]]:
        configuration = cast(dict[str, object], json.loads(accounts.accounts_config_file().read_text()))
        return cast(list[dict[str, str]], configuration["home_cutovers"])

    def assert_login_moved(self) -> None:
        self.assertFalse((self.source / "auth.json").exists())
        self.assertEqual((self.target / "auth.json").stat().st_ino, self.auth_inode)
        self.assertEqual(self.journal_record()["state"], "complete")
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.history()[0], {key: self.journal_record()[key] for key in ("label", "login", "at")})
        selected = agent_accounts.codex_account_home("codex2")
        self.assertIsInstance(selected, agent_accounts.CodexHomeHoldingLogin)
        self.assertEqual(selected.home, self.target)

    def test_codex_cutover_prepares_shared_links_and_renames_login_with_dated_history(self) -> None:
        original_seed = (self.source / "config.toml").read_bytes()
        began = datetime.now(timezone.utc)
        self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()
        self.assertTrue(began <= datetime.fromisoformat(self.history()[0]["at"]) <= datetime.now(timezone.utc))
        for name in ("config.toml", "sessions"):
            self.assertTrue((self.target / name).is_symlink())
            self.assertEqual((self.target / name).resolve(), self.source / name)
        self.assertEqual((self.source / "config.toml").read_bytes(), original_seed)
        self.assertEqual((self.target / "sessions/kept.jsonl").read_text(), "synthetic session seed\n")
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())

    def test_plain_codex_setup_keeps_duplicate_login_refusal_but_cutover_can_prepare_the_home(self) -> None:
        self.assertTrue(any("already holds its login" in problem for problem in accounts.setup("codex2")))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()

    def test_cutover_waits_for_initialize_after_moving_the_login_before_reporting_ready(self) -> None:
        wait_line = (
            "Wait for codex2 (~/.codex2) to finish its shared session index and answer initialize; "
            "an interrupted index can hold startup for 15 minutes, then indexing must finish; limit 20 minutes.\n"
        )
        ready_line = "codex2 (~/.codex2) login moved; shared session index ready.\n"

        async def initialize(home: Path) -> agent_accounts.CodexHomePrepared:
            self.assertEqual(home, self.target)
            self.assert_login_moved()
            self.assertEqual(self.output.getvalue(), wait_line)
            return agent_accounts.CodexHomePrepared()

        with patch.object(agent_accounts, "prepare_codex_home", side_effect=initialize) as prepare:
            self.assertEqual(self.run_cutover(), 0)
        _ = prepare.assert_awaited_once_with(self.target)
        self.assertEqual(self.output.getvalue(), wait_line + ready_line)
        self.assertEqual(self.errors.getvalue(), "")

    def test_failed_initialization_leaves_the_login_move_complete_and_names_the_setup_recovery(self) -> None:
        async def fail_initialize(home: Path) -> agent_accounts.CodexHomePreparationFailed:
            self.assertEqual(home, self.target)
            self.assert_login_moved()
            return agent_accounts.CodexHomePreparationFailed("synthetic initialize timeout")

        with patch.object(agent_accounts, "prepare_codex_home", side_effect=fail_initialize) as prepare:
            self.assertNotEqual(self.run_cutover(), 0)
        _ = prepare.assert_awaited_once_with(self.target)
        self.assert_login_moved()
        self.assertEqual(self.errors.getvalue(),
            "accounts: ~/.codex2 login moved, but its shared session index is not ready "
            + "(synthetic initialize timeout); run accounts.py setup codex2 to finish the index\n")
        self.assertNotIn("shared session index ready.", self.output.getvalue())

    def test_rerun_after_failed_initialization_does_not_repeat_the_move_or_preparation(self) -> None:
        with patch.object(agent_accounts, "prepare_codex_home",
                          return_value=agent_accounts.CodexHomePreparationFailed("synthetic initialize timeout")):
            self.assertNotEqual(self.run_cutover(), 0)
        before = (self.journal.read_bytes(), accounts.accounts_config_file().read_bytes(),
                  file_state(self.target / "auth.json"))
        _ = self.output.seek(0)
        _ = self.output.truncate()
        _ = self.errors.seek(0)
        _ = self.errors.truncate()
        with (patch.object(agent_accounts, "prepare_codex_home", side_effect=AssertionError("rerun must use setup to prepare")),
              patch.object(os, "replace", side_effect=AssertionError("completed cutover must not move files"))):
            self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()
        self.assertEqual((self.journal.read_bytes(), accounts.accounts_config_file().read_bytes(),
                          file_state(self.target / "auth.json")), before)
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.errors.getvalue(), "")

    def test_undo_restores_the_login_without_running_first_startup(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        _ = self.output.seek(0)
        _ = self.output.truncate()
        with patch.object(agent_accounts, "prepare_codex_home", side_effect=AssertionError("undo must not prepare")):
            self.assertEqual(self.run_cutover(undo=True), 0)
        self.assertEqual(self.journal_record()["state"], "undone")
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)
        self.assertFalse((self.target / "auth.json").exists())
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.errors.getvalue(), "")

    def test_complete_codex_cutover_rerun_changes_no_identity_journal_or_history(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        before = (self.journal.read_bytes(), accounts.accounts_config_file().read_bytes(),
                  file_state(self.target / "auth.json"))
        with patch.object(accounts, "list_codex_home_processes", side_effect=AssertionError("completed move needs no process check")):
            self.assertEqual(self.run_cutover(), 0)
        self.assertEqual((self.journal.read_bytes(), accounts.accounts_config_file().read_bytes(),
                          file_state(self.target / "auth.json")), before)

    def test_completed_codex_cutover_can_move_a_new_legacy_login_to_its_own_numbered_home(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        first = self.history()[0]
        claims = base64.urlsafe_b64encode(json.dumps({"email": "other@example.invalid"}).encode()).decode().rstrip("=")
        _ = (self.source / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        replacement_inode = (self.source / "auth.json").stat().st_ino
        self.assertEqual(accounts.main(["cutover-home", "codex3"]), 0)
        self.assertFalse((self.source / "auth.json").exists())
        self.assertEqual((self.home / ".codex3/auth.json").stat().st_ino, replacement_inode)
        self.assertEqual((self.target / "auth.json").stat().st_ino, self.auth_inode)
        self.assertEqual(self.journal_record()["label"], "codex3")
        self.assertEqual(self.journal_record()["state"], "complete")
        self.assertEqual(self.history(), [first, {key: self.journal_record()[key] for key in ("label", "login", "at")}])

    def test_completed_cutover_with_a_new_source_login_still_refuses_an_occupied_numbered_home(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        before = (self.journal.read_bytes(), accounts.accounts_config_file().read_bytes())
        _ = (self.source / "auth.json").write_text("synthetic replacement identity")
        replacement_inode = (self.source / "auth.json").stat().st_ino
        with patch.object(agent_accounts, "codex_email_on_disk", return_value="codex-cutover@example.invalid"):
            self.assertNotEqual(self.run_cutover(), 0)
        self.assertIn("already holds auth.json", self.errors.getvalue())
        self.assertEqual((self.journal.read_bytes(), accounts.accounts_config_file().read_bytes()), before)
        self.assertEqual((self.source / "auth.json").stat().st_ino, replacement_inode)
        self.assertEqual((self.target / "auth.json").stat().st_ino, self.auth_inode)

    def test_completed_cutover_revalidates_the_new_source_login_against_the_requested_account(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        os.replace(self.target / "auth.json", self.source / "auth.json")
        before = (self.journal.read_bytes(), accounts.accounts_config_file().read_bytes())
        self.assertNotEqual(accounts.main(["cutover-home", "codex3"]), 0)
        self.assertEqual(self.errors.getvalue(), "accounts: ~/.codex does not hold codex3's login\n")
        self.assertEqual((self.journal.read_bytes(), accounts.accounts_config_file().read_bytes()), before)
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)
        self.assertFalse((self.home / ".codex3").exists())

    def test_undone_codex_cutover_starts_a_fresh_move_with_new_dated_history(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        first = self.history()[0]
        self.assertEqual(self.run_cutover(undo=True), 0)
        self.assertEqual(self.run_cutover(), 0)
        self.assertFalse((self.source / "auth.json").exists())
        self.assertEqual((self.target / "auth.json").stat().st_ino, self.auth_inode)
        self.assertEqual(self.journal_record()["state"], "complete")
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(self.history()[0], first)
        self.assertGreater(self.history()[1]["at"], first["at"])

    def test_codex_undo_renames_login_back_and_preserves_cutover_history(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        history = accounts.accounts_config_file().read_bytes()
        self.assertEqual(self.run_cutover(undo=True), 0)
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)
        self.assertFalse((self.target / "auth.json").exists())
        self.assertEqual(self.journal_record()["state"], "undone")
        self.assertEqual(accounts.accounts_config_file().read_bytes(), history)
        self.assertEqual(agent_accounts.codex_account_home("codex2").home, self.source)
        self.assertEqual(self.run_cutover(undo=True), 0)

    def test_codex_processes_on_legacy_home_refuse_before_journal_preparation_or_rename(self) -> None:
        processes = [
            accounts.CodexHomeProcess(321, "codex app-server daemon", account.ProcessWithoutCodexHome()),
            accounts.CodexHomeProcess(654, "codex --remote-control", account.ProcessCodexHome(self.source)),
        ]
        with patch.object(accounts, "list_codex_home_processes", return_value=processes):
            self.assertNotEqual(self.run_cutover(), 0)
        for process in processes:
            self.assertIn(f"{process.pid} ({process.command})", self.errors.getvalue())
        self.assertIn("stop it, then rerun cutover-home", self.errors.getvalue())
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)

    def test_codex_process_on_numbered_home_refuses_undo_without_changing_completed_move(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        before = (self.journal.read_bytes(), accounts.accounts_config_file().read_bytes())
        process = accounts.CodexHomeProcess(321, "codex app-server daemon", account.ProcessCodexHome(self.target))
        with patch.object(accounts, "list_codex_home_processes", return_value=[process]):
            self.assertNotEqual(self.run_cutover(undo=True), 0)
        self.assertIn("321 (codex app-server daemon)", self.errors.getvalue())
        self.assertIn("stop it, then rerun cutover-home", self.errors.getvalue())
        self.assertEqual((self.journal.read_bytes(), accounts.accounts_config_file().read_bytes()), before)
        self.assert_login_moved()

    def test_unreadable_codex_process_environment_refuses_forward_and_undo_without_mutation(self) -> None:
        process = accounts.CodexHomeProcess(987, "codex app-server daemon", account.ProcessEnvironmentUnread("denied"))
        with patch.object(accounts, "list_codex_home_processes", return_value=[process]):
            self.assertNotEqual(self.run_cutover(), 0)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.target.exists())
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)
        self.assertIn("cannot read environment of process 987 (codex app-server daemon)", self.errors.getvalue())
        self.assertIn("stop it, then rerun cutover-home", self.errors.getvalue())
        self.assertEqual(self.run_cutover(), 0)
        before = self.journal.read_bytes()
        with patch.object(accounts, "list_codex_home_processes", return_value=[process]):
            self.assertNotEqual(self.run_cutover(undo=True), 0)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_login_moved()

    def test_codex_process_on_another_home_does_not_block_forward_or_undo(self) -> None:
        process = accounts.CodexHomeProcess(321, "codex exec fixture", account.ProcessCodexHome(self.home / ".codex3"))
        with patch.object(accounts, "list_codex_home_processes", return_value=[process]):
            self.assertEqual(self.run_cutover(), 0)
            self.assert_login_moved()
            self.assertEqual(self.run_cutover(undo=True), 0)
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)

    def test_wrong_source_login_refuses_without_preparing_target_or_writing_journal(self) -> None:
        self.assertNotEqual(accounts.main(["cutover-home", "codex3"]), 0)
        self.assertIn("~/.codex does not hold codex3's login", self.errors.getvalue())
        self.assertFalse(self.journal.exists())
        self.assertFalse((self.home / ".codex3").exists())
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)

    def test_missing_source_login_without_journal_refuses_without_creating_a_target(self) -> None:
        (self.source / "auth.json").unlink()
        self.assertNotEqual(self.run_cutover(), 0)
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.target.exists())
        self.assertIn("home login", self.errors.getvalue())

    def test_existing_numbered_auth_is_never_overwritten_by_forward_cutover(self) -> None:
        self.target.mkdir()
        _ = (self.target / "auth.json").write_text("synthetic occupied identity")
        occupied = (self.target / "auth.json").stat().st_ino
        self.assertNotEqual(self.run_cutover(), 0)
        self.assertIn("already holds auth.json", self.errors.getvalue())
        self.assertFalse(self.journal.exists())
        self.assertEqual((self.target / "auth.json").stat().st_ino, occupied)
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)

    def test_codex_cutover_on_mac_bypasses_claude_keychain_checks_in_both_directions(self) -> None:
        with (patch.object(sys, "platform", "darwin"),
              patch.object(accounts, "_mac_login_keychain_visible", side_effect=AssertionError("Codex never uses keychain"))):
            self.assertEqual(self.run_cutover(), 0)
            self.assert_login_moved()
            self.assertEqual(self.run_cutover(undo=True), 0)
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)

    def test_durable_moving_journal_before_rename_finishes_on_rerun(self) -> None:
        replace = os.replace
        def interrupted(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(source) == self.source / "auth.json":
                self.assertEqual(self.journal_record()["state"], "moving")
                raise OSError("synthetic interruption before rename")
            replace(source, destination)
        with patch.object(os, "replace", side_effect=interrupted):
            self.assertNotEqual(self.run_cutover(), 0)
        self.assertEqual(self.journal_record()["state"], "moving")
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)
        self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()

    def test_pending_codex_cutover_refuses_a_changed_or_unreadable_source_login_without_mutation(self) -> None:
        journal = {"label": "codex2", "login": "codex-cutover@example.invalid",
                   "at": "2026-10-16T12:00:00Z", "state": "moving"}
        self.journal.parent.mkdir(parents=True)
        _ = self.journal.write_text(json.dumps(journal))
        before = (self.journal.read_bytes(), file_state(self.source / "auth.json"))
        for login in ("replacement@example.invalid", ""):
            with self.subTest(readable=bool(login)):
                _ = self.errors.seek(0)
                _ = self.errors.truncate()
                with patch.object(agent_accounts, "codex_email_on_disk", return_value=login):
                    self.assertNotEqual(self.run_cutover(), 0)
                self.assertEqual(self.errors.getvalue(),
                    "accounts: ~/.codex now holds another login than the recorded cutover; undo it first\n")
                self.assertEqual((self.journal.read_bytes(), file_state(self.source / "auth.json")), before)
                self.assertFalse(self.target.exists())
                self.assertFalse(accounts.accounts_config_file().exists())

    def test_pending_codex_cutover_accepts_the_recorded_source_login_case_insensitively(self) -> None:
        journal = {"label": "codex2", "login": "CODEX-CUTOVER@EXAMPLE.INVALID",
                   "at": "2026-10-16T12:00:00Z", "state": "moving"}
        self.journal.parent.mkdir(parents=True)
        _ = self.journal.write_text(json.dumps(journal))
        self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()

    def test_mac_assignment_prompt_cannot_hide_a_process_using_the_legacy_home(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "321 codex\n", "")
        arguments = ("/Applications/Codex Tools/codex", "exec", "CODEX_HOME=/scratch/other", "-c", "key=value")
        payload = (len(arguments).to_bytes(4, sys.byteorder, signed=True)
                   + os.fsencode(arguments[0]) + b"\0\0"
                   + b"\0".join(os.fsencode(word) for word in arguments) + b"\0HOME=/scratch\0")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(subprocess, "run", return_value=listing),
              patch.object(account, "kern_procargs2", return_value=payload),
              patch.object(accounts, "list_codex_home_processes", side_effect=self.process_lister)):
            self.assertNotEqual(self.run_cutover(), 0)
        self.assertIn(f"321 ({shlex.join(arguments)}) uses {self.source}", self.errors.getvalue())
        self.assertIn("stop it, then rerun cutover-home", self.errors.getvalue())
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(accounts.accounts_config_file().exists())
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)

    def test_interruption_after_auth_rename_recovers_when_source_is_already_gone(self) -> None:
        replace = os.replace
        def interrupted(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            replace(source, destination)
            if Path(source) == self.source / "auth.json":
                raise OSError("synthetic interruption after rename")
        with patch.object(os, "replace", side_effect=interrupted):
            self.assertNotEqual(self.run_cutover(), 0)
        self.assertFalse((self.source / "auth.json").exists())
        self.assertEqual(self.journal_record()["state"], "moving")
        self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()

    def test_interruption_after_history_write_reruns_without_duplicate_evidence(self) -> None:
        record = accounts._record_home_cutover  # pyright: ignore[reportPrivateUsage]
        def interrupted(journal: accounts.HomeCutover) -> None:
            record(journal)
            raise OSError("synthetic interruption after history")
        with patch.object(accounts, "_record_home_cutover", side_effect=interrupted):
            self.assertNotEqual(self.run_cutover(), 0)
        before = accounts.accounts_config_file().read_bytes()
        self.assertEqual(self.journal_record()["state"], "moving")
        self.assertEqual(self.run_cutover(), 0)
        self.assert_login_moved()
        self.assertEqual(accounts.accounts_config_file().read_bytes(), before)

    def test_interruption_after_undo_rename_finishes_when_numbered_auth_is_already_gone(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        replace = os.replace
        def interrupted(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            replace(source, destination)
            if Path(source) == self.target / "auth.json":
                raise OSError("synthetic interruption after undo rename")
        with patch.object(os, "replace", side_effect=interrupted):
            self.assertNotEqual(self.run_cutover(undo=True), 0)
        self.assertEqual(self.journal_record()["state"], "undoing")
        self.assertEqual((self.source / "auth.json").stat().st_ino, self.auth_inode)
        self.assertEqual(self.run_cutover(undo=True), 0)
        self.assertFalse((self.target / "auth.json").exists())
        self.assertEqual(self.journal_record()["state"], "undone")
        self.assertEqual(len(self.history()), 1)

    def test_moving_journal_with_both_auth_files_gone_reports_loss_without_false_completion(self) -> None:
        journal = {"label": "codex2", "login": "codex-cutover@example.invalid",
                   "at": "2026-10-16T12:00:00Z", "state": "moving"}
        self.journal.parent.mkdir(parents=True)
        _ = self.journal.write_text(json.dumps(journal))
        (self.source / "auth.json").unlink()
        before = self.journal.read_bytes()
        self.assertNotEqual(self.run_cutover(), 0)
        self.assertIn("neither Codex home holds auth.json", self.errors.getvalue())
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertFalse(accounts.accounts_config_file().exists())

    def test_dated_codex_cutover_resolves_historical_home_but_not_later_or_claude_records(self) -> None:
        self.assertEqual(self.run_cutover(), 0)
        at = self.journal_record()["at"]
        for spelling in ("~/.codex", str(self.source)):
            self.assertEqual(accounts.resolve_history_label(spelling, "2026-01-01T00:00:00Z"),
                             accounts.ResolvedHistoryLabel("codex2"))
            for later in (at, "2999-01-01T00:00:00Z"):
                self.assertIsInstance(accounts.resolve_history_label(spelling, later), accounts.HistoricalAccountUnresolved)
        self.assertIsInstance(accounts.resolve_history_label("~/.claude", "2026-01-01T00:00:00Z"),
                              accounts.HistoricalAccountUnresolved)

    def test_status_names_legacy_login_only_until_cutover_and_omits_signed_out_default_row(self) -> None:
        with patch.object(accounts, "_stored_usage", return_value={}):
            before = accounts.status_report()
            self.assertIn("~/.codex holds a login; run accounts.py cutover-home codex2", before)
            self.assertEqual(self.run_cutover(), 0)
            after = accounts.status_report()
            self.assertNotIn("~/.codex holds a login", after)
            self.assertNotIn("~/.codex", [row.folder for row in accounts.codex_statuses(self.target)])
            self.assertIn("~/.codex2", after)
            self.assertEqual(self.run_cutover(undo=True), 0)
            self.assertIn("~/.codex holds a login; run accounts.py cutover-home codex2", accounts.status_report())


class CodexCutoverProcessListingTests(unittest.TestCase):
    """Platform adapters preserve actionable commands without exposing environments."""

    def test_linux_codex_listing_keeps_command_and_reads_only_candidate_home_settings(self) -> None:
        listing = subprocess.CompletedProcess([], 0,
            "321 codex codex app-server daemon\n654 codex /opt/codex --remote-control\n777 python python worker.py\n", "")
        settings = [account.ProcessWithoutCodexHome(), account.ProcessEnvironmentUnread("denied")]
        with (patch.object(sys, "platform", "linux"),
              patch.object(subprocess, "run", return_value=listing),
              patch.object(account, "process_codex_home", side_effect=settings) as read_home):
            found = accounts.list_codex_home_processes()
        self.assertEqual([process.pid for process in found], [321, 654])
        self.assertEqual([process.command for process in found], ["codex app-server daemon", "/opt/codex --remote-control"])
        self.assertEqual([process.setting for process in found], settings)
        self.assertEqual([call.args for call in read_home.call_args_list], [(321,), (654,)])

    def test_mac_codex_listing_keeps_full_kernel_argv_without_environment_and_fails_closed_on_unreadable_records(self) -> None:
        arguments = {
            321: ("/Applications/Codex Tools/codex", "-c", "key=value", "exec", "user's prompt"),
            654: ("codex", "exec", "CODEX_HOME=/scratch/prompt"),
        }
        environments = {
            321: b"HOME=/scratch\0CODEX_HOME=/scratch/selected home\0FIXTURE_VALUE=private-marker\0",
            654: b"HOME=/scratch\0FIXTURE_VALUE=private-marker\0",
        }
        def kernel_record(pid: int) -> bytes:
            if pid == 987:
                raise PermissionError("denied")
            argv = arguments[pid]
            return (len(argv).to_bytes(4, sys.byteorder, signed=True)
                    + os.fsencode(argv[0]) + b"\0\0\0"
                    + b"\0".join(os.fsencode(word) for word in argv) + b"\0" + environments[pid])
        listing = subprocess.CompletedProcess([], 0, "  321 codex           \n  654 codex           \n  987 codex           \n  777 python          \n  888 Codex (Service) \n", "")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(subprocess, "run", return_value=listing) as listed,
              patch.object(account, "kern_procargs2", side_effect=kernel_record),
              patch.object(account, "process_codex_home", wraps=account.process_codex_home) as read_home):
            found = accounts.list_codex_home_processes()
        listed.assert_called_once_with(("ps", "-u", str(os.getuid()), "-o", "pid=,ucomm="),
                                       capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual([process.pid for process in found], [321, 654, 987])
        self.assertEqual([process.command for process in found],
                         [shlex.join(arguments[321]), shlex.join(arguments[654]), "codex"])
        self.assertEqual(found[0].setting, account.ProcessCodexHome(Path("/scratch/selected home")))
        self.assertIsInstance(found[1].setting, account.ProcessWithoutCodexHome)
        self.assertIsInstance(found[2].setting, account.ProcessEnvironmentUnread)
        self.assertEqual([call.args for call in read_home.call_args_list], [(321,), (654,), (987,)])
        self.assertTrue(all("private-marker" not in process.command for process in found))

    def test_codex_process_listing_failure_refuses_instead_of_assuming_no_live_process(self) -> None:
        with patch.object(subprocess, "run", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(accounts.AccountsError, "cannot list Codex processes; no login moved"):
                _ = accounts.list_codex_home_processes()


class CutoverKeychain(ServiceKeychain):
    """A service-only fake with the effective keychain search list."""

    def __init__(self) -> None:
        super().__init__()
        self.search_list: tuple[Path, ...] = ()
        self.search_calls: list[tuple[str, ...]] = []

    @override
    def run(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        argv = tuple(arguments)
        if argv == ("security", "list-keychains"):
            self.search_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0,
                "".join(f"    {json.dumps(str(path))}\n" for path in self.search_list), "")
        return super().run(arguments, **_options)


class MacHomeCutoverTests(unittest.TestCase):
    """A verified named login permits removal of the Mac's legacy local copy."""

    home: Path = Path()
    keychain: CutoverKeychain = CutoverKeychain()
    output: io.StringIO = io.StringIO()
    errors: io.StringIO = io.StringIO()
    identity: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home),
            "AGENT_NOTES_DIR": str(self.home / "notes")}, clear=True))
        self.keychain = CutoverKeychain()
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.keychain.run))
        _ = self.enterContext(patch.object(sys, "platform", "darwin"))
        _ = self.enterContext(patch.object(sessions, "live_sessions", return_value=[]))
        self.output, self.errors = io.StringIO(), io.StringIO()
        _ = self.enterContext(redirect_stdout(self.output))
        _ = self.enterContext(redirect_stderr(self.errors))
        self.addCleanup(self.assert_private_output)
        self.identity = {"emailAddress": "synthetic-owner@example.invalid", "accountUuid": "synthetic-account-id"}
        self.initialize_home()

    def initialize_home(self) -> None:
        self.keychain.search_list = (self.home / "Library/Keychains/login.keychain-db",)
        (self.home / ".claude").mkdir()
        (self.home / "notes").mkdir()
        _ = (self.home / "notes/claude2.md").write_text("---\nlogin: synthetic-owner@example.invalid\n---\n")
        _ = (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": self.identity,
            "mcpServers": {"fixture": {}}, "projects": {"/project": {"hasTrustDialogAccepted": True}},
            "arbitrarySeed": [1, "kept"]}))
        self.assertEqual(accounts.setup("claude2"), [])
        _ = (self.home / ".claude2/.claude.json").write_text(json.dumps({"oauthAccount": self.identity,
            "projects": {"/named": {"hasTrustDialogAccepted": True}}}))
        for service, token in (("Claude Code-credentials", "synthetic-legacy-token"),
                               (folder_service(self.home / ".claude2"), "synthetic-numbered-token")):
            self.keychain.entries[service] = json.dumps({"oauthAccount": self.identity,
                "claudeAiOauth": {"accessToken": token, "refreshToken": "synthetic-refresh-secret"}})

    def assert_private_output(self) -> None:
        output = self.output.getvalue() + self.errors.getvalue()
        self.assertFalse(any(value in output for value in (*self.identity.values(),
            "synthetic-other-id", "synthetic-legacy-token", "synthetic-numbered-token",
            "synthetic-refresh-secret", "synthetic private diagnostic")))

    def snapshot(self) -> tuple[bytes, bytes, dict[str, str]]:
        return ((self.home / ".claude.json").read_bytes(),
                (self.home / ".claude2/.claude.json").read_bytes(), dict(self.keychain.entries))

    def deletes(self) -> list[tuple[str, ...]]:
        return [call for call in self.keychain.calls if call[1] == "delete-generic-password"]

    def test_system_only_search_list_refuses_before_journal_identity_or_keychain_changes(self) -> None:
        self.keychain.search_list = (Path("/Library/Keychains/System.keychain"),)
        before = self.snapshot()
        self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIn("accounts: this session cannot see the Mac's login keychain; "
                      + "run cutover-home in a Terminal on the Mac", self.errors.getvalue())
        self.assertTrue(self.snapshot() == before, "search-list refusal changed identity or keychain state")
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())
        self.assertEqual(self.keychain.calls, [])
        self.assertEqual(self.keychain.search_calls, [("security", "list-keychains")])

    def test_completed_cutover_undo_needs_no_login_keychain_or_keychain_calls(self) -> None:
        original = self.snapshot()
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.keychain.search_list = (Path("/Library/Keychains/System.keychain"),)
        self.keychain.calls.clear()
        self.keychain.search_calls.clear()
        entries_before = dict(self.keychain.entries)
        self.assertEqual(accounts.main(["cutover-home", "--undo"]), 0)
        restored = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
        self.assertTrue(restored["oauthAccount"] == self.identity)
        self.assertTrue((self.home / ".claude2/.claude.json").read_bytes() == original[1])
        self.assertTrue(self.keychain.entries == entries_before, "undo changed keychain state")
        self.assertEqual(self.keychain.calls, [])
        self.assertEqual(self.keychain.search_calls, [])

    def test_forward_cutover_accepts_the_legacy_login_keychain_filename(self) -> None:
        self.keychain.search_list = (self.home / "Library/Keychains/login.keychain",)
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)
        self.assertEqual(len(self.deletes()), 1)

    def test_completed_undo_allows_another_forward_cutover(self) -> None:
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertEqual(accounts.main(["cutover-home", "--undo"]), 0)
        # The user's separate login restores the legacy keychain after undo.
        self.keychain.entries["Claude Code-credentials"] = "synthetic-restored-entry"
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)
        self.assertEqual(len(self.deletes()), 2)

    def test_undo_before_identity_removal_allows_a_fresh_cutover(self) -> None:
        write = accounts._atomic_write  # pyright: ignore[reportPrivateUsage]
        journal_path = self.home / ".local/state/accounts/cutover-home.json"
        def interrupt(path: Path, contents: str) -> None:
            write(path, contents)
            if path == journal_path:
                raise OSError("interrupted after intent")
        before = self.snapshot()
        with patch.object(accounts, "_atomic_write", side_effect=interrupt):
            self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertTrue(self.snapshot() == before)
        self.assertEqual(accounts.main(["cutover-home", "--undo"]), 0)
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)

    def test_mac_unrelated_node_process_does_not_block_cutover(self) -> None:
        def run(arguments: tuple[str, ...], **options: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "ps":
                return subprocess.CompletedProcess(arguments, 0, "321 node node /opt/vite/bin/vite.js\n", "")
            return self.keychain.run(arguments, **options)
        with (patch.object(subprocess, "run", side_effect=run),
              patch.object(accounts, "_mac_process_arguments",
                           return_value=["node", "/opt/vite/bin/vite.js"]),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()) as setting):
            self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        setting.assert_not_called()
        self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)

    def test_mac_kernel_arguments_preserve_claude_package_path_and_prompt(self) -> None:
        arguments = ["node", "/Applications/Claude Code/@anthropic-ai/claude-code/cli.js", "-p", "user's prompt"]
        payload = (len(arguments).to_bytes(4, sys.byteorder, signed=True) + b"/usr/bin/node\0\0\0" +
                   b"\0".join(word.encode() for word in arguments) + b"\0")
        def read_kernel(query: ctypes.Array[ctypes.c_int], count: int, output: ctypes.Array[ctypes.c_char],
                        size: ctypes.c_void_p, new: object, new_size: int) -> int:
            self.assertEqual((list(query), count, new, new_size), ([1, 49, 321], 3, None, 0))
            _ = ctypes.memmove(output, payload, len(payload))
            ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(payload)
            return 0
        listing = subprocess.CompletedProcess([], 0, "321 node node flattened text\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(ctypes, "CDLL", return_value=SimpleNamespace(sysctl=read_kernel)),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting())):
            self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIn("live session", self.errors.getvalue())

    def test_mac_unreadable_candidate_arguments_refuse_before_identity_changes(self) -> None:
        before = self.snapshot()
        listing = subprocess.CompletedProcess([], 0, "321 node node /opt/vite/bin/vite.js\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(ctypes, "CDLL", return_value=SimpleNamespace(sysctl=Mock(return_value=-1)))):
            self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertTrue(self.snapshot() == before)
        self.assertIn("cannot read process 321 arguments", self.errors.getvalue())

    def test_mac_claude_node_process_without_setting_blocks_cutover(self) -> None:
        before = self.snapshot()
        listing = subprocess.CompletedProcess([], 0,
            "321 node node /opt/@anthropic-ai/claude-code/cli.js -p user's prompt\n", "")
        with (patch.object(subprocess, "run", return_value=listing),
              patch.object(accounts, "_mac_process_arguments",
                           return_value=["node", "/opt/@anthropic-ai/claude-code/cli.js", "-p", "user's prompt"]),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting()) as setting):
            self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        setting.assert_called_once_with(321)
        self.assertTrue(self.snapshot() == before)

    def test_mismatched_named_identity_refuses_without_file_or_keychain_changes(self) -> None:
        numbered = self.home / ".claude2/.claude.json"
        _ = numbered.write_text(json.dumps({"oauthAccount": {**self.identity, "accountUuid": "synthetic-other-id"}}))
        before = self.snapshot()
        calls_before = tuple(self.keychain.calls)
        self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertTrue(self.snapshot() == before, "mismatch changed identity state")
        self.assertEqual(tuple(self.keychain.calls), calls_before)
        self.assertEqual(self.deletes(), [])
        self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())

    def test_named_login_requires_both_json_identity_and_its_own_keychain_entry(self) -> None:
        numbered = self.home / ".claude2/.claude.json"
        initial = numbered.read_bytes()
        for missing in ("json", "keychain"):
            with self.subTest(missing=missing):
                _ = numbered.write_bytes(initial)
                if missing == "json":
                    _ = numbered.write_text("{}")
                else:
                    del self.keychain.entries[folder_service(self.home / ".claude2")]
                before = self.snapshot()
                self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
                self.assertTrue(self.snapshot() == before, "missing login changed identity state")
                self.assertEqual(self.deletes(), [])
                self.assertFalse((self.home / ".local/state/accounts/cutover-home.json").exists())

    def test_live_legacy_process_prevents_mac_cutover_before_mutation(self) -> None:
        before = self.snapshot()
        with (patch.object(accounts, "_claude_process_ids", return_value=[321]),
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting())):
            self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertTrue(self.snapshot() == before, "live session refusal changed identity state")
        self.assertEqual(self.deletes(), [])

    def test_mac_cutover_keeps_seed_deletes_only_legacy_service_and_dates_history(self) -> None:
        seed_before = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
        numbered_before = (self.home / ".claude2/.claude.json").read_bytes()
        named_credentials = self.keychain.entries[folder_service(self.home / ".claude2")]
        def deletion_preconditions() -> None:
            self.assertTrue((self.home / ".local/state/accounts/cutover-home.json").is_file())
            seed = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
            self.assertNotIn("oauthAccount", seed)
        self.keychain.after_delete = deletion_preconditions
        began = datetime.now(timezone.utc)
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        ended = datetime.now(timezone.utc)
        seed = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
        del seed_before["oauthAccount"]
        self.assertEqual(seed, seed_before)
        self.assertTrue((self.home / ".claude2/.claude.json").read_bytes() == numbered_before)
        self.assertTrue(self.keychain.entries[folder_service(self.home / ".claude2")] == named_credentials)
        self.assertEqual(self.deletes(), [("security", "delete-generic-password", "-s", "Claude Code-credentials")])
        self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)
        config = cast(dict[str, object], json.loads(accounts.accounts_config_file().read_text()))
        history = cast(list[dict[str, str]], config["home_cutovers"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["label"], "claude2")
        self.assertTrue(history[0]["login"] == self.identity["emailAddress"])
        self.assertTrue(began <= datetime.fromisoformat(history[0]["at"]) <= ended)
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertEqual(len(self.deletes()), 1)

    def test_undo_restores_home_identity_and_explains_required_home_login(self) -> None:
        original = self.snapshot()
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertEqual(accounts.main(["cutover-home", "--undo"]), 0)
        restored = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
        self.assertTrue(restored["oauthAccount"] == self.identity)
        self.assertTrue((self.home / ".claude2/.claude.json").read_bytes() == original[1])
        self.assertNotIn("Claude Code-credentials", self.keychain.entries)
        self.assertEqual(len(self.deletes()), 1)
        output = self.output.getvalue() + self.errors.getvalue()
        self.assertIn("/login", output)
        self.assertIn("~/.claude", output)

    def test_live_numbered_process_prevents_mac_undo_before_mutation(self) -> None:
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        before = self.snapshot()
        with (patch.object(accounts, "_claude_process_ids", return_value=[321]),
              patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(self.home / ".claude2"))):
            self.assertNotEqual(accounts.main(["cutover-home", "--undo"]), 0)
        self.assertTrue(self.snapshot() == before, "live numbered session refusal changed identity state")
        self.assertEqual(len(self.deletes()), 1)

    def test_interruption_after_each_durable_cutover_step_can_finish_and_undo(self) -> None:
        write = accounts._atomic_write  # pyright: ignore[reportPrivateUsage]
        mutations: list[str] = []
        def observe(path: Path, contents: str) -> None:
            write(path, contents)
            mutations.append("write")
        self.keychain.after_delete = lambda: mutations.append("delete")
        with patch.object(accounts, "_atomic_write", side_effect=observe):
            self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIn("delete", mutations)
        self.assertGreaterEqual(mutations.count("write"), 3)
        for stop_after in range(1, len(mutations) + 1):
            with self.subTest(step=stop_after), tempfile.TemporaryDirectory() as directory:
                self.home = Path(directory).resolve()
                with patch.dict(os.environ, {"HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home / "notes")}):
                    self.keychain.entries.clear()
                    self.keychain.calls.clear()
                    self.initialize_home()
                    completed = 0
                    def interrupted_step() -> None:
                        nonlocal completed
                        completed += 1
                        if completed == stop_after:
                            raise OSError("synthetic interruption")
                    def interrupted_write(path: Path, contents: str) -> None:
                        write(path, contents)
                        interrupted_step()
                    self.keychain.after_delete = interrupted_step
                    with patch.object(accounts, "_atomic_write", side_effect=interrupted_write):
                        self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
                    self.keychain.after_delete = lambda: None
                    self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
                    self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)
                    self.assertTrue(self.deletes())
                    self.assertTrue(all(call[3] == "Claude Code-credentials" for call in self.deletes()))
                    self.assertNotIn("Claude Code-credentials", self.keychain.entries)
                    config = cast(dict[str, object], json.loads(accounts.accounts_config_file().read_text()))
                    self.assertEqual(len(cast(list[object], config["home_cutovers"])), 1)
                    self.assertEqual(accounts.main(["cutover-home", "--undo"]), 0)
                    restored = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
                    self.assertTrue(restored["oauthAccount"] == self.identity)

    def test_keychain_delete_failure_is_private_and_rerun_finishes(self) -> None:
        run = self.keychain.run
        def denied(arguments: tuple[str, ...], **options: object) -> subprocess.CompletedProcess[str]:
            if arguments[1] == "delete-generic-password":
                return subprocess.CompletedProcess(arguments, 1, "", "synthetic-refresh-secret")
            return run(arguments, **options)
        with patch.object(subprocess, "run", side_effect=denied):
            self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIn("Claude Code-credentials", self.keychain.entries)
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertIsInstance(account.home_account_state(), account.ConfigOnlyHome)
        self.assertNotIn("Claude Code-credentials", self.keychain.entries)

    def test_interrupted_undo_can_be_repeated_without_changing_named_login(self) -> None:
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        numbered_before = (self.home / ".claude2/.claude.json").read_bytes()
        write = accounts._atomic_write  # pyright: ignore[reportPrivateUsage]
        def interrupted(path: Path, contents: str) -> None:
            write(path, contents)
            if path == self.home / ".claude.json":
                raise OSError("synthetic interruption")
        with patch.object(accounts, "_atomic_write", side_effect=interrupted):
            self.assertNotEqual(accounts.main(["cutover-home", "--undo"]), 0)
        self.assertEqual(accounts.main(["cutover-home", "--undo"]), 0)
        self.assertTrue((self.home / ".claude2/.claude.json").read_bytes() == numbered_before)
        restored = cast(dict[str, object], json.loads((self.home / ".claude.json").read_text()))
        self.assertTrue(restored["oauthAccount"] == self.identity)
        self.assertEqual(len(self.deletes()), 1)

    def test_keychain_presence_access_error_refuses_instead_of_finishing_cutover(self) -> None:
        run = self.keychain.run
        for service in (folder_service(self.home / ".claude2"), "Claude Code-credentials"):
            with self.subTest(service_kind="legacy" if service == "Claude Code-credentials" else "numbered"):
                before = self.snapshot()
                def denied(arguments: tuple[str, ...], **options: object) -> subprocess.CompletedProcess[str]:
                    if arguments[1:] == ("find-generic-password", "-s", service):
                        return subprocess.CompletedProcess(arguments, 1, "", "synthetic-refresh-secret")
                    return run(arguments, **options)
                with patch.object(subprocess, "run", side_effect=denied):
                    self.assertNotEqual(accounts.main(["cutover-home", "claude2"]), 0)
                self.assertTrue(self.keychain.entries == before[2], "presence error changed keychain state")
                self.assertTrue((self.home / ".claude2/.claude.json").read_bytes() == before[1])
                self.assertEqual(self.deletes(), [])
                journal = self.home / ".local/state/accounts/cutover-home.json"
                if journal.exists():
                    state = cast(dict[str, object], json.loads(journal.read_text()))
                    self.assertNotEqual(state["state"], "complete")
                else:
                    self.assertTrue((self.home / ".claude.json").read_bytes() == before[0])
        self.assertEqual(accounts.main(["cutover-home", "claude2"]), 0)
        self.assertNotIn("Claude Code-credentials", self.keychain.entries)
