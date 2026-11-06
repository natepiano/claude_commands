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
from scripts.message import sessions
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

    def test_clone_cli_creates_a_task_owned_checkout_without_a_root_busy_mark(self) -> None:
        result = self.cli("clone", str(self.source), "--task", "gate:camera", "--name", "camera")
        self.assertEqual(result.returncode, 0, result.stderr)
        checkout = Path(result.stdout.strip())
        self.assertTrue(checkout.is_relative_to(self.base / "scratch"))
        self.assertEqual((checkout / "tracked.txt").read_text(), "source HEAD\n")
        self.assertFalse((checkout / scratch.BUSY).exists())
        record = scratch.task_of(checkout.parent)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:camera")
        self.assertIs(record.fields["test_hana_clone"], True)
        self.assertEqual(record.fields["source_path"], str(self.source))
        self.assertEqual(record.fields["source_commit"], self.git(checkout, "rev-parse", "HEAD"))
        self.assertEqual(scratch.end("gate:camera"), [checkout.parent])
        self.assertFalse(checkout.parent.exists())
        self.assertEqual((self.source / "tracked.txt").read_text(), "source HEAD\n")

    def test_clone_cli_with_one_until_value_ends_only_the_selected_clone(self) -> None:
        checkouts: list[Path] = []
        records: list[copy_record.Registered] = []
        for name in ("first", "second"):
            result = self.cli("clone", str(self.source), "--until", "2099-01-01 09:30", "--name", name)
            self.assertEqual(result.returncode, 0, result.stderr)
            checkout = Path(result.stdout.strip())
            record = scratch.task_of(checkout.parent)
            assert isinstance(record, copy_record.Registered)
            checkouts.append(checkout)
            records.append(record)
        self.assertNotEqual(records[0].task, records[1].task)

        self.assertEqual(scratch.end(records[0].task), [checkouts[0].parent])
        self.assertFalse(checkouts[0].parent.exists())
        self.assertEqual((checkouts[1] / "tracked.txt").read_text(), "source HEAD\n")
        self.assertEqual(self.git(checkouts[1], "rev-parse", "HEAD"), self.head)
        self.assertEqual(scratch.task_of(checkouts[1].parent), records[1])

    def symlink_scratch_root(self) -> Path:
        physical = self.base / "physical-scratch"
        physical.mkdir()
        alias = self.base / "scratch-alias"
        alias.symlink_to(physical, target_is_directory=True)
        os.environ["SCRATCH_ROOT"] = str(alias)
        return alias

    def test_clone_through_symlink_scratch_root(self) -> None:
        alias = self.symlink_scratch_root()
        result = self.cli("clone", str(self.source), "--task", "gate:symlink", "--name", "camera")
        self.assertEqual(result.returncode, 0, result.stderr)
        checkout = Path(result.stdout.strip())
        self.assertTrue(checkout.is_relative_to(alias))
        self.assertNotEqual(checkout, checkout.resolve())
        self.assertEqual(self.git(checkout, "rev-parse", "--absolute-git-dir"),
                         str((checkout / ".git").resolve()))
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.head)
        self.assertEqual((checkout / "tracked.txt").read_text(), "source HEAD\n")
        self.assertEqual(scratch.end("gate:symlink"), [checkout.parent])
        self.assertFalse(checkout.parent.exists())

    def test_symlink_scratch_clone_refuses_foreign_git_directory(self) -> None:
        _ = self.symlink_scratch_root()
        checkout = copies.clone(self.source, task="gate:foreign")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.source / ".git")}):
            with self.assertRaisesRegex(copies.CopyError, "Git directory is .*expected"):
                copies.initialize_git(checkout, self.source)
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.head)

    def test_symlink_scratch_clone_refuses_symlink_git_directory(self) -> None:
        _ = self.symlink_scratch_root()
        checkout = copies.clone(self.source, task="gate:git-link")
        metadata = checkout / ".git"
        saved = checkout / "saved-git"
        _ = metadata.rename(saved)
        metadata.symlink_to(saved, target_is_directory=True)
        with self.assertRaisesRegex(copies.CopyError, "must own its .git directory"):
            copies.initialize_git(checkout, self.source)

    def test_apply_changes_through_symlink_scratch_root(self) -> None:
        _ = self.symlink_scratch_root()
        checkout = copies.clone(self.source, task="gate:apply-link")
        _ = (self.source / "untracked.txt").write_text("new file\n")
        copies.apply_changes(checkout, self.source, self.head)
        self.assertEqual((checkout / "untracked.txt").read_text(), "new file\n")

    def test_borrower_order_resolves_stored_git_directory(self) -> None:
        alias = self.symlink_scratch_root()
        with copies.workspace("verify-unit", self.source, self.head, name="a-parent", branch="parent") as parent:
            pass
        with copies.workspace("verify-unit", parent, self.head, name="z-child", branch="child") as child:
            pass
        link = alias / "parent"
        link.symlink_to(parent, target_is_directory=True)
        _ = scratch.update_record(child, lambda record: {
            **(record.fields if isinstance(record, copy_record.Registered) else {}),
            "borrows_from": str(link / ".git"),
        })
        self.assertEqual([tree for tree, _ in copies.owned_workspaces("verify-unit")], [child, parent])

    def test_first_use_owns_git_and_never_changes_source_branch(self) -> None:
        _ = self.git(self.source, "status", "--porcelain")
        before = self.snapshot(self.source)
        with copies.standing("verify-unit", self.source, copies.CommittedSource("HEAD")) as tree:
            self.assertEqual(tree, self.gates / "verify-unit-gate")
            self.assertTrue((tree / ".git").is_dir())
            self.assertEqual(self.git(tree, "rev-parse", "--absolute-git-dir"), str(tree / ".git"))
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            self.assertEqual(self.git(tree, "branch", "--show-current"), "")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertEqual(self.snapshot(self.source), before)
        self.assertEqual(self.git(self.source, "rev-parse", "main"), self.head)

    def test_copies_use_upstream_url_and_tracking_refs_instead_of_local_branches(self) -> None:
        _ = self.git(self.source, "branch", "builds", self.head)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/builds", self.published)
        origin = self.git(self.source, "remote", "get-url", "origin")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(copies.workspace("verify-unit", self.source, self.head,
                                                         name="upstream", branch="worker")),
                     copies.clone(self.source, task="gate:upstream", word="upstream")]
            for tree in trees:
                with self.subTest(tree=tree):
                    self.assertEqual(self.git(tree, "remote", "get-url", "origin"), origin)
                    self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)
                    self.assertNotEqual(self.git(tree, "rev-parse", "origin/builds"),
                                        self.git(self.source, "rev-parse", "builds"))
                    self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
                    self.assertEqual(self.git(tree, "config", "--get-all", "remote.source.fetch"),
                                     "+refs/heads/*:refs/remotes/source/*")
                    self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)
                    self.assertIsNone(scratch.repo_problem(tree))
                    self.assertEqual((tree / ".git/objects/info/alternates").read_text().strip(),
                                     str(self.source / ".git/objects"))

    def test_existing_copies_heal_local_origin_and_prune_local_branch_refs(self) -> None:
        _ = self.git(self.source, "branch", "builds", self.head)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/builds", self.published)
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as standing:
            pass
        with copies.workspace("verify-unit", self.source, self.head, name="heal", branch="worker") as workspace:
            pass
        for tree in (standing, workspace):
            _ = self.git(tree, "remote", "set-url", "origin", str(self.source))
            _ = self.git(tree, "update-ref", "refs/remotes/origin/builds", self.head)
            _ = self.git(tree, "update-ref", "refs/remotes/origin/local-only", self.head)
            _ = self.git(tree, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/builds")
            _ = self.git(tree, "remote", "remove", "source")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(copies.workspace("verify-unit", self.source, self.head,
                                                         name="heal", branch="worker"))]
            self.assertEqual(trees, [standing, workspace])
            for tree in trees:
                with self.subTest(tree=tree):
                    self.assertEqual(self.git(tree, "remote", "get-url", "origin"),
                                     self.git(self.source, "remote", "get-url", "origin"))
                    self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)
                    self.assertEqual(self.git(tree, "for-each-ref", "--format=%(refname) %(objectname)",
                                              "refs/remotes/origin"),
                                     self.git(self.source, "for-each-ref", "--format=%(refname) %(objectname)",
                                              "refs/remotes/origin"))
                    self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
                    self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)
                    self.assertIsNone(scratch.repo_problem(tree))
        for tree in (standing, workspace):
            self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)

    def test_origin_head_is_copied_only_when_the_source_records_it(self) -> None:
        _ = self.git(self.source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "origin/HEAD"), self.published)
        _ = self.git(self.source, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "for-each-ref", "--format=%(refname)",
                                      "refs/remotes/origin/HEAD"), "")

    def test_end_of_use_refreshes_source_branches_for_the_copy_sweep(self) -> None:
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(copies.workspace("verify-unit", self.source, self.head,
                                                         name="saved", branch="worker"))]
            incoming = self.incoming()
        for tree in trees:
            with self.subTest(tree=tree):
                self.assertEqual(self.git(tree, "rev-parse", "source/incoming"), incoming)
                self.assertIsNone(scratch.repo_problem(tree))

    def test_source_without_origin_keeps_local_url_and_branch_tracking(self) -> None:
        _ = self.git(self.source, "remote", "remove", "origin")
        _ = self.git(self.source, "branch", "builds", self.head)
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "remote", "get-url", "origin"), str(self.source))
            self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.head)
            self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
            self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)

    def test_new_commit_reuses_tree_keeps_unchanged_mtime_and_target(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource("HEAD")) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(self.git(reused, "rev-parse", "HEAD"), next_commit)
            self.assertEqual(stable.stat().st_mtime_ns, unchanged_mtime)
            self.assertEqual(cache.read_bytes(), b"warm build output")
            self.assertFalse((tree / "leftover.txt").exists())
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertIn(scratch.CACHE_TAG_SIGNATURE, (tree / "target/CACHEDIR.TAG").read_text())
        self.assertTrue((tree / "target" / scratch.USE_STAMP).is_file())

    def test_held_first_tree_sends_second_caller_to_second_tree(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as second:
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
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
                return tree

        with ThreadPoolExecutor(max_workers=1) as executor:
            with ExitStack() as primary:
                first = primary.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)))
                with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)):
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
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
                return tree

        with ThreadPoolExecutor(max_workers=1) as executor:
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)):
                with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as second:
                    with redirect_stderr(notice):
                        third = executor.submit(third_use)
                        self.assertTrue(notice.noticed.wait(timeout=10))
                        self.assertFalse(third.done())
                self.assertEqual(third.result(timeout=10), second)

    def test_configured_root_contains_only_the_two_standing_checkouts(self) -> None:
        root = self.base / "another-root"
        self.write_config({**self.settings, "root": str(root)})
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as second:
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
        with copies.standing("verify-unit", linked, copies.CommittedSource("HEAD")) as tree:
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

    def test_legacy_workspace_refuses_matching_relative_urls_for_different_repositories(self) -> None:
        tree = self.base / "old-run/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        _ = self.git(self.base, "clone", "-q", "--bare", str(self.source), str(tree.parent / "origin.git"))
        for repo in (self.source, tree):
            _ = self.git(repo, "remote", "set-url", "origin", "../origin.git")

        with self.assertRaisesRegex(copies.CopyError, "legacy workspace source differs"):
            with copies.workspace("verify-unit", self.source, self.head, name="legacy", branch="worker", path=tree):
                self.fail("the unrelated workspace reached the caller")

        self.assertIsInstance(scratch.task_of(tree), copy_record.NotRegistered)
        self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.git(tree, "remote", "get-url", "origin"), "../origin.git")

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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            with copies.standing("verify-unit", tree, copies.CommittedSource(self.head)) as proof:
                self.assertEqual(proof, tree)
                with scratch.busy_locks(proof, create=False) as idle:
                    self.assertFalse(idle)
            self.assertEqual(list(self.gates.iterdir()), [tree])

    def test_worker_proof_uses_the_source_repository_gate_pool(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as frozen:
            with copies.workspace("verify-unit", self.source, self.head, name="package", branch="worker") as worker:
                with copies.standing("verify-unit", worker, copies.CommittedSource(self.head)) as proof:
                    self.assertEqual(proof, frozen.with_name("verify-unit-gate-2"))

    def test_apply_uncommitted_captures_staged_unstaged_and_untracked(self) -> None:
        _ = (self.source / "tracked.txt").write_text("staged edit\n")
        _ = self.git(self.source, "add", "tracked.txt")
        _ = (self.source / "stable.txt").write_text("unstaged edit\n")
        _ = (self.source / "untracked.bin").write_bytes(b"binary\x00payload")
        _ = (self.source / "ignored.txt").write_text("ignored\n")
        _ = self.git(self.source, "status", "--porcelain")
        before = self.snapshot(self.source)
        with copies.standing("verify-unit", self.source, copies.WorktreeSource(self.head, self.source)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head), merge=copies.MergeRevision("incoming")) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^1"), self.head)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_apply_precedes_merge(self) -> None:
        incoming = self.incoming()
        _ = (self.source / "untracked.txt").write_text("applied before merge\n")
        with copies.standing("verify-unit", self.source, copies.WorktreeSource(self.head, self.source), merge=copies.MergeRevision("incoming")) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(tree, self.gates / "verify-unit-gate")
            self.assertEqual((tree / "tracked.txt").read_text(), "source HEAD\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
            self.assertFalse((tree / ".git/MERGE_HEAD").exists())

    def test_failed_apply_exits_three_releases_lock_and_resets(self) -> None:
        _ = (self.source / "tracked.txt").write_text("patch based on source HEAD\n")
        result = self.run_cli("--commit", self.published, "--apply-uncommitted", str(self.source))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("tracked.txt", result.stdout + result.stderr)
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as second:
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

    def test_owner_removal_covers_repositories_and_editable_copies(self) -> None:
        other = ImmutableGitSeed.create(self.base / "other", "main", "Gate Tests",
                                        "gate-tests@example.invalid", {"other.txt": "content"}).checkout
        other = other.rename(other.with_name("other-repository"))
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as first:
            pass
        with copies.standing("verify-unit", other, copies.CommittedSource("HEAD")) as second:
            pass
        with copies.workspace("verify-unit", self.source, self.head,
                              name="worker", branch="worker") as worker:
            pass
        legacy = self.base / "legacy-run/worker"
        legacy.mkdir(parents=True)
        copies.initialize_git(legacy, self.source)
        _ = self.git(legacy, "checkout", "-q", "-B", "legacy", self.head)
        with copies.workspace("verify-unit", self.source, self.head, name="legacy", branch="legacy",
                              task="gate:legacy-run", path=legacy):
            pass
        with copies.standing("other-unit", self.source, copies.CommittedSource(self.head)) as kept:
            pass
        narrowed = self.cli("remove", "--owner", "verify-unit", "--repo", str(other))
        self.assertEqual(narrowed.returncode, 0, narrowed.stderr)
        self.assertFalse(second.exists())
        self.assertTrue(first.exists())
        result = self.cli("remove", "--owner", "verify-unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(first.exists())
        self.assertFalse(worker.exists())
        self.assertFalse(legacy.exists())
        self.assertNotIn(str(legacy), scratch.holds())
        self.assertTrue(kept.exists())
        copies.remove_trees("verify-unit")

    def test_owner_removal_retries_busy_and_interrupted_trees_without_a_source(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            result = self.cli("remove", "--owner", "verify-unit")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("busy", result.stderr)
            self.assertTrue(tree.exists())
        def interrupt(path: Path) -> None:
            (path / ".gate.json").unlink()
            raise OSError("interrupted owner removal")

        with mock.patch.object(shutil, "rmtree", side_effect=interrupt):
            result = self.cli("remove", "--owner", "verify-unit")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(copies.removing_path(tree).exists())
        shutil.rmtree(self.source)
        result = self.cli("remove", "--owner", "verify-unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(copies.removing_path(tree).exists())
        self.assertEqual(self.cli("remove", "--owner", "verify-unit").returncode, 0)

    def test_owner_removal_finishes_a_deleted_editable_copy(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head,
                              name="deleted-worker", branch="worker") as tree:
            pass
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        _ = scratch.hold(tree, record.task, "keep the worker during its run")
        delete = scratch.remove

        def interrupt(path: Path) -> None:
            delete(path)
            raise OSError("interrupted after folder deletion")

        with mock.patch.object(scratch, "remove", side_effect=interrupt):
            result = self.cli("remove", "--owner", "verify-unit")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(tree.exists())
        self.assertTrue(scratch.removal_mark(tree).exists())
        result = self.cli("remove", "--owner", "verify-unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(scratch.removal_mark(tree).exists())
        self.assertNotIn(str(tree), scratch.holds())
        self.assertEqual(self.cli("remove", "--owner", "verify-unit").returncode, 0)

    def test_owner_removal_accepts_names_that_cannot_create_copies(self) -> None:
        for owner in ("Verify", "unit_name"):
            with self.subTest(owner=owner):
                result = self.cli("remove", "--owner", owner)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.gates.exists())

    def test_busy_editable_copy_identifies_its_holder(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head,
                              name="busy-worker", branch="worker") as tree:
            with mock.patch.object(sessions, "running_sessions", return_value=[]):
                result = self.cli("remove", "--owner", "verify-unit")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(str(tree), result.stderr)
            self.assertIn(f"pid {os.getpid()}", result.stderr)

    def test_repository_removal_skips_a_workspace_with_a_deleted_source(self) -> None:
        other = ImmutableGitSeed.create(self.base / "other", "main", "Gate Tests",
                                        "gate-tests@example.invalid", {"other.txt": "content"}).checkout
        other = other.rename(other.with_name("unrelated-repository"))
        with copies.workspace("verify-unit", other, "HEAD", name="other-worker", branch="worker") as kept:
            pass
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            pass
        shutil.rmtree(other)
        result = self.cli("remove", "--owner", "verify-unit", "--repo", str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())
        self.assertTrue(kept.exists())

    def test_explicit_repository_scopes_select_only_the_requested_repository(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            pass
        copies.remove_trees("verify-unit", copies.SelectedRepository(self.source.name))
        self.assertFalse(tree.exists())
        copies.remove_trees("verify-unit", copies.AllRepositories())

    def test_owner_removal_requires_a_repository_scope_in_the_python_api(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            pass
        for value in ("", str(self.source), self.source):
            with self.subTest(value=value), self.assertRaisesRegex(copies.CopyError, "repository scope"):
                copies.remove_trees("verify-unit", cast(copies.RepositoryScope, cast(object, value)))
            self.assertTrue(tree.exists())
        copies.remove_trees("verify-unit", copies.AllRepositories())
        self.assertFalse(tree.exists())

    def test_owner_removal_allows_an_ordinary_copy_with_a_crate_name(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="crate-notes", branch="notes") as tree:
            pass
        copies.remove_trees("verify-unit")
        self.assertFalse(tree.exists())

    def test_owner_removal_preserves_a_source_with_an_adopted_borrower(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="parent", branch="worker") as parent:
            pass
        child = self.base / "adopted/borrower"
        child.mkdir(parents=True)
        copies.initialize_git(child, parent)
        _ = self.git(child, "checkout", "-q", "-B", "borrower", self.head)
        with copies.workspace("other-unit", parent, self.head, name="borrower", branch="borrower", path=child):
            pass
        with self.assertRaisesRegex(copies.CopyError, "borrowed") as refusal:
            copies.remove_trees("verify-unit")
        self.assertIn(str(child), str(refusal.exception))
        self.assertTrue(parent.exists())
        self.assertEqual(self.git(child, "cat-file", "-p", self.head), self.git(parent, "cat-file", "-p", self.head))
        for pending in (child, parent):
            with self.subTest(pending=pending):
                with self.assertRaisesRegex(RuntimeError, "interrupted removal"):
                    scratch.remove_marked(pending, mock.Mock(side_effect=RuntimeError("interrupted removal")))
                with self.assertRaisesRegex(copies.CopyError, "borrowed"):
                    copies.remove_trees("verify-unit")
                self.assertTrue(parent.exists())
                self.assertEqual(self.git(child, "rev-parse", "HEAD"), self.head)
        copies.remove_trees("other-unit")
        copies.remove_trees("verify-unit")
        self.assertFalse(child.exists())
        self.assertFalse(parent.exists())

    def test_owner_removal_matches_transferred_names_without_constructing_unsafe_paths(self) -> None:
        for owner in ("Verify", "unit_name"):
            with self.subTest(owner=owner):
                with copies.workspace("verify-unit", self.source, self.head, name=owner, branch="worker") as tree:
                    pass
                record = scratch.task_of(tree)
                assert isinstance(record, copy_record.Registered)
                scratch.hand_over(tree, owner, record.task)
                copies.remove_trees(owner)
                self.assertFalse(tree.exists())
        for owner in ("../escape", str(self.base / "outside")):
            with self.subTest(owner=owner), mock.patch.object(copies, "tree_paths", side_effect=AssertionError("unsafe path")):
                copies.remove_trees(owner)

    def test_owner_removal_finds_adopted_copies_with_a_changed_hold_reason(self) -> None:
        tree = self.base / "adopted/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        with copies.workspace("verify-unit", self.source, self.head, name="adopted", branch="worker", path=tree):
            pass
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        scratch.hand_over(tree, "Next_unit", record.task)
        _ = scratch.hold(tree, record.task, "a different hold reason")
        copies.remove_trees("Next_unit")
        self.assertFalse(tree.exists())
        self.assertNotIn(str(tree), scratch.holds())

    def test_owner_transfer_after_selection_preserves_the_new_owners_copy(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="transfer", branch="worker") as tree:
            pass
        select_workspaces = copies.owned_workspaces

        def transfer(owner: str, repo: copies.RepositoryScope) -> list[tuple[Path, copy_record.Registered]]:
            selected = select_workspaces(owner, repo)
            scratch.hand_over(tree, "next-unit", selected[0][1].task)
            return selected

        with mock.patch.object(copies, "owned_workspaces", side_effect=transfer):
            with self.assertRaisesRegex(copies.CopyError, "changed"):
                copies.remove_trees("verify-unit")
        self.assertTrue(tree.exists())
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["owner_unit"], "next-unit")
        self.assertNotIn("ended_task", record.fields)

    def test_owner_removal_deletes_borrowers_before_their_sources(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="a-parent", branch="worker") as parent:
            pass
        with copies.workspace("verify-unit", parent, self.head, name="z-child", branch="worker") as child:
            pass
        copies.remove_trees("verify-unit")
        self.assertFalse(child.exists())
        self.assertFalse(parent.exists())

    def test_owner_removal_preserves_an_external_borrower_and_does_not_end_a_busy_copy(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="a-source", branch="worker") as parent:
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                copies.remove_trees("verify-unit")
            record = scratch.task_of(parent)
            assert isinstance(record, copy_record.Registered)
            self.assertNotIn("ended_task", record.fields)
        with copies.workspace("other-unit", parent, self.head, name="borrower", branch="worker") as child:
            pass
        with self.assertRaisesRegex(copies.CopyError, "borrowed"):
            copies.remove_trees("verify-unit")
        self.assertTrue(parent.exists())
        self.assertTrue(child.exists())

    def test_owner_removal_refuses_a_damaged_record_until_repaired(self) -> None:
        with copies.workspace("verify-unit", self.source, self.head, name="damaged", branch="worker") as tree:
            pass
        path = tree / copy_record.RECORD
        record = path.read_text()
        _ = path.write_text("{invalid")
        with self.assertRaisesRegex(copies.CopyError, "repair record") as refusal:
            copies.remove_trees("verify-unit")
        self.assertIn(str(path), str(refusal.exception))
        self.assertTrue(tree.exists())
        _ = path.write_text(record)
        copies.remove_trees("verify-unit")
        self.assertFalse(tree.exists())

    def test_invalid_owner_refuses_without_creating_a_tree(self) -> None:
        for owner in ("../escape", "Verify", "unit/name", "-unit", ""):
            with self.subTest(owner=owner):
                result = self.run_cli("--owner", owner)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.gates.exists())

    def test_underscore_owner_succeeds_and_creates_the_checkout(self) -> None:
        result = self.run_cli("--owner", "unit_name")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.gates / "unit_name-gate").exists())

    def test_record_is_local_metadata_and_repeated_use_preserves_tree(self) -> None:
        with copies.standing("verify-unit", str(self.source), copies.CommittedSource(self.head)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as reused:
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
with copies.standing('verify-unit', Path(sys.argv[1]), copies.CommittedSource('HEAD')):
    pass
"""
        interrupted = self.python_run(program, str(self.source))
        self.assertEqual(interrupted.returncode, 19, interrupted.stderr)
        tree = self.gates / "verify-unit-gate"
        self.assertFalse((tree / ".gate.json").exists())
        self.assertTrue(list(tree.glob("..gate.json.*.tmp")))
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as recovered:
            self.assertEqual(recovered, tree)
            self.assertEqual(self.git(recovered, "rev-parse", "HEAD"), self.head)
        self.assertEqual(list(tree.glob("..gate.json.*.tmp")), [])

    def test_interrupted_remove_finishes_even_after_record_is_deleted(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as recovered:
            self.assertEqual(recovered, tree)

    def test_relocated_source_rebuilds_git_metadata_and_keeps_target(self) -> None:
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            cache = tree / "target/compiled"
            _ = cache.write_text("warm")
        relocated = self.base / "relocated" / self.source.name
        relocated.parent.mkdir()
        _ = self.source.rename(relocated)
        with copies.standing("verify-unit", relocated, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(cache.read_text(), "warm")
            self.assertEqual(self.record(tree)["source"], str(relocated))
            self.assertEqual(self.git(tree, "remote", "get-url", "origin"),
                             self.git(relocated, "remote", "get-url", "origin"))
            self.assertEqual((tree / ".git/objects/info/alternates").read_text().strip(),
                             str(relocated / ".git/objects"))

    def test_deleted_borrowed_objects_are_recovered_from_replacement_source(self) -> None:
        origin = self.git(self.source, "remote", "get-url", "origin")
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            cache = tree / "target/compiled"
            _ = cache.write_text("warm")
        shutil.rmtree(self.source)
        _ = self.git(self.base, "clone", "-q", "--branch", "main", origin, str(self.source))
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.published)
        with copies.standing("verify-unit", self.source, copies.CommittedSource("HEAD")) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.published)
            self.assertEqual(cache.read_text(), "warm")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_caller_oserror_passes_through_without_conversion(self) -> None:
        failure = OSError("caller failed writing its own output")
        with self.assertRaises(OSError) as raised:
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)):
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
                with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)):
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
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
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
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
                return tree

        with ThreadPoolExecutor(max_workers=1) as executor:
            with ExitStack() as primary:
                first = primary.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)))
                with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)):
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
        with copies.standing("verify-unit", self.source, copies.WorktreeSource(self.head, self.source)) as tree:
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
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_merge_origin_ref_fetches_upstream_even_when_source_tracking_is_stale(self) -> None:
        incoming = self.incoming()
        _ = self.git(self.source, "push", "-q", "origin", "incoming")
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/incoming", self.published)
        before = self.git(self.source, "rev-parse", "origin/incoming")
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head), merge=copies.MergeRevision("origin/incoming")) as tree:
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
            [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(copies.__file__)), "run", "--owner", "verify-unit",
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
            with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as second:
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
