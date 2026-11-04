"""Proposal document reads distinguish missing fields from unavailable documents."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.production import proposals_folder


class ProposalDocumentTests(unittest.TestCase):
    def test_configured_paths_and_missing_fields_have_distinct_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / "docs/plans/demo-production.md"
            doc.parent.mkdir(parents=True)
            _ = doc.write_text("# Demo\n")
            self.assertIsInstance(proposals_folder.issues_folder(doc), proposals_folder.ProposalsUnconfigured)
            self.assertIsInstance(proposals_folder.ledger(doc), proposals_folder.LedgerUnconfigured)
            ledger = root / "ledger.md"
            _ = doc.write_text(f"- **Proposals:** issues\n- **Improvement queue:** {ledger}\n")
            folder = proposals_folder.issues_folder(doc)
            queue = proposals_folder.ledger(doc)
            assert isinstance(folder, proposals_folder.ProposalsFolder)
            assert isinstance(queue, proposals_folder.ImprovementLedger)
            self.assertEqual(folder.path, root / "issues")
            self.assertEqual(queue.path, ledger)

    def test_unreadable_documents_are_unavailable_instead_of_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            doc = Path(directory) / "production.md"
            for content in (None, b"\xff"):
                with self.subTest(content=content):
                    if content is not None:
                        _ = doc.write_bytes(content)
                    folder = proposals_folder.issues_folder(doc)
                    queue = proposals_folder.ledger(doc)
                    self.assertIsInstance(folder, proposals_folder.ProposalsUnavailable)
                    self.assertIsInstance(queue, proposals_folder.LedgerUnavailable)
                    self.assertIn(str(doc), str(folder))
                    self.assertIn(str(doc), str(queue))


if __name__ == "__main__":
    _ = unittest.main()
