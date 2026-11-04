"""Seed real fixture sources without losing identity or another machine's staff."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

import io
import json
import os
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, staff_sources, unit_config, session_staff
from scripts.production.staff_test_support import SOURCES_SCRIPT, FixtureMachine, StaffTestCase, unit_membership
from scripts.production.unit_config_format_test_support import (
    check_format, count_taplo, remove_assignment_spacing, require_taplo, write_aligning_config,
)
from scripts.shutdown import pane_record, remote
import herdr
from scripts.production import showrunners
from scripts.production.session_staff import MemberSources
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
import showrunner_tree


class StaffSourcesTests(StaffTestCase):
    def unfilled_unit(self, slug: str, name: str) -> staff.StaffRow:
        row = unit_membership(slug, name)
        record = staff.snapshot()
        _ = self.write_document({"schema": 2, "rows": [
            replace(item, worktree=staff.WorktreeUnfilled()).to_json() if item.id == row.id else item.to_json()
            for item in record.rows]})
        return unit_membership(slug, name)

    @override
    def tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        owner, unit, worker = super().tree()
        owner = staff.set_doc(owner.id, staff.LiveDoc("natedev", self.source_root / "sdlc-production.md"))
        return owner, unit, worker

    def test_live_keeps_the_unit_and_reports_unreadable_branch_or_missing_configuration(self) -> None:
        fixture = self.row_production()
        _ = self.session_record(fixture.runner.current_session, fixture.runner.name)
        _ = self.session_record(fixture.unit.current_session, fixture.unit.name)
        before = self.staff_file.read_bytes()
        unavailable = fixture.worktree.with_name("unavailable-member")
        for failure in ("branch", "configuration"):
            with self.subTest(failure=failure):
                if failure == "branch":
                    _ = fixture.worktree.rename(unavailable)
                else:
                    _ = unavailable.rename(fixture.worktree)
                    (fixture.worktree / ".unit.toml").unlink()
                with redirect_stderr(io.StringIO()) as errors:
                    document = staff_sources.live()
                self.assertFalse(document["complete"])
                members = cast(list[dict[str, object]], document["members"])
                self.assertIn(fixture.unit.id, [member["id"] for member in members])
                lines = [line for line in errors.getvalue().splitlines() if fixture.unit.name in line]
                self.assertEqual(len(lines), 1, errors.getvalue())
                self.assertIn(str(fixture.worktree), lines[0])
                if failure == "configuration":
                    self.assertIn(".unit.toml", lines[0])
                self.assertEqual(self.staff_file.read_bytes(), before)

    def assert_fill_formats_once(self, path: Path) -> None:
        with count_taplo(self, self.root) as invocations:
            code, output, errors = self.fill_result()
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn("changed", output.splitlines())
            self.assertEqual(invocations.count(), 1)
            remove_assignment_spacing(path)
            code, output, errors = self.fill_result()
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn("changed", output.splitlines())
            self.assertEqual(invocations.count(), 2)
            before = path.read_bytes(), path.stat().st_mtime_ns
            code, output, errors = self.fill_result()
            self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
            self.assertEqual(invocations.count(), 3)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_live_fill_runs_taplo_once_for_publication_repair_and_rerun(self) -> None:
        _ = self.fill_production("sdlc", "format-unit")
        folder = self.source_root / "worktrees/format-unit"
        _ = write_aligning_config(folder)
        self.assert_fill_formats_once(folder / ".unit.toml")

    def test_fill_format_repair_recovers_before_and_after_replacement(self) -> None:
        _ = require_taplo(self)
        _ = self.fill_production("sdlc", "format-unit")
        folder = self.source_root / "worktrees/format-unit"
        _ = write_aligning_config(folder)
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        source = folder / ".unit.toml"
        expected = source.read_bytes()
        replace_file = os.replace
        for after in (False, True):
            with self.subTest(after=after):
                remove_assignment_spacing(source)

                def interrupted(temporary: Path, destination: Path) -> None:
                    if destination == source:
                        if after:
                            replace_file(temporary, destination)
                        raise OSError("interrupted configuration replacement")
                    replace_file(temporary, destination)

                with mock.patch.object(os, "replace", side_effect=interrupted):
                    code, output, errors = self.fill_result()
                self.assertEqual(code, 1, output + errors)
                self.assertIn("interrupted configuration replacement", errors)
                code, output, errors = self.fill_result()
                self.assertEqual((code, errors), (0, ""), output)
                self.assertIn("unchanged" if after else "changed", output.splitlines())
                self.assertEqual(source.read_bytes(), expected)
                before = source.read_bytes(), source.stat().st_mtime_ns
                code, output, errors = self.fill_result()
                self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
                self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)

    @override
    def setUp(self) -> None:
        super().setUp()
        # Keep the host's scratch policy, with a private root for this fixture.
        _ = self.enterContext(mock.patch.object(showrunners, "SCRATCH_ROOT", self.root / "scratch-checkout"))

    @override
    def fill_production(self, slug: str, *units: str) -> Path:
        doc = super().fill_production(slug, *units)
        write_text_atomic(doc, f"- **Showrunner checkout:** `{doc.parents[1]}`\n"
                          + "- **Merge branch:** `main`\n" + doc.read_text())
        return doc

    def test_fill_repairs_existing_config_format_and_second_rerun_preserves_files(self) -> None:
        binary = require_taplo(self)
        doc = self.fill_production("sdlc", "format-unit")
        folder = self.source_root / "worktrees/format-unit"
        _ = write_aligning_config(folder)
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        source = folder / ".unit.toml"
        value = unit_config.read(folder)
        remove_assignment_spacing(source)
        self.assertEqual(unit_config.read(folder), value)
        wrong_format = check_format(binary, source, folder)
        self.assertNotEqual(wrong_format.returncode, 0)
        before = source.read_bytes()
        paths = (self.staff_file, doc, folder / "taplo.toml", doc.parents[1] / ".git/info/exclude")
        retained = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertIn("changed", output.splitlines())
        self.assertNotIn("unchanged", output.splitlines())
        self.assertNotEqual(source.read_bytes(), before)
        self.assertEqual(unit_config.read(folder), value)
        result = check_format(binary, source, folder)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], retained)
        repaired = source.read_bytes(), source.stat().st_mtime_ns
        code, output, errors = self.fill_result()
        self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), repaired)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], retained)

    def test_fill_keeps_unfilled_staff_paths_despite_session_folder_copies(self) -> None:
        _ = self.fill_production("sdlc", "fallback-unit", "unavailable-unit")
        folder = self.source_root / "worktrees/fallback-unit"
        config = unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.NoAppPort(), ())
        _ = unit_config.write(folder, config)
        fallback = self.unfilled_unit("sdlc", "fallback-unit")
        session = self.session_record(fallback.id, "fallback-unit")
        value = cast(dict[str, object], json.loads(session.read_text()))
        value["cwd"] = str(folder)
        self.write_json(session, value)
        self.pane("w1:fallback", session_id=fallback.id, environment={"STAFF_ID": fallback.id})
        _ = self.fill_production("tools", "following-unit")
        unavailable = self.unfilled_unit("sdlc", "unavailable-unit")
        before = (folder / ".unit.toml").read_bytes()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            code, output, errors = self.fill_result()
        self.assertEqual(code, 1, output)
        self.assertEqual(reads.call_count, 1)
        self.assertIn(f"{unavailable.id} {unavailable.name}:", errors)
        self.assertIn("staff worktree is not present", errors)
        self.assertIn(f"{fallback.id} {fallback.name}:", errors)
        self.assertEqual(unit_membership("sdlc", "fallback-unit"), fallback)
        self.assertIsInstance(fallback.worktree, staff.WorktreeUnfilled)
        self.assertEqual((folder / ".unit.toml").read_bytes(), before)
        self.assertEqual(unit_config.read(folder), config)
        following = unit_membership("tools", "following-unit")
        self.assertIsInstance(following.worktree, staff.PresentWorktree)
        self.assertIsInstance(unit_config.read(self.source_root / "worktrees/following-unit"), unit_config.UnitConfig)

    def director_pane(self, pane_id: str, row: staff.StaffRow, folder: Path, environment: dict[str, str]) -> None:
        self.pane(pane_id, session_id=row.id, environment=environment)
        entries = pane_record.read("agents")
        entries[pane_id]["cwd"] = str(folder)
        pane_record.save("agents", entries)

    def test_fill_uses_the_staff_folder_when_registry_cwd_names_a_phase_worktree(self) -> None:
        document = self.fill_production("sdlc", "web-unit")
        row = unit_membership("sdlc", "web-unit")
        director = self.source_root / "worktrees/web-unit"
        phase = self.source_root / "worktrees/web-phase"
        _ = self.fixture_git(document.parents[1], "worktree", "add", "-b", "web-phase", str(phase))
        value = unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.AppPort(15733), ("director",))
        _ = unit_config.write(director, value)
        _ = unit_config.write(phase, replace(value, port=unit_config.AppPort(8999)))
        registry = self.session_record(row.current_session, row.name)
        details = cast(dict[str, object], json.loads(registry.read_text()))
        details["cwd"] = str(phase)
        self.write_json(registry, details)
        self.director_pane("w1:director", row, director, {"SHOWRUNNER_UNIT_ID": row.name})
        phase_bytes = (phase / ".unit.toml").read_bytes()
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.worktree, staff.PresentWorktree("natedev", director))
        self.assertEqual(unit_config.read(director, cleanup=False), value)
        self.assertEqual((phase / ".unit.toml").read_bytes(), phase_bytes)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        code, output, errors = self.fill_result()
        self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_fill_does_not_fill_a_staff_folder_from_registry_cwd(self) -> None:
        document = self.fill_production("sdlc", "web-unit")
        row = self.unfilled_unit("sdlc", "web-unit")
        phase = self.source_root / "worktrees/web-phase"
        _ = self.fixture_git(document.parents[1], "worktree", "add", "-b", "web-phase", str(phase))
        value = unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.AppPort(8999), ())
        _ = unit_config.write(phase, value)
        registry = self.session_record(row.current_session, row.name)
        details = cast(dict[str, object], json.loads(registry.read_text()))
        details["cwd"] = str(phase)
        self.write_json(registry, details)
        entries = pane_record.read("agents")
        pane_record.save("agents", {pane: entry for pane, entry in entries.items()
                                    if entry["session_id"] != row.current_session})
        phase_bytes = (phase / ".unit.toml").read_bytes()
        unit_file = self.source_root / "worktrees/web-unit/.unit.toml"
        unit_bytes = unit_file.read_bytes(), unit_file.stat().st_mtime_ns
        _, output, errors = self.fill_result()
        self.assertIn(row.name, output + errors)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertIsInstance(row.worktree, staff.WorktreeUnfilled)
        self.assertEqual((unit_file.read_bytes(), unit_file.stat().st_mtime_ns), unit_bytes)
        self.assertEqual((phase / ".unit.toml").read_bytes(), phase_bytes)

    def test_fill_requires_checkout_and_merge_branch_evidence_before_using_a_timer_document(self) -> None:
        for slug, field in (("no-checkout", "Showrunner checkout"), ("no-branch", "Merge branch"),
                            ("feature", "branch")):
            with self.subTest(slug=slug):
                doc = self.fill_production(slug, f"{slug}-unit")
                _ = self.unfilled_unit(slug, f"{slug}-unit")
                if slug == "feature":
                    _ = self.fixture_git(doc.parents[1], "checkout", "-b", "feature")
                else:
                    write_text_atomic(doc, "\n".join(line for line in doc.read_text().splitlines()
                                                    if not line.startswith(f"- **{field}:**")) + "\n")
                owner = next(row for row in staff.find(kind="showrunner") if row.name == slug)
                unit_file = self.source_root / f"worktrees/{slug}-unit/.unit.toml"
                unit_bytes = unit_file.read_bytes(), unit_file.stat().st_mtime_ns
                code, output, errors = self.fill_result()
                self.assertEqual(code, 1, output)
                self.assertIn(f"{owner.id} {owner.name}:", errors)
                self.assertIn(field, errors)
                current = staff.by_id(owner.id)
                assert current is not None
                self.assertIsInstance(current.doc, staff.DocUnfilled)
                self.assertIsInstance(unit_membership(slug, f"{slug}-unit").worktree, staff.WorktreeUnfilled)
                self.assertEqual((unit_file.read_bytes(), unit_file.stat().st_mtime_ns), unit_bytes)

    def test_fill_keeps_a_saved_doc_but_never_uses_it_to_fill_children_without_a_timer(self) -> None:
        doc = self.fill_production("sdlc", "stale-unit")
        _ = self.unfilled_unit("sdlc", "stale-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        saved = staff.LiveDoc(remote.local_machine(), doc)
        _ = staff.set_doc(owner.id, saved)
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc/conf"
        timer.unlink()
        timer.parent.rmdir()
        _ = self.session_record(owner.id, "sdlc")
        self.pane("w1:showrunner", session_id=owner.id, environment={"STAFF_ID": owner.id})
        unit_file = self.source_root / "worktrees/stale-unit/.unit.toml"
        unit_bytes = unit_file.read_bytes(), unit_file.stat().st_mtime_ns
        code, output, errors = self.fill_result()
        child = unit_membership("sdlc", "stale-unit")
        self.assertEqual(code, 1, output)
        self.assertIn(f"{child.id} {child.name}:", errors)
        self.assertIn("the staff worktree is not present", errors)
        self.assertNotIn(f"{owner.id} {owner.name}:", errors)
        current = staff.by_id(owner.id)
        assert current is not None
        self.assertEqual(current.doc, saved)
        self.assertIsInstance(child.worktree, staff.WorktreeUnfilled)
        self.assertEqual((unit_file.read_bytes(), unit_file.stat().st_mtime_ns), unit_bytes)

    def test_fill_reports_an_unreadable_timer_document_even_without_a_live_session(self) -> None:
        for slug in ("missing", "undecodable"):
            with self.subTest(slug=slug):
                doc = self.fill_production(slug)
                owner = next(row for row in staff.find(kind="showrunner") if row.name == slug)
                if slug == "missing":
                    doc.unlink()
                else:
                    _ = doc.write_bytes(b"\xff")
                code, output, errors = self.fill_result()
                self.assertEqual(code, 1, output)
                self.assertIn(f"{owner.id} {owner.name}:", errors)
                self.assertIn(str(doc), errors)
                self.assertIn("production doc could not be read", errors)
                current = staff.by_id(owner.id)
                assert current is not None
                self.assertIsInstance(current.doc, staff.DocUnfilled)

    def test_fill_reports_the_nested_timer_owner_when_its_document_is_unreadable(self) -> None:
        doc = self.fill_production("builds")
        root = fixture_member("showrunner", name="natedev")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "builds")
        owner = staff.move(owner.id, staff.ReportsTo(root.id))
        other_root = fixture_member("showrunner", name="other")
        other = fixture_member("showrunner", name="other-builds", parent=other_root.id)
        # Timer names can predate the current staff name. Its retained target
        # launch, rather than the timer slug or the unreadable doc, binds it.
        old_timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-builds"
        (old_timer / "conf").unlink()
        old_timer.rmdir()
        self.register_production("legacy-builds", doc)
        self.pane("w1:builds", session_id=owner.id, environment={"STAFF_ID": owner.id})
        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    doc.unlink()
                else:
                    _ = doc.write_bytes(b"\xff")
                before = self.staff_file.read_bytes()
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    code, output, errors = self.fill_result()
                self.assertEqual(code, 1, output)
                self.assertEqual(reads.call_count, 1)
                self.assertIn(f"{owner.id} {owner.name}:", errors)
                self.assertIn(str(doc), errors)
                self.assertIn("production doc could not be read", errors)
                self.assertNotIn(f"{other.id} {other.name}:", errors)
                self.assertNotIn("unchanged", output)
                self.assertEqual(self.staff_file.read_bytes(), before)
                current = staff.by_id(owner.id)
                assert current is not None
                self.assertIsInstance(current.doc, staff.DocUnfilled)

    def fill_result(self, *, dry_run: bool = False) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = staff_sources.fill(dry_run=dry_run)
        return code, output.getvalue(), errors.getvalue()

    def test_live_staff_roots_a_surviving_style_child_and_keeps_its_unit_after_parent_wrap(self) -> None:
        parent_doc = self.production("parent")
        style_doc = self.production("style", "names-unit", parent="parent")
        write_text_atomic(style_doc, "- **Kind:** style\n" + style_doc.read_text())
        self.pane("w1:style", session_id="ffa0f240-6489-5d0c-a907-083d633c124a", environment={})
        self.pane("w1:names", session_id="7f89b939-c570-5fd3-9802-e62dfb8aeea6", environment={
            "SHOWRUNNER_UNIT": "style", "SHOWRUNNER_UNIT_ID": "names-unit"})
        _ = self.session_record("ffa0f240-6489-5d0c-a907-083d633c124a", "style")
        _ = self.session_record("7f89b939-c570-5fd3-9802-e62dfb8aeea6", "names-unit")
        _ = self.seed()
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "parent")
        child = next(row for row in staff.find(kind="showrunner") if row.name == "style")
        unit = unit_membership("style", "names-unit")
        _ = staff.set_doc(parent.id, staff.WrappedDoc("natedev", parent_doc))
        before = self.staff_file.read_bytes()
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        by_id = {entry["id"]: entry for entry in members}
        self.assertNotIn(parent.id, by_id)
        self.assertIn(child.id, by_id)
        self.assertIsNone(by_id[child.id]["parent"])
        self.assertEqual(by_id[unit.id]["parent"], child.id)
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_fill_reports_a_branch_tip_document_without_a_live_file_instead_of_filling_it(self) -> None:
        doc = self.fill_production("sdlc")
        repository = doc.parents[1]
        missing = doc.parent / "missing-production.md"
        write_text_atomic(missing, doc.read_text().replace("`sdlc`", "`missing`"))
        owner = fixture_member("showrunner", name="missing")
        _ = self.fixture_git(repository, "add", str(missing))
        _ = self.fixture_git(repository, "commit", "-m", "document at branch tip")
        missing.unlink()
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertNotIn(str(missing), output)
        current = staff.by_id(owner.id)
        assert current is not None
        self.assertIsInstance(current.doc, staff.DocUnfilled)
        self.assertFalse(missing.exists())

    def test_fill_refuses_two_document_locations_for_one_showrunner_without_writing_it(self) -> None:
        doc = self.fill_production("sdlc")
        other = doc.parent / "elsewhere" / doc.name
        other.parent.mkdir()
        write_text_atomic(other, doc.read_text())
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertIn(str(doc), output)
        self.assertNotIn(str(other), output)
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        self.assertEqual(owner.doc, staff.LiveDoc(remote.local_machine(), doc))

    def test_fill_preserves_conflicting_locations_accepts_equal_ones_and_fills_unfilled_fields(self) -> None:
        _ = self.fill_production("sdlc")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        old_doc = staff.LiveDoc("mac", self.root / "other-checkout/docs/sdlc-production.md")
        _ = staff.set_doc(owner.id, old_doc)
        other = self.fill_production("tools", "conflict-unit", "equal-unit", "unfilled-unit")
        tools = next(row for row in staff.find(kind="showrunner") if row.name == "tools")
        same_doc = staff.LiveDoc(remote.local_machine(), other)
        _ = staff.set_doc(tools.id, same_doc)
        conflict = unit_membership("tools", "conflict-unit")
        prior_worktree = staff.PresentWorktree("mac", self.root / "existing-unit")
        _ = staff.set_worktree(conflict.id, prior_worktree, expected=conflict.worktree)
        equal = unit_membership("tools", "equal-unit")
        equal_worktree = staff.PresentWorktree(remote.local_machine(), self.source_root / "worktrees/equal-unit")
        _ = staff.set_worktree(equal.id, equal_worktree, expected=equal.worktree)
        conflict_file = self.source_root / "worktrees/conflict-unit/.unit.toml"
        conflict_bytes = conflict_file.read_bytes(), conflict_file.stat().st_mtime_ns
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        rows = staff.snapshot()
        for identity, expected in ((owner.id, old_doc), (tools.id, same_doc)):
            current = rows.by_id(identity)
            assert current is not None
            self.assertEqual(current.doc, expected)
        for identity, expected_worktree in ((conflict.id, prior_worktree), (equal.id, equal_worktree)):
            current = rows.by_id(identity)
            assert current is not None
            self.assertEqual(current.worktree, expected_worktree)
        self.assertEqual((conflict_file.read_bytes(), conflict_file.stat().st_mtime_ns), conflict_bytes)
        for name in ("equal-unit", "unfilled-unit"):
            row = unit_membership("tools", name)
            self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), self.source_root / "worktrees" / name))
            self.assertIsInstance(unit_config.read(self.source_root / "worktrees" / name), unit_config.UnitConfig)

    def test_fill_dry_run_keeps_abandoned_configuration_temporary_files(self) -> None:
        _ = self.fill_production("sdlc", "models-unit")
        folder = self.source_root / "worktrees/models-unit"
        dead = folder / "..unit.toml.999999999.abandoned.tmp"
        write_text_atomic(dead, 'plan = "unfinished')
        before = dead.read_bytes(), dead.stat().st_mtime_ns, self.staff_file.read_bytes(), (folder / ".unit.toml").read_bytes()
        code, _, errors = self.fill_result(dry_run=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(dead.exists())
        self.assertEqual((dead.read_bytes(), dead.stat().st_mtime_ns, self.staff_file.read_bytes(),
                          (folder / ".unit.toml").read_bytes()), before)

    def test_fill_dry_run_lists_sources_without_changing_staff_excludes_or_config(self) -> None:
        doc = self.fill_production("sdlc", "models-unit")
        exclude = doc.parents[1] / ".git/info/exclude"
        config = self.source_root / "worktrees/models-unit/.unit.toml"
        before = self.staff_file.read_bytes(), exclude.read_bytes(), self.staff_file.stat().st_mtime_ns, config.read_bytes()
        code, output, errors = self.fill_result(dry_run=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(str(doc), output)
        self.assertIn(str(self.source_root / "worktrees/models-unit"), output)
        self.assertEqual((self.staff_file.read_bytes(), exclude.read_bytes(), self.staff_file.stat().st_mtime_ns,
                          config.read_bytes()), before)

    def test_fill_uses_the_pane_staff_id_after_a_unit_rename(self) -> None:
        _ = self.fill_production("sdlc", "models-unit")
        original = unit_membership("sdlc", "models-unit")
        renamed = staff.rename(original.id, "registry")
        other = fixture_member("showrunner", name="other")
        same_name = fixture_member("unit", name="models", parent=other.id)
        _ = self.session_record(renamed.current_session, renamed.name)
        self.director_pane("w1:models", original, self.source_root / "worktrees/models-unit", {
            "STAFF_ID": original.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        selected = staff.by_id(original.id)
        assert selected is not None
        self.assertEqual(selected.name, renamed.name)
        self.assertEqual(selected.worktree, staff.PresentWorktree(
            remote.local_machine(), self.source_root / "worktrees/models-unit"))
        other_row = staff.by_id(same_name.id)
        assert other_row is not None
        self.assertIsInstance(other_row.worktree, staff.WorktreeUnfilled)

    def test_fill_reads_branch_tip_and_ignored_production_docs_and_skips_wrapped_ones(self) -> None:
        doc = self.fill_production("sdlc", "models-unit")
        repository = doc.parents[1]
        archived = doc.parent / "branch-only-production.md"
        write_text_atomic(archived, "# Branch only\n- **Showrunner session:** `branch-only`\n")
        _ = fixture_member("showrunner", name="branch-only")
        _ = self.fixture_git(repository, "add", str(archived))
        _ = self.fixture_git(repository, "commit", "-m", "production at branch tip")
        ignored = doc.parent / "style-production.md"
        write_text_atomic(repository / ".git/info/exclude", "/docs/style-production.md\n")
        write_text_atomic(ignored, doc.read_text().replace("`sdlc`", "`style`"))
        _ = fixture_member("showrunner", name="style")
        wrapped = doc.parent / "wrapped-production.md"
        write_text_atomic(wrapped, "- **Status:** wrapped\n" + doc.read_text().replace("`sdlc`", "`wrapped`"))
        old = fixture_member("showrunner", name="wrapped")
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertNotIn("refs/heads/main", output)
        self.assertNotIn(str(archived), output)
        self.assertNotIn(str(ignored), output)
        self.assertNotIn(str(wrapped), output)
        live_owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        self.assertEqual(live_owner.doc, staff.LiveDoc(remote.local_machine(), doc))
        style_owner = next(row for row in staff.find(kind="showrunner") if row.name == "style")
        self.assertIsInstance(style_owner.doc, staff.DocUnfilled)
        self.assertEqual(self.fixture_git(repository, "ls-files", "--", str(ignored)), "")
        branch_row = next(row for row in staff.find(kind="showrunner") if row.name == "branch-only")
        self.assertIsInstance(branch_row.doc, staff.DocUnfilled)
        old_row = staff.by_id(old.id)
        assert old_row is not None
        self.assertIsInstance(old_row.doc, staff.DocUnfilled)

    def test_fill_refuses_tracked_and_negated_config_before_mutating_the_unit(self) -> None:
        _ = self.fill_production("sdlc", "tracked-unit", "negated-unit")
        tracked, negated = (self.source_root / "worktrees" / name for name in ("tracked-unit", "negated-unit"))
        write_text_atomic(tracked / ".unit.toml", 'plan = "standby"\nresponsibilities = []\n')
        _ = self.fixture_git(tracked, "add", ".unit.toml")
        _ = self.fixture_git(tracked, "commit", "-m", "tracked configuration")
        write_text_atomic(negated / ".gitignore", "!.unit.toml\n")
        before = {folder: (folder / ".unit.toml").read_bytes() for folder in (tracked, negated)}
        members = [unit_membership("sdlc", name) for name in ("tracked-unit", "negated-unit")]
        code, _, errors = self.fill_result()
        self.assertEqual(code, 1)
        self.assertIn("tracked", errors)
        self.assertIn("ignored", errors)
        for member in members:
            self.assertEqual(staff.by_id(member.id), member)
        for folder, content in before.items():
            self.assertEqual((folder / ".unit.toml").read_bytes(), content)

    def test_live_inputs_keep_rejected_production_documents_and_read_them_after_correction(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        rejected = self.production("recovering", "later-unit")
        _ = self.session_record("8e2a7bad-d715-5c2d-9ec5-2a8bf9a81289", "recovering")
        _ = self.seed()
        owner = staff.by_path("recovering")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", rejected))
        original = rejected.read_bytes()
        _ = rejected.write_bytes(b"\xff invalid production\n")
        with redirect_stderr(io.StringIO()) as problems:
            value = staff_sources.live()
        self.assertIn("production doc could not be read", problems.getvalue())
        self.assertIn(str(rejected), cast(list[str], value["inputs"]))
        self.assertNotIn("recovering", [member["name"] for member in cast(list[dict[str, object]], value["members"])])
        _ = rejected.write_bytes(original)
        with redirect_stderr(io.StringIO()) as problems:
            recovered = staff_sources.live()
        self.assertNotIn(str(rejected), problems.getvalue())
        self.assertIn(str(rejected), cast(list[str], recovered["inputs"]))
        members = cast(list[dict[str, object]], recovered["members"])
        self.assertEqual(next(member["identity"] for member in members if member["name"] == "recovering"),
                         {"claude": "8e2a7bad-d715-5c2d-9ec5-2a8bf9a81289"})

    def test_live_inputs_include_present_and_missing_shared_session_configuration(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.seed()
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "herdr/shared-session"
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    config.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(config, "agents\n")
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(config), inputs)
                self.assert_no_system_inputs(inputs)


    def test_live_owner_ignores_a_newer_launch_whose_session_has_ended(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.seed()
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record(unit.id, "models-unit")
        self.pane("w1:running", session_id=unit.id, environment={"STAFF_ID": unit.id},
                  written_at="2026-10-30T09:00:00+00:00")
        self.pane("w1:ended", session_id="3790170d-f8c8-5dbd-ba9b-38eb5179177f", environment={"STAFF_ID": unit.id},
                  written_at="2026-10-31T09:00:00+00:00")
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        member = next(member for member in members if member["id"] == unit.id)
        self.assertEqual(member["identity"], {"claude": unit.id})

    def test_live_inputs_include_the_remote_owner_record_and_exclude_process_and_system_reads(self) -> None:
        owner = self.tree()[0]
        for machine in ("natedev", "mac"):
            _ = staff.set_doc(owner.id, staff.LiveDoc(
                machine, self.root / f"{machine}-sources" / "sdlc-production.md"))
            with self.subTest(machine=machine), self.machine(machine):
                if machine == "mac":
                    self.assertNotIn("STAFF_FILE", os.environ)
                _ = self.production("sdlc", "models-unit")
                _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
                remote_reads = len(self.ssh_commands()) if machine == "mac" else 0
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                if machine == "mac":
                    self.assertEqual(len(self.ssh_commands()) - remote_reads, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(self.staff_file), inputs)
                self.assert_no_system_inputs(inputs)

    def seed(self, *, dry_run: bool = False) -> tuple[str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            staff_sources.seed(dry_run=dry_run)
        return output.getvalue(), errors.getvalue()

    def assert_no_system_inputs(self, inputs: list[str]) -> None:
        # The test's own temp root may sit under /dev/shm (the gate sets TMPDIR there).
        root = f"{self.root}/"
        for path in inputs:
            if path.startswith(root):
                continue
            self.assertFalse(path.startswith(("/proc/", "/dev/", "/usr/share/zoneinfo/")), path)

    def test_codex_scan_continues_after_unparsable_process_arguments(self) -> None:
        from scripts.message import process_table

        doc = self.fill_production("scan-demo")
        owner = staff.by_path("scan-demo")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        table = {
            42401: process_table.Process(42401, 1, "codex", 'codex --config "unfinished'),
            42402: process_table.Process(42402, 1, "codex", "codex --model gpt-6"),
        }
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": owner.current_session}) as environment,
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        environment.assert_called_once_with(42402)
        members = cast(list[dict[str, object]], document["members"])
        found = next(member for member in members if member["id"] == owner.id)
        self.assertEqual(found["identity"], {"codex": owner.current_session})
        self.assertFalse(document["complete"])
        self.assertEqual(errors.getvalue().splitlines(), [
            "staff sources: Codex process scan: pid 42401: No closing quotation"])

    def test_missing_codex_thread_reports_one_codex_problem(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("pane-demo", "models-unit")
        unit = staff.by_path("pane-demo/models")
        assert unit is not None
        launch = pane_record.launch("test:pane", unit.id, str(self.root), {}, [])
        _ = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("pane-unit-current")))
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        record = staff.snapshot()
        sources = MemberSources({}, (launch,))
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
            evidence = staff_sources.observe_workers(record, sources)
        expected = "staff sources: Codex process 42401: native thread identity is unavailable"
        self.assertEqual(evidence.diagnostics, (expected,))
        self.assertNotIn("Claude", "\n".join(evidence.diagnostics))

    def test_codex_without_a_thread_in_an_unowned_pane_reports_a_diagnostic_without_refusal(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("other-pane-demo", "models-unit")
        unit = staff.by_path("other-pane-demo/models")
        assert unit is not None
        held = pane_record.launch("other:pane", unit.id, str(self.root), {}, [])
        unowned = pane_record.launch("test:pane", fixture_runtime_id("unowned-pane"), str(self.root), {}, [])
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        for launches in ((held,), (held, unowned)):
            with (self.subTest(recorded=unowned in launches),
                  mock.patch.object(process_table, "snapshot", return_value=table),
                  mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
                evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources({}, launches))
            self.assertEqual(evidence.diagnostics, (
                "staff sources: Codex process 42401: pane holds no staff member; not staff",))

    def test_missing_codex_thread_in_a_pane_with_staff_unit_labels_keeps_the_scan_incomplete(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("label-pane-demo", "models-unit")
        launch = pane_record.launch("test:pane", "", str(self.root), {
            "SHOWRUNNER_UNIT": "label-pane-demo", "SHOWRUNNER_UNIT_ID": "models-unit"}, [])
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
            evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources({}, (launch,)))
        self.assertEqual(evidence.diagnostics, (
            "staff sources: Codex process 42401: native thread identity is unavailable",))

    def test_unparsable_claude_settings_do_not_make_the_codex_scan_incomplete(self) -> None:
        from scripts.message import process_table

        doc = self.fill_production("claude-scan-demo")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "claude-scan-demo")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        table = {
            42401: process_table.Process(42401, 1, "python3",
                                        'relaunch.py run -- claude --resume session --settings {"name":"unfinished}'),
            42402: process_table.Process(42402, 1, "claude",
                                        'claude --resume session --settings {"name":"unfinished}'),
        }
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment") as environment,
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        environment.assert_not_called()
        self.assertTrue(document["complete"])
        self.assertEqual(errors.getvalue(), "")

    def test_codex_without_a_thread_or_pane_reports_a_diagnostic_without_refusal(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(42401, 1, "codex", "/local/bin/codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={})):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources({}, ()))
        self.assertEqual(evidence.diagnostics, (
            "staff sources: Codex process 42401: not in a herdr pane; not staff",))

    def test_unparsable_node_codex_arguments_keep_the_scan_incomplete(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(
            42401, 1, "node", 'node /modules/@openai/codex/bin/codex.js --config "unfinished')}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment") as environment):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources({}, ()))
        environment.assert_not_called()
        self.assertEqual(evidence.diagnostics,
                         ("staff sources: Codex process scan: pid 42401: No closing quotation",))

    def test_failed_codex_process_snapshot_does_not_report_a_claude_registry_failure(self) -> None:
        from scripts.message import process_table

        with mock.patch.object(process_table, "snapshot", side_effect=OSError("process table unavailable")):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources({}, ()))
        self.assertEqual(evidence.diagnostics, ("staff sources: Codex processes: process table unavailable",))
        self.assertNotIn("Claude", "\n".join(evidence.diagnostics))

    def test_live_staff_includes_codex_showrunner_and_unit_by_current_thread_without_role_inference(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("codex-demo", "models-unit")
        owner = staff.by_path("codex-demo")
        unit = staff.by_path("codex-demo/models")
        assert owner is not None and unit is not None
        owner_current = staff.native_id(fixture_runtime_id("codex-showrunner-current"))
        unit_current = staff.native_id(fixture_runtime_id("codex-unit-current"))
        _ = staff.update_current_session(owner.current_session, owner_current)
        _ = staff.update_current_session(unit.current_session, unit_current)
        table = {
            42401: process_table.Process(42401, 1, "codex", "codex --model gpt-6"),
            42402: process_table.Process(42402, 1, "codex", "codex --config model=gpt-6"),
            42403: process_table.Process(42403, 1, "codex", "codex --model gpt-6 app-server"),
        }
        environments = {42401: {"CODEX_THREAD_ID": owner_current},
                        42402: {"CODEX_THREAD_ID": unit_current},
                        42403: {"CODEX_THREAD_ID": fixture_runtime_id("app-server-host-only")}}
        before = self.staff_file.read_bytes()
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", side_effect=environments.__getitem__),
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        members = cast(list[dict[str, object]], document["members"])
        by_id = {member["id"]: member for member in members}
        self.assertEqual(by_id[owner.id]["identity"], {"codex": owner_current})
        self.assertEqual(by_id[unit.id]["identity"], {"codex": unit_current})
        self.assertEqual((by_id[owner.id]["kind"], by_id[unit.id]["kind"]), ("showrunner", "unit"))
        self.assertEqual(by_id[unit.id]["parent"], owner.id)
        self.assertNotIn(environments[42403]["CODEX_THREAD_ID"], errors.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_preserves_recorded_moves_despite_stale_sources(self) -> None:
        old = fixture_member("showrunner", name="old")
        new = fixture_member("showrunner", name="new")
        child = fixture_member("showrunner", name="tools", parent=old.id)
        unit = fixture_member("unit", name="models", parent=old.id)
        worker = fixture_member("worker", name="impl1", parent=unit.id)
        old_doc = self.production("old", "models-unit")
        new_doc = self.production("new")
        _ = staff.set_doc(old.id, staff.LiveDoc("natedev", old_doc))
        _ = staff.set_doc(new.id, staff.LiveDoc("natedev", new_doc))
        child_doc = self.production("child", workspace="tools", parent="old")
        _ = staff.set_doc(child.id, staff.LiveDoc("natedev", child_doc))
        moved_unit = staff.move(unit.id, staff.ReportsTo(new.id))
        moved_child = staff.move(child.id, staff.ReportsTo(new.id))
        before = self.staff_file.read_bytes()
        output, errors = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn("unchanged", errors)
        self.assertNotIn("add ", output)
        self.assertEqual(staff.by_id(unit.id), moved_unit)
        self.assertEqual(staff.by_id(child.id), moved_child)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(showrunner_tree.parent_of("child"), "new")
        self.assertEqual(showrunner_tree.children_of("new"), ["child"])
        self.assertEqual(showrunner_tree.children_of("old"), [])

    def test_reading_a_snapshot_never_collects_seed_sources(self) -> None:
        expected = self.tree()
        with mock.patch.object(staff_sources, "_collect", side_effect=AssertionError("read ran seed")):
            record = staff.snapshot()
            self.assertCountEqual(record.rows, expected)

    def test_seed_rerun_uses_timers_parent_files_docs_and_pane_records(self) -> None:
        _ = self.production("parent", workspace="umbrella")
        _ = self.production("sdlc", "models-unit", workspace="tools", parent="parent")
        self.pane("w1:p1", session_id="1d7d742d-e609-510b-bc96-dc9075f7b72b", environment={})
        self.pane("w1:p2", session_id="86591fc4-4d3f-59f2-8ad1-7e155ba0787e", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        first = self.script(SOURCES_SCRIPT, "seed")
        before = self.staff_file.read_bytes()
        second = self.script(SOURCES_SCRIPT, "seed")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn("unchanged", second.stderr)
        self.assertIn("tools", first.stdout)
        root = staff.by_path("umbrella")
        child = staff.by_path("tools")
        unit = staff.by_path("tools/models")
        assert root is not None and child is not None and unit is not None
        self.assertEqual(child.parent, staff.StaffRoot())
        self.assertEqual(unit.parent, staff.ReportsTo(child.id))
        self.assertEqual(len(staff.find()), 3)
        self.assertEqual(staff.find(kind="worker"), [])

    def test_seed_keeps_renamed_unit_and_showrunner_by_pane_staff_ids(self) -> None:
        showrunner, unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        renamed_unit = staff.rename(unit.id, "panes")
        renamed_showrunner = staff.rename(showrunner.id, "control")
        self.pane("w1:p1", session_id=showrunner.id, environment={"STAFF_ID": showrunner.id})
        self.pane("w1:p2", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        _ = self.seed()
        before = self.staff_file.read_bytes()
        _output, errors = self.seed()
        self.assertIn("unchanged", errors)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.find(kind="showrunner"), [renamed_showrunner])
        self.assertEqual(staff.find(kind="unit"), [renamed_unit])
        self.assertEqual(staff.by_path("control/panes/impl1"), worker)
        self.assertEqual(len(staff.find()), 3)

    def test_persisted_member_reference_survives_two_clears_rename_and_name_reuse(self) -> None:
        parent, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        launch = pane_record.read_strict("agents")["fixture:sdlc/models-unit"]
        pane_record.write("agents", launch)
        second = staff.StaffId(fixture_runtime_id("member-clear-B"))
        third = staff.StaffId(fixture_runtime_id("member-clear-C"))
        _ = staff.update_current_session(unit.current_session, second)
        current = staff.update_current_session(second, third)
        current = staff.rename(current.id, "renamed")
        replacement = fixture_member("unit", name="models", parent=parent.id,
                                     runtime_id=fixture_runtime_id("replacement-models"))
        path = pane_record.record_path("agents")
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for reference in (unit.id, third):
            with self.subTest(reference=reference):
                # The first case is the writer's retained A; the second models a
                # retained observation already carrying the live conversation C.
                if reference == third:
                    launch["session_id"] = reference
                    pane_record.save("agents", {launch["pane_id"]: launch})
                before = path.read_bytes(), path.stat().st_mtime_ns
                record = staff.snapshot()
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
                    found = staff_sources.member(selector, record)
                self.assertEqual(found, staff_sources.MemberFound(current))
                self.assertNotEqual(current.id, replacement.id)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_persisted_member_continues_past_a_cleared_launch_under_another_parent(self) -> None:
        parent, moved, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        other = fixture_member("showrunner", name="elsewhere")
        moved = staff.move(moved.id, staff.ReportsTo(other.id))
        moved = staff.update_current_session(moved.current_session, staff.StaffId(fixture_runtime_id("moved-B")))
        target = fixture_member("unit", name="actual", parent=parent.id)
        self.pane("newest-local", session_id=moved.id, written_at="2026-11-01T12:00:00Z",
                  environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        self.pane("older-transferred", session_id=target.id, written_at="2026-10-31T12:00:00Z",
                  environment={"SHOWRUNNER_UNIT": "former", "SHOWRUNNER_UNIT_ID": "models-unit"})
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.MemberFound(target))
        # With two eligible identities, continuation must refuse ambiguity.
        competing = fixture_member("unit", name="competing", parent=parent.id)
        self.pane("competing", session_id=competing.id,
                  environment={"SHOWRUNNER_UNIT": "former", "SHOWRUNNER_UNIT_ID": "models-unit"})
        with self.assertRaisesRegex(staff.StaffUnreadable, "conflicting staff identities"):
            _ = staff_sources.member(selector, staff.snapshot())

    def test_persisted_cleared_references_preserve_kind_and_parent_safeguards(self) -> None:
        parent, unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        other = fixture_member("showrunner", name="elsewhere")
        moved = staff.move(unit.id, staff.ReportsTo(other.id))
        _ = staff.update_current_session(moved.current_session, staff.StaffId(fixture_runtime_id("wrong-parent-B")))
        _ = fixture_member("unit", name="models", parent=parent.id, runtime_id=fixture_runtime_id("same-name"))
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for reference in (fixture_runtime_id("missing"), parent.id, worker.id, unit.id):
            with self.subTest(reference=reference):
                self.pane("latest", session_id=reference, written_at="2026-11-01T12:00:00Z",
                          environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
                self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))

    def test_runtime_grouping_rejects_fixed_and_superseded_sessions_after_two_clears(self) -> None:
        from scripts.production import session_staff

        _, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        second = staff.StaffId(fixture_runtime_id("group-B"))
        third = staff.StaffId(fixture_runtime_id("group-C"))
        _ = staff.update_current_session(unit.current_session, second)
        _ = staff.update_current_session(second, third)
        record = staff.snapshot()
        sources = session_staff.member_sources()
        for runtime in (third, unit.id, second):
            with self.subTest(runtime=runtime):
                marked = session_staff.MarkedUnit("models-unit",
                    session_staff.LiveClaude("models", runtime, "", 123),
                    herdr.HerdrPlace("agents", "fixture:sdlc/models-unit"), staff_id=runtime)
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
                    grouped = session_staff.group_marked_units((("sdlc", marked),), record=record, sources=sources)
                self.assertEqual(grouped.units.get("sdlc", {}), {"models-unit": marked} if runtime == third else {})
                self.assertEqual(grouped.faults, {})

    def test_current_session_parent_and_showrunner_follow_the_retained_launch_after_rename(self) -> None:
        import unit_lookup
        from scripts.production import session_staff

        parent, unit, _ = self.tree()
        doc = self.production("sdlc", "models-unit")
        parent = staff.set_doc(parent.id, staff.LiveDoc("natedev", doc))
        current = staff.update_current_session(unit.current_session, staff.StaffId(fixture_runtime_id("lookup-B")))
        current = staff.rename(current.id, "renamed")
        other = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="models", parent=other.id)
        sources, record = session_staff.member_sources(), staff.snapshot()
        path = pane_record.record_path("agents")
        before = path.read_bytes(), path.stat().st_mtime_ns
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
            self.assertEqual(unit_lookup.session_showrunner(current.current_session, record=record, sources=sources),
                             session_staff.MemberFound(parent))
            self.assertEqual(unit_lookup.unit_parent("models-unit", session_staff.LiveSession(current.current_session), record=record, sources=sources),
                             unit_lookup.UnitProduction("sdlc", str(doc)))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_live_fill_uses_current_registry_session_for_a_retained_member_reference(self) -> None:
        _ = self.fill_production("sdlc", "models-unit")
        unit = unit_membership("sdlc", "models-unit")
        folder = self.source_root / "worktrees/models-unit"
        self.director_pane("unit", unit, folder, {"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        current = staff.update_current_session(unit.current_session, staff.StaffId(fixture_runtime_id("fill-B")))
        registry = self.session_record(current.current_session, "models-unit")
        raw = cast(dict[str, object], json.loads(registry.read_text()))
        raw["cwd"] = str(folder)
        self.write_json(registry, raw)
        _ = unit_config.write(folder, unit_config.UnitConfig(
            unit_config.StandbyPlan(), unit_config.NoAppPort(), ()))
        path = pane_record.record_path("agents")
        before = path.read_bytes(), path.stat().st_mtime_ns
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        updated = staff.by_id(unit.id)
        assert updated is not None
        self.assertEqual(updated.worktree, staff.PresentWorktree("natedev", folder))
        self.assertEqual(updated.current_session, current.current_session)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_member_result_is_explicit_and_uses_only_the_supplied_snapshot(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        record = staff.snapshot()
        missing = staff_sources.UnitMember("sdlc", "missing")
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("member reread the staff file")):
            self.assertEqual(staff_sources.member(missing, record), staff_sources.NoSuchMember(missing))
            self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))
            self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))

    def test_member_uses_newest_matching_launch_and_id_before_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        renamed = staff.rename(unit.id, "panes")
        fallback = fixture_member("unit", name="models", parent=showrunner.id,
                                  runtime_id=fixture_runtime_id("new-models-backend"))
        self.pane("w1:old", session_id=fallback.id, written_at="2026-10-29T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:new", session_id=renamed.id, environment={
            "STAFF_ID": renamed.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:other", session_id=fallback.id, written_at="2026-10-31T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "other", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        result = staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), staff.snapshot())
        self.assertEqual(result, staff_sources.MemberFound(renamed))

    def test_member_with_no_staff_id_matches_unit_by_short_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        self.pane("w1:p2", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        record = staff.snapshot()
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))
        self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))

    def test_legacy_member_follows_a_transfer_without_using_the_production_mark(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        _ = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, staff.ReportsTo(receiving.id))
        self.pane("w1:unit", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        record = staff.snapshot()
        for slug in ("sdlc", "missing-source"):
            with self.subTest(slug=slug):
                self.assertEqual(staff_sources.member(staff_sources.UnitMember(slug, "models-unit"), record),
                                 staff_sources.NoSuchMember(staff_sources.UnitMember(slug, "models-unit")))
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("receiving", "models-unit"), record),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721"),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))

    def test_production_member_rejects_a_moved_row_despite_its_old_id_bearing_launch(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        _ = self.production("receiving", workspace="control")
        receiving = fixture_member("showrunner", name="control",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, staff.ReportsTo(receiving.id))
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))
        self.assertEqual(staff_sources.unit_parent("models-unit", unit.id),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))
        self.assertEqual(staff.by_id(unit.id), moved)

    def test_observed_id_bearing_pane_follows_its_current_parent(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, staff.ReportsTo(receiving.id))
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        other = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="models", parent=other.id)
        self.assertEqual(staff_sources.member(staff_sources.ObservedUnit("models-unit", session_staff.LiveSession(unit.current_session)), staff.snapshot()),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", unit.id),
                         staff_sources.UnitProduction("receiving", str(new_doc)))

    def test_ambiguous_legacy_member_never_uses_the_mark_to_choose_a_row(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _original, _unit, _worker = self.tree()
        receiving = fixture_member("showrunner", name="receiving")
        _ = fixture_member("unit", name="models", parent=receiving.id)
        self.pane("w1:unit", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        selector = staff_sources.ObservedUnit("models-unit")
        with self.assertRaisesRegex(staff.StaffUnreadable, "models.*no unique staff entry"):
            _ = staff_sources.member(selector, staff.snapshot())

    def test_mac_unit_parent_uses_one_owner_read_for_the_whole_resolution(self) -> None:
        _original, unit, _worker = self.tree()
        with self.machine("mac"):
            _ = self.production("sdlc", "models-unit")
            _ = self.production("receiving")
            receiving = fixture_member("showrunner", name="receiving",
                                       doc=staff.LiveDoc(remote.local_machine(), self.source_root / "receiving-production.md"))
            _ = staff.move(unit.id, staff.ReportsTo(receiving.id))
            self.pane("w1:unit", session_id=unit.id, environment={
                "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
            })
            before = len(self.ssh_commands())
            parent = staff_sources.unit_parent("models-unit", unit.id)
            self.assertEqual(parent.slug, "receiving")
            self.assertEqual(len(self.ssh_commands()) - before, 1)

    def test_unit_parent_agrees_with_showrunner_members_after_transfer(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit", workspace="sdlc")
        new_doc = self.production("receiving", workspace="new-tools")
        destination = fixture_member("showrunner", name="new-tools")
        original = staff.set_doc(original.id, staff.LiveDoc("natedev", old_doc))
        destination = staff.set_doc(destination.id, staff.LiveDoc("natedev", new_doc))
        destination = staff.rename(destination.id, "control")
        self.pane("w1:showrunner", session_id=destination.id, environment={"STAFF_ID": destination.id})
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        for slug, expected_parent, doc in (("sdlc", original, old_doc), ("receiving", destination, new_doc)):
            with self.subTest(slug=slug):
                _ = staff.move(unit.id, staff.ReportsTo(expected_parent.id))
                record = staff.snapshot()
                members = staff_sources.showrunner_members(record)
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot, \
                        mock.patch.object(pane_record, "read_strict", wraps=pane_record.read_strict) as launches, \
                        mock.patch.object(showrunners, "read_sources", wraps=showrunners.read_sources) as productions:
                    parent = staff_sources.unit_parent("models-unit", unit.id)
                self.assertEqual(parent, staff_sources.UnitProduction(slug, str(doc)))
                self.assertEqual(members[parent.slug], expected_parent)
                recorded_unit = record.by_id(unit.id)
                assert recorded_unit is not None
                self.assertEqual(staff.ReportsTo(members[parent.slug].id), recorded_unit.parent)
                self.assertEqual(snapshot.call_count, 1)
                self.assertEqual(launches.call_count, 1)
                self.assertEqual(productions.call_count, 1)

    def test_member_with_unresolved_or_wrong_kind_id_does_not_take_an_unrelated_name(self) -> None:
        showrunner, _unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for staff_id in (fixture_runtime_id("missing-member"), showrunner.id, worker.id):
            with self.subTest(staff_id=staff_id):
                self.pane("w1:p2", session_id=staff_id, environment={ "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
                })
                self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))

    def test_mac_seed_reads_local_sources_and_sends_one_input_to_owner(self) -> None:
        _ = self.production("owner-only")
        with self.machine("mac"):
            _ = self.production("mac-only", "laptop-unit", workspace="laptop")
            self.pane("w1:p1", session_id="session-mac-only", environment={})
            remote_reads = len(self.ssh_commands())
            _ = self.seed()
            self.assertFalse(self.mac_file.exists())
            self.assertTrue((self.mac_file.parent / "operation.lock").exists())
        self.assertIsNotNone(staff.by_path("laptop/laptop"))
        self.assertIsNone(staff.by_path("owner-only"))
        self.assertEqual({row.name for row in staff.find()}, {"laptop"})
        self.assertEqual(self.ssh_commands()[remote_reads:], [["show", "--snapshot", "--json"], ["apply-seed", "--json"]])
        self.assertTrue((self.staff_file.parent / "operation.lock").exists())
        self.assertNotEqual((self.staff_file.parent / "operation.lock").stat().st_ino, (self.mac_file.parent / "operation.lock").stat().st_ino)

    def test_each_seed_order_preserves_other_machine_units_and_workers(self) -> None:
        for first in ("natedev", "mac"):
            with self.subTest(first=first):
                showrunner = fixture_member("showrunner", name="sdlc")
                alpha = fixture_member("unit", name="alpha", parent=showrunner.id)
                beta = fixture_member("unit", name="beta", parent=showrunner.id)
                alpha_worker = fixture_member("worker", name="impl1", parent=alpha.id)
                beta_worker = fixture_member("worker", name="impl1", parent=beta.id)
                layouts: tuple[tuple[FixtureMachine, str], ...] = (("natedev", "alpha-unit"), ("mac", "beta-unit"))
                for machine, local_unit in layouts:
                    with self.machine(machine):
                        _ = self.production("sdlc")
                        _ = self.worktree(local_unit)
                order: tuple[FixtureMachine, FixtureMachine] = ("natedev", "mac") if first == "natedev" else ("mac", "natedev")
                for machine in order:
                    with self.machine(machine):
                        _ = self.seed()
                    self.assertCountEqual(staff.find(), [showrunner, alpha, beta, alpha_worker, beta_worker])

    def test_retirement_requires_positive_evidence_and_keeps_workers(self) -> None:
        showrunner, unit, worker = self.tree()
        empty = fixture_member("unit", name="empty", parent=showrunner.id)
        omitted = fixture_member("unit", name="omitted", parent=showrunner.id)
        remote = fixture_member("unit", name="remote", parent=showrunner.id)
        unregistered = fixture_member("showrunner", name="other-machine")
        _ = self.production("sdlc", "models-unit", "empty-unit")
        before = self.staff_file.read_bytes()
        _output, errors = self.seed()
        self.assertEqual(staff.by_id(empty.id), empty)
        self.assertEqual(staff.by_id(omitted.id), omitted)
        self.assertEqual(staff.by_id(remote.id), remote)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(staff.by_id(unregistered.id), unregistered)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(errors, "unchanged\n")

    def test_unreadable_doc_keeps_its_rows_and_reports_one_source_line(self) -> None:
        _ = self.tree()
        doc = self.production("sdlc", "models-unit")
        before = self.staff_file.read_bytes()
        doc.unlink()
        doc.mkdir()
        _output, errors = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        lines = [line for line in errors.splitlines() if "sdlc" in line]
        self.assertEqual(len(lines), 1, errors)

    def test_dry_run_describes_changes_without_applying_them(self) -> None:
        _ = self.tree()
        _ = self.production("other")
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as apply:
            output, _errors = self.seed(dry_run=True)
        apply.assert_not_called()
        self.assertIn("add showrunner:other", output)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_path("other"))

    def test_seed_uses_one_snapshot_and_applies_all_sources_in_one_write(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        _ = self.production("one", "alpha-unit")
        _ = self.production("two", "beta-unit")
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as read, \
                mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as apply:
            _ = self.seed()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(apply.call_count, 1)
        self.assertIsNotNone(staff.by_path("one/alpha"))
        self.assertIsNotNone(staff.by_path("two/beta"))

    def test_seed_cli_reports_an_unreadable_record_without_replacing_it(self) -> None:
        before = self.write_document({"schema": 999, "rows": []})
        _ = self.production("sdlc")
        failed = self.script(SOURCES_SCRIPT, "seed", code=2)
        self.assertTrue(failed.stderr.strip())
        self.assertNotIn("Traceback", failed.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_waits_for_lifecycle_operation_then_reads_updated_docs(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        _ = self.production("old", "models-unit")
        _ = self.production("new")
        _ = self.seed()
        unit = staff.by_path("old/models")
        destination = staff.by_path("new")
        assert unit is not None and destination is not None
        lock = self.staff_file.parent / "operation.lock"
        with self.lock_notice(lock) as listener:
            with staff.operation():
                moved = staff.move(unit.id, staff.ReportsTo(destination.id))
                pending = self.start_script(SOURCES_SCRIPT, "seed")
                self.assert_operation_blocks(pending, listener)
            _output, errors = pending.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(pending.returncode, 0, errors)
        self.assertEqual(staff.by_id(unit.id), moved)
        self.assertEqual(staff.by_path("new/models"), moved)
        self.assertIsNone(staff.by_path("old/models"))

    def test_apply_seed_keeps_current_names_and_refuses_an_invalid_transaction_atomically(self) -> None:
        first = fixture_member("showrunner", name="first")
        second = fixture_member("showrunner", name="second")
        seed = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.RootSeedParent()),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        renamed = staff.rename(second.id, "renamed")
        record = staff.apply_seed(seed)
        self.assertEqual(record.by_id(second.id), replace(renamed, parent=staff.ReportsTo(first.id)))
        invalid = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.SeedParent("second")),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff.apply_seed(invalid)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_keeps_a_renamed_unit_whose_identity_is_only_on_the_other_machine(self) -> None:
        showrunner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(
            "natedev", self.source_root / "sdlc-production.md"))
        unit = fixture_member("unit", name="beta", parent=showrunner.id)
        renamed = staff.rename(unit.id, "panes")
        _ = self.production("sdlc")
        with self.machine("mac"):
            _ = self.production("sdlc", "beta-unit")
            self.pane("w1:beta", session_id=unit.id, environment={
                "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "beta-unit",
            })
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.find(kind="unit"), [renamed])
        self.assertEqual(self.staff_file.read_bytes(), before)
        with self.machine("mac"):
            _ = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(unit.id), renamed)

    def test_bad_pane_evidence_prevents_every_seed_change_and_names_the_file(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = staff.rename(unit.id, "panes")
        _ = self.production("parent")
        _ = self.production("sdlc", "models-unit", "new-unit", parent="parent")
        session = herdr.shared_session()
        path = pane_record.record_path(session)
        for content in ("{broken", "[]", None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                    path.mkdir()
                else:
                    write_text_atomic(path, content)
                before = self.staff_file.read_bytes()
                failed = self.script(SOURCES_SCRIPT, "seed", code=2)
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertEqual(staff.by_id(showrunner.id), showrunner)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(len(failed.stderr.splitlines()), 1, failed.stderr)
                self.assertTrue(failed.stderr.startswith(f"StaffUnreadable: {path}:"), failed.stderr)
                self.assertEqual(pane_record.read(session), {})
                if content is None:
                    path.rmdir()

    def test_malformed_pane_entries_refuse_seed_but_keep_legacy_read_results(self) -> None:
        showrunner = fixture_member("showrunner", name="sdlc")
        _ = fixture_member("unit", name="old", parent=showrunner.id)
        _ = self.production("sdlc", "new-unit")
        session = herdr.shared_session()
        path = pane_record.record_path(session)
        before = self.staff_file.read_bytes()
        valid: dict[str, object] = {
            "session_id": "cdc4420c-ae5d-5a9a-8135-75e8c0875088", "environment": {}, "written_at": "2026-10-30T09:00:00+00:00",
        }
        malformed: tuple[object, ...] = (
            None, {}, {**valid, "environment": None}, {**valid, "environment": {"STAFF_ID": 7}},
            {**valid, "session_id": 7}, {**valid, "written_at": None},
        )
        for entry in malformed:
            with self.subTest(entry=entry):
                document = {"w1:p1": entry}
                self.write_json(path, document)
                self.assertEqual(pane_record.read(session), document)
                with self.assertRaises(ValueError) as raised:
                    _ = pane_record.read_strict(session)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn("w1:p1", str(raised.exception))
                failed = self.script(SOURCES_SCRIPT, "seed", code=2)
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(len(failed.stderr.splitlines()), 1, failed.stderr)
                self.assertTrue(failed.stderr.startswith(f"StaffUnreadable: {path}:"), failed.stderr)
                self.assertIn("w1:p1", failed.stderr)
                self.assertEqual(pane_record.read(session), document)

    def test_seed_keeps_a_parent_whose_timer_is_on_the_other_machine(self) -> None:
        parent = fixture_member("showrunner", name="remote")
        child = fixture_member("showrunner", name="local", parent=parent.id)
        _ = self.production("local", parent="remote")
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_roots_a_showrunner_only_when_parent_evidence_says_root(self) -> None:
        parent = fixture_member("showrunner", name="parent")
        child = fixture_member("showrunner", name="child", parent=parent.id)
        _ = self.production("parent", parent="child")
        _ = self.production("child", parent="parent")
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(staff.by_id(parent.id), parent)
        self.assertEqual(self.staff_file.read_bytes(), before)
        _ = staff.move(child.id, staff.ReportsTo(parent.id))
        parent_file = showrunners.SHOWRUNNER_STATE_DIR / "child" / showrunners.PARENT_FILE
        parent_file.unlink()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unreadable_parent_evidence_leaves_its_production_unchanged(self) -> None:
        showrunner = self.tree()[0]
        _ = self.production("sdlc", "models-unit", "new-unit")
        path = showrunners.SHOWRUNNER_STATE_DIR / "sdlc" / showrunners.PARENT_FILE
        path.mkdir(parents=True)
        self.assertEqual(showrunner_tree.parent_of("sdlc"), "")
        self.assertEqual(showrunner_tree.parent_of("sdlc"), "")
        _output, errors = self.seed()
        self.assertIsNotNone(staff.by_path("sdlc/new"))
        self.assertEqual(staff.by_id(showrunner.id), showrunner)
        self.assertEqual(errors, "unchanged\n")

    def test_seed_reads_each_source_once_and_reports_a_bad_doc_once(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        docs = [self.production("one", "alpha-unit", "beta-unit"), self.production("two", "gamma-unit")]
        bad = self.production("bad")
        bad.unlink()
        bad.mkdir()
        self.pane("w1:one", session_id="session-one", environment={})
        paths = [*docs, bad, pane_record.record_path(herdr.shared_session())]
        for slug in ("one", "two", "bad"):
            paths.append(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}" / "conf")
        parent_paths = [showrunners.SHOWRUNNER_STATE_DIR / slug / showrunners.PARENT_FILE
                        for slug in ("one", "two")]
        reads: Counter[Path] = Counter()
        read_text = Path.read_text

        def counted(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads[path] += 1
            return read_text(path, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", new=counted):
            _output, errors = self.seed()
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(reads[path], 1)
        for path in parent_paths:
            with self.subTest(path=path):
                self.assertEqual(reads[path], 0)
        self.assertEqual(len([line for line in errors.splitlines() if str(bad) in line]), 1, errors)
        for path in ("one/alpha", "one/beta", "two/gamma"):
            self.assertIsNotNone(staff.by_path(path))

    def test_an_unreadable_timer_does_not_hide_other_productions(self) -> None:
        old = fixture_member("showrunner", name="bad")
        old_unit = fixture_member("unit", name="kept", parent=old.id)
        _ = self.production("bad")
        _ = self.production("good", "models-unit")
        conf = showrunners.NOTIFIER_STATE_DIR / "showrunner-bad" / "conf"
        conf.unlink()
        conf.mkdir()
        _output, errors = self.seed()
        self.assertEqual(staff.by_id(old.id), old)
        self.assertEqual(staff.by_id(old_unit.id), old_unit)
        self.assertIsNotNone(staff.by_path("good/models"))
        self.assertEqual(len([line for line in errors.splitlines() if "showrunner-bad" in line]), 1, errors)

    def test_showrunner_discovery_still_raises_unreadable_session_records(self) -> None:
        _ = self.production("sdlc")
        failure = OSError("session records are unreadable")
        errors = io.StringIO()
        with mock.patch.object(showrunners, "socket_for", side_effect=failure), redirect_stderr(errors):
            self.assertEqual(showrunners.registered_showrunners(), [])
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("showrunner-sdlc", errors.getvalue())
        self.assertIn("session records are unreadable", errors.getvalue())

    def test_a_stale_mac_retirement_keeps_a_unit_transferred_since_the_snapshot(self) -> None:
        old = fixture_member("showrunner", name="old")
        new = fixture_member("showrunner", name="new")
        unit = fixture_member("unit", name="models", parent=old.id)
        evidence = staff.SeedInput((), (staff.SeedRetirement(unit.id, old.id),))
        moved = staff.move(unit.id, staff.ReportsTo(new.id))
        before = self.staff_file.read_bytes()
        with self.machine("mac"), redirect_stderr(io.StringIO()) as errors:
            record = staff.apply_seed(evidence)
        self.assertEqual(record.by_id(unit.id), moved)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn(unit.name, errors.getvalue())
        self.assertIn("parent changed", errors.getvalue())
        self.assertEqual(self.ssh_commands(), [["apply-seed", "--json"]])

    def test_retirement_transport_requires_the_observed_parent(self) -> None:
        parent = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=parent.id)
        before = self.staff_file.read_bytes()
        for retirement in (unit.id, {"id": unit.id}, {"id": unit.id, "parent": None}):
            with self.subTest(retirement=retirement), self.assertRaises(staff.StaffRefused):
                _ = staff.apply_seed(staff.SeedInput.from_json({"members": [], "retire": [retirement]}))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_only_a_typed_absent_record_bootstraps_seed(self) -> None:
        _ = self.production("sdlc")
        failure = staff.StaffUnreadable("cannot read staff record: [Errno 2] another read failed")
        with mock.patch.object(staff, "snapshot", side_effect=failure):
            with self.assertRaises(staff.StaffUnreadable) as raised:
                staff_sources.seed()
        self.assertIs(raised.exception, failure)
        self.assertFalse(self.staff_file.exists())

    def test_seed_uses_the_same_initial_record_read_as_the_writers(self) -> None:
        _ = self.production("sdlc")
        with mock.patch.object(staff, "initial_snapshot", wraps=staff.initial_snapshot) as initial:
            _ = self.seed()
        initial.assert_called_once_with()
        self.assertEqual([row.name for row in staff.snapshot().rows], ["sdlc"])


class NonLocalMemberReferenceTests(unittest.TestCase):
    def test_seed_lists_only_showrunners_despite_a_transferred_unit_launch_label(self) -> None:
        old_id = staff.StaffId(fixture_runtime_id("seed-old-owner"))
        new_id = staff.StaffId(fixture_runtime_id("seed-new-owner"))
        unit_id = staff.StaffId(fixture_runtime_id("seed-moved-unit"))
        current = staff.native_id(fixture_runtime_id("seed-moved-current"))
        old = staff.StaffRow(old_id, "showrunner", "old", staff.StaffRoot(), current_session=old_id,
                             doc=staff.LiveDoc("natedev", Path("/fixture/old.md")))
        new = staff.StaffRow(new_id, "showrunner", "new", staff.StaffRoot(), current_session=new_id,
                             doc=staff.LiveDoc("natedev", Path("/fixture/new.md")))
        moved = staff.StaffRow(unit_id, "unit", "alpha", staff.ReportsTo(new.id), current_session=current)
        record = staff.StaffSnapshot((old, new, moved))
        document = "# Fixture\n"
        productions = {
            slug: showrunners.ShowrunnerSource(
                showrunners.Timer(slug, "session:" + row.current_session, Path(f"/fixture/{slug}.md"), "UTC"),
                document)
            for slug, row in (("old", old), ("new", new))
        }
        launch: pane_record.PaneLaunch = {
            "pane_id": "w1:alpha", "session_id": unit_id,
            "config_dir": "/fixture/claude", "cwd": "/fixture/alpha",
            "name": "alpha-unit", "model": "", "remote_control": False,
            "environment": {"SHOWRUNNER_UNIT": "old", "SHOWRUNNER_UNIT_ID": "alpha-unit"},
            "flags_read": True, "written_at": "2026-11-01T12:00:00Z",
        }
        sources = MemberSources(productions, (launch,))
        with (mock.patch.object(remote, "local_machine", return_value="natedev"),
              mock.patch.object(staff, "snapshot", side_effect=AssertionError("unexpected staff read")),
              mock.patch.object(Path, "is_dir", side_effect=AssertionError("unexpected directory read"))):
            seed = staff_sources._collect(record, sources)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([(member.key, member.match) for member in seed.members], [
            ("showrunner:old", old.id), ("showrunner:new", new.id),
        ])
        self.assertEqual(record.rows, (old, new, moved))

    def test_unresolved_nonlocal_launch_allows_name_lookup_but_resolved_evidence_does_not(self) -> None:
        from scripts.production import session_staff

        parent_id = staff.StaffId(fixture_runtime_id("nonlocal-parent"))
        unit_id = staff.StaffId(fixture_runtime_id("nonlocal-unit"))
        worker_id = staff.StaffId(fixture_runtime_id("nonlocal-worker"))
        other_id = staff.StaffId(fixture_runtime_id("nonlocal-other-parent"))
        moved_id = staff.StaffId(fixture_runtime_id("nonlocal-moved-unit"))
        parent = staff.StaffRow(parent_id, "showrunner", "demo", staff.StaffRoot(), current_session=parent_id)
        unit = staff.StaffRow(unit_id, "unit", "alpha", staff.ReportsTo(parent_id), current_session=unit_id)
        worker = staff.StaffRow(worker_id, "worker", "impl", staff.ReportsTo(unit_id), current_session=worker_id)
        other = staff.StaffRow(other_id, "showrunner", "other", staff.StaffRoot(), current_session=other_id)
        moved = staff.StaffRow(moved_id, "unit", "alpha", staff.ReportsTo(other_id), current_session=moved_id)
        record = staff.StaffSnapshot((parent, unit, worker, other, moved))
        selector = session_staff.UnitMember("demo", "alpha-unit")
        missing = fixture_runtime_id("nonlocal-unregistered-session")
        for reference in (missing, worker.id, parent.id, moved.id):
            with self.subTest(reference=reference):
                # No production mark: this must reach the non-local launch loop.
                launch: pane_record.PaneLaunch = {
                    "pane_id": "w1:p1", "session_id": reference,
                    "config_dir": "/fixture/claude", "cwd": "/fixture/unit",
                    "name": "alpha-unit", "model": "", "remote_control": False,
                    "environment": {"SHOWRUNNER_UNIT_ID": "alpha-unit"},
                    "flags_read": True, "written_at": "2026-11-01T12:00:00Z",
                }
                sources = session_staff.MemberSources({}, (launch,))
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("unexpected staff read")):
                    found = sources.member(selector, record)
                expected = (session_staff.MemberFound(unit) if reference == missing
                            else session_staff.NoSuchMember(selector))
                self.assertEqual(found, expected)


if __name__ == "__main__":
    _ = unittest.main()
