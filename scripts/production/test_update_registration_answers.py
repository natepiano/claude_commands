"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import fcntl
import io
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.production.update_registration_test_support import RegistrationFixture, registration, dailies_render, review, TEST_HANG_GUARD_SECS

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_ask_files_a_units_question_with_its_review_and_shows_the_asker(self) -> None:
        full = "Keep the old key binding? Keep (recommended — users know it) or drop it."
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(full + " Reviewed.")):
            printed = self.run_ask("alpha-unit: keep the old key binding?", full, "alpha-unit")
        self.assertIn("filed question: " + full + " Reviewed.", printed)
        self.assertNotIn("reviewed question:", printed)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([(row["text"], row["asked_by"], row["question"]) for row in rows],
                         [("alpha-unit: keep the old key binding?", "alpha-unit", full)])
        self.assertEqual(rows[0]["reviewed"], full + " Reviewed.")
        history = self.outstanding().with_suffix(".history.jsonl").read_text()
        self.assertIn("alpha-unit: keep the old key binding?", history)
        listed = self.open_lines()
        self.assertRegex(listed[0], r"^1\. example, since .*, asked by alpha-unit: " + re.escape(full) + "$")

    def test_ask_refuses_an_unknown_unit_a_duplicate_and_a_held_review_without_writing(self) -> None:
        with self.assertRaisesRegex(registration.RegistrationFailure, "beta-unit is not a unit of example"):
            _ = self.run_ask("which?", "Which one?", "beta-unit")
        held = review.QuestionAwaitingFacts("Retry it?", ("say which build",), ())
        with (mock.patch.object(review, "review_question", return_value=held),
              self.assertRaisesRegex(registration.RegistrationFailure, "question held for review: retry\\?: say which build")):
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
        self.assertFalse(self.outstanding().exists())
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
            with self.assertRaisesRegex(registration.RegistrationFailure, "already open"):
                _ = self.run_ask("retry?", "Retry it?", "alpha-unit")

    def test_a_list_rewrite_keeps_a_filed_question_and_answer_names_the_unit(self) -> None:
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
        state = self.question_state(["Reminder"])
        kept = self.run_questions(state)
        self.assertIn("questions: kept — retry?: asked by alpha-unit", kept)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([row["text"] for row in rows], ["Reminder", "retry?"])
        self.assertEqual(rows[1]["asked_by"], "alpha-unit")
        # A rewrite that names the item keeps its asker too.
        _ = self.run_questions(self.question_state(["Reminder", "retry?"]))
        self.assertEqual(cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))[1]["asked_by"], "alpha-unit")
        answered = self.run_command("answer", "--item", "retry?")
        self.assertEqual(answered.stdout.splitlines(), ["relay: to alpha-unit", "answer: ok — example: retry?"],
                         answered.stdout + answered.stderr)
        self.assertEqual([row["text"] for row in cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))],
                         ["Reminder"])
        reminder = self.run_command("answer", "--item", "Reminder")
        self.assertEqual(reminder.stdout.splitlines()[0], "relay: none; the item belongs to this production")
        self.assertEqual(self.run_command("answer", "--item", "retry?").returncode, 2)

    def test_answer_reaches_a_list_below_and_relays_through_its_showrunner_never_above(self) -> None:
        for slug in ("top", "example", "kid"):
            self.timer(slug)
        self.attach("example", "top")
        self.attach("kid", "example")
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today?", "question": "Merge today?",
                                    "asked_by": "kid-unit"})
        self.write_open_for("top", {"since": "2026-10-06T07:00", "text": "ship it?", "question": "Ship it?",
                                    "asked_by": "top-unit"})
        answered = self.run_command("answer", "--item", "merge today?")
        self.assertEqual(answered.stdout.splitlines(),
                         ["relay: to the showrunner of kid, for kid-unit", "answer: ok — kid: merge today?"],
                         answered.stdout + answered.stderr)
        self.assertEqual(json.loads(self.outstanding().with_name("kid.json").read_text()), [])
        self.assertEqual(self.run_command("answer", "--item", "ship it?").returncode, 2)

    def test_answer_waits_for_the_lists_lock(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry?", "question": "Retry it?",
                                        "asked_by": "alpha-unit"})
        lock = self.outstanding().with_suffix(".lock")
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, sys\n"
            f"handle = open({str(lock)!r}, 'a')\n"
            "fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read(1)\n")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        stdin = cast(io.TextIOWrapper, holder.stdin)
        stdout = cast(io.TextIOWrapper, holder.stdout)

        def cleanup() -> None:
            stdin.close()
            _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)
            stdout.close()

        self.addCleanup(cleanup)
        self.assertEqual(stdout.readline().strip(), "held")
        real_flock = fcntl.flock
        contended: list[Path] = []

        def acquire(handle: io.TextIOWrapper, operation: int) -> None:
            if operation == fcntl.LOCK_EX:
                # Prove the other process owns this exact lock at acquisition.
                # Releasing via stdin is the event that lets answer proceed.
                with self.assertRaises(BlockingIOError):
                    real_flock(handle, operation | fcntl.LOCK_NB)
                contended.append(Path(handle.name))
                _ = stdin.write("x")
                stdin.flush()
            real_flock(handle, operation)

        with mock.patch.object(fcntl, "flock", acquire):
            answered = self.run_command("answer", "--item", "retry?")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertEqual(contended, [lock], "answer must acquire the held list lock before changing it")

    def test_the_reader_refuses_an_asker_without_a_question(self) -> None:
        _ = self.outstanding().write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "x", "asked_by": "alpha-unit"}]))
        with self.assertRaisesRegex(dailies_render.InputError, "question required"):
            _ = dailies_render.read_outstanding(self.outstanding())


if __name__ == "__main__":
    _ = unittest.main()
