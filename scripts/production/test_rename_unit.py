"""Rename the staff unit and preserve its folder contents and Git history."""
from __future__ import annotations

import unittest
import json
import os
from pathlib import Path
from typing import final
from unittest import mock

from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production import staff, unit_config
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id
from scripts.production.rename_test_support import RenameDisplays
from scripts.production.rename_rerun_test_support import file_versions, final_files, saved_fixture
from scripts.production.rename_unit_test_support import FolderRenameFixture, FolderSessionFixture, UnitRenameFixture


@final
class UnitRenameTests(UnitRenameFixture):
    def test_review_refuses_a_branch_that_differs_from_the_pending_move(self) -> None:
        from scripts.production import unit_move

        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        with saved_fixture(self.root) as restore:
            for pending_name in ("", "alpha"):
                with self.subTest(pending_name=pending_name):
                    restore()
                    unit_move.prepare_move(row, folder, "pending-branch", pending_name)
                    before = file_versions(self.root)
                    result = self.invoke(["apply", "alpha", "alpha", "--to-branch", "cargo-liner-handler"])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("finish the pending move to branch pending-branch", result.stderr)
                    self.assertEqual(file_versions(self.root), before)

    def test_review_push_uses_an_explicit_refspec_for_the_move_and_retry(self) -> None:
        from scripts.production import unit_move

        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        branch = "cargo-liner-handler"
        with saved_fixture(self.root) as restore:
            for recovering in (False, True):
                with self.subTest(recovering=recovering):
                    restore()
                    _ = self.git("config", "remote.origin.push", f"refs/heads/{branch}:refs/heads/unwanted")
                    if recovering:
                        unit_move.prepare_move(row, folder, branch, row.name)
                        _ = self.git("branch", "-m", branch, cwd=folder)
                    git = unit_move.git
                    pushes: list[tuple[str, ...]] = []

                    def record(checkout: Path, *arguments: str) -> str:
                        if arguments[:1] == ("push",):
                            pushes.append(arguments)
                        return git(checkout, *arguments)

                    with mock.patch.object(unit_move, "git", side_effect=record):
                        result = self.invoke(["apply", "alpha", "alpha", "--to-branch", branch])
                    self.assert_ok(result)
                    self.assertEqual(pushes, [("push", "--set-upstream", "origin", f"{branch}:refs/heads/{branch}")])
                    self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", branch),
                                     self.git("rev-parse", "HEAD", cwd=folder))
                    self.assertEqual(self.git("--git-dir", str(self.origin), "for-each-ref", "refs/heads/unwanted"), "")

    def test_name_change_pushes_the_branch_with_its_new_suffix(self) -> None:
        folder = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.assert_ok(self.rename("apply"))
        destination = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "demo-gamma")
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "demo-gamma"), tip)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=destination), "origin/demo-gamma")

    def test_to_branch_keeps_the_unit_name_and_folder_and_resumes_its_session(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.write(self.home / f".claude/projects/fixture/{row.current_session}.jsonl", "{}\n")
        session = FolderSessionFixture(row, live=True)
        with session.installed(), RenameDisplays(row).installed():
            self.assert_ok(self.invoke(["apply", "alpha", "alpha", "--to-branch", "cargo-liner-handler"]))
        self.assertEqual(staff.by_id(row.id), row)
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=folder), "cargo-liner-handler")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=folder), tip)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "cargo-liner-handler"), tip)
        self.assertEqual(session.stops, [row.current_session])
        self.assertEqual([resumed.id for resumed in session.starts], [row.id])

    def test_to_branch_with_a_name_change_moves_the_folder_suffix_and_preserves_work(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        destination = self.rust / "claude-demo-gamma"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.write(folder / "draft.md", "Keep the draft.\n")
        self.assert_ok(self.invoke(["apply", "alpha", "gamma", "--to-branch", "cargo-liner-handler"]))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual((renamed.name, renamed.worktree), ("gamma", staff.PresentWorktree("natedev", destination)))
        self.assertFalse(folder.exists())
        self.assertEqual((destination / "draft.md").read_text(), "Keep the draft.\n")
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "cargo-liner-handler")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "cargo-liner-handler"), tip)

    def test_to_branch_plan_describes_the_unit_branch_without_writes(self) -> None:
        before = file_versions(self.root)
        result = self.invoke(["plan", "alpha", "alpha", "--to-branch", "cargo-liner-handler"])
        self.assert_ok(result)
        self.assertIn("Rename branch demo-alpha -> cargo-liner-handler", result.stdout)
        self.assertEqual(file_versions(self.root), before)

    def test_to_branch_refuses_an_existing_target_for_units_and_showrunners(self) -> None:
        for arguments in (["alpha", "alpha"], ["demo", "demo", "--to-doc", str(self.doc)]):
            with self.subTest(arguments=arguments):
                before = file_versions(self.root)
                result = self.invoke(["apply", *arguments, "--to-branch", "demo-mul-add"])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("branch already exists: demo-mul-add", result.stderr)
                self.assertEqual(file_versions(self.root), before)

    def test_to_branch_and_suffix_rename_reruns_finish_an_interrupted_push(self) -> None:
        from scripts.production import unit_move

        with saved_fixture(self.root) as restore:
            for name, branch, options in (("alpha", "cargo-liner-handler", ["--to-branch", "cargo-liner-handler"]),
                                          ("gamma", "cargo-liner-handler", ["--to-branch", "cargo-liner-handler"]),
                                          ("gamma", "demo-gamma", [])):
                with self.subTest(name=name, branch=branch):
                    restore()
                    row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
                    displays = RenameDisplays(row)
                    arguments = ["apply", "alpha", name, *options]
                    git = unit_move.git

                    def interrupted(checkout: Path, *arguments: str) -> str:
                        if arguments[:1] == ("push",):
                            raise OSError("The push stopped.")
                        return git(checkout, *arguments)

                    with displays.installed():
                        with mock.patch.object(unit_move, "git", side_effect=interrupted):
                            failed = self.invoke(arguments)
                        self.assertNotEqual(failed.returncode, 0)
                        self.assertIn("The push stopped.", failed.stderr)
                        destination = self.rust / f"claude-demo-{name}"
                        self.assertEqual(self.git("branch", "--show-current", cwd=destination), branch)
                        self.assert_ok(self.invoke(arguments))
                        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", branch),
                                         self.git("rev-parse", "HEAD", cwd=destination))
                        before = file_versions(self.root)
                        rerun = self.invoke(arguments)
                        self.assert_ok(rerun)
                        self.assertEqual(rerun.stdout.strip(), "unchanged")
                        self.assertEqual(file_versions(self.root), before)

    def test_to_branch_does_not_allow_a_unit_document_move(self) -> None:
        before = file_versions(self.root)
        result = self.invoke(["apply", "alpha", "alpha", "--to-doc", str(self.doc)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--to-doc is only for showrunners", result.stderr)
        self.assertEqual(file_versions(self.root), before)

    def test_bare_unit_name_renames_the_unit_folder_and_branch(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        with RenameDisplays(row).installed():
            self.assert_ok(self.invoke(["apply", "alpha", "gamma"]))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.name, "gamma")
        self.assertEqual(renamed.worktree, staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.rust / "claude-demo-gamma"), "demo-gamma")

    def test_bare_unit_rename_rerun_keeps_the_callers_unit_and_leaves_the_foreign_unit_unchanged(self) -> None:
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        own = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        foreign_parent = fixture_member("showrunner", name="foreign")
        foreign = fixture_member("unit", name="alpha", parent=foreign_parent.id)
        self.env["CLAUDE_CODE_SESSION_ID"] = parent.current_session
        with RenameDisplays(own).installed():
            self.assert_ok(self.invoke(["apply", "alpha", "gamma"]))
            before = final_files(self.root)
            rerun = self.invoke(["apply", "alpha", "gamma"])
            self.assert_ok(rerun)
            self.assertEqual(rerun.stdout.strip(), "unchanged")
            self.assertEqual(final_files(self.root), before)
        renamed = staff.by_id(own.id)
        assert renamed is not None
        self.assertEqual(renamed.name, "gamma")
        self.assertEqual(staff.by_id(foreign.id), foreign)

    def test_bare_unit_name_with_two_matches_outside_the_caller_requires_a_path(self) -> None:
        caller = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        for name in ("first", "second"):
            parent = fixture_member("showrunner", name=name)
            _ = fixture_member("unit", name="duplicate", parent=parent.id)
        self.env["CLAUDE_CODE_SESSION_ID"] = caller.current_session
        before = final_files(self.root)
        result = self.invoke(["apply", "duplicate", "renamed"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate: several rows match; use showrunner/name or the staff id", result.stderr)
        self.assertEqual(final_files(self.root), before)

    def test_same_name_repairs_displays_with_an_unfilled_worktree(self) -> None:
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        row = fixture_member("unit", name="unfilled", parent=parent.id)
        displays = RenameDisplays(row)
        with displays.installed():
            result = self.rename("apply", row.name, row.name)
        self.assert_ok(result)
        self.assertEqual((displays.pane, displays.tab), (row.name,) * 2)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_the_current_session_name_without_moving(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("unit-session-after-clear")))
        record = sessions.SessionRecord(sessionId=row.current_session, name="previous", pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        path = self.root / "state/sessions/998041.json"
        self.write(path, json.dumps(record))
        displays = RenameDisplays(row)
        displays.pane = displays.tab = row.name
        before = final_files(self.root)
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True)):
            self.assert_ok(self.rename("apply", row.name, row.name))
            self.assertEqual(json.loads(path.read_text())["name"], row.name)
            settled = path.read_bytes(), path.stat().st_mtime_ns
            rerun = self.rename("apply", row.name, row.name)
        self.assert_ok(rerun)
        self.assertEqual(rerun.stdout.strip(), "unchanged")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), settled)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(final_files(self.root), before)

    def test_same_name_repairs_each_display_without_moving_the_worktree(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("unit-after-clear")))
        for field in ("pane", "tab"):
            with self.subTest(field=field):
                displays = RenameDisplays(row)
                displays.pane = displays.tab = row.name
                setattr(displays, field, "previous")
                before = final_files(self.root)
                with displays.installed():
                    result = self.rename("apply", row.name, row.name)
                    self.assert_ok(result)
                    self.assertNotIn("unchanged", result.stdout)
                    self.assertEqual((displays.pane, displays.tab), (row.name, row.name))
                    self.assertEqual(staff.by_id(row.id), row)
                    self.assertEqual(final_files(self.root), before)
                    commands = list(displays.commands)
                    rerun = self.rename("apply", row.name, row.name)
                self.assert_ok(rerun)
                self.assertEqual(rerun.stdout.strip(), "unchanged")
                self.assertEqual(displays.commands, commands)
                self.assertEqual(final_files(self.root), before)

    def test_rename_continues_when_the_removed_worktree_is_absent(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        source = self.rust / "claude-demo-alpha"
        _ = self.git("worktree", "remove", str(source))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", source), expected=row.worktree)
        with mock.patch.object(herdr.Herdr, "panes", return_value=[]):
            result = self.rename("apply")
        self.assert_ok(result)
        destination = self.rust / "claude-demo-gamma"
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual((renamed.name, renamed.worktree),
                         ("gamma", staff.RemovedWorktree("natedev", destination)))
        self.assertFalse(destination.exists())
        self.assertIn(f"anomaly: folder is absent: {destination}", result.stderr.splitlines())
        before = final_files(self.root)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(final_files(self.root), before)

    def test_worktree_rename_updates_a_removed_location_without_reactivating_its_row(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        source, destination = self.rust / "claude-demo-alpha", self.rust / "claude-demo-gamma"
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", source), expected=row.worktree)
        self.assert_ok(self.rename("apply"))
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual((retained.id, retained.name, retained.parent, retained.worktree),
                         (row.id, "gamma", row.parent, staff.RemovedWorktree("natedev", destination)))
        before = final_files(self.root)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(final_files(self.root), before)

    def test_a_retry_after_git_move_sets_the_new_staff_path_and_keeps_config_bytes_and_identity(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        source = self.rust / "claude-demo-alpha"
        destination = self.rust / "claude-demo-gamma"
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", source), expected=row.worktree)
        config = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                        unit_config.AppPort(8123), ("nightly",))
        _ = unit_config.write(source, config)
        data = (source / ".unit.toml").read_bytes()
        with mock.patch.object(staff, "set_worktree", side_effect=SystemExit(91)):
            interrupted = self.rename("apply")
        self.assertNotEqual(interrupted.returncode, 0, interrupted.stdout + interrupted.stderr)
        self.assertFalse(source.exists())
        self.assertTrue(destination.is_dir())
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual(retained.worktree, staff.PresentWorktree("natedev", source))
        self.assert_ok(self.rename("apply"))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual((renamed.name, renamed.parent, renamed.worktree),
                         ("gamma", row.parent, staff.PresentWorktree("natedev", destination)))
        self.assertEqual((destination / ".unit.toml").read_bytes(), data)
        self.assertEqual(self.git("check-ignore", ".unit.toml", cwd=destination), ".unit.toml")
        before = final_files(self.root)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(final_files(self.root), before)

    def test_retry_after_staff_publication_keeps_the_settled_worktree(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        destination = staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma")
        publish = staff.set_worktree

        def interrupted(identity: staff.StaffId, value: staff.PresentWorktree | staff.RemovedWorktree, *,
                        expected: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled) -> staff.StaffRow:
            self.assertEqual(expected, row.worktree)
            _ = publish(identity, value, expected=expected)
            raise SystemExit(91)

        with mock.patch.object(staff, "set_worktree", side_effect=interrupted):
            stopped = self.rename("apply")
        self.assertNotEqual(stopped.returncode, 0, stopped.stderr)
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.worktree, destination)
        self.assert_ok(self.rename("apply"))
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.worktree, destination)
        before = final_files(self.root)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(final_files(self.root), before)

    def test_rename_unit_preserves_staff_and_worker_identity(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        worker = fixture_member("worker", name="impl1", parent=row.id)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(
            row.id, "unit", "gamma", row.parent, current_session=row.id,
            worktree=staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma"), doc=row.doc))
        self.assertEqual(staff.snapshot().by_id(worker.id), worker)

    def test_uncommitted_work_between_phases_also_keeps_the_tip(self) -> None:
        worktree = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "draft.md", "a new file\n")
        self.assert_ok(self.rename("apply"))
        new = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new), tip)
        self.assertEqual((new / "draft.md").read_text(), "a new file\n")

    def test_a_mid_phase_rename_keeps_the_commits_and_the_uncommitted_work(self) -> None:
        source = self.rust / "claude-demo-alpha"
        self.write(source / "work.md", "phase work\n")
        _ = self.git("add", "work.md", cwd=source)
        _ = self.git("commit", "-m", "checkpoint: phase 1", cwd=source)
        tip = self.git("rev-parse", "HEAD", cwd=source)
        self.write(source / "work.md", "phase work\nmore, not committed\n")
        self.write(source / "draft.md", "a new file\n")
        plan = (source / "docs/plans/alpha.md").read_bytes()
        self.assert_ok(self.rename("apply"))
        destination = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "demo-gamma")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
        self.assertEqual((destination / "work.md").read_text(), "phase work\nmore, not committed\n")
        self.assertEqual((destination / "draft.md").read_text(), "a new file\n")
        self.assertEqual((destination / "docs/plans/alpha.md").read_bytes(), plan)


