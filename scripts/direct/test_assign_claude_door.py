"""Verify Claude dispatch through the assign door with a local agent stub."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, final, override
import unittest
from unittest.mock import patch

from scripts.agents import test_codex_mesh as mesh_fixture
from scripts.direct import assign_gate
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, AssignLauncherFixture
from scripts.lib.test_models_registry import write_registry


setUpModule = mesh_fixture.setUpModule


@final
class AssignClaudeDoorTests(AssignLauncherFixture):
    backend_log: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        # The file claim requires a commit for the phase baseline.
        self.commit_project()
        _ = write_registry(self.registry, {
            "assignments": {"direct": "claude"},
            "direct.claude": {"impl": "opus:high", "test": "opus:high",
                              "fix": "opus:high", "review": "opus:xhigh"},
        }, update=True)
        binary_dir = self.session_dir.parent / "bin"
        binary_dir.mkdir()
        backend = binary_dir / "claude"
        self.backend_log = self.session_dir.parent / "claude-argv.jsonl"
        _ = backend.write_text(
            f"#!{sys.executable}\n" +
            "import json, sys\n" +
            "from pathlib import Path\n" +
            f"with Path({str(self.backend_log)!r}).open('a') as output:\n" +
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        backend.chmod(0o755)
        shim = self.home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        shim.symlink_to(ROOT / "scripts/lib/py")
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture@example.invalid"},
        }))
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
            "CLAUDE_BIN": str(backend),
            "UNIT_WORKER_POLL_SECS": "0",
        })))
        _ = os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def assign_command(self) -> list[str]:
        return ["bash", str(ASSIGN), "--role", "implement", "--new-worker", "claude-door",
                "--session-dir", str(self.session_dir), "--phase", "7", "--cwd", str(self.project),
                "--prompt", str(self.prompt), "--paths", str(self.paths)]

    def assert_stub_reached_once(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertTrue(self.backend_log.exists(), result.stdout + result.stderr)
        calls = self.backend_log.read_text().splitlines()
        self.assertEqual(len(calls), 1, result.stdout + result.stderr)
        arguments = cast(list[str], json.loads(calls[0]))
        self.assertEqual(arguments[:1], ["--bg"])
        self.assertIn("--name", arguments)
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertEqual(arguments[arguments.index("--effort") + 1], "high")
        self.assertEqual(self.methods("thread/start"), [])
        # The Bash parent preserves the missing-banner failure status.
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("could not start a background session", result.stderr)
        self.assertNotIn("Claude launcher ended before native identity proof", result.stderr)
        self.assertNotIn(assign_gate.REFUSAL, result.stderr)

    def test_claude_ancestor_reaches_the_configured_claude_stub(self) -> None:
        result = self.run_launcher(self.assign_command(), family="claude")
        self.assert_stub_reached_once(result)

    def test_codex_ancestor_reaches_the_configured_claude_stub(self) -> None:
        result = self.run_launcher(self.assign_command(), family="codex")
        self.assert_stub_reached_once(result)

    def test_non_agent_parent_refuses_before_the_stub(self) -> None:
        result = self.run_launcher(self.assign_command(), family="bash", isolate_ancestors=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("requires a live Claude or Codex ancestor", result.stderr)
        self.assertFalse(self.backend_log.exists())



if __name__ == "__main__":
    _ = unittest.main()
