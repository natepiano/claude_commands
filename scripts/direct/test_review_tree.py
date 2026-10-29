#!/usr/bin/env python3
"""Tests for the changed paths a review leaves to the other phases open beside it."""
from __future__ import annotations

import json
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_reservation, recorder_state, review_tree  # noqa: E402


@final
class OtherPhasesPathsTests(unittest.TestCase):
    session: Path = Path()

    @override
    def setUp(self) -> None:
        self.session = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def open_phases(self, files: dict[str, str]) -> None:
        """The recorder lists each phase id open, and each phase's directory holds its files list."""
        records = [{"id": phase, "instance_id": phase, "status": "active"} for phase in files]
        _ = (self.session / recorder_state.STATE_FILENAME).write_text(json.dumps({
            "phase": records[-1], "open_phases": records[:-1]}))
        for phase, listed in files.items():
            directory = phase_reservation.phase_directory(self.session, phase)
            directory.mkdir(parents=True)
            _ = (directory / phase_reservation.FILES_FILE).write_text(listed)

    def test_an_ad_hoc_phase_open_beside_another_keeps_its_paths_from_the_review(self) -> None:
        self.open_phases({"ad hoc": "notes.md\n", "3": "reviewed.txt\n"})
        changed = ["notes.md", "reviewed.txt"]
        self.assertEqual(review_tree.other_phases_paths(self.session, "3", changed), {"adhoc": ["notes.md"]})
        self.assertEqual(review_tree.other_phases_paths(self.session, "ad hoc", changed), {"3": ["reviewed.txt"]})

    def test_review_changes_uses_the_phase_that_owns_the_edited_worktree(self) -> None:
        first, second = self.session / "tree-x", self.session / "tree-y"
        first.mkdir()
        _ = (first / "shared.txt").write_text("Before review.\n")
        for command in (
            ["init", "-q", "-b", "main"], ["add", "shared.txt"],
            ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "base"],
            ["worktree", "add", "-q", "-b", "other", str(second)],
        ):
            _ = subprocess.run(["git", "-C", str(first), *command], check=True, capture_output=True, timeout=60)
        self.open_phases({"1": "shared.txt\n", "2": "shared.txt\n", "3": "reviewed.txt\n"})
        for name, root in (("1", first), ("2", second)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                code = phase_reservation.main(["claim", "--session-dir", str(self.session),
                                               "--phase", name, "--repo", str(root)])
            self.assertEqual(code, 0, errors.getvalue())
        baseline = self.session / "baseline.json"
        _ = baseline.write_text(json.dumps({"head": review_tree.head(first), "paths": {}}))

        for root, owner in ((first, "1"), (second, "2")):
            with self.subTest(root=root):
                _ = (root / "shared.txt").write_text("The sibling phase changed this.\n")
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = review_tree.main(["changes", "--root", str(root), "--baseline", str(baseline),
                                             "--session-dir", str(self.session), "--phase", "3"])
                self.assertEqual(code, 0, errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(),
                                 f"review tree: open phase {owner}'s files changed during the review: shared.txt\n")


if __name__ == "__main__":
    _ = unittest.main()
