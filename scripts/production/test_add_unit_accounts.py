"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import argparse
import io
import json
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import final
from unittest.mock import patch

from scripts.production import add_unit
from scripts.production import autocompact
from scripts.agents import inventory
from scripts.accounts import accounts


from scripts.production.add_unit_test_support import AddUnitTestCase, configure_test_zone
from scripts.production import staff
from scripts.production.staff_test_support import unit_membership


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitAccountsTests(AddUnitTestCase):


    def test_explicit_account_environment_omits_every_prepared_removal(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        flags = ("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                 "--cwd", str(prior), "--account", "claude2")
        self.prepare_herdr_launch(*flags)
        prepared = accounts.PreparedAccount({"ACCOUNT_TEST_VARIABLE": "selected", "CLAUDE_CODE_OAUTH_TOKEN": "remove-me"},
                                             frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
        with patch.dict(os.environ, self.env), patch.object(accounts, "prepared_account", return_value=prepared):
            code = add_unit.main(["--production", self.production_name, *flags])
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
            code = add_unit.main(["--production", self.production_name, "alpha", "--plan", "docs/plans/given.md",
                                  "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--account", "claude3"])
        self.assertEqual(code, 2)
        self.assertIn("account logged out", errors.getvalue())
        prepared.assert_called_once_with("claude3", resume=True)
        self.assertEqual(self.herdr_calls(), [])

    def test_attach_refuses_unclear_source_account_before_stopping(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(inventory, "listing", return_value=[]):
            result = self.cli("alpha", "--permanent", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("source account is unclear", result.stderr)
        self.assertEqual(trace, [])

    def test_herdr_resume_uses_group_account_and_last_real_transcript_model(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        # The pane mark joins its unit only through the unit's staff row.
        row = unit_membership("build-followups", "alpha")
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", self.root / "project-alpha"), expected=row.worktree)
        self.persist_herdr_unit("alpha", prior)
        transcript = Path(self.env["HOME"]) / ".claude/projects/project/62ccbd64-f22e-5647-b20e-7899aa39d766.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text('\n'.join([json.dumps({"type": "assistant", "message": {"model": "opus"}}),
            json.dumps({"type": "user", "message": {"model": "ignored"}}),
            json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}), "{partial"]))
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertIn("herdr session attach agents, workspace build-followups, tab alpha", result.stdout)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(self.agent_arguments()[:4], ["--model", "opus", "--effort", "high"])
        self.assertIn(["--session", "alpha", "server", "stop"], self.herdr_calls())

    def test_herdr_resume_without_transcript_uses_director_model(self) -> None:
        self.write_agent_config(director="sonnet:high")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.persist_herdr_unit("alpha", prior)
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(self.agent_arguments()[:2], ["--model", "sonnet"])
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))


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


    def test_selected_account_launch_never_reads_shutdown_holds(self) -> None:
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))

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
                "--production", self.production_name, "alpha", "--plan", "docs/plans/given.md",
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
            "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--account", "claude3")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_manual_unit_resume_keeps_its_recorded_model_despite_changed_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        self.persist_unit_account(directory, session="alpha", model="opus")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")

    def test_resume_autocompact_is_recorded_and_passed_to_claude(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                            "--cwd", str(prior), "--autocompact", "200k")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume"):arguments.index("--resume") + 4],
                         ["--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--autocompact", "200k"])
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("62ccbd64-f22e-5647-b20e-7899aa39d766"), "200k")

    def test_registered_unit_autocompact_records_without_new_work_checks(self) -> None:
        _ = self.commit_row_off_the_naming_pattern(session_id="3001165e-ca76-59c2-9490-878224585fad")
        before = self.repository_state()
        result = self.set_unit_autocompact("200k")
        self.assertEqual(result.stdout.strip(),
                         "trunk: auto-compact 200k applies to session 3001165e-ca76-59c2-9490-878224585fad at its next restart")
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("3001165e-ca76-59c2-9490-878224585fad"), "200k")
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_auto_deletes_the_record(self) -> None:
        _ = self.commit_row_off_the_naming_pattern(session_id="3001165e-ca76-59c2-9490-878224585fad")
        with patch.dict(os.environ, self.env):
            autocompact.record("3001165e-ca76-59c2-9490-878224585fad", "200k")
        result = self.set_unit_autocompact("auto")
        self.assertIn("the saved setting applies", result.stdout)
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("3001165e-ca76-59c2-9490-878224585fad"), "")
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_check_writes_nothing(self) -> None:
        _ = self.commit_row_off_the_naming_pattern(session_id="3001165e-ca76-59c2-9490-878224585fad")
        before = self.repository_state()
        result = self.set_unit_autocompact("300k", "--check")
        self.assertEqual(result.stdout.strip(),
                         "trunk: auto-compact 300k would apply to session 3001165e-ca76-59c2-9490-878224585fad at its next restart")
        self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_autocompact_refusals_launch_nothing_and_record_nothing(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        before = self.repository_state()
        for flags, reason in ((("--autocompact", "200k"), "--autocompact requires --resume"),
                              (("--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--autocompact", "50k"),
                               "outside 100k to 1M"),
                              (("--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--autocompact", "big"),
                               "token count such as 200k")):
            with self.subTest(flags=flags):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(reason, result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.servers_started(), [])
        self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())

    def test_unit_resume_uses_its_pane_account_instead_of_the_current_group(self) -> None:
        """A conversation stays on its launch account unless explicitly moved."""
        old = self.prepare_unit_account("claude2")
        caller = self.prepare_unit_account("claude3")
        _ = self.prepare_unit_account("claude1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        self.persist_unit_account(old, session="alpha")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.prepare_herdr_launch("alpha", "--plan", "docs/plans/given.md",
                                  "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        with (patch.dict(os.environ, self.env),
              patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepared):
            code = add_unit.main(["--production", self.production_name, "alpha", "--plan", "docs/plans/given.md",
                "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior)])
        self.assertEqual(code, 0)
        prepared.assert_any_call("claude2", resume=True)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(old))
        self.assertIn("--resume 62ccbd64-f22e-5647-b20e-7899aa39d766", self.launch_command())


    def test_unit_resume_without_recorded_account_uses_configured_units(self) -> None:
        """F004: no retained account is needed once the group is configured."""
        directory = self.prepare_unit_account("claude1")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "9c4ae85d-eb74-56be-a309-c9d336878d2b", "--cwd", str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_unit_resume_refuses_config_directory_outside_configured_account(self) -> None:
        """F004: an explicit directory cannot replace the units account."""
        directory = self.root / "external account with spaces"
        directory.mkdir()
        _ = self.prepare_unit_account("claude1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.checkout), "--config-dir", str(directory))
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
            "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--config-dir", str(directory))
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
            "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--account", "claude3", "--model", "opus")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_refuses_account_that_differs_from_configured_units(self) -> None:
        """F004: --account must agree with the configured group."""
        _ = self.prepare_unit_account("claude2")
        _ = self.prepare_unit_account("claude1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.checkout), "--account", "claude2")
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
                     ("--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.checkout))):
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

    def test_names_without_table_rows_keep_the_staff_name_across_productions(self) -> None:
        sample_doc = self.doc.with_name("sample-production.md")
        _ = sample_doc.write_text(self.production_doc(), encoding="utf-8")
        _ = self.staff.record_production("sample", sample_doc)
        for production_name, names in ((self.production_name, ("designers", "reviewers", "showrunners", "units")),
                                       ("sample", ("reports", "designers-helper"))):
            for name in names:
                with self.subTest(production=production_name, name=name):
                    request = add_unit.launch_request(argparse.Namespace(
                        production=production_name, name=name, plan="docs/plans/given.md", brief=None,
                        standby=False, permanent=False, port=None, timeout=90.0, check=False,
                        resume=None, cwd=None, restart_note=None, session_name=None, host_session=None,
                        probe=False,
                    ))
                    self.assertEqual(request.unit, name)


if __name__ == "__main__":
    _ = unittest.main()
