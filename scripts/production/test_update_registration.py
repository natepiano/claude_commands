"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import io
import os
import shutil
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import IO, cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.production import rename, session_staff, staff, showrunners
from scripts.production.rename_test_support import RenameDisplays
from scripts.production.cli_fixture import production_paths
from scripts.production.staff_test_support import fixture_member, showrunner_relationship
from scripts.production.update_registration_test_support import RegistrationFixture, review, dailies_render, registration

import dailies_input
from scripts.production import footer_counts
import next_item
import tidy_wrapped
from scripts.hooks import showrunner_footer



class RegistrationTests(RegistrationFixture):
    def test_unreadable_timer_reports_and_continues_prompt_adoption(self) -> None:
        timer = self.state / "notifier" / f"showrunner-{self.owner_id}"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_bytes(b"TARGET=\xff\n")
        prompt = self.state / "showrunner" / self.owner_id / "prompt.txt"
        prompt.parent.mkdir(parents=True)
        _ = prompt.write_text("old prompt\n")
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            lookup = showrunners.timer_scratch(self.owner_id, record=staff.snapshot())
        self.assertIsInstance(lookup, showrunners.TimerConfigurationUnreadable)

        result = self.run_command("log", "adopt the prompt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("timer configuration unreadable", result.stderr)
        self.assertIn("decode", result.stderr)
        self.assertIn(f"rewrote {prompt} from produce.md", result.stdout)
        self.assertIn(str(self.state / "showrunner" / self.owner_id / "session"), prompt.read_text())

    def test_start_and_resume_retarget_only_updates_and_retire_old_session(self) -> None:
        doc_bytes = self.doc.read_bytes()
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok", first.stdout)
        initial_calls = self.notifier_calls()
        self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]), 1)
        updates = next(call for call in initial_calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"])
        self.assertEqual(updates[updates.index("--to") + 1], f"session:{self.fixture_staff_id()}")
        self.assertIn("next_due", first.stdout)
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", instance]]), 1)

        new_env = {**self.env, "CLAUDE_CODE_SESSION_ID": "resume-session-id"}
        resumed = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        repeated = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        # The showrunner's name is written nowhere: no registry, no doc line, no commit.
        self.assertFalse(self.config.exists())
        self.assertEqual(self.doc.read_bytes(), doc_bytes)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        calls = self.notifier_calls()
        self.assertEqual(len([call for call in calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]), 3)
        updates = [call for call in calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]
        self.assertEqual([call[call.index("--to") + 1] for call in updates],
                         [f"session:{self.fixture_staff_id()}"] * 3)
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in calls if call[:2] == ["new", instance]]), 1)

    def test_on_demand_keeps_a_stopped_timer_as_the_showrunners_record(self) -> None:
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes", "- **Updates:** on demand"),
                                encoding="utf-8")
        _ = self.git("commit", "-qam", "dailies on demand")
        before = len(self.notifier_calls())
        resumed = self.run_command("register")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn(f"register: ok — showrunner-{self.fixture_staff_id()} on demand: no scheduled dailies", resumed.stdout)
        calls = self.notifier_calls()[before:]
        self.assertIn(["new", f"showrunner-{self.fixture_staff_id()}"], [call[:2] for call in calls])
        self.assertIn(["stop", f"showrunner-{self.fixture_staff_id()}"], calls)
        self.assertNotIn(["remove", f"showrunner-{self.fixture_staff_id()}"], calls)
        self.assertEqual((self.state / f"instance-showrunner-{self.fixture_staff_id()}").read_text(encoding="utf-8"), "stopped")
        again = self.run_command("register")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        # A re-register retargets the timer and stops it again, so it stays stopped.
        self.assertEqual([call[0] for call in self.notifier_calls()[before + len(calls):]],
                         ["new", "stop", "status"])
        self.assertEqual((self.state / f"instance-showrunner-{self.fixture_staff_id()}").read_text(encoding="utf-8"), "stopped")

    def test_a_new_production_with_no_updates_line_starts_quiet(self) -> None:
        self.without_updates_line()
        first = self.run_command("register")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"register: ok — showrunner-{self.fixture_staff_id()} on demand: no scheduled dailies", first.stdout)
        self.assertEqual((self.state / f"instance-showrunner-{self.fixture_staff_id()}").read_text(encoding="utf-8"), "stopped")
        self.assertNotIn("footer", first.stdout)

    def test_a_line_asking_for_a_schedule_still_schedules(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("every 15 minutes", "every 20 minutes"), encoding="utf-8")
        _ = self.git("commit", "-qam", "every 20 minutes")

        result = self.run_command("register")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = next(call for call in self.notifier_calls() if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"])
        self.assertEqual(updates[updates.index("--every") + 1], "20")
        self.assertNotIn(["stop", f"showrunner-{self.fixture_staff_id()}"], self.notifier_calls())
        self.assertIn("next_due", result.stdout)

    def test_register_requires_the_environment_session_id(self) -> None:
        without_id = {key: value for key, value in self.env.items() if key != "CLAUDE_CODE_SESSION_ID"}
        missing_id = self.run_command("register", "--session", "first-showrunner", env=without_id)
        self.assertEqual(missing_id.returncode, 2)
        self.assertIn("register: failed", missing_id.stdout + missing_id.stderr)
        self.assertEqual(self.notifier_calls(), [])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_refuses_dirty_production_doc_without_side_effects(self) -> None:
        with self.doc.open("a", encoding="utf-8") as output:
            _ = output.write("\nA local note.\n")
        before = self.doc.read_bytes()
        result = self.run_command("register", "--session", "resumed-showrunner")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("register: failed — the production doc has uncommitted edits; commit or discard them first", result.stdout)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notifier_calls(), [])
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_warns_for_a_session_not_named_for_the_production_and_still_registers(self) -> None:
        record = self.state / "sessions" / f"{os.getpid()}.json"
        _ = record.write_text(json.dumps({"pid": os.getpid(), "sessionId": "current-session-id", "name": "other"}),
                              encoding="utf-8")
        result = self.run_command("register")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("register: warn", result.stdout)
        self.assertIn("production example has showrunner other", result.stdout)
        self.assertIn("/showrunner:rename plan example other", result.stdout)
        self.assertIn("register: ok", result.stdout)
        self.assertNotEqual(self.notifier_calls(), [])

    def test_registered_prompt_keeps_status_save_dailies_call_and_render_state(self) -> None:
        result = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompts = list((self.state / "showrunner").rglob("prompt.txt"))
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0].read_text(encoding="utf-8")
        self.assertIn("unit_status.txt", prompt)
        self.assertIn("/showrunner:dailies gantt", prompt)
        state_dir = self.state / "showrunner" / self.fixture_staff_id() / "session"
        self.assertEqual(prompt.count(f"Pass `--render-state {state_dir}/dailies_state.json` to the builder"), 1)
        self.assertIn(f"unit_status.sh {state_dir}/unit_status ", prompt)
        self.assertNotIn("<STATE_DIR>", prompt)
        self.assertEqual(prompt.count("--render-state"), 1)
        self.assertIn("America/Los_Angeles", prompt)
        self.assertIn("--production example", prompt)
        self.assertNotIn("first-showrunner", prompt)


