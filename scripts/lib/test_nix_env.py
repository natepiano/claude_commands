"""Environment proof and executable boundary, using only a stub direnv."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import cast, override
from unittest.mock import patch

from scripts.lib import copy_record, nix_env, scratch
from scripts.lib.nix_env_test_support import HELPER, ROOT, NixFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class GeneratedSourceTests(NixFixture):
    def test_generated_source_requires_a_readable_registered_record_with_true_field(self) -> None:
        with patch.dict(os.environ, {copy_record.ROOT_ENV: str(self.root / 'scratch')}):
            folder = scratch.new('gate:generated-source', 'proof')
            self.assertFalse(nix_env.generated_source(folder))
            _ = scratch.update_record(folder, lambda record: {
                **(record.fields if isinstance(record, copy_record.Registered) else {}), 'generated_source': True})
            self.assertTrue(nix_env.generated_source(folder))
            outside = self.root / 'outside'
            outside.mkdir()
            _ = (outside / copy_record.RECORD).write_bytes((folder / copy_record.RECORD).read_bytes())
            self.assertFalse(nix_env.generated_source(outside))
            nested = folder / 'nested'
            nested.mkdir()
            _ = (nested / copy_record.RECORD).write_bytes((folder / copy_record.RECORD).read_bytes())
            self.assertFalse(nix_env.generated_source(nested))
            with patch.object(copy_record, 'read_record', return_value=copy_record.UnreadableRecord('unreadable')):
                self.assertFalse(nix_env.generated_source(folder))
            with patch.object(copy_record, 'read_record', side_effect=OSError('read failed')):
                self.assertFalse(nix_env.generated_source(folder))
            _ = scratch.update_record(folder, lambda record: {
                **(record.fields if isinstance(record, copy_record.Registered) else {}), 'generated_source': 1})
            self.assertFalse(nix_env.generated_source(folder))
            (folder / copy_record.RECORD).unlink()
            self.assertFalse(nix_env.generated_source(folder))


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

    def test_passed_deadline_refuses_before_direnv_runs(self) -> None:
        import time
        from scripts.lib import operation_deadline

        _ = self.envrc(self.work)
        with (operation_deadline.within(time.monotonic() - 1),
              patch.object(subprocess, "run") as run):
            result = nix_env.ensure(self.work, self.env)
        self.assertEqual(result, nix_env.Refused(f"nix_env: {self.work}: deadline passed"))
        run.assert_not_called()
        self.assertEqual(self.direnv_calls(), [])

    def cli(self, *arguments: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts/lib/py"), str(HELPER), *arguments], cwd=self.work,
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
            [str(ROOT / "scripts/lib/py"), "-S", "-X", "importtime", str(HELPER), "ensure", str(self.work)],
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


class GitNixFixture(NixFixture):
    def git(self, directory: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
            cwd=directory, env=self.env, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def repository(self, directory: Path) -> Path:
        _ = self.envrc(directory)
        _ = self.git(directory, "init")
        _ = self.git(directory, "add", ".envrc")
        _ = self.git(directory, "commit", "-m", "fixture")
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


class RegisteredWorktreeTests(GitNixFixture):
    def worktree(self, repository: Path, directory: Path) -> Path:
        directory.parent.mkdir(parents=True, exist_ok=True)
        _ = self.git(repository, "worktree", "add", "--detach", str(directory), "HEAD")
        return directory

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


class RegisteredCopyTests(GitNixFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.env[copy_record.COPIES_ENV] = str(self.root / "copies")
        os.environ[copy_record.COPIES_ENV] = self.env[copy_record.COPIES_ENV]

    def clone(self, repository: Path) -> Path:
        checkout = copy_record.copies_root() / "pair/copy"
        checkout.parent.mkdir(parents=True)
        _ = self.git(self.root, "clone", "--shared", str(repository), str(checkout))
        return checkout

    def register(self, repository: Path, checkout: Path) -> None:
        commit = self.git(repository, "rev-parse", "HEAD")
        scratch.register_copy(
            checkout, task="gate:nix-env", owner_unit="efficiency", source_path=str(repository),
            source_commit=commit, copy_commit=commit, target_mode="shared", seed_source=str(repository),
            borrows_from=str(repository / ".git"))

    def test_registered_shared_copy_with_unchanged_envrc_is_approved_and_loads(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        search_path = list(sys.path)
        self.assertTrue(nix_env.trusted_envrc(checkout / ".envrc"))
        self.assertTrue(nix_env.registered_copy(checkout / ".envrc"))
        self.assertEqual(sys.path, search_path)
        # Approval is tied to the recorded commit, not the source's current file.
        _ = (repository / ".envrc").write_text("changed source\n", encoding="utf-8")
        outcome = nix_env.ensure(checkout, {**self.env, "DIRENV_DENY": "1"})
        self.assertIsInstance(outcome, nix_env.Loaded)
        if not isinstance(outcome, nix_env.Loaded):
            self.fail(str(outcome))
        self.assertEqual(outcome.env["IN_NIX_SHELL"], "impure")
        self.assertEqual(self.direnv_calls(), [
            {"args": ["allow", str(checkout / ".envrc")], "cwd": str(checkout)},
            {"args": ["export", "json"], "cwd": str(checkout)}])

        # The standalone helper must find scratch even when started in the copy.
        Path(self.env["DIRENV_APPROVAL"]).unlink()
        result = subprocess.run(
            [str(ROOT / "scripts/lib/py"), "-S", str(HELPER), "ensure", str(checkout)], cwd=checkout,
            env={**self.env, "DIRENV_DENY": "1"}, capture_output=True, text=True,
            timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = cast(dict[str, str], json.loads(result.stdout))
        self.assertEqual(environment["IN_NIX_SHELL"], "impure")

    def test_registered_copy_with_edited_envrc_requires_explicit_approval(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        _ = (checkout / ".envrc").write_text("use another flake\n", encoding="utf-8")
        self.assert_explicit_approval_required(checkout)

    def test_copy_without_record_requires_explicit_approval(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.assert_explicit_approval_required(checkout)

    def test_git_repository_override_cannot_approve_a_foreign_commit(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        other = self.repository(self.root / "other-repository")
        changed_envrc = "use another flake\n"
        _ = (other / ".envrc").write_text(changed_envrc, encoding="utf-8")
        _ = self.git(other, "add", ".envrc")
        _ = self.git(other, "commit", "-m", "foreign envrc")
        foreign_commit = self.git(other, "rev-parse", "HEAD")
        _ = (checkout / ".envrc").write_text(changed_envrc, encoding="utf-8")
        _ = scratch.update_record(checkout, lambda record: {
            **(record.fields if isinstance(record, copy_record.Registered) else {}),
            "source_commit": foreign_commit})

        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertFalse(nix_env.registered_copy(checkout / ".envrc"))
            self.assertFalse(nix_env.trusted_envrc(checkout / ".envrc"))
            self.assert_explicit_approval_required(checkout)

    def test_registered_copy_requires_regular_envrc_even_when_symlink_bytes_match(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        envrc = checkout / ".envrc"
        self.assertTrue(nix_env.trusted_envrc(envrc))
        target = self.envrc(self.root / "outside")
        envrc.unlink()
        envrc.symlink_to(target)
        self.assertFalse(nix_env.registered_copy(envrc))
        self.assert_explicit_approval_required(checkout)
        envrc.unlink()
        _ = envrc.write_bytes(target.read_bytes())
        self.assertTrue(nix_env.trusted_envrc(envrc))

    def test_copy_from_untrusted_source_requires_explicit_approval(self) -> None:
        repository = self.repository(self.root / "outside/repo")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        self.assert_explicit_approval_required(checkout)

    def test_source_commit_without_envrc_requires_explicit_approval(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        _ = self.git(repository, "rm", ".envrc")
        _ = self.git(repository, "commit", "-m", "without envrc")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        _ = self.envrc(checkout)
        self.assert_explicit_approval_required(checkout)

    def test_copied_record_outside_copies_root_requires_explicit_approval(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.register(repository, checkout)
        outside = self.root / "outside-copy"
        _ = self.envrc(outside)
        _ = (outside / copy_record.RECORD).write_bytes((checkout / copy_record.RECORD).read_bytes())
        self.assert_explicit_approval_required(outside)


class RegisteredCloneTests(GitNixFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        locations = {copy_record.ROOT_ENV: str(self.root / "scratch"),
                     copy_record.COPIES_ENV: str(self.root / "copies")}
        self.env.update(locations)
        os.environ.update(locations)

    def clone(self, repository: Path, task_folder: Path | None = None) -> Path:
        folder = task_folder if task_folder is not None else copy_record.root() / "task"
        folder.mkdir(parents=True)
        checkout = folder / "checkout"
        _ = self.git(self.root, "clone", "--shared", str(repository), str(checkout))
        fields = {"task": "gate:nix-env", "test_hana_clone": True,
                  "source_path": str(repository), "source_commit": self.git(checkout, "rev-parse", "HEAD")}
        _ = (folder / copy_record.RECORD).write_text(json.dumps(fields), encoding="utf-8")
        return checkout

    def assert_clone_refused(self, checkout: Path) -> None:
        self.assertFalse(nix_env.registered_copy(checkout / ".envrc"))
        self.assertFalse(nix_env.trusted_envrc(checkout / ".envrc"))
        self.assert_explicit_approval_required(checkout)

    def test_registered_clone_with_unchanged_envrc_is_approved_and_loads(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        self.assertEqual(copy_record.record_path(checkout), checkout.parent / copy_record.RECORD)
        self.assertTrue(nix_env.registered_copy(checkout / ".envrc"))
        self.assertTrue(nix_env.trusted_envrc(checkout / ".envrc"))
        _ = (repository / ".envrc").write_text("changed source\n", encoding="utf-8")
        self.assertIsInstance(nix_env.ensure(checkout, {**self.env, "DIRENV_DENY": "1"}), nix_env.Loaded)
        self.assertEqual(self.direnv_calls(), [
            {"args": ["allow", str(checkout / ".envrc")], "cwd": str(checkout)},
            {"args": ["export", "json"], "cwd": str(checkout)}])

    def test_task_clone_parent_with_matching_envrc_requires_explicit_approval(self) -> None:
        repository = self.repository(self.home / "rust/repo")
        checkout = self.clone(repository)
        parent = checkout.parent
        _ = (parent / ".envrc").write_bytes((checkout / ".envrc").read_bytes())
        self.assertIsNone(copy_record.record_path(parent))
        self.assertFalse(nix_env.registered_copy(parent / ".envrc"))
        self.assertFalse(nix_env.trusted_envrc(parent / ".envrc"))
        self.assert_explicit_approval_required(parent)

    def test_registered_clone_with_edited_envrc_requires_explicit_approval(self) -> None:
        checkout = self.clone(self.repository(self.home / "rust/repo"))
        _ = (checkout / ".envrc").write_text("use another flake\n", encoding="utf-8")
        self.assert_clone_refused(checkout)

    def test_clone_without_source_commit_requires_explicit_approval(self) -> None:
        checkout = self.clone(self.repository(self.home / "rust/repo"))
        path = checkout.parent / copy_record.RECORD
        fields = cast(dict[str, object], json.loads(path.read_text()))
        del fields["source_commit"]
        _ = path.write_text(json.dumps(fields), encoding="utf-8")
        self.assert_clone_refused(checkout)

    def test_checkout_outside_scratch_root_requires_explicit_approval(self) -> None:
        checkout = self.clone(self.repository(self.home / "rust/repo"), self.root / "outside/task")
        self.assertIsNone(copy_record.record_path(checkout))
        self.assert_clone_refused(checkout)

    def test_clone_without_test_hana_clone_requires_explicit_approval(self) -> None:
        checkout = self.clone(self.repository(self.home / "rust/repo"))
        path = checkout.parent / copy_record.RECORD
        fields = cast(dict[str, object], json.loads(path.read_text()))
        del fields["test_hana_clone"]
        _ = path.write_text(json.dumps(fields), encoding="utf-8")
        self.assertIsNone(copy_record.record_path(checkout))
        self.assert_clone_refused(checkout)

    def test_clone_from_untrusted_source_requires_explicit_approval(self) -> None:
        checkout = self.clone(self.repository(self.root / "outside/repo"))
        self.assert_clone_refused(checkout)

    def test_clone_under_symlink_scratch_root_is_approved(self) -> None:
        physical = self.root / "physical-scratch"
        physical.mkdir()
        copy_record.root().symlink_to(physical, target_is_directory=True)
        checkout = self.clone(self.repository(self.home / "rust/repo"))
        self.assertEqual(copy_record.record_path(checkout), checkout.parent.resolve() / copy_record.RECORD)
        self.assertTrue(nix_env.registered_copy(checkout / ".envrc"))
        self.assertTrue(nix_env.trusted_envrc(checkout / ".envrc"))


class NixConfigTests(NixFixture):
    def test_direnv_commands_read_the_timeout_setting_at_use(self) -> None:
        _ = self.envrc(self.work)
        settings = Path(self.env["CONF_DIR"]) / "nix_env.conf"
        original = settings.read_text()
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("direnv", 600)) as run:
            self.assertEqual(nix_env.ensure(self.work), nix_env.Refused(f"nix_env: {self.work}: direnv timed out after 600 s"))
            self.assertEqual(run.call_args.kwargs["timeout"], 600)
            _ = settings.write_text(original.replace("command_timeout_seconds=600", "command_timeout_seconds=2"))
            self.assertEqual(nix_env.ensure(self.work), nix_env.Refused(f"nix_env: {self.work}: direnv timed out after 2 s"))
            self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_config_keeps_the_existing_values(self) -> None:
        from scripts.lib import conf

        settings = conf.read(Path(__file__).resolve().parents[2] / "config/nix_env.conf")
        for key, expected in {'trusted_home_folders': 'rust worktrees', 'trusted_home_prefix': '.claude', 'heal_state_directory': '~/.local/state/nix-heal', 'session_directory_glob': '.claude*/sessions'}.items():
            with self.subTest(key=key):
                self.assertEqual(settings.text(key), expected)
        for key, expected in {'command_timeout_seconds': 600.0, 'restart_interval_seconds': 3600.0, 'relaunch_launch_seconds': 90.0, 'relaunch_confirmation_seconds': 30.0, 'alert_timeout_seconds': 30.0}.items():
            with self.subTest(key=key):
                self.assertEqual(settings.number(key), expected)
