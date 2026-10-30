"""Release steps finish in a disposable repository after a failed Git step."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase


class ReleaseRerunTests(ScriptRepositoryTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def test_a_created_release_tag_finishes_both_remote_pushes_on_rerun(self) -> None:
        expected_tags: list[str] = []
        for version, failed_push in (("1.0", ("push", "-u")), ("1.1", ("push", "origin", "v1.1"))):
            with self.subTest(failed_push=failed_push):
                _ = self.git("switch", "-C", f"release-{version}")
                self.fail_git(*failed_push)
                stopped = self.script("release/push_release.sh", version)
                self.assertNotEqual(stopped.returncode, 0)
                self.assertEqual(self.git("rev-parse", f"v{version}").stdout, self.git("rev-parse", "HEAD").stdout)
                self.assertEqual(self.git("ls-remote", "origin", f"refs/tags/v{version}").stdout, "")
                finished = self.script("release/push_release.sh", version)
                self.assertEqual(finished.returncode, 0, finished.stderr)
                refs = self.git("ls-remote", "origin", f"refs/heads/release-{version}", f"refs/tags/v{version}").stdout
                self.assertEqual(len(refs.splitlines()), 2)
                expected_tags.append(f"v{version}")
                self.assertEqual(self.git("tag").stdout.splitlines(), expected_tags)

    def test_a_release_tag_on_another_commit_is_refused(self) -> None:
        _ = self.git("tag", "v1.0")
        _ = self.git("switch", "-c", "release-1.0")
        _ = self.write(self.repo / "seed.txt", "changed\n")
        _ = self.git("commit", "-am", "next")
        result = self.script("release/push_release.sh", "1.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another commit", result.stderr)

    def test_restoring_unreleased_finishes_a_failed_commit_or_push_without_duplicate_sections(self) -> None:
        changelog = self.write(self.repo / "CHANGELOG.md", f"## [1.0] - {date.today()}\n\nrelease\n")
        _ = self.git("add", "CHANGELOG.md")
        _ = self.git("commit", "-m", "release")
        self.fail_git("commit")
        stopped = self.script("release/restore_unreleased.sh", "1.0", "CHANGELOG.md")
        self.assertNotEqual(stopped.returncode, 0)
        self.fail_git("push")
        stopped = self.script("release/restore_unreleased.sh", "1.0", "CHANGELOG.md")
        self.assertNotEqual(stopped.returncode, 0)
        committed = self.git("rev-parse", "HEAD").stdout
        finished = self.script("release/restore_unreleased.sh", "1.0", "CHANGELOG.md")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(changelog.read_text().count("## [Unreleased]"), 1)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, committed)
        self.assertEqual(self.git("rev-parse", "origin/main").stdout, committed)

    def test_finalizing_multiple_changelogs_resumes_after_the_first_backup_is_removed(self) -> None:
        first = self.write(self.repo / "first.md", "## [Unreleased]\n\nfirst\n")
        second = self.write(self.repo / "second.md", "## [Unreleased]\n\nsecond\n")
        self.stop_command("rm", target="first.md.portbak", after=True)
        stopped = self.script("release/finalize_changelogs.sh", "1.0", "first.md", "second.md")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertNotIn("Unreleased", first.read_text())
        self.assertIn("Unreleased", second.read_text())
        finished = self.script("release/finalize_changelogs.sh", "1.0", "first.md", "second.md")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        for path in (first, second):
            self.assertEqual(path.read_text().count("## [1.0]"), 1)
            self.assertNotIn("Unreleased", path.read_text())
