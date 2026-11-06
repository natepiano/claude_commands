"""Check the Cargo selection for binary and mixed workspaces."""

from __future__ import annotations

import unittest

from scripts.direct import verify_metadata
from scripts.direct.verify_dev_features_test_support import DevFeatureFixture


class VerifyCheckNoLibraryTests(DevFeatureFixture):
    def test_binary_workspace_sends_only_bins_to_cargo_check(self) -> None:
        (self.root / "src/lib.rs").unlink()
        _ = (self.root / "src/main.rs").write_text("fn main() {}\n")
        result = self.verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), ["check --workspace --bins"])


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
