from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.accounts import accounts, new_account
from scripts.whoami import agent_accounts
from scripts.whoami import note_file


def shipped_color_expectations(text: str) -> tuple[str, str, bytes]:
    account_colors: set[str] = set()
    role_colors: set[str] = set()
    account_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        key, separator, value = line.split("#", 1)[0].strip().partition("=")
        key = key.strip()
        if separator and key.startswith(("account.", "role.")):
            if key.startswith("account."):
                account_colors.add(value.strip())
                account_lines.append(line)
            else:
                role_colors.add(value.strip())
    available = [color for color in new_account.STATUSLINE_PALETTE
                 if color not in new_account.STATUSLINE_EXCLUDED_COLORS and color not in account_colors]
    # Once distinct colors run out, new accounts may share the existing role colors.
    free = [color for color in available if color not in role_colors]
    free.extend(color for color in available if color in role_colors)
    return free[0], free[1], account_lines[-1].encode("utf-8")


SHIPPED_STATUSLINE = (Path(__file__).resolve().parents[2] / "config/statusline.conf").read_bytes()
FIRST_FREE_COLOR, SECOND_FREE_COLOR, LAST_SHIPPED_ACCOUNT_LINE = shipped_color_expectations(
    SHIPPED_STATUSLINE.decode("utf-8"))


class NewAccountTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home: Path = Path()
        self.notes: Path = Path()
        self.nixos: Path = Path()
        self.colors: Path = Path()

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
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("no real process")))
        _ = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("no real process")))
        self.write_settings([])
        nightly = self.home / ".claude/scripts/nightly_review/settings.json"
        nightly.parent.mkdir(parents=True)
        _ = nightly.write_text(json.dumps({"permissions": {"deny": ["Edit(~/.claude/**)"]}}))
        self.colors = self.home / ".claude/config/statusline.conf"
        self.colors.parent.mkdir()
        _ = self.colors.write_bytes(SHIPPED_STATUSLINE)

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

    def test_parser_accepts_both_spellings_and_derives_the_note_filename(self) -> None:
        for name in ("claude4", "claude 4"):
            with self.subTest(name=name):
                parsed = new_account.parse_account("claude", name)
                self.assertEqual(parsed.label, "claude4")
                self.assertEqual(new_account.note_path(parsed), self.notes / "claude4.md")

    def test_note_step_waits_for_a_login_then_writes_the_supplied_identity(self) -> None:
        account = new_account.parse_account("claude", "claude4")

        pending = new_account.note_step(account, new_account.NoHomeLogin())

        self.assertFalse(pending.done)
        self.assertIn("claude4 needs its login", pending.text)
        self.assertEqual(list(self.notes.iterdir()), [])

        written = new_account.note_step(account, new_account.HomeLogin("four@example.invalid"))

        self.assertTrue(written.done, written.text)
        note = note_file.read_note(self.notes / "claude4.md")
        self.assertIsInstance(note, note_file.Note)
        assert isinstance(note, note_file.Note)
        self.assertEqual(note.get("login"), "four@example.invalid")

    def test_direct_new_account_uses_a_supplied_home_login_without_a_signed_in_home(self) -> None:
        account = new_account.parse_account("claude", "claude4")

        steps = new_account.new_account(account, new_account.HomeLogin("four@example.invalid"))

        note = note_file.read_note(self.notes / "claude4.md")
        self.assertIsInstance(note, note_file.Note)
        assert isinstance(note, note_file.Note)
        self.assertEqual(note.get("login"), "four@example.invalid")
        self.assertTrue(any(not step.done and "log in as claude4" in step.text for step in steps))

    def test_direct_new_account_without_a_supplied_login_discovers_its_signed_in_home(self) -> None:
        account = new_account.parse_account("claude", "claude4")
        self.assertEqual(accounts.setup(account.label), [])
        self.log_in("claude4", "discovered@example.invalid")

        steps = new_account.new_account(account, new_account.NoHomeLogin())

        note = note_file.read_note(self.notes / "claude4.md")
        self.assertIsInstance(note, note_file.Note)
        assert isinstance(note, note_file.Note)
        self.assertEqual(note.get("login"), "discovered@example.invalid")
        self.assertTrue(any(step.done and "~/.claude4 is logged in" in step.text for step in steps))

    def test_refuses_another_type_or_no_number(self) -> None:
        for argv in (("claude", "codex3"), ("codex", "codex"), ("gemini", "gemini1"),
                     ("claude", "claude0")):
            code, stdout, stderr = self.run_cli(*argv)
            self.assertEqual((code, stdout), (1, ""), argv)
            self.assertIn("new_account:", stderr)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".claude", ".claude.json"])

    def test_short_and_legacy_commands_add_one_explicit_nightly_deny(self) -> None:
        settings = self.home / ".claude/scripts/nightly_review/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        _ = settings.write_text(json.dumps({"permissions": {"deny": ["Edit(~/.claude/**)"]}}))
        for arguments in (("claude4",), ("claude", "claude 4")):
            _, stdout, stderr = self.run_cli(*arguments)
            self.assertEqual(stderr, "")
            self.assertNotRegex(stdout, r"(?:claude|codex) [1-9][0-9]*")
            saved = cast(dict[str, dict[str, list[str]]], json.loads(settings.read_text()))
            self.assertEqual(saved["permissions"]["deny"].count("Edit(~/.claude4/**)"), 1)
            self.assertIn("Edit(~/.claude/**)", saved["permissions"]["deny"])
        self.log_in("claude4", "four@example.invalid")
        _, stdout, _ = self.run_cli("claude4")
        self.assertTrue((self.notes / "claude4.md").exists())
        self.assertFalse((self.notes / "claude 4.md").exists())
        self.assertNotRegex(stdout, r"(?:claude|codex) [1-9][0-9]*")

    def test_claude_first_run_sets_up_and_leaves_login_note_settings_and_alias(self) -> None:
        code, stdout, _ = self.run_cli("claude", "claude4")
        self.assertEqual(code, 1)
        self.assertTrue((self.home / ".claude4" / "settings.json").is_symlink())
        self.assertIn("done: set up ~/.claude4", stdout)
        self.assertIn("for you: claude4 needs its login in an account note", stdout)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.home / '.claude4'} claude", stdout)
        self.assertIn('permissions.allow: "Edit(~/.claude4/**)"', stdout)
        self.assertIn('sandbox.filesystem.allowWrite: "~/.claude4"', stdout)
        self.assertIn("claude4 = \"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude4\";",
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
        note = (self.notes / "claude4.md").read_text(encoding="utf-8")
        self.assertIn("login: four@example.invalid\n", note)
        self.assertIn("state: inactive\n", note)
        self.assertTrue(note.startswith('---\ndate_created: "[['))
        self.assertIn("done: ~/.claude4 is logged in", stdout)
        self.assertIn("done: claude4 alias: comes from the account note", stdout)
        self.assertTrue(stdout.endswith("claude4 is ready; /showrunner:accounts shows its row.\n"))
        self.assertIsInstance(accounts.account_directory_readiness("claude4"), accounts.AccountDirectoryReady)

    def test_new_claude_account_inserts_the_first_free_color_after_the_last_shipped_account_without_changing_other_bytes(self) -> None:
        before = self.colors.read_bytes()

        _, stdout, stderr = self.run_cli("claude5")

        self.assertEqual(stderr, "")
        self.assertIn(f"done: status line color {FIRST_FREE_COLOR} for claude5", stdout)
        self.assertLess(stdout.index("nightly review cannot edit ~/.claude5"),
                        stdout.index(f"status line color {FIRST_FREE_COLOR} for claude5"))
        self.assertEqual(self.colors.read_bytes(), before.replace(
            LAST_SHIPPED_ACCOUNT_LINE,
            LAST_SHIPPED_ACCOUNT_LINE + f"account.claude5={FIRST_FREE_COLOR}\n".encode("utf-8"), 1))

    def test_next_claude_account_gets_the_second_free_color_after_the_first_is_taken(self) -> None:
        before = self.colors.read_bytes()
        first = new_account.statusline_color_step(new_account.parse_account("claude", "claude5"))

        second = new_account.statusline_color_step(new_account.parse_account("claude", "claude6"))

        self.assertTrue(first.done, first.text)
        self.assertTrue(second.done, second.text)
        self.assertEqual(first.text, f"status line color {FIRST_FREE_COLOR} for claude5")
        self.assertEqual(second.text, f"status line color {SECOND_FREE_COLOR} for claude6")
        self.assertEqual(self.colors.read_bytes(), before.replace(
            LAST_SHIPPED_ACCOUNT_LINE,
            LAST_SHIPPED_ACCOUNT_LINE + (f"account.claude5={FIRST_FREE_COLOR}\n"
                + f"account.claude6={SECOND_FREE_COLOR}\n").encode("utf-8"), 1))

    def test_adding_an_account_color_preserves_restrictive_config_permissions_under_a_looser_umask(self) -> None:
        before = self.colors.read_bytes()
        for mode in (0o640, 0o600):
            with self.subTest(mode=oct(mode)):
                _ = self.colors.write_bytes(before)
                self.colors.chmod(mode)
                previous_umask = os.umask(0o022)
                try:
                    step = new_account.statusline_color_step(new_account.parse_account("claude", "claude5"))
                finally:
                    _ = os.umask(previous_umask)

                self.assertTrue(step.done, step.text)
                self.assertIn(f"account.claude5={FIRST_FREE_COLOR}\n", self.colors.read_text(encoding="utf-8"))
                self.assertEqual(stat.S_IMODE(self.colors.stat().st_mode), mode)

    def test_existing_account_color_is_reported_without_rewriting_the_file(self) -> None:
        account = new_account.parse_account("claude", "claude5")
        first = new_account.statusline_color_step(account)
        before = self.colors.read_bytes()
        before_stat = self.colors.stat()

        with patch.object(os, "replace", wraps=os.replace) as replace:
            repeated = new_account.statusline_color_step(account)

        self.assertTrue(repeated.done, repeated.text)
        self.assertEqual(repeated.text, f"status line color {FIRST_FREE_COLOR} for claude5")
        self.assertEqual(repeated, first)
        self.assertEqual(self.colors.read_bytes(), before)
        self.assertEqual((self.colors.stat().st_ino, self.colors.stat().st_mtime_ns),
                         (before_stat.st_ino, before_stat.st_mtime_ns))
        replace.assert_not_called()

    def test_exhausted_distinct_colors_fall_back_to_role_colors_then_leave_the_account_uncolored(self) -> None:
        _ = self.colors.write_text(
            "# Only the role color purple and three unused colors remain for accounts.\n"
            + "role.showrunner=purple\nrole.unit-director=yellow\n"
            + "account.claude1=blue\naccount.claude2=magenta\naccount.claude3=cyan\n"
            + "account.claude4=orange\naccount.claude10=pink\naccount.claude11=teal\naccount.claude12=brown\n"
            + "account.claude13=mauve\n",
            encoding="utf-8")
        for label, color in (("claude5", "bright-blue"), ("claude6", "bright-magenta"),
                             ("claude7", "bright-cyan"), ("claude8", "purple")):
            with self.subTest(label=label):
                step = new_account.statusline_color_step(new_account.parse_account("claude", label))
                self.assertTrue(step.done, step.text)
                self.assertEqual(step.text, f"status line color {color} for {label}")
                self.assertIn(f"account.{label}={color}\n", self.colors.read_text(encoding="utf-8"))
        before = self.colors.read_bytes()

        step = new_account.statusline_color_step(new_account.parse_account("claude", "claude9"))

        self.assertTrue(step.done, step.text)
        self.assertIn("claude9 stays uncolored", step.text)
        self.assertEqual(self.colors.read_bytes(), before)

    def test_missing_status_line_config_is_done_without_creating_a_file(self) -> None:
        self.colors.unlink()

        step = new_account.statusline_color_step(new_account.parse_account("claude", "claude5"))

        self.assertTrue(step.done, step.text)
        self.assertIn("claude5 stays uncolored", step.text)
        self.assertFalse(self.colors.exists())

    def test_unreadable_status_line_config_reports_for_you_with_its_path_and_error(self) -> None:
        self.colors.unlink()
        self.colors.mkdir()

        code, stdout, stderr = self.run_cli("claude5")

        self.assertEqual((code, stderr), (1, ""))
        self.assertIn(f"for you: could not update {self.colors}:", stdout)
        self.assertIn("Is a directory", stdout)
        self.assertTrue(self.colors.is_dir())

    def test_a_glob_permission_rule_covers_the_folder_but_not_a_linux_sandbox_entry(self) -> None:
        self.write_settings(["~/.claude[0-9]*"])
        _, stdout, _ = self.run_cli("claude", "claude4")
        self.assertNotIn("permissions.allow", stdout)
        self.assertIn('sandbox.filesystem.allowWrite: "~/.claude4"', stdout)
        with patch.object(sys, "platform", "darwin"):
            _, stdout, _ = self.run_cli("claude", "claude4")
        self.assertIn("done: ~/.claude/settings.json covers ~/.claude4", stdout)

    def test_a_login_another_note_holds_is_refused(self) -> None:
        _ = (self.notes / "claude1.md").write_text("---\nlogin: same@example.invalid\n---\n", encoding="utf-8")
        code, stdout, _ = self.run_cli("claude", "claude4", "SAME@example.invalid")
        self.assertEqual(code, 1)
        self.assertIn("for you: claude1.md already holds that login", stdout)
        self.assertFalse((self.notes / "claude4.md").exists())

    def test_rerun_with_only_its_legacy_note_continues_instead_of_refusing_its_login(self) -> None:
        _ = self.run_cli("claude4")
        self.log_in("claude4", "four@example.invalid")
        self.write_settings(["~/.claude4"])
        _ = self.nixos.write_text("for note in ~/rust/hanadocs/agents/claude(| )<->.md(N); do\n")
        body = "---\nlogin: four@example.invalid\n---\nKeep this account's notes.\n"
        _ = (self.notes / "claude 4.md").write_text(body)

        code, stdout, stderr = self.run_cli("claude4")

        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertIn("claude4 is ready", stdout)
        self.assertNotIn("already holds that login", stdout)
        with patch.object(note_file, "AGENTS_DIR", self.notes):
            notes = note_file.read_notes()
        self.assertEqual([(note.label, note.text) for note in notes], [("claude4", body)])

    def test_codex_without_a_login_stops_before_setup(self) -> None:
        code, stdout, _ = self.run_cli("codex", "codex3")
        self.assertEqual(code, 1)
        self.assertIn("run /new_account codex codex3 <its login>", stdout)
        self.assertFalse((self.home / ".codex3").exists())

    def test_codex_with_a_login_writes_the_note_sets_up_and_prints_sign_in(self) -> None:
        colors_before = self.colors.read_bytes()
        (self.home / ".codex").mkdir()
        _ = (self.home / ".codex" / "config.toml").write_text("", encoding="utf-8")
        with patch.object(agent_accounts, "codex_homes", return_value=[self.home / ".codex"]), \
                patch.object(agent_accounts, "codex_home", return_value=self.home / ".codex"):
            code, stdout, _ = self.run_cli("codex", "codex3", "three@example.invalid")
        self.assertEqual(code, 1, stdout)
        self.assertIn("login: three@example.invalid\n", (self.notes / "codex3.md").read_text(encoding="utf-8"))
        self.assertTrue((self.home / ".codex3" / "config.toml").is_symlink())
        self.assertIn("CODEX_HOME=~/.codex3 codex login", stdout)
        self.assertNotIn("settings.json", stdout)
        self.assertNotIn("status line color", stdout)
        self.assertNotIn("stays uncolored", stdout)
        self.assertEqual(self.colors.read_bytes(), colors_before)

    def test_mac_existing_alias_is_done_and_names_the_darwin_module(self) -> None:
        module = self.nixos.parent / "modules/darwin/shell.nix"
        module.parent.mkdir(parents=True)
        _ = module.write_text('  claude5 = "existing command";\n')
        with (patch.object(sys, "platform", "darwin"),
              patch.object(new_account, "nixos_shell_module", return_value=module)):
            _, stdout, stderr = self.run_cli("claude", "claude5")
        self.assertEqual(stderr, "")
        self.assertIn(f"done: claude5 alias in {module} (live after a rebuild)", stdout)
        self.assertNotIn("add to shellAliases", stdout)

    def test_mac_missing_alias_prints_the_add_line_in_the_darwin_module(self) -> None:
        module = self.nixos.parent / "modules/darwin/shell.nix"
        module.parent.mkdir(parents=True)
        _ = module.write_text('  claude1 = "existing command";\n')
        with (patch.object(sys, "platform", "darwin"),
              patch.object(new_account, "nixos_shell_module", return_value=module)):
            _, stdout, stderr = self.run_cli("claude", "claude5")
        self.assertEqual(stderr, "")
        self.assertIn(f"for you: add to shellAliases in {module}, then rebuild:", stdout)
        self.assertIn('claude5 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec claude5";', stdout)
        self.assertEqual(module.read_text(), '  claude1 = "existing command";\n')

    def test_missing_shell_module_names_the_account_and_platform_path(self) -> None:
        for platform, folder in (("darwin", "darwin"), ("linux", "linux")):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(new_account, "nixos_shell_module", return_value=None)):
                _, stdout, stderr = self.run_cli("claude", "claude5")
                self.assertEqual(stderr, "")
                self.assertIn("for you: no nixos shell module", stdout)
                self.assertIn("claude5 alias", stdout)
                self.assertIn(f"modules/{folder}/shell.nix", stdout)

    def test_mac_alias_loop_still_comes_from_the_account_note(self) -> None:
        _ = self.nixos.write_text(new_account.ALIAS_LOOP_MARKER)
        with patch.object(sys, "platform", "darwin"):
            _, stdout, stderr = self.run_cli("claude", "claude5")
        self.assertEqual(stderr, "")
        self.assertIn("done: claude5 alias: comes from the account note; open a new terminal", stdout)

    def test_claude_alias_loop_does_not_define_a_new_codex_alias(self) -> None:
        _ = self.nixos.write_text(new_account.ALIAS_LOOP_MARKER
            + '\n  codex1 = "existing command";\n  codex2 = "existing command";\n')
        account = new_account.parse_account("codex", "codex3")
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                step = new_account.alias_step(account)
                self.assertEqual(step, new_account.Step(False,
                    f'add to shellAliases in {self.nixos}, then rebuild:\n'
                    + '    codex3 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex3";'))

    def test_codex_setup_appends_alias_on_both_platforms_only_after_success(self) -> None:
        account = new_account.parse_account("codex", "codex3")
        for platform in ("linux", "darwin"):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(new_account, "home_login", return_value=new_account.NoHomeLogin()),
                  patch.object(accounts, "setup", return_value=["fixture setup failed"]) as setup,
                  patch.object(new_account, "alias_step", wraps=new_account.alias_step) as alias):
                self.assertEqual(new_account.codex_steps(account), [new_account.Step(False, "fixture setup failed")])
                alias.assert_not_called()
                setup.return_value = []
                steps = new_account.codex_steps(account)
                alias.assert_called_once_with(account)
                self.assertIn("set up ~/.codex3", steps[0].text)
                self.assertIn("codex login", steps[1].text)
                self.assertEqual(steps[-1], new_account.Step(False,
                    f'add to shellAliases in {self.nixos}, then rebuild:\n'
                    + '    codex3 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex3";'))

    def test_codex_signed_in_check_appends_alias_on_both_platforms_only_after_success(self) -> None:
        account = new_account.parse_account("codex", "codex3")
        for platform in ("linux", "darwin"):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(new_account, "home_login", return_value=new_account.HomeLogin("three@example.invalid")),
                  patch.object(accounts, "check", return_value=["fixture check failed"]) as check,
                  patch.object(new_account, "alias_step", wraps=new_account.alias_step) as alias):
                self.assertEqual(new_account.codex_steps(account), [new_account.Step(False, "fixture check failed")])
                alias.assert_not_called()
                check.return_value = []
                steps = new_account.codex_steps(account)
                alias.assert_called_once_with(account)
                self.assertIn("set up and signed in", steps[0].text)
                self.assertIn('codex3 = "~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec codex3";', steps[-1].text)
                self.assertFalse(steps[-1].done)


class ShellModuleTests(unittest.TestCase):
    def test_platform_module_lookup_prefers_etc_then_home_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_repository, home = root / "system-repository", root / "home"

            def repository_path(value: str) -> Path:
                return system_repository if value == "/etc/nixos" else Path(value)

            for platform in ("darwin", "linux"):
                for repository in (system_repository, home / "nixos"):
                    module = repository / f"modules/{platform}/shell.nix"
                    module.parent.mkdir(parents=True)
                    _ = module.write_text("# fixture\n")
            for platform, folder in (("darwin", "darwin"), ("linux", "linux"), ("freebsd", "linux")):
                with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                      patch.object(accounts, "accounts_home", return_value=home),
                      patch.object(new_account, "Path", side_effect=repository_path)):
                    expected = system_repository / f"modules/{folder}/shell.nix"
                    if not expected.exists():
                        _ = expected.write_text("# fixture\n")
                    self.assertEqual(new_account.nixos_shell_module(), expected)
                    expected.unlink()
                    fallback = home / f"nixos/modules/{folder}/shell.nix"
                    if not fallback.exists():
                        _ = fallback.write_text("# fixture\n")
                    self.assertEqual(new_account.nixos_shell_module(), fallback)
                    fallback.unlink()
                    self.assertIsNone(new_account.nixos_shell_module())


if __name__ == "__main__":
    _ = unittest.main()
