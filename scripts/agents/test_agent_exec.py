"""Shell launchers select the caller's Claude account or the mapped Codex workers home."""
from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, override
import unittest
from unittest.mock import patch

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
        registry = write_registry(self.root / "agents.conf", {
            "assignments": {"cli": "codex"},
            "cli.codex": {"interactive": "gpt-test:high", "commit_prep": "gpt-test:high"},
            "cli.options": {"codex_service_tier": "default"},
            "codex.agents": {"gpt-test": "high"},
        })
        self.launched = self.root / "codex-start.json"
        self.env.update(AGENT_NOTES_DIR=str(notes), AGENTS_CONFIG_FILE=str(registry),
                        AGENTS_CONFIG_PUBLISH="0", CODEX_CATALOG_SYNC_STATE_FILE=str(registry),
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
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "AGENT_NOTES_DIR": str(notes)})))
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
        registry = self.home / "agents.conf"
        _ = write_registry(registry, {'assignments': {'cli': 'claude', 'sample': 'claude'}, 'cli.claude': {'interactive': 'opus:high'}, 'sample.claude': {'task': 'opus:high'}, 'claude.agents': {'opus': 'low,medium,high,max'}})
        binary = self.home / "bin/claude"
        binary.parent.mkdir()
        _ = binary.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                              + "if sys.argv[1:3] == ['agents', '--json']: print('[]'); sys.exit(0)\n"
                              + "if '--bg' in sys.argv:\n"
                              + " from pathlib import Path\n"
                              + " Path(os.environ['BACKGROUND_LAUNCH_FILE']).write_text(json.dumps({key: os.environ.get(key, '') for key in ['CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_REMOTE_MEMORY_DIR']}))\n"
                              + " print('backgrounded · abcdef12 · fixture'); sys.exit(0)\n"
                              + "print(json.dumps({'config': os.environ.get('CLAUDE_CONFIG_DIR', ''), "
                              + "'memory': os.environ.get('CLAUDE_CODE_REMOTE_MEMORY_DIR', ''), "
                              + "'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')}))\n"
                              + "print(json.dumps({'type': 'result', 'result': 'finished'}))\n")
        binary.chmod(0o755)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")}
        self.environment.update(AGENTS_CONFIG_PUBLISH="0", HOME=str(self.home), AGENT_NOTES_DIR=str(notes), AGENTS_CONFIG_FILE=str(registry),
                                CODEX_CATALOG_SYNC_STATE_FILE=str(registry),
                                XDG_CACHE_HOME=str(self.home / "cache"), XDG_STATE_HOME=str(self.home / "state"),
                                PATH=str(binary.parent) + os.pathsep + os.environ["PATH"])

    def test_shell_launchers_use_the_caller_account_else_the_showrunners_account(self) -> None:
        prompt = self.home / "prompt"
        _ = prompt.write_text("do a task")
        for launcher in ("cli", "exec"):
            for caller, expected in (("claude1", "claude1"), ("", "claude3")):
                with self.subTest(launcher=launcher, caller=caller):
                    environment = dict(self.environment)
                    if caller:
                        environment["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(caller))
                    environment["CLAUDE_CODE_OAUTH_TOKEN"] = "discard-this-test-value"
                    command = ["bash", str(SCRIPTS / "cli_agent/cli_agent.sh")]
                    log = self.home / "launch.log"
                    if launcher == "exec":
                        command = ["bash", str(SCRIPTS / "agents/agent_exec.sh"), "sample.task", "readonly",
                                   str(self.home), str(prompt), str(self.home / "result"), str(log)]
                    result = subprocess.run(command, capture_output=True, text=True, check=True,
                                            env=environment, cwd=self.home)
                    output = log.read_text() if launcher == "exec" else result.stdout
                    launched = cast(LaunchedClaude, json.loads(output.splitlines()[0]))
                    self.assertEqual(launched, {"config": str(accounts.account_dir(expected)),
                                                "memory": str(self.home / ".claude"), "token": ""})

    def test_cli_and_print_launchers_keep_legacy_home_environment_until_cutover(self) -> None:
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
        prompt = self.home / "prompt"
        _ = prompt.write_text("do a task")
        for launcher in ("cli", "exec"):
            with self.subTest(launcher=launcher):
                log = self.home / "legacy.log"
                command = (["bash", str(SCRIPTS / "cli_agent/cli_agent.sh")] if launcher == "cli" else
                           ["bash", str(SCRIPTS / "agents/agent_exec.sh"), "sample.task", "readonly",
                            str(self.home), str(prompt), str(self.home / "result"), str(log)])
                result = subprocess.run(command, capture_output=True, text=True, check=True,
                                        env=self.environment, cwd=self.home)
                output = log.read_text() if launcher == "exec" else result.stdout
                self.assertEqual(json.loads(output.splitlines()[0]), {"config": "", "memory": "", "token": ""})

    def test_background_agent_selects_the_caller_or_showrunners_before_launching(self) -> None:
        prompt = self.home / "background-prompt"
        _ = prompt.write_text("fixture task")
        captured = self.home / "background-environment.json"
        for caller, expected in (("claude1", "claude1"), ("", "claude3")):
            with self.subTest(caller=caller):
                environment = {**self.environment, "AGENT_BG_DETACH": "1", "BACKGROUND_LAUNCH_FILE": str(captured)}
                if caller:
                    environment["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(caller))
                result = subprocess.run(["bash", str(SCRIPTS / "agents/agent_bg.sh"), "fixture-worker", str(self.home),
                                         str(prompt), str(self.home / "summary"), str(self.home / "log"), str(self.home / "id")],
                                        capture_output=True, text=True, check=True, env=environment, cwd=self.home)
                self.assertIn("launched fixture-worker", result.stdout)
                self.assertEqual(json.loads(captured.read_text()), {
                    "CLAUDE_CONFIG_DIR": str(accounts.account_dir(expected)),
                    "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.home / ".claude")})

    def test_berth_fixer_account_selection_reaches_here_and_existing_window_launches(self) -> None:
        worktree = self.home / "rust/berth-fix"
        worktree.mkdir(parents=True)
        ghostty = self.home / "bin/ghostty"
        _ = ghostty.write_text(f"#!{sys.executable}\nimport os, sys\n"
                               + "args = sys.argv[sys.argv.index('-e') + 1:]\n"
                               + "env = {key: value for key, value in os.environ.items() if not key.startswith('CLAUDE')}\n"
                               + "os.execvpe(args[0], args, env)\n")
        ghostty.chmod(0o755)
        for caller, expected in (("claude1", "claude1"), ("", "claude3"), ("legacy", "")):
            environment = dict(self.environment)
            if caller == "legacy":
                _ = (self.home / ".claude.json").write_text(json.dumps({
                    "oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
            elif caller:
                environment["CLAUDE_CONFIG_DIR"] = str(accounts.account_dir(caller))
            for arguments in (["--here"], []):
                with self.subTest(caller=caller, arguments=arguments):
                    result = subprocess.run(["bash", str(SCRIPTS / "berth_fix/launch.sh"), *arguments],
                                            capture_output=True, text=True, check=True, env=environment, cwd=self.home)
                    self.assertEqual(json.loads(result.stdout.splitlines()[0]), {
                        "config": str(accounts.account_dir(expected)) if expected else "",
                        "memory": str(self.home / ".claude") if expected else "", "token": ""})


if __name__ == "__main__":
    _ = unittest.main()
