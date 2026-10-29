"""Guild tip reads and head writes use local Git fixtures, never the real tips repo."""
from __future__ import annotations

import fcntl
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import override
from unittest import mock

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.guilds import tips  # noqa: E402

SCRIPT = Path(__file__).with_name("tips.py")

# Report an actual failed flock so the holder releases only after contention occurs.
LOCK_PUBLISH_DRIVER = """
import sys
sys.path.insert(0, sys.argv[1])
from scripts.guilds import tips
from types import SimpleNamespace
tips.GIT_TIMEOUT = 2 if sys.argv[2] == "expire" else float(sys.argv[2])
if sys.argv[2] == "expire":
    elapsed = 0.0
    def monotonic() -> float:
        return elapsed
    def retry_sleep(_delay: float) -> None:
        global elapsed
        elapsed += 1.0
        print("lock retry", flush=True)
    tips.time = SimpleNamespace(monotonic=monotonic, sleep=retry_sleep)
real_flock = tips.fcntl.flock
def observed_flock(fd: int, operation: int) -> None:
    try:
        real_flock(fd, operation)
    except BlockingIOError:
        print("lock blocked", flush=True)
        raise
tips.fcntl.flock = observed_flock
raise SystemExit(tips.main(sys.argv[3:]))
"""


def tip(head: str, text: str) -> str:
    return (f'---\ntags: [guild/{head}, guild-event, head/{head}]\n'
            'date_created: "[[2026-10-21]]"\ndate_modified: "[[2026-10-21]]"\n'
            f'---\n## Fixture tip\n{text}\nSource: fixture.\n')


