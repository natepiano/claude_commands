"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from scripts.production import checkpoint_gate

from scripts.agents import remote

from scripts.production.staff_test_support import isolated_staff_file, unit_membership, recorded_showrunner
from scripts.production import staff

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path, PurePosixPath
from typing import cast, final
from zoneinfo import ZoneInfo

from scripts.production import merge_checkpoint
from scripts.production import showrunners
from scripts.production import production_doc

from scripts.lib import main_publish, scratch

from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, REAL_MERGE_TESTS, STUB, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()
    state = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(state.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"ZONE_STATE": state.name}))


class CheckpointTestFolderTests(unittest.TestCase):
    def test_package_step_uses_the_manifest_name_and_package_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            package = checkout / "scripts/aws_target/proof_crate"
            package.mkdir(parents=True)
            _ = (package / "Cargo.toml").write_text('[package]\nname = "aws-routing-proof"\nversion = "0.1.0"\n')
            production = production_doc.ProductionDocument(checkout / "production.md", "fixture", "merge", checkout,
                                                  checkout / "production.log", ZoneInfo("UTC"))
            steps = checkpoint_gate.test_steps(production, ("scripts/aws_target/proof_crate/src/lib.rs",), ())
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].label, "package aws-routing-proof")
            self.assertEqual(steps[0].command[-2:], ["test", "aws-routing-proof"])
            self.assertEqual(steps[0].folder, package)

    def test_example_step_uses_the_manifest_name_and_checkout_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            package = checkout / "crates/proof_crate"
            package.mkdir(parents=True)
            _ = (package / "Cargo.toml").write_text('[package]\nname = "aws-routing-proof"\nversion = "0.1.0"\n')
            production = production_doc.ProductionDocument(checkout / "production.md", "fixture", "merge", checkout,
                                                  checkout / "production.log", ZoneInfo("UTC"))
            steps = checkpoint_gate.test_steps(production, ("crates/proof_crate/examples/demo.rs",), ())
            self.assertEqual(len(steps), 2)
            self.assertEqual(steps[1].label, "example aws-routing-proof/demo")
            self.assertEqual(steps[1].command[-4:], ["example", "aws-routing-proof", "--name", "demo"])
            self.assertEqual(steps[1].folder, checkout)

    def test_package_commands_and_retries_use_the_package_folder(self) -> None:
        for workspace in (False, True):
            for retry in (False, True):
                with self.subTest(workspace=workspace, retry=retry), tempfile.TemporaryDirectory() as temporary:
                    checkout = Path(temporary)
                    relative = ("crates/proof_crate/examples/demo.rs" if workspace
                                else "scripts/aws_target/proof_crate/src/lib.rs")
                    package = checkout / Path(relative).parents[1]
                    source = checkout / relative
                    source.parent.mkdir(parents=True)
                    _ = source.write_text("fn main() {}\n" if workspace else "pub fn proof() {}\n")
                    _ = (package / "Cargo.toml").write_text('[package]\nname = "proof_crate"\nversion = "0.1.0"\n')
                    if workspace:
                        _ = (checkout / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/proof_crate"]\n')
                    verify = checkout / "verify.sh"
                    _ = verify.write_text(
                        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
                        + "print(f'{sys.argv[1]}={Path.cwd()}')\n"
                        + ("if sys.argv[1] == 'test' and not Path('retried').exists():\n"
                           + "    Path('retried').touch()\n    sys.exit(1)\n" if retry else ""))
                    verify.chmod(0o755)
                    production = production_doc.ProductionDocument(checkout / "production.md", "fixture", "merge", checkout,
                                                          checkout / "production.log", ZoneInfo("UTC"))
                    log = checkout / "test.log"
                    merge_tests = ((sys.executable, "-c", "from pathlib import Path; print(f'merge={Path.cwd()}')"),)
                    with mock.patch.dict(os.environ, {"MERGE_CHECKPOINT_VERIFY": str(verify)}):
                        _ = checkpoint_gate.run_checkpoint_test_steps(
                            production, (relative,), merge_tests, frozenset({"proof_crate"}), frozenset(), log)
                    output = log.read_text()
                    self.assertEqual(output.count(f"test={package}\n"), 2 if retry else 1)
                    self.assertIn("package_proof_crate_EXIT=" + ("1" if retry else "0"), output)
                    if retry:
                        self.assertIn("package_proof_crate_RERUN_EXIT=0", output)
                    if workspace:
                        self.assertIn(f"example={checkout}\n", output)
                    self.assertIn(f"merge={checkout}\n", output)


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_successful_push_ends_landed_worktrees_and_keeps_dirty_worktrees(self) -> None:
        checkout = Path(__file__).resolve().parents[2]
        records = self.root / "worktree-records"
        config = self.root / "staff.conf"
        _ = config.write_text((checkout / "config/staff.conf").read_text())
        self.env.update({"WORKTREES_STATE_DIR": str(records), "STAFF_CONFIG": str(config)})
        library = Path(self.env["HOME"]) / ".claude/scripts/lib"
        library.symlink_to(checkout / "scripts/lib", target_is_directory=True)
        _ = self.git("remote", "set-head", "origin", "production")
        checkpoint = self.checkpoint()
        trees = {"landed-work": self.root / "landed-work", "dirty-work": self.root / "dirty-work"}
        for branch, tree in trees.items():
            made = subprocess.run(
                ["bash", str(checkout / "scripts/worktree/make_worktree.sh"), str(tree), branch],
                cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False, timeout=20)
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            _ = self.git("merge", "--ff-only", checkpoint, cwd=tree)
            _ = self.git("push", "-u", "origin", branch, cwd=tree)
        self.write(trees["dirty-work"], "src/alpha.txt", "uncommitted work\n")

        result = self.run_checkpoint(checkpoint).process

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("push: ok", result.stdout)
        self.assertIn("worktrees: ended", result.stdout)
        self.assertIn(str(trees["landed-work"]), result.stdout)
        self.assertIn("worktrees: kept", result.stdout)
        self.assertIn(str(trees["dirty-work"]), result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"),
                         self.git("rev-parse", "refs/heads/production", cwd=self.origin))
        worktrees = self.git("worktree", "list", "--porcelain")
        self.assertNotIn(f"worktree {trees['landed-work']}\n", worktrees + "\n")
        self.assertNotIn(trees["landed-work"].name, [path.name for path in self.root.iterdir()])
        for folder in (self.checkout, self.origin):
            branches = self.git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=folder).splitlines()
            self.assertNotIn("refs/heads/landed-work", branches)
            self.assertIn("refs/heads/dirty-work", branches)
        remaining = [cast(dict[str, object], json.loads(path.read_text()))
                     for path in (records / "made").glob("*.json")]
        self.assertEqual([(row["path"], row["branch"]) for row in remaining],
                         [(str(trees["dirty-work"]), "dirty-work")])
        self.assertEqual((trees["dirty-work"] / "src/alpha.txt").read_text(), "uncommitted work\n")

    def test_merge_uses_the_staff_name_with_a_different_document_name(self) -> None:
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            _ = staff.rename(showrunners.owner_for("example").id, "hana")
        result = self.run_checkpoint(self.checkpoint(), "--production", "hana")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "production", cwd=self.origin))

    def test_checkpoint_refuses_another_staff_parent_without_changes_on_retry(self) -> None:
        checkpoint = self.checkpoint()
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            parent = recorded_showrunner("receiving", self.root / "receiving-production.md")
            unit = unit_membership("example", "alpha")
            _ = staff.move(unit.id, staff.ReportsTo(parent.id))
        before = self.git("show-ref"), self.git("status", "--porcelain")
        origin = self.git("show-ref", cwd=self.origin)
        document = self.doc.read_bytes()
        record = Path(self.env["STAFF_FILE"]).read_bytes()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_checkpoint(checkpoint).process
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("alpha: staff parent is", result.stdout)
                self.assertIn("receiving", result.stdout)
                self.assertEqual((self.git("show-ref"), self.git("status", "--porcelain")), before)
                self.assertEqual(self.git("show-ref", cwd=self.origin), origin)
                self.assertEqual(self.doc.read_bytes(), document)
                self.assertEqual(Path(self.env["STAFF_FILE"]).read_bytes(), record)
                self.assertEqual(self.events("checkpoint-test"), [])

    def test_as_built_merges_after_code_checkpoint_reaches_branch_through_second_parent(self) -> None:
        for merge_production, unmerged_code, side_docs, missing_main in (
                (False, False, False, False), (True, False, False, False), (True, True, False, False),
                (False, False, True, False), (False, False, False, True)):
            with self.subTest(merge_production=merge_production, unmerged_code=unmerged_code,
                              side_docs=side_docs, missing_main=missing_main):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                first = self.run_checkpoint(self.checkpoint())
                self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
                code_merge = self.git("rev-parse", "HEAD")
                _ = self.git("checkout", "-b", "integration", "HEAD^1")
                _ = self.git("commit", "--allow-empty", "-m", "Independent integration")
                _ = self.git("merge", "--no-ff", "-m", "Integrate production", "production")
                _ = self.git("branch", "-f", "production", "HEAD")
                _ = self.git("checkout", "production")
                self.assertNotIn(code_merge, self.git("rev-list", "--first-parent", "HEAD").splitlines())
                self.assertEqual(self.git("merge-base", "--is-ancestor", code_merge, "production"), "")
                closing = self.as_built_commit()
                held_commit = closing
                if unmerged_code:
                    closing = self.checkpoint(content="code after the as-built commit\n")
                if side_docs:
                    _ = self.git("checkout", "-b", "side-phase", f"{closing}^", cwd=self.unit)
                    side = self.checkpoint("docs/side.md", "unmerged side phase\n",
                                           subject="checkpoint(beta): side phase", push=False)
                    held_commit = side
                    _ = self.git("checkout", "alpha-branch", cwd=self.unit)
                    _ = self.git("merge", "--no-ff", "-m", "Merge side phase after as-built", side, cwd=self.unit)
                    _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
                    closing = self.git("rev-parse", "HEAD", cwd=self.unit)
                if merge_production:
                    _ = self.git("fetch", "origin", "production", cwd=self.unit)
                    _ = self.git("merge", "--no-ff", "-m", "Merge production after as-built",
                                 "origin/production", cwd=self.unit)
                    _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
                    closing = self.git("rev-parse", "HEAD", cwd=self.unit)
                if missing_main:
                    _ = self.push_main_change("docs/main.md", "independent main change\n")
                    _ = self.git("fetch", "origin", "main", cwd=self.unit)
                    _ = self.git("merge", "--no-ff", "-m", "Merge main after as-built", "origin/main", cwd=self.unit)
                    _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
                    closing = self.git("rev-parse", "HEAD", cwd=self.unit)
                    _ = self.git("update-ref", "-d", "refs/remotes/origin/main")
                (self.state / "events.jsonl").unlink()
                result = self.run_checkpoint(closing, "--as-built", review=False)
                if unmerged_code or side_docs:
                    subject = self.git("show", "-s", "--format=%s", held_commit)
                    self.assert_stopped_before_merge(result, "held", f"unmerged commit {held_commit[:12]}: {subject}")
                    self.assertIn(f"wait for {held_commit[:12]} to merge, then resend the as-built",
                                  result.process.stdout)
                else:
                    self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
                    self.assertIn("test: ok — as-built has no package tests", result.process.stdout)
                    tree = self.git("ls-tree", "-r", "--name-only", "HEAD")
                    self.assertIn("docs/as-built/alpha.md", tree)
                    self.assertNotIn("docs/plans/alpha.md", tree)
                    self.assertRegex(self.git("log", "-1", "--format=%s"), r"^Merge alpha phase \S+ as-built \(")

    def test_checkpoint_merges_into_its_staff_parent_and_ignores_unavailable_worktrees(self) -> None:
        checkpoint = self.checkpoint()
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            row = unit_membership("example", "retired")
            _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.root / "removed"), expected=row.worktree)
            _ = unit_membership("example", "standby")
        result = self.run_checkpoint(checkpoint)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshot"
            _ = shutil.copytree(self.root, snapshot)
            for outcome in ("green", "red", "conflict", "resume", "moving-green", "moving-red"):
                with self.subTest(main=outcome):
                    shutil.rmtree(self.root)
                    _ = shutil.copytree(snapshot, self.root)
                    self.bind_fixture(self.root)
                    (self.state / "events.jsonl").unlink()
                    relative = "src/alpha.txt" if outcome == "conflict" else "crates/main-proof/Cargo.toml"
                    content = "main side\n" if outcome == "conflict" else '[package]\nname = "main-proof"\nversion = "0.1.0"\n'
                    main = self.push_main_change(relative, content)
                    checkpoint = self.checkpoint(content="later unit change\n")
                    before = self.git("rev-parse", "HEAD")
                    origin_tip = self.git("rev-parse", "production", cwd=self.origin)
                    observer = STUB + '''
import fcntl
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
common = subprocess.run(["git", "rev-parse", "--git-common-dir"], check=True, capture_output=True, text=True).stdout.strip()
with (Path(common) / "main-push.lock").open("a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_state = "held"
    else:
        lock_state = "free"
with (state / "tested-locks").open("a") as output:
    output.write(lock_state + "\\n")
with (state / "tested-tips").open("a") as output:
    output.write(tip + "\\n")
if name == "checkpoint-test" and (state / "move-main").exists() and not (state / "moved-main").exists():
    writer = Path((state / "move-main").read_text())
    manifest = writer / "crates/later-main/Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[package]\\nname = "later-main"\\nversion = "0.1.0"\\n')
    for args in (["add", "crates/later-main"], ["commit", "-m", "Main moves during checkpoint tests"], ["push", "origin", "main"]):
        subprocess.run(["git", *args], cwd=writer, check=True, capture_output=True)
    moved = subprocess.run(["git", "rev-parse", "HEAD"], cwd=writer, check=True, capture_output=True, text=True).stdout.strip()
    (state / "moved-main").write_text(moved)
if (state / "second-red").exists() and (state / "moved-main").exists():
    moved = (state / "moved-main").read_text()
    if subprocess.run(["git", "merge-base", "--is-ancestor", moved, tip]).returncode == 0:
        raise SystemExit(1)
raise SystemExit(1 if (state / "red").exists() else 0)
'''
                    for name in ("checkpoint-test", "verify.sh"):
                        self.executable(self.bin / name, observer)
                    if outcome == "red":
                        _ = (self.state / "red").touch()
                    if outcome.startswith("moving-"):
                        _ = (self.state / "move-main").write_text(str(self.root / "main-fix-writer"))
                    if outcome == "moving-red":
                        _ = (self.state / "second-red").touch()
                    if outcome == "resume":
                        _ = self.git("merge", "--no-ff", "-m", "Merge interrupted checkpoint", checkpoint)
                    result = self.run_checkpoint(checkpoint, phase="3")
                    if outcome != "conflict":
                        tips = (self.state / "tested-tips").read_text().splitlines()
                        lock_states = (self.state / "tested-locks").read_text().splitlines()
                        self.assertEqual(lock_states, ["free" if tip == tips[0] else "held" for tip in tips])
                    if outcome.startswith("moving-"):
                        tips = (self.state / "tested-tips").read_text().splitlines()
                        moved = (self.state / "moved-main").read_text()
                        self.assertEqual(len(set(tips)), 2)
                        self.assertNotEqual(merge_checkpoint.git(self.checkout, "merge-base", "--is-ancestor", moved, tips[0]).returncode, 0)
                        self.assertEqual(self.git("merge-base", "--is-ancestor", moved, tips[-1]), "")
                        self.assertIn(["test", "later-main"], self.events("verify.sh"))
                        if outcome == "moving-red":
                            self.assertIn("red: failed", result.process.stdout)
                            self.assertEqual(self.git("rev-parse", "HEAD"), before)
                        else:
                            self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
                            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tips[-1])
                    elif outcome == "conflict":
                        self.assertIn("push: held — origin/main conflicts: src/alpha.txt", result.process.stdout)
                        self.assertIn("merge origin/main into the merge branch, resolve the conflict, and run the push again",
                                      result.process.stdout)
                        self.assertEqual(self.events("checkpoint-test"), [])
                        self.assertEqual(self.git("rev-parse", "HEAD"), before)
                    else:
                        tips = (self.state / "tested-tips").read_text().splitlines()
                        self.assertEqual(len(set(tips)), 1)
                        self.assertEqual(self.git("merge-base", "--is-ancestor", main, tips[0]), "")
                        self.assertEqual(self.git("merge-base", "--is-ancestor", checkpoint, tips[0]), "")
                        self.assertIn(["test", "main-proof"], self.events("verify.sh"))
                        if outcome == "red":
                            self.assertIn("red: failed", result.process.stdout)
                            self.assertEqual(self.git("rev-parse", "HEAD"), before)
                        else:
                            self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
                            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tips[0])
                    if outcome in ("red", "conflict", "moving-red"):
                        self.assertEqual(result.process.returncode, 1, result.process.stdout + result.process.stderr)
                        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), origin_tip)
                        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_failed_push_preserves_the_merge_branches_unpublished_tip(self) -> None:
        for failure in ("push", "final-fetch"):
            with self.subTest(failure=failure):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.write(self.checkout, "local-only.txt", "local commit must survive\n")
                _ = self.git("add", "local-only.txt")
                _ = self.git("commit", "-m", "local merge branch work not on origin")
                before = self.git("rev-parse", "HEAD")
                remote = self.git("rev-parse", "origin/production")
                self.assertNotEqual(before, remote)
                main = self.push_main_change("main-only.txt", "independent main work\n")
                checkpoint = self.checkpoint()
                if failure == "push":
                    hook = self.origin / "hooks/pre-receive"
                    _ = hook.write_text("#!/bin/sh\nexit 1\n")
                    hook.chmod(0o755)
                else:
                    real_git = shutil.which("git")
                    self.executable(self.bin / "checkpoint-test", STUB + '''
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
(state / "tested-tip").write_text(tip)
''')
                    self.executable(self.bin / "git", f'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
state = Path(os.environ["CHECKPOINT_TEST_STATE"])
if sys.argv[-3:] == ["fetch", "origin", "main"] and (state / "tested-tip").exists():
    print("fixture final fetch failed", file=sys.stderr)
    raise SystemExit(1)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
''')
                result = self.run_checkpoint(checkpoint)
                self.assertEqual(result.process.returncode, 1, result.process.stdout + result.process.stderr)
                self.assertIn("push: failed", result.process.stdout)
                if failure == "final-fetch":
                    self.assertIn("test: ok", result.process.stdout)
                    self.assertIn("fixture final fetch failed", result.process.stdout)
                    tested_tip = (self.state / "tested-tip").read_text()
                    self.assertEqual(self.git("merge-base", "--is-ancestor", checkpoint, tested_tip), "")
                    self.assertEqual(self.git("merge-base", "--is-ancestor", main, tested_tip), "")
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
                self.assertEqual((self.checkout / "local-only.txt").read_text(), "local commit must survive\n")
                self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), remote)
                self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), main)

    def import_production_paths(self, *, review_rows: bool = True) -> Path:
        self.write(self.other, "outside/imported.txt", "other production\n")
        self.write(self.other, "outside/second.txt", "second imported path\n")
        _ = self.git("add", "outside", cwd=self.other)
        _ = self.git("commit", "-m", "checkpoint(other): imported paths\n\nChange-Review: none — internal check", cwd=self.other)
        _ = self.git("push", "origin", "HEAD:refs/heads/other-production", cwd=self.other)
        _ = self.git("merge", "--no-ff", "-m", "merge other production", "origin/other-production", cwd=self.unit)
        doc = self.root / "other-production.md"
        lines = [f"- **Repository:** `{self.checkout}`", "- **Merge branch:** `other-production`"]
        if review_rows:
            lines.append(f"- **Change review:** `{self.root / 'other-review.md'}`")
        _ = doc.write_text("\n".join(lines) + "\n")
        return doc

    def test_scope_leaves_out_another_productions_paths(self) -> None:
        doc = self.import_production_paths()
        sha = self.checkpoint(subject="checkpoint(alpha): own work\n\nChange-Review: none — internal check")
        with mock.patch.object(showrunners, "timer_docs", return_value={"other": doc}):
            accepted = self.run_checkpoint(sha)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("scope: ok — 1 changed paths; 2 came from origin merge branches", accepted.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)
        self.assertEqual((self.checkout / "outside/imported.txt").read_text(), "other production\n")

    def test_scope_keeps_imported_path_changed_by_unit(self) -> None:
        doc = self.import_production_paths()
        sha = self.checkpoint("outside/imported.txt", "unit changed this\n")
        with mock.patch.object(showrunners, "timer_docs", return_value={"other": doc}):
            accepted = self.run_checkpoint(sha)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("scope: ok — 1 changed paths; 1 came from origin merge branches", accepted.process.stdout)
        self.assertEqual((self.checkout / "outside/imported.txt").read_text(), "unit changed this\n")

    def test_scope_keeps_deleted_plan_absent_at_an_origin_merge_base(self) -> None:
        doc = self.import_production_paths()
        with mock.patch.object(showrunners, "timer_docs", return_value={"other": doc}):
            code = self.run_checkpoint(self.checkpoint())
            self.assertEqual(code.process.returncode, 0, code.process.stdout + code.process.stderr)
            sha = self.as_built_commit()
            accepted = self.run_checkpoint(sha, "--as-built", review=False)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("scope: ok — 2 changed paths", accepted.process.stdout)
        self.assertIn("docs/plans/alpha.md", self.git(
            "diff", "--name-only", "--diff-filter=D", "HEAD^1", "HEAD").splitlines())

    def test_scope_leaves_out_own_origin_paths_not_in_local_merge_branch(self) -> None:
        self.write(self.other, "outside/own-production.txt", "already on own origin branch\n")
        _ = self.git("add", "outside/own-production.txt", cwd=self.other)
        _ = self.git("commit", "-m", "checkpoint(beta): own production work", cwd=self.other)
        _ = self.git("push", "origin", "HEAD:refs/heads/production", cwd=self.other)
        _ = self.git("merge", "--no-ff", "-m", "merge production", "origin/production", cwd=self.unit)
        sha = self.checkpoint()
        with mock.patch.object(showrunners, "timer_docs", return_value={}):
            accepted = self.run_checkpoint(sha)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("scope: ok — 1 changed paths; 1 came from origin merge branches", accepted.process.stdout)
        self.assertEqual((self.checkout / "outside/own-production.txt").read_text(), "already on own origin branch\n")

    def test_scope_leaves_out_paths_after_production_review_rows_are_added(self) -> None:
        doc = self.import_production_paths(review_rows=False)
        sha = self.checkpoint()
        with mock.patch.object(showrunners, "timer_docs", return_value={"other": doc}):
            _ = doc.write_text(doc.read_text() + f"- **Change review:** `{self.root / 'other-review.md'}`\n")
            accepted = self.run_checkpoint(sha)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("scope: ok — 1 changed paths; 2 came from origin merge branches", accepted.process.stdout)

    def test_same_checkpoint_rerun_skips_another_productions_reviewed_commits(self) -> None:
        from scripts.production import change_review

        self.write(self.other, "crates/alpha/imported.txt", "other production\n")
        _ = self.git("add", "crates/alpha/imported.txt", cwd=self.other)
        _ = self.git("commit", "-m", "checkpoint(other): already on another production", cwd=self.other)
        foreign = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("push", "origin", "HEAD:refs/heads/other-production", cwd=self.other)
        _ = self.git("fetch", "origin", cwd=self.unit)
        _ = self.git("merge", "--no-ff", "-m", "merge other production", "origin/other-production", cwd=self.unit)
        sha = self.checkpoint(subject="checkpoint(alpha): own work\n\nChange-Review: none — internal check")
        other_doc = self.root / "other-production.md"
        _ = other_doc.write_text(f"- **Repository:** `{self.checkout}`\n"
                                 + "- **Merge branch:** `other-production`\n"
                                 + f"- **Change review:** `{self.root / 'other-review.md'}`\n")
        with mock.patch.object(showrunners, "timer_docs", return_value={}):
            held = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(held, "held", "change review: held")
        self.assertIn(foreign[:change_review.SHORT], held.process.stdout)
        with mock.patch.object(showrunners, "timer_docs", return_value={"other": other_doc}):
            accepted = self.run_checkpoint(sha)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("change review: ok — 1 unit commit(s)", accepted.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)

        for subject, count in (("checkpoint(alpha): final change", 1), ("queue(7): final change", 2)):
            with self.subTest(subject=subject):
                _ = self.checkpoint(content=f"draft for {subject}\n",
                                    subject="fix(alpha): draft\n\nChange-Review: Draft change | Read | Draft result")
                final = self.checkpoint(content=f"final for {subject}\n",
                                        subject=f"{subject}\n\nChange-Review: Final change | Read | Final result")
                with mock.patch.object(showrunners, "timer_docs", return_value={"other": other_doc}):
                    accepted = self.run_checkpoint(final)
                self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
                self.assertIn(f"change review: ok — {count} unit commit(s)", accepted.process.stdout)

    def test_missing_hash_and_unpushed_checkpoint_stop_at_ancestry(self) -> None:
        missing = self.run_checkpoint("0" * 40)
        self.assert_stopped_before_merge(missing, "failed", "commit")
        self.assertIn("push", missing.process.stdout)
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(unpushed, "held", "origin")
        self.assertIn("push", unpushed.process.stdout)
    def test_never_pushed_unit_branch_is_held_like_an_unpushed_checkpoint(self) -> None:
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        _ = self.git("push", "origin", "--delete", "alpha-branch", cwd=self.unit)
        never_pushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(never_pushed, "held", "ancestry: held — commit is not on origin")
        self.assertIn("send alpha: push your branch to origin and resend the checkpoint",
                      never_pushed.process.stdout)
        self.assertEqual(never_pushed.process.stdout, unpushed.process.stdout)
    def test_checkpoint_on_second_origin_branch_merges_the_checkpoint_and_names_branch(self) -> None:
        for outcome in ("live-owner", "ancestor", "equal-patch", "merge-branch", "moved", "unlanded",
                        "delete-failed", "no-promotion"):
            with self.subTest(outcome=outcome):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                _ = self.git("checkout", "-b", "alpha-phase", cwd=self.unit)
                sha = self.checkpoint(push=False)
                # The request can name a commit before the branch tip.
                later = self.checkpoint("outside.txt", "later work\n", push=False)
                _ = self.git("push", "origin", "alpha-phase", cwd=self.unit)
                if outcome not in ("live-owner", "equal-patch", "unlanded"):
                    _ = self.git("update-ref", "refs/heads/alpha-phase", sha, cwd=self.origin)
                if outcome != "live-owner":
                    _ = self.git("checkout", "alpha-branch", cwd=self.unit)
                if outcome == "equal-patch":
                    _ = self.git("cherry-pick", later, cwd=self.promote)
                    _ = self.git("push", "origin", "main", cwd=self.promote)
                if outcome == "merge-branch":
                    doc = self.root / "other-production.md"
                    _ = doc.write_text("- **Merge branch:** alpha-phase\n")
                    _ = recorded_showrunner("other", doc)
                if outcome == "moved":
                    self.executable(self.checkout / ".git/hooks/pre-push", "#!/bin/sh\n"
                        + 'read local_ref local_sha remote_ref remote_sha\n'
                        + 'if [ "$local_sha" = 0000000000000000000000000000000000000000 ] '
                        + '&& [ "$remote_ref" = refs/heads/alpha-phase ]; then\n'
                        + f"git --git-dir={shlex.quote(str(self.origin))} update-ref refs/heads/alpha-phase {later} {sha}\n"
                        + "fi\nexit 0\n")
                if outcome == "delete-failed":
                    self.executable(self.origin / "hooks/pre-receive", "#!/bin/sh\n"
                        + 'read old new ref\n'
                        + '[ "$new" != 0000000000000000000000000000000000000000 ]\n')
                if outcome != "no-promotion":
                    _ = self.doc.write_text(self.doc.read_text() + f"\n- **Promote:** {self.promote}\n")
                _ = self.git("config", "remote.origin.fetch",
                             "+refs/heads/alpha-branch:refs/remotes/origin/alpha-branch")
                if outcome != "no-promotion":
                    for branch in ("production", "main"):
                        _ = self.git("config", "--add", "remote.origin.fetch",
                                     f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
                result = self.run_checkpoint(sha)
                self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
                self.assertIn("ancestry: ok — checkpoint exists and is on origin/alpha-phase\n",
                              result.process.stdout)
                if outcome == "equal-patch":
                    self.assertEqual(self.git("merge-base", "--is-ancestor", sha, "HEAD"), "")
                    self.assertNotIn(later, self.git("rev-list", "HEAD").splitlines())
                else:
                    self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)
                    self.assertNotEqual(self.git("rev-parse", "HEAD^2"), later)
                self.assertEqual((self.checkout / "outside.txt").exists(), outcome == "equal-patch")
                self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), self.git("rev-parse", "HEAD"))
                remote = self.git("ls-remote", "--heads", "origin", "refs/heads/alpha-phase")
                if outcome in ("ancestor", "equal-patch"):
                    self.assertEqual(remote, "")
                    self.assertIn("branch: deleted — origin/alpha-phase", result.process.stdout)
                    if outcome == "equal-patch":
                        self.assertEqual(self.git("cherry", "origin/main", later).split()[0], "-")
                    rerun = self.run_checkpoint(sha)
                    self.assertEqual(rerun.process.returncode, 0, rerun.process.stdout + rerun.process.stderr)
                else:
                    expected = later if outcome in ("live-owner", "moved", "unlanded") else sha
                    self.assertEqual(remote.split()[0], expected)
                    reasons = {"live-owner": "present worktree", "merge-branch": "production merge branch",
                               "moved": "delete failed or branch moved", "unlanded": "work has not reached main",
                               "delete-failed": "delete failed", "no-promotion": "no promotion configured"}
                    self.assertIn(reasons[outcome], result.process.stdout)

    def test_checkpoint_on_no_origin_branch_checks_all_heads_and_still_holds(self) -> None:
        sha = self.checkpoint(push=False)
        # Simulate a deleted alternate branch with a stale local tracking ref.
        _ = self.git("push", "origin", f"{sha}:refs/heads/alpha-phase", cwd=self.unit)
        _ = self.git("update-ref", "refs/remotes/origin/alpha-phase", sha)
        _ = self.git("update-ref", "-d", "refs/heads/alpha-phase", cwd=self.origin)
        with mock.patch.object(merge_checkpoint, "git", wraps=merge_checkpoint.git) as commands:
            result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "held", "ancestry: held — commit is not on origin")
        self.assertIn("send alpha: push your branch to origin and resend the checkpoint", result.process.stdout)
        commands.assert_any_call(self.checkout, "fetch", "--prune", "origin",
                                 "+refs/heads/*:refs/remotes/origin/*")
        self.assertEqual(self.git("for-each-ref", "refs/remotes/origin/alpha-phase"), "")

    def test_second_origin_branch_is_accepted_when_the_unit_branch_was_deleted(self) -> None:
        sha = self.checkpoint(push=False)
        _ = self.git("push", "origin", "HEAD:refs/heads/alpha-phase", cwd=self.unit)
        _ = self.git("push", "origin", "--delete", "alpha-branch", cwd=self.unit)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("is on origin/alpha-phase", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)

    def test_unit_branch_on_origin_uses_the_fast_path(self) -> None:
        sha = self.checkpoint()
        with mock.patch.object(merge_checkpoint, "git", wraps=merge_checkpoint.git) as commands:
            result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("is on origin/alpha-branch", result.process.stdout)
        self.assertFalse(any(call.args[1:3] == ("fetch", "--prune") for call in commands.call_args_list))

    def test_unreachable_origin_still_fails_at_ancestry(self) -> None:
        sha = self.checkpoint()
        _ = self.git("remote", "set-url", "origin", str(self.root / "absent.git"))
        result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "failed", "ancestry: failed")
        self.assertIn("send alpha: the showrunner will inspect the failed command", result.process.stdout)
    def test_a_code_checkpoint_removes_its_phases_scratch_folders(self) -> None:
        plan = self.root / "scratch-plan.md"
        _ = plan.write_text("### Phase 2 — Checkpoint\n\n### Phase 3 — Later work\n")
        with mock.patch.dict(os.environ, self.env), mock.patch.object(scratch, "registered_plan", return_value=plan):
            done = [scratch.new("phase:alpha/2", word) for word in ("gate", "copy")]
            later = scratch.new("phase:alpha/3")
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scratch: ok — removed 2 folder(s) of phase:alpha/2: ", result.process.stdout)
        self.assertFalse(any(path.exists() for path in done))
        self.assertTrue(later.is_dir())
    def test_real_merge_tests_run_only_the_two_commands_before_commentary(self) -> None:
        self.production_doc(merge_tests_line=REAL_MERGE_TESTS)
        self.env["CLAUDE_PYTHON"] = sys.executable
        for name in ("python3", "basedpyright"):
            self.executable(self.bin / name, STUB)
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("python3"), [
            ["-m", "unittest", "discover", "-s", "scripts/buildlog", "-p", "test_*.py"],
        ])
        self.assertEqual(self.events("style_sync.py"), [
            ["notify", "--repo-dir", str(self.checkout),
             "--branch", "production", "--tip", self.git("rev-parse", "HEAD")],
        ])
        self.assertEqual(self.events("basedpyright"), [["scripts/buildlog"]])
        self.assertEqual(self.events("0"), [])
        all_args = [arg for event in (self.events("python3") + self.events("basedpyright")) for arg in event]
        self.assertFalse(any(".venv" in arg for arg in all_args))
        self.assertIn("test: ok", result.process.stdout)
    def test_merge_tests_that_are_not_commands_are_refused_by_name_before_the_merge(self) -> None:
        """cargo-liner wrote packages and a crate directory where the line wants commands."""
        (self.checkout / "crates/tui_pane").mkdir(parents=True)
        sha = self.checkpoint()
        for entry, detail in (("crates/tui_pane", "crates/tui_pane is neither on PATH"),
                              ("tui-pane-package", "tui-pane-package is neither on PATH"),
                              ("checkpoint-executable-that-does-not-exist", "does-not-exist is neither on PATH"),
                              ("$MERGE_TEST_UNSET/bin/tool --all", "names $MERGE_TEST_UNSET, which is not set")):
            with self.subTest(entry=entry):
                self.production_doc(merge_tests_line=f"- **Merge tests:** `checkpoint-test`, `{entry}`")
                result = self.run_checkpoint(sha)
                self.assertEqual(result.process.returncode, 1)
                self.assert_stopped_before_merge(result, "failed", f"Merge tests entry `{entry}`")
                self.assertIn(detail, result.process.stdout)
    def test_merge_tests_expand_home_and_variables(self) -> None:
        """The commands run with no shell, so ~ and $VAR are expanded before they run."""
        tools = Path(self.env["HOME"]) / "tools"
        tools.mkdir()
        self.executable(tools / "merge-tool", STUB)
        self.env["MERGE_TEST_DIR"] = "scripts/buildlog"
        self.production_doc(merge_tests_line="- **Merge tests:** `~/tools/merge-tool $MERGE_TEST_DIR`, "
                                             + "`$HOME/tools/merge-tool ${MERGE_TEST_DIR}/one ~/two`")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("merge-tool"),
                         [["scripts/buildlog"], ["scripts/buildlog/one", str(tools.parent / "two")]])
    def test_merge_tests_field_is_required_but_dash_runs_no_merge_test(self) -> None:
        sha = self.checkpoint()
        self.production_doc(merge_tests_line=None)
        absent = self.run_checkpoint(sha)
        self.assertEqual(absent.process.returncode, 1)
        self.assertIn("input: failed", absent.process.stdout)
        self.assertIn("production doc lacks Merge tests", absent.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), absent.before)
        self.production_doc(merge_tests_line="- **Merge tests:** —")
        empty = self.run_checkpoint(sha)
        self.assertEqual(empty.process.returncode, 0, empty.process.stderr + empty.process.stdout)
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertIn("test: ok — 0 commands", empty.process.stdout)
    def test_later_phase_uses_last_unit_checkpoint_for_ancestry(self) -> None:
        first_sha = self.checkpoint()
        first = self.run_checkpoint(first_sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        self.assertIn(f"({first_sha[:7]})", self.git("log", "-1", "--format=%s"))
        next_sha = self.checkpoint(content="phase three\n")
        later = self.run_checkpoint(next_sha, phase="3")
        self.assertEqual(later.process.returncode, 0, later.process.stderr + later.process.stdout)
        self.assertIn("ancestry: ok", later.process.stdout)
    def test_code_checkpoint_requires_review_trial_before_merge(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, review=False)
        self.assertIn("failed", result.process.stdout)
        self.assertIn("--review-trial", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])
    def test_alternate_review_trial_is_refused_and_canonical_numbers_reach_record(self) -> None:
        sha = self.checkpoint()
        alternate = self.run_checkpoint(sha, trial="review trial: UX 2, code 3, review 14 min, UX check 4 min, UX repair 1 min")
        self.assertEqual(alternate.process.returncode, 1)
        self.assert_stopped_before_merge(alternate, "failed", "review trial: ux <N> findings")
        self.assertIn("input: failed", alternate.process.stdout)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        for flag, value in (("--ux-findings", "2"), ("--code-findings", "3"),
                            ("--review-minutes", "14"), ("--ux-check-minutes", "4"),
                            ("--ux-repair-minutes", "1")):
            self.assertEqual(review[0][review[0].index(flag) + 1], value)
        self.assertNotIn("--simplify-findings", review[0])
        self.assertNotIn("--simplify-minutes", review[0])
        self.assertNotIn("--note", review[0])

    def test_a_failed_repair_review_add_keeps_the_first_log_until_a_retry_succeeds(self) -> None:
        first = self.run_checkpoint(self.checkpoint())
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        first_log = self.log.read_bytes()
        sha = self.checkpoint(content="alpha repair\n", subject="repair alpha")
        failure = self.state / "fail-review_regime.py"
        failure.touch()

        failed = self.run_checkpoint(sha)
        self.assertEqual(failed.process.returncode, 1, failed.process.stdout + failed.process.stderr)
        self.assertIn("record: failed", failed.process.stdout)
        self.assertNotIn("record: ok", failed.process.stdout)
        self.assertEqual(self.log.read_bytes(), first_log)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertNotEqual(pushed, failed.before)
        self.assertEqual(len(self.events("review_regime.py")), 2)

        failure.unlink()
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertIn("record: ok — review ledger and production log updated\n", resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(len(self.events("review_regime.py")), 3)
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], first_log.decode().strip())
        self.assertIn(f"alpha phase 2 ({sha}) merged as {pushed}; ", lines[1])


@final
class TestPassedTests(unittest.TestCase):
    VENV = "venv .venv subdirectory not found in venv path /checkout.\n"

    def judge(self, status: int, stdout: str, stderr: str = "") -> bool:
        result = subprocess.CompletedProcess(["basedpyright"], status, stdout, stderr)
        return checkpoint_gate.test_passed(result)

    def test_clean_basedpyright_requires_status_zero(self) -> None:
        self.assertTrue(self.judge(0, "0 errors, 0 warnings, 0 notes\n", self.VENV))
        self.assertFalse(self.judge(3, "0 errors, 0 warnings, 0 notes\n", self.VENV))

    def test_basedpyright_with_errors_fails(self) -> None:
        self.assertFalse(self.judge(1, "/checkout/a.py:1:1 - error: x\n2 errors, 0 warnings, 0 notes\n", self.VENV))
        self.assertFalse(self.judge(3, "2 errors, 0 warnings, 0 notes\n", self.VENV))

    def test_other_commands_pass_only_on_status_zero(self) -> None:
        result = subprocess.CompletedProcess(["python3"], 3, "0 errors, 0 warnings, 0 notes\n", "")
        self.assertFalse(checkpoint_gate.test_passed(result))
        self.assertTrue(checkpoint_gate.test_passed(subprocess.CompletedProcess(["python3"], 0, "", "")))


@final
class MacPromotionPathTests(unittest.TestCase):
    def remote_command(self, promote_line: str) -> str:
        promotion = production_doc.promotion_from_doc([f"- **Promote:** {promote_line}"])
        assert isinstance(promotion, production_doc.PromoteTo)
        assert isinstance(promotion.mac, production_doc.MacCheckout)
        done = subprocess.CompletedProcess(["ssh"], 0, "rc=0\n", "")
        with mock.patch("scripts.production.merge_checkpoint.subprocess.run", return_value=done) as run:
            _ = merge_checkpoint.pull_mac_checkout(promotion.mac.path, Path("/home/natepiano/.claude"))
        argv = cast(list[str], run.call_args.args[0])
        self.assertEqual(argv[:2], ["ssh", "mac"])
        return argv[2]

    def test_tilde_mac_path_is_expanded_by_the_mac_shell(self) -> None:
        remote = self.remote_command("mac `~/.claude`")
        self.assertTrue(remote.startswith('cd "$HOME"/.claude && tip=$(git ls-remote'), remote)
        self.assertNotIn(str(Path.home()) + "/.claude &&", remote)

    def test_absolute_mac_path_is_kept(self) -> None:
        remote = self.remote_command("/home/natepiano/.claude, mac /Users/someone/my repo")
        self.assertTrue(remote.startswith("cd '/Users/someone/my repo' && tip=$(git ls-remote"), remote)

    def test_mac_pull_queues_only_connection_failures_and_preserves_zone_notices(self) -> None:
        cases = (("offline", 255, "", "", main_publish.MacPullPending),
                 ("remote failure", 0, "rc=1\n", "", checkpoint_gate.Stop),
                 ("remote exit 255", 255, "rc=1\n", "", checkpoint_gate.Stop),
                 ("pulled", 0, "rc=0\n", "", main_publish.MacPulled),
                 ("pulled with zone", 0, "rc=0\n", "Zone changed", main_publish.MacPulled),
                 ("offline with zone", 255, "", "Zone changed", main_publish.MacPullPending))
        for label, code, output, text, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                ssh = state / "ssh"
                _ = ssh.write_text(f"#!{sys.executable}\nimport sys\n"
                                   + f"sys.stdout.write({output!r})\n"
                                   + f"sys.stderr.write({'Mac asleep' if code == 255 else 'pull refused'!r})\n"
                                   + f"sys.exit({code})\n")
                ssh.chmod(0o755)
                config = state / "merge_checkpoint.conf"
                _ = config.write_text("# whole: 1..\nMAC_PULL_TRIES=2\n"
                                      + "# range: 0..\nMAC_PULL_RETRY_SECONDS=0\n")
                with mock.patch.dict(os.environ, {"ZONE_STATE": str(state / "zone"), "CONF_DIR": folder,
                                                  "PATH": folder + os.pathsep + os.environ["PATH"]}):
                    waiting = main_publish.PendingMacPull("zone", text, "2026-11-08", "earlier failure")
                    if text or expected is not main_publish.MacPullPending:
                        main_publish.queue(waiting)
                    if expected is checkpoint_gate.Stop:
                        with self.assertRaises(checkpoint_gate.Stop):
                            _ = merge_checkpoint.pull_mac_checkout(PurePosixPath("~/.claude"), state)
                        self.assertEqual(main_publish.read_queue(), waiting)
                        continue
                    outcome = merge_checkpoint.pull_mac_checkout(PurePosixPath("~/.claude"), state)
                    self.assertIsInstance(outcome, expected)
                    pending = main_publish.read_queue()
                    if expected is main_publish.MacPullPending:
                        self.assertIsInstance(pending, main_publish.PendingMacPull)
                        assert isinstance(pending, main_publish.PendingMacPull)
                        self.assertEqual(pending.text, text)
                        self.assertIn("Mac asleep", pending.reason)
                        self.assertEqual(len(list((state / "zone").iterdir())), 1)
                        if text:
                            self.assertEqual((pending.label, pending.since), (waiting.label, waiting.since))
                    elif text:
                        waiting = main_publish.PendingMacPull(waiting.label, waiting.text, waiting.since, waiting.reason, ())
                        self.assertEqual(pending, waiting)
                    else:
                        self.assertIsInstance(pending, main_publish.NoPendingMacPull)

        from scripts.lib import zone

        for text in ("", "Zone changed"):
            with self.subTest(custom_checkout=True, text=text), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                ssh = state / "ssh"
                _ = ssh.write_text(f"#!{sys.executable}\n" + '''import json
from pathlib import Path
import sys
state = Path(__file__).parent
with (state / "ssh.log").open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if (state / "offline").exists():
    print("Mac asleep", file=sys.stderr)
    raise SystemExit(255)
print("mac: fixture - sent" if "broadcast.py" in sys.argv[-1] else "rc=0")
''')
                ssh.chmod(0o755)
                _ = (state / "merge_checkpoint.conf").write_text(
                    "# whole: 1..\nMAC_PULL_TRIES=1\n# range: 0..\nMAC_PULL_RETRY_SECONDS=0\n")
                offline = state / "offline"
                offline.touch()
                targets = ((PurePosixPath("~/custom checkout"), state / "first-source"),
                           (PurePosixPath("/Users/fixture/second checkout"), state / "second-source"))
                with mock.patch.dict(os.environ, {"ZONE_STATE": str(state / "zone"), "CONF_DIR": folder,
                                                  "ZONE_SSH": str(ssh),
                                                  "PATH": folder + os.pathsep + os.environ["PATH"]}):
                    if text:
                        main_publish.queue(main_publish.PendingMacPull("zone", text, "2026-11-08", "offline"))
                    for mac, source in targets:
                        self.assertIsInstance(merge_checkpoint.pull_mac_checkout(mac, source), main_publish.MacPullPending)
                    offline.unlink()
                    self.assertIsInstance(merge_checkpoint.pull_mac_checkout(PurePosixPath("~/.claude"), state),
                                          main_publish.MacPulled)
                    self.assertIsInstance(main_publish.read_queue(), main_publish.PendingMacPull)
                    before_drain = len((state / "ssh.log").read_text().splitlines())
                    _ = zone.drain(state, pulled=None)
                    commands = [cast(list[str], json.loads(line))[-1]
                                for line in (state / "ssh.log").read_text().splitlines()[before_drain:]]
                    for mac, source in targets:
                        self.assertTrue(any(f"cd {main_publish.remote_path(mac)} && tip=$(git ls-remote" in command
                                            and f"natedev:{source} main" in command for command in commands), commands)
                    if text:
                        self.assertTrue(any("broadcast.py" in command and text in command for command in commands), commands)
                    self.assertIsInstance(main_publish.read_queue(), main_publish.NoPendingMacPull)


if __name__ == "__main__":
    _ = unittest.main()
