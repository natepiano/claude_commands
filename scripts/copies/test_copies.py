"""Real-Git acceptance tests for the standing gate checkout contract."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from threading import Event
import unittest
from typing import TextIO, cast, override
from unittest import mock

from scripts.copies import copies
from scripts.lib import copy_record, scratch
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli


class WaitNotice(io.StringIO):
    """Signal when the third caller has announced its blocking acquisition."""

    def __init__(self) -> None:
        super().__init__()
        self.noticed: Event = Event()

    @override
    def write(self, text: str) -> int:
        count = super().write(text)
        if "wait" in self.getvalue().lower():
            self.noticed.set()
        return count


class StandingTests(unittest.TestCase):
    base: Path = Path()
    source: Path = Path()
    gates: Path = Path()
    configuration: Path = Path()
    settings: dict[str, str] = {}
    head: str = ""
    published: str = ""

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        home = self.base / "home"
        home.mkdir()
        self.configuration = self.base / "copies.conf"
        self.settings = {"root": str(self.base / "configured-gates"),
                         "wait_seconds": "0.01", "git_timeout_seconds": "10",
                         "guard_timeout_seconds": "10"}
        self.write_config(self.settings)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_") and key != "UNIT_RUN_DIR"}
        environment.update({
            "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Gate Tests",
            "GIT_AUTHOR_EMAIL": "gate-tests@example.invalid", "GIT_COMMITTER_NAME": "Gate Tests",
            "GIT_COMMITTER_EMAIL": "gate-tests@example.invalid",
            "SCRATCH_ROOT": str(self.base / "scratch"),
            "SCRATCH_HOLDS": str(self.base / "holds.json"),
            "SCRATCH_COPIES_ROOT": str(self.base / "old-copies"),
            "COPIES_CONFIG": str(self.configuration),
        })
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        seed = ImmutableGitSeed.create(
            self.base, "main", "Gate Tests", "gate-tests@example.invalid",
            {"tracked.txt": "published\n", "stable.txt": "unchanged\n",
             ".gitignore": "target/\nignored.txt\n"},
        )
        self.source = seed.checkout
        self.gates = Path(self.settings["root"]) / self.source.name
        self.published = self.git(self.source, "rev-parse", "HEAD")
        _ = (self.source / "tracked.txt").write_text("source HEAD\n")
        _ = self.git(self.source, "commit", "-qam", "unpushed source commit")
        self.head = self.git(self.source, "rev-parse", "HEAD")

    def write_config(self, settings: Mapping[str, str]) -> None:
        _ = self.configuration.write_text("[copies]\n" + "".join(f"{key}={value}\n" for key, value in settings.items()))

    def git(self, path: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(copies, lambda: copies.main(list(arguments)), list(arguments),
                          dict(os.environ), self.base)

    def run_cli(self, *options: str, command: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        return self.cli("run", "--owner", "verify-unit", "--repo", str(self.source),
                        "--commit", "HEAD", *options, "--", *(command or (sys.executable, "-c", "pass")))

    def record(self, tree: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads((tree / ".gate.json").read_text()))

    def snapshot(self, path: Path) -> dict[str, bytes]:
        return {str(entry.relative_to(path)): entry.read_bytes()
                for entry in path.rglob("*") if entry.is_file()}

    def incoming(self) -> str:
        _ = self.git(self.source, "switch", "-qc", "incoming")
        _ = (self.source / "incoming.txt").write_text("merged content\n")
        _ = self.git(self.source, "add", "incoming.txt")
        _ = self.git(self.source, "commit", "-qm", "incoming feature")
        commit = self.git(self.source, "rev-parse", "HEAD")
        _ = self.git(self.source, "switch", "-q", "main")
        return commit

    def python_run(self, program: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", program, *arguments],
                              capture_output=True, text=True, timeout=10)

    def test_first_use_owns_git_and_never_changes_source_branch(self) -> None:
        _ = self.git(self.source, "status", "--porcelain")
        before = self.snapshot(self.source)
        with copies.standing("verify-unit", self.source, "HEAD") as tree:
            self.assertEqual(tree, self.gates / "verify-unit-gate")
            self.assertTrue((tree / ".git").is_dir())
            self.assertEqual(self.git(tree, "rev-parse", "--absolute-git-dir"), str(tree / ".git"))
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            self.assertEqual(self.git(tree, "branch", "--show-current"), "")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertEqual(self.snapshot(self.source), before)
        self.assertEqual(self.git(self.source, "rev-parse", "main"), self.head)

    def test_new_commit_reuses_tree_keeps_unchanged_mtime_and_target(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as tree:
            stable = tree / "stable.txt"
            os.utime(stable, ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
            # Keep this an unchanged tracked file after arranging its sentinel timestamp.
            _ = self.git(tree, "update-index", "--refresh")
            unchanged_mtime = stable.stat().st_mtime_ns
            cache = tree / "target/debug/compiled"
            cache.parent.mkdir(parents=True, exist_ok=True)
            _ = cache.write_bytes(b"warm build output")
            _ = (tree / "leftover.txt").write_text("remove untracked work\n")
        _ = (self.source / "tracked.txt").write_text("new commit\n")
        _ = self.git(self.source, "commit", "-qam", "next source commit")
        next_commit = self.git(self.source, "rev-parse", "HEAD")
        with copies.standing("verify-unit", self.source, "HEAD") as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(self.git(reused, "rev-parse", "HEAD"), next_commit)
            self.assertEqual(stable.stat().st_mtime_ns, unchanged_mtime)
            self.assertEqual(cache.read_bytes(), b"warm build output")
            self.assertFalse((tree / "leftover.txt").exists())
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertIn(scratch.CACHE_TAG_SIGNATURE, (tree / "target/CACHEDIR.TAG").read_text())
        self.assertTrue((tree / "target" / scratch.USE_STAMP).is_file())

    def test_held_first_tree_sends_second_caller_to_second_tree(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as first:
            with copies.standing("verify-unit", self.source, self.head) as second:
                self.assertEqual(second, first.with_name("verify-unit-gate-2"))
                with scratch.busy_locks(first, create=False) as idle:
                    self.assertFalse(idle)
                with scratch.busy_locks(second, create=False) as idle:
                    self.assertFalse(idle)
            with scratch.busy_locks(second, create=False) as idle:
                self.assertTrue(idle)

    def test_third_caller_waits_for_first_without_creating_a_third_tree(self) -> None:
        notice = WaitNotice()

        def third_use() -> Path:
            with copies.standing("verify-unit", self.source, self.head) as tree:
                return tree

        with ThreadPoolExecutor(max_workers=1) as executor:
            with ExitStack() as primary:
                first = primary.enter_context(copies.standing("verify-unit", self.source, self.head))
                with copies.standing("verify-unit", self.source, self.head):
                    with redirect_stderr(notice), redirect_stdout(notice):
                        third = executor.submit(third_use)
                        self.assertTrue(notice.noticed.wait(timeout=10), "third caller never announces its wait")
                        self.assertFalse(third.done())
                        self.assertIn(str(first), notice.getvalue())
                        self.assertIn(str(os.getpid()), notice.getvalue())
                    primary.close()
                    self.assertEqual(third.result(timeout=10), first)
        self.assertEqual(len([line for line in notice.getvalue().splitlines() if line.strip()]), 1)
        self.assertEqual(sorted(path.name for path in self.gates.iterdir()),
                         ["verify-unit-gate", "verify-unit-gate-2"])

    def test_waiting_caller_uses_second_tree_when_it_is_released_first(self) -> None:
        notice = WaitNotice()

        def third_use() -> Path:
            with copies.standing("verify-unit", self.source, self.head) as tree:
                return tree

        with ThreadPoolExecutor(max_workers=1) as executor:
            with copies.standing("verify-unit", self.source, self.head):
                with copies.standing("verify-unit", self.source, self.head) as second:
                    with redirect_stderr(notice):
                        third = executor.submit(third_use)
                        self.assertTrue(notice.noticed.wait(timeout=10))
                        self.assertFalse(third.done())
                self.assertEqual(third.result(timeout=10), second)

    def test_configured_root_contains_only_the_two_standing_checkouts(self) -> None:
        root = self.base / "another-root"
        self.write_config({**self.settings, "root": str(root)})
        with copies.standing("verify-unit", self.source, self.head) as first:
            with copies.standing("verify-unit", self.source, self.head) as second:
                expected = root / self.source.name
                self.assertEqual(first, expected / "verify-unit-gate")
                self.assertEqual(second, expected / "verify-unit-gate-2")
                self.assertEqual(sorted(path.name for path in expected.iterdir()),
                                 ["verify-unit-gate", "verify-unit-gate-2"])
        self.assertFalse(self.gates.exists())

    def test_missing_or_invalid_required_configuration_refuses_before_clone(self) -> None:
        variants = [
            {**self.settings, key: value}
            for key, value in (("root", ""), ("wait_seconds", "bad"), ("wait_seconds", "-1"),
                               ("git_timeout_seconds", "0"), ("git_timeout_seconds", "nan"),
                               ("guard_timeout_seconds", "inf"))
        ]
        variants.extend({key: value for key, value in self.settings.items() if key != missing}
                        for missing in self.settings)
        for settings in variants:
            with self.subTest(settings=settings):
                self.write_config(settings)
                result = self.run_cli()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.gates.exists())

    def test_linked_source_uses_canonical_repository_folder_name(self) -> None:
        linked = self.base / "linked-worktree"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.published)
        with copies.standing("verify-unit", linked, "HEAD") as tree:
            self.assertEqual(tree, self.gates / "verify-unit-gate")
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.published)
            self.assertTrue((tree / ".git").is_dir())

    def test_workspace_records_ownership_and_preserves_edits_on_resume(self) -> None:
        name = "crate-run-package"
        branch = "style-crate/run/package"
        with copies.workspace("verify-unit", self.source, self.head, name=name, branch=branch) as tree:
            record = scratch.task_of(tree)
            self.assertIsInstance(record, copy_record.Registered)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.fields["owner_unit"], "verify-unit")
            self.assertEqual(record.fields["source_commit"], self.head)
            self.assertEqual(record.fields["borrows_from"], str(self.source / ".git"))
            self.assertEqual(self.git(tree, "rev-parse", "--absolute-git-dir"), str(tree / ".git"))
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
            _ = (tree / "tracked.txt").write_text("pending recovery\n")
            _ = self.git(tree, "add", "tracked.txt")
            _ = (tree / "untracked.txt").write_text("pending file\n")
            with scratch.busy_locks(tree, create=False) as idle:
                self.assertFalse(idle)
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                with copies.workspace("verify-unit", self.source, self.head, name=name, branch=branch):
                    self.fail("a concurrent caller entered the workspace")
        with copies.workspace("verify-unit", self.source, self.head, name=name, branch=branch) as resumed:
            self.assertEqual(resumed, tree)
            self.assertEqual((resumed / "tracked.txt").read_text(), "pending recovery\n")
            self.assertEqual(self.git(resumed, "diff", "--cached", "--name-only"), "tracked.txt")
            self.assertTrue((resumed / "untracked.txt").is_file())
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.head)

    def test_workspace_retries_each_initialization_boundary_without_resetting_finished_edits(self) -> None:
        for boundary in ("registration", "clone", "checkout", "config"):
            with self.subTest(boundary=boundary):
                tree = copies.workspace_path("verify-unit", self.source, boundary)
                original_git = copies.git
                original_register = scratch.register_copy
                interrupted = False
                def register(path: Path, **fields: str) -> None:
                    nonlocal interrupted
                    original_register(path, **fields)
                    if boundary == "registration" and not interrupted:
                        interrupted = True
                        raise RuntimeError("interrupted init")
                def git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
                    nonlocal interrupted
                    result = original_git(repo, *arguments, data=data)
                    match = ((boundary == "clone" and "clone" in arguments)
                             or (boundary == "checkout" and "checkout" in arguments)
                             or (boundary == "config" and repo == tree and len(arguments) == 3
                                 and arguments[:2] == ("config", "user.email")))
                    if match and not interrupted:
                        interrupted = True
                        raise RuntimeError("interrupted init")
                    return result
                with mock.patch.object(scratch, "register_copy", side_effect=register), mock.patch.object(copies, "git", side_effect=git):
                    with self.assertRaisesRegex(RuntimeError, "interrupted init"):
                        with copies.workspace("verify-unit", self.source, self.head, name=boundary, branch="worker"):
                            self.fail("initialization reached the caller")
                record = scratch.task_of(tree)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.fields["copy_commit"], "")
                with copies.workspace("verify-unit", self.source, self.head, name=boundary, branch="worker") as resumed:
                    self.assertEqual(self.git(resumed, "rev-parse", "HEAD"), self.head)
                    _ = (resumed / "tracked.txt").write_text("finished workspace edit\n")
                with copies.workspace("verify-unit", self.source, self.head, name=boundary, branch="worker") as resumed:
                    self.assertEqual((resumed / "tracked.txt").read_text(), "finished workspace edit\n")

    def test_legacy_workspace_keeps_commits_index_and_journal_and_ends_through_holds(self) -> None:
        tree = self.base / "old-run/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        _ = (tree / "stable.txt").write_text("legacy commit\n")
        _ = self.git(tree, "commit", "-qam", "legacy worker commit")
        tip = self.git(tree, "rev-parse", "HEAD")
        _ = (tree / "tracked.txt").write_text("legacy index\n")
        _ = self.git(tree, "add", "tracked.txt")
        journal = tree / ".git/style-batch-intent.json"
        _ = journal.write_text("journal\n")
        _ = (tree / "pending.txt").write_text("pending\n")
        with copies.workspace("verify-unit", self.source, self.head, name="legacy", branch="worker",
                              task="gate:legacy-run", path=tree) as resumed:
            self.assertEqual(resumed, tree)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), tip)
            self.assertEqual(self.git(tree, "diff", "--cached", "--name-only"), "tracked.txt")
            self.assertEqual(journal.read_text(), "journal\n")
            self.assertEqual((tree / "pending.txt").read_text(), "pending\n")
        _ = self.git(self.source, "fetch", "-q", str(tree), "worker:refs/heads/legacy")
        _ = self.git(tree, "reset", "--hard", self.head)
        (tree / "pending.txt").unlink()
        with copies.workspace("verify-unit", self.source, self.head, name="legacy", branch="worker",
                              task="gate:legacy-run", path=tree):
            pass
        copies.end_workspaces("gate:legacy-run", [tree])
        self.assertNotIn(str(tree), scratch.holds())
        self.assertFalse(tree.exists())

    def test_workspace_uses_the_existing_copy_sweep(self) -> None:
        with copies.workspace("verify-unit", self.source, self.published,
                              name="sweep-package", branch="worker") as tree:
            self.assertIn(tree, list(scratch.copy_paths()))
            record = scratch.task_of(tree)
            assert isinstance(record, copy_record.Registered)
            self.assertFalse(scratch.end_copy(tree, record.task, time.time()))
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        self.assertTrue(scratch.end_copy(tree, record.task, time.time()))
        self.assertFalse(tree.exists())

    def test_workspace_refuses_another_base_or_branch_without_resetting(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="reserved", branch="worker") as tree:
            _ = (tree / "tracked.txt").write_text("keep these edits\n")
        for revision, branch in ((self.published, "worker"), (self.head, "other")):
            with self.subTest(revision=revision, branch=branch):
                with self.assertRaises(copies.CopyError):
                    with copies.workspace("verify-unit", self.source, revision, name="reserved", branch=branch):
                        self.fail("a different request entered the workspace")
                self.assertEqual((tree / "tracked.txt").read_text(), "keep these edits\n")

    def test_proof_reuses_its_held_standing_checkout(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as tree:
            with copies.standing("verify-unit", tree, self.head) as proof:
                self.assertEqual(proof, tree)
                with scratch.busy_locks(proof, create=False) as idle:
                    self.assertFalse(idle)
            self.assertEqual(list(self.gates.iterdir()), [tree])

    def test_worker_proof_uses_the_source_repository_gate_pool(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as frozen:
            with copies.workspace("verify-unit", self.source, self.head, name="package", branch="worker") as worker:
                with copies.standing("verify-unit", worker, self.head) as proof:
                    self.assertEqual(proof, frozen.with_name("verify-unit-gate-2"))

    def test_apply_uncommitted_captures_staged_unstaged_and_untracked(self) -> None:
        _ = (self.source / "tracked.txt").write_text("staged edit\n")
        _ = self.git(self.source, "add", "tracked.txt")
        _ = (self.source / "stable.txt").write_text("unstaged edit\n")
        _ = (self.source / "untracked.bin").write_bytes(b"binary\x00payload")
        _ = (self.source / "ignored.txt").write_text("ignored\n")
        _ = self.git(self.source, "status", "--porcelain")
        before = self.snapshot(self.source)
        with copies.standing("verify-unit", self.source, self.head, apply_uncommitted=self.source) as tree:
            self.assertEqual((tree / "tracked.txt").read_text(), "staged edit\n")
            self.assertEqual((tree / "stable.txt").read_text(), "unstaged edit\n")
            self.assertEqual((tree / "untracked.bin").read_bytes(), b"binary\x00payload")
            self.assertFalse((tree / "ignored.txt").exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^"), self.head)
            self.assertEqual(self.git(tree, "log", "-1", "--format=%s"),
                             f"gate: uncommitted {self.source} at {self.head[:7]}")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertEqual(self.snapshot(self.source), before)

    def test_merge_fetches_source_ref_and_creates_a_merge_commit(self) -> None:
        incoming = self.incoming()
        with copies.standing("verify-unit", self.source, self.head, merge="incoming") as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^1"), self.head)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_apply_precedes_merge(self) -> None:
        incoming = self.incoming()
        _ = (self.source / "untracked.txt").write_text("applied before merge\n")
        with copies.standing("verify-unit", self.source, self.head,
                             apply_uncommitted=self.source, merge="incoming") as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertTrue(self.git(tree, "log", "-1", "--format=%s", "HEAD^1").startswith("gate: uncommitted "))
            self.assertEqual((tree / "untracked.txt").read_text(), "applied before merge\n")

    def test_merge_conflict_exits_three_names_file_and_next_use_resets(self) -> None:
        _ = self.git(self.source, "switch", "-qc", "conflict")
        _ = (self.source / "tracked.txt").write_text("incoming conflict\n")
        _ = self.git(self.source, "commit", "-qam", "conflicting change")
        _ = self.git(self.source, "switch", "-q", "main")
        _ = (self.source / "tracked.txt").write_text("uncommitted conflict\n")
        result = self.run_cli("--apply-uncommitted", str(self.source), "--merge", "conflict")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("tracked.txt", result.stdout + result.stderr)
        with copies.standing("verify-unit", self.source, self.head) as tree:
            self.assertEqual(tree, self.gates / "verify-unit-gate")
            self.assertEqual((tree / "tracked.txt").read_text(), "source HEAD\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
            self.assertFalse((tree / ".git/MERGE_HEAD").exists())

    def test_failed_apply_exits_three_releases_lock_and_resets(self) -> None:
        _ = (self.source / "tracked.txt").write_text("patch based on source HEAD\n")
        result = self.run_cli("--commit", self.published, "--apply-uncommitted", str(self.source))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("tracked.txt", result.stdout + result.stderr)
        with copies.standing("verify-unit", self.source, self.head) as tree:
            self.assertEqual(tree, self.gates / "verify-unit-gate")
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_run_uses_tree_cwd_substitution_and_command_exit_status(self) -> None:
        output = self.base / "command-result.json"
        program = ("import json,os,sys; from pathlib import Path; "
                   + "Path(sys.argv[1]).write_text(json.dumps([os.getcwd(),sys.argv[2]])); sys.exit(7)")
        result = self.run_cli(command=(sys.executable, "-c", program, str(output), "checkout={tree}/tracked.txt"))
        self.assertEqual(result.returncode, 7, result.stderr)
        tree = self.gates / "verify-unit-gate"
        self.assertEqual(json.loads(output.read_text()), [str(tree), f"checkout={tree}/tracked.txt"])
        with scratch.busy_locks(tree, create=False) as idle:
            self.assertTrue(idle)

    def test_list_reports_tree_owner_repo_commit_busy_pid_and_target_size(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as tree:
            result = self.cli("list")
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(line for line in result.stdout.splitlines() if str(tree) in line)
            for value in ("verify-unit", self.source.name, self.head[:7], "busy", str(os.getpid())):
                self.assertIn(value, line)
        result = self.cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines() if str(tree) in line)
        self.assertIn("idle", line)
        self.assertRegex(line, r"\s\d+(?:\.\d+)?[KMGTPE]?B?\s*$")

    def test_remove_refuses_busy_tree_then_deletes_both_idle_trees_and_repeats(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as first:
            with copies.standing("verify-unit", self.source, self.head) as second:
                result = self.cli("remove", "--owner", "verify-unit", "--repo", str(self.source))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("busy", result.stderr)
                self.assertTrue(first.is_dir())
                self.assertTrue(second.is_dir())
        result = self.cli("remove", "--owner", "verify-unit", "--repo", str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(self.cli("remove", "--owner", "verify-unit", "--repo", str(self.source)).returncode, 0)

    def test_invalid_owner_refuses_without_creating_a_tree(self) -> None:
        for owner in ("../escape", "Verify", "unit/name", "-unit", "unit_name", ""):
            with self.subTest(owner=owner):
                result = self.run_cli("--owner", owner)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.gates.exists())

    def test_record_is_local_metadata_and_repeated_use_preserves_tree(self) -> None:
        with copies.standing("verify-unit", str(self.source), self.head) as tree:
            stable_mtime = (tree / "stable.txt").stat().st_mtime_ns
            record = self.record(tree)
            self.assertEqual(record["owner"], "verify-unit")
            self.assertEqual(record["repo"], self.source.name)
            self.assertEqual(record["source"], str(self.source))
            self.assertEqual(record["commit"], self.head)
            self.assertEqual(record["holder_pid"], os.getpid())
            self.assertIsInstance(record["last_use"], (int, float))
            self.assertIn(self.head, record.values())
            self.assertIn(os.getpid(), record.values())
            self.assertIn("/.gate.json", (tree / ".git/info/exclude").read_text())
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        with copies.standing("verify-unit", self.source, self.head) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual((tree / "stable.txt").stat().st_mtime_ns, stable_mtime)
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertEqual(list((self.base / "old-copies").glob("**/.scratch.json")), [])

    def test_interrupted_first_record_publication_recovers_on_next_use(self) -> None:
        program = """
