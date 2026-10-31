"""Behavioral contract for account resolution and status-line labels."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.whoami import account, agent_accounts
from scripts.whoami.account_test_support import write_codex_auth


STATUS_LINE = Path(__file__).parents[1] / "statusline" / "statusline.jq"
EXAMPLE_FIRST_LINE = "unassigned | /tmp/claude_commands"
ACCOUNT_FIRST_LINE = EXAMPLE_FIRST_LINE
# The account leads the second line, then the model and tokens, then the quota windows.
MODEL_TOKENS = "Opus 5.5 high | 182,340"


class AccountTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.notes: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.notes = self.root / "agents"
        self.notes.mkdir()

    def write_claude_config(self, path: Path, login: str) -> None:
        _ = path.write_text(json.dumps({"oauthAccount": {"emailAddress": login}}))

    def write_codex_auth(self, home: Path, login: str) -> None:
        write_codex_auth(home, login)

    def write_note(self, name: str, login: str) -> None:
        _ = (self.notes / name).write_text(
            f"---\nlogin: {login}\n---\nBody stays.\n"
        )

    def environment_without_claude_config_dir(self) -> dict[str, str]:
        environment = dict(os.environ)
        _ = environment.pop("CLAUDE_CONFIG_DIR", None)
        environment["HOME"] = str(self.home)
        environment["AGENT_NOTES_DIR"] = str(self.notes)
        return environment

    def test_default_claude_config_dir_reads_dot_claude_json_from_home(self) -> None:
        config_dir = self.home / ".claude"
        config_dir.mkdir()
        self.write_claude_config(self.home / ".claude.json", "default@example.com")
        self.write_claude_config(config_dir / ".claude.json", "wrong@example.com")

        with (
            patch.dict(os.environ, self.environment_without_claude_config_dir(), clear=True),
            patch.object(Path, "home", return_value=self.home),
        ):
            resolved = account.claude_account(config_dir)

        self.assertEqual(
            resolved,
            account.Account("claude", "default@example.com", "default@example.com"),
        )

    def test_nondefault_claude_config_dir_reads_dot_claude_json_inside_it(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        self.write_claude_config(self.home / ".claude.json", "wrong@example.com")
        self.write_claude_config(config_dir / ".claude.json", "second@example.com")
        environment = self.environment_without_claude_config_dir()
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(Path, "home", return_value=self.home),
        ):
            resolved = account.claude_account(config_dir)

        self.assertEqual(
            resolved,
            account.Account("claude", "second@example.com", "second@example.com"),
        )

    def test_label_for_matches_tool_note_login_case_insensitively(self) -> None:
        self.write_note("codex2.md", "person@example.com")
        self.write_note("claude2.md", "Person@Example.COM")

        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            label = account.label_for("claude", "person@example.com")

        self.assertEqual(label, "claude2")

    def test_label_for_falls_back_to_login_without_a_matching_note(self) -> None:
        self.write_note("claude2.md", "someone-else@example.com")

        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            label = account.label_for("claude", "unlisted@example.com")

        self.assertEqual(label, "unlisted@example.com")

    def test_named_claude_account_resolves_label_and_login(self) -> None:
        self.write_note("claude2.md", "person@example.com")
        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            by_label = account.named_claude_account("CLAUDE 2")
            by_login = account.named_claude_account("other@example.com")

        self.assertEqual(
            by_label,
            account.Account("claude", "person@example.com", "claude2"),
        )
        self.assertEqual(
            by_login,
            account.Account("claude", "other@example.com", "other@example.com"),
        )

    def test_named_claude_account_rejects_unknown_name(self) -> None:
        with (
            patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}),
            self.assertRaisesRegex(
                account.UnknownAccountName,
                "unknown account claude2: give a note label such as claude2, or a login",
            ),
        ):
            _ = account.named_claude_account("claude2")

    def test_named_claude_account_rejects_path_containing_at_sign(self) -> None:
        with (
            patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}),
            self.assertRaises(account.UnknownAccountName),
        ):
            _ = account.named_claude_account("/tmp/x@example.com")

    def test_own_claude_account_raises_when_login_is_unreadable(self) -> None:
        config_dir = self.root / "empty-config"
        config_dir.mkdir()
        with (
            patch.object(account, "claude_config_dir", return_value=config_dir),
            self.assertRaisesRegex(
                account.UnreadableAccount,
                "this process's Claude account is unreadable",
            ),
        ):
            _ = account.own_claude_account()

    def test_named_account_start_points_memory_at_the_default_directory(self) -> None:
        home = self.root / "home with 'quotes'"
        config_dir = home / ".claude3"
        with patch.dict(os.environ, {"HOME": str(home)}):
            environment = account.named_account_environment(config_dir)
            prefix = account.named_account_prefix(config_dir)

        self.assertEqual(environment, {
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(home / ".claude"),
        })
        self.assertEqual(tuple(environment), account.NAMED_ACCOUNT_VARIABLES)
        self.assertEqual(
            prefix,
            f"CLAUDE_CONFIG_DIR={shlex.quote(str(config_dir))} "
            + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(home / '.claude'))} ",
        )

    def test_process_codex_home_distinguishes_explicit_home_missing_variable_and_unreadable_environment(self) -> None:
        with patch.object(sys, "platform", "linux"):
            with patch.object(Path, "read_bytes", return_value=b"USER=test\0CODEX_HOME=/tmp/codex-two\0"):
                resolved = account.process_codex_home(4321)
            self.assertIsInstance(resolved, account.ProcessCodexHome)
            assert isinstance(resolved, account.ProcessCodexHome)
            self.assertEqual(resolved.home, Path("/tmp/codex-two"))
            with patch.object(Path, "read_bytes", return_value=b"USER=test\0PATH=/usr/bin\0"):
                missing = account.process_codex_home(4321)
            self.assertIsInstance(missing, account.ProcessWithoutCodexHome)
            with patch.object(Path, "read_bytes", side_effect=PermissionError("no process access")):
                unreadable = account.process_codex_home(4321)
            self.assertIsInstance(unreadable, account.ProcessEnvironmentUnread)
            assert isinstance(unreadable, account.ProcessEnvironmentUnread)
            self.assertTrue(unreadable.reason)

    def test_mac_prompt_assignment_is_an_argument_and_never_selects_a_codex_home(self) -> None:
        arguments = ("/Applications/Codex Tools/codex", "exec", "-c", "key=value", "", "CODEX_HOME=/scratch/other")
        for environment in (b"HOME=/scratch\0", b"HOME=/scratch\0CODEX_HOME=\0"):
            with self.subTest(empty_setting=b"CODEX_HOME=" in environment):
                payload = (len(arguments).to_bytes(4, sys.byteorder, signed=True)
                           + os.fsencode(arguments[0]) + b"\0\0\0"
                           + b"\0".join(os.fsencode(word) for word in arguments) + b"\0" + environment)
                with (patch.object(sys, "platform", "darwin"),
                      patch.object(account, "kern_procargs2", return_value=payload),
                      patch.object(subprocess, "run", side_effect=AssertionError("ps text cannot identify environment"))):
                    record = account.mac_process_record(321)
                    selected = account.process_codex_home(321)
                self.assertIsInstance(record, account.MacProcessRecord)
                assert isinstance(record, account.MacProcessRecord)
                self.assertEqual(record.arguments, arguments)
                self.assertIsInstance(record.setting, account.ProcessWithoutCodexHome)
                self.assertIsInstance(selected, account.ProcessWithoutCodexHome)

    def test_mac_codex_home_comes_only_from_environment_after_the_exact_argument_count(self) -> None:
        payload = (int(3).to_bytes(4, sys.byteorder, signed=True)
                   + b"/opt/codex\0\0codex\0exec\0CODEX_HOME=/scratch/prompt\0"
                   + b"CODEX_HOME=/scratch/selected home\0FIXTURE_VALUE=private-marker\0")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(account, "kern_procargs2", return_value=payload)):
            record = account.mac_process_record(321)
            selected = account.process_codex_home(321)
        self.assertIsInstance(record, account.MacProcessRecord)
        assert isinstance(record, account.MacProcessRecord)
        self.assertEqual(record.arguments, ("codex", "exec", "CODEX_HOME=/scratch/prompt"))
        self.assertEqual(record.setting, account.ProcessCodexHome(Path("/scratch/selected home")))
        self.assertEqual(selected, record.setting)
        self.assertNotIn("private-marker", repr(record))

    def test_mac_kernel_executable_is_retained_independently_of_a_spoofed_argument_zero(self) -> None:
        executable = "/Applications/ChatGPT.app/Contents/MacOS/codex"
        payload = (int(3).to_bytes(4, sys.byteorder, signed=True)
                   + os.fsencode(executable) + b"\0\0codex\0app-server\0daemon\0HOME=/scratch\0")
        with patch.object(account, "kern_procargs2", return_value=payload):
            record = account.mac_process_record(321)
        self.assertIsInstance(record, account.MacProcessRecord)
        assert isinstance(record, account.MacProcessRecord)
        self.assertEqual(record.executable, Path(executable))
        self.assertEqual(record.arguments, ("codex", "app-server", "daemon"))

    def test_mac_refused_or_malformed_kernel_record_is_unreadable_instead_of_default_home(self) -> None:
        malformed = (
            b"", b"\x01\x00",
            int(0).to_bytes(4, sys.byteorder, signed=True) + b"/opt/codex\0\0",
            int(-1).to_bytes(4, sys.byteorder, signed=True) + b"/opt/codex\0\0",
            int(1).to_bytes(4, sys.byteorder, signed=True) + b"unterminated-executable",
            int(3).to_bytes(4, sys.byteorder, signed=True) + b"/opt/codex\0\0codex\0",
            int(1).to_bytes(4, sys.byteorder, signed=True) + b"/opt/codex\0\0codex",
            int(1).to_bytes(4, sys.byteorder, signed=True) + b"/opt/codex\0\0codex\0invalid-environment\0",
        )
        with patch.object(sys, "platform", "darwin"):
            for index, payload in enumerate(malformed):
                with self.subTest(case=index), patch.object(account, "kern_procargs2", return_value=payload):
                    record = account.mac_process_record(321)
                    selected = account.process_codex_home(321)
                self.assertIsInstance(record, account.ProcessEnvironmentUnread)
                self.assertIsInstance(selected, account.ProcessEnvironmentUnread)
                assert isinstance(selected, account.ProcessEnvironmentUnread)
                self.assertTrue(selected.reason)
            with patch.object(account, "kern_procargs2", side_effect=PermissionError("denied")):
                self.assertIsInstance(account.mac_process_record(321), account.ProcessEnvironmentUnread)
                self.assertIsInstance(account.process_codex_home(321), account.ProcessEnvironmentUnread)

    def test_a_kernel_record_keeps_the_arguments_and_only_the_kept_environment(self) -> None:
        payload = (int(5).to_bytes(4, sys.byteorder, signed=True) + b"/opt/claude/bin/claude\0\0\0\0"
                   + b"claude\0--name\0alpha unit\0--model\0\0"
                   + b"CLAUDE_CONFIG_DIR=/Users/n/.claude3\0CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-private\0"
                   + b"SHOWRUNNER_UNIT_ID=alpha-unit\0HOME=/Users/n\0\0executable_path=/opt/claude/bin/claude\0\0\0")
        parts = account.procargs2_parts(payload, lambda name: name == "CLAUDE_CONFIG_DIR" or name.startswith("SHOWRUNNER_"))
        self.assertEqual(parts.executable, Path("/opt/claude/bin/claude"))
        self.assertEqual(parts.arguments, ("claude", "--name", "alpha unit", "--model", ""))
        self.assertEqual(parts.environment, {"CLAUDE_CONFIG_DIR": "/Users/n/.claude3", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        self.assertNotIn("private", repr(parts))

    def test_a_short_kernel_record_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _ = account.procargs2_parts(int(9).to_bytes(4, sys.byteorder, signed=True) + b"/bin/claude\0\0claude\0",
                                        lambda _: True)

    def test_legacy_note_alone_still_resolves_the_spaced_account_label(self) -> None:
        self.write_note("claude 2.md", "legacy@example.invalid")
        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            self.assertEqual(account.label_for("claude", "legacy@example.invalid"), "claude2")
            self.assertEqual(account.named_claude_account("claude 2").login, "legacy@example.invalid")

    def test_attribution_pass_reads_home_once_and_observes_cutover_on_next_pass(self) -> None:
        home = self.home / ".claude"
        home.mkdir()
        seed = self.home / ".claude.json"
        self.write_claude_config(seed, "old@example.invalid")
        self.write_note("claude2.md", "old@example.invalid")
        with (patch.dict(os.environ, self.environment_without_claude_config_dir(), clear=True),
              patch.object(account, "claude_on_disk", wraps=agent_accounts.claude_on_disk) as read_home,
              patch.object(account, "process_config_dir", return_value=account.NoAccountSetting())):
            state = account.home_account_state()
            # A pass retains one consistent snapshot even if the home changes mid-pass.
            _ = seed.write_text("{}")
            for pid in (101, 102):
                self.assertEqual(account.process_account_directory(account.NoAccountSetting(), home_state=state),
                                 account.ConfiguredClaudeFolder(home))
                self.assertEqual(account.account_of(pid, home_state=state),
                                 account.Account("claude", "old@example.invalid", "claude2"))
            read_home.assert_called_once_with(home)
            next_state = account.home_account_state()
            self.assertIsInstance(account.process_account_directory(account.NoAccountSetting(), home_state=next_state),
                                  account.NoAccountSetting)
            self.assertIsNone(account.account_of(103, home_state=next_state))
            self.assertEqual(read_home.call_count, 2)

    def test_process_config_dir_reads_set_variable_from_linux_process_environment(self) -> None:
        environ = b"USER=test\0CLAUDE_CONFIG_DIR=/tmp/claude-two\0PATH=/usr/bin\0"

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(Path, "read_bytes", autospec=True, return_value=environ) as read_bytes,
        ):
            config_dir = account.process_config_dir(4321)

        self.assertEqual(config_dir, account.ConfiguredClaudeFolder(Path("/tmp/claude-two")))
        read_bytes.assert_called_once_with(Path("/proc/4321/environ"))

    def test_process_config_dir_keeps_an_absent_account_setting(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(Path, "home", return_value=self.home),
            patch.object(Path, "read_bytes", return_value=b"USER=test\0PATH=/usr/bin\0"),
        ):
            config_dir = account.process_config_dir(4321)

        self.assertIsInstance(config_dir, account.NoAccountSetting)

    def test_process_config_dir_names_an_unreadable_environment(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(Path, "read_bytes", side_effect=PermissionError),
        ):
            config_dir = account.process_config_dir(4321)

        self.assertIsInstance(config_dir, account.ProcessEnvironmentUnread)

    def test_darwin_process_owned_by_another_uid_is_unreadable(self) -> None:
        output = "0 /sbin/launchd HOME=/var/root CLAUDE_CONFIG_DIR=/root/.claude"

        config_dir = account.parse_darwin_config_dir(output, expected_uid=501)

        self.assertIsInstance(config_dir, account.ProcessEnvironmentUnread)

    def test_darwin_process_without_visible_home_environment_is_unreadable(self) -> None:
        output = "501 /usr/bin/claude --resume session-id"

        config_dir = account.parse_darwin_config_dir(output, expected_uid=501)

        self.assertIsInstance(config_dir, account.ProcessEnvironmentUnread)

    def test_darwin_process_with_visible_environment_defaults_when_config_dir_is_unset(
        self,
    ) -> None:
        with patch.object(Path, "home", return_value=self.home):
            config_dir = account.parse_darwin_config_dir(
                "501 /usr/bin/claude HOME=/Users/test USER=test",
                expected_uid=501,
            )

        self.assertIsInstance(config_dir, account.NoAccountSetting)

    def test_darwin_process_with_apostrophe_and_visible_environment_uses_config_dir(
        self,
    ) -> None:
        output = (
            "501 claude tell the user's words CLAUDE_CONFIG_DIR=/from-argv "
            "HOME=/Users/test CLAUDE_CONFIG_DIR=/from-environment"
        )

        config_dir = account.parse_darwin_config_dir(output, expected_uid=501)

        self.assertEqual(config_dir, account.ConfiguredClaudeFolder(Path("/from-environment")))

    def test_darwin_process_config_dir_names_a_failed_observation(self) -> None:
        result = subprocess.CompletedProcess(["ps"], 1, "", "failed")
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(subprocess, "run", return_value=result) as run,
        ):
            config_dir = account.process_config_dir(4321)

        self.assertIsInstance(config_dir, account.ProcessEnvironmentUnread)
        run.assert_called_once()

    def test_write_label_does_not_rewrite_unchanged_content(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        identity = account.Account("claude", "second@example.com", "claude2")

        account.write_label(config_dir, identity)
        label_path = config_dir / "account-label"
        old_timestamp = 946_684_800_000_000_000
        os.utime(label_path, ns=(old_timestamp, old_timestamp))
        account.write_label(config_dir, identity)

        self.assertEqual(label_path.read_text(), "claude2\n")
        self.assertEqual(label_path.stat().st_mtime_ns, old_timestamp)
        self.assertEqual(set(config_dir.iterdir()), {label_path})

    def test_write_label_replaces_a_legacy_cache_with_the_canonical_label(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        path = config_dir / "account-label"
        _ = path.write_text("claude 2\n")
        account.write_label(config_dir, account.Account("claude", "second@example.com", "claude2"))
        self.assertEqual(path.read_text(), "claude2\n")

    def test_write_label_option_removes_stale_label_when_account_is_unreadable(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        label_path = config_dir / "account-label"
        _ = label_path.write_text("stale account\n")
        environment = self.environment_without_claude_config_dir()
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)

        with (
            patch.dict(os.environ, environment, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            status = account.main(["--write-label"])

        self.assertEqual(status, 1)
        self.assertFalse(label_path.exists())

    def test_write_label_option_writes_label_for_readable_account(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        self.write_claude_config(config_dir / ".claude.json", "person@example.com")
        self.write_note("claude2.md", "person@example.com")
        environment = self.environment_without_claude_config_dir()
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)

        with (
            patch.dict(os.environ, environment, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            status = account.main(["--write-label"])

        self.assertEqual(status, 0)
        self.assertEqual((config_dir / "account-label").read_text(), "claude2\n")

    def test_codex_account_reads_id_token_email_from_given_home(self) -> None:
        codex_home = self.root / "codex-two"
        codex_home.mkdir()
        login = "codex@example.com"
        self.write_codex_auth(codex_home, login)

        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            resolved = account.codex_account(codex_home)

        self.assertEqual(resolved, account.Account("codex", login, login))

    def test_claude_account_returns_none_for_non_object_account_json(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        config_path = config_dir / ".claude.json"

        malformed_documents: tuple[object, ...] = (
            [],
            {"oauthAccount": "not an object"},
        )
        for malformed in malformed_documents:
            with self.subTest(malformed=malformed):
                _ = config_path.write_text(json.dumps(malformed))
                resolved = account.claude_account(config_dir)

                self.assertIsNone(resolved)

    def test_claude_account_survives_a_malformed_usage_cache(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        config_path = config_dir / ".claude.json"

        malformed_caches: tuple[object, ...] = ("not an object", {"utilization": []})
        for cache in malformed_caches:
            with self.subTest(cache=cache):
                document = {
                    "oauthAccount": {"emailAddress": "two@example.com"},
                    "cachedUsageUtilization": cache,
                }
                _ = config_path.write_text(json.dumps(document))
                resolved = account.claude_account(config_dir)

                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved.login, "two@example.com")

    def test_codex_account_returns_none_for_non_object_account_json(self) -> None:
        codex_home = self.root / "codex-two"
        codex_home.mkdir()
        auth_path = codex_home / "auth.json"

        malformed_documents: tuple[object, ...] = (
            [],
            {"tokens": "not an object"},
        )
        for malformed in malformed_documents:
            with self.subTest(malformed=malformed):
                _ = auth_path.write_text(json.dumps(malformed))
                resolved = account.codex_account(codex_home)

                self.assertIsNone(resolved)

    def configured_cli_environment(self) -> dict[str, str]:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        codex_home = self.root / "codex-two"
        codex_home.mkdir()
        self.write_claude_config(config_dir / ".claude.json", "claude@example.com")
        self.write_codex_auth(codex_home, "codex@example.com")
        self.write_note("claude2.md", "claude@example.com")
        self.write_note("codex3.md", "codex@example.com")
        environment = self.environment_without_claude_config_dir()
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
        environment["CODEX_HOME"] = str(codex_home)
        return environment

    def test_plain_cli_prints_both_accounts_from_configured_directories(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.configured_cli_environment(), clear=True),
            redirect_stdout(output),
        ):
            status = account.main([])

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            "claude2 (claude@example.com) · codex3 (codex@example.com)\n",
        )

    def test_json_cli_prints_both_accounts_from_configured_directories(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.configured_cli_environment(), clear=True),
            redirect_stdout(output),
        ):
            status = account.main(["--json"])

        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "claude": {"login": "claude@example.com", "label": "claude2"},
                "codex": {"login": "codex@example.com", "label": "codex3"},
            },
        )

    def test_cli_returns_one_when_claude_account_is_unreadable(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        environment = self.environment_without_claude_config_dir()
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)

        with (
            patch.dict(os.environ, environment, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            status = account.main([])

        self.assertEqual(status, 1)

    def status_line_input(self) -> dict[str, object]:
        return {
            "workspace": {"current_dir": "/tmp/claude_commands"},
            "context_window": {"total_input_tokens": 182_340},
            "model": {"display_name": "Opus 5.5"},
            "effort": {"level": "high"},
        }

    def render_status_line(
        self,
        document: object,
        label: str | None = None,
    ) -> str:
        arguments = [
            "jq",
            "-r",
            "--arg",
            "pwd",
            "/tmp/fixed/fallback",
        ]
        if label is not None:
            arguments.extend(("--arg", "account", label))
        arguments.extend(("-f", str(STATUS_LINE)))
        result = subprocess.run(
            arguments,
            capture_output=True,
            check=True,
            env={"PATH": os.environ["PATH"]},
            input=json.dumps(document),
            text=True,
        )
        return result.stdout

    def test_status_line_prints_account_first_and_both_remaining_windows_second(self) -> None:
        document = self.status_line_input() | {
            "rate_limits": {
                "five_hour": {"used_percentage": 96.4, "resets_at": 1_800_000_000},
                "seven_day": {"used_percentage": 58.2, "resets_at": 1_800_000_000},
            }
        }

        self.assertEqual(
            self.render_status_line(document, "claude2"),
            ACCOUNT_FIRST_LINE
            + f"\nclaude2 | {MODEL_TOKENS} | 5-hour 3% left until 03:00 EST | weekly 41% left until Fri 03:00 EST\n",
        )

    def test_status_line_without_reset_times_names_no_reset(self) -> None:
        document = self.status_line_input() | {
            "rate_limits": {
                "five_hour": {"used_percentage": 96.4},
                "seven_day": {"used_percentage": 58.2},
            }
        }

        self.assertEqual(
            self.render_status_line(document, "claude2"),
            ACCOUNT_FIRST_LINE + f"\nclaude2 | {MODEL_TOKENS} | 5-hour 3% left | weekly 41% left\n",
        )

    def test_status_line_floors_and_clamps_remaining_percentage(self) -> None:
        cases = (
            (0, "100"),
            (100, "0"),
            (104, "0"),
        )
        for used_percentage, remaining_percentage in cases:
            with self.subTest(used_percentage=used_percentage):
                document = self.status_line_input() | {
                    "rate_limits": {
                        "seven_day": {"used_percentage": used_percentage}
                    }
                }
                self.assertEqual(
                    self.render_status_line(document),
                    EXAMPLE_FIRST_LINE
                    + f"\n{MODEL_TOKENS} | weekly {remaining_percentage}% left\n",
                )

    def test_status_line_puts_the_account_before_the_model_when_limits_are_absent(self) -> None:
        self.assertEqual(
            self.render_status_line(self.status_line_input(), "claude2"),
            ACCOUNT_FIRST_LINE + f"\nclaude2 | {MODEL_TOKENS}\n",
        )

    def test_status_line_prints_windows_without_an_account_argument(self) -> None:
        document = self.status_line_input() | {
            "rate_limits": {
                "five_hour": {"used_percentage": 95},
                "seven_day": {"used_percentage": 60},
            }
        }

        self.assertEqual(
            self.render_status_line(document),
            EXAMPLE_FIRST_LINE + f"\n{MODEL_TOKENS} | 5-hour 5% left | weekly 40% left\n",
        )

    def test_status_line_omits_a_null_or_missing_window(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            (
                {
                    "five_hour": None,
                    "seven_day": {"used_percentage": 60},
                },
                "weekly 40% left",
            ),
            (
                {
                    "five_hour": {"used_percentage": 95},
                    "seven_day": None,
                },
                "5-hour 5% left",
            ),
            ({"seven_day": {"used_percentage": 60}}, "weekly 40% left"),
            ({"five_hour": {"used_percentage": 95}}, "5-hour 5% left"),
        )
        for rate_limits, expected_second_line in cases:
            with self.subTest(rate_limits=rate_limits):
                document = self.status_line_input() | {"rate_limits": rate_limits}
                self.assertEqual(
                    self.render_status_line(document),
                    EXAMPLE_FIRST_LINE + f"\n{MODEL_TOKENS} | {expected_second_line}\n",
                )

    def test_status_line_omits_a_window_without_used_percentage(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            (
                {
                    "five_hour": {"resets_at": 1_800_000_000},
                    "seven_day": {"used_percentage": 60},
                },
                "weekly 40% left",
            ),
            (
                {
                    "five_hour": {"used_percentage": 95},
                    "seven_day": {"resets_at": 1_800_000_000},
                },
                "5-hour 5% left",
            ),
        )
        for rate_limits, expected_second_line in cases:
            with self.subTest(rate_limits=rate_limits):
                document = self.status_line_input() | {"rate_limits": rate_limits}
                self.assertEqual(
                    self.render_status_line(document),
                    EXAMPLE_FIRST_LINE + f"\n{MODEL_TOKENS} | {expected_second_line}\n",
                )

    def test_status_line_has_the_model_and_tokens_alone_on_line_two_without_other_parts(self) -> None:
        documents = (
            self.status_line_input(),
            self.status_line_input() | {"rate_limits": None},
        )
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(
                    self.render_status_line(document),
                    EXAMPLE_FIRST_LINE + f"\n{MODEL_TOKENS}\n",
                )

    def test_status_line_omits_an_empty_account_label(self) -> None:
        self.assertEqual(
            self.render_status_line(self.status_line_input(), ""),
            EXAMPLE_FIRST_LINE + f"\n{MODEL_TOKENS}\n",
        )


if __name__ == "__main__":
    _ = unittest.main()
