"""Shell launchers select the caller's Claude account or the mapped Codex workers home."""
from __future__ import annotations

from scripts.lib.test_models_registry import RegistryReadProbe, write_registry
from scripts.direct.assign_launcher_test_support import backend_fixture_command
from scripts.direct import test_claude_worker

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, override
import unittest
from unittest.mock import patch

from scripts.lib import models_registry
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import sessions
from scripts.production.staff_test_support import isolated_staff_file, unit_membership, fixture_runtime_id, source_environment
from scripts.accounts import accounts
from scripts.lib.nix_env_test_support import NixFixture
from scripts.whoami.account_test_support import write_codex_auth


SCRIPTS = Path(__file__).resolve().parents[1]


class LaunchedClaude(TypedDict):
    config: str
    memory: str
    token: str


class LaunchedCodex(TypedDict):
    home: str
    arguments: list[str]


class CodexCliLauncherTests(NixFixture):
    workers: Path = Path()
    launched: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        notes = self.root / "notes"
        notes.mkdir()
        login = "cli-workers@example.invalid"
        _ = (notes / "codex1.md").write_text(f"---\nlogin: {login}\n---\n")
        # The selected login can be held in a differently numbered home.
        self.workers = self.home / ".codex7"
        self.workers.mkdir()
        write_codex_auth(self.workers, login)
        selection = self.home / ".claude/config/accounts.json"
        selection.parent.mkdir(parents=True)
        _ = selection.write_text('{"workers": "codex1"}')
        registry = write_registry(self.root / "models.conf", {
            "assignments": {"cli": "codex"},
            "cli.codex": {"interactive": "gpt-test:high", "commit_prep": "gpt-test:high"},
            "cli.options": {"codex_service_tier": "default"},
            "codex.agents": {"gpt-test": "high"},
        })
        self.launched = self.root / "codex-start.json"
        self.env.update(AGENT_NOTES_DIR=str(notes), MODELS_CONFIG_FILE=str(registry),
                        MODELS_CONFIG_PUBLISH="0", CODEX_CATALOG_SYNC_STATE_FILE=str(registry),
                        CODEX_CONFIG_FILE=str(self.root / "codex.toml"),
                        CODEX_MODELS_CACHE_FILE=str(self.root / "models.json"),
                        CODEX_START_LOG=str(self.launched))
        self.script(self.bin / "codex", '''import json, os, sys
from pathlib import Path
_ = Path(os.environ["CODEX_START_LOG"]).write_text(json.dumps({
    "home": os.environ.get("CODEX_HOME", ""), "arguments": sys.argv[1:]}))
''')

    def test_cli_repl_and_skill_use_the_workers_home_over_a_callers_home(self) -> None:
        for arguments in ([], ["commit_prep", "noclippy"]):
            for caller_home in ("", str(self.root / "other-codex-home")):
                with self.subTest(arguments=arguments, caller_home=caller_home):
                    environment = dict(self.env)
                    if caller_home:
                        environment["CODEX_HOME"] = caller_home
                    result = subprocess.run(["bash", str(SCRIPTS / "cli_agent/cli_agent.sh"), *arguments],
                                            env=environment, cwd=self.work, capture_output=True, text=True,
                                            timeout=300, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    launched = cast(LaunchedCodex, json.loads(self.launched.read_text()))
                    self.assertEqual(launched["home"], str(self.workers))
                    expected = ["-m", "gpt-test", "-c", 'model_reasoning_effort="high"',
                                "-c", 'service_tier="default"']
                    if arguments:
                        expected.append("commit_prep noclippy")
                    self.assertEqual(launched["arguments"], expected)

    def test_a_launch_inside_a_worker_reads_the_changed_registry(self) -> None:
        registry = Path(self.env["MODELS_CONFIG_FILE"])
        original = write_registry(registry, {"assignments": {"sample": "codex"},
            "sample.codex": {"task": "gpt-test:high"}, "sample.options": {"codex_service_tier": "fast"}}).read_text()
        changed = original.replace("task=gpt-test:high", "task=gpt-next:low")
        changed = changed.replace("[codex.agents]", "[codex.agents]\ngpt-next=low")
        changed = changed.replace("codex_service_tier=fast", "codex_service_tier=default")
        prompt = self.root / "prompt"
        _ = prompt.write_text("launch from a worker")
        self.script(self.bin / "codex", '''import json, os, subprocess, sys
from pathlib import Path
with Path(os.environ["CODEX_START_LOG"]).open("a") as output:
    output.write(json.dumps({"arguments": sys.argv[1:], "operation_environment": {
        key: os.environ[key] for key in ("MODELS_LAUNCH_CONFIGURATION", "MODELS_REGISTRY_EDIT_BASE") if key in os.environ}}) + "\\n")
Path(sys.argv[sys.argv.index("-o") + 1]).write_text("complete")
if "NESTED_LAUNCH" not in os.environ:
    Path(os.environ["MODELS_CONFIG_FILE"]).write_text(os.environ["NEXT_REGISTRY"])
    command = ["bash", os.environ["AGENT_EXEC"], "sample.task", "readonly", os.getcwd(),
               os.environ["PROMPT"], os.environ["NESTED_OUTPUT"], os.environ["NESTED_LOG"]]
    sys.path.insert(0, os.environ["FIXTURE_CHECKOUT"])
    from scripts.direct.assign_launcher_test_support import backend_fixture_command
    command, environment = backend_fixture_command(
        Path(os.environ["NESTED_LOG"]).parent / "nested-assign-parent", command,
        {**os.environ, "NESTED_LAUNCH": "1"})
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
''')
        with patch.dict(os.environ, self.env):
            environment = models_registry.launch_configuration(os.environ).environment(
                models_registry.edit_base(os.environ).environment(self.env))
        environment.update(NEXT_REGISTRY=changed, AGENT_EXEC=str(SCRIPTS / "agents/agent_exec.sh"),
            PROMPT=str(prompt), NESTED_OUTPUT=str(self.root / "nested-output"), NESTED_LOG=str(self.root / "nested-log"),
            FIXTURE_CHECKOUT=str(SCRIPTS.parent))
        command, environment = backend_fixture_command(self.root / "assign-parent",
            ["bash", environment["AGENT_EXEC"], "sample.task", "readonly", str(self.work),
             str(prompt), str(self.root / "output"), str(self.root / "log")], environment)
        result = subprocess.run(command, env=environment,
            cwd=self.work, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + (self.root / "log").read_text())
        launches = [cast(dict[str, object], json.loads(line)) for line in self.launched.read_text().splitlines()]
        self.assertEqual(len(launches), 2)
        for launch, model, effort, tier in zip(launches, ("gpt-test", "gpt-next"), ("high", "low"), ("fast", "default")):
            with self.subTest(model=model):
                self.assertEqual(launch["operation_environment"], {})
                arguments = cast(list[str], launch["arguments"])
                self.assertEqual(arguments[arguments.index("-m") + 1], model)
                self.assertIn(f'model_reasoning_effort="{effort}"', arguments)
                self.assertIn(f'service_tier="{tier}"', arguments)
        self.assertEqual((self.root / "nested-output").read_text(), "complete")

    def test_unavailable_workers_account_refuses_before_codex_starts(self) -> None:
        (self.workers / "auth.json").unlink()
        for arguments in ([], ["commit_prep", "noclippy"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(["bash", str(SCRIPTS / "cli_agent/cli_agent.sh"), *arguments],
                                        env={**self.env, "CODEX_HOME": str(self.workers)}, cwd=self.work,
                                        capture_output=True, text=True, timeout=300, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("accounts: workers is on codex1", result.stderr)
                self.assertFalse(self.launched.exists())


class ClaudeLauncherTests(unittest.TestCase):
    home: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = self.home / "notes"
        notes.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "PATH": os.environ.get("PATH", os.defpath),
            "AGENT_NOTES_DIR": str(notes), "STAFF_FILE": str(self.home / "staff.json"),
            "STAFF_CONFIG": str(SCRIPTS.parent / "config/staff.conf"),
            "CONF_DIR": str(SCRIPTS.parent / "config"),
            **source_environment(self.home / "sources")}, clear=True)))
        shim = self.home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        shim.symlink_to(SCRIPTS / "lib/py")
        _ = (self.home / ".claude.json").write_text("{}")
        for label in ("claude1", "claude3"):
            _ = (notes / f"{label}.md").write_text(f"---\nlogin: {label}@example.invalid\n---\n")
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": f"{label}@example.invalid"}}))
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"showrunners": "claude3"}))
        registry = self.home / "models.conf"
        _ = write_registry(registry, {'assignments': {'cli': 'claude', 'sample': 'claude'}, 'cli.claude': {'interactive': 'opus:high'}, 'sample.claude': {'task': 'opus:high'}, 'claude.agents': {'opus': 'low,medium,high,max'}})
        binary = self.home / "bin/claude"
        binary.parent.mkdir()
        _ = binary.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                              + "if os.environ.get('TEST_AGENT_ENV'):\n"
                              + " from pathlib import Path\n"
                              + " Path(os.environ['TEST_AGENT_ENV']).write_text(json.dumps(dict(os.environ)))\n"
                              + "if sys.argv[1:3] == ['agents', '--json']: print('[]'); sys.exit(0)\n"
                              + "if '--bg' in sys.argv:\n"
                              + " from pathlib import Path\n"
                              + " Path(os.environ['BACKGROUND_LAUNCH_FILE']).write_text(json.dumps({key: os.environ.get(key, '') for key in ['CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_REMOTE_MEMORY_DIR']}))\n"
                              + " Path(os.environ['FIXTURE_REGISTRY_PATH']).write_text(os.environ['FIXTURE_REGISTRY_RECORD'])\n"
                              + " print('backgrounded · ' + os.environ['FIXTURE_NATIVE_ID'][:8] + ' · fixture'); sys.exit(0)\n"
                              + "print(json.dumps({'config': os.environ.get('CLAUDE_CONFIG_DIR', ''), "
                              + "'memory': os.environ.get('CLAUDE_CODE_REMOTE_MEMORY_DIR', ''), "
                              + "'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')}))\n"
                              + "print(json.dumps({'type': 'result', 'result': 'finished'}))\n")
        binary.chmod(0o755)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")}
        self.environment.update(MODELS_CONFIG_PUBLISH="0", HOME=str(self.home), AGENT_NOTES_DIR=str(notes), MODELS_CONFIG_FILE=str(registry),
                                CODEX_CATALOG_SYNC_STATE_FILE=str(registry),
                                XDG_CACHE_HOME=str(self.home / "cache"), XDG_STATE_HOME=str(self.home / "state"),
                                PATH=str(binary.parent) + os.pathsep + os.environ["PATH"])

    def test_cli_agents_remove_operation_configuration_after_resolving_both_families(self) -> None:
        notes = Path(self.environment["AGENT_NOTES_DIR"])
        _ = (notes / "codex1.md").write_text("---\nlogin: cli@example.invalid\n---\n")
        workers = self.home / ".codex1"
        workers.mkdir()
        write_codex_auth(workers, "cli@example.invalid")
        selection = accounts.accounts_config_file()
        selection.parent.mkdir(parents=True, exist_ok=True)
        _ = selection.write_text('{"workers":"codex1"}')
        binary = self.home / "bin/codex"
        _ = binary.write_text(f"#!{sys.executable}\nimport json, os\nfrom pathlib import Path\n"
                              + "Path(os.environ['TEST_AGENT_ENV']).write_text(json.dumps(dict(os.environ)))\n")
        binary.chmod(0o755)
        recorded = self.home / "cli-environment.json"
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                _ = write_registry(Path(self.environment["MODELS_CONFIG_FILE"]), {"assignments": {"cli": family}})
                with patch.dict(os.environ, {**self.environment, "MODELS_CONFIG_TIER_ONLY": "1"}):
                    environment = models_registry.launch_configuration({}).environment(
                        models_registry.edit_base({}).environment(self.environment))
                environment.update(TEST_AGENT_ENV=str(recorded), MODELS_CONFIG_TIER_ONLY="1",
                                   CLAUDE_CONFIG_DIR=str(accounts.account_dir("claude1")))
                result = subprocess.run(["bash", str(SCRIPTS / "cli_agent/cli_agent.sh")],
                                        cwd=self.home, env=environment, text=True, capture_output=True,
                                        timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                launched = cast(dict[str, str], json.loads(recorded.read_text()))
                self.assertNotIn("MODELS_LAUNCH_CONFIGURATION", launched)
                self.assertNotIn("MODELS_REGISTRY_EDIT_BASE", launched)
                self.assertEqual(launched["MODELS_CONFIG_FILE"], environment["MODELS_CONFIG_FILE"])
                if family == "codex":
                    self.assertEqual(launched["CODEX_HOME"], str(workers))
                else:
                    self.assertEqual(launched["CLAUDE_CONFIG_DIR"], str(accounts.account_dir("claude1")))

    def test_claude_exec_does_not_inherit_launch_or_editor_configuration(self) -> None:
        prompt = self.home / "prompt"
        _ = prompt.write_text("read the task")
        recorded = self.home / "agent-environment.json"
        with patch.dict(os.environ, self.environment):
            environment = models_registry.launch_configuration(os.environ).environment(
                models_registry.edit_base(os.environ).environment(self.environment))
        command, environment = backend_fixture_command(self.home / "assign-parent",
            ["bash", str(SCRIPTS / "agents/agent_exec.sh"), "sample.task", "readonly",
             str(self.home), str(prompt), str(self.home / "output"), str(self.home / "log")],
            {**environment, "TEST_AGENT_ENV": str(recorded)})
        result = subprocess.run(command, env=environment, cwd=self.home,
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        agent_environment = cast(dict[str, str], json.loads(recorded.read_text()))
        self.assertNotIn("MODELS_LAUNCH_CONFIGURATION", agent_environment)
        self.assertNotIn("MODELS_REGISTRY_EDIT_BASE", agent_environment)
        self.assertEqual((self.home / "output").read_text().strip(), "finished")

    def test_shell_launchers_use_the_caller_account_else_the_showrunners_account(self) -> None:
        prompt = self.home / "prompt"
        _ = prompt.write_text("do a task")
        for launcher in ("cli", "exec"):
            for caller, expected in (("claude1", "claude1"), ("", "claude3")):
                with self.subTest(launcher=launcher, caller=caller):
                    probe = RegistryReadProbe(self.home, Path(self.environment["MODELS_CONFIG_FILE"]))
                    environment = probe.environment(self.environment)
                    if caller:
                        environment["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(caller))
                    environment["CLAUDE_CODE_OAUTH_TOKEN"] = "discard-this-test-value"
                    command = ["bash", str(SCRIPTS / "cli_agent/cli_agent.sh")]
                    log = self.home / "launch.log"
                    if launcher == "exec":
                        command = ["bash", "-c", 'source "$1"; shift; agents_exec_main "$@"', "fixture",
                                   str(SCRIPTS / "agents/agent_exec.sh"), "sample.task", "readonly",
                                   str(self.home), str(prompt), str(self.home / "result"), str(log)]
                    result = subprocess.run(command, capture_output=True, text=True, check=True,
                                            env=environment, cwd=self.home)
                    output = log.read_text() if launcher == "exec" else result.stdout
                    launched = cast(LaunchedClaude, json.loads(output.splitlines()[0]))
                    self.assertEqual(launched, {"config": str(accounts.account_dir(expected)),
                                                "memory": str(self.home / ".claude"), "token": ""})
                    if launcher == "exec":
                        self.assertEqual(len(probe.reads()), 1, probe.reads())

    def test_cli_and_print_launchers_keep_legacy_home_environment_until_cutover(self) -> None:
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
        prompt = self.home / "prompt"
        _ = prompt.write_text("do a task")
        for launcher in ("cli", "exec"):
            with self.subTest(launcher=launcher):
                log = self.home / "legacy.log"
                command = (["bash", str(SCRIPTS / "cli_agent/cli_agent.sh")] if launcher == "cli" else
                           ["bash", "-c", 'source "$1"; shift; agents_exec_main "$@"', "fixture",
                                   str(SCRIPTS / "agents/agent_exec.sh"), "sample.task", "readonly",
                            str(self.home), str(prompt), str(self.home / "result"), str(log)])
                result = subprocess.run(command, capture_output=True, text=True, check=True,
                                        env=self.environment, cwd=self.home)
                output = log.read_text() if launcher == "exec" else result.stdout
                self.assertEqual(json.loads(output.splitlines()[0]), {"config": "", "memory": "", "token": ""})

    def test_background_agent_selects_the_caller_or_showrunners_before_launching(self) -> None:
        prompt = self.home / "background-prompt"
        _ = prompt.write_text("fixture task")
        captured = self.home / "background-environment.json"
        sender = self.home / "send.py"
        _ = sender.write_text("print('SENT: fixture')\n")
        native = fixture_runtime_id("background-account-selection")
        record: dict[str, object] = {"pid": os.getpid(), "procStart": sessions.process_start(os.getpid()),
                  "sessionId": native, "name": "fixture-worker", "entrypoint": "cli",
                  "messagingSocketPath": "", "updatedAt": 1, "formerNames": [], "tmux": ""}
        for caller, expected in (("claude1", "claude1"), ("", "claude3")):
            private = self.home / f"background-{expected}"
            private_env = source_environment(private / "sources")
            for folder in private_env.values():
                Path(folder).mkdir(parents=True, exist_ok=True)
            with (self.subTest(caller=caller),
                  isolated_staff_file(private / "staff.json", private_env),
                  patch.dict(os.environ, private_env)):
                owner = unit_membership("fixture", "account-selection")
                registry = Path(private_env["NOTIFIER_SESSIONS_DIR"])
                environment = {**self.environment, **private_env, "BACKGROUND_LAUNCH_FILE": str(captured),
                               "CODEX_THREAD_ID": owner.id, "NOTIFIER_SESSIONS_DIR": str(registry),
                               "FIXTURE_REGISTRY_PATH": str(registry / f"{os.getpid()}.json"),
                               "FIXTURE_REGISTRY_RECORD": json.dumps(record), "FIXTURE_NATIVE_ID": native,
                               "CLAUDE_WORKER_SEND_SCRIPT": str(sender)}
                _ = environment.pop("CLAUDE_CONFIG_DIR", None)
                if caller:
                    environment["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(caller))
                command = test_claude_worker.worker_command("fixture-worker", private / "run", self.home, prompt,
                                         self.home / "summary", self.home / "log")
                _ = subprocess.run(command, capture_output=True, text=True, check=True,
                                   env=environment, cwd=self.home)
                self.assertEqual(json.loads(captured.read_text()), {
                    "CLAUDE_CONFIG_DIR": str(accounts.account_dir(expected)),
                    "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.home / ".claude")})

    def test_shell_fixture_cannot_use_the_parent_session_state(self) -> None:
        parent = self.home / "parent-session"
        parent.mkdir()
        paths = (parent / "mesh_roster.json", parent / "staff.json", parent / "impl_worker_impl.json")
        for path in paths:
            _ = path.write_text('{"parent": "must remain unchanged"}\n')
        before = {path.name: path.read_bytes() for path in paths}
        environment = {**self.environment, "UNIT_RUN_DIR": str(parent),
            "PLAN_DELEGATE_BOARD_DIR": str(parent), "MESH_SESSION_DIR": str(parent),
            "STAFF_FILE": str(paths[1]), "STAFF_ID": "parent-worker",
            "WORKER_STAFF_ID": "parent-worker", "CODEX_THREAD_ID": "parent-thread",
            "CLAUDE_CODE_SESSION_ID": "parent-session", "ROSTER_RUN_ROOT": str(parent),
            "NOTIFIER_SESSIONS_DIR": str(parent), "UNIT_WORKERS_STATE": str(parent)}
        result = subprocess.run(["bash", str(SCRIPTS / "agents/test_agent_exec.sh")],
                                env=environment, cwd=self.home, capture_output=True, text=True, check=False)
        self.assertEqual({path.name: path.read_bytes() for path in parent.iterdir()}, before)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agent_exec tests passed", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
