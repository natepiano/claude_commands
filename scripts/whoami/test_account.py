"""Behavioral contract for account resolution and status-line labels."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

import account


STATUS_LINE = Path(__file__).parents[1] / "statusline" / "statusline.jq"


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
        claims = (
            base64.urlsafe_b64encode(json.dumps({"email": login}).encode())
            .decode()
            .rstrip("=")
        )
        _ = (home / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}})
        )

    def write_note(self, name: str, login: str) -> None:
        _ = (self.notes / name).write_text(
            f"---\nlogin: {login}\nstate: inactive\n---\nBody stays.\n"
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
        self.write_note("codex 2.md", "person@example.com")
        self.write_note("claude 2.md", "Person@Example.COM")

        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            label = account.label_for("claude", "person@example.com")

        self.assertEqual(label, "claude 2")

    def test_label_for_falls_back_to_login_without_a_matching_note(self) -> None:
        self.write_note("claude 2.md", "someone-else@example.com")

        with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(self.notes)}):
            label = account.label_for("claude", "unlisted@example.com")

        self.assertEqual(label, "unlisted@example.com")

    def test_process_config_dir_reads_set_variable_from_linux_process_environment(self) -> None:
        environ = b"USER=test\0CLAUDE_CONFIG_DIR=/tmp/claude-two\0PATH=/usr/bin\0"

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(Path, "read_bytes", autospec=True, return_value=environ) as read_bytes,
        ):
            config_dir = account.process_config_dir(4321)

        self.assertEqual(config_dir, Path("/tmp/claude-two"))
        read_bytes.assert_called_once_with(Path("/proc/4321/environ"))

    def test_process_config_dir_defaults_to_dot_claude_when_variable_is_unset(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(Path, "home", return_value=self.home),
            patch.object(Path, "read_bytes", return_value=b"USER=test\0PATH=/usr/bin\0"),
        ):
            config_dir = account.process_config_dir(4321)

        self.assertEqual(config_dir, self.home / ".claude")

    def test_process_config_dir_returns_none_when_environment_is_unreadable(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(Path, "read_bytes", side_effect=PermissionError),
        ):
            config_dir = account.process_config_dir(4321)

        self.assertIsNone(config_dir)

    def test_darwin_process_owned_by_another_uid_is_unreadable(self) -> None:
        output = "0 /sbin/launchd HOME=/var/root CLAUDE_CONFIG_DIR=/root/.claude"

        config_dir = account.parse_darwin_config_dir(output, expected_uid=501)

        self.assertIsNone(config_dir)

    def test_darwin_process_without_visible_home_environment_is_unreadable(self) -> None:
        output = "501 /usr/bin/claude --resume session-id"

        config_dir = account.parse_darwin_config_dir(output, expected_uid=501)

        self.assertIsNone(config_dir)

    def test_darwin_process_with_visible_environment_defaults_when_config_dir_is_unset(
        self,
    ) -> None:
        with patch.object(Path, "home", return_value=self.home):
            config_dir = account.parse_darwin_config_dir(
                "501 /usr/bin/claude HOME=/Users/test USER=test",
                expected_uid=501,
            )

        self.assertEqual(config_dir, self.home / ".claude")

    def test_darwin_process_with_apostrophe_and_visible_environment_uses_config_dir(
        self,
    ) -> None:
        output = (
            "501 claude tell the user's words CLAUDE_CONFIG_DIR=/from-argv "
            "HOME=/Users/test CLAUDE_CONFIG_DIR=/from-environment"
        )

        config_dir = account.parse_darwin_config_dir(output, expected_uid=501)

        self.assertEqual(config_dir, Path("/from-environment"))

    def test_darwin_process_config_dir_returns_none_when_ps_fails(self) -> None:
        result = subprocess.CompletedProcess(["ps"], 1, "", "failed")
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(subprocess, "run", return_value=result) as run,
        ):
            config_dir = account.process_config_dir(4321)

        self.assertIsNone(config_dir)
        run.assert_called_once()

    def test_write_label_does_not_rewrite_unchanged_content(self) -> None:
        config_dir = self.root / "claude-two"
        config_dir.mkdir()
        identity = account.Account("claude", "second@example.com", "claude 2")

        account.write_label(config_dir, identity)
        label_path = config_dir / "account-label"
        old_timestamp = 946_684_800_000_000_000
        os.utime(label_path, ns=(old_timestamp, old_timestamp))
        account.write_label(config_dir, identity)

        self.assertEqual(label_path.read_text(), "claude 2\n")
        self.assertEqual(label_path.stat().st_mtime_ns, old_timestamp)
        self.assertEqual(set(config_dir.iterdir()), {label_path})

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
        self.write_note("claude 2.md", "person@example.com")
        environment = self.environment_without_claude_config_dir()
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)

        with (
            patch.dict(os.environ, environment, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            status = account.main(["--write-label"])

        self.assertEqual(status, 0)
        self.assertEqual((config_dir / "account-label").read_text(), "claude 2\n")

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
        self.write_note("claude 2.md", "claude@example.com")
        self.write_note("codex 3.md", "codex@example.com")
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
            "claude 2 (claude@example.com) · codex 3 (codex@example.com)\n",
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
                "claude": {"login": "claude@example.com", "label": "claude 2"},
                "codex": {"login": "codex@example.com", "label": "codex 3"},
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

    def render_status_line(self, label: str | None) -> str:
        arguments = [
            "jq",
            "-r",
            "-f",
            str(STATUS_LINE),
            "--arg",
            "pwd",
            "/tmp/fixed/worktree",
        ]
        if label is not None:
            arguments.extend(("--arg", "account", label))
        result = subprocess.run(
            arguments,
            capture_output=True,
            check=True,
            input=json.dumps({}),
            text=True,
        )
        return result.stdout.removesuffix("\n")

    def test_status_line_appends_a_nonempty_account_label(self) -> None:
        self.assertEqual(
            self.render_status_line("claude 2"),
            "worktree | 0 |  | claude 2",
        )

    def test_status_line_omits_an_empty_account_label(self) -> None:
        self.assertEqual(self.render_status_line(""), "worktree | 0 | ")

    def test_status_line_omits_account_field_when_argument_is_absent(self) -> None:
        self.assertEqual(self.render_status_line(None), "worktree | 0 | ")


if __name__ == "__main__":
    _ = unittest.main()
