"""Claude shell launchers keep a caller account or select the showrunners account."""
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


SCRIPTS = Path(__file__).resolve().parents[1]


class LaunchedClaude(TypedDict):
    config: str
    memory: str
    token: str


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
        self.environment.update(HOME=str(self.home), AGENT_NOTES_DIR=str(notes), AGENTS_CONFIG_FILE=str(registry),
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
