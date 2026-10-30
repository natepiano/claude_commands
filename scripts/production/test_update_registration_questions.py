"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.production.update_registration_test_support import RegistrationFixture, QuestionRegistration, registration, dailies_render, review

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_stopped_list_rewrites_keep_prior_questions_and_ask_retry_registers_once(self) -> None:
        for action in ("ask", "mark-important", "questions"):
            with self.subTest(action=action):
                self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "Reminder"},
                                    {"since": "2026-10-06T08:00", "text": "unit question?", "question": "Keep the layout?",
                                     "asked_by": "alpha-unit"})
                path = self.outstanding()
                history = path.with_suffix(".history.jsonl")
                before = path.read_bytes()
                command = (("ask", "--text", "new question?", "--question", "Try the new layout?", "--unit", "alpha-unit")
                           if action == "ask" else ("mark-important", "--item", "Reminder")
                           if action == "mark-important" else
                           ("questions", "--state", str(self.question_state(["Reminder", "New reminder"]))))
                real_replace = os.replace

                def stop(source: Path, destination: Path) -> None:
                    if Path(destination) == path:
                        raise OSError("question list replacement stopped")
                    real_replace(source, destination)

                with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Try the new layout?")):
                    with mock.patch.object(os, "replace", side_effect=stop):
                        failed = self.run_command(*command)
                    self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
                    self.assertIn("question list replacement stopped", failed.stdout)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual([item.text for item in dailies_render.read_outstanding(path)], ["Reminder", "unit question?"])
                    if action == "ask":
                        entries = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                        self.assertEqual(sum(item["text"] == "new question?" for item in entries), 1)
                    retried = self.run_command(*command)
                self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
                saved = cast(list[dict[str, object]], json.loads(path.read_text()))
                unit_question = next(item for item in saved if item["text"] == "unit question?")
                self.assertEqual((unit_question["question"], unit_question["asked_by"]), ("Keep the layout?", "alpha-unit"))
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
                _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
            self.assertEqual((path.read_bytes(), history.read_bytes()), before)
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
        self.assertEqual([item.text for item in dailies_render.read_outstanding(path)], ["Reminder", "retry?"])
        entries = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([item["text"] for item in entries], ["Earlier registration", "retry?"])

    def test_stopped_report_replacement_preserves_report_and_rerun_updates_it(self) -> None:
        reports = self.outstanding().parent.parent / "reports/example.json"
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
        written = self.outstanding().parent.parent / "reports/example.json"
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
