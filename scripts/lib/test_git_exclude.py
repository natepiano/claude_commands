"""Shared excludes protect a unit file in every worktree, including clean tracked files."""
import subprocess

from scripts.lib.git_exclude import ExcludeRefused, ensure_excluded, require_untracked_ignored
from scripts.production.unit_config_test_support import UnitConfigFixture


class GitExcludeTests(UnitConfigFixture):
    def test_preview_and_writer_agree_when_shared_rules_end_with_a_negation(self) -> None:
        for existing, refused in (("/.unit.toml\n!/.unit.toml\n", True),
                                  ("!/.unit.toml\n", False)):
            with self.subTest(existing=existing):
                _ = self.exclude.write_text(existing)
                before = self.file_versions()
                if refused:
                    with self.assertRaisesRegex(ExcludeRefused, "not ignored"):
                        require_untracked_ignored(self.worktree, ".unit.toml", planned_pattern="/.unit.toml")
                else:
                    require_untracked_ignored(self.worktree, ".unit.toml", planned_pattern="/.unit.toml")
                self.assertEqual(self.file_versions(), before)
                if refused:
                    with self.assertRaisesRegex(ExcludeRefused, "not ignored"):
                        ensure_excluded(self.worktree, "/.unit.toml")
                    self.assertEqual(self.file_versions(), before)
                else:
                    ensure_excluded(self.worktree, "/.unit.toml")
                    self.assertEqual(self.exclude.read_text(), existing + "/.unit.toml\n")
                    require_untracked_ignored(self.worktree, ".unit.toml")
                    published = self.file_versions()
                    ensure_excluded(self.worktree, "/.unit.toml")
                    self.assertEqual(self.file_versions(), published)

    def test_entry_is_shared_across_worktrees_and_rerun_preserves_bytes_and_mtime(self) -> None:
        linked = self.root / "linked"
        _ = self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        _ = self.exclude.write_text("keep-this-pattern")
        ensure_excluded(linked, "/.unit.toml")
        before = self.exclude.read_bytes(), self.exclude.stat().st_mtime_ns
        ensure_excluded(self.worktree, "/.unit.toml")
        self.assertEqual((self.exclude.read_bytes(), self.exclude.stat().st_mtime_ns), before)
        self.assertEqual(self.exclude.read_text(), "keep-this-pattern\n/.unit.toml\n")
        for checkout in (self.worktree, linked):
            require_untracked_ignored(checkout, ".unit.toml")
            result = subprocess.run(["git", "-C", str(checkout), "check-ignore", "-q", ".unit.toml"])
            self.assertEqual(result.returncode, 0)
            indexed = subprocess.run(["git", "-C", str(checkout), "ls-files", "--error-unmatch", ".unit.toml"],
                                     capture_output=True)
            self.assertNotEqual(indexed.returncode, 0)

    def test_a_clean_tracked_unit_file_refuses_without_changing_the_index_or_excludes(self) -> None:
        _ = self.source.write_text('plan="standby"\nresponsibilities=[]\n')
        _ = self.git("add", ".unit.toml")
        _ = self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-q", "-m", "Tracked by mistake")
        self.assertEqual(self.git("status", "--porcelain"), "")
        before = self.file_versions(), self.git("ls-files", "--stage")
        with self.assertRaisesRegex(ExcludeRefused, "tracked"):
            ensure_excluded(self.worktree, "/.unit.toml")
        self.assertEqual((self.file_versions(), self.git("ls-files", "--stage")), before)

    def test_higher_priority_negation_refuses_even_when_shared_exclude_has_the_entry(self) -> None:
        ensure_excluded(self.worktree, "/.unit.toml")
        _ = (self.worktree / ".gitignore").write_text("!/.unit.toml\n")
        before = self.file_versions()
        with self.assertRaisesRegex(ExcludeRefused, "not ignored"):
            ensure_excluded(self.worktree, "/.unit.toml")
        self.assertEqual(self.file_versions(), before)
        self.assertFalse(self.source.exists())

    def test_non_repository_and_multiline_patterns_refuse_without_creating_a_file(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        for checkout, pattern in ((outside, "/.unit.toml"), (self.worktree, "/one\n/two")):
            with self.subTest(checkout=checkout, pattern=pattern), self.assertRaises(ExcludeRefused):
                ensure_excluded(checkout, pattern)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotIn("/one", self.exclude.read_text())
