"""Test proposal migration and stable references in private folders."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.production import migrate_proposals, proposal_issue
from issues import Issue, frontmatter


def row(name: str, kind: staff.Kind, parent: str = "") -> staff.StaffRow:
    identity = staff.StaffId(fixture_runtime_id(name))
    return staff.StaffRow(identity, kind, name, staff.StaffId(parent) if parent else None, current_session=identity)


class ProposalIdentityTests(unittest.TestCase):
    def test_interrupted_migration_matches_clean_run_and_rerun_preserves_bytes(self) -> None:
        owner = row("demo", "showrunner")
        unit = row("names-unit", "unit", owner.id)
        record = staff.StaffSnapshot((owner, unit))
        original = ('---\ntags: [proposal]\nunit: names-unit\nproposed_by: names-unit (demo)\n'
                    'approved_in: demo\nshowrunner: demo\nplan: old.md\n---\nBody.\n')
        with tempfile.TemporaryDirectory() as directory, patch.object(staff, "snapshot", return_value=record):
            root = Path(directory)
            with patch.object(Path, "home", return_value=root):
                for name in ("clean", "resumed"):
                    folder = root / name
                    folder.mkdir()
                    for filename in ("a.md", "b.md"):
                        _ = (folder / filename).write_text(original)
                self.assertEqual(migrate_proposals.migrate(root / "clean"), (2, []))
                self.assertEqual(migrate_proposals.migrate(root / "resumed", limit=1), (1, []))
                self.assertEqual(migrate_proposals.migrate(root / "resumed"), (1, []))
                before = [(root / "resumed" / name).read_bytes() for name in ("a.md", "b.md")]
                self.assertEqual(before, [(root / "clean" / name).read_bytes() for name in ("a.md", "b.md")])
                self.assertEqual(migrate_proposals.migrate(root / "resumed"), (0, []))
                self.assertEqual(before, [(root / "resumed" / name).read_bytes() for name in ("a.md", "b.md")])
                issue = Issue(root / "clean/a.md", frontmatter(root / "clean/a.md"))
                self.assertEqual(issue.value("unit"), unit.id)
                self.assertEqual(issue.value("proposed_by"), unit.id)
                self.assertEqual(issue.value("approved_in"), owner.id)
                self.assertNotIn("plan", issue.fields)
                self.assertNotIn("showrunner", issue.fields)
                self.assertIsInstance(proposal_issue.decision_answer(issue), proposal_issue.ApprovedDecision)
                renamed = replace(unit, name="new-unit", current_session=staff.StaffId(fixture_runtime_id("clear")))
                with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner, renamed))):
                    self.assertEqual(proposal_issue.staff_name(issue.value("unit")), "new-unit")
                    self.assertEqual(migrate_proposals.migrate(root / "clean"), (0, []))

    def test_unknown_reference_preserves_the_whole_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())):
            root = Path(directory)
            path = root / "unknown.md"
            original = '---\ntags: [proposal]\nunit: unknown\nplan: keep.md\n---\nBody.\n'
            _ = path.write_text(original)
            with patch.object(Path, "home", return_value=root):
                changed, problems = migrate_proposals.migrate(root)
            self.assertEqual(changed, 0)
            self.assertEqual(len(problems), 1)
            self.assertIn("unknown", problems[0])
            self.assertEqual(path.read_text(), original)
