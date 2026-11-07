"""Interrupted guide generation never reports a partial set as ready."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bevy_migration_split_guide as splitter

GUIDE = "### One\nFirst section.\n### Two\nSecond section.\n"


class GuideRerunTests(unittest.TestCase):
    def test_split_reruns_after_first_section_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "guides"
            write = Path.write_text

            def stopped(path: Path, text: str) -> int:
                result = write(path, text)
                if path.suffix == ".md":
                    raise InterruptedError("stop after first section")
                return result

            sections = splitter.parse_sections(GUIDE)
            with mock.patch.object(Path, "write_text", stopped):
                with self.assertRaises(InterruptedError):
                    _ = splitter.write_sections(sections, output)
            self.assertEqual(list(output.glob("*.md")), [])
            self.assertEqual(splitter.write_sections(sections, output), 2)
            self.assertEqual({p.name for p in output.glob("*.md")}, {"One.md", "Two.md"})

    def test_ensure_reruns_splitter_for_partial_untracked_guides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "rust/bevy-0.18.0"
            repo.mkdir(parents=True)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guides = repo / "release-content/migration-guides"
            guides.mkdir(parents=True)
            _ = (guides / "One.md").write_text("partial\n")
            (guides.parent / ".migration-guides.staging").mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            curl = bin_dir / "curl"
            _ = curl.write_text("#!/bin/sh\ncat <<'GUIDE'\n" + GUIDE + "GUIDE\n")
            curl.chmod(0o755)
            env = dict(os.environ, HOME=str(root), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", str(Path(__file__).with_name("bevy_migration_ensure_repo.sh")), "0.18.0"],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p.name for p in guides.glob("*.md")}, {"One.md", "Two.md"})
            self.assertEqual((guides / "One.md").read_text(), "### One\nFirst section.")

    def ensure(self, root: Path, guide: str = GUIDE) -> subprocess.CompletedProcess[str]:
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        curl = bin_dir / "curl"
        _ = curl.write_text("#!/bin/sh\ncat <<'GUIDE'\n" + guide + "GUIDE\n")
        curl.chmod(0o755)
        env = dict(os.environ, HOME=str(root), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        return subprocess.run(["bash", str(Path(__file__).with_name("bevy_migration_ensure_repo.sh")), "0.18.0"],
                              env=env, capture_output=True, text=True, check=False)

    def test_published_guides_keep_user_changes_after_publication_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "rust/bevy-0.18.0"
            repo.mkdir(parents=True)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guides = repo / "release-content/migration-guides"
            guides.mkdir(parents=True)
            _ = (guides / "Old.md").write_text("old complete set\n")
            replace = os.replace

            def stopped(source: Path, target: Path) -> None:
                replace(source, target)
                if target == guides or target == guides / "One.md":
                    raise InterruptedError("stop after publishing guides")

            with mock.patch.object(os, "replace", stopped), self.assertRaises(InterruptedError):
                _ = splitter.write_sections(splitter.parse_sections(GUIDE), guides)
            _ = (guides / "One.md").write_text("user correction\n")
            _ = (guides / "Notes.md").write_text("user notes\n")
            result = self.ensure(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((guides / "One.md").read_text(), "user correction\n")
            self.assertEqual((guides / "Notes.md").read_text(), "user notes\n")
            self.assertNotIn("fetching from bevy-website", result.stderr)

    def test_tracked_guides_finish_directory_swap_instead_of_accepting_mixed_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "rust/bevy-0.18.0"
            repo.mkdir(parents=True)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guides = repo / "release-content/migration-guides"
            guides.mkdir(parents=True)
            for name in ("One.md", "Two.md"):
                _ = (guides / name).write_text("old " + name)
            _ = subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            complete = GUIDE + "### Three\nNew section.\n"
            replace = os.replace

            def stopped(source: Path, target: Path) -> None:
                replace(source, target)
                if source == guides or target == guides / "One.md":
                    raise InterruptedError("stop between directory renames")

            with mock.patch.object(os, "replace", stopped), self.assertRaises(InterruptedError):
                _ = splitter.write_sections(splitter.parse_sections(complete), guides)
            result = self.ensure(root, complete)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p.name for p in guides.glob("*.md")}, {"One.md", "Two.md", "Three.md"})
            self.assertEqual((guides / "One.md").read_text(), "### One\nFirst section.")
            self.assertEqual((guides / "Two.md").read_text(), "### Two\nSecond section.")

    def test_legacy_set_without_swap_state_preserves_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guides = Path(directory) / "guides"
            guides.mkdir()
            _ = (guides / "Notes.md").write_text("legacy user guide")
            self.assertTrue(splitter.guides_complete(guides))


if __name__ == "__main__":
    _ = unittest.main()
