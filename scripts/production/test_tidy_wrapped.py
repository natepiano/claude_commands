#!/usr/bin/env python3
"""Tests for tidy_wrapped.py: only a wrapped production with no update timer loses its state files."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

from scripts.lib import copy_record
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import (
    fixture_member, isolated_source_readers, isolated_staff_file, source_environment,
)
from scripts.production import staff
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli
from scripts.copies import copies

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
            "HOME": str(self.root),
            "COPIES_CONFIG": str(Path(copies.__file__).resolve().parents[2] / "config/copies.conf"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "SCRATCH_HOLDS": str(self.root / "copy-holds.json"),
        }))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))

    def write(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")
        return path

    def doc(self, slug: str, status: str) -> Path:
        return self.write(self.root / "docs" / f"{slug}-production.md", production(status))

    def state_files(self, slug: str) -> tuple[Path, ...]:
        return tuple(self.write(path, "[]\n") for path in (
            self.state / "footers-off" / slug, self.state / "review-paused" / f"{slug}.json",
            self.state / "outstanding" / f"{slug}.json", self.state / slug / "log-backups" / "log-1.md"))

    def run_tidy(self, *docs: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SHOWRUNNER_STATE_DIR": str(self.state), "NOTIFIER_STATE_DIR": str(self.notifier)}
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, docs)], env=env,
                              capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)

    def test_wrap_removes_production_and_unit_checkouts_before_state(self) -> None:
        source = ImmutableGitSeed.create(self.root, "main", "Cleanup Test",
                                          "cleanup@example.invalid", {"file.txt": "content"}).checkout
        doc = self.doc("done", "wrapped")
        owner = fixture_member("showrunner", name="done", doc=staff.WrappedDoc("natedev", doc.resolve()))
        _ = fixture_member("unit", name="alpha-unit", parent=owner.id)
        _ = fixture_member("unit", name="beta-unit", parent=owner.id,
                           worktree=staff.RemovedWorktree("natedev", self.root / "beta"))
        state = self.state_files("done")
        with copies.standing("alpha-unit", source, "HEAD") as unit_tree:
            with copies.standing("done", source, "HEAD") as production_tree:
                pass
            with copies.standing("beta-unit", source, "HEAD") as retired_tree:
                pass
            with copies.standing("live", source, "HEAD") as unrelated:
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
        owner = fixture_member("showrunner", name="Done_Name", doc=staff.WrappedDoc("natedev", doc))
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
        self.assertFalse((self.state / "done").exists())
        self.assertTrue(all(path.exists() for path in other))

    def test_wrap_closes_and_retains_questions_without_a_proposals_line(self) -> None:
        import tidy_wrapped

        doc = self.doc("done", "wrapped")
        owner = fixture_member("showrunner", name="renamed-owner", doc=staff.WrappedDoc("natedev", doc))
        other_doc = self.doc("live", "running")
        other = fixture_member("showrunner", name="other", doc=staff.LiveDoc("natedev", other_doc))
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
        reminders = self.write(self.state / "outstanding/done.json", "[]\n")
        environment = {**os.environ, **source_environment(self.root / "sources"),
                       "HOME": str(self.root), "SHOWRUNNER_STATE_DIR": str(self.state),
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
        import tidy_wrapped

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
        first = fixture_member("showrunner", name="first-owner", doc=staff.WrappedDoc("natedev", document))
        second = fixture_member("showrunner", name="second-owner", doc=staff.WrappedDoc("natedev", other / relative))
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
                       "HOME": str(self.root), "SHOWRUNNER_STATE_DIR": str(self.state),
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
        result = self.run_tidy(self.doc("live", "running"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("kept: ", result.stderr)
        self.assertIn("live-production.md: not wrapped", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

    def test_wrap_removes_its_answered_history_and_keeps_another_productions(self) -> None:
        answered = self.write(self.state / "outstanding/done.answered.jsonl", '{"text":"answered question"}\n')
        other = self.write(self.state / "outstanding/live.answered.jsonl", '{"text":"keep this answer"}\n')
        before = other.read_bytes()
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done: removed 1 state file\n", ""))
        self.assertFalse(answered.exists())
        self.assertEqual(other.read_bytes(), before)

    def test_a_wrapped_doc_whose_timer_remains_keeps_every_file(self) -> None:
        own = self.state_files("done")
        (self.notifier / "showrunner-done").mkdir()
        result = self.run_tidy(self.doc("done", "wrapped"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("done still has an update timer", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

    def test_a_slug_naming_a_shared_folder_deletes_nothing(self) -> None:
        own = self.state_files("live")
        result = self.run_tidy(self.doc("outstanding", "wrapped"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("'outstanding' is not a production slug", result.stderr)
        self.assertTrue(all(path.exists() for path in own))

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


if __name__ == "__main__":
    _ = unittest.main()
