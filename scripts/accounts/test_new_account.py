from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.accounts import accounts, new_account
import agent_accounts


class NewAccountTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home: Path = Path()
        self.notes: Path = Path()
        self.nixos: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = root / "home"
        self.notes = root / "notes"
        self.nixos = root / "nixos" / "shell.nix"
        (self.home / ".claude").mkdir(parents=True)
        self.notes.mkdir()
        self.nixos.parent.mkdir()
        _ = self.nixos.write_text("claude1 = \"...\";\n", encoding="utf-8")
        environment: object = cast(object, self.enterContext(patch.dict(os.environ, {
            "AGENT_NOTES_DIR": str(self.notes), "HOME": str(self.home), "PATH": os.defpath,
        }, clear=True)))
        del environment
        _ = (self.home / ".claude.json").write_text("{}", encoding="utf-8")
        _ = self.enterContext(patch.object(new_account, "nixos_shell_module", return_value=self.nixos))
        _ = self.enterContext(patch.object(sys, "platform", "linux"))
        _ = self.enterContext(patch.object(agent_accounts, "prepare_codex_home",
                                           return_value=agent_accounts.CodexHomePrepared()))
        self.write_settings([])

    def write_settings(self, folders: list[str]) -> None:
        settings = {
            "permissions": {"allow": [rule for folder in folders for rule in (
                f"Read({folder}/projects/**/tool-results/**)", f"Edit({folder}/**)")]},
            "sandbox": {"filesystem": {"allowWrite": folders}},
        }
        _ = (self.home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def log_in(self, name: str, login: str) -> None:
        config = self.home / f".{name}" / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = new_account.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_label_has_the_space_accounts_py_accepts_today(self) -> None:
        self.assertEqual(new_account.account_label("claude4"), "claude 4")
        self.assertEqual(new_account.account_label("codex12"), "codex 12")

    def test_refuses_a_name_with_a_space_another_type_or_no_number(self) -> None:
        for argv in (("claude", "claude 4"), ("claude", "codex3"), ("codex", "codex"), ("gemini", "gemini1"),
                     ("claude", "claude0")):
            code, stdout, stderr = self.run_cli(*argv)
            self.assertEqual((code, stdout), (1, ""), argv)
            self.assertIn("new_account:", stderr)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".claude", ".claude.json"])

    def test_claude_first_run_sets_up_and_leaves_login_note_settings_and_alias(self) -> None:
        code, stdout, _ = self.run_cli("claude", "claude4")
        self.assertEqual(code, 1)
        self.assertTrue((self.home / ".claude4" / "settings.json").is_symlink())
        self.assertIn("done: set up ~/.claude4", stdout)
        self.assertIn("for you: claude 4 needs its login in an account note", stdout)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.home / '.claude4'} claude", stdout)
        self.assertIn('permissions.allow: "Edit(~/.claude4/**)"', stdout)
        self.assertIn('sandbox.filesystem.allowWrite: "~/.claude4"', stdout)
        self.assertIn("claude4 = \"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec 'claude 4'\";",
                      stdout)
        self.assertTrue(stdout.endswith("Then run /new_account claude claude4 again.\n"))
        self.assertEqual(list(self.notes.iterdir()), [])

    def test_claude_rerun_after_login_writes_the_note_and_finishes(self) -> None:
        _ = self.run_cli("claude", "claude4")
        self.log_in("claude4", "four@example.invalid")
        self.write_settings(["~/.claude4"])
        _ = self.nixos.write_text("for note in ~/rust/hanadocs/agents/claude(| )<->.md(N); do\n", encoding="utf-8")

        code, stdout, _ = self.run_cli("claude", "claude4")

        self.assertEqual(code, 0, stdout)
        note = (self.notes / "claude 4.md").read_text(encoding="utf-8")
        self.assertIn("login: four@example.invalid\n", note)
        self.assertIn("state: inactive\n", note)
        self.assertTrue(note.startswith('---\ndate_created: "[['))
        self.assertIn("done: ~/.claude4 is logged in", stdout)
        self.assertIn("done: claude4 alias: comes from the account note", stdout)
        self.assertTrue(stdout.endswith("claude4 is ready; /showrunner:accounts shows its row.\n"))
        self.assertIsInstance(accounts.account_directory_readiness("claude 4"), accounts.AccountDirectoryReady)

    def test_a_glob_permission_rule_covers_the_folder_but_not_a_linux_sandbox_entry(self) -> None:
        self.write_settings(["~/.claude[0-9]*"])
        _, stdout, _ = self.run_cli("claude", "claude4")
        self.assertNotIn("permissions.allow", stdout)
        self.assertIn('sandbox.filesystem.allowWrite: "~/.claude4"', stdout)
        with patch.object(sys, "platform", "darwin"):
            _, stdout, _ = self.run_cli("claude", "claude4")
        self.assertIn("done: ~/.claude/settings.json covers ~/.claude4", stdout)

    def test_a_login_another_note_holds_is_refused(self) -> None:
        _ = (self.notes / "claude 1.md").write_text("---\nlogin: same@example.invalid\n---\n", encoding="utf-8")
        code, stdout, _ = self.run_cli("claude", "claude4", "SAME@example.invalid")
        self.assertEqual(code, 1)
        self.assertIn("for you: claude 1.md already holds that login", stdout)
        self.assertFalse((self.notes / "claude 4.md").exists())

    def test_codex_without_a_login_stops_before_setup(self) -> None:
        code, stdout, _ = self.run_cli("codex", "codex3")
        self.assertEqual(code, 1)
        self.assertIn("run /new_account codex codex3 <its login>", stdout)
        self.assertFalse((self.home / ".codex3").exists())

    def test_codex_with_a_login_writes_the_note_sets_up_and_prints_sign_in(self) -> None:
        (self.home / ".codex").mkdir()
        _ = (self.home / ".codex" / "config.toml").write_text("", encoding="utf-8")
        with patch.object(agent_accounts, "codex_homes", return_value=[self.home / ".codex"]), \
                patch.object(agent_accounts, "codex_home", return_value=self.home / ".codex"):
            code, stdout, _ = self.run_cli("codex", "codex3", "three@example.invalid")
        self.assertEqual(code, 1, stdout)
        self.assertIn("login: three@example.invalid\n", (self.notes / "codex 3.md").read_text(encoding="utf-8"))
        self.assertTrue((self.home / ".codex3" / "config.toml").is_symlink())
        self.assertIn("CODEX_HOME=~/.codex3 codex login", stdout)
        self.assertNotIn("settings.json", stdout)

    def test_mac_has_no_alias_step(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            _, stdout, _ = self.run_cli("claude", "claude4")
        self.assertNotIn("alias", stdout)
        self.assertEqual([line for line in stdout.splitlines() if "shellAliases" in line], [])


if __name__ == "__main__":
    _ = unittest.main()
