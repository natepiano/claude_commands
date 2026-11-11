"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Literal, cast
from unittest import mock

from scripts.lib import names
from scripts.lib.test_names import NAMES_TOML

from scripts.production.update_registration_test_support import RegistrationFixture, registration, dailies_render, review, read_production

from scripts.production.update_registration_test_support import setUpModule as setUpModule
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member


class RegistrationTests(RegistrationFixture):
    def test_withdraw_removes_one_open_item_without_an_answer_or_relay_and_retains_its_history(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "sync decision",
                                        "question": "Sync the document?", "asked_by": "alpha"},
                            {"since": "2026-10-06T08:00", "text": "Reminder"})
        previous = self.open_rows()
        path = self.outstanding()
        history = path.with_suffix(".history.jsonl")
        stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        saved = {"time": stamp, "text": "sync decision", "question": "Sync the document?", "reviewed": "Sync it?"}
        other = {**saved, "question": "Sync a different document?"}
        _ = history.write_text(json.dumps(saved) + "\n" + json.dumps(other) + "\n")
        answered = path.with_suffix(".answered.jsonl")
        _ = answered.write_text(json.dumps({"time": stamp, "slug": "example", "text": "Earlier decision"}) + "\n")
        answered_before = answered.read_bytes()
        before = datetime.now(timezone.utc)
        result = self.run_command("withdraw", "--item", "  sync decision  ")
        after = datetime.now(timezone.utc)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["withdraw: ok — example: sync decision"])
        self.assertNotIn("relay:", result.stdout)
        self.assertEqual(self.open_rows(), [previous[0]])
        self.assertEqual(answered.read_bytes(), answered_before)
        records = [cast(dict[str, str], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        withdrawn = records[0].pop("withdrawn")
        self.assertEqual(records, [saved, other])
        self.assertLessEqual(before, datetime.fromisoformat(withdrawn))
        self.assertLessEqual(datetime.fromisoformat(withdrawn), after)
        _ = self.run_questions(self.question_state(["Reminder", "Another reminder"]))
        retained = [cast(dict[str, str], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(retained[0], {**saved, "withdrawn": withdrawn})

    def test_withdraw_records_a_descendant_item_without_existing_history(self) -> None:
        for slug in ("top", "example", "kid"):
            self.timer(slug)
        self.attach("example", "top")
        self.attach("kid", "example")
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "sync decision",
                                    "question": "Sync the document?", "asked_by": "kid"})
        self.write_open_for("top", {"since": "2026-10-06T08:00", "text": "sync decision"})
        path = self.outstanding().with_name(f"{self.fixture_staff_id(name='kid')}.json")
        before = datetime.now(timezone.utc)
        result = self.run_command("withdraw", "--item", "sync decision")
        after = datetime.now(timezone.utc)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["withdraw: ok — kid: sync decision"])
        repeated = self.run_command("withdraw", "--item", "sync decision")
        self.assertEqual(repeated.returncode, 2, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout.strip(),
                         "withdraw: failed — 0 open items match 'sync decision'; name one by its exact text")
        saved = cast(dict[str, str], json.loads(path.with_suffix(".history.jsonl").read_text()))
        self.assertEqual((saved["text"], saved["question"]), ("sync decision", "Sync the document?"))
        self.assertEqual(saved["time"], saved["withdrawn"])
        self.assertLessEqual(before, datetime.fromisoformat(saved["withdrawn"]))
        self.assertLessEqual(datetime.fromisoformat(saved["withdrawn"]), after)
        above = self.outstanding().with_name(f"{self.fixture_staff_id(name='top')}.json")
        self.assertEqual([item["text"] for item in cast(list[dict[str, str]], json.loads(above.read_text()))],
                         ["sync decision"])

    def test_withdraw_requires_exact_open_text_and_changes_no_file_on_failure(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "sync decision",
                                        "question": "Sync the document?", "asked_by": "alpha"})
        answered = self.outstanding().with_suffix(".answered.jsonl")
        _ = answered.write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                            "slug": "example", "text": "Earlier decision"}) + "\n")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for wanted in ("sync", "Sync the document?", "Earlier decision", "missing"):
            with self.subTest(wanted=wanted):
                result = self.run_command("withdraw", "--item", wanted)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(),
                                 f"withdraw: failed — 0 open items match {wanted!r}; name one by its exact text")
                self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_withdraw_refuses_the_same_exact_text_in_two_open_lists_without_changes(self) -> None:
        for slug in ("example", "kid"):
            self.timer(slug)
            self.write_open_for(slug, {"since": "2026-10-06T08:00", "text": "sync decision"})
        self.attach("kid", "example")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = self.run_command("withdraw", "--item", "sync decision")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(),
                         "withdraw: failed — 2 open items match 'sync decision'; name one by its exact text")
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_ask_files_a_units_question_with_its_review_and_shows_the_asker(self) -> None:
        full = "Keep the old key binding? Keep (recommended — users know it) or drop it."
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(full + " Reviewed.")):
            printed = self.run_ask("alpha: keep the old key binding?", full, "alpha")
        self.assertIn("filed question: " + full + " Reviewed.", printed)
        self.assertNotIn("reviewed question:", printed)
        rows = self.open_rows()
        self.assertEqual([(row["text"], row["asked_by"], row["question"]) for row in rows],
                         [("alpha: keep the old key binding?", self.fixture_staff_id("alpha"), full)])
        self.assertEqual(rows[0]["reviewed"], full + " Reviewed.")
        history = self.outstanding().with_suffix(".history.jsonl").read_text()
        self.assertIn("alpha: keep the old key binding?", history)
        listed = self.open_lines()
        self.assertRegex(listed[0], r"^1\. example, since .*, asked by alpha: " + re.escape(full) + "$")

    def test_ask_refuses_an_unknown_unit_a_duplicate_and_a_held_review_without_writing(self) -> None:
        with self.assertRaisesRegex(registration.RegistrationFailure, "beta is not a unit of example"):
            _ = self.run_ask("which?", "Which one?", "beta")
        held = review.QuestionAwaitingFacts("Retry it?", ("say which build",), ())
        with (mock.patch.object(review, "review_question", return_value=held),
              self.assertRaisesRegex(registration.RegistrationFailure, "question held for review: retry\\?: say which build")):
            _ = self.run_ask("retry?", "Retry it?", "alpha")
        self.assertFalse(self.outstanding().exists())
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            _ = self.run_ask("retry?", "Retry it?", "alpha")
            with self.assertRaisesRegex(registration.RegistrationFailure, "already open"):
                _ = self.run_ask("retry?", "Retry it?", "alpha")

    def test_a_list_rewrite_keeps_a_filed_question_and_answer_names_the_unit(self) -> None:
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            _ = self.run_ask("retry?", "Retry it?", "alpha")
        state = self.question_state(["Reminder"])
        kept = self.run_questions(state)
        self.assertIn(f"questions: kept — retry?: asked by {self.fixture_staff_id('alpha')}", kept)
        rows = self.open_rows()
        self.assertEqual([row["text"] for row in rows], ["Reminder", "retry?"])
        self.assertEqual(rows[1]["asked_by"], self.fixture_staff_id("alpha"))
        # A rewrite that names the item keeps its asker too.
        _ = self.run_questions(self.question_state(["Reminder", "retry?"]))
        self.assertEqual(self.open_rows()[1]["asked_by"], self.fixture_staff_id("alpha"))
        answered = self.run_command("answer", "--item", "retry?")
        self.assertEqual(answered.stdout.splitlines(),
                         [f"relay: to alpha (staff {self.fixture_staff_id('alpha')})",
                          "answer: ok — example: retry?"],
                         answered.stdout + answered.stderr)
        self.assertEqual([row["text"] for row in self.open_rows()],
                         ["Reminder"])
        reminder = self.run_command("answer", "--item", "Reminder")
        self.assertEqual(reminder.stdout.splitlines()[0], "relay: none; the item belongs to this production")
        repeated = self.run_command("answer", "--item", "retry?")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertRegex(repeated.stdout, r"already answered \w{3} \w{3} \d{2} \d{2}:\d{2}")
        self.assertIn("relay: to alpha", repeated.stdout)

    def test_answer_rerun_recovers_relay_after_list_replacement_completed_then_stopped(self) -> None:
        self.check_answer_write_interruption(after_replace=True)

    def test_answer_replay_names_the_renamed_asking_unit(self) -> None:
        self.check_answer_replay_after_staff_change("unit rename")

    def test_answer_replay_names_the_renamed_production_owner(self) -> None:
        self.check_answer_replay_after_staff_change("production rename")

    def test_answer_replay_names_the_asking_units_new_production(self) -> None:
        self.check_answer_replay_after_staff_change("unit move")

    def check_answer_replay_after_staff_change(
            self, change: Literal["unit rename", "production rename", "unit move"]) -> None:
        self.timer("kid")
        self.attach("kid", "example")
        owner = self.fixture_staff_id(name="kid")
        unit = fixture_member("unit", name="requester", parent=owner)
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "retry?",
                                    "question": "Retry it?", "asked_by": unit.id})
        first = self.run_command("answer", "--item", "retry?")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = next((self.state / "showrunner/outstanding").glob("*.answered.jsonl"))
        before = path.read_bytes()
        saved = cast(dict[str, str], json.loads(before))
        answered_at = datetime.fromisoformat(saved["time"])
        if change == "unit rename":
            unit = staff.rename(unit.id, "renamed-requester")
            destination, item_owner = "kid", "kid"
        elif change == "production rename":
            renamed = staff.rename(owner, "renamed-kid")
            destination, item_owner = renamed.name, renamed.name
        else:
            moved_owner = fixture_member("showrunner", name="elsewhere")
            unit = staff.move(unit.id, staff.ReportsTo(moved_owner.id))
            destination, item_owner = moved_owner.name, "kid"

        repeated = self.run_command("answer", "--item", "retry?")

        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout.splitlines(), [
            f"already answered {answered_at:%a %b %d %H:%M}",
            f"relay: to the showrunner of {destination}, for {unit.name} (staff {unit.id})",
            f"answer: ok — {item_owner}: retry?",
        ])
        self.assertEqual(path.read_bytes(), before)

    def test_answer_rerun_removes_open_item_after_answer_record_was_saved(self) -> None:
        self.check_answer_write_interruption(after_replace=False)

    def test_answer_rerun_prefers_the_exact_answer_over_another_open_items_prefix(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry", "question": "Retry it?",
                                        "asked_by": self.fixture_staff_id("alpha")},
                            {"since": "2026-10-06T08:00", "text": "retry later", "question": "Retry later?",
                             "asked_by": self.fixture_staff_id("beta")})
        path = self.outstanding()
        record = self.question_record_path("retry")
        real_replace = os.replace

        def stop(source: Path, destination: Path) -> None:
            real_replace(source, destination)
            if Path(destination) == record:
                raise KeyboardInterrupt

        with mock.patch.object(os, "replace", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            _ = self.run_command("answer", "--item", "retry")
        before = path.read_bytes()
        repeated = self.run_command("answer", "--item", "retry")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("already answered", repeated.stdout)
        self.assertIn("relay: to alpha", repeated.stdout)
        self.assertNotIn("relay: to beta", repeated.stdout)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([item.text for item in self.read_items(path)], ["retry later"])

    def test_answer_rerun_matches_the_saved_full_question_after_a_stopped_rewrite(self) -> None:
        question = "Should we preserve the older shortcut?"
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "shortcut decision",
                                        "question": question, "asked_by": self.fixture_staff_id("alpha")})
        path = self.outstanding()
        record = self.question_record_path("shortcut decision")
        real_replace = os.replace

        def stop(source: Path, destination: Path) -> None:
            real_replace(source, destination)
            if Path(destination) == record:
                raise KeyboardInterrupt

        with mock.patch.object(os, "replace", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            _ = self.run_command("answer", "--item", "older shortcut")
        repeated = self.run_command("answer", "--item", "older shortcut")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("already answered", repeated.stdout)
        self.assertIn("relay: to alpha", repeated.stdout)
        self.assertEqual(self.read_items(path), [])
        saved = cast(dict[str, object], json.loads(path.with_suffix(".answered.jsonl").read_text()))
        self.assertEqual(saved["question"], question)

    def check_answer_write_interruption(self, *, after_replace: bool) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry?", "question": "Retry it?",
                                        "asked_by": self.fixture_staff_id("alpha")},
                            {"since": "2026-10-06T08:00", "text": "Reminder"})
        path = self.outstanding()
        before = path.read_bytes()
        record = self.question_record_path("retry?")
        record_before = record.read_bytes()
        answered_path = path.with_suffix(".answered.jsonl")
        real_replace = os.replace

        def stop(source: Path, destination: Path) -> None:
            if Path(destination) == record:
                records = [cast(dict[str, object], json.loads(line))
                           for line in answered_path.read_text().splitlines()]
                self.assertEqual([(item["text"], item["asked_by"]) for item in records],
                                 [("retry?", self.fixture_staff_id("alpha"))])
                if after_replace:
                    real_replace(source, destination)
                raise KeyboardInterrupt
            real_replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            _ = self.run_command("answer", "--item", "retry?")
        if after_replace:
            self.assertEqual([item.text for item in self.read_items(path)], ["Reminder"])
        else:
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(record.read_bytes(), record_before)
        repeated = self.run_command("answer", "--item", "retry?")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("relay: to alpha", repeated.stdout)
        self.assertEqual("already answered" in repeated.stdout, after_replace)
        self.assertEqual([item.text for item in self.read_items(path)], ["Reminder"])
        records = [cast(dict[str, object], json.loads(line)) for line in answered_path.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["name"], records[0]["text"], records[0]["asked_by"]),
                         ("example", "retry?", self.fixture_staff_id("alpha")))

    def test_answered_record_expiry_and_legacy_asker_preserve_replay_rules(self) -> None:
        path = self.outstanding().with_suffix(".answered.jsonl")
        now = datetime.now(timezone.utc)
        records: list[dict[str, str]] = [
            {"time": (now - timedelta(hours=49)).isoformat(), "name": "example", "text": "expired?", "asked_by": self.fixture_staff_id("alpha")},
            {"time": (now - timedelta(hours=1)).isoformat(), "name": "example", "text": "legacy?"},
        ]
        _ = path.write_text("".join(json.dumps(item) + "\n" for item in records))
        before = path.read_bytes()
        expired = self.run_command("answer", "--item", "expired?")
        self.assertEqual(expired.returncode, 2, expired.stdout + expired.stderr)
        self.assertNotIn("already answered", expired.stdout)
        legacy = self.run_command("answer", "--item", "legacy?")
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        self.assertIn("already answered", legacy.stdout)
        self.assertIn("relay:", legacy.stdout)
        self.assertIn("not recorded", legacy.stdout)
        self.assertNotIn("relay: to alpha", legacy.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_answer_replay_prefers_exact_text_and_refuses_ambiguous_partial_matches(self) -> None:
        path = self.outstanding().with_suffix(".answered.jsonl")
        stamp = datetime.now(timezone.utc).isoformat()
        records = [{"time": stamp, "name": "example", "text": text, "asked_by": asker}
                   for text, asker in (("retry", self.fixture_staff_id("alpha")),
                                       ("retry later", self.fixture_staff_id("showrunner")))]
        _ = path.write_text("".join(json.dumps(item) + "\n" for item in records))
        exact = self.run_command("answer", "--item", "retry")
        self.assertEqual(exact.returncode, 0, exact.stdout + exact.stderr)
        self.assertIn("relay: to alpha", exact.stdout)
        ambiguous = self.run_command("answer", "--item", "retr")
        self.assertEqual(ambiguous.returncode, 2, ambiguous.stdout + ambiguous.stderr)
        self.assertNotIn("relay:", ambiguous.stdout)

    def test_answer_matching_stops_at_the_first_nonempty_priority_level(self) -> None:
        path = self.outstanding()
        journal = path.with_suffix(".answered.jsonl")
        stamp = datetime.now(timezone.utc).isoformat()
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry",
                                        "question": "Retry now?", "asked_by": self.fixture_staff_id("alpha")})
        _ = journal.write_text(json.dumps({"time": stamp, "name": "example", "text": "retry",
                                          "asked_by": self.fixture_staff_id("beta")}) + "\n")
        opened = self.run_command("answer", "--item", "retry")
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        self.assertIn("relay: to alpha", opened.stdout)
        self.assertNotIn("already answered", opened.stdout)

        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry later",
                                        "question": "Retry later?", "asked_by": self.fixture_staff_id("alpha")})
        records = [{"time": stamp, "name": "example", "text": "retry", "asked_by": asker}
                   for asker in (self.fixture_staff_id("alpha"), self.fixture_staff_id("beta"))]
        _ = journal.write_text("".join(json.dumps(saved) + "\n" for saved in records))
        before = path.read_bytes(), journal.read_bytes()
        ambiguous = self.run_command("answer", "--item", "retry")
        self.assertEqual(ambiguous.returncode, 2, ambiguous.stdout + ambiguous.stderr)
        self.assertIn("2 open items match", ambiguous.stdout)
        self.assertEqual((path.read_bytes(), journal.read_bytes()), before)

        _ = journal.write_text(json.dumps({"time": stamp, "name": "example", "text": "retry yesterday",
                                          "asked_by": self.fixture_staff_id("beta")}) + "\n")
        partial = self.run_command("answer", "--item", "retry")
        self.assertEqual(partial.returncode, 0, partial.stdout + partial.stderr)
        self.assertIn("relay: to alpha", partial.stdout)
        self.assertNotIn("already answered", partial.stdout)

    def test_missing_asker_context_preserves_the_callers_diagnostic_reason(self) -> None:
        opened = registration.OutstandingItem(since="2026-10-06T08:00", text="Reminder")
        saved = registration.AnsweredItem(time=datetime.now(timezone.utc).isoformat(),
                                          name="example", text="Reminder")
        open_asker = registration.question_asker(opened, registration.OpenItemWithoutAsker())
        saved_asker = registration.question_asker(saved,
                         registration.AskerNotRecorded("open item has no recorded asker"))
        with mock.patch.dict(os.environ, self.env):
            production = read_production("example")
        self.assertEqual(registration.relay_line(production, "example", open_asker),
                         "relay: none; the item belongs to this production")
        self.assertEqual(registration.relay_line(production, "example", saved_asker),
                         "relay: none; open item has no recorded asker; the item belongs to this production")

    def test_answer_reaches_a_list_below_and_relays_through_its_showrunner_never_above(self) -> None:
        for name in ("top", "example", "kid"):
            self.timer(name)
        self.attach("example", "top")
        self.attach("kid", "example")
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today?", "question": "Merge today?",
                                    "asked_by": "kid"})
        self.write_open_for("top", {"since": "2026-10-06T07:00", "text": "ship it?", "question": "Ship it?",
                                    "asked_by": "top"})
        answered = self.run_command("answer", "--item", "merge today?")
        self.assertEqual(answered.stdout.splitlines(),
                         [f"relay: to the showrunner of kid, for kid (staff {self.fixture_staff_id('kid', 'kid')})",
                          "answer: ok — kid: merge today?"],
                         answered.stdout + answered.stderr)
        self.assertEqual(json.loads(self.outstanding().with_name(f"{self.fixture_staff_id(name='kid')}.json").read_text()), [])
        self.assertEqual(self.run_command("answer", "--item", "ship it?").returncode, 2)

    def test_below_production_answer_uses_the_configured_role_name_on_first_send_and_replay(self) -> None:
        for name in ("example", "kid"):
            self.timer(name)
        self.attach("kid", "example")
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "ship today?",
                                    "question": "Ship today?", "asked_by": "showrunner"})
        config = self.root / "names.toml"
        _ = config.write_text(NAMES_TOML.replace('showrunner = ["showrunner"]',
                                                'showrunner = ["coordinator"]'))
        configured = names.load_names(config)
        self.assertIsInstance(configured, names.Names)
        with mock.patch.object(names, "runtime_names", return_value=configured):
            first = self.run_command("answer", "--item", "ship today?")
            repeated = self.run_command("answer", "--item", "ship today?")
        for result in (first, repeated):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("relay: to the showrunner of kid, for coordinator", result.stdout)
        self.assertIn("already answered", repeated.stdout)
        saved = cast(dict[str, object], json.loads(self.outstanding().with_name(f"{self.fixture_staff_id(name='kid')}.answered.jsonl").read_text()))
        self.assertEqual(saved["asked_by"], self.fixture_staff_id("showrunner", "kid"))

    def test_below_production_answer_keeps_a_relay_when_names_cannot_be_read(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            production = read_production("example")
        with mock.patch.object(names, "runtime_names", return_value=names.UnreadableNames("names.toml", "unavailable")):
            line = registration.relay_line(production, "kid", registration.ShowrunnerAsker())
        self.assertEqual(line, "relay: to the showrunner of kid, for requesting role (display name unavailable)")

    def test_answer_waits_for_the_lists_lock(self) -> None:
        for unrelated in (False, True):
            with self.subTest(unrelated=unrelated):
                self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry?", "question": "Retry it?",
                                                "asked_by": self.fixture_staff_id("alpha")})
                records = Path(self.env["HERDR_PANE_RECORD_DIR"])
                publication_lock = records / ".publication.lock"
                if unrelated:
                    records.mkdir(parents=True, exist_ok=True)
                    _ = (records / "agents.json").write_text("{}")
                lock = self.outstanding().with_suffix(".lock")
                holder = subprocess.Popen([sys.executable, "-c", (
                    "import fcntl, sys\n"
                    f"handle = open({str(lock)!r}, 'a')\n"
                    "fcntl.flock(handle, fcntl.LOCK_EX)\n"
                    "print('held', flush=True)\n"
                    "sys.stdin.read(1)\n")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                stdin = cast(io.TextIOWrapper, holder.stdin)
                stdout = cast(io.TextIOWrapper, holder.stdout)

                self.addCleanup(stdout.close)
                self.addCleanup(holder.wait, timeout=TEST_HANG_GUARD_SECS)
                self.addCleanup(stdin.close)
                self.assertEqual(stdout.readline().strip(), "held")
                real_flock = fcntl.flock
                contended: list[Path] = []
                passed: list[Path] = []

                def acquire(handle: io.TextIOWrapper | int, operation: int) -> None:
                    identity = os.fstat(handle if isinstance(handle, int) else handle.fileno())
                    if operation == fcntl.LOCK_EX and os.path.samestat(identity, lock.stat()):
                        if unrelated:
                            self.assertIn(publication_lock, passed)
                        # Prove that the other process holds the selected lock.
                        with self.assertRaises(BlockingIOError):
                            real_flock(handle, operation | fcntl.LOCK_NB)
                        contended.append(lock)
                        _ = stdin.write("x")
                        stdin.flush()
                    real_flock(handle, operation)
                    if (operation == fcntl.LOCK_EX and publication_lock.exists()
                            and os.path.samestat(identity, publication_lock.stat())):
                        passed.append(publication_lock)

                with mock.patch.object(fcntl, "flock", acquire):
                    answered = self.run_command("answer", "--item", "retry?")
                self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
                self.assertEqual(contended, [lock], "answer must acquire the held list lock before changing it")

    def test_the_reader_refuses_an_asker_without_a_question(self) -> None:
        self.timer("example")
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep this layout?", "asked_by": "alpha"})
        _ = self.outstanding().write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "x", "asked_by": self.fixture_staff_id("alpha")}]))
        with self.assertRaisesRegex(dailies_render.InputError, "question required"):
            _ = dailies_render.read_outstanding(self.outstanding())


if __name__ == "__main__":
    _ = unittest.main()
