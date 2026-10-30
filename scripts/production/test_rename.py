"""Production rename planning, state moves and apply recovery."""

from __future__ import annotations

import json
import os
import shlex
import unittest
from collections.abc import Callable
from typing import cast, final
from unittest import mock

from scripts.direct import workers
from scripts.production import rename_test_support as support
from scripts.production import staff
import rename
from unit_move import PathMove
from scripts.production.rename_test_support import CHECK_TIMER_HANDOFF, FAIL_GIT_MOVE_INDEX, FAIL_PLAN_REWRITE, NEW, OLD, UNITS


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_rename_finds_the_workspace_member_and_keeps_its_id(self) -> None:
        row = staff.snapshot().find(kind="showrunner")[0]
        _ = staff.rename(row.id, "tools")
        self.write(self.doc, self.doc.read_text().replace("## Production Context\n", "## Production Context\n\n- **Showrunner session:** `tools`\n"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record workspace name")
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "showrunner", "new", None))
        self.assertEqual(self.journal()["staff_id"], row.id)
        self.assertIn("- **Showrunner session:** `new`", self.new_doc.read_text())

    def test_taken_staff_name_refuses_before_sources_change(self) -> None:
        _ = staff.ensure("showrunner", name=NEW)
        before = self.snapshot()
        failed = self.run_rename("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertIn("StaffRefused", failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_staff_member_refuses_before_sources_change(self) -> None:
        row = staff.snapshot().find(kind="showrunner")[0]
        for unit in staff.snapshot().find(kind="unit", parent=row.id):
            _ = staff.retire(unit.id)
        _ = staff.retire(row.id)
        before = self.snapshot()
        failed = self.run_rename("apply")
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def interrupt_apply(self, count: int) -> rename.RenamePlan:
        save = cast(Callable[[rename.RenamePlan, tuple[str, ...]], None], getattr(rename, "_save"))
        stopped: list[rename.RenamePlan] = []

        def interrupted(plan: rename.RenamePlan, completed: tuple[str, ...]) -> None:
            save(plan, completed)
            if len(completed) == count:
                stopped.append(plan)
                raise OSError("fixture interrupted apply")

        with mock.patch.object(rename, "_save", side_effect=interrupted):
            result = self.run_rename("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture interrupted apply", result.stderr)
        self.assertEqual(self.completed_steps(), count)
        return stopped[0]

    def move_showrunner_checkout(self) -> None:
        old = self.checkout
        self.checkout = old.with_name("moved-checkout")
        _ = old.rename(self.checkout)
        self.doc = self.checkout / "docs/plans/old-production.md"
        self.new_doc = self.doc.with_name("new-production.md")
        _ = self.git("worktree", "repair")
        for path in (self.doc, self.new_doc):
            if path.exists():
                self.write(path, path.read_text().replace(str(old), str(self.checkout)))
        for conf in self.notifier.glob("showrunner-*/conf"):
            self.write(conf, conf.read_text().replace(str(old), str(self.checkout)))
        for unit in UNITS:
            self.assertEqual(self.git("-C", str(self.worktrees / unit), "rev-parse", "--git-common-dir"),
                             str(self.checkout / ".git"))
        self.assertFalse(old.exists())

    def assert_journal_has_no_current_locations(self) -> None:
        journal = self.journal()
        for key in ("checkout", "old_doc", "new_doc", "timer", "state", "stall", "review_pool"):
            self.assertNotIn(key, journal)
        self.assertEqual(journal["plans"], [f"docs/plans/{unit}.md" for unit in UNITS])
        self.assertNotIn(str(self.root), json.dumps(journal))

    def test_resume_before_doc_timer_steps_after_checkout_moves(self) -> None:
        _ = self.interrupt_apply(0)
        self.move_showrunner_checkout()
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()
        self.assert_journal_has_no_current_locations()

    def test_resume_after_doc_timer_steps_after_checkout_moves(self) -> None:
        _ = self.interrupt_apply(3)
        self.move_showrunner_checkout()
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()
        self.assert_journal_has_no_current_locations()
        self.assert_success(self.run_command("repair-workers", NEW, "--dry-run"))

    def test_old_format_journal_ignores_stale_locations_between_doc_and_timer_steps(self) -> None:
        plan = self.interrupt_apply(1)
        journal = self.journal()
        # The seven-step count is also an older supported journal representation.
        del journal["completed"]
        journal["step"] = 1

        def move_record(move: PathMove) -> dict[str, str]:
            return {"source": str(move.source), "destination": str(move.destination)}

        journal.update(checkout=str(self.checkout), old_doc=str(self.doc), new_doc=str(self.new_doc),
                       plans=[str(path) for path in plan.plans], timer=move_record(plan.timer),
                       state=[move_record(move) for move in plan.state],
                       stall=[move_record(move) for move in plan.stall], review_pool=move_record(plan.review_pool))
        self.write(self.state / "new/rename.json", json.dumps(journal))
        self.move_showrunner_checkout()
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()
        self.assert_journal_has_no_current_locations()

    def test_resume_refuses_missing_expected_timer_even_when_other_slug_resolves(self) -> None:
        _ = self.interrupt_apply(0)
        old = self.notifier / "showrunner-old"
        new = self.notifier / "showrunner-new"
        _ = old.rename(new)
        for other_exists in (True, False):
            with self.subTest(other_timer_exists=other_exists):
                if not other_exists:
                    _ = new.rename(self.notifier / "unregistered")
                result = self.run_rename("apply")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rename old -> new at _rename_doc: expected old's update timer", result.stderr)
                self.assertEqual(self.completed_steps(), 0)

    def test_resume_after_timer_step_refuses_old_timer_as_substitute(self) -> None:
        _ = self.interrupt_apply(3)
        _ = (self.notifier / "showrunner-new").rename(self.notifier / "showrunner-old")
        result = self.run_rename("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rename old -> new at _rename_showrunner_state: expected new's update timer", result.stderr)
        self.assertEqual(self.completed_steps(), 3)

    def test_unit_move_recovery_and_worker_repair_resolve_moved_showrunner_checkout(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        with mock.patch.object(rename, "_move_unit_worktree", side_effect=OSError("fixture unit move interrupted")):
            result = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture unit move interrupted", result.stderr)
        # These unit worktrees use the conventional names, unlike the apply fixtures.
        old = self.checkout
        self.checkout = old.with_name("moved-checkout")
        _ = old.rename(self.checkout)
        self.doc = self.checkout / "docs/plans/old-production.md"
        self.new_doc = self.doc.with_name("new-production.md")
        _ = self.git("worktree", "repair")
        self.write(self.new_doc, self.new_doc.read_text().replace(str(old), str(self.checkout)))
        conf = self.notifier / "showrunner-new/conf"
        self.write(conf, conf.read_text().replace(str(old), str(self.checkout)))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")
        self.assert_success(self.run_command("repair-workers", NEW))
        self.assertNotIn("new_doc", self.journal())

    def test_plan_ignores_as_built_paths_and_prose_in_plan_cells(self) -> None:
        text = self.doc.read_text()
        for unit, prose in zip(UNITS, (
            "docs/as-built/agent-registry.md and docs/as-built/session-notifier.md (runs done 2026-10-08)",
            "`docs/as-built/agent-registry.md` and `docs/as-built/session-notifier.md` (runs done 2026-10-08)",
        ), strict=True):
            text = text.replace(f"docs/plans/{unit}.md", prose)
        self.write(self.doc, text)
        before = self.snapshot()
        planned = self.run_rename("plan")
        self.assert_success(planned)
        self.assertIn("2. Production headers: no unit plans", planned.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_plan_selects_multiple_quoted_plans_and_apply_rewrites_both_headers(self) -> None:
        self.write(self.doc, self.doc.read_text().replace(
            "docs/plans/alpha-unit.md",
            "`docs/plans/alpha-unit.md` and `docs/plans/beta-unit.md` "
            + "(see `README.md` and `docs/as-built/history.md`; runs done 2026-10-08)"
        ).replace("| beta-unit | docs/plans/beta-unit.md |", "| beta-unit | permanent |"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record several plans in one cell")
        planned = self.run_rename("plan")
        self.assert_success(planned)
        headers = [line for line in planned.stdout.splitlines() if line.startswith("2.")]
        self.assertEqual(len(headers), 2, planned.stdout)
        for unit in UNITS:
            self.assertTrue(any(str(self.plan_path(unit)) in line for line in headers))
        self.assert_success(self.run_rename("apply"))
        for unit in UNITS:
            self.assertRegex(self.plan_path(unit).read_text(),
                             r"(?m)^> \*\*Production:(?: new\*\*|\*\* new) — unit")
        self.assertEqual(self.git("status", "--porcelain"), "")

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
        self.assertEqual(self.journal()["plans"], [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_plan_skips_a_tracked_plan_missing_from_disk(self) -> None:
        self.plan_path("alpha-unit").unlink()
        planned = self.run_rename("plan")
        self.assert_success(planned)
        headers = [line for line in planned.stdout.splitlines() if line.startswith("2.")]
        self.assertEqual(len(headers), 1, planned.stdout)
        self.assertIn(str(self.plan_path("beta-unit")), headers[0])

    def test_plan_accepts_absolute_and_home_relative_quoted_plan_paths(self) -> None:
        self.env["HOME"] = str(self.checkout)
        self.write(self.doc, self.doc.read_text().replace(
            "docs/plans/alpha-unit.md", f"`{self.plan_path('alpha-unit')}`"
        ).replace("| beta-unit | docs/plans/beta-unit.md |", "| beta-unit | `~/docs/plans/beta-unit.md` |"))
        planned = self.run_rename("plan")
        self.assert_success(planned)
        headers = [line for line in planned.stdout.splitlines() if line.startswith("2.")]
        self.assertEqual(len(headers), 2, planned.stdout)
        for unit in UNITS:
            self.assertTrue(any(str(self.plan_path(unit)) in line for line in headers))

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
                     *(self.stall_path(slug, unit) for unit in UNITS for slug in (OLD, NEW)),
                     self.pool / "old", self.pool / "new"):
            self.assertIn(str(path), result.stdout)
        self.assertIn(f"{self.state}/*/parent", result.stdout)
        steps = [int(line.split(".", 1)[0]) for line in result.stdout.splitlines()]
        self.assertEqual(set(steps), set(range(1, 9)))
        self.assertEqual(steps, sorted(steps))
        self.assertIn("merge branch old -> new", result.stdout)
        self.assertIn("push origin new", result.stdout)
        self.assertIn("keep old until finish", result.stdout)
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

    def test_a_former_source_name_resumes_before_the_timer_moves(self) -> None:
        self.write(self.doc, self.doc.read_text().replace(
            "## Production Context\n", "## Production Context\n\n- **Formerly:** `ancient`\n"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record the source production's earlier name")
        base = self.git("rev-parse", OLD)
        failed = self.run_command("apply", "ancient", NEW, wrapper=FAIL_PLAN_REWRITE)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated unit header rewrite failure", failed.stderr)
        self.assertEqual(self.completed_steps(), 1)
        self.assertTrue((self.notifier / "showrunner-old/conf").exists())
        self.assertFalse((self.notifier / "showrunner-new").exists())
        self.assert_success(self.run_rename("apply", source="ancient"))
        self.assertEqual(self.completed_steps(), 9)
        self.assertEqual(self.git("rev-parse", "new^"), base)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_move_interrupted_before_index_update_commits_only_the_new_doc(self) -> None:
        failed = self.run_command("apply", OLD, NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertEqual(self.completed_steps(), 0)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.exists())
        self.assertEqual(self.git("ls-files", "--", "docs/plans/old-production.md",
                                  "docs/plans/new-production.md"), "docs/plans/old-production.md")
        self.assert_success(self.run_rename("apply"))
        production_docs = [path for path in self.git("ls-tree", "-r", "--name-only", NEW).splitlines()
                           if path.endswith("-production.md")]
        self.assertEqual(production_docs, ["docs/plans/new-production.md"])
        self.assert_applied()

    def test_retired_plan_prose_is_skipped_but_its_stall_state_moves(self) -> None:
        retired = "| retired-unit | (retired; use `/unit:report` for history) | — | — |\n"
        self.write(self.doc, self.doc.read_text().replace(
            "\n## Production rules", retired + "\n## Production rules"))
        self.write(self.stall_path(OLD, "retired-unit"), '{"stretch": 23}\n')
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record a retired unit with a quoted command")
        before = self.snapshot()
        planned = self.run_rename("plan")
        self.assert_success(planned)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(str(self.stall_path(NEW, "retired-unit")), planned.stdout)
        self.assertNotIn("/unit:report", planned.stdout)
        self.assert_success(self.run_rename("apply"))
        self.assertIn(retired, self.new_doc.read_text())
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
        context = self.new_doc.read_text().split("## Production Context", 1)[1].split("## Units", 1)[0]
        self.assertEqual(context, "\n\n- **Formerly:** `old`\n- **Merge branch:** `new`\n"
                         + f"- **Showrunner checkout:** `{self.checkout}`\n- **Log:** `production.log`\n\n")

    def test_apply_resumes_after_a_step_four_filesystem_failure(self) -> None:
        failed = self.run_rename("apply", fail_state=True)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated step 4 state move failure", failed.stdout + failed.stderr)
        self.assertTrue(self.new_doc.exists())
        self.assertTrue((self.notifier / "showrunner-new/conf").exists())
        self.assertFalse((self.notifier / "showrunner-old").exists())
        self.assertTrue((self.pool / "old/pool.json").exists())
        for unit in UNITS:
            self.assertTrue(self.stall_path(OLD, unit).exists())
            self.assertRegex(self.plan_path(unit).read_text(),
                             r"(?m)^> \*\*Production:(?: new\*\*|\*\* new) — unit")
        self.assertEqual(self.completed_steps(), 3)
        self.assertIs(self.journal()["done"], False)
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
        for destination in ("taken", "retired", "older"):
            for action in ("plan", "apply"):
                with self.subTest(destination=destination, action=action):
                    before = self.snapshot()
                    result = self.run_rename(action, destination)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(destination, result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(), before)

    def test_apply_rerun_keeps_unit_moves_and_other_journal_keys(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        journal = self.journal()
        moves = journal["unit_moves"]
        journal["future_record"] = {"keep": [1, "two"]}
        journal["done"] = True
        completed = cast(list[str], journal["completed"])
        completed.remove("_rename_unit_workers")
        completed.append("_later_apply_step")
        self.write(self.state / "new/rename.json", json.dumps(journal))
        self.assert_success(self.run_rename("apply"))
        saved = self.journal()
        self.assertEqual(saved["unit_moves"], moves)
        self.assertEqual(saved["future_record"], {"keep": [1, "two"]})
        self.assertIs(saved["done"], True)
        self.assertIn("_later_apply_step", cast(list[str], saved["completed"]))

    def test_repair_without_unit_moves_finds_old_run_and_allows_adoption(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        run = self.seed_paused_unit_worker()
        journal = self.journal()
        del journal["unit_moves"]
        self.write(self.state / "new/rename.json", json.dumps(journal))
        before = self.snapshot()
        preview = self.run_command("repair-workers", NEW, "--dry-run")
        self.assert_success(preview)
        self.assertIn(f"would change run {run}", preview.stdout)
        self.assertIn(f"would change worker {run}/w7", preview.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assert_success(self.run_command("repair-workers", NEW))
        new = self.unit_worktree(NEW, "alpha-unit")
        destination = self.root / "runs/after-repair"
        self.write(destination / workers.STATE, json.dumps({"status": "active", "working_dir": str(new),
            "production": NEW, "unit": "alpha-unit", "run_id": destination.name}))
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(workers.adopt(run, destination, "w7"), "w7")
        before = self.snapshot()
        repeated = self.run_command("repair-workers", NEW)
        self.assert_success(repeated)
        self.assertIn("workers have nothing to change", repeated.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_repair_without_a_rename_journal_uses_the_timer_doc(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        run = self.seed_paused_unit_worker()
        (self.state / "new/rename.json").unlink()
        fixed = self.run_command("repair-workers", NEW)
        self.assert_success(fixed)
        self.assertIn(f"changed worker {run}/w7", fixed.stdout)
        state = cast(dict[str, object], json.loads((run / workers.STATE).read_text()))
        self.assertEqual(state["working_dir"], str(self.unit_worktree(NEW, "alpha-unit")))

    def test_repair_skips_a_run_that_matches_two_units(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        run = self.seed_paused_unit_worker()
        state = cast(dict[str, object], json.loads((run / workers.STATE).read_text()))
        state["plan_doc"] = ""
        self.write(run / workers.STATE, json.dumps(state))
        journal = self.journal()
        del journal["unit_moves"]
        self.write(self.state / "new/rename.json", json.dumps(journal))
        self.write(self.new_doc, self.new_doc.read_text().replace(
            str(self.unit_worktree(OLD, "beta-unit")), str(self.unit_worktree(NEW, "alpha-unit"))))
        before = self.snapshot()
        fixed = self.run_command("repair-workers", NEW)
        self.assert_success(fixed)
        self.assertIn(f"run {run}: ambiguous unit", fixed.stdout)
        self.assertIn("skipped", fixed.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_repair_reports_why_no_move_was_found(self) -> None:
        self.assert_success(self.run_rename("apply"))
        result = self.run_command("repair-workers", NEW)
        self.assert_success(result)
        self.assertIn("no moved unit was found: no unit run names an old worktree", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
