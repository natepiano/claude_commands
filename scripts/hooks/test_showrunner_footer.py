from __future__ import annotations

from scripts.agents import remote

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from collections.abc import Generator
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest import mock
from typing import cast

from scripts.hooks import showrunner_footer
from scripts.production.update_registration import QuestionStaff
from scripts.production import update_registration as registration
from scripts.production.update_registration import due
from scripts.production.cli_fixture import production_paths
from scripts.production import staff
from scripts.production.staff_test_support import (
    fixture_member, fixture_runtime_id, isolated_source_readers, isolated_staff_file, source_environment,
)
from scripts.statusline import counts_row


AT = "2026-10-04T11:00"


@contextlib.contextmanager
def canonical_outstanding(root: Path, entries: list[dict[str, object]]) -> Generator[Path]:
    environment = {**source_environment(root / "sources"), "HOME": str(root / "home"),
                   "USER_ZONE": "America/Los_Angeles",
                   "HERDR_BIN": "/nonexistent/herdr"}
    with (isolated_staff_file(root / "staff.json", environment),
          mock.patch.dict(os.environ, environment), production_paths(environment)):
        folder = root / "issues"
        folder.mkdir(exist_ok=True)
        doc = root / "reader-production.md"
        _ = doc.write_text(f"> **Status: PRODUCTION — running.** reader\n- **Proposals:** {folder}\n")
        owner = fixture_member("showrunner", name="reader", doc=staff.LiveDoc(remote.local_machine(), doc))
        timer = Path(environment["NOTIFIER_STATE_DIR"]) / f"showrunner-{owner.id}"
        timer.mkdir(parents=True, exist_ok=True)
        _ = (timer / "conf").write_text(f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/check {doc}\n")
        path = showrunner_footer.outstanding_path(owner.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        reminders: list[dict[str, object]] = []
        for index, item in enumerate(entries):
            if "question" not in item and "reviewed" not in item:
                reminders.append(item)
                continue
            fields = {**item, "asked_by": str(owner.id)}
            _ = (folder / f"question-{index}.md").write_text(
                "---\nrecord_type: question\nstatus: open\n"
                + f"production_id: {owner.id}\nasked_by: {owner.id}\n"
                + f"registration: {json.dumps(fields)}\n---\n")
        _ = path.write_text(json.dumps(reminders))
        yield path


class QuestionLibraryTests(unittest.TestCase):
    def test_invalid_timer_text_does_not_hide_a_valid_staff_timer(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            with (isolated_staff_file(root / "staff.json", {}),
                  mock.patch.dict(os.environ, {"NOTIFIER_STATE_DIR": str(root)})):
                owner = fixture_member("showrunner", name="timer-owner")
                invalid = root / "showrunner-invalid"
                invalid.mkdir()
                _ = (invalid / "conf").write_bytes(b"\xff")
                valid = root / f"showrunner-{owner.id}"
                valid.mkdir()
                _ = (valid / "conf").write_text(f"TARGET=session:{owner.id}\n")
                parent = fixture_member("showrunner", name="parent")
                for kind, enabled in (("showrunner", 1), ("showrunner", 0), ("unit", 0)):
                    with self.subTest(kind=kind, enabled=enabled):
                        _ = (valid / "state").write_text(f"ENABLED={enabled}\n")
                        if kind == "unit":
                            _ = staff.change_director(owner.id, staff.UnitRole(owner.name, parent.id,
                                                     staff.PresentWorktree("natedev", root)))
                        errors = io.StringIO()
                        with contextlib.redirect_stderr(errors):
                            self.assertEqual(showrunner_footer.targeted_instances(owner.current_session),
                                             [valid] if kind == "showrunner" else [])
                        self.assertEqual(errors.getvalue(), "")

    def test_read_production_uses_one_staff_read_and_the_timer_folder_owner(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            with (isolated_staff_file(root / "staff.json", {}),
                  mock.patch.dict(os.environ, {"USER_ZONE": "UTC"})):
                doc = root / "different-document-production.md"
                _ = doc.write_text("> **Status: PRODUCTION — running.** Test\n")
                owner = fixture_member("showrunner", name="hana", doc=staff.LiveDoc(remote.local_machine(), doc))
                other = fixture_member("showrunner", name="another-owner")
                instance = root / f"showrunner-{owner.id}"
                instance.mkdir()
                _ = (instance / "conf").write_text(f"TARGET=session:{other.id}\n")
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as read_staff:
                    production = showrunner_footer.read_production(instance)
                self.assertEqual(production, showrunner_footer.Production(
                    owner.name, doc, "UTC", owner))
                read_staff.assert_called_once_with()

    def test_deferred_time_reports_missing_owner_and_keeps_a_known_owner(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            environment = {**source_environment(root / "sources"), "HOME": str(root),
                           "HERDR_BIN": "/nonexistent/herdr", "SHOWRUNNER_STATE_DIR": str(root / "showrunner")}
            with (isolated_staff_file(root / "staff.json", environment),
                  mock.patch.dict(os.environ, environment), isolated_source_readers()):
                owner = fixture_member("showrunner", name="old-display-name")
                _ = staff.rename(owner.id, "current-display-name")
                _ = staff.update_current_session(owner.current_session,
                    staff.native_id(fixture_runtime_id("deferred-owner-cleared")))
                folder = root / "showrunner/outstanding"
                folder.mkdir(parents=True)
                _ = (folder / f"{owner.id}.json").write_text(json.dumps([
                    {"since": "2000-01-01T08:00", "text": "Choose the layout", "after": "2099-01-02T08:00"}]))
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    self.assertIsNone(showrunner_footer.deferred_until(["missing-owner"], "UTC"))
                self.assertIn("unavailable", errors.getvalue())
                self.assertIn("missing-owner", errors.getvalue())
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    due = showrunner_footer.deferred_until(["missing-owner", owner.id], "UTC")
                self.assertEqual(due, datetime(2099, 1, 2, 8, tzinfo=UTC))
                self.assertIn("unavailable", errors.getvalue())
                self.assertIn("missing-owner", errors.getvalue())

    def test_row_reports_missing_owner_and_keeps_readable_efficiency_count(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            environment = {**source_environment(root / "sources"), "HOME": str(root),
                           "HERDR_BIN": "/nonexistent/herdr", "USER_ZONE": "UTC"}
            with (isolated_staff_file(root / "staff.json", environment),
                  mock.patch.dict(os.environ, environment), isolated_source_readers()):
                owner = fixture_member("showrunner", name="display-owner")
                ledger = root / "ledger.md"
                _ = ledger.write_text("- 2026-11-04 Improve the layout — proposed\n")
                document = root / "unowned-production.md"
                _ = document.write_text("> **Status: PRODUCTION — running.** Test\n"
                                        + f"- **Efficiency ledger:** {ledger}\n")
                instance = Path(environment["NOTIFIER_STATE_DIR"]) / f"showrunner-{owner.id}"
                instance.mkdir(parents=True)
                _ = (instance / "conf").write_text(
                    f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
                _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), document))
                identities = QuestionStaff.for_record(staff.StaffSnapshot(()))
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    line, due, paths = counts_row.row(instance, identities=identities)
                self.assertEqual(line, "For you: 1 efficiency")
                self.assertEqual(due, "")
                self.assertIn(document, paths)
                self.assertIn(ledger, paths)
                self.assertIn("unavailable", errors.getvalue())

    def test_switch_and_outstanding_paths_keep_the_fixed_staff_id(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            environment = {"HOME": str(root), "HERDR_BIN": "/nonexistent/herdr",
                           "SHOWRUNNER_STATE_DIR": str(root / "showrunner")}
            with (isolated_staff_file(root / "staff.json", environment),
                  mock.patch.dict(os.environ, environment)):
                owner = fixture_member("showrunner", name="display-name", runtime_id=fixture_runtime_id("footer-owner"))
                _ = staff.update_current_session(owner.current_session, staff.native_id(fixture_runtime_id("footer-current")))
                self.assertEqual(showrunner_footer.outstanding_path(owner.id),
                                 root / "showrunner/outstanding" / f"{owner.id}.json")
                _ = staff.rename(owner.id, "renamed-owner")
                self.assertEqual(showrunner_footer.outstanding_path(owner.id),
                                 root / "showrunner/outstanding" / f"{owner.id}.json")

    def test_read_production_skips_wrapped_doc(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            doc = root / "production.md"
            _ = doc.write_text("> **Status: PRODUCTION — wrapped.** demo\n- **User zone:** America/Los_Angeles — demo\n")
            with isolated_staff_file(root / "staff.json", {}):
                owner = fixture_member("showrunner", name="demo", doc=staff.WrappedDoc(remote.local_machine(), doc))
                instance = root / f"showrunner-{owner.id}"
                instance.mkdir()
                _ = (instance / "conf").write_text(f"CHECK=zsh /x/production_check.sh {owner.id}\n")
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
                                       doc=staff.LiveDoc(remote.local_machine(), doc))
                instance = Path(environment["NOTIFIER_STATE_DIR"]) / f"showrunner-{owner.id}"
                instance.mkdir(parents=True)
                _ = (instance / "conf").write_text(
                    f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/production_check.sh {owner.id}\n",
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
                identities = QuestionStaff.read()
                with (mock.patch.object(counts_row, "showrunner_footer", showrunner_footer),
                      mock.patch.object(QuestionStaff, "read", return_value=identities) as read_context,
                      mock.patch.object(showrunner_footer, "outstanding_path",
                                        wraps=showrunner_footer.outstanding_path) as outstanding):
                    line, due, sources = counts_row.row(instance)
                self.assertTrue(outstanding.call_args_list)
                for call in outstanding.call_args_list:
                    self.assertIs(cast(object, call.kwargs["identities"]), identities)
                self.assertEqual(line, "For you: 1 question")
                self.assertEqual(due, str(int(datetime(2099, 1, 2, 8, tzinfo=UTC).timestamp())))
                self.assertIn(doc, sources)
                for record in folder.glob("*.md"):
                    self.assertIn(record, sources)
                read_context.assert_called_once_with()

    def test_outstanding_reader_preserves_full_questions_and_distinguishes_review_results(self) -> None:
        question = "Session: demo\nAction: choose a route\n" + "Which route? " * 120
        entries: list[dict[str, object]] = [
            {"since": AT, "text": "reminder"},
            {"since": AT, "text": "facts needed", "question": question},
            {"since": AT, "text": "route", "question": question, "reviewed": "Use the short route?"},
            {"since": AT, "text": "review failed", "question": question,
             "review_unavailable": "reviewer unavailable", "after": "2026-10-04T12:00"},
        ]
        with (tempfile.TemporaryDirectory() as scratch,
              canonical_outstanding(Path(scratch), entries) as path):
            items = registration.parse_outstanding_entries(entries, path)
            self.assertIsInstance(items[0], registration.OutstandingReminder)
            awaiting = items[1]
            self.assertIsInstance(awaiting, registration.OutstandingQuestion)
            assert isinstance(awaiting, registration.OutstandingQuestion)
            self.assertEqual(awaiting.question, question)
            reviewed = items[2]
            self.assertIsInstance(reviewed, registration.ReviewedOutstandingQuestion)
            assert isinstance(reviewed, registration.ReviewedOutstandingQuestion)
            self.assertEqual(reviewed.reviewed, "Use the short route?")
            unavailable = items[3]
            self.assertIsInstance(unavailable, registration.ReviewUnavailableOutstandingQuestion)
            assert isinstance(unavailable, registration.ReviewUnavailableOutstandingQuestion)
            self.assertEqual(unavailable.question, question)
            self.assertEqual(unavailable.reason, "reviewer unavailable")
            self.assertEqual(due(items, datetime.fromisoformat(AT)), items[:3])
            self.assertEqual(due(items, datetime(2026, 10, 4, 12)), items)
            timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{path.stem}"
            production = showrunner_footer.read_production(timer)
            self.assertIsNotNone(production)
            assert production is not None
            class FooterClock:
                @staticmethod
                def now(zone: ZoneInfo) -> datetime:
                    return datetime.fromisoformat(AT).replace(tzinfo=zone)

            with mock.patch.object(showrunner_footer, "datetime", FooterClock):
                self.assertEqual(showrunner_footer.open_questions([production]), 3)
            output = showrunner_footer.counts_line([production], 3)
            self.assertIsNotNone(output)
            assert output is not None
            self.assertIn("3 questions", output)
            self.assertNotIn("Which route?", output)

    def test_outstanding_reader_refuses_conflicting_or_incomplete_review_evidence(self) -> None:
        cases: list[dict[str, object]] = [
            {"reviewed": "Choose now?"},
            {"question": "Choose now?", "reviewed": "Choose now?", "review_unavailable": "offline"},
            {"question": ""},
            {"question": "Choose now?", "reviewed": 42},
            {"question": "Choose now?", "review_unavailable": 42},
            {"question": None},
            {"question": "Choose now?", "reviewed": None},
            {"question": "Choose now?", "review_unavailable": None},
        ]
        for fields in cases:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as scratch:
                with canonical_outstanding(Path(scratch), [{"since": AT, "text": "route", **fields}]) as path:
                    with self.assertRaises(registration.InputError):
                        _ = registration.parse_outstanding_entries([{"since": AT, "text": "route", **fields}], path)


if __name__ == "__main__":
    _ = unittest.main()
