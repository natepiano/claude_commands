"""Final gate  behavior with real Git and Bash."""
from __future__ import annotations

import hashlib
import json
from typing import cast
from scripts.direct.test_verify_untested_examples import VERIFY
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_clean_final_records_the_tree_and_query_runs_no_cargo_or_admission(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tree = self.git("write-tree")
        self.assertEqual([path.name for path in self.pass_files()], [f"{tree}.json"])
        row = cast(dict[str, object], json.loads(self.pass_files()[0].read_text()))
        self.assertEqual(row["tree"], tree)
        self.assertEqual(row["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(row["repo"], str(self.root))
        self.assertEqual(row["verify_sha256"], hashlib.sha256(VERIFY.read_bytes()).hexdigest())
        self.assertEqual(row["features"], "extra")
        self.assertEqual(row["status"], 0)
        calls = self.calls_made()
        metadata_count = self.metadata_count()
        self.environment.update({"BUILD_TARGETS_SCRIPT": "/missing-checker", "IN_NIX_SHELL": "",
                                 "BUILD_HOLD_DIR": "/missing-hold-root"})
        for args in (("final-pass",), ("final-pass", tree)):
            with self.subTest(args=args):
                answer = self.verify(*args)
                self.assertEqual(answer.returncode, 0, answer.stderr)
                self.assertEqual(json.loads(answer.stdout), row)
        self.assertEqual(self.calls_made(), calls)
        self.assertEqual(self.metadata_count(), metadata_count)
        self.environment["IN_NIX_SHELL"] = "1"
        self.environment["BUILD_TARGETS_SCRIPT"] = str(VERIFY.parent.parent / "build_targets/build_targets.py")
        again = self.verify("final")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.calls_made(), calls + calls)


if __name__ == "__main__":
    _ = unittest.main()