@final
class QuestionIdentityTests(RegistrationFixture):
    asker_id: staff.StaffId = staff.StaffId("")
    issues: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.issues = self.root / "issues"
        self.issues.mkdir(exist_ok=True)
        self.timer("example")
        parent = showrunner_relationship("example")
        self.asker_id = fixture_member("unit", name="alpha", parent=parent.id,
                                       worktree=staff.PresentWorktree("natedev", self.root / "alpha")).id

    def file_question(self, title: str = "Choose the layout", question: str = "Keep the layout?") -> None:
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(question)):
            result = self.run_command("ask", "--text", title, "--question", question,
                                      "--unit", "alpha")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.question_rows()
        self.assertTrue(rows, "The filed question needs a canonical record")
        self.assertEqual(rows[-1]["asked_by"], self.asker_id)

    def question_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for path in sorted(self.issues.glob("*.md")):
            lines = path.read_text(encoding="utf-8").splitlines()
            if "record_type: question" not in lines or "status: open" not in lines:
                continue
            saved = next(line.removeprefix("registration: ") for line in lines
                         if line.startswith("registration: "))
            rows.append(cast(dict[str, object], json.loads(saved)))
        return rows

    @contextmanager
    def forbid_legacy_question_access(self) -> Generator[None]:
        legacy = self.outstanding()
        real_open = Path.open

        def open_file(path: Path, mode: str = "r", buffering: int = -1,
                      encoding: str | None = None, errors: str | None = None,
                      newline: str | None = None) -> IO[str] | IO[bytes]:
            if path == legacy:
                raise AssertionError("A migrated question must use its record")
            return cast(IO[str] | IO[bytes], real_open(path, mode, buffering, encoding, errors, newline))

        with (mock.patch.dict(os.environ, self.env), production_paths(self.env),
              mock.patch.object(Path, "open", open_file)):
            yield

    def migrate(self) -> None:
        migrated = self.run_command("migrate-questions")
        self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
        path = self.outstanding()
        if path.exists():
            self.assertEqual(json.loads(path.read_text()), [])
            path.unlink()

    def test_migrated_question_commands_never_access_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        with self.forbid_legacy_question_access():
            for command in (("open",), ("next",), ("questions-table",),
                            ("mark-important", "--item", "Choose the layout"),
                            ("questions", "--state", str(self.question_state(["Choose the layout"]))),
                            ("answer", "--item", "Choose the layout")):
                with self.subTest(command=command):
                    result = self.run_command(*command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.question_rows(), [])

    def test_next_reader_keeps_the_migrated_question_and_its_queue_key(self) -> None:
        legacy = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep the layout?", "reviewed": "Keep the layout?",
                                        "asked_by": "alpha", "urgency": "user"}
        _ = self.outstanding().write_text(json.dumps([legacy]))
        self.migrate()
        with self.forbid_legacy_question_access():
            owner = registration.QuestionStaff.read().owner(self.doc)
            after = next_item.read_queue("question", self.doc)
        self.assertEqual(after.name, f"question-{owner.id}")
        self.assertEqual(len(after.entries), 1)
        self.assertEqual(after.entries[0].key, f"{owner.id}: Choose the layout")
        self.assertEqual(after.entries[0].item,
                         "example [user], since Tue Oct 06 08:00, asked by alpha: Keep the layout?")

    def test_dailies_reader_preserves_review_timing_and_rank_after_migration(self) -> None:
        legacy = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep the layout?", "review_unavailable": "offline",
                                        "asked_by": str(self.asker_id), "after": "2099-01-02T08:00",
                                        "urgency": "soon", "due": "2099-01-03", "follows": "Earlier choice"}
        _ = self.outstanding().write_text(json.dumps([legacy]))
        self.migrate()
        with self.forbid_legacy_question_access():
            after = dailies_render.read_outstanding(self.outstanding())
        self.assertEqual(len(after), 1)
        item = after[0]
        self.assertIsInstance(item, dailies_render.ReviewUnavailableOutstandingQuestion)
        assert isinstance(item, dailies_render.ReviewUnavailableOutstandingQuestion)
        self.assertEqual((item.text, item.question, item.asked_by),
                         ("Choose the layout", "Keep the layout?", self.asker_id))
        self.assertEqual(item.since, datetime(2026, 10, 6, 8))
        self.assertEqual(item.reason, "offline")
        self.assertEqual(item.timing, dailies_render.DeferredUntil(datetime(2099, 1, 2, 8)))
        self.assertEqual(item.rank.follows, "Earlier choice")

    def test_footer_counts_read_migrated_records_without_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        with self.forbid_legacy_question_access():
            counts = footer_counts.read_counts(self.doc,
                                                questions=len(dailies_render.read_outstanding(self.outstanding())))
            sources = footer_counts.sources(self.doc)
        self.assertEqual(counts.questions, 1)
        self.assertEqual(footer_counts.counts_line(counts), "For you: 1 question")
        for record in self.issues.glob("*.md"):
            self.assertIn(record, sources)

    def test_dailies_counts_read_migrated_records_without_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        with self.forbid_legacy_question_access():
            fields = dailies_input.child_fields("example", datetime.now(ZoneInfo("America/Los_Angeles")),
                                                ZoneInfo("America/Los_Angeles"), expand=False,
                                                record=staff.snapshot())
        self.assertEqual(fields["open"], 1)
        self.assertNotIn("unreadable", fields)

    def test_showrunner_footer_reads_migrated_records_without_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        production = showrunner_footer.Production("example", self.doc, "America/Los_Angeles",
                                                  showrunner_footer.NoDailiesSchedule.PAUSED_OR_UNSCHEDULED,
                                                  showrunners.owner_for("example"))
        with self.forbid_legacy_question_access():
            self.assertEqual(showrunner_footer.open_questions([production]), 1)

    def test_rename_state_keeps_a_migrated_question_and_its_fixed_asker(self) -> None:
        self.file_question()
        self.migrate()
        before = {path: path.read_bytes() for path in self.issues.glob("*.md")}
        row = staff.by_id(self.asker_id)
        assert row is not None
        removed = self.root / "removed-asker-worktree"
        self.assertFalse(removed.exists())
        row = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", removed),
                                 expected=row.worktree)
        displays = RenameDisplays(row, live=False)
        with (self.forbid_legacy_question_access(),
              displays.installed(), redirect_stdout(io.StringIO())):
            _ = rename.apply(str(self.asker_id), "renamed")
            renamed = staff.by_id(self.asker_id)
            assert renamed is not None
            self.assertEqual((renamed.id, renamed.name), (self.asker_id, "renamed"))
            _ = rename.apply(str(self.asker_id), "renamed")
        self.assertEqual({path: path.read_bytes() for path in self.issues.glob("*.md")}, before)
        self.assertEqual(self.question_rows()[0]["asked_by"], self.asker_id)

    def test_tidy_removes_only_the_wrapped_productions_open_questions(self) -> None:
        self.file_question()
        self.migrate()
        proposal = self.issues / "Keep this proposal.md"
        _ = proposal.write_text("---\nstatus: open\ntags: [proposal]\n---\nKeep this proposal.\n")
        before = proposal.read_bytes()
        _ = self.doc.write_text(self.doc.read_text() + "\n> **Status: PRODUCTION — wrapped.** Complete.\n")
        shutil.rmtree(self.state / f"notifier/showrunner-{self.fixture_staff_id()}")
        with (self.forbid_legacy_question_access(),
              mock.patch.object(tidy_wrapped, "STATE_DIR", self.state / "showrunner"),
              mock.patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              redirect_stdout(io.StringIO())):
            result = tidy_wrapped.main([str(self.doc)])
        self.assertEqual(result, 0)
        self.assertEqual(self.question_rows(), [])
        self.assertEqual(proposal.read_bytes(), before)

    def test_migration_rerun_completes_after_the_record_write(self) -> None:
        legacy = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                  "question": "Keep the layout?", "asked_by": "alpha",
                  "review_unavailable": "offline", "after": "2099-01-02T08:00"}
        _ = self.outstanding().write_text(json.dumps([legacy]))
        original = self.outstanding().read_bytes()
        replace = os.replace

        def stop_after_record(source: Path, destination: Path) -> None:
            if Path(destination) == self.outstanding():
                self.assertEqual(len(self.question_rows()), 1)
                raise KeyboardInterrupt("Stop before the legacy list removal")
            replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=stop_after_record), self.assertRaises(KeyboardInterrupt):
            _ = self.run_command("migrate-questions")
        self.assertEqual(self.outstanding().read_bytes(), original)
        self.migrate()
        rows = self.question_rows()
        self.assertEqual(rows, [{**legacy, "asked_by": self.asker_id}])
        remaining: object = cast(object, json.loads(self.outstanding().read_text())) if self.outstanding().exists() else []
        self.assertEqual(remaining, [])
        before = self.store_snapshot()
        self.migrate()
        self.assertEqual(self.store_snapshot(), before)

    def test_interrupted_migration_keeps_unwritten_questions_during_ordinary_writes(self) -> None:
        for action in ("answer", "questions"):
            with self.subTest(action=action):
                for path in self.issues.glob("*.md"):
                    path.unlink()
                legacy = [{"since": "2026-10-06T08:00", "text": title,
                           "question": title + "?", "asked_by": str(self.asker_id)}
                          for title in ("Choose the layout", "Choose the compiler")]
                _ = self.outstanding().write_text(json.dumps(legacy))
                replace = os.replace
                written: list[Path] = []

                def stop_after_first_record(source: Path, destination: Path) -> None:
                    target = Path(destination)
                    if target.suffix == ".md" and target.parent == self.issues:
                        if written:
                            raise KeyboardInterrupt("Stop after the first question record")
                        written.append(target)
                    replace(source, destination)

                with (mock.patch.object(os, "replace", side_effect=stop_after_first_record),
                      self.assertRaises(KeyboardInterrupt)):
                    _ = self.run_command("migrate-questions")
                self.assertEqual(len(written), 1)
                if action == "answer":
                    result = self.run_command("answer", "--item", "Choose the layout")
                    expected = ["Choose the compiler"]
                else:
                    state = self.question_state(["Choose the layout"])
                    result = self.run_command("questions", "--state", str(state))
                    expected = ["Choose the layout", "Choose the compiler"]
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.migrate()
                self.assertCountEqual([row["text"] for row in self.question_rows()], expected)

    def test_migration_keeps_identical_question_text_from_distinct_askers(self) -> None:
        owner = staff.by_id(self.asker_id)
        assert owner is not None
        second = fixture_member("unit", name="beta", parent=owner.parent,
                                worktree=staff.PresentWorktree("natedev", self.root / "beta"))
        legacy = [{"since": "2026-10-06T08:00", "text": "Choose the layout",
                   "question": "Keep the layout?", "asked_by": str(asker)}
                  for asker in (self.asker_id, second.id)]
        _ = self.outstanding().write_text(json.dumps(legacy))
        self.migrate()
        rows = self.question_rows()
        self.assertEqual(len(rows), 2)
        self.assertCountEqual([row["asked_by"] for row in rows], [self.asker_id, second.id])
        before = self.store_snapshot()
        self.migrate()
        self.assertEqual(self.store_snapshot(), before)

    def test_next_keeps_local_items_when_a_remote_child_has_a_local_reminder(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "Choose the layout"})
        parent = staff.by_id(self.asker_id)
        assert parent is not None
        child = fixture_member("showrunner", name="remote-child", parent=parent.parent,
                               doc=staff.LiveDoc("macbook", self.doc.with_name("remote-child-production.md")))
        reminder = self.outstanding().with_name("remote-child.json")
        _ = reminder.write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "Choose the compiler"}]))
        before = reminder.read_bytes()
        errors = io.StringIO()
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            identities = registration.QuestionStaff.read()
            self.assertIn(child.name, identities.names("example", below=True))
            self.assertNotIn(child.name, identities.documents())
            owner = identities.owner(self.doc)
            with redirect_stderr(errors):
                queue = next_item.read_queue("question", self.doc, below=True, record=identities.record)
        self.assertEqual([entry.key for entry in queue.entries], [f"{owner.id}: Choose the layout"])
        self.assertEqual(errors.getvalue().splitlines(), ["questions: remote-child: ownership evidence is unavailable"])
        self.assertEqual(reminder.read_bytes(), before)

    def test_descendant_without_a_timer_remains_in_next_and_answer_search(self) -> None:
        child_doc = self.document_for("child")
        parent = staff.by_id(self.asker_id)
        assert parent is not None
        child = fixture_member("showrunner", name="child", parent=parent.parent,
                               doc=staff.LiveDoc("natedev", child_doc))
        self.write_open_for("child", {"since": "2026-10-06T08:00", "text": "Choose the compiler",
                                      "question": "Use this compiler?", "asked_by": str(child.id)})
        self.assertFalse((self.state / "notifier/showrunner-child").exists())
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            queue = next_item.read_queue("question", self.doc, below=True)
        self.assertEqual([entry.key for entry in queue.entries], [f"{child.id}: Choose the compiler"])
        answer = self.run_command("answer", "--item", "Choose the compiler")
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertIn("child: Choose the compiler", answer.stdout)
        self.assertEqual(self.question_rows(), [])

    def test_questions_table_uses_one_staff_snapshot_without_an_explicit_document(self) -> None:
        self.file_question()
        from scripts.production.cli_fixture import invoke_cli
        owner = staff.by_id(self.fixture_staff_id())
        assert owner is not None
        environment = {**self.env, "CLAUDE_CODE_SESSION_ID": owner.current_session}
        with (mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot,
              mock.patch.object(session_staff, "member_sources",
                                wraps=session_staff.member_sources) as sources):
            result = invoke_cli(registration, registration.main, ["questions-table"],
                                environment, self.checkout, production_state=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Choose the layout", result.stdout)
        snapshot.assert_called_once_with()
        self.assertEqual(sources.call_count, 1)

    def test_footer_ignores_questions_when_the_document_has_no_local_staff_owner(self) -> None:
        self.file_question()
        owner = staff.by_id(self.asker_id)
        assert owner is not None and isinstance(owner.parent, staff.ReportsTo)
        _ = staff.set_doc(owner.parent.id, staff.LiveDoc("macbook", self.doc))
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            production = showrunner_footer.Production("example", self.doc, "America/Los_Angeles",
                            showrunner_footer.NoDailiesSchedule.PAUSED_OR_UNSCHEDULED,
                                                  showrunners.owner_for("example"))
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(showrunner_footer.open_questions([production]), 0)
            self.assertIn("questions: unavailable", errors.getvalue())
            self.assertEqual(dailies_render.read_outstanding(self.outstanding()), [])
            self.assertIn(self.doc, footer_counts.sources(self.doc))
        refused = self.run_command("ask", "--text", "Choose the compiler", "--question", "Use it?")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no showrunner staff row", refused.stdout + refused.stderr)

    def store_snapshot(self) -> dict[str, tuple[bytes, int]]:
        paths = [*self.issues.rglob("*.md"), *self.outstanding().parent.glob("*.json"),
                 *self.outstanding().parent.glob("*.jsonl")]
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def test_answer_resolves_the_fixed_asker_after_a_rename(self) -> None:
        self.file_question()
        _ = staff.rename(self.asker_id, "renamed")
        references: list[str] = []
        resolve = staff.StaffSnapshot.by_member_reference

        def resolve_reference(record: staff.StaffSnapshot, reference: str) -> staff.StaffRow | None:
            references.append(reference)
            return resolve(record, reference)

        with mock.patch.object(staff.StaffSnapshot, "by_member_reference", resolve_reference):
            result = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.asker_id), references)
        self.assertIn("renamed", result.stdout)
        self.assertNotIn("relay: to alpha", result.stdout)

    def test_answer_resolves_the_fixed_asker_after_a_transfer(self) -> None:
        self.file_question()
        destination = fixture_member("showrunner", name="destination")
        _ = staff.move(self.asker_id, staff.ReportsTo(destination.id))
        references: list[str] = []
        resolve = staff.StaffSnapshot.by_member_reference

        def resolve_reference(record: staff.StaffSnapshot, reference: str) -> staff.StaffRow | None:
            references.append(reference)
            return resolve(record, reference)

        with mock.patch.object(staff.StaffSnapshot, "by_member_reference", resolve_reference):
            result = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.asker_id), references)
        self.assertIn("destination", result.stdout)
        self.assertIn("alpha", result.stdout)

    def test_reminder_replacement_and_answer_preserve_the_filed_question(self) -> None:
        self.file_question()
        state = self.question_state(["Review the release notes"])
        with mock.patch.object(review, "review_question", side_effect=AssertionError("A reminder needs no review")):
            result = self.run_command("questions", "--state", str(state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("Review the release notes", listed.stdout)
        self.assertIn("Keep the layout?", listed.stdout)
        self.assertIn("2 due, 0 deferred", listed.stdout)

        answered = self.run_command("answer", "--item", "Review the release notes")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertEqual(answered.stdout.splitlines(),
                         ["relay: none; the item belongs to this production",
                          "answer: ok — example: Review the release notes"])
        remaining = self.run_command("next")
        self.assertIn("Keep the layout?", remaining.stdout)
        self.assertNotIn("Review the release notes", remaining.stdout)

    def test_next_keeps_the_shown_question_until_its_answer(self) -> None:
        self.file_question()
        first = self.run_command("next")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("Keep the layout?", first.stdout)
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Ship the release?")):
            filed = self.run_command("ask", "--text", "Choose the release", "--question", "Ship the release?",
                                     "--unit", "alpha", "--urgency", "user")
        self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        repeated = self.run_command("next")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout.splitlines()[0], first.stdout.splitlines()[0])
        self.assertIn("1 more due", repeated.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        following = self.run_command("next")
        self.assertEqual(following.returncode, 0, following.stdout + following.stderr)
        self.assertIn("Ship the release?", following.stdout)
        self.assertNotIn("Keep the layout?", following.stdout)

    def test_question_table_lists_a_filed_question_beside_an_excluded_proposal(self) -> None:
        proposal = self.issues / "A separate proposal.md"
        _ = proposal.write_text("\n".join([
            "---", "status: open", "tags: [issue, proposal]", "proposal_id: P1",
            "proposed_by: example", "date_created: 2026-11-02", "---", "A separate proposal.", "",
        ]), encoding="utf-8")
        proposal_before = proposal.read_bytes()
        self.file_question()
        records = [path for path in self.issues.glob("*.md") if path != proposal]
        self.assertEqual(len(records), 1)
        stored = records[0].read_text(encoding="utf-8")
        self.assertIn(str(self.asker_id), stored)
        self.assertIn("Keep the layout?", stored)
        listed = self.run_command("questions-table")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("|", listed.stdout)
        self.assertIn("Choose the layout", listed.stdout)
        self.assertIn("alpha", listed.stdout)
        self.assertNotIn("A separate proposal", listed.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        closed = self.run_command("questions-table")
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertNotIn("Choose the layout", closed.stdout)
        self.assertEqual(proposal.read_bytes(), proposal_before)

    def test_question_store_migration_is_idempotent_and_preserves_next(self) -> None:
        _ = self.outstanding().write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep the layout?", "reviewed": "Keep the layout?",
                                        "asked_by": "alpha"}]))
        migrated = self.run_command("migrate-questions")
        self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
        rows = self.question_rows()
        self.assertEqual(rows[0]["asked_by"], self.asker_id)
        remaining: object = cast(object, json.loads(self.outstanding().read_text())) if self.outstanding().exists() else []
        self.assertEqual(remaining, [])
        records = list(self.issues.glob("*.md"))
        self.assertEqual(len(records), 1)
        self.assertIn(str(self.asker_id), records[0].read_text(encoding="utf-8"))
        following = self.run_command("next")
        self.assertEqual(following.returncode, 0, following.stdout + following.stderr)
        self.assertEqual(following.stdout.splitlines(),
                         ["example, since Tue Oct 06 08:00, asked by alpha: Keep the layout?",
                          "next: ok — 0 more due"])
        before = self.store_snapshot()
        repeated = self.run_command("migrate-questions")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.store_snapshot(), before)

    def test_question_review_and_defer_keep_the_fixed_asker(self) -> None:
        held = review.QuestionAwaitingFacts("Keep the layout?", ("Which layout?",))
        with mock.patch.object(review, "review_question", return_value=held):
            result = self.run_command("ask", "--text", "Choose the layout", "--question", "Keep the layout?",
                                      "--unit", "alpha")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Which layout?", result.stdout + result.stderr)
        self.assertEqual(list(self.issues.glob("*.md")), [])
        with mock.patch.object(review, "review_question",
                               return_value=review.QuestionReviewUnavailable("Keep the layout?", "fixture timeout")):
            filed = self.run_command("ask", "--text", "Choose the layout", "--question", "Keep the layout?",
                                     "--unit", "alpha")
        self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        state = self.question_state([{"text": "Choose the layout", "after": "2099-01-02T08:00"}])
        deferred = self.run_command("questions", "--state", str(state))
        self.assertEqual(deferred.returncode, 0, deferred.stdout + deferred.stderr)
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("deferred until", listed.stdout)
        self.assertIn("Keep the layout?", listed.stdout)
        self.assertIn("0 due, 1 deferred", listed.stdout)
        rows = self.question_rows()
        self.assertEqual(rows[0]["asked_by"], self.asker_id)
        self.assertEqual(rows[0]["review_unavailable"], "fixture timeout")

    def test_open_and_answer_use_the_document_owner_without_a_timer_after_rename(self) -> None:
        self.file_question()
        owner = staff.snapshot().by_doc("natedev", self.doc)
        assert owner is not None
        timer = self.state / f"notifier/showrunner-{self.fixture_staff_id()}"
        shutil.rmtree(timer)
        _ = staff.rename(owner.id, "different-display-name")
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            identities = registration.QuestionStaff.read()
            self.assertEqual(identities.owner(self.doc).name, "different-display-name")
            self.assertEqual(identities.documents()["different-display-name"].doc, owner.doc)
            self.assertEqual(registration.outstanding_file("different-display-name", identities=identities),
                             self.state / "showrunner/outstanding" / f"{owner.id}.json")
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("Keep the layout?", listed.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertEqual(self.question_rows(), [])
        self.assertFalse(timer.exists())
        self.assertFalse((self.state / "showrunner/outstanding/example.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
