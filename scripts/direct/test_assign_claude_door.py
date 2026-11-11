"""Check the real assignment ancestor gate and native Claude command boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
from unittest.mock import patch

from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign, assign_gate
from scripts.direct.assign_launcher_test_support import ASSIGN, AssignLauncherFixture
from scripts.lib.test_models_registry import write_registry

setUpModule = codex_fixture.setUpModule


class AssignClaudeDoorTests(AssignLauncherFixture):
    backend_log: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.commit_project()
        _ = write_registry(self.registry, {
            "assignments": {"direct": "claude"},
            "direct.claude": {"impl": "opus:high", "test": "opus:high",
                              "fix": "opus:high", "review": "opus:xhigh"},
        }, update=True)
        self.backend_log = self.session_dir.parent / "worker-argv.jsonl"
        boundary = self.session_dir.parent / "worker-python"
        _ = boundary.write_text(f"#!{sys.executable}\n" + "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "arguments = sys.argv[1:]",
            "if len(arguments) > 1 and Path(arguments[0]).name == 'worker.py':",
            f"    with Path({str(self.backend_log)!r}).open('a') as output: output.write(json.dumps(arguments) + chr(10))",
            "    print('fixture worker did not start', file=sys.stderr)",
            "    raise SystemExit(1)",
            f"os.execv({assign.PYTHON!r}, [{assign.PYTHON!r}, *arguments])",
        )) + "\n")
        boundary.chmod(0o755)
        self.instrument_launcher("from scripts.direct import assign\n" + f"assign.PYTHON = {str(boundary)!r}\n")

    def assign_command(self) -> list[str]:
        return ["bash", str(ASSIGN), "--role", "implement", "--new-worker", "claude-door",
                "--session-dir", str(self.session_dir), "--phase", "7", "--cwd", str(self.project),
                "--prompt", str(self.prompt), "--paths", str(self.paths), "--model", "opus", "--effort", "high"]

    def assert_stub_reached_once(self, result: subprocess.CompletedProcess[str], model: str = "opus") -> None:
        self.assertTrue(self.backend_log.exists(), result.stdout + result.stderr)
        calls = self.backend_log.read_text().splitlines()
        self.assertEqual(len(calls), 1, result.stdout + result.stderr)
        arguments = cast(list[str], json.loads(calls[0]))
        self.assertEqual(arguments[1:3], ["assign", "impl"])
        self.assertIn("--name", arguments)
        self.assertEqual(arguments[arguments.index("--model") + 1], model)
        self.assertEqual(arguments[arguments.index("--effort") + 1], "high")
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(assign_gate.REFUSAL, result.stderr)

    def test_claude_ancestor_reaches_the_configured_claude_stub(self) -> None:
        result = self.run_launcher(self.assign_command(), family="claude")
        self.assert_stub_reached_once(result)

    def test_codex_ancestor_reaches_the_configured_claude_stub(self) -> None:
        config = self.session_dir.parent / "claude-config"
        catalog = config / "cache/model-catalog/fixture-cc.json"
        catalog.parent.mkdir(parents=True)
        binaries = self.session_dir.parent / "bin"
        binaries.mkdir(exist_ok=True)
        binary = binaries / "claude"
        _ = binary.write_text("#!/bin/sh\nprintf '2.1.319 (Claude Code)\\n'\n")
        binary.chmod(0o755)
        for model in ("opus", "claude-opus-fixture"):
            with self.subTest(model=model), patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(config), "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            }):
                if model != "opus":
                    _ = catalog.write_text(json.dumps({"fetchedAt": 1, "catalog": {"config": {
                        "provider_alias_targets": {"opus": {"default": model}},
                        "models": [{"id": model, "min_claude_code_version": "2.1.319"}],
                    }}}))
                self.backend_log.unlink(missing_ok=True)
                result = self.run_launcher(self.assign_command(), family="codex")
                self.assert_stub_reached_once(result)

    def test_non_agent_parent_refuses_before_the_stub(self) -> None:
        result = self.run_launcher(self.assign_command(), family="bash", isolate_ancestors=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("requires a live Claude or Codex ancestor", result.stderr)
        self.assertFalse(self.backend_log.exists())
