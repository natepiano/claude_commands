#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for tidy_wrapped.py: only a wrapped production with no update timer loses its state files."""
from __future__ import annotations

from scripts.shutdown import remote

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from typing import cast, override
from unittest import mock

from scripts.lib import copy_record
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import (
    fixture_member, fixture_runtime_id, isolated_source_readers, isolated_staff_file, source_environment,
)
from scripts.production import staff
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment, fake_remote_removal, publish_mirror
from scripts.copies.remote_test_support import local_remote_transport

SCRIPT = Path(__file__).with_name("tidy_wrapped.py")


def production(status: str) -> str:
    return f"# Example\n\n> **Status: PRODUCTION — {status}.** One line.\n"


class TidyWrappedTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    notifier: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "showrunner"
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {
            **copies_environment(self.root), "HERDR_BIN": "/nonexistent/herdr",
        }))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))

    def write(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")
        return path

    def doc(self, production_name: str, status: str) -> Path:
        document = self.write(self.root / "docs" / f"{production_name}-production.md", production(status))
        _ = self.owner(production_name)
        return document

    def owner(self, production_name: str) -> staff.StaffRow:
        document = self.root / "docs" / f"{production_name}-production.md"
        rows = [row for row in staff.initial_snapshot().rows
                if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.path == document]
        if rows:
            return rows[0]
        return fixture_member("showrunner", name=production_name, runtime_id=fixture_runtime_id("tidy:" + production_name),
                              doc=staff.WrappedDoc(remote.local_machine(), document))

    def state_files(self, production_name: str) -> tuple[Path, ...]:
        owner = self.owner(production_name)
        return tuple(self.write(path, "[]\n") for path in (
            self.state / "footers-off" / owner.id, self.state / "review-paused" / f"{owner.id}.json",
            self.state / "outstanding" / f"{owner.id}.json",
            self.state / owner.id / "log-backups" / "log-1.md"))

    def run_tidy(self, *docs: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SHOWRUNNER_STATE_DIR": str(self.state), "NOTIFIER_STATE_DIR": str(self.notifier)}
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, docs)], env=env,
                              capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)







    def test_wrap_removes_production_and_unit_checkouts_before_state(self) -> None:
        source = ImmutableGitSeed.create(self.root, "main", "Cleanup Test",
                                          "cleanup@example.invalid", {"file.txt": "content"}).checkout
        doc = self.doc("done", "wrapped")
        owner = self.owner("done")
        _ = fixture_member("unit", name="alpha", parent=owner.id)
        _ = fixture_member("unit", name="beta", parent=owner.id,
                           worktree=staff.RemovedWorktree(remote.local_machine(), self.root / "beta"))
        state = self.state_files("done")
        with copies.standing("alpha", source, copies.CommittedSource("HEAD")) as unit_tree:
            with copies.standing("done", source, copies.CommittedSource("HEAD")) as production_tree:
                pass
            with copies.standing("beta", source, copies.CommittedSource("HEAD")) as retired_tree:
                pass
            with copies.standing("live", source, copies.CommittedSource("HEAD")) as unrelated:
                pass
            refused = self.run_tidy(doc)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertTrue(all(path.exists() for path in state))
        result = self.run_tidy(doc)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for tree in (unit_tree, production_tree, retired_tree):
            self.assertFalse(tree.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(any(path.exists() for path in state))
        self.assertEqual(self.run_tidy(doc).returncode, 0)

    def test_wrap_removes_unit_suffix_copies_and_keeps_another_units_copy(self) -> None:
        source = ImmutableGitSeed.create(self.root, "main", "Cleanup Test",
                                         "cleanup@example.invalid", {"file.txt": "content"}).checkout
        doc = self.doc("done", "wrapped")
        owner = self.owner("done")
        active = fixture_member("unit", name="alpha", parent=owner.id)
        retired = fixture_member("unit", name="beta", parent=owner.id,
                                 worktree=staff.RemovedWorktree("natedev", self.root / "beta"))
        with copies.standing(copies.unit_owner(active.name), source, copies.CommittedSource("HEAD")) as unit_tree:
            pass
        with copies.standing(copies.unit_owner(retired.name), source, copies.CommittedSource("HEAD")) as retired_tree:
            pass
        with copies.standing("live-unit", source, copies.CommittedSource("HEAD")) as unrelated:
            pass
        result = self.run_tidy(doc)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(unit_tree.exists())
        self.assertFalse(retired_tree.exists())
        self.assertTrue(unrelated.exists())

    def test_wrap_keeps_state_until_a_damaged_copy_record_is_repaired(self) -> None:
        source = ImmutableGitSeed.create(self.root, "main", "Cleanup Test",
                                          "cleanup@example.invalid", {"file.txt": "content"}).checkout
        doc = self.doc("done", "wrapped")
        state = self.state_files("done")
        with copies.workspace("done", source, "HEAD", name="damaged", branch="worker") as tree:
            pass
        record = tree / copy_record.RECORD
        original = record.read_text()
        _ = record.write_text("{invalid")
        refused = self.run_tidy(doc)
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn(str(record), refused.stderr)
        self.assertTrue(all(path.exists() for path in state))
        _ = record.write_text(original)
        self.assertEqual(self.run_tidy(doc).returncode, 0)
        self.assertFalse(tree.exists())
        self.assertFalse(any(path.exists() for path in state))

    def test_wrap_accepts_production_and_unit_names_that_cannot_own_copies(self) -> None:
        doc = self.doc("Done_Name", "wrapped")
        owner = fixture_member("showrunner", name="Done_Name", doc=staff.WrappedDoc(remote.local_machine(), doc))
        _ = fixture_member("unit", name="Alpha_unit", parent=owner.id)
        state = self.state_files("Done_Name")
        result = self.run_tidy(doc)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(path.exists() for path in state))

    def test_a_wrapped_production_loses_its_own_files_and_keeps_the_others(self) -> None:
        own = self.state_files("done")
        other = self.state_files("live")
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "done: removed 4 state files\n")
        self.assertFalse(any(path.exists() for path in own))
        self.assertFalse((self.state / self.owner("done").id).exists())
        self.assertTrue(all(path.exists() for path in other))

    def test_multiple_documents_share_one_question_staff_context(self) -> None:
        from scripts.production import tidy_wrapped
        from scripts.production.update_registration import QuestionStaff

        documents = [self.doc(production_name, "wrapped") for production_name in ("first", "second")]
        paths = [path for production_name in ("first", "second") for path in self.state_files(production_name)]
        environment = {**source_environment(self.root / "sources"),
                       "SHOWRUNNER_STATE_DIR": str(self.state), "NOTIFIER_STATE_DIR": str(self.notifier)}
        with (mock.patch.dict(os.environ, environment), isolated_source_readers(),
              mock.patch.object(tidy_wrapped, "STATE_DIR", self.state),
              mock.patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.notifier)):
            identities = QuestionStaff.read()
            with (mock.patch.object(QuestionStaff, "read", return_value=identities) as read_context,
                  mock.patch.object(tidy_wrapped, "owned_paths", wraps=tidy_wrapped.owned_paths) as owned,
                  mock.patch.object(copies, "remove_trees"), redirect_stdout(io.StringIO())):
                self.assertEqual(tidy_wrapped.main([str(document) for document in documents]), 0)
            read_context.assert_called_once_with()
            self.assertEqual(owned.call_count, 2)
            for call in owned.call_args_list:
                owner = cast(staff.StaffRow, call.args[0])
                self.assertIs(owner, identities.record.by_id(owner.id))
        self.assertFalse(any(path.exists() for path in paths))

    def test_wrap_closes_and_retains_questions_without_a_proposals_line(self) -> None:
        from scripts.production import tidy_wrapped

        doc = self.doc("done", "wrapped")
        owner = staff.rename(self.owner("done").id, "renamed-owner")
        _ = self.doc("live", "running")
        other = staff.rename(self.owner("live").id, "other")
        records: list[Path] = []
        for member, title in ((owner, "Choose the layout"), (other, "Choose the release")):
            fields = {"since": "2000-01-01T08:00", "text": title,
                      "question": title + "?", "asked_by": str(member.id)}
            records.append(self.write(self.state / member.id / "questions/question-layout.md",
                "---\nrecord_type: question\nstatus: open\n"
                + f"production_id: {member.id}\nasked_by: {member.id}\n"
                + f"registration: {json.dumps(fields)}\n---\n# {title}\n"))
        own, unrelated = records
        unrelated_before = unrelated.read_bytes(), unrelated.stat().st_mtime_ns
        reminders = self.write(self.state / "outstanding" / f"{owner.id}.json", "[]\n")
        environment = {**os.environ, **source_environment(self.root / "sources"),
                       "HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr", "SHOWRUNNER_STATE_DIR": str(self.state),
                       "NOTIFIER_STATE_DIR": str(self.notifier)}
        with (mock.patch.dict(os.environ, environment), isolated_source_readers(),
              mock.patch.object(tidy_wrapped, "STATE_DIR", self.state),
              mock.patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.notifier)):
            result = invoke_cli(tidy_wrapped, lambda: tidy_wrapped.main([str(doc)]),
                                [str(doc)], environment, self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status: closed", own.read_text().splitlines())
        self.assertIn(f"asked_by: {owner.id}", own.read_text().splitlines())
        self.assertEqual((unrelated.read_bytes(), unrelated.stat().st_mtime_ns), unrelated_before)
        self.assertFalse(reminders.exists())

    def test_wrap_refuses_ambiguous_document_ownership_without_changing_any_file(self) -> None:
        from scripts.production import tidy_wrapped

        checkout = self.root / "main"
        checkout.mkdir()

        def git(*arguments: str) -> None:
            result = subprocess.run(["git", *arguments], cwd=checkout, capture_output=True,
                                    text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)

        git("init", "-b", "main")
        git("config", "user.name", "Cleanup Test")
        git("config", "user.email", "cleanup@example.invalid")
        folder = self.root / "questions"
        relative = Path("docs/plans/done-production.md")
        document = self.write(checkout / relative, production("wrapped") + f"\n- **Proposals:** `{folder}`\n")
        git("add", ".")
        git("commit", "-m", "Add a wrapped production")
        other = self.root / "other-owner"
        caller = self.root / "caller"
        git("worktree", "add", "-b", "other-owner", str(other))
        git("worktree", "add", "-b", "caller", str(caller))
        first = fixture_member("showrunner", name="first-owner", doc=staff.WrappedDoc(remote.local_machine(), document))
        second = fixture_member("showrunner", name="second-owner", doc=staff.WrappedDoc(remote.local_machine(), other / relative))
        fields = {"since": "2000-01-01T08:00", "text": "Choose the layout",
                  "question": "Keep this layout?", "asked_by": str(first.id)}
        question = self.write(folder / "question-layout.md",
                              "---\nrecord_type: question\nstatus: open\n"
                              + f"production_id: {first.id}\nasked_by: {first.id}\n"
                              + f"registration: {json.dumps(fields)}\n---\n# Choose the layout\n")
        state = self.state_files("done")
        paths = [*state, question, document, other / relative, caller / relative, self.root / "staff.json"]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        environment = {**os.environ, **source_environment(self.root / "sources"),
                       "HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr", "SHOWRUNNER_STATE_DIR": str(self.state),
                       "NOTIFIER_STATE_DIR": str(self.notifier)}
        with (mock.patch.dict(os.environ, environment), isolated_source_readers(),
              mock.patch.object(tidy_wrapped, "STATE_DIR", self.state),
              mock.patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.notifier)):
            result = invoke_cli(tidy_wrapped, lambda: tidy_wrapped.main([str(caller / relative)]),
                                [str(caller / relative)], environment, self.root)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(first.id), result.stderr)
        self.assertIn(str(second.id), result.stderr)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)

    def test_a_running_production_keeps_every_file(self) -> None:
        own = self.state_files("live")
        doc = self.doc("live", "running")
        result = self.run_tidy(doc)
        self.assertEqual(result.returncode, 1)
        self.assertIn("kept: ", result.stderr)
        self.assertIn("live-production.md: not wrapped", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

    def test_wrap_removes_its_answered_history_and_keeps_another_productions(self) -> None:
        answered = self.write(self.state / "outstanding" / f"{self.owner('done').id}.answered.jsonl", '{"text":"answered question"}\n')
        other = self.write(self.state / "outstanding" / f"{self.owner('live').id}.answered.jsonl", '{"text":"keep this answer"}\n')
        before = other.read_bytes()
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done: removed 1 state file\n", ""))
        self.assertFalse(answered.exists())
        self.assertEqual(other.read_bytes(), before)

    def test_a_wrapped_doc_whose_timer_remains_keeps_every_file(self) -> None:
        own = self.state_files("done")
        doc = self.doc("done", "wrapped")
        owner = staff.rename(self.owner("done").id, "renamed-owner")
        for name in (f"showrunner-{owner.id}",):
            with self.subTest(timer=name):
                timer = self.notifier / name
                timer.mkdir()
                result = self.run_tidy(doc)
                self.assertEqual(result.returncode, 1)
                self.assertIn("renamed-owner still has an update timer", result.stderr)
                self.assertTrue(all(path.exists() for path in own))
                timer.rmdir()

    def test_missing_or_unreadable_staff_keeps_every_file(self) -> None:
        own = self.state_files("done")
        doc = self.doc("done", "wrapped")
        record = self.root / "staff.json"
        record.unlink()
        for state, reason in (("missing", "No such file"), ("unreadable", "invalid staff JSON:")):
            with self.subTest(staff=state):
                if state == "unreadable":
                    _ = self.write(record, "{invalid")
                result = self.run_tidy(doc)
                self.assertEqual(result.returncode, 1)
                self.assertIn("kept: cannot read staff record", result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertTrue(all(path.exists() for path in own))

    def test_a_staff_name_matching_a_shared_folder_removes_only_its_owned_state(self) -> None:
        shared = [self.write(self.state / name / "keep", "shared state\n")
                  for name in ("footers-off", "review-paused", "outstanding", "counts", "reports")]
        other = self.state_files("live")
        for path in shared:
            with self.subTest(name=path.parent.name):
                name = path.parent.name
                owner = self.owner(name)
                own = (*self.state_files(name), *(self.write(path, "state\n") for path in (
                    self.state / "outstanding" / f"{owner.id}.answered.jsonl",
                    self.state / "outstanding" / f"{owner.id}.history.jsonl",
                    self.state / "outstanding" / f"{owner.id}.lock",
                    self.state / "counts" / owner.id,
                    self.state / "reports" / f"{owner.id}.json")))
                result = self.run_tidy(self.doc(name, "wrapped"))
                self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout + result.stderr)
                self.assertFalse(any(path.exists() for path in own))
                self.assertTrue(all(path.exists() for path in other))
                self.assertTrue(all(path.parent.is_dir() for path in shared))
                self.assertEqual([path.read_text() for path in shared], ["shared state\n"] * len(shared))

    def test_a_refused_doc_does_not_stop_the_next(self) -> None:
        kept = self.state_files("live")
        removed = self.state_files("done")
        result = self.run_tidy(self.doc("live", "running"), self.doc("done", "wrapped"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "done: removed 4 state files\n")
        self.assertTrue(all(path.exists() for path in kept))
        self.assertFalse(any(path.exists() for path in removed))

    def test_a_wrapped_production_with_nothing_left_says_so(self) -> None:
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done: no state files\n", ""))


    def test_wrap_removes_only_the_owners_remote_mirrors(self) -> None:
        from scripts.production import tidy_wrapped
        doc = self.doc("done", "wrapped")
        mine = copies.RemoteMirror("done", copies.SelectedRepository("hana"), "mac", "mirror/done", "done-session")
        other = copies.RemoteMirror("live", copies.SelectedRepository("hana"), "mac", "mirror/live", "live-session")
        with fake_remote_removal((mine, other)) as remove:
            _ = tidy_wrapped.tidy("done", doc)
        remove.assert_called_once_with(mine)
        self.assertEqual(copies.owned_remote_mirrors("live"), [other])

    def test_wrap_refuses_a_busy_remote_mirror_with_holder_and_path(self) -> None:
        from scripts.production import tidy_wrapped
        doc = self.doc("done", "wrapped")
        state = self.state_files("done")
        mirror = copies.RemoteMirror("done", copies.SelectedRepository("hana"), "mac", "mirror/done", "done-session")
        copies.register_remote(mirror)
        with copies.remote_lease(mirror):
            with self.assertRaisesRegex(tidy_wrapped.TidyRefused, "mirror/done.*done-session"):
                _ = tidy_wrapped.tidy("done", doc)
        self.assertTrue(all(path.exists() for path in state))
        self.assertTrue(copies.remote_record_path(mirror).exists())

    def test_wrap_removes_an_idle_remote_mirror_through_the_remote_command(self) -> None:
        from scripts.production import tidy_wrapped
        doc = self.doc("done", "wrapped")
        mirror = copies.RemoteMirror("done", copies.SelectedRepository("hana"), "mac",
                                     str(self.root / "mirrors/done"), "done-session")
        with local_remote_transport(self.root):
            self.assertEqual(publish_mirror(mirror), 0)
            _ = tidy_wrapped.tidy("done", doc)
        self.assertFalse(Path(mirror.path).exists())
        self.assertFalse(copies.remote_record_path(mirror).exists())


if __name__ == "__main__":
    _ = unittest.main()
