"""Test proposal migration and stable references in private folders."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from typing import final, override
import unittest
from unittest.mock import patch

from scripts.production import session_staff, showrunners, staff
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.production import migrate_proposals, proposal_issue
from scripts.shutdown import remote
from issues import Issue, frontmatter


def row(name: str, kind: staff.Kind, parent: staff.ReportsTo | staff.StaffRoot) -> staff.StaffRow:
    identity = staff.StaffId(fixture_runtime_id(name))
    return staff.StaffRow(identity, kind, name, parent, current_session=identity)


@final
class ProposalIdentityTests(unittest.TestCase):
    sources = session_staff.MemberSources({}, ())

    @override
    def setUp(self) -> None:
        super().setUp()
        sources = patch.object(session_staff, "member_sources", return_value=self.sources)
        _ = sources.start()
        self.addCleanup(sources.stop)
        journals = patch.object(showrunners, "rename_history", return_value={})
        _ = journals.start()
        self.addCleanup(journals.stop)

    def test_interrupted_migration_matches_clean_run_and_rerun_preserves_bytes(self) -> None:
        owner = row("demo", "showrunner", staff.StaffRoot())
        unit = row("names-unit", "unit", staff.ReportsTo(owner.id))
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

    def test_unknown_references_preserve_values_and_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())):
            root = Path(directory)
            path = root / "unknown.md"
            original = ('---\ntags: [proposal]\nunit: retired-unit\nproposed_by: retired-unit (retired-production)\n'
                        'approved_in: retired-production\n---\nBody.\n')
            _ = path.write_text(original)
            with patch.object(Path, "home", return_value=root):
                self.assertEqual(migrate_proposals.migrate(root), (1, []))
                migrated = path.read_bytes()
                with patch("sys.argv", ["migrate_proposals", str(root)]), patch("builtins.print"):
                    self.assertEqual(migrate_proposals.main(), 0)
                self.assertEqual(path.read_bytes(), migrated)
            issue = Issue(path, frontmatter(path))
            self.assertEqual(issue.value("unit"), "retired-unit")
            self.assertEqual(issue.value("proposed_by"), "retired-unit (retired-production)")
            self.assertEqual(issue.value("approved_in"), "retired-production")
            self.assertTrue(path.read_text().endswith("---\nBody.\n"))

    def test_former_production_name_resolves_through_its_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / "hana-production.md"
            owner = replace(row("hana", "showrunner", staff.StaffRoot()), doc=staff.LiveDoc("natedev", doc))
            unit = row("camera-unit", "unit", staff.ReportsTo(owner.id))
            record = staff.StaffSnapshot((owner, unit))
            document = "# Production\n\n## Production Context\n\n- **Formerly:** `tool-based-ui`\n"
            sources = session_staff.MemberSources({"hana": showrunners.ShowrunnerSource(
                showrunners.Timer("hana", "", doc, "UTC"), document)}, ())
            path = root / "proposal.md"
            _ = path.write_text('---\ntags: [proposal]\napproved_in: tool-based-ui\n' +
                                'proposed_by: camera-unit (tool-based-ui)\nunit: camera-unit\n---\nBody.\n')
            with (patch.object(staff, "snapshot", return_value=record),
                  patch.object(session_staff, "member_sources", return_value=sources),
                  patch.object(remote, "local_machine", return_value="natedev"),
                  patch.object(Path, "home", return_value=root)):
                self.assertEqual(migrate_proposals.migrate(root), (1, []))
            issue = Issue(path, frontmatter(path))
            self.assertEqual(issue.value("approved_in"), owner.id)
            self.assertEqual(issue.value("proposed_by"), unit.id)
            self.assertEqual(issue.value("unit"), unit.id)

    def test_current_name_precedes_former_names_and_rename_journals(self) -> None:
        current = row("old", "showrunner", staff.StaffRoot())
        other = row("new", "showrunner", staff.StaffRoot())
        with patch.object(showrunners, "rename_history", return_value={"old": "new"}):
            self.assertEqual(migrate_proposals.reference(staff.StaffSnapshot((current, other)), "old",
                             ("showrunner",), sources=self.sources), current.id)

    def test_rename_journal_resolves_a_production_without_a_loaded_document(self) -> None:
        owner = row("new", "showrunner", staff.StaffRoot())
        with patch.object(showrunners, "rename_history", return_value={"old": "new"}):
            self.assertEqual(migrate_proposals.reference(staff.StaffSnapshot((owner,)), "old",
                             ("showrunner",), sources=self.sources), owner.id)

    def test_ambiguous_reference_preserves_the_whole_file(self) -> None:
        first = row("first", "showrunner", staff.StaffRoot())
        second = row("second", "showrunner", staff.StaffRoot())
        units = (row("shared-unit", "unit", staff.ReportsTo(first.id)),
                 replace(row("other-unit", "unit", staff.ReportsTo(second.id)), name="shared-unit"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ambiguous.md"
            original = '---\ntags: [proposal]\nunit: shared-unit\nplan: keep.md\n---\nBody.\n'
            _ = path.write_text(original)
            with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((first, second, *units))),
                  patch.object(Path, "home", return_value=root)):
                changed, problems = migrate_proposals.migrate(root)
            self.assertEqual(changed, 0)
            self.assertEqual(len(problems), 1)
            self.assertIn("shared-unit", problems[0])
            self.assertEqual(path.read_text(), original)
