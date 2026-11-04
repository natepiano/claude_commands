"""Rename the staff unit and preserve its folder contents and Git history."""
from __future__ import annotations

import unittest
import json
from pathlib import Path
from typing import final
from unittest import mock

from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production import staff, unit_config
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id
from scripts.production.rename_test_support import RenameDisplays
from scripts.production.rename_rerun_test_support import final_files
from scripts.production.rename_unit_test_support import UnitRenameFixture


@final
class UnitRenameTests(UnitRenameFixture):
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
        destination = self.home / "worktrees/gamma"
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual((renamed.name, renamed.worktree),
                         ("gamma", staff.RemovedWorktree("natedev", destination)))
        self.assertFalse(destination.exists())
        self.assertEqual(result.stderr.splitlines(), [f"anomaly: folder is absent: {destination}"])
        before = final_files(self.root)
        self.assert_ok(self.rename("apply"))
        self.assertEqual(final_files(self.root), before)

    def test_worktree_rename_updates_a_removed_location_without_reactivating_its_row(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        source, destination = self.rust / "claude-demo-alpha", self.home / "worktrees/gamma"
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
        destination = self.home / "worktrees/gamma"
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", source), expected=row.worktree)
        config = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                        unit_config.AppPort(8123), ("nightly",))
        _ = unit_config.write(source, config)
        data = (source / ".unit.toml").read_bytes()
        with mock.patch.object(staff, "set_worktree", side_effect=OSError("stopped after folder move")):
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
        destination = staff.PresentWorktree("natedev", self.home / "worktrees/gamma")
        publish = staff.set_worktree

        def interrupted(identity: staff.StaffId, value: staff.PresentWorktree | staff.RemovedWorktree, *,
                        expected: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled) -> staff.StaffRow:
            self.assertEqual(expected, row.worktree)
            _ = publish(identity, value, expected=expected)
            raise OSError("stopped after staff publication")

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
            worktree=staff.PresentWorktree("natedev", self.home / "worktrees/gamma"), doc=row.doc))
        self.assertEqual(staff.snapshot().by_id(worker.id), worker)

    def test_uncommitted_work_between_phases_also_keeps_the_tip(self) -> None:
        worktree = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "draft.md", "a new file\n")
        self.assert_ok(self.rename("apply"))
        new = self.home / "worktrees/gamma"
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
        destination = self.home / "worktrees/gamma"
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "gamma")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
        self.assertEqual((destination / "work.md").read_text(), "phase work\nmore, not committed\n")
        self.assertEqual((destination / "draft.md").read_text(), "a new file\n")
        self.assertEqual((destination / "docs/plans/alpha.md").read_bytes(), plan)


if __name__ == "__main__":
    _ = unittest.main()
