"""Launch regressions: real entry functions, isolated commands and refusals."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
from scripts.herdr import herdr, herdr_sessions
from scripts.production import add_unit, add_showrunner
from scripts.notify import review
from scripts.notify.user_action import NoActionRequired
from scripts.direct.assign_launcher_test_support import backend_fixture_command
from scripts.direct import test_claude_worker
from scripts.accounts import accounts
from scripts.lib import nix_env
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff
from scripts.production.production_doc import Production
from scripts.production.staff_test_support import isolated_staff_file, unit_membership
from scripts.shutdown.remote import local_machine
from scripts.lib.nix_env_test_support import NixFixture
from scripts.whoami import account
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class AgentExecEnvironmentTests(NixFixture):
    def test_exec_removes_operation_configuration_after_the_worktree_environment_load(self) -> None:
        _ = self.envrc(self.work)
        restored = {"KEEP": "loaded", "IN_NIX_SHELL": "impure",
                    "MODELS_LAUNCH_CONFIGURATION": "stale launch", "MODELS_REGISTRY_EDIT_BASE": "stale editor"}
        with (chdir(self.work), patch.dict(os.environ, {"DIRENV_DIFF": json.dumps(restored)}),
              patch.object(os, "execvpe", side_effect=SystemExit(0)) as execute):
            with self.assertRaises(SystemExit):
                _ = nix_env.main(["exec", sys.executable, "--version"])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:2], (sys.executable, [sys.executable, "--version"]))
        environment = cast(dict[str, str], execute.call_args.args[2])
        self.assertEqual(environment["KEEP"], "loaded")
        self.assertEqual(environment["NIX_ENV_ENVRC"], str(self.work / ".envrc"))
        self.assertNotIn("MODELS_LAUNCH_CONFIGURATION", environment)
        self.assertNotIn("MODELS_REGISTRY_EDIT_BASE", environment)


class HerdrLaunchTests(NixFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="fixture-server"))
        _ = self.enterContext(patch.object(herdr, "start_server"))
        _ = self.enterContext(patch.object(herdr.Herdr, "run_in_pane"))
        _ = self.enterContext(patch.object(os, "execvp", side_effect=AssertionError("unexpected fixture attach")))
        _ = self.enterContext(patch.object(account, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": str(self.home / ".claude3")}))

    def continued(self, launch: Callable[[], object]) -> object:
        reason = "nix_env: fixture: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"
        events: list[str] = []

        def unavailable(*_args: object, **_kwargs: object) -> nix_env.Refused:
            events.append("environment")
            return nix_env.Refused(reason)

        def open_pane(*_args: object, **_kwargs: object) -> str:
            events.append("pane")
            return "new-pane"

        def start(*_args: object, **_kwargs: object) -> herdr.PromptNotSubmitted:
            events.append("agent")
            return herdr.PromptNotSubmitted("fixture publication is unavailable")

        with (patch.object(nix_env, "ensure", side_effect=unavailable) as ensure,
              patch.object(herdr, "open_tab", side_effect=open_pane),
              patch.object(herdr, "start_agent", side_effect=start)):
            try:
                return launch()
            finally:
                self.assertEqual(Path(cast(str | Path, ensure.call_args.args[0])), self.work)
                self.assertEqual(events, ["environment", "pane", "agent"])

    def test_choke_point_proves_environment_before_opening_and_passes_loaded_env(self) -> None:
        with (patch.object(nix_env, "ensure", return_value=nix_env.Loaded({"IN_NIX_SHELL": "pure", "NIX_LDFLAGS": "-Lfixture"}, self.work / ".envrc")),
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as started):
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", [], herdr.NoOpeningPrompt(), 100, agent="fixture")
        self.assertIsInstance(result, herdr.AgentLaunched)
        self.assertEqual(opened.call_args.args[3], str(self.work))
        self.assertEqual(opened.call_args.args[4], {"IN_NIX_SHELL": "pure", "NIX_LDFLAGS": "-Lfixture"})
        started.assert_called_once()
        _ = self.continued(lambda: herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", [], herdr.NoOpeningPrompt(), 100, agent="fixture"))

    def production(self) -> Production:
        return Production(self.root / "child-production.md", "child", "merge", self.root / "checkout", self.root / "log", ZoneInfo("UTC"),
                          staff.StaffRow(staff.StaffId(""), "showrunner", "child", staff.StaffRoot(),
                                         current_session=staff.StaffId("")))

    def test_choke_point_keeps_the_new_tab_when_agent_start_fails(self) -> None:
        with (patch.object(nix_env, "ensure", return_value=nix_env.NoEnvrc(self.env)),
              patch.object(herdr, "open_tab", return_value="new-pane"),
              patch.object(herdr, "start_agent", side_effect=herdr.HerdrFailure("start refused")),
              patch.object(herdr, "close_agent") as close):
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "codex", [],
                                        herdr.NoOpeningPrompt(), 100, agent="fixture")
        self.assertIsInstance(result, herdr.AgentLaunched)
        assert isinstance(result, herdr.AgentLaunched)
        self.assertIsInstance(result.outcome, herdr.PromptNotSubmitted)
        close.assert_not_called()

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
            port=add_unit.OmittedCell(), session=add_unit.ResumedSession("sid", self.work), timeout=1)
        errors = io.StringIO()
        with isolated_staff_file(self.root / "staff.json", self.env), redirect_stderr(errors):
            member = unit_membership("child", "unit")
            member = staff.set_worktree(member.id, staff.PresentWorktree(local_machine(), request.worktree), expected=member.worktree)
            _ = self.continued(lambda: add_unit.launch_session(request, add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
                "resume", accounts.NamedAccount("claude3", self.home / ".claude3")))
        self.assertIn("launch names: sid (worker): no staff row; saved name retained", errors.getvalue())

    def test_add_showrunner_uses_requested_cwd_before_opening(self) -> None:
        _ = self.continued(lambda: add_showrunner.launch(self.production(), "opus", "high", None, self.work,
            accounts.NamedAccount("claude3", self.home / ".claude3")))


    def test_unit_restore_choke_point_consumes_only_its_own_prepared_environment(self) -> None:
        _ = self.envrc(self.work)
        handoff = {nix_env.HEAL_SESSION_ENV: "sid", nix_env.HEAL_DIRECTORY_ENV: str(self.work.resolve()),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with (patch.dict("os.environ", handoff),
              patch.object(nix_env, "ensure", return_value=nix_env.Loaded(
                  {"IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lloaded"}, self.work / ".envrc")) as ensure,
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as started):
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", ["--resume", "sid"],
                                        herdr.NoOpeningPrompt(), 100, agent="unit")
            self.assertIsInstance(result, herdr.AgentLaunched)
            opened.assert_called_once()
            self.assertEqual(cast(dict[str, str], opened.call_args.args[4])["NIX_LDFLAGS"], "-Lprepared")
            ensure.assert_not_called()
            opened.reset_mock()
            started.reset_mock()
            result = herdr.launch_agent("host", "space", "title", str(self.work), {}, "claude", ["--resume", "other"],
                                        herdr.NoOpeningPrompt(), 100, agent="unit")
            self.assertIsInstance(result, herdr.AgentLaunched)
            opened.assert_called_once()
            started.assert_called_once()
            self.assertEqual(Path(cast(str | Path, ensure.call_args.args[0])), self.work)
            self.assertEqual(cast(dict[str, str], opened.call_args.args[4])["NIX_LDFLAGS"], "-Lloaded")

    def test_herdr_sessions_refusal_prints_one_line_and_opens_nothing(self) -> None:
        reason = "nix_env: fixture: missing shell"
        output = io.StringIO()
        with (patch.object(herdr, "security_session", return_value=herdr.NotMacSession()),
              patch.object(accounts, "account_for_unscoped_launch", return_value=accounts.NamedAccount("claude3", self.home / ".claude3")),
              patch.object(herdr, "claude_trust", return_value=herdr.Trusted(self.work, self.home / ".claude.json", self.work)),
              patch.object(herdr, "server_environment", return_value={}), patch.object(herdr, "Herdr") as client,
              patch.object(nix_env, "ensure", return_value=nix_env.Refused(reason)) as ensure,
              patch.object(herdr, "open_tab", return_value="new-pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptNotSubmitted("fixture")) as started,
              patch.object(os, "execvp", return_value=None) as attached,
              redirect_stdout(output), redirect_stderr(output)):
            client.configure_mock(**{"return_value.panes.return_value": []})
            result = herdr_sessions.main(["herdr_sessions.py", "start", "--name", "worker", "--cwd", str(self.work)])
        self.assertEqual(result, 0)
        self.assertEqual(Path(cast(str | Path, ensure.call_args.args[0])), self.work)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn(reason, output.getvalue())
        opened.assert_called_once()
        started.assert_called_once()
        attached.assert_called_once()


class ShellLaunchTests(NixFixture):
    scripts: Path = Path()
    started: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.scripts = self.home / ".claude/scripts"
        _ = shutil.copytree(ROOT / "scripts/lib", self.scripts / "lib", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        for relative in ("agents/agent_exec.sh", "agents/worker_lifecycle.py", "cli_agent/cli_agent.sh", "berth_fix/launch.sh", "make_a_worktree/direnv_allow.sh"):
            destination = self.scripts / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == "agents/agent_exec.sh":
                destination.symlink_to(ROOT / "scripts" / relative)
            else:
                _ = shutil.copyfile(ROOT / "scripts" / relative, destination)
        (self.scripts / "direct").symlink_to(ROOT / "scripts/direct", target_is_directory=True)
        _ = (self.scripts / "agents/claude_account.sh").write_text('claude_launch_environment() { return 0; }\n')
        _ = (self.scripts / "agents/models_config.sh").write_text(
            (ROOT / "scripts/agents/models_config.sh").read_text()
            + "\nagents_resolve() { AGENT_FAMILY=claude; }\n"
            + "agents_claude_args() { echo '--model fixture'; }\n")
        registry = write_registry(self.root / "models.conf")
        self.env.update(MODELS_CONFIG_FILE=str(registry), MODELS_CONFIG_PUBLISH="0", MODELS_CONFIG_TIER_ONLY="1")
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
        command, environment = ["bash", str(self.scripts / relative), *args], self.env
        if relative == "direct/claude_worker.py":
            run, name, cwd, prompt, summary, log = args
            command = test_claude_worker.worker_command(name, Path(run), Path(cwd), Path(prompt), Path(summary), Path(log))
        if relative == "agents/agent_exec.sh":
            command, environment = backend_fixture_command(self.root / "assign-parent", command, environment)
        return subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True,
                              timeout=TEST_HANG_GUARD_SECS, check=False)

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

    def test_claude_worker(self) -> None:
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture@example.invalid"}}))
        (self.scripts / "production").symlink_to(ROOT / "scripts/production", target_is_directory=True)
        self.env["CLAUDE_CONFIG_DIR"] = ""
        with isolated_staff_file(self.root / "staff.json", self.env):
            owner = unit_membership("child", "unit")
            self.env["CODEX_THREAD_ID"] = owner.id
            self.launch_contract("direct/claude_worker.py", [str(self.root / "run"), "fixture", str(self.work), str(self.root / "prompt"),
                str(self.root / "result"), str(self.root / "launch.log")], self.work)
        rows = [cast(dict[str, object], json.loads(line)) for line in self.started.read_text().splitlines()]
        arguments = cast(list[str], rows[0]["args"])
        settings = cast(dict[str, dict[str, str]], json.loads(arguments[arguments.index("--settings") + 1]))
        self.assertEqual(settings["env"]["IN_NIX_SHELL"], "impure")
        self.assertEqual(settings["env"]["NIX_LDFLAGS"], "-Lfixture")

    def test_berth_fix(self) -> None:
        directory = self.home / "rust/berth-fix"
        directory.mkdir(parents=True)
        self.launch_contract("berth_fix/launch.sh", ["--here"], directory)

    def test_berth_fix_reuses_reserved_id_until_its_account_transcript_exists(self) -> None:
        home = self.home.rename(self.root / "home.with_under score")
        self.scripts = home / ".claude/scripts"
        self.env["HOME"] = str(home)
        directory = home / "rust/berth-fix"
        _ = self.envrc(directory)
        config = home / ".claude3"
        self.env["CLAUDE_CONFIG_DIR"] = str(config)
        self.env["DIRENV_DIFF"] = "{}"
        failed = self.run_script("berth_fix/launch.sh", ["--here"], directory)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.started.exists())
        record = home / ".claude/state/berth-fix/session.json"
        reserved = cast(dict[str, str], json.loads(record.read_text()))["session_id"]
        del self.env["DIRENV_DIFF"]
        project = re.sub(r"[^a-zA-Z0-9]", "-", str(directory))
        # Another account's transcript must not turn the reserved id into a resume.
        other_account = home / ".claude/projects" / project
        other_account.mkdir(parents=True)
        _ = (other_account / f"{reserved}.jsonl").write_text("fixture transcript\n")
        recovered = self.run_script("berth_fix/launch.sh", ["--here"], directory)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        first = cast(dict[str, object], json.loads(self.started.read_text().splitlines()[0]))
        arguments = cast(list[str], first["args"])
        self.assertEqual(arguments[arguments.index("--session-id") + 1], reserved)
        self.assertNotIn("-r", arguments)
        own_history = config / "projects" / project
        own_history.mkdir(parents=True)
        _ = (own_history / f"{reserved}.jsonl").write_text("fixture transcript\n")
        resumed = self.run_script("berth_fix/launch.sh", ["--here"], directory)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        last = cast(dict[str, object], json.loads(self.started.read_text().splitlines()[-1]))
        arguments = cast(list[str], last["args"])
        self.assertEqual(arguments[arguments.index("-r") + 1], reserved)
        self.assertNotIn("--session-id", arguments)

    def test_berth_fix_rerun_moves_account_history_after_an_interrupted_worktree_move(self) -> None:
        repository = self.home / "rust/cargo-liner"
        _ = self.envrc(repository)
        legacy = self.home / "rust/cargo-liner-berth-fix"
        directory = self.home / "rust/berth-fix"
        for arguments in (["init"], ["add", ".envrc"], ["commit", "-m", "fixture"],
                          ["worktree", "add", "-b", "fix/berth", str(legacy)]):
            result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                                    cwd=repository, env=self.env, capture_output=True, text=True,
                                    timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        config = self.home / ".claude3"
        self.env["CLAUDE_CONFIG_DIR"] = str(config)
        old_name = re.sub(r"[^a-zA-Z0-9]", "-", str(legacy))
        new_name = re.sub(r"[^a-zA-Z0-9]", "-", str(directory))
        old_history, new_history = config / "projects" / old_name, config / "projects" / new_name
        old_history.mkdir(parents=True)
        _ = (old_history / "fixture.jsonl").write_text("conversation survives\n")
        default_history = self.home / ".claude/projects" / old_name
        default_history.mkdir(parents=True)
        _ = (default_history / "other-account.jsonl").write_text("other account\n")
        self.script(self.bin / "mv", 'import sys\nprint("fixture stopped before history move", file=sys.stderr)\nraise SystemExit(19)\n')
        interrupted = self.run_script("berth_fix/launch.sh", ["--here"], self.work)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue(directory.is_dir())
        self.assertFalse(legacy.exists())
        self.assertTrue(old_history.is_dir())
        self.assertFalse(new_history.exists())
        self.assertFalse(self.started.exists())
        (self.bin / "mv").unlink()
        recovered = self.run_script("berth_fix/launch.sh", ["--here"], self.work)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(old_history.exists())
        self.assertEqual((new_history / "fixture.jsonl").read_text(), "conversation survives\n")
        self.assertTrue(default_history.is_dir())
        self.assertTrue(self.started.exists())
        # Once repaired, another launch leaves the destination history intact.
        self.assertEqual(self.run_script("berth_fix/launch.sh", ["--here"], self.work).returncode, 0)
        self.assertEqual((new_history / "fixture.jsonl").read_text(), "conversation survives\n")

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
