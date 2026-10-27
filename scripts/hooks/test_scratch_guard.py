"""The scratch guard refuses a command that makes a new loose folder in /tmp, $TMPDIR, /tmp/claude or ~/rust."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, cast, override


HOOK = Path(__file__).with_name("pre-tool-use-scratch-guard.py")


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


class ScratchGuardTests(unittest.TestCase):
    home: Path = Path()
    temp: Path = Path()
    name: str = ""

    @override
    def setUp(self) -> None:
        base = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.home, self.temp = base / "home", base / "temp"
        (self.home / "rust" / "hana").mkdir(parents=True)
        self.temp.mkdir()
        # A name no real folder in /tmp holds, so the real /tmp root judges it as new.
        self.name = f"scratch-guard-{secrets.token_hex(6)}"

    def run_hook(self, command: str | list[str], cwd: Path | None = None, tool: str = "Bash",
                 temp: Path | None = None) -> str:
        payload = json.dumps({"tool_name": tool, "tool_input": {"command": command},
                              "cwd": str(cwd or self.home)})
        environment = {**os.environ, "HOME": str(self.home), "TMPDIR": str(temp or self.temp)}
        result = subprocess.run([str(HOOK)], input=payload, capture_output=True, text=True, check=False,
                                timeout=10, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_refused(self, command: str | list[str], folder: str, cwd: Path | None = None) -> None:
        output = self.run_hook(command, cwd)
        self.assertNotEqual(output, "", command)
        decision = cast(HookResponse, json.loads(output))["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(f"makes a new loose folder ({folder})", decision["permissionDecisionReason"])
        self.assertIn("scratch.py new --task", decision["permissionDecisionReason"])

    def assert_allowed(self, command: str | list[str], cwd: Path | None = None, tool: str = "Bash") -> None:
        self.assertEqual(self.run_hook(command, cwd, tool), "", command)

    def test_each_judged_command_making_a_loose_folder_is_refused(self) -> None:
        tmp, rust = os.path.realpath("/tmp"), self.home / "rust"
        for command, folder in [
            (f"mkdir -p /tmp/{self.name}/sub", f"{tmp}/{self.name}"),
            (f"mkdir -m 755 ~/rust/{self.name}", f"{rust}/{self.name}"),
            (f"cd /tmp && git clone git@github.com:natepiano/{self.name}.git", f"{tmp}/{self.name}"),
            (f"git -C ~/rust/hana worktree add -b x ../{self.name}", f"{rust}/{self.name}"),
            (f"cp -a ~/rust/hana /tmp/{self.name}", f"{tmp}/{self.name}"),
            (f"rsync -a --exclude target ~/rust/hana ~/rust/{self.name}", f"{rust}/{self.name}"),
            (f"cargo new --name x /tmp/claude/{self.name}", f"{os.path.realpath('/tmp/claude')}/{self.name}"),
            ("mktemp -d", f"{self.temp}/tmp.XXXXXXXXXX"),
            ("tar -xzf a.tgz -C ~/rust", f"{rust}/(archive entries)"),
        ]:
            self.assert_refused(command, folder)

    def test_a_copy_into_an_existing_root_names_the_folder_it_makes(self) -> None:
        self.assert_allowed("cp -r ~/rust/hana ~/rust/")  # hana exists: nothing new
        self.assert_refused(f"cp -R hana {self.temp}", f"{self.temp}/hana", cwd=self.home / "rust")

    def test_task_scratch_scratchpads_run_folders_and_worktrees_are_allowed(self) -> None:
        for command in [
            "mkdir -p /tmp/claude-1000/-home-x/session/scratchpad/copy",
            "mkdir -p /tmp/claude/unit/abc /tmp/claude/delegate/abc /tmp/claude/scratch/run-x",
            "git clone x ~/rust/repro/cargo-liner-C54-demo",
            "git -C ~/rust/hana worktree add ~/worktrees/hana-x",
            f"make_a_worktree hana ~/rust/{self.name}",
            "mkdir -p ~/rust/hana/target/sub",
            "mktemp -d -p /tmp/claude-1000/-home-x/session/scratchpad",
            "mktemp /tmp/file.XXXX",
            "cp ~/rust/hana/a /tmp/a",
            "tar -czf /tmp/out.tgz -C ~/rust hana",
            f"ssh mac 'mkdir /tmp/{self.name}'",
            f'mkdir "$WORK/{self.name}"',
        ]:
            self.assert_allowed(command)

    def test_words_that_are_data_are_never_judged(self) -> None:
        self.assert_allowed(f"cat <<'EOF' > notes.md\nmkdir /tmp/{self.name}\nEOF\necho done")
        self.assert_allowed(f"git commit -m 'mkdir /tmp/{self.name}'")
        self.assert_allowed(f"# mkdir /tmp/{self.name}\necho done")
        self.assert_refused(f"cat <<-EOF\n\tbody\n\tEOF\nmkdir /tmp/{self.name}", f"{os.path.realpath('/tmp')}/{self.name}")

    def test_codex_argv_and_other_tools(self) -> None:
        self.assert_refused(["bash", "-lc", f"mkdir /tmp/{self.name}"], f"{os.path.realpath('/tmp')}/{self.name}")
        self.assert_allowed(f"mkdir /tmp/{self.name}", tool="apply_patch")

    def test_tmpdir_inside_a_session_scratchpad_is_no_root(self) -> None:
        sandbox = self.temp / "claude-1000"
        sandbox.mkdir()
        self.assertEqual(self.run_hook("mktemp -d", temp=sandbox), "")


if __name__ == "__main__":
    _ = unittest.main()
