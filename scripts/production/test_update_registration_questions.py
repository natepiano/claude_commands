"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from typing import cast
from unittest import mock

from scripts.production.update_registration_test_support import RegistrationFixture, QuestionRegistration, registration

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
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
