"""Short-name repair is read-only in plan and repeatable across stale journals."""
from __future__ import annotations

import json
import unittest
from typing import cast
from unittest import mock

from scripts.production import staff
from scripts.production import fake_unit_host
from scripts.production.rename_test_support import legacy_staff_journal
from scripts.production.rename_rerun_test_support import saved_fixture
from scripts.production.rename_unit_test_support import SLUG, UnitRenameFixture


class UnitRenameRepairTests(UnitRenameFixture):
    def test_plan_refuses_a_name_taken_under_another_showrunner_without_writing(self) -> None:
        other = staff.ensure("showrunner", name="other")
        _ = staff.ensure("unit", name="gamma", parent=other.id)
        before = self.snapshot()
        planned = self.rename("plan")
        self.assertEqual(planned.returncode, 2, planned.stderr)
        self.assertIn("unit name already exists: gamma", planned.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_same_name_moves_legacy_paths_and_keeps_membership_and_live_state(self) -> None:
        before = staff.snapshot()
        stretch = self.stall_path("alpha-unit").read_bytes()
        planned = self.rename("plan", "alpha", "alpha")
        self.assert_ok(planned)
        self.assertIn("branch demo-alpha -> alpha", planned.stdout)
        self.assert_ok(self.rename("apply", "alpha", "alpha"))
        destination = self.home / "worktrees/alpha"
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "alpha")
        self.assertFalse((self.rust / "claude-demo-alpha").exists())
        self.assertEqual(staff.snapshot(), before)
        self.assertEqual(self.stall_path("alpha-unit").read_bytes(), stretch)
        recorder = cast(dict[str, object], json.loads((self.root / "sessions/s1/progress_history_state.json").read_text()))
        self.assertEqual((recorder["working_dir"], recorder["branch"]), (str(destination), "alpha"))
        snapshot = self.snapshot()
        self.assert_ok(self.rename("plan", "alpha", "alpha"))
        result = self.rename("apply", "alpha", "alpha")
        self.assert_ok(result)
        self.assertIn("done; unchanged", result.stdout)
        self.assertEqual(self.snapshot(), snapshot)
        _ = self.mark_live("alpha-unit", "sid-alpha", name="alpha")
        live_snapshot = self.snapshot()
        self.assert_ok(self.rename("apply", "alpha", "alpha"))
        self.assertEqual(self.snapshot(), live_snapshot)

    def test_same_name_plan_repairs_a_wrong_tab_when_paths_and_session_match(self) -> None:
        self.assert_ok(self.rename("apply", "alpha", "alpha"))
        _ = self.mark_live("alpha-unit", "sid-alpha", name="alpha")
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit(
            "demo-alpha-unit", "w1:p1", {"SHOWRUNNER_UNIT": SLUG, "SHOWRUNNER_UNIT_ID": "alpha-unit"},
            label="old-tab")])
        before = self.snapshot()
        planned = self.rename("plan", "alpha", "alpha")
        self.assert_ok(planned)
        self.assertNotIn("(done)", planned.stdout)
        self.assertIn("already matches: the id is alpha-unit", planned.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_same_name_repairs_only_the_folder_when_branch_already_matches(self) -> None:
        old = self.rust / "claude-demo-alpha"
        _ = self.git("branch", "-m", "demo-alpha", "alpha")
        _ = self.git("push", "origin", "alpha")
        self.write(self.doc, self.doc.read_text().replace("| demo-alpha |", "| alpha |"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "branch is already repaired")
        planned = self.rename("plan", "alpha", "alpha")
        self.assert_ok(planned)
        self.assertIn(f"keep branch alpha; worktree {old} ->", planned.stdout)
        self.assert_ok(self.rename("apply", "alpha", "alpha"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.home / "worktrees/alpha"), "alpha")

    def test_completed_legacy_journal_never_blocks_a_different_plan_or_writes(self) -> None:
        self.assert_ok(self.rename("apply"))
        path = self.state / SLUG / "rename_unit.json"
        legacy_staff_journal(path)
        unit = next(row for row in staff.snapshot().find(kind="unit") if row.name == "gamma")
        _ = staff.retire(unit.id)
        before = self.snapshot()
        self.assert_ok(self.rename("plan", "mul_add", "multiply"))
        self.assertEqual(self.snapshot(), before)

    def test_unfinished_journal_for_an_absent_staff_id_is_stale(self) -> None:
        self.assert_ok(self.rename("apply"))
        path = self.state / SLUG / "rename_unit.json"
        saved = self.journal()
        saved.update(staff_id="s00000000000", done=False, step=0)
        self.write(path, json.dumps(saved))
        before = self.snapshot()
        self.assert_ok(self.rename("plan", "mul_add", "multiply"))
        self.assertEqual(self.snapshot(), before)

    def test_retired_journal_does_not_block_another_units_plan_apply_or_verify(self) -> None:
        self.assert_ok(self.rename("apply"))
        path = self.state / SLUG / "rename_unit.json"
        retired_id = staff.StaffId(cast(str, self.journal()["staff_id"]))
        _ = staff.retire(retired_id)
        self.assertIsNone(staff.snapshot().by_id(retired_id))
        successor = next(row for row in staff.snapshot().find(kind="unit") if row.name == "mul_add")
        with saved_fixture(self.root) as restore:
            for done in (True, False):
                for legacy in (True, False):
                    with self.subTest(done=done, legacy=legacy):
                        restore()
                        saved = self.journal()
                        saved["done"] = done
                        self.write(path, json.dumps(saved))
                        if legacy:
                            legacy_staff_journal(path)
                        before = self.snapshot()
                        self.assert_ok(self.rename("plan", "mul_add", "multiply"))
                        self.assertEqual(self.snapshot(), before)
                        self.assert_ok(self.rename("apply", "mul_add", "multiply"))
                        self.assert_ok(self.rename("verify", "mul_add", "multiply"))
                        self.assertIs(self.journal()["done"], True)
                        self.assertEqual(self.journal()["staff_id"], successor.id)
                        renamed = staff.snapshot().by_id(successor.id)
                        self.assertIsNotNone(renamed)
                        assert renamed is not None
                        self.assertEqual(renamed.name, "multiply")
                        self.assertEqual(renamed.parent, successor.parent)

    def test_matching_legacy_journal_plan_does_not_migrate_on_disk(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        legacy_staff_journal(self.state / SLUG / "rename_unit.json")
        before = self.snapshot()
        self.assert_ok(self.rename("plan"))
        self.assertEqual(self.snapshot(), before)

    def test_staff_showrunner_name_wins_over_a_retired_slug_alias(self) -> None:
        parent = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == SLUG)
        _ = staff.rename(parent.id, "natedev")
        self.write(self.doc, self.doc.read_text().replace("- **Log:**", "- **Showrunner session:** `natedev`\n- **Log:**"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "name showrunner from staff")
        import showrunners

        canonical = showrunners.canonical_slug

        def old_alias(value: str) -> str:
            return "retired" if value == "natedev" else canonical(value)

        with mock.patch.object(showrunners, "canonical_slug", side_effect=old_alias):
            before = self.snapshot()
            result = self.invoke(["plan", "natedev", "alpha", "alpha", "--scratch", str(self.root / "scratch")])
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    _ = unittest.main()
