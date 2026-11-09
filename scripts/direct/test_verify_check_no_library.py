"""Check the Cargo selection for binary and mixed workspaces."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from scripts.direct import verify_metadata
from scripts.direct.verify_dev_features_test_support import DevFeatureFixture, VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyCheckNoLibraryTests(DevFeatureFixture):
    def test_binary_workspace_sends_only_bins_to_cargo_check(self) -> None:
        (self.root / "src/lib.rs").unlink()
        _ = (self.root / "src/main.rs").write_text("fn main() {}\n")
        for has_manifest in (True, False):
            with self.subTest(has_manifest=has_manifest):
                if has_manifest:
                    result = self.verify("check", "sample")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.calls_made(), ["check --workspace --bins"])
                else:
                    (self.root / "Cargo.toml").unlink()
                    real_git = shutil.which("git")
                    if real_git is None:
                        self.fail("The repository fixture requires Git.")
                    git = Path(self.environment["PATH"]) / "git"
                    git.unlink()
                    git.symlink_to(real_git)
                    _ = subprocess.run(["git", "init", "--quiet"], cwd=self.root, env=self.environment,
                        capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                    _ = self.calls.write_text("")
                    _ = self.metadata_calls.write_text("")
                    result = subprocess.run(["bash", str(VERIFY), "final"], cwd=self.root, env=self.environment,
                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                    details = (result.stdout + result.stderr + "\ncargo calls: " + self.calls.read_text()
                               + "\nmetadata calls: " + self.metadata_calls.read_text())
                    self.assertEqual(result.returncode, 2, details)
                    self.assertEqual(result.stderr,
                        f"verify.sh: {self.root} has no Cargo.toml; verify.sh checks Cargo repositories only\n", details)
                    self.assertEqual(self.calls_made(), [])
                    self.assertEqual(self.metadata_calls.read_text().splitlines(), [])


class VerifyCheckSelectionTests(unittest.TestCase):
    def test_mixed_workspace_keeps_lib_for_another_member(self) -> None:
        metadata: verify_metadata.CargoMetadata = {
            "packages": [
                {
                    "name": "sample",
                    "id": "path+file:///workspace/sample#0.1.0",
                    "manifest_path": "/workspace/sample/Cargo.toml",
                    "features": {},
                    "targets": [{
                        "name": "sample", "kind": ["bin"], "test": True,
                        "src_path": "/workspace/sample/src/main.rs", "required-features": [],
                    }],
                },
                {
                    "name": "library",
                    "id": "path+file:///workspace/library#0.1.0",
                    "manifest_path": "/workspace/library/Cargo.toml",
                    "features": {},
                    "targets": [{
                        "name": "library", "kind": ["lib"], "test": True,
                        "src_path": "/workspace/library/src/lib.rs", "required-features": [],
                    }],
                },
            ],
            "workspace_members": [
                "path+file:///workspace/sample#0.1.0",
                "path+file:///workspace/library#0.1.0",
            ],
            "workspace_root": "/workspace",
            "target_directory": "/workspace/target",
        }
        self.assertEqual(verify_metadata.check_targets(metadata).splitlines(), ["--lib", "--bins"])


if __name__ == "__main__":
    _ = unittest.main()
