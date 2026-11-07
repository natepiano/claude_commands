"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.production import proposal_issue, proposals_folder, staff
from scripts.production.issues import Issue, frontmatter
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_member
from scripts.production.update_registration_test_support import RegistrationFixture, QuestionRegistration, registration, review

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_shared_proposals_and_questions_follow_staff_rename_and_transfer_without_rewrites(self) -> None:
        owner_id = self.owner_id
        unit_id = self.fixture_staff_id("alpha")
        destination_doc = self.doc.with_name("other-production.md")
        _ = destination_doc.write_text(self.doc.read_text())
        destination = fixture_member("showrunner", name="destination",
                                     doc=staff.LiveDoc("natedev", destination_doc))
        proposal = self.root / "issues/layout-proposal.md"
        _ = proposal.write_text("\n".join([
            "---", "status: open", "tags: [issue, proposal]", "proposal_id: P1",
            f"proposed_by: {unit_id}", f"unit: {unit_id}", "date_created: 2026-11-02",
            "---", "A separate proposal.", "",
        ]))
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Keep this layout?")):
            for title, asker in (("Choose the layout", str(unit_id)), ("Choose the colors", "")):
                filed = self.run_command("ask", "--text", title, "--question", "Keep this layout?",
                                         "--unit", asker)
                self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        questions = list((self.root / "issues").glob("question-*.md"))
        self.assertEqual(len(questions), 2)
        self.assertEqual({frontmatter(path)["production_id"][0] for path in questions}, {owner_id})
        self.assertEqual({frontmatter(path)["asked_by"][0] for path in questions}, {owner_id, unit_id})
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in [proposal, *questions]}
        _ = staff.rename(owner_id, "renamed-owner")
        _ = staff.rename(unit_id, "renamed-unit")
        for transferred in (False, True):
            with self.subTest(transferred=transferred):
                if transferred:
                    _ = staff.move(unit_id, staff.ReportsTo(destination.id))
                self.assertEqual([issue.path for issue in proposals_folder.open_proposals(self.doc)],
                                 [] if transferred else [proposal])
                self.assertEqual([issue.path for issue in proposals_folder.open_proposals(destination_doc)],
                                 [proposal] if transferred else [])
                self.assertEqual(proposal_issue.proposal_unit(Issue(proposal, frontmatter(proposal))), "renamed-unit")
                listed = self.run_command("questions-table")
                self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
                for text in ("Choose the layout", "Choose the colors", "renamed-unit", "renamed-owner"):
                    self.assertIn(text, listed.stdout)
                self.assertNotIn("A separate proposal", listed.stdout)
                other = invoke_cli(registration, registration.main,
                                   ["questions-table", "--production", destination.name],
                                   self.env, self.checkout, production_state=True)
                self.assertEqual(other.returncode, 0, other.stdout + other.stderr)
                self.assertEqual(other.stdout.strip(), "no open questions")
                self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in before}, before)

    def check_question_reopening(self, minutes: int) -> tuple[str, str]:
        title = "Choose the layout"
        first = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        moment = first

        def now(zone: tzinfo | None = None) -> datetime:
            return moment.astimezone(zone)

        with (mock.patch.object(registration, "datetime", wraps=datetime, now=now),
              mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Keep this layout?"))):
            _ = self.run_ask(title, "Keep this layout?")
            path = self.question_record_path(title)
            first_since = self.open_rows()[0]["since"]
            answered = self.run_command("answer", "--item", title)
            self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
            self.assertIn("status: closed", path.read_text().splitlines())
            moment = first + timedelta(minutes=minutes)
            _ = self.run_ask(title, "Keep this layout?")
        self.assertEqual(self.question_record_path(title), path)
        return first_since, self.open_rows()[0]["since"]

    def test_reopening_a_question_in_a_later_minute_changes_since(self) -> None:
        first, reopened = self.check_question_reopening(1)
        self.assertNotEqual(reopened, first)

    def test_same_minute_reopening_keeps_since_as_a_documented_limit(self) -> None:
        first, reopened = self.check_question_reopening(0)
        self.assertEqual(reopened, first)

    def test_question_from_a_linked_checkout_uses_the_showrunner_relative_proposals_folder(self) -> None:
        main = self.checkout
        relative = self.doc.relative_to(main)
        _ = self.doc.write_text("\n".join(
            "- **Proposals:** `questions`" if line.startswith("- **Proposals:**") else line
            for line in self.doc.read_text().splitlines()) + "\n")
        _ = self.git("add", str(relative))
        _ = self.git("commit", "-m", "Use a relative question folder")
        owner = self.root / "question-owner-checkout"
        unit = self.root / "question-unit-checkout"
        _ = self.git("worktree", "add", "-b", "question-owner", str(owner))
        _ = self.git("worktree", "add", "-b", "question", str(unit))
        owner_id = self.fixture_staff_id()
        asker_id = self.fixture_staff_id("alpha")
        _ = staff.set_doc(owner_id, staff.LiveDoc("natedev", owner / relative))
        self.checkout = unit
        self.doc = unit / relative
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Keep this layout?")):
            filed = self.run_command("ask", "--text", "Choose the shared layout", "--question", "Keep this layout?",
                                     "--unit", str(asker_id))
        self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        records = list((owner / "questions").glob("question-*.md"))
        self.assertEqual(len(records), 1)
        self.assertFalse((unit / "questions").exists())
        self.assertIn(f"asked_by: {asker_id}", records[0].read_text().splitlines())
        self.checkout = owner
        self.doc = owner / relative
        listed = self.run_command("questions-table")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("Choose the shared layout", listed.stdout)
        self.assertIn("waiting for your answer", listed.stdout)

    def ambiguous_question_document(self) -> tuple[Path, staff.StaffId, staff.StaffId]:
        relative = self.doc.relative_to(self.checkout)
        other = self.root / "other-owner-checkout"
        caller = self.root / "ambiguous-caller-checkout"
        _ = self.git("worktree", "add", "-b", "other-question-owner", str(other))
        _ = self.git("worktree", "add", "-b", "ambiguous-question-caller", str(caller))
        second = fixture_member("showrunner", name="other-question-owner",
                                doc=staff.LiveDoc("natedev", other / relative))
        return caller / relative, self.fixture_staff_id(), second.id

    def test_ask_uses_the_staff_name_when_checkouts_share_a_document_path(self) -> None:
        document, first, second = self.ambiguous_question_document()
        asker_id = self.fixture_staff_id("alpha")
        staff_file = self.state / "staff/staff.json"
        before = staff_file.read_bytes()
        self.checkout = document.parents[2]
        self.doc = document
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Keep this layout?")):
            result = self.run_command("ask", "--text", "Choose the layout", "--question", "Keep this layout?",
                                      "--unit", str(asker_id))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        question, = (self.root / "issues").glob("question-*.md")
        self.assertIn(f"production_id: {first}", question.read_text().splitlines())
        self.assertNotIn(f"production_id: {second}", question.read_text().splitlines())
        self.assertEqual(staff_file.read_bytes(), before)

    def test_footer_sources_reports_ambiguous_document_ownership_without_a_traceback(self) -> None:
        import footer_counts

        document, first, second = self.ambiguous_question_document()
        staff_file = self.state / "staff/staff.json"
        before = staff_file.read_bytes(), staff_file.stat().st_mtime_ns
        def read_sources() -> int:
            for path in footer_counts.sources(document):
                print(path)
            return 0

        result = invoke_cli(footer_counts, read_sources, [], self.env, self.checkout, production_state=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(document), result.stdout.splitlines())
        self.assertIn("unavailable", result.stderr)
        self.assertIn(str(first), result.stderr)
        self.assertIn(str(second), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((staff_file.read_bytes(), staff_file.stat().st_mtime_ns), before)

    def test_unit_files_and_lists_questions_from_linked_and_main_checkouts(self) -> None:
        main = self.checkout
        relative = self.doc.relative_to(main)
        owner_id = self.fixture_staff_id()
        asker_id = self.fixture_staff_id("alpha")
        owner = self.root / "owner-checkout"
        unit = self.root / "unit-checkout"
        _ = self.git("worktree", "add", "-b", "owner-copy", str(owner))
        _ = self.git("worktree", "add", "-b", "unit-copy", str(unit))
        _ = staff.set_doc(owner_id, staff.LiveDoc("natedev", owner / relative))
        staff_file = self.state / "staff/staff.json"
        before = staff_file.read_bytes()
        for checkout, title in ((unit, "Unit checkout question"), (main, "Main checkout question")):
            with self.subTest(checkout=checkout):
                self.checkout: Path = checkout
                self.doc: Path = relative
                with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Keep this layout?")):
                    filed = self.run_command("ask", "--text", title, "--question", "Keep this layout?",
                                             "--unit", str(asker_id))
                self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
                listed = self.run_command("questions-table")
                self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
                self.assertIn(title, listed.stdout)
        records = list((self.root / "issues").glob("question-*.md"))
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIn(f"asked_by: {asker_id}", record.read_text().splitlines())
            self.assertIn(f"production_id: {owner_id}", record.read_text().splitlines())
        self.assertEqual(staff_file.read_bytes(), before)

    def test_question_without_a_proposals_line_can_be_filed_listed_and_answered(self) -> None:
        _ = self.doc.write_text("\n".join(line for line in self.doc.read_text().splitlines()
                                         if not line.startswith("- **Proposals:**")) + "\n")
        owner = self.fixture_staff_id()
        folder = self.state / "showrunner" / owner / "questions"
        state = self.question_state([{"text": "Choose the layout", "question": "Keep this layout?"}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Keep this layout?")):
            filed = self.run_command("questions", "--state", str(state))
        self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        records = list(folder.glob("question-*.md"))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIn(f"asked_by: {owner}", record.read_text().splitlines())
        self.assertEqual(self.open_rows()[0]["asked_by"], owner)
        self.assertFalse(self.outstanding().exists())
        self.assertEqual(list((self.root / "issues").iterdir()), [])

        listed = self.run_command("questions-table")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("Choose the layout", listed.stdout)
        self.assertIn("waiting for your answer", listed.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertIn("answer: ok", answered.stdout)
        self.assertIn("status: closed", record.read_text().splitlines())
        self.assertEqual(self.open_rows(), [])
        listed_after_answer = self.run_command("questions-table")
        self.assertEqual(listed_after_answer.returncode, 0, listed_after_answer.stdout + listed_after_answer.stderr)
        self.assertEqual(listed_after_answer.stdout.strip(), "no open questions")

    def test_question_migration_without_a_proposals_line_preserves_records_on_a_rerun(self) -> None:
        _ = self.doc.write_text("\n".join(line for line in self.doc.read_text().splitlines()
                                         if not line.startswith("- **Proposals:**")) + "\n")
        owner = self.fixture_staff_id()
        folder = self.state / "showrunner" / owner / "questions"
        question = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                    "question": "Keep this layout?", "asked_by": str(owner)}
        reminder = {"since": "2026-10-06T09:00", "text": "Review the colors"}
        legacy = self.outstanding()
        _ = legacy.write_text(json.dumps([question, reminder]) + "\n")
        migrated = self.run_command("migrate-questions")
        self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
        records = list(folder.glob("question-*.md"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(legacy.read_text()), [reminder])
        self.assertEqual(next(row for row in self.open_rows() if "question" in row), question)
        self.assertEqual(list((self.root / "issues").iterdir()), [])
        paths = [legacy, *records]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        repeated = self.run_command("migrate-questions")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(list(folder.glob("question-*.md")), records)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)

    def test_stopped_list_rewrites_keep_prior_questions_and_ask_retry_registers_once(self) -> None:
        for action in ("ask", "mark-important", "questions"):
            with self.subTest(action=action):
                self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "Reminder"},
                                    {"since": "2026-10-06T08:00", "text": "unit question?", "question": "Keep the layout?",
                                     "asked_by": "alpha"})
                path = self.outstanding()
                history = path.with_suffix(".history.jsonl")
                before = path.read_bytes()
                command = (("ask", "--text", "new question?", "--question", "Try the new layout?", "--unit", "alpha")
                           if action == "ask" else ("mark-important", "--item", "Reminder")
                           if action == "mark-important" else
                           ("questions", "--state", str(self.question_state(["Reminder", "New reminder"]))))
                real_replace = os.replace

                def stop(source: Path, destination: Path) -> None:
                    target = Path(destination)
                    if target == path or (action == "ask" and target.suffix == ".md"
                                          and "record_type: question" in Path(source).read_text()):
                        raise OSError("question list replacement stopped")
                    real_replace(source, destination)

                with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Try the new layout?")):
                    with mock.patch.object(os, "replace", side_effect=stop):
                        failed = self.run_command(*command)
                    self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
                    self.assertIn("question list replacement stopped", failed.stdout)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual([item.text for item in self.read_items(path)], ["Reminder", "unit question?"])
                    if action == "ask":
                        entries = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                        self.assertEqual(sum(item["text"] == "new question?" for item in entries), 1)
                    retried = self.run_command(*command)
                self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
                saved = self.open_rows()
                unit_question = next(item for item in saved if item["text"] == "unit question?")
                self.assertEqual((unit_question["question"], unit_question["asked_by"]),
                                 ("Keep the layout?", self.fixture_staff_id("alpha")))
                if action == "ask":
                    self.assertEqual(sum(item["text"] == "new question?" for item in saved), 1)
                    entries = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                    matching = [item for item in entries if item["text"] == "new question?"]
                    self.assertEqual(len(matching), 1)
                    self.assertEqual(matching[0]["question"], "Try the new layout?")
                elif action == "mark-important":
                    self.assertEqual(next(item for item in saved if item["text"] == "Reminder")["urgency"], "user")
                else:
                    self.assertEqual([item["text"] for item in saved], ["Reminder", "New reminder", "unit question?"])

    def test_question_history_replacement_failure_preserves_history_and_open_list(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "Reminder"})
        path = self.outstanding()
        history = path.with_suffix(".history.jsonl")
        _ = history.write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "text": "Earlier registration"}) + "\n")
        before = path.read_bytes(), history.read_bytes()
        real_replace = os.replace

        def stop(source: Path, destination: Path) -> None:
            if Path(destination) == history:
                raise KeyboardInterrupt
            real_replace(source, destination)

        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            with mock.patch.object(os, "replace", side_effect=stop), self.assertRaises(KeyboardInterrupt):
                _ = self.run_ask("retry?", "Retry it?", "alpha")
            self.assertEqual((path.read_bytes(), history.read_bytes()), before)
            _ = self.run_ask("retry?", "Retry it?", "alpha")
        self.assertEqual([item.text for item in self.read_items(path)], ["Reminder", "retry?"])
        entries = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([item["text"] for item in entries], ["Earlier registration", "retry?"])

    def test_stopped_report_replacement_preserves_report_and_rerun_updates_it(self) -> None:
        reports = self.outstanding().parent.parent / "reports" / self.outstanding().name
        reports.parent.mkdir(parents=True)
        _ = reports.write_text('[{"text":"Earlier report","until":"2026-10-17T09:00"}]\n')
        before = reports.read_bytes()
        state = self.root / "judgment.json"
        replacement = [{"text": "Updated report", "until": "2026-10-18T09:00"}]
        _ = state.write_text(json.dumps({"report_to_user": replacement}))
        real_replace = os.replace

        def stop(source: Path, destination: Path) -> None:
            if Path(destination) == reports:
                raise KeyboardInterrupt
            real_replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            _ = self.run_command("questions", "--state", str(state))
        self.assertEqual(reports.read_bytes(), before)
        retried = self.run_command("questions", "--state", str(state))
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(reports.read_text()), replacement)

    def test_questions_state_updates_outstanding_only_when_open_items_are_present(self) -> None:
        outstanding = self.outstanding()
        saved = [{"since": "2026-10-01T08:00", "text": "review colors"}]
        _ = outstanding.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        absent = self.run_command("questions", "--state", str(state))
        self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": ["review colors"]}), encoding="utf-8")
        retained = self.run_command("questions", "--state", str(state))
        self.assertEqual(retained.returncode, 0, retained.stdout + retained.stderr)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual(entries[0]["since"], "2026-10-01T08:00")
        _ = state.write_text(json.dumps({"open_for_user": []}), encoding="utf-8")
        cleared = self.run_command("questions", "--state", str(state))
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertEqual(json.loads(outstanding.read_text(encoding="utf-8")), [])

    def test_questions_state_replaces_reports_only_when_report_to_user_is_present(self) -> None:
        written = self.outstanding().parent.parent / "reports" / self.outstanding().name
        state = self.root / "judgment.json"
        report = {"text": "hanadocs is back", "until": "2026-10-17T09:00"}
        _ = state.write_text(json.dumps({"report_to_user": [report]}), encoding="utf-8")
        saved = self.run_command("questions", "--state", str(state))
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), [report])
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        kept = self.run_command("questions", "--state", str(state))
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), [report])
        for bad in ([{"text": "x", "until": "tomorrow"}], [{"text": "", "until": "2026-10-17T09:00"}],
                    [{"text": "x", "until": "2026-10-17T09:00", "question": "?"}], "x"):
            with self.subTest(bad=bad):
                _ = state.write_text(json.dumps({"report_to_user": bad}), encoding="utf-8")
                refused = self.run_command("questions", "--state", str(state))
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn("report_to_user", refused.stdout)
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), [report])

    def test_questions_records_only_new_questions_and_prunes_history_older_than_two_days(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text(json.dumps([{"since": "2026-10-01T08:00", "text": "Choose a compiler."}]))
        now = datetime.now(timezone.utc)
        history = outstanding.with_suffix(".history.jsonl")
        records = [QuestionRegistration(time=(now - timedelta(hours=49)).isoformat(), text="Expired question?"),
                   QuestionRegistration(time=(now - timedelta(hours=1)).isoformat(), text="Recent question?")]
        _ = history.write_text("\n".join(json.dumps(item) for item in records) + "\n")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["Choose a compiler.", "Approve the release."]}))
        for _ in range(2):
            result = self.run_command("questions", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = [cast(QuestionRegistration, json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([item["text"] for item in saved], ["Recent question?", "Approve the release."])
        self.assertEqual(saved[0], records[1])
        stamp = datetime.fromisoformat(saved[1]["time"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertGreaterEqual(stamp, now)

    def test_questions_keeps_registration_history_when_the_question_closes(self) -> None:
        outstanding = self.outstanding()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["Would you retry the build?"]}))
        opened = self.run_command("questions", "--state", str(state))
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        history = outstanding.with_suffix(".history.jsonl")
        before = history.read_bytes()
        _ = state.write_text(json.dumps({"open_for_user": []}))
        closed = self.run_command("questions", "--state", str(state))
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertEqual(json.loads(outstanding.read_text()), [])
        self.assertEqual(history.read_bytes(), before)
        saved = cast(QuestionRegistration, json.loads(before.decode().strip()))
        self.assertEqual(saved["text"], "Would you retry the build?")
        self.assertIsNotNone(datetime.fromisoformat(saved["time"]).tzinfo)

    def test_questions_rejects_bad_deferred_item_before_writing_outstanding(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"review colors"}]\n', encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}), encoding="utf-8")
        result = self.run_command("questions", "--state", str(state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("questions: failed", result.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_a_stored_item_with_plumbing_is_named_and_a_new_one_is_refused(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"free the cargo-berth on the Mac"}]\n',
                                   encoding="utf-8")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["free the cargo-berth on the Mac", "review colors"]}),
                             encoding="utf-8")
        kept = self.run_command("questions", "--state", str(state))
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertIn("questions: reword — free the cargo-berth on the Mac: 'berth' is the production's own plumbing",
                      kept.stdout)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual([entry["text"] for entry in entries], ["free the cargo-berth on the Mac", "review colors"])
        before = outstanding.read_bytes()
        _ = state.write_text(json.dumps({"open_for_user": ["review colors", "release the writers"]}), encoding="utf-8")
        refused = self.run_command("questions", "--state", str(state))
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("questions: failed — release the writers: 'writers' is the production's own plumbing",
                      refused.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_questions_store_what_waits_on_the_user_and_print_one_line(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"choose layout"}]')
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [
            "review colors", {"text": "choose layout", "after": "2099-01-01T08:00"}]}), encoding="utf-8")
        stored = self.run_command("questions", "--state", str(state))
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        # The reply carries no footer: the user reads the counts in the status line.
        self.assertEqual(stored.stdout.strip(), "questions: ok — recorded")
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual([entry["text"] for entry in entries], ["review colors", "choose layout"])
        self.assertEqual(entries[1]["after"], "2099-01-01T08:00")
        before = outstanding.read_bytes()
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        kept = self.run_command("questions", "--state", str(state))
        self.assertEqual(kept.stdout.strip(), "questions: ok — recorded")
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}),
                             encoding="utf-8")
        bad = self.run_command("questions", "--state", str(state))
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        self.assertIn("questions: failed", bad.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_questions_store_a_level_and_keep_the_users_mark(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"choose layout","urgency":"user",'
                                   + '"marked":"2026-10-01T09:00"},{"since":"2026-10-01T08:00","text":"merge before the demo"}]')
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [
            {"text": "choose layout", "urgency": "later"},
            {"text": "merge before the demo", "urgency": "soon", "due": "2026-10-09T12:00", "follows": "choose layout"},
        ]}), encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            parsed = registration.judgment_state(state, "questions")
        self.assertIsInstance(parsed.outstanding, registration.ReplaceOutstanding)
        stored = self.run_command("questions", "--state", str(state))
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual({key: entries[0].get(key) for key in ("urgency", "marked")},
                         {"urgency": "user", "marked": "2026-10-01T09:00"})
        self.assertEqual({key: entries[1].get(key) for key in ("urgency", "due", "follows")},
                         {"urgency": "soon", "due": "2026-10-09T12:00", "follows": "choose layout"})
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "urgency": "soon"}]}),
                             encoding="utf-8")
        bad = self.run_command("questions", "--state", str(state))
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)


if __name__ == "__main__":
    _ = unittest.main()
