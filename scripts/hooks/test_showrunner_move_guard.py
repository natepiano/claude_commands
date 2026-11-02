"""A showrunner's hand paths for stopping or moving a session are refused and name /showrunner:move."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast, override

HOOK = Path(__file__).with_name("pre-tool-use-showrunner-move-guard.py")
spec = importlib.util.spec_from_file_location("showrunner_move_guard", HOOK)
assert spec is not None and spec.loader is not None


class Guard(Protocol):
    EXIT_REFUSAL: str
    NOW_REFUSAL: str
    KILL_REFUSAL: str
    RESUME_REFUSAL: str

    def refusal(self, command: str, proc: Path | None = None) -> str: ...


module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
guard = cast(Guard, cast(object, module))
SHOWRUNNER = "11111111-2222-3333-4444-555555555555"
MOVE = '~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunner_move.py session nightly-review "claude 1"'


class RefusalTest(unittest.TestCase):
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.proc = Path(root.name)
        for pid, comm, argv in ((4242, "claude", b"claude\0--resume\0"), (5151, "node", b"/usr/bin/node\0server.js\0")):
            (self.proc / str(pid)).mkdir()
            _ = (self.proc / str(pid) / "comm").write_text(comm + "\n", encoding="utf-8")
            _ = (self.proc / str(pid) / "cmdline").write_bytes(argv)

    def refused(self, command: str) -> str:
        return guard.refusal(command, self.proc)

    def test_exit_sent_into_another_pane_is_refused(self) -> None:
        for command in ("tmux send-keys -t merge-gate:0.0 /exit Enter",
                        "tmux send-keys -t merge-gate '/exit' Enter && sleep 1",
                        "herdr pane send --pane 3 '/exit'"):
            self.assertEqual(self.refused(command), guard.EXIT_REFUSAL, command)


    def test_killing_a_claude_process_is_refused(self) -> None:
        for command in ("kill 4242", "kill -TERM 4242", "kill -9 5151 4242", "pkill claude", "kill $(pgrep -f claude)"):
            self.assertEqual(self.refused(command), guard.KILL_REFUSAL, command)

    def test_resuming_claude_on_a_config_dir_by_hand_is_refused(self) -> None:
        for command in ("CLAUDE_CONFIG_DIR=~/.claude1 claude --resume abc",
                        "cd /tmp && env CLAUDE_CONFIG_DIR=$HOME/.claude1 claude -r abc 'You were moved'",
                        "export CLAUDE_CONFIG_DIR=~/.claude1; claude --resume abc"):
            self.assertEqual(self.refused(command), guard.RESUME_REFUSAL, command)

    def test_the_skill_path_and_nearby_commands_pass(self) -> None:
        for command in (MOVE,
                        "tmux send-keys -t merge-gate 'run the gate' Enter",
                        "kill 5151",
                        "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py stop sid",
                        "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py resume sid --why 'continue now'",
                        "claude --resume abc",
                        "CLAUDE_CONFIG_DIR=~/.claude1 claude --version"):
            self.assertEqual(self.refused(command), "", command)


class SessionTest(unittest.TestCase):
    """Run as Claude Code runs it: only a showrunner's session is guarded."""
    state: Path = Path()

    @override
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.state = Path(root.name) / "notifier"
        doc = Path(root.name) / "production.md"
        _ = doc.write_text("# production\n", encoding="utf-8")
        timer = self.state / "showrunner-production"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text(
            f"TARGET=session:{SHOWRUNNER}\nCHECK=~/.claude/scripts/production/production_check.sh {doc}\n", encoding="utf-8")

    def run_hook(self, session_id: str, command: str, tool: str = "Bash") -> str:
        payload = {"session_id": session_id, "tool_name": tool, "tool_input": {"command": command}}
        done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              check=True, env={**os.environ, "NOTIFIER_STATE_DIR": str(self.state)})
        return done.stdout

    def test_a_showrunner_is_refused_with_the_skill_named(self) -> None:
        response = cast(dict[str, dict[str, str]], json.loads(self.run_hook(SHOWRUNNER, "tmux send-keys -t unit /exit Enter")))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("/showrunner:move", decision["permissionDecisionReason"])

    def test_the_skill_path_passes_in_a_showrunner(self) -> None:
        self.assertEqual(self.run_hook(SHOWRUNNER, MOVE), "")

    def test_another_session_is_not_guarded(self) -> None:
        self.assertEqual(self.run_hook("99999999-0000-0000-0000-000000000000", "tmux send-keys -t unit /exit Enter"), "")

    def test_another_tool_passes(self) -> None:
        self.assertEqual(self.run_hook(SHOWRUNNER, "tmux send-keys -t unit /exit Enter", tool="Read"), "")


if __name__ == "__main__":
    _ = unittest.main()