class TipsTests(unittest.TestCase):
    root: Path = Path()
    origin: Path = Path()
    checkout: Path = Path()
    other: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "head"
        self.other = self.root / "other-head"
        self.environment = {**os.environ, "HOME": str(self.root), "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                            "GIT_AUTHOR_NAME": "Guild fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                            "GIT_COMMITTER_NAME": "Guild fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                            "GUILD_TIPS_REPO": str(self.checkout)}
        _ = self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        _ = self.git(self.root, "clone", str(self.origin), str(self.checkout))
        for guild in ("common", "designers", "reviewers", "showrunners", "units"):
            _ = self.write(self.checkout, f"guilds/{guild}/baseline.md", tip("units" if guild == "common" else guild,
                                                                       f"Committed {guild} evidence."))
        _ = self.write(self.checkout, "review-charter.md", "Original charter.\n")
        _ = self.git(self.checkout, "add", ".")
        _ = self.git(self.checkout, "commit", "-m", "Fixture baseline")
        _ = self.git(self.checkout, "push", "-u", "origin", "main")
        _ = self.git(self.root, "clone", str(self.origin), str(self.other))

    def git(self, directory: Path, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(directory), *arguments], env=self.environment,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, directory: Path, relative: str, text: str) -> Path:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return path

    def prepared(self, relative: str, text: str) -> Path:
        folder = self.root / "prepared"
        _ = self.write(folder, relative, text)
        return folder

    def invoke(self, *arguments: str, checkout: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, "GUILD_TIPS_REPO": str(checkout or self.checkout)}
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=environment,
                              capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)

    def publish(self, head: str, folder: Path, checkout: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.invoke("publish", "--head", head, "--from", str(folder), checkout=checkout)

    def assert_lock_released(self) -> None:
        common = Path(self.git(self.checkout, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.checkout / common
        with (common / "guild-tips.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

    def advance_origin_during_push(self, relative: str, text: str) -> None:
        """A competing head pushes after our pull, before our first push sends refs."""
        _ = self.write(self.other, relative, text)
        _ = self.git(self.other, "add", relative)
        _ = self.git(self.other, "commit", "-m", "Competing head edit")
        marker = self.root / "race-fired"
        hook = self.checkout / ".git/hooks/pre-push"
        q = shlex.quote
        _ = hook.write_text(f"#!/bin/sh\nif [ ! -e {q(str(marker))} ]; then\n"
                            + f"  touch {q(str(marker))}\n"
                            + "  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\n"
                            + f"  git -C {q(str(self.other))} push origin main >&2 || exit 1\nfi\n", encoding="utf-8")
        hook.chmod(0o755)

    def test_two_heads_publish_then_load_reads_only_common_and_own_committed_tips(self) -> None:
        designers = self.prepared("designers/new.md", tip("designers", "Designer revision evidence."))
        first = self.publish("designers", designers)
        self.assertEqual(first.returncode, 0, first.stderr)
        reviewers = self.root / "reviewer-prepared"
        _ = self.write(reviewers, "reviewers/new.md", tip("reviewers", "Reviewer citation evidence."))
        second = self.publish("reviewers", reviewers, self.other)
        self.assertEqual(second.returncode, 0, second.stderr)
        expected = self.git(self.other, "rev-parse", "HEAD")
        _ = self.write(self.checkout, "guilds/designers/baseline.md", "Uncommitted replacement.\n")
        loaded = self.invoke("load", "--guild", "designers")
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(loaded.stdout.splitlines()[0], f"commit {expected}")
        for text in ("guilds/common/baseline.md", "guilds/designers/new.md", "Designer revision evidence.",
                     "Committed designers evidence."):
            self.assertIn(text, loaded.stdout)
        self.assertNotIn("Reviewer citation evidence.", loaded.stdout)
        self.assertNotIn("Uncommitted replacement.", loaded.stdout)
        self.assertIn(expected, second.stdout)
        self.assert_lock_released()

    def test_failed_pull_names_the_local_commit_and_loads_its_tip_bytes(self) -> None:
        expected = self.git(self.checkout, "rev-parse", "HEAD")
        _ = self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "absent.git"))
        loaded = self.invoke("load", "--guild", "units")
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(loaded.stdout.splitlines()[0], f"commit {expected}")
        self.assertIn("Committed units evidence.", loaded.stdout)
        self.assertEqual(len(loaded.stderr.strip().splitlines()), 1, loaded.stderr)
        self.assert_lock_released()

    def test_failed_lock_uses_the_local_baseline(self) -> None:
        expected = self.git(self.checkout, "rev-parse", "HEAD")
        (self.checkout / ".git/guild-tips.lock").mkdir()
        loaded = self.invoke("load", "--guild", "reviewers")
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(loaded.stdout.splitlines()[0], f"commit {expected}")
        self.assertIn("Committed reviewers evidence.", loaded.stdout)
        self.assertEqual(len(loaded.stderr.strip().splitlines()), 1)

    def test_missing_baseline_refuses_start(self) -> None:
        _ = self.git(self.checkout, "rm", "-r", "guilds")
        _ = self.git(self.checkout, "commit", "-m", "Remove fixture baseline")
        _ = self.git(self.checkout, "push")
        loaded = self.invoke("load", "--guild", "designers")
        self.assertEqual(loaded.returncode, 2)
        self.assertIn("restore the guild tips baseline before starting", loaded.stderr)
        self.assert_lock_released()

    def test_publish_preserves_unrelated_dirty_and_untracked_paths(self) -> None:
        _ = self.write(self.checkout, "review-charter.md", "User edit must stay.\n")
        _ = self.write(self.checkout, ".obsidian/workspace.json", "Private fixture state.\n")
        folder = self.prepared("units/new.md", tip("units", "Head-owned change."))
        result = self.publish("units", folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.checkout, "show", "HEAD:review-charter.md"), "Original charter.")
        self.assertEqual((self.checkout / "review-charter.md").read_text(), "User edit must stay.\n")
        self.assertEqual((self.checkout / ".obsidian/workspace.json").read_text(), "Private fixture state.\n")
        self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-only"), "")
        self.assertEqual(self.git(self.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         "guilds/units/new.md")

    def test_staged_index_and_dirty_target_are_refused_without_changing_them(self) -> None:
        folder = self.prepared("units/baseline.md", tip("units", "Prepared replacement."))
        before = self.git(self.checkout, "rev-parse", "HEAD")
        _ = self.write(self.checkout, "review-charter.md", "Staged user edit.\n")
        _ = self.git(self.checkout, "add", "review-charter.md")
        staged = self.publish("units", folder)
        self.assertEqual(staged.returncode, 1)
        self.assertIn("review-charter.md", staged.stderr)
        self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-only"), "review-charter.md")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
        self.assert_lock_released()
        # Use the second clean clone rather than clearing the first clone's index.
        _ = self.write(self.other, "guilds/units/baseline.md", "Local target edit.\n")
        dirty = self.publish("units", folder, self.other)
        self.assertEqual(dirty.returncode, 1)
        self.assertIn("guilds/units/baseline.md", dirty.stderr)
        self.assertEqual((self.other / "guilds/units/baseline.md").read_text(), "Local target edit.\n")
        self.assertEqual(self.git(self.other, "rev-parse", "HEAD"), before)

    def test_existing_ignored_target_is_refused_without_overwriting_its_bytes(self) -> None:
        relative = "guilds/units/ignored.md"
        _ = self.write(self.checkout, ".gitignore", relative + "\n")
        _ = self.git(self.checkout, "add", ".gitignore")
        _ = self.git(self.checkout, "commit", "-m", "Ignore fixture private tip")
        _ = self.git(self.checkout, "push")
        target = self.write(self.checkout, relative, "Private ignored bytes.\n")
        folder = self.prepared("units/ignored.md", tip("units", "Prepared replacement."))
        before = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.publish("units", folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn(relative, result.stderr)
        self.assertEqual(target.read_bytes(), b"Private ignored bytes.\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-only"), "")
        self.assert_lock_released()

    def test_common_tip_ownership_ignores_head_names_in_yaml_comments(self) -> None:
        cases = (
            ("tags: [guild/common] # head/designers", False),
            ('tags: ["guild/common", "head/designers", "label#part"] # head/reviewers', True),
            ("tags:\n  - guild/common # head/designers", False),
            ('tags:\n  - "guild/common"\n  - "head/designers" # head/reviewers', True),
        )
        for index, (tags, permitted) in enumerate(cases):
            with self.subTest(tags=tags):
                relative = f"common/comment-{index}.md"
                text = (f'---\n{tags}\ndate_created: "[[2026-10-21]]"\n'
                        'date_modified: "[[2026-10-21]]"\n---\n## Comment ownership\n'
                        'A YAML comment cannot assign a head.\nSource: fixture.\n')
                folder = self.root / f"comment-prepared-{index}"
                _ = self.write(folder, relative, text)
                before = self.git(self.checkout, "rev-parse", "HEAD")
                result = self.publish("designers", folder)
                self.assertEqual(result.returncode, 0 if permitted else 1, result.stderr)
                if permitted:
                    self.assertEqual(self.git(self.origin, "show", f"main:guilds/{relative}"), text.strip())
                else:
                    self.assertFalse((self.checkout / "guilds" / relative).exists())
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
                self.assert_lock_released()

    def test_publish_waits_for_a_busy_lock_and_refuses_when_the_wait_expires(self) -> None:
        folder = self.prepared("units/after-lock.md", tip("units", "Published after contention."))
        command = [sys.executable, "-c", LOCK_PUBLISH_DRIVER, str(SCRIPT.resolve().parents[2]), str(TEST_HANG_GUARD_SECS),
                   "publish", "--head", "units", "--from", str(folder)]
        lock_path = self.checkout / ".git/guild-tips.lock"
        with lock_path.open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as publisher:
                if publisher.stdout is None:
                    self.fail("The publisher has no output pipe")
                self.assertEqual(publisher.stdout.readline(), "lock blocked\n")
                fcntl.flock(holder, fcntl.LOCK_UN)
                output, errors = publisher.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(publisher.returncode, 0, errors)
                self.assertIn(self.git(self.origin, "rev-parse", "main"), output)
        self.assertIn("Published after contention.", self.git(self.origin, "show", "main:guilds/units/after-lock.md"))
        blocked_folder = self.root / "blocked-prepared"
        _ = self.write(blocked_folder, "units/blocked.md", tip("units", "Must not be written."))
        before = self.git(self.checkout, "rev-parse", "HEAD")
        command[4] = "expire"
        command[-1] = str(blocked_folder)
        with lock_path.open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            result = subprocess.run(command, env=self.environment, capture_output=True, text=True,
                                    check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("lock", result.stderr.lower())
        self.assertEqual(result.stdout.splitlines().count("lock retry"), 2)
        self.assertEqual(result.stdout.splitlines().count("lock blocked"), 3)
        self.assertFalse((self.checkout / "guilds/units/blocked.md").exists())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-only"), "")
        self.assert_lock_released()

    def test_load_waits_for_a_busy_lock_then_falls_back_only_when_its_wait_expires(self) -> None:
        expected = self.git(self.checkout, "rev-parse", "HEAD")
        lock_path = self.checkout / ".git/guild-tips.lock"
        real_flock = fcntl.flock
        with (lock_path.open("a") as holder, mock.patch.dict(os.environ, self.environment),
              mock.patch.object(tips, "LOAD_LOCK_TIMEOUT", TEST_HANG_GUARD_SECS)):
            real_flock(holder, fcntl.LOCK_EX)

            def release_after_contention(fd: int, operation: int) -> None:
                try:
                    real_flock(fd, operation)
                except BlockingIOError:
                    real_flock(holder, fcntl.LOCK_UN)
                    raise

            warnings = io.StringIO()
            with (mock.patch.object(fcntl, "flock", side_effect=release_after_contention),
                  redirect_stderr(warnings)):
                loaded = tips.load("units")
            self.assertEqual(loaded.splitlines()[0], f"commit {expected}")
            self.assertIn("Committed units evidence.", loaded)
            self.assertEqual(warnings.getvalue(), "")

            # Keep the real holder locked for the entire second call.
            real_flock(holder, fcntl.LOCK_EX)
            warnings = io.StringIO()
            readings = iter((0.0, 0.0, float(TEST_HANG_GUARD_SECS)))
            delays: list[float] = []
            lock_clock = SimpleNamespace(monotonic=lambda: next(readings), sleep=delays.append)
            with (redirect_stderr(warnings),
                  mock.patch("scripts.guilds.tips.time", lock_clock),
                  mock.patch.object(fcntl, "flock", wraps=real_flock) as attempts):
                fallback = tips.load("units")
            self.assertEqual(delays, [0.05])
            self.assertEqual(attempts.call_count, 2)
            self.assertEqual(fallback, loaded)
            self.assertEqual(len(warnings.getvalue().strip().splitlines()), 1)
            self.assertIn("lock", warnings.getvalue().lower())
            self.assertIn(expected, warnings.getvalue())
        self.assert_lock_released()

    def test_head_cannot_write_another_guild_or_another_heads_common_tip(self) -> None:
        for relative in ("reviewers/new.md", "common/baseline.md"):
            with self.subTest(relative=relative):
                folder = self.root / re.sub(r"[^a-z]", "_", relative)
                _ = self.write(folder, relative, tip("reviewers", "Foreign edit."))
                before = self.git(self.checkout, "rev-parse", "HEAD")
                result = self.publish("designers", folder)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
                self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
                self.assert_lock_released()

    def test_accepting_head_can_publish_its_common_tip_and_views(self) -> None:
        folder = self.prepared("common/baseline.md", tip("units", "Shared report evidence revision."))
        _ = self.write(folder, "units_base/events.base", 'views:\n  - type: table\n    name: Events\n')
        result = self.publish("units", folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Shared report evidence revision.", self.git(self.origin, "show", "main:guilds/common/baseline.md"))
        self.assertIn("type: table", self.git(self.origin, "show", "main:guilds/units_base/events.base"))
        self.assertEqual(self.git(self.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines(),
                         ["guilds/common/baseline.md", "guilds/units_base/events.base"])

    def test_publish_pull_failure_leaves_prepared_edit_unapplied_and_unlocks(self) -> None:
        before = self.git(self.checkout, "rev-parse", "HEAD")
        _ = self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "absent.git"))
        folder = self.prepared("designers/new.md", tip("designers", "Prepared but not published."))
        result = self.publish("designers", folder)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(f"commit {before}", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before)
        self.assertFalse((self.checkout / "guilds/designers/new.md").exists())
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assert_lock_released()

    def test_rejected_push_rebases_only_its_edit_and_retries_normal_push(self) -> None:
        self.advance_origin_during_push("guilds/reviewers/race.md", tip("reviewers", "Concurrent reviewer tip."))
        folder = self.prepared("designers/new.md", tip("designers", "Local designer tip."))
        result = self.publish("designers", folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        remote = self.git(self.origin, "rev-parse", "main")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), remote)
        self.assertIn(remote, result.stdout)
        self.assertIn("Concurrent reviewer tip.", self.git(self.origin, "show", "main:guilds/reviewers/race.md"))
        self.assertIn("Local designer tip.", self.git(self.origin, "show", "main:guilds/designers/new.md"))
        self.assertEqual(self.git(self.checkout, "rev-list", "--count", "HEAD"), "3")
        self.assertFalse((self.checkout / ".git/rebase-merge").exists())
        self.assert_lock_released()

    def test_rebase_conflict_aborts_keeps_local_commit_and_reports_to_showrunner(self) -> None:
        self.advance_origin_during_push("guilds/designers/baseline.md", tip("designers", "Remote replacement."))
        folder = self.prepared("designers/baseline.md", tip("designers", "Local conflicting replacement."))
        result = self.publish("designers", folder)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("showrunner", result.stderr)
        self.assertIn("Local conflicting replacement.", self.git(self.checkout, "show", "HEAD:guilds/designers/baseline.md"))
        self.assertIn("Remote replacement.", self.git(self.origin, "show", "main:guilds/designers/baseline.md"))
        self.assertNotEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "main"))
        self.assertFalse((self.checkout / ".git/rebase-merge").exists())
        self.assertFalse((self.checkout / ".git/rebase-apply").exists())
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assert_lock_released()

    def test_rejected_push_with_unrelated_dirty_work_refuses_rebase_and_preserves_it(self) -> None:
        self.advance_origin_during_push("guilds/reviewers/race.md", tip("reviewers", "Remote review tip."))
        _ = self.write(self.checkout, "review-charter.md", "User charter edit.\n")
        folder = self.prepared("units/new.md", tip("units", "Local tip retained after rejection."))
        result = self.publish("units", folder)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("review-charter.md", result.stderr)
        self.assertEqual((self.checkout / "review-charter.md").read_text(), "User charter edit.\n")
        self.assertIn("Local tip retained after rejection.", self.git(self.checkout, "show", "HEAD:guilds/units/new.md"))
        self.assertNotEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "main"))
        self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-only"), "")
        self.assertFalse((self.checkout / ".git/rebase-merge").exists())
        self.assert_lock_released()

    def test_divergence_with_foreign_unpublished_work_is_refused(self) -> None:
        _ = self.write(self.checkout, "review-charter.md", "Foreign unpublished charter.\n")
        _ = self.git(self.checkout, "add", "review-charter.md")
        _ = self.git(self.checkout, "commit", "-m", "Foreign unpublished work")
        foreign = self.git(self.checkout, "rev-parse", "HEAD")
        _ = self.write(self.other, "guilds/reviewers/remote.md", tip("reviewers", "Remote work."))
        _ = self.git(self.other, "add", ".")
        _ = self.git(self.other, "commit", "-m", "Remote work")
        _ = self.git(self.other, "push")
        folder = self.prepared("units/new.md", tip("units", "Prepared head tip."))
        result = self.publish("units", folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("review-charter.md", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), foreign)
        self.assertFalse((self.checkout / "guilds/units/new.md").exists())
        self.assert_lock_released()


if __name__ == "__main__":
    _ = unittest.main()