import os, sys
from pathlib import Path
from scripts.copies import copies
from scripts.lib import atomic_file
replace = atomic_file.os.replace
def interrupt(source, destination):
    if Path(destination).name == '.gate.json':
        os._exit(19)
    return replace(source, destination)
atomic_file.os.replace = interrupt
with copies.standing('verify-unit', Path(sys.argv[1]), 'HEAD'):
    pass
"""
        interrupted = self.python_run(program, str(self.source))
        self.assertEqual(interrupted.returncode, 19, interrupted.stderr)
        tree = self.gates / "verify-unit-gate"
        self.assertFalse((tree / ".gate.json").exists())
        self.assertTrue(list(tree.glob("..gate.json.*.tmp")))
        with copies.standing("verify-unit", self.source, self.head) as recovered:
            self.assertEqual(recovered, tree)
            self.assertEqual(self.git(recovered, "rev-parse", "HEAD"), self.head)
        self.assertEqual(list(tree.glob("..gate.json.*.tmp")), [])

    def test_interrupted_remove_finishes_even_after_record_is_deleted(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as tree:
            pass
        def interrupt(path: Path) -> None:
            (path / ".gate.json").unlink()
            raise OSError("interrupted directory removal")

        with mock.patch.object(shutil, "rmtree", side_effect=interrupt):
            result = self.cli("remove", "--owner", "verify-unit", "--repo", str(self.source))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(tree.with_name(tree.name + ".removing").exists())
        result = self.cli("remove", "--owner", "verify-unit", "--repo", str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.gates.iterdir()), [])
        with copies.standing("verify-unit", self.source, self.head) as recovered:
            self.assertEqual(recovered, tree)

    def test_relocated_source_rebuilds_git_metadata_and_keeps_target(self) -> None:
        with copies.standing("verify-unit", self.source, self.head) as tree:
            cache = tree / "target/compiled"
            _ = cache.write_text("warm")
        relocated = self.base / "relocated" / self.source.name
        relocated.parent.mkdir()
        _ = self.source.rename(relocated)
        with copies.standing("verify-unit", relocated, self.head) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(cache.read_text(), "warm")
            self.assertEqual(self.record(tree)["source"], str(relocated))
            self.assertEqual(self.git(tree, "remote", "get-url", "origin"), str(relocated))
            self.assertEqual((tree / ".git/objects/info/alternates").read_text().strip(),
                             str(relocated / ".git/objects"))

    def test_deleted_borrowed_objects_are_recovered_from_replacement_source(self) -> None:
        origin = self.git(self.source, "remote", "get-url", "origin")
        with copies.standing("verify-unit", self.source, self.head) as tree:
            cache = tree / "target/compiled"
            _ = cache.write_text("warm")
        shutil.rmtree(self.source)
        _ = self.git(self.base, "clone", "-q", "--branch", "main", origin, str(self.source))
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.published)
        with copies.standing("verify-unit", self.source, "HEAD") as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.published)
            self.assertEqual(cache.read_text(), "warm")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_caller_oserror_passes_through_without_conversion(self) -> None:
        failure = OSError("caller failed writing its own output")
        with self.assertRaises(OSError) as raised:
            with copies.standing("verify-unit", self.source, self.head):
                raise failure
        self.assertIs(raised.exception, failure)

    def test_post_use_failure_has_distinct_error_and_preserves_caller_failure(self) -> None:
        failure = ValueError("gate verdict failed")
        mark_target = copies.mark_target
        uses = 0

        def refresh(tree: Path) -> None:
            nonlocal uses
            uses += 1
            if uses > 1:
                raise OSError("refresh failed")
            mark_target(tree)

        tree = self.gates / "verify-unit-gate"
        with mock.patch.object(copies, "mark_target", side_effect=refresh):
            with self.assertRaises(copies.PostUseError) as raised:
                with copies.standing("verify-unit", self.source, self.head):
                    raise failure
        self.assertIs(raised.exception.caller_error, failure)
        with scratch.busy_locks(tree, create=False) as idle:
            self.assertTrue(idle)

    def test_initial_record_names_requested_commit_before_git_initialization(self) -> None:
        git = copies.git

        def check_before_clone(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
            if arguments[:1] == ("clone",):
                self.assertEqual(self.record(self.gates / "verify-unit-gate")["commit"], self.head)
            return git(repo, *arguments, data=data)

        with mock.patch.object(copies, "git", side_effect=check_before_clone):
            with copies.standing("verify-unit", self.source, self.head) as tree:
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_claim_state_distinguishes_owned_and_busy_checkout(self) -> None:
        tree = self.gates / "verify-unit-gate"
        with copies.claim_tree(tree, create=True) as first:
            self.assertIs(first, copies.ClaimState.ACQUIRED)
            with copies.claim_tree(tree, create=False) as second:
                self.assertIs(second, copies.ClaimState.BUSY)
        with copies.claim_tree(tree, create=False) as released:
            self.assertIs(released, copies.ClaimState.ACQUIRED)

    def test_waiter_wakes_on_first_release_without_fast_polling(self) -> None:
        self.write_config({**self.settings, "wait_seconds": "10"})
        notice = WaitNotice()

        def waiting_use() -> Path:
            with copies.standing("verify-unit", self.source, self.head) as tree:
                return tree

        with ThreadPoolExecutor(max_workers=1) as executor:
            with ExitStack() as primary:
                first = primary.enter_context(copies.standing("verify-unit", self.source, self.head))
                with copies.standing("verify-unit", self.source, self.head):
                    with redirect_stderr(notice):
                        future = executor.submit(waiting_use)
                        self.assertTrue(notice.noticed.wait(timeout=10))
                    primary.close()
                    self.assertEqual(future.result(timeout=2), first)

    def test_applied_untracked_files_have_current_mtime_and_preserve_mode(self) -> None:
        source = self.source / "untracked-build-input"
        _ = source.write_text("new source contents")
        source.chmod(0o755)
        os.utime(source, (1, 1))
        with copies.standing("verify-unit", self.source, self.head,
                             apply_uncommitted=self.source) as tree:
            applied = tree / source.name
            self.assertEqual(applied.read_text(), "new source contents")
            self.assertGreater(applied.stat().st_mtime_ns, source.stat().st_mtime_ns)
            self.assertEqual(applied.stat().st_mode & 0o777, 0o755)

    def test_each_git_step_uses_runtime_timeout_and_next_use_resets(self) -> None:
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrappers = self.base / "bin"
        wrappers.mkdir()
        wrapper = wrappers / "git"
        _ = wrapper.write_text(f"#!{sys.executable}\n"
                               + "import os,sys,time\n"
                               + "if os.environ['GATE_TEST_BLOCK'] in sys.argv[1:]: time.sleep(10)\n"
                               + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        for step in ("clone", "fetch", "checkout", "clean", "apply", "merge"):
            with self.subTest(step=step):
                self.write_config({**self.settings, "git_timeout_seconds": "0.1"})
                with mock.patch.dict(os.environ, {"PATH": str(wrappers) + os.pathsep + os.environ["PATH"],
                                                  "GATE_TEST_BLOCK": step}):
                    with self.assertRaisesRegex(copies.CopyError, f"git {step}.*timed out after 0.1"):
                        _ = copies.git(self.source, step)
        self.write_config(self.settings)
        with copies.standing("verify-unit", self.source, self.head) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_merge_origin_ref_fetches_upstream_even_when_source_tracking_is_stale(self) -> None:
        incoming = self.incoming()
        _ = self.git(self.source, "push", "-q", "origin", "incoming")
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/incoming", self.published)
        before = self.git(self.source, "rev-parse", "origin/incoming")
        with copies.standing("verify-unit", self.source, self.head, merge="origin/incoming") as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
        self.assertEqual(self.git(self.source, "rev-parse", "origin/incoming"), before)

    def test_merge_relative_origin_fetches_upstream_main_from_source_directory(self) -> None:
        incoming = self.incoming()
        _ = self.git(self.source, "push", "-q", "origin", "incoming:main")
        origin = self.git(self.source, "remote", "get-url", "origin")
        relative = os.path.relpath(origin, self.source)
        _ = self.git(self.source, "remote", "set-url", "origin", relative)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/main", self.published)

        result = self.python_run(
            "import sys; from scripts.copies import copies; sys.exit(copies.main(sys.argv[1:]))",
            "run", "--owner", "verify-unit", "--repo", str(self.source), "--commit", "HEAD",
            "--merge", "origin/main", "--", "git", "rev-parse", "HEAD^2")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), incoming)
        tree = self.gates / "verify-unit-gate"
        self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
        self.assertEqual(self.git(self.source, "rev-parse", "origin/main"), self.published)
        self.assertEqual(self.git(self.source, "remote", "get-url", "origin"), relative)

    def test_command_keeps_busy_lock_after_wrapper_is_killed(self) -> None:
        command = "import os,sys; print(os.getpid(), flush=True); sys.stdin.read(1)"
        process = subprocess.Popen(
            [sys.executable, str(Path(copies.__file__)), "run", "--owner", "verify-unit",
             "--repo", str(self.source), "--commit", "HEAD", "--", sys.executable, "-c", command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIsNotNone(process.stdout)
            output = cast(TextIO, process.stdout)
            self.assertTrue(select.select([output], [], [], 10)[0], "command did not start")
            child = int(output.readline().strip())
            process.kill()
            _ = process.wait(timeout=10)
            os.kill(child, 0)
            first = self.gates / "verify-unit-gate"
            with scratch.busy_locks(first, create=False) as idle:
                self.assertFalse(idle, "wrapper death released a surviving command's checkout")
            with copies.standing("verify-unit", self.source, self.head) as second:
                self.assertEqual(second, first.with_name("verify-unit-gate-2"))
        finally:
            if process.stdin is not None:
                _ = process.stdin.write("x")
                process.stdin.close()
                process.stdin = None
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=10)


if __name__ == "__main__":
    _ = unittest.main()
