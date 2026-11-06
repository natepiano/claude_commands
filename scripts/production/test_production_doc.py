"""Production readers use explicit names and one document format."""
from __future__ import annotations

import unittest
from dataclasses import replace

from scripts.production import production_doc, staff
from scripts.lib.user_zone import user_zone
from scripts.production.staff_test_support import StaffTestCase, fixture_member


class ProductionDocumentTests(StaffTestCase):
    def test_new_document_keeps_its_explicit_name_before_staff_publication(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text("# New production\n")
        with document.open("a") as stream:
            _ = stream.write(f"\n- **Merge branch:** `main`\n"
                             + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `log.md`\n")
        production = production_doc.read_new_production(document, "new-showrunner")
        self.assertEqual((production.name, production.doc, production.checkout, production.log),
                         ("new-showrunner", document, self.root, self.root / "log.md"))
        self.assertIsNone(staff.initial_snapshot().by_path("new-showrunner"))

    def test_existing_document_uses_the_same_fields_with_its_staff_name(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text(f"- **Merge branch:** `release`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `nested/log.md`\n")
        row = fixture_member("showrunner", name="retained-name", doc=staff.LiveDoc("natedev", document))
        expected = production_doc.Production(document, row.name, "release", self.root,
                                             self.root / "nested/log.md", user_zone(), row)
        self.assertEqual(production_doc.read_production(row.name, staff.snapshot()), expected)
        wrapped = staff.set_doc(row.id, staff.WrappedDoc("natedev", document))
        self.assertEqual(production_doc.read_production(row.name, staff.snapshot()), replace(expected, owner=wrapped))

    def test_both_readers_refuse_an_absolute_log_path(self) -> None:
        document = self.root / "different-production.md"
        _ = document.write_text(f"- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `/outside/log.md`\n")
        row = fixture_member("showrunner", name="retained-name", doc=staff.LiveDoc("natedev", document))
        with self.assertRaisesRegex(production_doc.Refusal, "Log must be relative"):
            _ = production_doc.read_new_production(document, row.name)
        with self.assertRaisesRegex(production_doc.Refusal, "Log must be relative"):
            _ = production_doc.read_production(row.name, staff.snapshot())


if __name__ == "__main__":
    _ = unittest.main()
