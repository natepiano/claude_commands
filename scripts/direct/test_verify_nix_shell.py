#!/usr/bin/env python3
"""Verify re-exec uses the real helper with fake direnv and no build tools."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TypedDict, cast, override
import unittest

from scripts.lib.nix_env_test_support import NixFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


VERIFY = Path(__file__).with_name("verify.sh")
SCRIPTS = VERIFY.parent.parent
NOTICE = "verify.sh: not in the nix shell; re-running under nix_env.py exec\n"
LOOP_REFUSAL = "verify.sh: nix_env.py re-exec still has no nix shell (IN_NIX_SHELL unset)\n"

PYTHON_SHIM = '''import json, os, sys
from pathlib import Path
with Path(os.environ["VERIFY_PYTHON_LOG"]).open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''

TOOL_SETUP = '''"$CLAUDE_PYTHON" - "$@" <<'PYTHON'
import json, os, sys
from pathlib import Path
Path(os.environ["VERIFY_SETUP_LOG"]).write_text(json.dumps({
    "args": sys.argv[1:], "cwd": os.getcwd(),
    "nix_shell": os.environ.get("IN_NIX_SHELL", ""),
    "ldflags": os.environ.get("NIX_LDFLAGS", ""),
    "loop_marker": os.environ.get("VERIFY_NIX_ENV_REEXEC", ""),
}))
print("verification reached")
PYTHON
exit "$VERIFY_FIXTURE_EXIT"
'''


class VerificationStart(TypedDict):
    args: list[str]
    cwd: str
    nix_shell: str
    ldflags: str
    loop_marker: str


class VerifyNixShellTests(NixFixture):
    verify_script: Path = Path()
    setup_log: Path = Path()
    python_log: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        scripts = self.root / "fixture scripts"
        direct = scripts / "direct"
        direct.mkdir(parents=True)
        self.verify_script = direct / "verify.sh"
        _ = shutil.copy2(VERIFY, self.verify_script)
        self.verify_script.chmod(0o755)
        self.script(scripts / "lib/py", PYTHON_SHIM)
        (scripts / "lib/nix_env.py").symlink_to(SCRIPTS / "lib/nix_env.py")
        guards = scripts / "build_hold"
        guards.mkdir()
        _ = shutil.copy2(SCRIPTS / "build_hold/override_guard.sh", guards / "override_guard.sh")
        lint = self.home / ".claude/scripts/lint"
        lint.mkdir(parents=True)
        _ = (lint / "invoke.sh").write_text(TOOL_SETUP, encoding="utf-8")
        self.setup_log = self.root / "verification.json"
        self.python_log = self.root / "python.jsonl"
        self.env.update({"CLAUDE_PYTHON": sys.executable,
                         "VERIFY_SETUP_LOG": str(self.setup_log),
                         "VERIFY_PYTHON_LOG": str(self.python_log),
                         "VERIFY_FIXTURE_EXIT": "37"})
        # Any accidental tool use must hit a stub, even with a broken setup.
        for name in ("cargo", "nix", "herdr", "claude", "codex", "shutdown.py", "taplo"):
            self.script(self.bin / name, 'raise SystemExit("unexpected tool invocation")\n')

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.verify_script), *arguments], cwd=self.work,
                              env=self.env, text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def verification_start(self) -> VerificationStart:
        return cast(VerificationStart, json.loads(self.setup_log.read_text(encoding="utf-8")))

    def helper_calls(self) -> list[list[str]]:
        if not self.python_log.exists():
            return []
        calls = [cast(list[str], json.loads(line))
                 for line in self.python_log.read_text(encoding="utf-8").splitlines()]
        return [call for call in calls if call and call[0].endswith("/nix_env.py")]

    def test_missing_shell_reexec_preserves_cwd_arguments_and_exit_status(self) -> None:
        _ = self.envrc(self.work)
        arguments = ("test", "package with spaces", "", "--filter", "a * [b] $literal", "tail")
        result = self.invoke(*arguments)
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, NOTICE)
        self.assertEqual(result.stdout, "verification reached\n")
        start = self.verification_start()
        self.assertEqual(start["args"], list(arguments))
        self.assertEqual(start["cwd"], str(self.work))
        self.assertEqual(start["nix_shell"], "impure")
        self.assertEqual(start["ldflags"], "-Lfixture")
        self.assertEqual(start["loop_marker"], "")
        self.assertEqual([call[1] for call in self.helper_calls()], ["ensure", "exec"])
        self.assertEqual(self.helper_calls()[1][2:], [str(self.verify_script), *arguments])

    def test_nearest_ancestor_envrc_is_found_by_the_helper(self) -> None:
        envrc = self.envrc(self.work.parent)
        result = self.invoke("check", "sample")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, NOTICE)
        self.assertEqual(self.direnv_calls()[0]["args"], ["allow", str(envrc)])
        self.assertEqual(self.verification_start()["nix_shell"], "impure")

    def test_refusal_stops_before_tool_setup_with_only_the_helper_line(self) -> None:
        _ = self.envrc(self.work)
        self.env["DIRENV_FAIL"] = "1"
        result = self.invoke("doc")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, f"nix_env: {self.work}: last failure detail\n")
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.setup_log.exists())
        self.assertEqual([call[1] for call in self.helper_calls()], ["ensure"])

    def test_no_envrc_has_no_notice_no_reexec_and_no_direnv_call(self) -> None:
        result = self.invoke("check", "", "sample with spaces")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.direnv_calls(), [])
        self.assertEqual([call[1] for call in self.helper_calls()], ["ensure"])
        self.assertEqual(self.verification_start()["nix_shell"], "")
        self.assertEqual(self.verification_start()["args"], ["check", "", "sample with spaces"])

    def test_existing_shell_is_silent_and_never_calls_the_helper(self) -> None:
        _ = self.envrc(self.work)
        self.env["IN_NIX_SHELL"] = "pure"
        self.env["DIRENV_FAIL"] = "1"
        result = self.invoke("doc")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.direnv_calls(), [])
        self.assertEqual(self.helper_calls(), [])
        self.assertEqual(self.verification_start()["nix_shell"], "pure")

    def test_reexec_losing_proof_refuses_without_another_exec(self) -> None:
        _ = self.envrc(self.work)
        startup = self.root / "drop-proof.sh"
        _ = startup.write_text("unset IN_NIX_SHELL\n", encoding="utf-8")
        self.env["DIRENV_DIFF"] = json.dumps({"IN_NIX_SHELL": "impure", "BASH_ENV": str(startup)})
        result = self.invoke("doc")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, NOTICE + LOOP_REFUSAL)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.setup_log.exists())
        self.assertEqual([call[1] for call in self.helper_calls()], ["ensure", "exec"])
        self.assertEqual(sum(call["args"] == ["export", "json"] for call in self.direnv_calls()), 2)


if __name__ == "__main__":
    _ = unittest.main()
