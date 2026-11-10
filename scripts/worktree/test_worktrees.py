"""Test worktree creation and removal through the public commands."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import NamedTuple, TypedDict, cast, override
import unittest
from uuid import uuid4


CHECKOUT = Path(__file__).resolve().parents[2]
MAKE_WORKTREE = CHECKOUT / "scripts/worktree/make_worktree.sh"

END_LANDED = """
import json
from pathlib import Path
import sys
from scripts.worktree.worktrees import Ended, end_landed

results = end_landed(Path(sys.argv[1]), sys.argv[2])
print("RESULT=" + json.dumps([
    {"kind": type(result).__name__, "path": str(result.path),
     "detail": result.branch if isinstance(result, Ended) else result.reason}
    for result in results
]))
"""


class MadeWorktree(TypedDict):
    version: int
    path: str
    branch: str
    repo: str
    base: str
    merge_branch: str
    owner: str
    made_at: str


class RepositoryState(NamedTuple):
    worktrees: str
    branches: str
    origin_branches: str
    records: dict[str, bytes]
    folders: tuple[str, ...]


class EndResult(TypedDict):
    kind: str
    path: str
    detail: str


class WorktreeLifecycleTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    origin: Path = Path()
    repository: Path = Path()
    merge_tree: Path = Path()
    trees: Path = Path()
    state: Path = Path()
    environment: dict[str, str] = {}
    base: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="worktree-lifecycle-")))
        self.home = self.root / "home"
        self.home.mkdir()
        scripts = self.home / ".claude/scripts"
        scripts.parent.mkdir()
        scripts.symlink_to(CHECKOUT / "scripts", target_is_directory=True)
        self.origin = self.root / "origin.git"
        self.repository = self.root / "clone"
        self.merge_tree = self.root / "integration"
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.state = self.root / "worktrees-state"
        config = self.root / "staff.conf"
        _ = config.write_text("# range: 0.001..\nOPERATION_LOCK_SECONDS=5\n"
                              + "# range: 0.001..\nREMOTE_SECONDS=5\n")
        staff_file = self.root / "staff.json"
        self.environment = {
            "HOME": str(self.home), "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "WORKTREES_STATE_DIR": str(self.state),
            "STAFF_CONFIG": str(config), "STAFF_FILE": str(staff_file),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_PYTHON": sys.executable, "PYTHONPATH": str(CHECKOUT),
            "HERDR_BIN": "/nonexistent/herdr",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Worktree fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Worktree fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        _ = self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        _ = self.git(self.root, "clone", str(self.origin), str(self.repository))
        _ = (self.repository / "code.txt").write_text("base\n")
        document = self.repository / "docs/production.md"
        document.parent.mkdir()
        _ = document.write_text("# Fixture production\n")
        _ = (self.repository / ".unit.toml").write_text('plan = "standby"\nresponsibilities = []\n')
        _ = self.git(self.repository, "add", ".")
        _ = self.git(self.repository, "commit", "-m", "Create the fixture")
        self.base = self.git(self.repository, "rev-parse", "HEAD")
        _ = self.git(self.repository, "push", "-u", "origin", "main")
        _ = self.git(self.repository, "remote", "set-head", "origin", "main")
        _ = self.git(self.repository, "worktree", "add", str(self.merge_tree), "-b", "integration")
        _ = self.git(self.merge_tree, "push", "-u", "origin", "integration")
        _ = self.git(self.repository, "checkout", "-b", "unit-home")
        machine = socket.gethostname().split(".", 1)[0]
        runner_id, unit_id = str(uuid4()), str(uuid4())
        rows: list[dict[str, object]] = [
            {"id": runner_id, "current_session": runner_id, "kind": "showrunner",
             "name": "fixture-production", "parent": None,
             "doc": {"machine": machine, "path": str(self.merge_tree / "docs/production.md"), "state": "live"}},
            {"id": unit_id, "current_session": unit_id, "kind": "unit",
             "name": "fixture-unit", "parent": runner_id,
             "worktree": {"machine": machine, "path": str(self.repository), "state": "present"}},
        ]
        _ = staff_file.write_text(json.dumps({"schema": 2, "rows": rows}))

    def command(self, folder: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, cwd=folder, env=self.environment,
                              capture_output=True, text=True, check=False, timeout=20)

    def git(self, folder: Path, *arguments: str) -> str:
        result = self.command(folder, ["git", *arguments])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def finish_landed(self) -> list[EndResult]:
        python = self.home / ".claude/scripts/lib/py"
        result = self.command(self.repository, [str(python), "-c", END_LANDED,
                                               str(self.repository), "integration"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = next(line.removeprefix("RESULT=") for line in result.stdout.splitlines()
                       if line.startswith("RESULT="))
        return cast(list[EndResult], json.loads(payload))

    def make(self, tree: Path, branch: str) -> None:
        result = self.command(self.repository, ["bash", str(MAKE_WORKTREE), str(tree), branch])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def commit(self, tree: Path, path: str, contents: str) -> str:
        destination = tree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(contents)
        _ = self.git(tree, "add", path)
        _ = self.git(tree, "commit", "-m", f"Change {path}")
        return self.git(tree, "rev-parse", "HEAD")

    def publish(self, tree: Path, branch: str) -> None:
        _ = self.git(tree, "push", "-u", "origin", branch)

    def land(self, branch: str) -> None:
        _ = self.git(self.merge_tree, "merge", "--no-ff", branch, "-m", "Merge the checkpoint")
        self.publish(self.merge_tree, "integration")

    def record_path(self, tree: Path) -> Path:
        digest = hashlib.sha256(str(tree.absolute()).encode()).hexdigest()
        return self.state / "made" / f"{digest}.json"

    def record(self, tree: Path) -> MadeWorktree:
        return cast(MadeWorktree, json.loads(self.record_path(tree).read_text()))

    def repository_state(self) -> RepositoryState:
        return RepositoryState(
            self.git(self.repository, "worktree", "list", "--porcelain"),
            self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            {path.name: path.read_bytes() for path in (self.state / "made").glob("*.json")},
            tuple(sorted(path.name for path in self.trees.iterdir())),
        )

    def assert_ended(self, tree: Path, branch: str) -> None:
        state = self.repository_state()
        self.assertNotIn(f"worktree {tree}\n", state.worktrees + "\n")
        self.assertNotIn(f"refs/heads/{branch}", [line.split()[0] for line in state.branches.splitlines()])
        self.assertNotIn(f"refs/heads/{branch}", [line.split()[0] for line in state.origin_branches.splitlines()])
        self.assertNotIn(self.record_path(tree).name, state.records)
        self.assertNotIn(tree.name, state.folders)

    def test_make_records_the_staff_owner_and_merge_branch(self) -> None:
        for branch in ("recorded", "record-unwritable"):
            with self.subTest(branch=branch):
                tree = self.trees / branch
                if branch == "record-unwritable":
                    blocked_state = self.root / "blocked-state"
                    _ = blocked_state.write_text("retain this file\n")
                    self.environment["WORKTREES_STATE_DIR"] = str(blocked_state)
                    retained = self.repository_state()

                    result = self.command(self.repository, ["bash", str(MAKE_WORKTREE), str(tree), branch])

                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.repository_state(), retained)
                    self.assertEqual(blocked_state.read_text(), "retain this file\n")
                else:
                    before = datetime.now(UTC)
                    self.make(tree, branch)
                    after = datetime.now(UTC)
                    record = self.record(tree)
                    self.assertEqual({key: value for key, value in record.items() if key != "made_at"}, {
                        "version": 1, "path": str(tree), "branch": branch, "repo": str(self.repository),
                        "base": self.base, "merge_branch": "integration", "owner": "fixture-unit",
                    })
                    made_at = datetime.fromisoformat(record["made_at"])
                    self.assertGreaterEqual(made_at, before.replace(microsecond=0))
                    self.assertLessEqual(made_at, after)
                    self.assertEqual((tree / "code.txt").read_text(), "base\n")
                    self.assertEqual(self.git(tree, "branch", "--show-current"), branch)

    def test_merged_checkpoint_and_plan_shrink_end_all_recorded_items(self) -> None:
        tree, branch = self.trees / "checkpoint", "checkpoint"
        self.make(tree, branch)
        _ = self.commit(tree, "code.txt", "checkpoint\n")
        self.publish(tree, branch)
        self.land(branch)
        _ = self.commit(tree, "docs/plans/phase.md", "# Completed phase\n")
        self.publish(tree, branch)

        results = self.finish_landed()

        self.assertEqual(results, [{"kind": "Ended", "path": str(tree), "detail": branch}])
        self.assertEqual((self.merge_tree / "code.txt").read_text(), "checkpoint\n")
        self.assert_ended(tree, branch)

    def test_cherry_picked_content_ends_under_a_different_commit_hash(self) -> None:
        tree, branch = self.trees / "cherry", "cherry"
        self.make(tree, branch)
        checkpoint = self.commit(tree, "code.txt", "cherry-picked\n")
        self.publish(tree, branch)
        _ = self.git(self.merge_tree, "cherry-pick", "-x", checkpoint)
        self.publish(self.merge_tree, "integration")

        results = self.finish_landed()

        self.assertEqual(results, [{"kind": "Ended", "path": str(tree), "detail": branch}])
        self.assertEqual((self.merge_tree / "code.txt").read_text(), "cherry-picked\n")
        self.assert_ended(tree, branch)

    def test_unlanded_commit_is_reported_and_preserves_all_items(self) -> None:
        tree, branch = self.trees / "unlanded", "unlanded"
        self.make(tree, branch)
        checkpoint = self.commit(tree, "code.txt", "unlanded work\n")
        self.publish(tree, branch)
        before = self.repository_state()

        results = self.finish_landed()

        self.assertEqual([(row["kind"], row["path"]) for row in results], [("Kept", str(tree))])
        self.assertIn(checkpoint, results[0]["detail"])
        self.assertEqual(self.repository_state(), before)
        self.assertEqual((tree / "code.txt").read_text(), "unlanded work\n")

    def test_dirty_and_staff_held_trees_preserve_all_items(self) -> None:
        for reason in ("dirty", "staff-held", "switched", "detached", "no-commit", "target-merge-only"):
            with self.subTest(reason=reason):
                tree = self.trees / reason
                self.make(tree, reason)
                if reason not in ("no-commit", "target-merge-only"):
                    _ = self.commit(tree, f"{reason}.txt", "landed work\n")
                    self.publish(tree, reason)
                    self.land(reason)
                if reason == "dirty":
                    _ = (tree / "code.txt").write_text("uncommitted work\n")
                elif reason == "staff-held":
                    staff_file = Path(self.environment["STAFF_FILE"])
                    record = cast(dict[str, object], json.loads(staff_file.read_text()))
                    rows = cast(list[dict[str, object]], record["rows"])
                    holder_id = str(uuid4())
                    rows.append({"id": holder_id, "current_session": holder_id, "kind": "unit",
                                 "name": "held-unit", "parent": rows[0]["id"],
                                 "worktree": {"machine": socket.gethostname().split(".", 1)[0],
                                              "path": str(tree), "state": "present"}})
                    _ = staff_file.write_text(json.dumps(record))
                elif reason == "switched":
                    _ = self.git(tree, "checkout", "-b", "replacement-branch")
                    _ = self.commit(tree, f"{reason}-work.txt", "unlanded replacement work\n")
                elif reason == "detached":
                    _ = self.git(tree, "checkout", "--detach")
                    _ = self.commit(tree, f"{reason}-work.txt", "unlanded replacement work\n")
                elif reason == "target-merge-only":
                    _ = self.commit(self.merge_tree, "target.txt", "new target work\n")
                    self.publish(self.merge_tree, "integration")
                    _ = self.git(tree, "fetch", "origin", "integration")
                    _ = self.git(tree, "merge", "--no-ff", "origin/integration", "-m", "Merge the target")
                before = self.repository_state()
                tip = self.git(tree, "rev-parse", "HEAD")

                results = self.finish_landed()

                selected = next(row for row in results if row["path"] == str(tree))
                self.assertEqual(selected["kind"], "Kept")
                self.assertTrue(selected["detail"], results)
                self.assertEqual(self.repository_state(), before)
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), tip)
                if reason == "no-commit":
                    self.assertEqual(selected["detail"], "no commit of its own")
                elif reason == "target-merge-only":
                    self.assertEqual(selected["detail"], "only merges of the target outside it")
                else:
                    self.assertEqual((tree / f"{reason}.txt").read_text(), "landed work\n")
                if reason == "dirty":
                    self.assertEqual((tree / "code.txt").read_text(), "uncommitted work\n")
                elif reason in ("switched", "detached"):
                    self.assertEqual((tree / f"{reason}-work.txt").read_text(), "unlanded replacement work\n")

    def test_partial_removal_resumes_from_the_retained_record(self) -> None:
        tree, branch = self.trees / "partial", "partial"
        self.make(tree, branch)
        checkpoint = self.commit(tree, "code.txt", "landed work\n")
        self.publish(tree, branch)
        self.land(branch)
        retained = self.record_path(tree).read_bytes()
        hook = self.origin / "hooks/pre-receive"
        _ = hook.write_text("#!/bin/sh\necho 'fixture refuses deletion' >&2\nexit 1\n")
        hook.chmod(0o755)

        interrupted = self.finish_landed()

        self.assertEqual([(row["kind"], row["path"]) for row in interrupted], [("Kept", str(tree))])
        self.assertIn("fixture refuses deletion", interrupted[0]["detail"])
        self.assertEqual(self.record_path(tree).read_bytes(), retained)
        self.assertEqual(self.git(self.origin, "rev-parse", f"refs/heads/{branch}"), checkpoint)
        partial = self.repository_state()
        self.assertNotIn(tree.name, partial.folders)
        self.assertNotIn(f"refs/heads/{branch}", [line.split()[0] for line in partial.branches.splitlines()])
        hook.unlink()

        results = self.finish_landed()

        self.assertEqual(results, [{"kind": "Ended", "path": str(tree), "detail": branch}])
        self.assert_ended(tree, branch)


if __name__ == "__main__":
    _ = unittest.main()
