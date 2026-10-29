"""Environment proof and executable boundary, using only a stub direnv."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import nix_env
from scripts.lib.nix_env_test_support import HELPER, NixFixture


class EnsureTests(NixFixture):
    def test_empty_export_preserves_loaded_environment_and_still_requires_nix_proof(self) -> None:
        envrc = self.envrc(self.work)
        for output in ("", "\n"):
            with self.subTest(output=output):
                base = {**self.env, "DIRENV_DIFF": output, "IN_NIX_SHELL": "impure"}
                result = nix_env.ensure(self.work, base)
                self.assertEqual(result, nix_env.Loaded(base, envrc))
                del base["IN_NIX_SHELL"]
                self.assertEqual(nix_env.ensure(self.work, base), nix_env.Refused(
                    f"nix_env: {self.work}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"))

    def test_nearest_parent_envrc_and_export_diff(self) -> None:
        parent = self.envrc(self.home / "worktrees")
        selected = self.envrc(self.work)
        nested = self.work / "src/deep"
        nested.mkdir(parents=True)
        base = {**self.env, "DIRENV_DIFF": json.dumps({"IN_NIX_SHELL": "pure", "REMOVE": None, "NEW": "loaded"})}
        original = dict(base)
        result = nix_env.ensure(nested, base)
        self.assertIsInstance(result, nix_env.Loaded)
        if not isinstance(result, nix_env.Loaded):
            self.fail(str(result))
        self.assertEqual(result.envrc, selected)
        self.assertNotEqual(result.envrc, parent)
        self.assertEqual(result.env["NEW"], "loaded")
        self.assertEqual(result.env["KEEP"], "caller")
        self.assertNotIn("REMOVE", result.env)
        self.assertEqual(base, original)
        self.assertEqual(self.direnv_calls(), [
            {"args": ["allow", str(selected)], "cwd": str(nested)},
            {"args": ["export", "json"], "cwd": str(nested)}])

    def test_approval_is_limited_to_trusted_roots(self) -> None:
        for name in ("rust/project", "worktrees/project", ".claude/scripts", ".claude3/project", "elsewhere"):
            with self.subTest(root=name):
                directory = self.home / name
                path = self.envrc(directory)
                before = len(self.direnv_calls())
                result = nix_env.ensure(directory, self.env)
                self.assertIsInstance(result, nix_env.Loaded)
                calls = self.direnv_calls()[before:]
                self.assertEqual(calls[0]["args"], ["export", "json"] if name == "elsewhere" else ["allow", str(path)])
                self.assertEqual(len(calls), 1 if name == "elsewhere" else 2)

    def test_untrusted_blocked_file_names_explicit_approval_command(self) -> None:
        directory = self.home / "untrusted space"
        path = self.envrc(directory)
        result = nix_env.ensure(directory, {**self.env, "DIRENV_DENY": "1"})
        self.assertIsInstance(result, nix_env.Refused)
        if not isinstance(result, nix_env.Refused):
            self.fail(str(result))
        self.assertIn(shlex.join(["direnv", "allow", str(path)]), result.reason)
        self.assertEqual(len(self.direnv_calls()), 1)
        self.assertFalse(Path(self.env["DIRENV_APPROVAL"]).exists())

    def test_trusted_symlink_cannot_approve_an_outside_envrc(self) -> None:
        outside = self.envrc(self.root / "outside")
        (self.work / ".envrc").symlink_to(outside)
        result = nix_env.ensure(self.work, {**self.env, "DIRENV_DENY": "1"})
        self.assertIsInstance(result, nix_env.Refused)
        self.assertEqual(self.direnv_calls()[0]["args"], ["export", "json"])

    def test_missing_nix_proof_refuses_with_contract_line(self) -> None:
        _ = self.envrc(self.work)
        result = nix_env.ensure(self.work, {**self.env, "DIRENV_DIFF": "{}"})
        self.assertEqual(result, nix_env.Refused(f"nix_env: {self.work}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"))

    def test_direnv_failure_uses_last_stderr_line(self) -> None:
        _ = self.envrc(self.work)
        result = nix_env.ensure(self.work, {**self.env, "DIRENV_FAIL": "1"})
        self.assertEqual(result, nix_env.Refused(f"nix_env: {self.work}: last failure detail"))

    def test_timeout_is_a_refusal_with_last_stderr_line(self) -> None:
        _ = self.envrc(self.work)
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("direnv", 600, stderr=b"progress\ntimed out detail\n")) as run:
            result = nix_env.ensure(self.work, self.env)
        self.assertEqual(result, nix_env.Refused(f"nix_env: {self.work}: timed out detail"))
        self.assertEqual(run.call_args.kwargs["timeout"], 600)

    def test_no_envrc_preserves_environment_without_direnv(self) -> None:
        result = nix_env.ensure(self.work, self.env)
        self.assertIsInstance(result, nix_env.NoEnvrc)
        if not isinstance(result, nix_env.NoEnvrc):
            self.fail(str(result))
        self.assertEqual(result.env, self.env)
        self.assertEqual(self.direnv_calls(), [])

    def cli(self, *arguments: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HELPER), *arguments], cwd=self.work,
                              env={**self.env, **(extra or {})}, capture_output=True, text=True, timeout=10, check=False)

    def test_cli_ensure_json_and_refusal_exit(self) -> None:
        _ = self.envrc(self.work)
        allowed = self.cli("ensure", str(self.work))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        value = cast(dict[str, str], json.loads(allowed.stdout))
        self.assertEqual(value["IN_NIX_SHELL"], "impure")
        self.assertEqual(value["KEEP"], "caller")
        refused = self.cli("ensure", str(self.work), extra={"DIRENV_DIFF": "{}"})
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, "")
        self.assertEqual(len(refused.stderr.splitlines()), 1)
        self.assertTrue(refused.stderr.startswith("nix_env: "))

    def test_cli_exec_rejects_relative_program_and_never_runs_on_refusal(self) -> None:
        _ = self.envrc(self.work)
        marker = self.root / "started"
        program = self.bin / "target"
        self.script(program, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        relative = self.cli("exec", "target")
        self.assertEqual(relative.returncode, 2)
        self.assertEqual(len(relative.stderr.splitlines()), 1)
        refused = self.cli("exec", str(program), extra={"DIRENV_DIFF": "{}"})
        self.assertEqual(refused.returncode, 1)
        self.assertFalse(marker.exists())

    def test_cli_exec_passes_loaded_environment_and_arguments(self) -> None:
        _ = self.envrc(self.work)
        program = self.bin / "target"
        self.script(program, 'import json, os, sys\nprint(json.dumps([os.environ["IN_NIX_SHELL"], sys.argv[1:], os.getcwd()]))\n')
        result = self.cli("exec", str(program), "argument with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["impure", ["argument with spaces"], str(self.work)])
