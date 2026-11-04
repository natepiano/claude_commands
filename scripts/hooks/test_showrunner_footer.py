from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from scripts.hooks import showrunner_footer
from update_registration import QuestionStaff
from scripts.production import staff
from scripts.production.staff_test_support import (
    fixture_member, isolated_source_readers, isolated_staff_file, source_environment,
)
from scripts.statusline import counts_row


class QuestionLibraryTests(unittest.TestCase):
    def test_read_production_skips_wrapped_doc(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            instance = root / "showrunner-demo"
            instance.mkdir()
            doc = root / "production.md"
            _ = doc.write_text("> **Status: PRODUCTION — wrapped.** demo\n- **User zone:** America/Los_Angeles — demo\n")
            _ = (instance / "conf").write_text(f"CHECK=zsh /x/production_check.sh {doc}\n")
            self.assertIsNone(showrunner_footer.read_production(instance))

    def test_counts_and_deferred_time_share_one_question_staff_context(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            environment = {**source_environment(root / "sources"), "HOME": str(root), "HERDR_BIN": "/nonexistent/herdr"}
            environment["USER_ZONE"] = "UTC"
            environment["NEXT_STATE_DIR"] = str(root / "next")
            with (isolated_staff_file(root / "staff.json", environment),
                  mock.patch.dict(os.environ, environment), isolated_source_readers(),
                  mock.patch.object(Path, "home", return_value=root)):
                folder = root / "issues"
                folder.mkdir()
                doc = root / "demo-production.md"
                _ = doc.write_text(
                    "> **Status: PRODUCTION — running.** demo\n"
                    + f"- **Proposals:** {folder}\n", encoding="utf-8")
                owner = fixture_member("showrunner", name="renamed-owner",
                                       doc=staff.LiveDoc("natedev", doc))
                instance = Path(environment["NOTIFIER_STATE_DIR"]) / "showrunner-demo"
                instance.mkdir(parents=True)
                _ = (instance / "conf").write_text(
                    f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/production_check.sh {doc}\n",
                    encoding="utf-8")
                for title, after in (("Choose the layout", ""), ("Choose the compiler", "2099-01-02T08:00")):
                    fields = {"since": "2000-01-01T08:00", "text": title,
                              "question": title + "?", "asked_by": str(owner.id)}
                    if after:
                        fields["after"] = after
                    _ = (folder / f"question-{title}.md").write_text(
                        "---\nrecord_type: question\nstatus: open\n"
                        + f"production_id: {owner.id}\nasked_by: {owner.id}\n"
                        + f"registration: {json.dumps(fields)}\n---\n# {title}\n", encoding="utf-8")
                with (mock.patch.object(counts_row, "showrunner_footer", showrunner_footer),
                      mock.patch.object(QuestionStaff, "read", wraps=QuestionStaff.read) as read_context):
                    line, due, sources = counts_row.row(instance)
                self.assertEqual(line, "For you: 1 question")
                self.assertEqual(due, str(int(datetime(2099, 1, 2, 8, tzinfo=UTC).timestamp())))
                self.assertIn(doc, sources)
                for record in folder.glob("*.md"):
                    self.assertIn(record, sources)
                read_context.assert_called_once_with()


if __name__ == "__main__":
    _ = unittest.main()
