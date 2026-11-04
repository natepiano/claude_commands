"""Production rename planning, state moves and apply reruns."""

from __future__ import annotations

from scripts.production.staff_test_support import fixture_member

import json
import shlex
import unittest
from typing import final
from unittest import mock

from scripts.production import rename_test_support as support
from scripts.production import staff
from scripts.production.rename_test_support import CHECK_TIMER_HANDOFF, FAIL_GIT_MOVE_INDEX, NEW, OLD, UNITS


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_document_rename_updates_a_wrapped_location_without_reactivating_its_row(self) -> None:
        row = self.production_row()
        _ = staff.set_doc(row.id, staff.WrappedDoc("natedev", self.doc))
        self.assert_success(self.run_rename("apply"))
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual((retained.id, retained.name, retained.parent, retained.doc),
                         (row.id, NEW, row.parent, staff.WrappedDoc("natedev", self.new_doc)))
        before = self.snapshot()
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.snapshot(), before)

    def test_retry_after_doc_move_updates_the_live_doc_on_the_same_staff_id(self) -> None:
        row = self.production_row()
        _ = staff.set_doc(row.id, staff.LiveDoc("natedev", self.doc))
        with mock.patch.object(staff, "set_doc", side_effect=OSError("stopped after doc move")):
            interrupted = self.run_rename("apply")
        self.assertIn("anomaly: rename_doc: stopped after doc move", interrupted.stderr)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.is_file())
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual(retained.doc, staff.LiveDoc("natedev", self.doc))
        self.assert_success(self.run_rename("apply"))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
        before = self.snapshot()
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.snapshot(), before)

    def production_row(self) -> staff.StaffRow:
        return next(row for row in staff.find(kind="showrunner") if row.name == OLD)

    def test_plan_uses_staff_children_without_reading_saved_parent_contents(self) -> None:
        self.write(self.state / "child/parent", "obsolete-parent\n")
        self.write(self.state / "other/parent", "old\n")
        result = self.run_rename("plan")
        self.assert_success(result)
        self.assertIn(f"4. parent {self.state / 'child/parent'}: old -> new", result.stdout)
        self.assertNotIn(f"4. parent {self.state / 'other/parent'}:", result.stdout)

    def test_rename_finds_the_workspace_member_and_keeps_its_id(self) -> None:
        row = self.production_row()
        _ = staff.rename(row.id, "tools")
        self.write(self.doc, self.doc.read_text().replace("## Production Context\n", "## Production Context\n\n- **Showrunner session:** `tools`\n"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record workspace name")
        self.assert_success(self.run_command("apply", "tools", NEW))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(
            row.id, "showrunner", "new", None, current_session=row.id,
            doc=staff.LiveDoc("natedev", self.new_doc)))
        self.assertIn("- **Showrunner session:** `new`", self.new_doc.read_text())

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

    def test_unit_move_resolves_a_moved_showrunner_checkout(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        # These unit worktrees use the conventional names, unlike the apply fixtures.
        old = self.checkout
        self.checkout = old.with_name("moved-checkout")
        _ = old.rename(self.checkout)
        self.doc = self.checkout / "docs/plans/old-production.md"
        self.new_doc = self.doc.with_name("new-production.md")
        _ = self.git("worktree", "repair")
        self.write(self.new_doc, self.new_doc.read_text().replace(str(old), str(self.checkout)))
        _ = self.git("commit", "-m", "Record the moved checkout", "--", str(self.new_doc))
        row = next(row for row in staff.find(kind="showrunner") if row.name == NEW)
        _ = staff.set_doc(row.id, staff.LiveDoc("natedev", self.new_doc))
        conf = self.notifier / "showrunner-new/conf"
        self.write(conf, conf.read_text().replace(str(old), str(self.checkout)))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")

    def test_plan_and_apply_skip_plans_removed_from_git(self) -> None:
        untracked = self.plan_path("alpha-unit")
        original = untracked.read_text()
        _ = self.git("rm", "--cached", "--", str(untracked))
        _ = self.git("rm", "--", str(self.plan_path("beta-unit")))
        self.write(self.checkout / ".gitignore", "docs/plans/alpha-unit.md\n")
        _ = self.git("add", ".gitignore")
        _ = self.git("commit", "-m", "Remove obsolete unit plans")
        planned = self.run_rename("plan")
        self.assert_success(planned)
        self.assertIn("2. Production headers: no unit plans", planned.stdout)
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(untracked.read_text(), original)
        self.assertFalse(self.plan_path("beta-unit").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_plan_skips_a_tracked_plan_missing_from_disk(self) -> None:
        self.plan_path("alpha-unit").unlink()
        planned = self.run_rename("plan")
        self.assert_success(planned)
        headers = [line for line in planned.stdout.splitlines() if line.startswith("2.")]
        self.assertEqual(len(headers), 1, planned.stdout)
        self.assertIn(str(self.plan_path("beta-unit")), headers[0])

    def test_plan_lists_every_production_move_without_writing(self) -> None:
        before = self.snapshot()
        result = self.run_rename("plan")
        self.assert_success(result)
        for path in (self.doc, self.new_doc, *(self.plan_path(unit) for unit in UNITS),
                     self.notifier / "showrunner-old", self.notifier / "showrunner-new",
                     *(self.state / directory / slug for directory in ("footers-off", "") for slug in (OLD, NEW)),
                     *(self.state / directory / f"{slug}.json"
                       for directory in ("review-paused", "outstanding") for slug in (OLD, NEW)),
                     self.state / "child/parent",
                     *(self.stall_path(slug, unit) for unit in UNITS for slug in (OLD, NEW))):
            self.assertIn(str(path), result.stdout)
        self.assertIn(f"{self.state}/*/parent", result.stdout)
        steps = [int(line.split(".", 1)[0]) for line in result.stdout.splitlines()]
        self.assertEqual(set(steps), set(range(1, 8)))
        self.assertEqual(steps, sorted(steps))
        self.assertIn("merge branch old -> new", result.stdout)
        self.assertIn("push origin new", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_apply_moves_state_and_commits_documents_on_the_new_merge_branch(self) -> None:
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()

    def test_apply_moves_pins_change_cells_child_parent_line_and_bevy_watch(self) -> None:
        listed = self.root / "lists/change-review - old.md"
        self.write(self.doc, self.doc.read_text().replace(
            "## Production Context\n", f"## Production Context\n\n- **Change review:** `{listed}`\n").replace(
            "- **Log:** `production.log`", "- **Log:** `docs/plans/old-log.md` — git-excluded"))
        # A production log is never committed and stands untracked, as in a checkout whose .gitignore re-includes docs/.
        self.write(self.checkout / "docs/plans/old-log.md", "one line per event\n")
        self.write(self.checkout / "docs/plans/old-log-recovered.md", "recovered lines\n")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Name the change review list")
        _ = self.git("push", "origin", OLD)
        rows = ["| # | Change | Merged | How to check | Production |", "| --- | --- | --- | --- | --- |",
                "| C1 | own | m | try | old |", "| C2 | child's | m | try | child |", "| C3 | root's | m | try |  |"]
        self.write(listed, "\n".join([
            "---", "date_created: 2026-10-27", "date_modified: 2026-10-27", "---", "",
            "# old showrunner: changes to review", "", "## To review", "", *rows, "",
            "## Reviewed", "", "| # | Change | Verdict | Date |", "| --- | --- | --- | --- |", ""]))
        pins = self.home / ".local/state/next"
        question = {"current": "old: first", "ended": {"old: earlier": "2026-10-27T01:00:00"}, "moved": ["oldish: kept"]}
        self.write(pins / "question-old.json", json.dumps(question))
        self.write(pins / "next-old-all-below.json", json.dumps({"current": "C1", "source": "change-old"}))
        child_doc = self.root / "child/docs/child-production.md"
        self.write(child_doc, f"# Child\n\n## Production Context\n\n- **Parent production:** {self.doc}\n")
        self.write(self.notifier / "showrunner-child/conf",
                   "EVERY=15\nTARGET=session:test-child\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(child_doc)]) + "\n")
        bevy = self.home / ".local/state/bevy-upgrade"
        self.write(bevy / "old.json", json.dumps({"slug": OLD, "production": str(self.doc), "entries": 3}))
        self.write(bevy / "old.lock", "")
        self.write(self.notifier / "bevy-upgrade-old/conf", "EVERY=240\nRUN=py bevy_upgrade.py --production "
                   + shlex.quote(str(self.doc)) + " check --scheduled\nFROM=bevy-upgrade-old\n")
        board = self.root / "state/unit-board/old.json"
        self.write(board, "{}\n")
        planned = self.run_rename("plan")
        self.assert_success(planned)
        for expected in ("/next pin", "change review", "in 1 rows", f"Parent production {child_doc}",
                         "bevy upgrade watch", "remove derived unit board", "production log"):
            self.assertIn(expected, planned.stdout)
        self.assert_success(self.run_rename("apply"))
        self.assertFalse((pins / "question-old.json").exists())
        self.assertEqual(json.loads((pins / "question-new.json").read_text()),
                         {"current": "new: first", "ended": {"new: earlier": "2026-10-27T01:00:00"},
                          "moved": ["oldish: kept"]})
        self.assertEqual(json.loads((pins / "next-new-all-below.json").read_text()),
                         {"current": "C1", "source": "change-old"})
        text = listed.read_text()
        self.assertIn("| C1 | own | m | try | new |", text)
        self.assertIn("| C2 | child's | m | try | child |", text)
        self.assertRegex(text, r"\| C3 \| root's \| m \| try \| *\|")
        self.assertIn(f"- **Parent production:** {self.new_doc}\n", child_doc.read_text())
        self.assertFalse((bevy / "old.json").exists())
        self.assertFalse((bevy / "old.lock").exists())
        self.assertEqual(json.loads((bevy / "new.json").read_text()),
                         {"slug": NEW, "production": str(self.new_doc), "entries": 3})
        self.assertFalse((self.notifier / "bevy-upgrade-old").exists())
        conf = (self.notifier / "bevy-upgrade-new/conf").read_text()
        self.assertIn(f"--production {shlex.quote(str(self.new_doc))} check", conf)
        self.assertIn("FROM=bevy-upgrade-new\n", conf)
        self.assertFalse(board.exists())
        self.assertFalse((self.checkout / "docs/plans/old-log.md").exists())
        self.assertEqual((self.checkout / "docs/plans/new-log.md").read_text(), "one line per event\n")
        self.assertIn("- **Log:** `docs/plans/new-log.md` — git-excluded", self.new_doc.read_text())
        self.assertEqual((self.checkout / "docs/plans/new-log-recovered.md").read_text(), "recovered lines\n")
        self.assertEqual(self.git("status", "--porcelain").splitlines(),
                         ["?? docs/plans/new-log-recovered.md", "?? docs/plans/new-log.md"])

    def test_timer_keeps_a_readable_document_through_doc_and_instance_moves(self) -> None:
        result = self.run_command("apply", OLD, NEW, wrapper=CHECK_TIMER_HANDOFF)
        self.assert_success(result)

    def test_apply_extends_existing_former_names_in_production_context(self) -> None:
        self.write(self.doc, self.doc.read_text().replace(
            "## Production Context\n", "## Production Context\n\n- **Formerly:** `ancient`, `earlier`\n"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record earlier production names")
        self.assert_success(self.run_rename("apply"))
        context = self.new_doc.read_text().split("## Production Context", 1)[1].split("\n## ", 1)[0]
        formerly = [line for line in context.splitlines() if line.startswith("- **Formerly:**")]
        self.assertEqual(len(formerly), 1, context)
        for slug in ("ancient", "earlier", OLD):
            self.assertIn(f"`{slug}`", formerly[0])

    def test_a_move_interrupted_before_index_update_commits_only_the_new_doc(self) -> None:
        failed = self.run_command("apply", OLD, NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.exists())
        self.assert_success(self.run_rename("apply"))
        production_docs = [path for path in self.git("ls-tree", "-r", "--name-only", NEW).splitlines()
                           if path.endswith("-production.md")]
        self.assertEqual(production_docs, ["docs/plans/new-production.md"])
        self.assert_applied()

    def test_a_retired_unit_has_no_plan_but_its_stall_state_moves(self) -> None:
        _ = fixture_member("unit", name="retired", parent=self.production_row().id,
                           worktree=staff.RemovedWorktree("natedev", self.worktrees / "retired"))
        self.write(self.stall_path(OLD, "retired-unit"), '{"stretch": 23}\n')
        before = self.snapshot()
        planned = self.run_rename("plan")
        self.assert_success(planned)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(str(self.stall_path(NEW, "retired-unit")), planned.stdout)
        self.assert_success(self.run_rename("apply"))
        self.assertFalse(self.stall_path(OLD, "retired-unit").exists())
        self.assertEqual(self.stall_path(NEW, "retired-unit").read_text(), '{"stretch": 23}\n')
        committed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", NEW).splitlines()
        self.assertEqual(set(committed), {"docs/plans/old-production.md", "docs/plans/new-production.md",
                                         *(f"docs/plans/{unit}.md" for unit in UNITS)})

    def test_check_rewrites_an_escaped_space_path_and_preserves_other_shell_text(self) -> None:
        directory = self.doc.parent / "my checkout"
        directory.mkdir()
        moved_doc = directory / self.doc.name
        _ = self.git("mv", str(self.doc), str(moved_doc))
        self.doc = moved_doc
        _ = staff.set_doc(self.production_row().id, staff.LiveDoc("natedev", moved_doc))
        self.new_doc = directory / "new-production.md"
        _ = self.git("commit", "-m", "Keep the production doc under a path with spaces")
        prefix = "CHECK=env 'NOTE=keep  spacing'  zsh /scripts/production_check.sh "
        suffix = " --literal 'other  argument'\n"
        escaped = str(self.doc).replace(" ", "\\ ")
        conf = self.notifier / "showrunner-old/conf"
        original = conf.read_text()
        updated = "".join(prefix + escaped + suffix if line.startswith("CHECK=") else line
                          for line in original.splitlines(keepends=True))
        self.write(conf, updated)
        self.assert_success(self.run_rename("apply"))
        expected = "".join(prefix + shlex.quote(str(self.new_doc)) + suffix if line.startswith("CHECK=") else line
                           for line in original.splitlines(keepends=True))
        self.assertEqual((self.notifier / "showrunner-new/conf").read_text(), expected)
        self.assertTrue(self.new_doc.exists())
        self.assertFalse(self.doc.exists())

    def test_formerly_is_inserted_without_splitting_the_context_list(self) -> None:
        self.assert_success(self.run_rename("apply"))
        context = self.new_doc.read_text().split("## Production Context", 1)[1].split("## Production rules", 1)[0]
        self.assertEqual(context, "\n\n- **Formerly:** `old`\n- **Merge branch:** `new`\n"
                         + f"- **Showrunner checkout:** `{self.checkout}`\n- **Log:** `production.log`\n\n")

    def test_apply_resumes_after_a_step_four_filesystem_failure(self) -> None:
        failed = self.run_rename("apply", fail_state=True)
        self.assertIn("simulated step 4 state move failure", failed.stdout + failed.stderr)
        self.assertTrue(self.new_doc.exists())
        self.assertTrue((self.notifier / "showrunner-new/conf").exists())
        self.assertFalse((self.notifier / "showrunner-old").exists())
        for unit in UNITS:
            self.assertRegex(self.plan_path(unit).read_text(),
                             r"(?m)^> \*\*Production:(?: new\*\*|\*\* new) — unit")
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()
        before = self.snapshot()
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.snapshot(), before)

    def test_invalid_and_reserved_destinations_refuse_without_writes(self) -> None:
        for destination in ("../escape", "bad.slug", "footers-off", "review-paused", "outstanding"):
            for action in ("plan", "apply"):
                with self.subTest(destination=destination, action=action):
                    before = self.snapshot()
                    result = self.run_rename(action, destination)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(destination, result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(), before)

    def test_current_and_former_timer_slugs_are_unavailable_as_destinations(self) -> None:
        taken = self.checkout / "docs/plans/taken-production.md"
        self.write(taken, "# Taken\n\n## Production Context\n\n- **Formerly:** `retired`, `older`\n")
        self.write(self.notifier / "showrunner-taken/conf",
                   "TARGET=session:taken\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(taken)]) + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Register another production")
        for destination in ("taken",):
            for action in ("plan", "apply"):
                with self.subTest(destination=destination, action=action):
                    before = self.snapshot()
                    result = self.run_rename(action, destination)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(destination, result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(), before)



if __name__ == "__main__":
    _ = unittest.main()
