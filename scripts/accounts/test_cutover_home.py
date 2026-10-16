"""A configuration-only home retains seed data while identity moves by rename."""
from __future__ import annotations

import ctypes
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
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.accounts import accounts
from scripts.message import sessions
from scripts.whoami.test_agent_accounts import ServiceKeychain, folder_service
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


class MacHomeCutoverTests(unittest.TestCase):
    """A verified named login permits removal of the Mac's legacy local copy."""

    home: Path = Path()
    keychain: ServiceKeychain = ServiceKeychain()
    output: io.StringIO = io.StringIO()
    errors: io.StringIO = io.StringIO()
    identity: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home),
            "AGENT_NOTES_DIR": str(self.home / "notes")}, clear=True))
        self.keychain = ServiceKeychain()
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
                self.home = Path(directory)
                with patch.dict(os.environ, {"HOME": directory, "AGENT_NOTES_DIR": str(self.home / "notes")}):
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
