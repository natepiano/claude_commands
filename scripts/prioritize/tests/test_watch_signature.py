from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import final



from scripts.prioritize import watch_signature


@final
class WatchSignatureTests(unittest.TestCase):
    def test_content_and_membership_changes_change_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            issues = root / "issues"
            issues.mkdir()
            goals = root / "prioritization goals.md"
            _ = goals.write_text("goals\n", encoding="utf-8")
            issue = issues / "issue.md"
            _ = issue.write_text("first\n", encoding="utf-8")
            first = watch_signature.build_signature(issues, goals)

            _ = issue.write_text("second and longer\n", encoding="utf-8")
            second = watch_signature.build_signature(issues, goals)
            self.assertNotEqual(first, second)

            _ = (issues / "new.md").write_text("new\n", encoding="utf-8")
            third = watch_signature.build_signature(issues, goals)
            self.assertNotEqual(second, third)

            _ = goals.write_text("changed goals\n", encoding="utf-8")
            fourth = watch_signature.build_signature(issues, goals)
            self.assertNotEqual(third, fourth)

    def test_symlinked_issue_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            issues = root / "issues"
            issues.mkdir()
            goals = root / "prioritization goals.md"
            _ = goals.write_text("goals\n", encoding="utf-8")
            target = root / "target.md"
            _ = target.write_text("target\n", encoding="utf-8")
            (issues / "linked.md").symlink_to(target)

            with self.assertRaises(watch_signature.SignatureError):
                _ = watch_signature.build_signature(issues, goals)


if __name__ == "__main__":
    _ = unittest.main()
