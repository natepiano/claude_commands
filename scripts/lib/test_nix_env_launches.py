"""Launch regressions: real entry functions, isolated commands and refusals."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, cast, override
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

ROOT = Path(__file__).resolve().parents[2]
for dependency in ("lib", "herdr", "production", "message", "shutdown", "notify", "whoami", "direct"):
    sys.path.insert(0, str(ROOT / "scripts" / dependency))
sys.path.insert(0, str(ROOT))
if TYPE_CHECKING:
    from scripts.herdr import herdr, herdr_sessions
    from scripts.production import add_unit, add_showrunner, production_units
    from scripts.shutdown import restart, pane_restore, pane_record
    from scripts.notify import review
    from scripts.notify.user_action import NoActionRequired
else:
    import herdr
    import add_unit
    import production_units
    import add_showrunner
    import restart
    import pane_restore
    import pane_record
    import herdr_sessions
    import review
    from user_action import NoActionRequired
from scripts.accounts import accounts
from scripts.lib import nix_env
from scripts.lib.nix_env_test_support import NixFixture
from scripts.whoami import account
from scripts.shutdown import launch_permission


class HerdrLaunchTests(NixFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="fixture-server"))
        _ = self.enterContext(patch.object(herdr, "start_server"))
        _ = self.enterContext(patch.object(account, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": str(self.home / ".claude3")}))

    def refusal(self, launch: Callable[[], object]) -> None:
        reason = "nix_env: fixture: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"
        with (patch.object(nix_env, "ensure", return_value=nix_env.Refused(reason)) as ensure,
              patch.object(herdr, "open_tab") as opened, patch.object(herdr, "start_agent") as started):
            try:
                result = launch()
            except Exception as error:
                self.assertEqual(str(error).splitlines(), [reason])
            else:
                self.assertEqual(result, nix_env.Refused(reason))
            self.assertEqual(Path(cast(str | Path, ensure.call_args.args[0])), self.work)
            opened.assert_not_called()
            started.assert_not_called()

    def test_choke_point_proves_environment_before_opening_and_passes_loaded_env(self) -> None:
        with (patch.object(nix_env, "ensure", return_value=nix_env.Loaded({"IN_NIX_SHELL": "pure", "NIX_LDFLAGS": "-Lfixture"}, self.work / ".envrc")),
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as started):
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", [], herdr.NoOpeningPrompt(), 100, agent="fixture")
        self.assertIsInstance(result, herdr.AgentLaunched)
        self.assertEqual(opened.call_args.args[3], str(self.work))
        self.assertEqual(opened.call_args.args[4], {"IN_NIX_SHELL": "pure", "NIX_LDFLAGS": "-Lfixture"})
        started.assert_called_once()
        self.refusal(lambda: herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", [], herdr.NoOpeningPrompt(), 100, agent="fixture"))

    def production(self) -> production_units.Production:
        return production_units.Production(self.root / "child-production.md", "child", "merge", self.root / "checkout", self.root / "log", ZoneInfo("UTC"))

    def test_choke_point_closes_the_new_tab_when_agent_start_fails(self) -> None:
        with (patch.object(nix_env, "ensure", return_value=nix_env.NoEnvrc(self.env)),
              patch.object(herdr, "open_tab", return_value="new-pane"),
              patch.object(herdr, "start_agent", side_effect=herdr.HerdrFailure("start refused")),
              patch.object(herdr, "close_agent") as close):
            with self.assertRaisesRegex(herdr.HerdrFailure, "start refused"):
                _ = herdr.launch_agent("host", "space", "title", str(self.work), {}, "codex", [],
                                       herdr.NoOpeningPrompt(), 100, agent="fixture")
        close.assert_called_once_with("host", "new-pane")

    def test_inherited_claude_identity_is_removed_but_target_choices_and_nix_env_survive(self) -> None:
        target = {"CLAUDE_CONFIG_DIR": str(self.home / ".claude3"), "CLAUDE_PROBE_SESSION": "target"}

        def unchanged(directory: Path, environment: Mapping[str, str]) -> nix_env.NoEnvrc:
            _ = directory
            return nix_env.NoEnvrc(dict(environment))

        with (patch.dict("os.environ", {"CLAUDE_TEST_SECRET": "secret", "CLAUDE_PROBE_SESSION": "caller",
                                      "CLAUDE_CODE_OAUTH_TOKEN": "caller-token", "NIX_LDFLAGS": "-Lloaded"}),
              patch.object(nix_env, "ensure", side_effect=unchanged),
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired())):
            _ = herdr.launch_agent("host", "space", "title", str(self.work), target, "claude", [],
                                   herdr.NoOpeningPrompt(), 100, agent="fixture")
        environment = cast(dict[str, str], opened.call_args.args[4])
        self.assertNotIn("CLAUDE_TEST_SECRET", environment)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
        self.assertEqual(environment["CLAUDE_PROBE_SESSION"], "target")
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], target["CLAUDE_CONFIG_DIR"])
        self.assertEqual(environment["NIX_LDFLAGS"], "-Lloaded")

    def test_launcher_unit_aliases_are_removed_before_target_values_apply(self) -> None:
        aliases = {"PLAN_DELEGATE_" + name: "launcher-" + name for name in (
            "BOARD_DIR", "TEAM_ROLE", "SESSION_DIR", "ACTIVE_DIR", "HISTORY_DIR",
            "CONFIG", "PASS_OWNER", "SYNC_WRITES", "NOW_EPOCH", "FUTURE_SETTING",
        )}

        def unchanged(directory: Path, environment: Mapping[str, str]) -> nix_env.NoEnvrc:
            _ = directory
            return nix_env.NoEnvrc(dict(environment))

        for target in ({}, {"PLAN_DELEGATE_BOARD_DIR": "target-board", "PLAN_DELEGATE_TEAM_ROLE": "target-slot"}):
            with (self.subTest(target=target), patch.dict("os.environ", aliases),
                  patch.object(nix_env, "ensure", side_effect=unchanged) as ensure,
                  patch.object(herdr, "open_tab", return_value="pane") as opened,
                  patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired())):
                _ = herdr.launch_agent("host", "space", "title", str(self.work), target, "claude", [],
                                       herdr.NoOpeningPrompt(), 100, agent="fixture")
                for environment in (cast(Mapping[str, str], ensure.call_args.args[1]),
                                    cast(Mapping[str, str], opened.call_args.args[4])):
                    self.assertEqual({key: value for key, value in environment.items()
                                      if key.startswith("PLAN_DELEGATE_")}, target)

    def test_account_removals_win_over_inherited_and_direnv_variables(self) -> None:
        with (patch.dict("os.environ", {"REMOVE_ME": "inherited"}),
              patch.object(nix_env, "ensure", return_value=nix_env.Loaded(
                  {"IN_NIX_SHELL": "pure", "REMOVE_ME": "direnv"}, self.work / ".envrc")) as ensure,
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired())):
            _ = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", [],
                                   herdr.NoOpeningPrompt(), 100, agent="fixture", unset_environment=frozenset({"REMOVE_ME"}))
        self.assertNotIn("REMOVE_ME", cast(dict[str, str], ensure.call_args.args[1]))
        self.assertEqual(opened.call_args.args[4], {"IN_NIX_SHELL": "pure"})

    def test_add_unit_uses_session_cwd_before_opening(self) -> None:
        request = add_unit.UnitLaunch(production=self.production(), identity=add_unit.UnitIdentity("unit", "worker"),
            branch="fixture", worktree=self.root / "unused-checkout", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(), session=add_unit.ResumedSession("sid", self.work),
            launch_kind=add_unit.NewWorkLaunch(), timeout=1)
        self.refusal(lambda: add_unit.launch_session(request, add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
            "resume", accounts.NamedAccount("claude3", self.home / ".claude3")))

    def test_add_showrunner_uses_requested_cwd_before_opening(self) -> None:
        self.refusal(lambda: add_showrunner.launch(self.production(), "opus", "high", None, self.work,
            accounts.NamedAccount("claude3", self.home / ".claude3")))

    def test_restart_uses_resume_cwd_before_opening(self) -> None:
        config = self.home / ".claude3"
        parts = restart.ResumeParts(str(self.work), {"CLAUDE_CONFIG_DIR": str(config)}, ("--resume", "sid"),
                                    "resume", restart.RetainResumeAccount(config), restart.ResumeConversation())
        session = {"session_id": "sid", "name": "worker", "kind": "standalone", "cwd": str(self.work)}
        invoke = cast(Callable[..., object], getattr(restart, "_herdr_launch"))
        with (patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": str(config)}),
              patch.object(restart, "_heal_folder_trust"), patch.object(restart, "_holds_live_agent", return_value=False),
              patch.object(restart, "_record_scheduled_restart_prompt"), patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()) as restored,
              patch.object(herdr, "server_environment", return_value={}), patch.object(herdr, "Herdr")):
            self.refusal(lambda: invoke(parts, restart.SharedHerdrTab("host"), session, dry_run=False, session_kind=herdr.NotMacSession()))
        restored.assert_not_called()

    def test_restart_uses_the_healers_prepared_environment_without_another_load(self) -> None:
        _ = self.envrc(self.work)
        config = self.home / ".claude3"
        parts = restart.ResumeParts(str(self.work), {"CLAUDE_CONFIG_DIR": str(config)}, ("--resume", "sid"),
                                    "resume", restart.RetainResumeAccount(config), restart.ResumeConversation())
        session = {"session_id": "sid", "name": "worker", "kind": "standalone", "cwd": str(self.work)}
        invoke = cast(Callable[..., object], getattr(restart, "_herdr_launch"))
        handoff = {nix_env.HEAL_SESSION_ENV: "sid", nix_env.HEAL_DIRECTORY_ENV: str(self.work.resolve()),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with (patch.dict("os.environ", handoff),
              patch.object(nix_env, "ensure", side_effect=AssertionError("no load may follow the healer's final hold check")) as ensure,
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": str(config)}),
              patch.object(restart, "_heal_folder_trust"), patch.object(restart, "_holds_live_agent", return_value=False),
              patch.object(restart, "_record_scheduled_restart_prompt"), patch.object(restart, "_focus_restored_pane", return_value=False),
              patch.object(herdr, "restore_server", return_value=herdr.ServerCreated()),
              patch.object(herdr, "keeps_restored_layout", return_value=True),
              patch.object(pane_restore, "restore", side_effect=AssertionError("heal must not load unrelated panes")),
              patch.object(herdr, "server_environment", return_value={}), patch.object(herdr, "Herdr"),
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptNotSubmitted("fixture")) as started):
            _ = invoke(parts, restart.SharedHerdrTab("host"), session, dry_run=False, session_kind=herdr.NotMacSession())
        ensure.assert_not_called()
        environment = cast(dict[str, str], opened.call_args.args[4])
        self.assertEqual(environment["NIX_LDFLAGS"], "-Lprepared")
        self.assertEqual(environment["IN_NIX_SHELL"], "impure")
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(config))
        self.assertNotIn(nix_env.HEAL_SESSION_ENV, environment)
        self.assertNotIn(nix_env.HEAL_DIRECTORY_ENV, environment)
        started.assert_called_once()

    def test_prepared_restart_proof_cannot_be_used_for_another_session_or_without_a_nix_shell(self) -> None:
        _ = self.envrc(self.work)
        config = self.home / ".claude3"
        parts = restart.ResumeParts(str(self.work), {"CLAUDE_CONFIG_DIR": str(config)}, ("--resume", "sid"),
                                    "resume", restart.RetainResumeAccount(config), restart.ResumeConversation())
        session = {"session_id": "sid", "name": "worker", "kind": "standalone", "cwd": str(self.work)}
        invoke = cast(Callable[..., object], getattr(restart, "_herdr_launch"))
        for identity, directory, shell in (("another-session", str(self.work), "impure"),
                                            ("sid", str(self.root), "impure"), ("sid", str(self.work), "")):
            with (self.subTest(identity=identity, directory=directory, shell=shell),
                  patch.dict("os.environ", {nix_env.HEAL_SESSION_ENV: identity, nix_env.HEAL_DIRECTORY_ENV: directory,
                                             "IN_NIX_SHELL": shell}),
                  patch.object(nix_env, "ensure", side_effect=AssertionError("a bad handoff must fail closed")),
                  patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": str(config)}),
                  patch.object(restart, "_heal_folder_trust"), patch.object(herdr, "restore_server") as restored,
                  patch.object(herdr, "open_tab") as opened, patch.object(herdr, "start_agent") as started):
                with self.assertRaisesRegex(OSError, "^nix_env: "):
                    _ = invoke(parts, restart.SharedHerdrTab("host"), session, dry_run=False, session_kind=herdr.NotMacSession())
                restored.assert_not_called()
                opened.assert_not_called()
                started.assert_not_called()

    def test_unit_restore_choke_point_consumes_only_its_own_prepared_environment(self) -> None:
        _ = self.envrc(self.work)
        handoff = {nix_env.HEAL_SESSION_ENV: "sid", nix_env.HEAL_DIRECTORY_ENV: str(self.work.resolve()),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with (patch.dict("os.environ", handoff),
              patch.object(nix_env, "ensure", side_effect=AssertionError("a unit restore must reuse the proved environment")) as ensure,
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as started):
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", ["--resume", "sid"],
                                        herdr.NoOpeningPrompt(), 100, agent="unit")
            self.assertIsInstance(result, herdr.AgentLaunched)
            opened.assert_called_once()
            self.assertEqual(cast(dict[str, str], opened.call_args.args[4])["NIX_LDFLAGS"], "-Lprepared")
            opened.reset_mock()
            started.reset_mock()
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", ["--resume", "other"],
                                        herdr.NoOpeningPrompt(), 100, agent="unit")
            self.assertIsInstance(result, nix_env.Refused)
            opened.assert_not_called()
            started.assert_not_called()
            ensure.assert_not_called()

    def test_pane_restore_refusal_preserves_the_old_pane(self) -> None:
        client = Mock()
        client.configure_mock(**{"pane_activity.return_value": herdr.PaneAtShell()})
        entry = pane_record.launch("pane", "sid", str(self.work), {"CLAUDE_CONFIG_DIR": str(self.home / ".claude3")},
                                   ["claude", "-n", "worker"])
        invoke = cast(Callable[..., object], getattr(pane_restore, "_resume"))
        with patch.object(pane_restore, "named_account_environment", return_value={}):
            self.refusal(lambda: invoke("host", client, pane_restore.RestoredPane("pane", "worker", "space", "sid"), entry))
        cast(Mock, client.close_tab_of).assert_not_called()

    def test_herdr_sessions_refusal_prints_one_line_and_opens_nothing(self) -> None:
        reason = "nix_env: fixture: missing shell"
        output = io.StringIO()
        with (patch.object(herdr, "security_session", return_value=herdr.NotMacSession()),
              patch.object(accounts, "account_for_unscoped_launch", return_value=accounts.NamedAccount("claude3", self.home / ".claude3")),
              patch.object(herdr, "claude_trust", return_value=herdr.Trusted(self.work, self.home / ".claude.json", self.work)),
              patch.object(launch_permission, "launch_barrier", side_effect=lambda: nullcontext()),
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
              patch.object(herdr, "server_environment", return_value={}), patch.object(herdr, "Herdr") as client,
              patch.object(nix_env, "ensure", return_value=nix_env.Refused(reason)) as ensure,
              patch.object(herdr, "open_tab") as opened, patch.object(herdr, "start_agent") as started,
              redirect_stdout(output), redirect_stderr(output)):
            client.configure_mock(**{"return_value.panes.return_value": []})
            result = herdr_sessions.main(["herdr_sessions.py", "start", "--name", "worker", "--cwd", str(self.work)])
        self.assertNotEqual(result, 0)
        self.assertEqual(Path(cast(str | Path, ensure.call_args.args[0])), self.work)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn(reason, output.getvalue())
        opened.assert_not_called()
        started.assert_not_called()


class ShellLaunchTests(NixFixture):
    scripts: Path = Path()
    started: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.scripts = self.home / ".claude/scripts"
        for relative in ("agents/agent_exec.sh", "agents/agent_bg.sh", "cli_agent/cli_agent.sh", "berth_fix/launch.sh", "make_a_worktree/direnv_allow.sh", "lib/nix_env.py"):
            destination = self.scripts / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copyfile(ROOT / "scripts" / relative, destination)
        py = self.scripts / "lib/py"
        _ = py.write_text('#!/bin/sh\nexec ' + sys.executable + ' "$@"\n')
        py.chmod(0o755)
        _ = (self.scripts / "agents/claude_account.sh").write_text('claude_launch_environment() { return 0; }\n')
        _ = (self.scripts / "agents/agents_config.sh").write_text('''agents_resolve() { AGENT_FAMILY=claude; }
agents_claude_args() { echo '--model fixture'; }
''')
        self.started = self.root / "started.jsonl"
        self.script(self.bin / "claude", '''import json, os, sys
from pathlib import Path
if sys.argv[1:2] == ["agents"]:
    print("[]")
    raise SystemExit(0)
with Path(os.environ["START_LOG"]).open("a") as stream:
    stream.write(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:],"shell":os.environ.get("IN_NIX_SHELL", "")}) + "\\n")
print('{"type":"result","result":"fixture completed"}')
''')
        self.env["START_LOG"] = str(self.started)
        _ = (self.root / "prompt").write_text("fixture prompt")

    def run_script(self, relative: str, args: list[str], directory: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.scripts / relative), *args], cwd=directory, env=self.env,
                              capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)

    def launch_contract(self, relative: str, args: list[str], directory: Path) -> None:
        _ = self.envrc(directory)
        self.env["DIRENV_DIFF"] = "{}"
        denied = self.run_script(relative, args, directory)
        self.assertNotEqual(denied.returncode, 0, denied.stdout)
        self.assertFalse(self.started.exists(), denied.stdout + denied.stderr)
        diagnostics = denied.stderr
        log = self.root / "launch.log"
        if log.exists():
            diagnostics += log.read_text()
        self.assertIn("nix_env:", diagnostics)
        self.assertEqual(len([line for line in diagnostics.splitlines() if "nix_env:" in line]), 1)
        self.assertTrue(all(row["cwd"] == str(directory) for row in self.direnv_calls()))
        del self.env["DIRENV_DIFF"]
        result = self.run_script(relative, args, directory)
        self.assertTrue(self.started.exists(), result.stdout + result.stderr)
        rows = [cast(dict[str, object], json.loads(line)) for line in self.started.read_text().splitlines()]
        self.assertEqual(rows[0]["cwd"], str(directory))
        self.assertEqual(rows[0]["shell"], "impure")

    def test_agent_exec(self) -> None:
        self.launch_contract("agents/agent_exec.sh", ["test", "readonly", str(self.work), str(self.root / "prompt"),
                             str(self.root / "result"), str(self.root / "launch.log")], self.work)

    def test_cli_agent(self) -> None:
        self.launch_contract("cli_agent/cli_agent.sh", [], self.work)

    def test_agent_bg(self) -> None:
        self.launch_contract("agents/agent_bg.sh", ["fixture", str(self.work), str(self.root / "prompt"),
            str(self.root / "result"), str(self.root / "launch.log"), str(self.root / "id")], self.work)
        rows = [cast(dict[str, object], json.loads(line)) for line in self.started.read_text().splitlines()]
        arguments = cast(list[str], rows[0]["args"])
        settings = cast(dict[str, dict[str, str]], json.loads(arguments[arguments.index("--settings") + 1]))
        self.assertEqual(settings["env"]["IN_NIX_SHELL"], "impure")
        self.assertEqual(settings["env"]["NIX_LDFLAGS"], "-Lfixture")

    def test_berth_fix(self) -> None:
        directory = self.home / "rust/berth-fix"
        directory.mkdir(parents=True)
        self.launch_contract("berth_fix/launch.sh", ["--here"], directory)

    def test_direnv_allow_is_a_failing_helper_call_not_a_warning(self) -> None:
        _ = self.envrc(self.work)
        self.env["DIRENV_DIFF"] = "{}"
        denied = self.run_script("make_a_worktree/direnv_allow.sh", [str(self.work)], self.work)
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(denied.stdout, "")
        self.assertEqual(denied.stderr.splitlines(), [f"nix_env: {self.work}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"])
        del self.env["DIRENV_DIFF"]
        allowed = self.run_script("make_a_worktree/direnv_allow.sh", [str(self.work)], self.work)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, "")


class NotifyLaunchTests(NixFixture):
    def test_review_refusal_never_starts_reviewer_and_success_passes_loaded_env(self) -> None:
        draft = review.Draft("fixture", "0", "Fixture", NoActionRequired(), "Fixture body.", False)
        calls: list[tuple[list[str], dict[str, str]]] = []

        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            self.assertGreater(timeout, 0)
            calls.append((argv, env))
            return subprocess.CompletedProcess(argv, 0, "guide" if argv[0] == "zsh" else "{}", "")

        with (patch.object(review, "GUIDE", self.work), patch.object(review, "reviewer_environment", return_value=(self.env, "fixture")),
              patch.object(review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(self.bin / "claude")),
              patch.object(review, "reviewer_assignment", return_value=("opus", "high")),
              patch.object(nix_env, "ensure", return_value=nix_env.Refused("nix_env: fixture: missing shell")) as ensure,
              patch.object(Path, "cwd", return_value=self.work)):
            result = review.review(draft, False, runner=runner)
            self.assertIsInstance(result, review.Unreviewed)
            if isinstance(result, review.Unreviewed):
                self.assertEqual(result.why, "nix_env: fixture: missing shell")
            ensure.assert_called_once_with(self.work, self.env)
            self.assertEqual(len(calls), 1)  # guide only; reviewer never starts
            ensure.return_value = nix_env.Loaded({**self.env, "IN_NIX_SHELL": "pure"}, self.work / ".envrc")
            _ = review.review(draft, False, runner=runner)
        self.assertEqual(calls[-1][0][0], str(self.bin / "claude"))
        self.assertEqual(calls[-1][1]["IN_NIX_SHELL"], "pure")
