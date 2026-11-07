"""Keep Cargo workspace selection stable for audit and offload."""

from pathlib import Path
import tempfile
from typing import final
import unittest

import cargo_manifest


@final
class CargoManifestTests(unittest.TestCase):
    def test_workspace_globs_keep_only_in_tree_members_and_apply_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "workspace"
            for name in ("z-last", "a-first", "skip"):
                (root / "crates" / name).mkdir(parents=True)
            outside = root.parent / "outside"
            outside.mkdir()
            (root / "crates" / "outside-link").symlink_to(outside, target_is_directory=True)
            _ = (root / "README").write_text("not a crate folder\n")
            manifest = root / "Cargo.toml"
            _ = manifest.write_text(
                '[workspace]\nmembers = ["crates/*", "../outside", "README", 7]\n'
                + 'exclude = ["crates/skip"]\n', encoding="utf-8",
            )

            members = cargo_manifest.member_directories(root, cargo_manifest.toml_table(manifest))

            self.assertEqual(members, (root / "crates/a-first", root / "crates/z-last"))

    def test_workspace_root_package_is_a_member_without_a_matching_glob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / "member"
            child.mkdir()
            manifest = root / "Cargo.toml"
            _ = manifest.write_text(
                '[package]\nname = "root-package"\nversion = "0.1.0"\n'
                + '[workspace]\nmembers = ["member"]\n', encoding="utf-8",
            )

            members = cargo_manifest.member_directories(root, cargo_manifest.toml_table(manifest))

            self.assertEqual(members, (root, child))


if __name__ == "__main__":
    _ = unittest.main()
