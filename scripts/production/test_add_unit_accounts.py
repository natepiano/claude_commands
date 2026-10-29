"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import final
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit
import autocompact
import launch_permission
import inventory
from scripts.accounts import accounts


from add_unit_test_support import AddUnitTestCase, configure_test_zone


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitAccountsTests(AddUnitTestCase):
    def test_untrusted_new_worktree_refuses_before_stub_row_or_worktree(self) -> None:
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"},
                                         "projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in ([], ["--check"]):
            with self.subTest(mode=mode):
                result = self.cli("alpha", "--brief", "Add a feature", *mode)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                folder = self.root / "project-alpha"
                self.assertIn(f"alpha-unit: Claude is not trusted in {folder} on claude2. Run: "
                              + f"cd {shlex.quote(str(self.checkout))} && "
                              + f"CLAUDE_CONFIG_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude2'))} "
                              + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude'))} claude", result.stderr)
                self.assert_no_launch_change(document, head)
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
                self.assertNotIn("build-followups-alpha", self.git("branch", "--list"))

    def test_trust_uses_launch_account_instead_of_callers_account(self) -> None:
        directory = self.prepare_unit_account("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude-3@example.invalid"}, "projects": {}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(Path(self.env["HOME"]) / ".claude")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--brief", "Add a feature")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("on claude3. Run:", result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))}", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_trust_file_becoming_unreadable_after_account_check_refuses_before_any_write(self) -> None:
        with patch.dict(os.environ, self.env):
            selected = accounts.role_account("unit")
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text('{"oauthAccount": "SECRET-EMAIL", ')
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        output = io.StringIO()
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stdout(output), redirect_stderr(errors),
              patch.object(add_unit, "account_for_launch", return_value=selected),
              patch.object(accounts, "check", return_value=[]),
              patch.object(launch_permission, "launch_permission",
                           return_value=launch_permission.AllowedByShutdownState())):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--brief", "Add a feature"])
        self.assertEqual(code, 2, (output.getvalue(), errors.getvalue()))
        self.assertEqual(errors.getvalue(), f"add_unit: alpha-unit: cannot read {config} "
                         + "to check Claude trust (JSONDecodeError)\n")
        self.assertNotIn("SECRET", output.getvalue() + errors.getvalue())
        self.assert_no_launch_change(document, head)
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_explicit_account_environment_omits_every_prepared_removal(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        flags = ("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                 "--cwd", str(prior), "--account", "claude2")
        self.prepare_herdr_launch(*flags)
        prepared = accounts.PreparedAccount({"ACCOUNT_TEST_VARIABLE": "selected", "CLAUDE_CODE_OAUTH_TOKEN": "remove-me"},
                                             frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
        with patch.dict(os.environ, self.env), patch.object(accounts, "prepared_account", return_value=prepared):
            code = add_unit.main(["--production", str(self.doc), *flags])
        self.assertEqual(code, 0)
        self.assertEqual(self.pane_environment()["ACCOUNT_TEST_VARIABLE"], "selected")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.pane_environment())
        self.assertNotIn("claude", self.agent_arguments())
        self.assertFalse(any(argument.endswith("accounts.py") for argument in self.agent_arguments()))

    def test_prepared_account_refusal_happens_before_any_herdr_call(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.prepare_unit_account("claude3")
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stderr(errors),
              patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused("account logged out")) as prepared):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
                                  "--resume", "session-123", "--cwd", str(prior), "--account", "claude3"])
        self.assertEqual(code, 2)
        self.assertIn("account logged out", errors.getvalue())
        prepared.assert_called_once_with("claude3", resume=True)
        self.assertEqual(self.herdr_calls(), [])

    def test_attach_refuses_unclear_source_account_before_stopping(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(inventory, "listing", return_value=[]):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("source account is unclear", result.stderr)
        self.assertEqual(trace, [])

    def test_herdr_resume_uses_group_account_and_last_real_transcript_model(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.persist_herdr_unit("alpha", prior)
        transcript = Path(self.env["HOME"]) / ".claude/projects/project/session-123.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text('\n'.join([json.dumps({"type": "assistant", "message": {"model": "opus"}}),
            json.dumps({"type": "user", "message": {"model": "ignored"}}),
            json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}), "{partial"]))
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior))
        self.assertIn("herdr session attach agents, workspace director, tab alpha", result.stdout)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(self.agent_arguments()[:4], ["--model", "opus", "--effort", "high"])
        self.assertIn(["--session", "alpha", "server", "stop"], self.herdr_calls())

    def test_herdr_resume_without_transcript_uses_director_model(self) -> None:
        self.write_agent_config(director="sonnet:high")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.persist_herdr_unit("alpha", prior)
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(self.agent_arguments()[:2], ["--model", "sonnet"])
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))

    def test_new_work_is_refused_while_account_is_down(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            "add_unit: claude2 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT), requested in a terminal; /shutdown restart first\n",
        )
        self.assert_no_launch_change(document, head)

    def test_named_unit_account_paths_are_literal_pane_environment_values(self) -> None:
        home = self.root / "home with 'quotes' $()"
        _ = Path(self.env["HOME"]).rename(home)
        self.env["HOME"] = str(home)
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        directory = self.prepare_unit_account("claude3")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(self.pane_environment()["CLAUDE_CODE_REMOTE_MEMORY_DIR"], str(home / ".claude"))
        self.assertNotIn("CLAUDE_TEST_SECRET", self.pane_environment())
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.pane_environment())

    def test_held_named_unit_account_refuses_new_work_before_changes(self) -> None:
        """F001: the launch guard checks the selected unit account's shutdown."""
        _ = self.prepare_unit_account("claude3")
        login = "claude-3@example.invalid"
        held = self.shutdown_record("down")
        held["login"] = login
        held["label"] = "claude3"
        self.write_shutdown_record(held, login=login)
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            "add_unit: claude3 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT), requested in a terminal; /shutdown restart first\n",
        )
        self.assert_no_launch_change(document, head)

    def test_default_account_shutdown_does_not_block_a_named_unit(self) -> None:
        """F001: another account's shutdown does not block the selected unit account."""
        directory = self.prepare_unit_account("claude3")
        self.write_shutdown_record(self.shutdown_record("down"))

        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_claude2_guard_environment_selects_its_numbered_folder(self) -> None:
        """The guard sees the selected folder, never the caller or config home."""
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        with (
            patch.dict(os.environ, self.env),
            patch.object(
                launch_permission, "launch_permission",
                return_value=launch_permission.AllowedByShutdownState(),
            ) as permission,
        ):
            expected = {
                key: value for key, value in os.environ.items()
                if not key.startswith("CLAUDE_")
            }
            result = add_unit.main([
                "--production", str(self.doc), "alpha",
                "--plan", "docs/plans/given.md", "--check",
            ])

        self.assertEqual(result, 0)
        expected["CLAUDE_CONFIG_DIR"] = str(Path(self.env["HOME"]) / ".claude2")
        permission.assert_called_once_with(
            expected, launch_permission.NewWorkLaunchPurpose()
        )

    def test_better_candidate_after_readiness_keeps_the_stored_group_account(self) -> None:
        """Better readings neither replace the stored group nor the checked launch account."""
        directory = self.prepare_unit_account("claude3")
        ensure_worktree = add_unit.ensure_worktree

        def change_role_after_worktree(request: add_unit.UnitLaunch) -> None:
            ensure_worktree(request)
            _ = self.prepare_unit_account("claude1")
            self.quota_reading("claude-1@example.invalid", 1, 1)
            _ = self.select_unit_account("claude3")

        with (
            patch.dict(os.environ, self.env),
            patch.object(accounts, "role_account", wraps=accounts.role_account) as selection,
            patch.object(accounts, "check", wraps=accounts.check) as readiness,
            patch.object(add_unit, "ensure_worktree", side_effect=change_role_after_worktree),
        ):
            result = add_unit.main([
                "--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
            ])

        self.assertEqual(result, 0)
        selection.assert_called_once_with("unit")
        self.assertIn("claude3", [call.args[0] for call in readiness.call_args_list])
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertTrue((directory.parent / ".claude1").exists())
        with patch.dict(os.environ, self.env):
            self.assertEqual(
                accounts.role_account("unit"),
                accounts.NamedAccount("claude3", directory),
            )

    def test_new_claude2_unit_passes_its_numbered_account_variables(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.pane_environment()["CLAUDE_CODE_REMOTE_MEMORY_DIR"], str(Path(self.env["HOME"]) / ".claude"))

    def test_new_named_unit_passes_selected_account_variables(self) -> None:
        directory = self.prepare_unit_account("claude3")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_explicit_unit_account_move_passes_prepared_account_variables(self) -> None:
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--account", "claude3")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_manual_unit_resume_keeps_its_recorded_model_despite_changed_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        self.persist_unit_account(directory, session="alpha", model="opus")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")

    def test_resume_autocompact_is_recorded_and_passed_to_claude(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(prior), "--autocompact", "200k")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume"):arguments.index("--resume") + 4],
                         ["--resume", "session-123", "--autocompact", "200k"])
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-123"), "200k")

    def test_registered_unit_autocompact_records_without_new_work_checks(self) -> None:
        _ = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()
        result = self.set_unit_autocompact("200k")
        self.assertEqual(result.stdout.strip(),
                         "trunk-unit: auto-compact 200k applies to session session-trunk at its next restart")
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-trunk"), "200k")
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_auto_deletes_the_record(self) -> None:
        _ = self.commit_row_off_the_naming_pattern()
        with patch.dict(os.environ, self.env):
            autocompact.record("session-trunk", "200k")
        result = self.set_unit_autocompact("auto")
        self.assertIn("the saved setting applies", result.stdout)
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-trunk"), "")
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_check_writes_nothing(self) -> None:
        _ = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()
        result = self.set_unit_autocompact("300k", "--check")
        self.assertEqual(result.stdout.strip(),
                         "trunk-unit: auto-compact 300k would apply to session session-trunk at its next restart")
        self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_autocompact_refusals_launch_nothing_and_record_nothing(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        before = self.repository_state()
        for flags, reason in ((("--autocompact", "200k"), "--autocompact requires --resume"),
                              (("--resume", "session-123", "--cwd", str(prior), "--autocompact", "50k"),
                               "outside 100k to 1M"),
                              (("--resume", "session-123", "--cwd", str(prior), "--autocompact", "big"),
                               "token count such as 200k")):
            with self.subTest(flags=flags):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(reason, result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.servers_started(), [])
        self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())

    def test_unit_resume_uses_configured_account_instead_of_recorded_account(self) -> None:
        """F004: the configured units account decides resumes too."""
        old = self.prepare_unit_account("claude2")
        caller = self.prepare_unit_account("claude3")
        directory = self.prepare_unit_account("claude1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        self.persist_unit_account(old, session="alpha")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.prepare_herdr_launch("alpha", "--plan", "docs/plans/given.md",
                                  "--resume", "session-123", "--cwd", str(prior))
        with (patch.dict(os.environ, self.env),
              patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepared):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
                "--resume", "session-123", "--cwd", str(prior)])
        self.assertEqual(code, 0)
        prepared.assert_any_call("claude1", resume=True)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertIn("--resume session-123", self.launch_command())

    def test_unit_resume_uses_configured_account_instead_of_old_named_account(self) -> None:
        self.commit_prepared_row(self.unit_row())
        directory = self.prepare_unit_account("claude1")
        other_directory = self.prepare_unit_account("claude3")
        self.env["CLAUDE_CONFIG_DIR"] = str(other_directory)
        self.persist_unit_account(directory)
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        _ = self.successful(
            "alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name", "--host-session", "recorded-herdr",
        )

        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(other_directory))
        self.assertFalse(any(argument.endswith("/accounts.py") for argument in self.agent_arguments()))
        self.assertIn("--resume session-123", self.launch_command())
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_without_recorded_account_uses_configured_units(self) -> None:
        """F004: no retained account is needed once the group is configured."""
        directory = self.prepare_unit_account("claude1")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "unrecorded-session", "--cwd", str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_unit_resume_refuses_config_directory_outside_configured_account(self) -> None:
        """F004: an explicit directory cannot replace the units account."""
        directory = self.root / "external account with spaces"
        directory.mkdir()
        _ = self.prepare_unit_account("claude1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(self.checkout), "--config-dir", str(directory))
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude1", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_unit_resume_refuses_missing_directory_that_differs_from_configured_default(self) -> None:
        """F004: an override mismatch names configuration, not a missing former account."""
        directory = self.root / "gone-account"
        prior = self.root / "prior-session"
        prior.mkdir()
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--config-dir", str(directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("units account is claude2", result.stderr)
        self.assertIn("--account and --config-dir must name claude2", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.servers_started(), [])

    def test_resumed_unit_uses_its_recorded_model_despite_a_different_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--account", "claude3", "--model", "opus")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_refuses_account_that_differs_from_configured_units(self) -> None:
        """F004: --account must agree with the configured group."""
        _ = self.prepare_unit_account("claude2")
        _ = self.prepare_unit_account("claude1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(self.checkout), "--account", "claude2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude1", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_unconfigured_units_refuse_new_standby_and_resume_without_writes(self) -> None:
        """F004: every unit launch mode requires explicit configuration."""
        with patch.dict(os.environ, self.env):
            accounts.groups_file().unlink()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in (("--plan", "docs/plans/given.md"), ("--standby",),
                     ("--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(self.checkout))):
            with self.subTest(mode=mode):
                result = self.cli("alpha", *mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("accounts.py move units <claudeN>", result.stderr)
                self.assert_no_launch_change(document, head)
                self.assertFalse((Path(self.env["HOME"]) / ".local/state/accounts/groups.json").exists())

    def test_new_unit_refuses_unfit_configured_account_without_default_fallback(self) -> None:
        """F004: a configured account cannot silently fall back to default."""
        directory = self.select_unit_account("claude3")
        directory.mkdir()
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude3", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_new_unit_and_standby_use_configured_units_despite_showrunner_caller(self) -> None:
        """F004: caller identity does not decide either new unit launch mode."""
        caller = self.prepare_unit_account("claude3")
        directory = self.prepare_unit_account("claude1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        _ = path.write_text(json.dumps({"units": "claude1", "showrunners": "claude3"}))
        for name, mode in (("alpha", ("--plan", "docs/plans/given.md")), ("beta", ("--standby",))):
            with self.subTest(mode=mode):
                _ = self.successful(name, *mode)
                self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_unit_accepts_explicit_account_matching_configured_units(self) -> None:
        """F004: matching --account selects no alternative account."""
        directory = self.prepare_unit_account("claude1")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--account", "claude1")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_units_and_showrunners_may_share_the_launch_account(self) -> None:
        """F004: sharing one account is valid configuration."""
        directory = self.prepare_unit_account("claude1")
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        _ = path.write_text(json.dumps({"units": "claude1", "showrunners": "claude1"}))
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_ready_unit_account_passes_check_without_launch_changes(self) -> None:
        _ = self.prepare_unit_account("claude3")
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--check")

        self.assert_no_launch_change(document, head)

    def test_sonnet_director_row_changes_launch_model_and_effort(self) -> None:
        self.write_agent_config(director="sonnet:xhigh")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assert_director_flags("sonnet", "xhigh")

    def test_director_row_without_effort_uses_claude_default(self) -> None:
        self.write_agent_config(director="opus")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assert_director_flags("opus", None)

    def test_codex_director_refused_before_any_change_with_or_without_check(self) -> None:
        self.write_agent_config(family="codex")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for check in (False, True):
            with self.subTest(check=check):
                arguments = ("alpha", "--plan", "docs/plans/given.md")
                result = self.cli(*arguments, *(("--check",) if check else ()))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip().splitlines(), [
                    "add_unit: unit directors launch only on claude; production.director resolves to codex (gpt-test)"])
                self.assert_no_launch_change(document, head)

    def test_missing_claude_director_set_returns_resolver_error_before_change(self) -> None:
        self.write_agent_config(claude_set=False)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip().splitlines(), [
            f"add_unit: ERROR: [production.director] missing set section [production.claude] in {self.agent_config}."])
        self.assert_no_launch_change(document, head)

    def test_names_without_table_rows_keep_suffix_across_productions(self) -> None:
        sample_doc = self.doc.with_name("sample-production.md")
        _ = sample_doc.write_text(self.production_doc(), encoding="utf-8")
        for document, names in ((self.doc, ("designers", "reviewers", "showrunners", "units")),
                                (sample_doc, ("reports", "designers-helper"))):
            for name in names:
                with self.subTest(production=document.name, name=name):
                    request = add_unit.launch_request(argparse.Namespace(
                        production=str(document), name=name, plan="docs/plans/given.md", brief=None,
                        standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
                        resume=None, cwd=None, restart_note=None, session_name=None, host_session=None,
                        probe=False,
                    ))
                    self.assertEqual(request.unit, name + "-unit")


if __name__ == "__main__":
    _ = unittest.main()
