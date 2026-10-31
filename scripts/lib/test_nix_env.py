"""Environment proof and executable boundary, using only a stub direnv."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import cast
from unittest.mock import patch

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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
                              env={**self.env, **(extra or {})}, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)

    def test_cli_ensure_json_and_refusal_exit(self) -> None:
        _ = self.envrc(self.work)
        self.env["NIX_ENV_ENVRC"] = "caller provenance remains unchanged by ensure"
        allowed = self.cli("ensure", str(self.work))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        value = cast(dict[str, str], json.loads(allowed.stdout))
        self.assertEqual(value["IN_NIX_SHELL"], "impure")
        self.assertEqual(value["KEEP"], "caller")
        self.assertEqual(value["NIX_ENV_ENVRC"], self.env["NIX_ENV_ENVRC"])
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

    def test_cli_exec_names_the_resolved_loaded_envrc(self) -> None:
        target = self.envrc(self.home / "rust/source")
        (self.work / ".envrc").symlink_to(target)
        self.env["NIX_ENV_ENVRC"] = "/stale/.envrc"
        self.env["DIRENV_DIFF"] = json.dumps({"IN_NIX_SHELL": "pure", "NIX_ENV_ENVRC": "/exported/.envrc"})
        program = self.bin / "provenance"
        self.script(program, 'import os\nprint(os.environ["NIX_ENV_ENVRC"])\n')
        result = self.cli("exec", str(program))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(target.resolve()) + "\n")
        self.assertEqual(sum(call["args"] == ["export", "json"] for call in self.direnv_calls()), 1)

    def test_cli_exec_clears_inherited_envrc_when_none_applies(self) -> None:
        self.env["NIX_ENV_ENVRC"] = "/previous/worktree/.envrc"
        program = self.bin / "environment"
        self.script(program, 'import json, os\nprint(json.dumps(dict(os.environ)))\n')
        result = self.cli("exec", str(program))
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = cast(dict[str, str], json.loads(result.stdout))
        self.assertNotIn("NIX_ENV_ENVRC", environment)
        self.assertEqual(environment["KEEP"], "caller")
        self.assertEqual(environment["REMOVE"], "old")
        self.assertEqual(self.direnv_calls(), [])

    def test_no_envrc_cli_import_budget(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", "-X", "importtime", str(HELPER), "ensure", str(self.work)],
            cwd=self.work, env=self.env, capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = cast(dict[str, str], json.loads(result.stdout))
        self.assertEqual({key: environment[key] for key in self.env}, self.env)
        imports = {line.rpartition("|")[2].strip() for line in result.stderr.splitlines()
                   if line.startswith("import time:")}
        self.assertTrue(imports, result.stderr)
        self.assertEqual(imports.intersection({"dataclasses", "inspect", "subprocess", "shlex"}), set())
        self.assertEqual(self.direnv_calls(), [])


class RegisteredWorktreeTests(NixFixture):
    def git(self, directory: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
            cwd=directory, env=self.env, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def repository(self, directory: Path) -> Path:
        _ = self.envrc(directory)
        _ = self.git(directory, "init")
        _ = self.git(directory, "add", ".envrc")
        _ = self.git(directory, "commit", "-m", "fixture")
        return directory

    def worktree(self, repository: Path, directory: Path) -> Path:
        directory.parent.mkdir(parents=True, exist_ok=True)
        _ = self.git(repository, "worktree", "add", "--detach", str(directory), "HEAD")
        return directory

    def assert_explicit_approval_required(self, directory: Path) -> None:
        before = len(self.direnv_calls())
        outcome = nix_env.ensure(directory, {**self.env, "DIRENV_DENY": "1"})
        self.assertIsInstance(outcome, nix_env.Refused)
        if not isinstance(outcome, nix_env.Refused):
            self.fail(str(outcome))
        self.assertIn(shlex.join(["direnv", "allow", str(directory / ".envrc")]), outcome.reason)
        self.assertEqual(self.direnv_calls()[before:], [
            {"args": ["export", "json"], "cwd": str(directory)}])
        self.assertFalse(Path(self.env["DIRENV_APPROVAL"]).exists())

    def test_trusted_repository_registered_snapshot_is_automatically_allowed(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.worktree(repository, self.root / "tmp/session/style-sync-lint-x/checkout")
        outcome = nix_env.ensure(checkout, {**self.env, "DIRENV_DENY": "1"})
        self.assertIsInstance(outcome, nix_env.Loaded)
        self.assertEqual(self.direnv_calls(), [
            {"args": ["allow", str(checkout / ".envrc")], "cwd": str(checkout)},
            {"args": ["export", "json"], "cwd": str(checkout)}])
        with patch.object(subprocess, "run", side_effect=AssertionError("trust checks must only read files")):
            self.assertTrue(nix_env.registered_worktree(checkout))

    def test_clone_of_trusted_repository_still_requires_explicit_approval(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        clone = self.root / "tmp/clone"
        clone.parent.mkdir(parents=True)
        _ = self.git(self.root, "clone", str(repository), str(clone))
        self.assert_explicit_approval_required(clone)

    def test_forged_git_file_cannot_borrow_a_registered_entry(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.worktree(repository, self.root / "tmp/real")
        forged = self.root / "tmp/forged"
        _ = self.envrc(forged)
        _ = (forged / ".git").write_text((checkout / ".git").read_text(encoding="utf-8"), encoding="utf-8")
        self.assert_explicit_approval_required(forged)

    def test_worktree_of_untrusted_repository_still_requires_explicit_approval(self) -> None:
        repository = self.repository(self.root / "outside/repo")
        checkout = self.worktree(repository, self.root / "tmp/wt")
        self.assert_explicit_approval_required(checkout)

    def test_relative_registration_pointers_are_accepted(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.worktree(repository, self.root / "tmp/wt")
        entry = Path((checkout / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
        _ = (checkout / ".git").write_text("gitdir: " + os.path.relpath(entry, checkout) + "\n", encoding="utf-8")
        _ = (entry / "gitdir").write_text(os.path.relpath(checkout / ".git", entry) + "\n", encoding="utf-8")
        self.assertTrue(nix_env.registered_worktree(checkout))

    def test_missing_or_malformed_registration_files_fail_closed(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.worktree(repository, self.root / "tmp/wt")
        git_file = checkout / ".git"
        entry = Path(git_file.read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
        for path in (git_file, entry / "commondir", entry / "gitdir"):
            original = path.read_text(encoding="utf-8")
            for replacement in (None, "", "missing\n", original + "extra line\n", "\0\n"):
                with self.subTest(path=path.name, replacement=replacement):
                    try:
                        if replacement is None:
                            path.unlink()
                        else:
                            _ = path.write_text(replacement, encoding="utf-8")
                        self.assertFalse(nix_env.registered_worktree(checkout))
                    finally:
                        _ = path.write_text(original, encoding="utf-8")

    def test_symlink_git_file_cannot_supply_registration(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.worktree(repository, self.root / "tmp/wt")
        git_file = checkout / ".git"
        saved = checkout / "saved-git-file"
        _ = git_file.rename(saved)
        git_file.symlink_to(saved)
        self.assert_explicit_approval_required(checkout)

    def test_registered_worktree_cannot_approve_a_symlink_to_an_unrelated_envrc(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.worktree(repository, self.root / "tmp/wt")
        outside = self.envrc(self.root / "outside")
        (checkout / ".envrc").unlink()
        (checkout / ".envrc").symlink_to(outside)
        self.assert_explicit_approval_required(checkout)