@final
class FolderRenameTests(FolderRenameFixture):
    old_name: str = "port"
    new_name: str = "x"

    def test_rename_finishes_a_pending_folder_move_before_renaming_its_folder_and_branch(self) -> None:
        from dataclasses import replace
        from scripts.production import unit_move

        moved = self.source.with_name("staging-port")
        destination = moved.with_name("staging-x")
        unit_move.prepare_move(self.owner, moved, unit_move.KeepBranch.CURRENT)
        real_git, publish, prepare, rename = unit_move.git, staff.set_worktree, unit_move.prepare_move, staff.rename
        with saved_fixture(self.root) as restore:
            for point in ("complete", "folder move", "folder publication", "before rename journal",
                          "rename journal", "staff rename", "rename move", "rename branch", "rename publication"):
                with self.subTest(point=point):
                    restore()
                    displays = RenameDisplays(self.owner)
                    displays.pane = displays.tab = self.owner.name

                    def stop_after(step: str) -> None:
                        if step == point:
                            raise SystemExit(91)

                    def git(folder: Path, *arguments: str) -> str:
                        result = real_git(folder, *arguments)
                        if arguments[:2] == ("worktree", "move"):
                            stop_after("folder move" if folder == self.source else "rename move")
                        elif arguments[:2] == ("branch", "-m"):
                            stop_after("rename branch")
                        return result

                    def publish_worktree(identity: staff.StaffId,
                                         value: staff.PresentWorktree | staff.RemovedWorktree, *,
                                         expected: staff.PresentWorktree | staff.RemovedWorktree |
                                         staff.WorktreeUnfilled) -> staff.StaffRow:
                        result = publish(identity, value, expected=expected)
                        stop_after("folder publication" if value.path == moved else "rename publication")
                        return result

                    def prepare_rename(row: staff.StaffRow, folder: Path, branch: str | unit_move.KeepBranch,
                                       name: str = "") -> None:
                        stop_after("before rename journal")
                        prepare(row, folder, branch, name)
                        stop_after("rename journal")

                    def rename_row(identity: staff.StaffId, name: str) -> staff.StaffRow:
                        result = rename(identity, name)
                        stop_after("staff rename")
                        return result

                    with FolderSessionFixture(self.owner, live=False).installed(), displays.installed():
                        with (mock.patch.object(unit_move, "git", side_effect=git),
                              mock.patch.object(staff, "set_worktree", side_effect=publish_worktree),
                              mock.patch.object(unit_move, "prepare_move", side_effect=prepare_rename),
                              mock.patch.object(staff, "rename", side_effect=rename_row)):
                            result = self.command()
                        if point != "complete":
                            self.assertEqual(result.returncode, 91, result.stdout + result.stderr)
                            result = self.command()
                        self.assertEqual((result.returncode, result.stderr), (0, ""))
                        self.assertEqual(result.stdout.splitlines()[-1], "done")
                        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="x",
                            worktree=staff.PresentWorktree("natedev", destination)))
                        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "cargo-liner-x")
                        self.assertEqual((destination / "kept.txt").read_text(), "Keep this work.\n")
                        self.assertEqual((displays.pane, displays.tab), ("x", "x"))
                        before = file_versions(self.root)
                        commands = list(displays.commands)
                        rerun = self.command()
                        self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
                        self.assertEqual(file_versions(self.root), before)
                        self.assertEqual(displays.commands, commands)

    def test_f017_same_name_with_unit_suffix_changes_nothing(self) -> None:
        from scripts.production import rename, unit_move

        displays = RenameDisplays(self.owner)
        displays.pane = displays.tab = self.owner.name
        labels = displays.pane, displays.tab, displays.workspace
        before = file_versions(self.root)
        parent = staff.snapshot().parent_of(self.owner)
        assert parent is not None
        arguments = ["apply", f"{parent.name}/port-unit", "port"]
        with (FolderSessionFixture(self.owner, live=False).installed(), displays.installed(),
              mock.patch.object(unit_move, "move_folder", wraps=unit_move.move_folder) as move):
            result = invoke_cli(rename, lambda: rename.main(arguments), arguments,
                                dict(os.environ), self.checkout)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        move.assert_not_called()
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-port")
        self.assertEqual((displays.pane, displays.tab, displays.workspace), labels)
        self.assertEqual(displays.commands, [])

    def test_f018_finished_pending_rename_reports_done_then_unchanged(self) -> None:
        from dataclasses import replace
        from scripts.production import unit_move

        unit_move.prepare_move(self.owner, self.destination, "cargo-liner-x", "x")
        renamed = staff.rename(self.owner.id, "x")
        _ = self.git("worktree", "move", str(self.source), str(self.destination))
        _ = self.git("branch", "-m", "cargo-liner-x", cwd=self.destination)
        renamed = staff.set_worktree(renamed.id, staff.PresentWorktree("natedev", self.destination),
                                    expected=renamed.worktree)
        displays = RenameDisplays(renamed)
        displays.pane = displays.tab = renamed.name
        with FolderSessionFixture(renamed, live=False).installed(), displays.installed():
            result = self.command()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertEqual(result.stdout.splitlines()[-1], "done")
            self.assertEqual(staff.by_id(renamed.id), replace(self.owner, name="x",
                worktree=staff.PresentWorktree("natedev", self.destination)))
            self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-x")
            before = file_versions(self.root)
            rerun = self.command()
        self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
        self.assertEqual(file_versions(self.root), before)

    def test_f020_displays_change_before_the_folder_and_branch(self) -> None:
        from scripts.production import unit_move

        displays = RenameDisplays(self.owner)
        events: list[str] = []
        real_git = unit_move.git

        def display(*arguments: str, **options: object) -> str:
            result = displays.run(*arguments, **options)
            events.append(arguments[0])
            return result

        def git(folder: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "move"):
                events.append("folder")
            elif arguments[:2] == ("branch", "-m"):
                events.append("branch")
            return real_git(folder, *arguments)

        with (FolderSessionFixture(self.owner, live=False).installed(), displays.installed(),
              mock.patch.object(herdr.Herdr, "run", side_effect=display),
              mock.patch.object(unit_move, "git", side_effect=git)):
            result = self.command()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(events, ["pane", "tab", "folder", "branch"])
        self.assertEqual((displays.pane, displays.tab), ("x", "x"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-x")
        self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")

    def test_same_name_writes_nothing_and_moves_nothing(self) -> None:
        from scripts.production import rename, unit_move

        before = file_versions(self.root)
        with (mock.patch.object(rename, "repair_displays", return_value=False),
              mock.patch.object(unit_move, "move_folder") as move):
            result = self.command(new="port")
        self.assertEqual(result.returncode, 0, result.stderr)
        move.assert_not_called()
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-port")

    def test_new_name_preserves_the_folder_parent_and_branch_prefix(self) -> None:
        from dataclasses import replace
        from scripts.production import rename

        with (FolderSessionFixture(self.owner, live=False).installed(),
              mock.patch.object(rename, "repair_displays", return_value=False)):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.destination, self.home / "rust/cargo-liner-x")
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-x")
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="x",
            worktree=staff.PresentWorktree("natedev", self.destination)))
        self.assertFalse((self.home / "worktrees/x").exists())

    def test_unmatched_folder_stays_and_reports_one_anomaly_while_branch_moves(self) -> None:
        from dataclasses import replace
        from scripts.production import rename

        original = self.source
        self.source = original.with_name("unmatched")
        _ = self.git("worktree", "move", str(original), str(self.source))
        _ = staff.set_worktree(self.owner.id, staff.PresentWorktree("natedev", self.source),
                              expected=staff.PresentWorktree("natedev", original))
        with (FolderSessionFixture(self.owner, live=False).installed(),
              mock.patch.object(rename, "repair_displays", return_value=False)):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertTrue(result.stderr.startswith("anomaly:"), result.stderr)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-x")
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="x"))


if __name__ == "__main__":
    _ = unittest.main()
