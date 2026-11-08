"""Rename the showrunner row and repair its live names."""

from __future__ import annotations

from scripts.production.cli_fixture import process_boundary

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast, final
from unittest import mock

from scripts.production import rename_test_support as support
from scripts.production import staff, unit_move
from scripts.message import sessions
from scripts.herdr import herdr
from scripts.production.rename_test_support import FAIL_GIT_MOVE_INDEX, NEW, OLD

@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_same_name_keeps_a_pane_with_another_native_session(self) -> None:
        row = self.production_row()
        displays = support.RenameDisplays(row)
        other = herdr.NativeAgentProcess(herdr.EnvironmentThread(fixture_runtime_id("another-live-session")), "probe-generation", {})
        with (displays.installed(), mock.patch.object(herdr, "native_process", return_value=other)):
            result = self.run_command("apply", row.name, row.name)
        self.assert_success(result)
        self.assertEqual(displays.commands, [])
        self.assertEqual((displays.pane, displays.tab, displays.workspace), ("previous",) * 3)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("anomaly:", result.stderr)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_reports_an_unavailable_display_and_succeeds(self) -> None:
        row = self.production_row()
        displays = support.RenameDisplays(row)
        with (displays.installed(), mock.patch.object(herdr.Herdr, "panes",
                side_effect=herdr.HerdrFailure("display unavailable"))):
            result = self.run_command("apply", row.name, row.name)
        self.assert_success(result)
        self.assertEqual(result.stderr.strip(), "anomaly: set_displays: display unavailable")
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_displays_without_an_update_timer(self) -> None:
        row = self.production_row()
        (self.notifier / f"showrunner-{row.name}" / "conf").unlink()
        displays = support.RenameDisplays(row)
        with displays.installed():
            result = self.run_command("apply", row.name, row.name)
        self.assert_success(result)
        self.assertEqual((displays.pane, displays.tab, displays.workspace), (row.name,) * 3)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_the_current_session_and_keeps_an_old_session(self) -> None:
        row = self.production_row()
        current = staff.native_id(fixture_runtime_id("showrunner-session-after-clear"))
        row = staff.update_current_session(row.current_session, current)
        record = sessions.SessionRecord(sessionId=current, name="previous", pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        path = self.root / "state/sessions/998041.json"
        old_path = self.root / "state/sessions/998042.json"
        self.write(path, json.dumps(record))
        self.write(old_path, json.dumps({**record, "sessionId": row.id, "pid": 998042}))
        before = old_path.read_bytes(), old_path.stat().st_mtime_ns
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = row.name
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True)):
            self.assert_success(self.run_command("apply", row.name, row.name))
            self.assertEqual(json.loads(path.read_text())["name"], row.name)
            settled = path.read_bytes(), path.stat().st_mtime_ns
            rerun = self.run_command("apply", row.name, row.name)
        self.assert_success(rerun)
        self.assertEqual(rerun.stdout.strip(), "unchanged")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), settled)
        self.assertEqual((old_path.read_bytes(), old_path.stat().st_mtime_ns), before)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_each_display_and_keeps_the_current_row(self) -> None:
        row = self.production_row()
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("showrunner-after-clear")))
        for field in ("pane", "tab", "workspace"):
            with self.subTest(field=field):
                displays = support.RenameDisplays(row)
                displays.pane = displays.tab = displays.workspace = row.name
                setattr(displays, field, "previous")
                before = self.snapshot()
                with displays.installed():
                    result = self.run_command("apply", row.name, row.name)
                    self.assert_success(result)
                    self.assertNotIn("unchanged", result.stdout)
                    self.assertEqual((displays.pane, displays.tab, displays.workspace), (row.name,) * 3)
                    self.assertEqual(staff.by_id(row.id), row)
                    self.assertEqual(self.snapshot(), before)
                    commands = list(displays.commands)
                    rerun = self.run_command("apply", row.name, row.name)
                self.assert_success(rerun)
                self.assertEqual(rerun.stdout.strip(), "unchanged")
                self.assertEqual(displays.commands, commands)
                self.assertEqual(self.snapshot(), before)

    def test_document_rename_updates_a_wrapped_location_without_reactivating_its_row(self) -> None:
        row = self.production_row()
        _ = staff.set_doc(row.id, staff.WrappedDoc("natedev", self.doc))
        self.assert_success(self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual((retained.id, retained.name, retained.parent, retained.doc),
                         (row.id, NEW, row.parent, staff.WrappedDoc("natedev", self.new_doc)))
        before = self.snapshot()
        self.assert_success(self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.snapshot(), before)

    def test_retry_after_doc_move_updates_the_live_doc_on_the_same_staff_id(self) -> None:
        row = self.production_row()
        _ = staff.set_doc(row.id, staff.LiveDoc("natedev", self.doc))
        with mock.patch.object(staff, "set_doc", side_effect=OSError("stopped after doc move")):
            interrupted = self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertIn("anomaly: document_move: stopped after doc move", interrupted.stderr)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.is_file())
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual(retained.doc, staff.LiveDoc("natedev", self.doc))
        self.assert_success(self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
        before = self.snapshot()
        self.assert_success(self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.snapshot(), before)

    def production_row(self) -> staff.StaffRow:
        return next(row for row in staff.find(kind="showrunner") if row.name == OLD)

    def test_bare_showrunner_name_renames_the_showrunner_and_keeps_its_id(self) -> None:
        row = self.production_row()
        row = staff.rename(row.id, "tools")
        self.assert_success(self.run_command("apply", "tools", NEW))
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW))
        self.assertTrue(self.doc.is_file())
        self.assertFalse(self.new_doc.exists())

    def test_taken_staff_name_refuses_before_sources_change(self) -> None:
        _ = fixture_member("showrunner", name=NEW)
        before = self.snapshot()
        failed = self.run_rename("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertIn("StaffRefused", failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_staff_member_refuses_before_sources_change(self) -> None:
        row = self.production_row()
        self.forget_rows(row.id, *(unit.id for unit in staff.snapshot().find(kind="unit", parent=row.id)))
        before = self.snapshot()
        failed = self.run_rename("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_document_move_reruns_after_git_move_loses_its_index_update(self) -> None:
        row = self.production_row()
        failed = self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.exists())
        self.assert_success(self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        production_docs = [path for path in self.git("ls-files").splitlines()
                           if path.endswith("-production.md")]
        self.assertEqual(production_docs, ["docs/plans/new-production.md"])
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))

    def test_document_move_recovery_keeps_partial_staging(self) -> None:
        source = self.doc.relative_to(self.checkout).as_posix()
        destination = self.new_doc.relative_to(self.checkout).as_posix()
        staged = self.doc.read_bytes() + b"\nThe staged document edit.\n"
        working = staged + b"\nThe unstaged document edit.\n"
        _ = self.doc.write_bytes(staged)
        _ = self.git("add", "--", source)
        staged_blob = self.git("rev-parse", f":{source}")
        _ = self.doc.write_bytes(working)

        failed = self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assertEqual(self.git("rev-parse", f":{source}"), staged_blob)

        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.git("rev-parse", f":{destination}"), staged_blob)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assertEqual(self.git("ls-files", "--", source), "")

    def test_document_move_reruns_after_the_index_source_is_removed(self) -> None:
        row = self.production_row()
        failed = self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        git = unit_move.git

        def interrupted(checkout: Path, *arguments: str) -> str:
            result = git(checkout, *arguments)
            if arguments[0] == "rm":
                raise OSError("stopped after index removal")
            return result

        with mock.patch.object(unit_move, "git", side_effect=interrupted):
            interrupted_move = self.run_command("apply", OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertIn("stopped after index removal", interrupted_move.stderr)
        self.assertEqual(self.git("ls-files", "--", "docs/plans/old-production.md", "docs/plans/new-production.md"),
                         "docs/plans/new-production.md")
        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.git("ls-files", "--", "docs/plans/old-production.md", "docs/plans/new-production.md"),
                         "docs/plans/new-production.md")
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
        before = self.file_versions()
        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.file_versions(), before)

    def test_document_move_keeps_the_blob_indexed_before_source_removal(self) -> None:
        source = self.doc.relative_to(self.checkout).as_posix()
        destination = self.new_doc.relative_to(self.checkout).as_posix()
        staged = self.doc.read_bytes() + b"\nThe staged document edit.\n"
        working = staged + b"\nThe unstaged document edit.\n"
        _ = self.doc.write_bytes(staged)
        self.doc.chmod(0o755)
        _ = self.git("add", "--", source)
        source_entry = self.git("ls-files", "-s", "--", source).split()[:3]
        _ = self.doc.write_bytes(working)
        failed = self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        git = unit_move.git

        def interrupted(checkout: Path, *arguments: str) -> str:
            if arguments[0] == "rm":
                raise OSError("stopped before source index removal")
            return git(checkout, *arguments)

        with mock.patch.object(unit_move, "git", side_effect=interrupted):
            interrupted_move = self.run_command("apply", OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertIn("stopped before source index removal", interrupted_move.stderr)
        self.assertEqual(self.git("ls-files", "-s", "--", destination).split()[:3], source_entry)
        self.assertEqual(self.git("ls-files", "-s", "--", source).split()[:3], source_entry)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.git("ls-files", "-s", "--", destination).split()[:3], source_entry)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assertEqual(self.git("ls-files", "--", source), "")

    def test_document_move_refuses_an_incomplete_staged_blob(self) -> None:
        row = self.production_row()
        failed = self.run_command("apply", OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        journal = self.state / row.id / "rename.json"
        fields = cast(dict[str, object], json.loads(journal.read_text()))
        for blob in ({}, {"mode": 100644, "object_id": "blob"},
                     {"mode": "100644", "object_id": 7},
                     {"mode": "", "object_id": ""}, None):
            with self.subTest(blob=blob):
                fields["staged_blob"] = blob
                self.write(journal, json.dumps(fields))
                before = self.file_versions()
                refused = self.run_command("apply", OLD, NEW,
                    "--to-doc", str(self.new_doc), "--to-branch", NEW)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn("the document move journal has an incomplete staged blob", refused.stderr)
                self.assertEqual(self.file_versions(), before)

    def test_invalid_optional_move_name_leaves_no_journal_and_allows_a_corrected_request(self) -> None:
        row = self.production_row()
        before = self.file_versions()
        failed = self.run_command("apply", OLD, "../escape",
                                  "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertFalse((self.state / row.id / "rename.json").exists())
        self.assertEqual(self.file_versions(), before)
        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertTrue(self.new_doc.exists())

    def test_document_edits_after_journaling_are_kept_and_the_move_finishes(self) -> None:
        row = self.production_row()
        with mock.patch.object(staff, "rename", side_effect=OSError("stopped after journal")):
            interrupted = self.run_command("apply", OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue((self.state / row.id / "rename.json").exists())
        content = self.doc.read_bytes() + b"\nA new document edit.\n"
        _ = self.doc.write_bytes(content)
        result = self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assert_success(result)
        self.assertTrue(self.new_doc.exists(), result.stderr)
        self.assertEqual(self.new_doc.read_bytes(), content)
        self.assertFalse(self.doc.exists())
        self.assertEqual(result.stderr.count("the document changed after the move request"), 1, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
        self.assertEqual(self.git("branch", "--show-current"), NEW)
        before = self.file_versions()
        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.file_versions(), before)

    def test_unit_reference_cannot_move_an_existing_showrunners_document(self) -> None:
        row = self.production_row()
        unit = fixture_member("unit", name="requested", parent=row.id)
        before = self.file_versions()
        result = self.run_command("apply", unit.id, row.name,
            "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not a showrunner", result.stderr)
        self.assertEqual(self.file_versions(), before)
        self.assertEqual(staff.by_id(row.id), row)

    def file_versions(self) -> dict[Path, tuple[bytes, int]]:
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file() and path.name != "herdr.jsonl"}

    def test_rename_changes_the_row_session_tab_and_workspace_without_a_move(self) -> None:
        row = self.production_row()
        record = sessions.SessionRecord(sessionId=row.current_session, name=OLD, pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        path = self.root / "state/sessions/998041.json"
        self.write(path, json.dumps(record))
        document = self.doc.read_bytes(), self.doc.stat().st_mtime_ns
        branch = self.git("branch", "--show-current")
        tip = self.git("rev-parse", "HEAD")
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = OLD
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True)):
            self.assert_success(self.run_rename("apply"))
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW))
        self.assertEqual((displays.pane, displays.tab, displays.workspace), (NEW,) * 3)
        self.assertEqual(json.loads(path.read_text())["name"], NEW)
        self.assertEqual((self.doc.read_bytes(), self.doc.stat().st_mtime_ns), document)
        self.assertFalse(self.new_doc.exists())
        self.assertEqual((self.git("branch", "--show-current"), self.git("rev-parse", "HEAD")), (branch, tip))

    def test_same_name_is_unchanged_when_every_name_agrees(self) -> None:
        row = self.production_row()
        record = sessions.SessionRecord(sessionId=row.current_session, name=row.name, pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        self.write(self.root / "state/sessions/998041.json", json.dumps(record))
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = row.name
        before = self.file_versions()
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True)):
            result = self.run_command("apply", row.name, row.name)
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), "unchanged")
        self.assertEqual(displays.commands, [])
        self.assertEqual(self.file_versions(), before)

    def test_to_doc_sets_the_row_after_the_file_move_and_keeps_the_document_text(self) -> None:
        row = self.production_row()
        content = self.doc.read_bytes()
        set_doc = staff.set_doc

        def publish(identity: staff.StaffId, doc: staff.LiveDoc | staff.WrappedDoc) -> staff.StaffRow:
            self.assertEqual(identity, row.id)
            self.assertFalse(self.doc.exists())
            self.assertEqual(self.new_doc.read_bytes(), content)
            current = staff.by_id(identity)
            assert current is not None
            self.assertEqual(current.doc, row.doc)
            return set_doc(identity, doc)

        with mock.patch.object(staff, "set_doc", side_effect=publish) as published:
            self.assert_success(self.run_command("apply", OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW))
        published.assert_called_once_with(row.id, staff.LiveDoc("natedev", self.new_doc))
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW, doc=staff.LiveDoc("natedev", self.new_doc)))
        self.assertEqual(self.git("branch", "--show-current"), NEW)
        before = self.file_versions()
        self.assert_success(self.run_command("apply", OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.file_versions(), before)

    def test_document_move_request_is_saved_before_the_row_changes(self) -> None:
        row = self.production_row()
        rename = staff.rename
        document = self.doc.read_bytes()

        def rename_row(identity: staff.StaffId, name: str) -> staff.StaffRow:
            journal = self.state / row.id / "rename.json"
            fields = cast(dict[str, object], json.loads(journal.read_text()))
            self.assertEqual(fields["staff_id"], row.id)
            self.assertEqual(fields["source"], str(self.doc))
            self.assertEqual(fields["destination"], str(self.new_doc))
            self.assertEqual(fields["branch"], NEW)
            self.assertIs(fields["done"], False)
            self.assertEqual(staff.by_id(row.id), row)
            self.assertEqual(self.doc.read_bytes(), document)
            self.assertFalse(self.new_doc.exists())
            self.assertEqual(self.git("branch", "--show-current"), OLD)
            return rename(identity, name)

        with mock.patch.object(staff, "rename", side_effect=rename_row) as renamed:
            self.assert_success(self.run_command("apply", OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW))
        renamed.assert_called_once_with(row.id, NEW)

    def test_finished_rename_rerun_changes_no_bytes_or_times(self) -> None:
        row = self.production_row()
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = OLD
        with displays.installed():
            self.assert_success(self.run_rename("apply"))
            before = self.file_versions()
            commands = list(displays.commands)
            result = self.run_rename("apply")
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), "unchanged")
        self.assertEqual(self.file_versions(), before)
        self.assertEqual(displays.commands, commands)

    # Keep the rename CLI process boundary.
    @process_boundary
    def test_plan_describes_the_rename_without_writes(self) -> None:
        before = self.file_versions()
        result = self.run_rename("plan")
        self.assert_success(result)
        self.assertIn(OLD, result.stdout)
        self.assertIn(NEW, result.stdout)
        self.assertEqual(self.file_versions(), before)

    def test_rename_preserves_staff_stores_and_parent_relationships(self) -> None:
        row = self.production_row()
        child = fixture_member("showrunner", name="child", parent=row.id)
        listed = self.root / "lists/change-review - old.md"
        rows = ["| # | Change | Merged | How to check | Production |",
                "| --- | --- | --- | --- | --- |",
                f"| C1 | own | m | try | {row.id} |",
                f"| C2 | child's | m | try | {child.id} |",
                f"| C3 | root's | m | try | {row.id} |"]
        self.write(listed, "\n".join(rows) + "\n")
        pins = self.home / ".local/state/next"
        question = {"current": f"{row.id}: first",
                    "ended": {f"{row.id}: earlier": "2026-10-27T01:00:00"},
                    "moved": ["oldish: kept"]}
        pin = pins / f"question-{row.id}.json"
        aggregate = pins / f"next-{row.id}-all-below.json"
        self.write(pin, json.dumps(question))
        self.write(aggregate, json.dumps({"current": "C1", "source": "change-old"}))
        bevy = self.home / ".local/state/bevy-upgrade"
        watch = bevy / f"{row.id}.json"
        lock = bevy / f"{row.id}.lock"
        job = self.notifier / f"bevy-upgrade-{row.id}/conf"
        self.write(watch, json.dumps({"staff_id": row.id, "entries": 3}))
        self.write(lock, "")
        self.write(job, f"EVERY=240\nRUN=py bevy_upgrade.py --staff-id {row.id} check --scheduled\n"
                       + f"FROM=bevy-upgrade-{row.id}\n")
        run = self.root / "runs/current/progress_history_state.json"
        self.write(run, json.dumps({"owner_staff_id": row.id}))
        fixed = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in (listed, pin, aggregate, watch, lock, job, run)}
        displays = support.RenameDisplays(row)
        with displays.installed():
            self.assert_success(self.run_rename("apply"))
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in fixed}, fixed)
        self.assertEqual(json.loads(pin.read_text()), question)
        self.assertEqual(json.loads(aggregate.read_text()), {"current": "C1", "source": "change-old"})
        for content in rows[2:]:
            self.assertIn(content, listed.read_text())
        self.assertEqual(json.loads(watch.read_text()), {"staff_id": row.id, "entries": 3})
        self.assertEqual(lock.read_text(), "")
        self.assertIn(f"--staff-id {row.id} check", job.read_text())
        self.assertIn(f"FROM=bevy-upgrade-{row.id}\n", job.read_text())
        self.assertEqual(json.loads(run.read_text()), {"owner_staff_id": row.id})
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW))
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(child.parent, staff.ReportsTo(row.id))

    def test_invalid_staff_name_refuses_without_writes(self) -> None:
        before = self.snapshot()
        result = self.run_rename("apply", "../escape")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("../escape", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    _ = unittest.main()
