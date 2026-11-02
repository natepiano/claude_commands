"""Seed real fixture sources without losing identity or another machine's staff."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

import io
import json
import os
import unittest
from collections import Counter
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import unit_runs
from scripts.production import staff, staff_sources, unit_config, live_units, fake_unit_host, worker_fill
from scripts.production.staff_test_support import (
    SOURCES_SCRIPT, FixtureMachine, StaffTestCase, production_membership, unit_membership,
)
from scripts.shutdown import pane_record, remote
import herdr
from scripts.production import showrunners
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
import showrunner_tree
import production_units
from unit_lookup import ProductionMembers, live_unit_table, retired_units, unit_is_retired


class StaffSourcesTests(StaffTestCase):
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

    @override
    def retired_fill_production(self, slug: str, *units: str, wrapped: bool = False) -> Path:
        doc = super().retired_fill_production(slug, *units, wrapped=wrapped)
        owner = next(row for row in staff.find(kind="showrunner") if row.name == slug)
        location = (staff.WrappedDoc("natedev", doc) if wrapped else staff.LiveDoc("natedev", doc))
        _ = staff.set_doc(owner.id, location)
        return doc

    def test_retired_fill_ignores_temporary_worktree_documents_as_sources(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        scratch = self.root / "scratch-checkout"
        _ = self.fixture_git(doc.parents[1], "worktree", "add", "-b", "scratch", str(scratch))
        extra = scratch / "docs" / doc.name
        write_text_atomic(extra, doc.read_text().replace("standby", "permanent"))
        before = self.staff_file.read_bytes()
        code, output, errors = self.fill_result(retired=True, dry_run=True)
        self.assertEqual(code, 0, output + errors)
        self.assertNotIn(str(extra), output + errors)
        self.assertNotIn("conflicting production documents", errors)
        self.assertIn("models-unit", output)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_retired_fill_finds_a_timerless_wrapped_owner_with_a_different_document_slug(self) -> None:
        doc = self.retired_fill_production("performance", "models-unit", wrapped=True)
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "performance")
        owner = staff.rename(owner.id, "fps")
        write_text_atomic(doc, doc.read_text().replace("**Showrunner session:** `performance`",
                                                     "**Showrunner session:** `fps`"))
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-performance" / "conf"
        timer.unlink()
        timer.parent.rmdir()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        code, output, errors = self.fill_result(retired=True, dry_run=True)
        self.assertEqual(code, 0, output + errors)
        self.assertIn(f"source {doc}\n", output)
        self.assertIn("would repair retired unit performance/models-unit:", output)
        self.assertNotIn("no current showrunner staff row", errors)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        after = self.staff_file.read_bytes()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                code, output, errors = self.fill_result(retired=True)
                self.assertEqual(code, 0, output + errors)
                if attempt:
                    self.assertEqual(output, "unchanged\n")
                    self.assertEqual(self.staff_file.read_bytes(), after)
                after = self.staff_file.read_bytes()
        self.assertEqual(staff.by_id(owner.id), owner)

    def test_retired_fill_excludes_an_only_bound_scratch_document_from_every_source_route(self) -> None:
        doc = self.retired_fill_production("scratch-only", "scratch-only-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "scratch-only")
        row = unit_membership("scratch-only", "scratch-only-unit")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns, doc.read_bytes()
        with mock.patch.object(showrunners, "SCRATCH_ROOT", doc.parents[1]):
            for dry_run in (True, False, False):
                with self.subTest(dry_run=dry_run):
                    code, output, errors = self.fill_result(retired=True, dry_run=dry_run)
                    self.assertEqual(code, 1, output + errors)
                    self.assertEqual(len(errors.splitlines()), 1, errors)
                    self.assertIn(f"{owner.id} {owner.name}:", errors)
                    self.assertIn(str(doc), errors)
                    self.assertIn("scratch", errors)
                    self.assertNotIn(f"source {doc}\n", output)
                    self.assertNotIn("would repair", output)
                    self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns,
                                      doc.read_bytes()), before)
                    self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_retired_fill_uses_only_the_showrunner_bound_document_despite_other_copies(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        second = doc.parent / "old" / doc.name
        write_text_atomic(second, doc.read_text().replace("standby", "permanent").replace(
            "**Showrunner session:** `sdlc`", "**Showrunner session:** `former-sdlc`"))
        # A stale timer copy must not override the durable staff document either.
        self.register_production("sdlc", second)
        code, output, errors = self.fill_result(retired=True, dry_run=True)
        self.assertEqual(code, 0, output + errors)
        self.assertNotIn("conflicting production documents", errors)
        self.assertNotIn(str(second), output + errors)
        self.assertIn("models-unit", output)
        self.assertIn(str(doc), output)

    def test_retired_fill_lists_a_production_without_a_showrunner_once_and_adds_nothing(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        self.forget_rows(*(row.id for row in staff.snapshot().rows))
        before = self.staff_file.read_bytes()
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        lines = [line for line in errors.splitlines() if "no current showrunner staff row" in line]
        self.assertEqual(len(lines), 1, errors)
        self.assertIn(str(doc), lines[0])
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.snapshot().rows, ())

    def test_retired_fill_previews_missing_rows_but_defers_admission_and_reruns_without_writes(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        row = unit_membership("sdlc", "models-unit")
        self.forget_rows(row.id)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns, doc.read_bytes()
        code, output, errors = self.fill_result(retired=True, dry_run=True)
        self.assertEqual(code, 0, output + errors)
        self.assertIn("models-unit", output)
        self.assertIn(f"would add retired unit sdlc/models-unit: {self.source_root / 'worktrees/models-unit'}", output)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                code, output, errors = self.fill_result(retired=True)
                self.assertEqual(code, 0, output + errors)
                self.assertIn("deferred: retired unit sdlc/models-unit waits for Phase 3h", output)
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns,
                                  doc.read_bytes()), before)
                self.assertIsNone(staff.by_id(row.id))
                self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_fill_uses_session_fallback_without_a_units_table_and_continues_after_a_missing_session(self) -> None:
        doc = self.fill_production("sdlc", "fallback-unit", "unavailable-unit")
        write_text_atomic(doc, doc.read_text().split("## Units")[0])
        folder = self.source_root / "worktrees/fallback-unit"
        config = unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.NoAppPort(), ())
        unit_config.write(folder, config)
        fallback = unit_membership("sdlc", "fallback-unit")
        session = self.session_record(fallback.id, "fallback-unit")
        value = cast(dict[str, object], json.loads(session.read_text()))
        value["cwd"] = str(folder)
        self.write_json(session, value)
        self.pane("w1:fallback", session_id=fallback.id, environment={"STAFF_ID": fallback.id})
        _ = self.fill_production("tools", "following-unit")
        unavailable = unit_membership("sdlc", "unavailable-unit")
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            code, output, errors = self.fill_result()
        self.assertEqual(code, 1, output)
        self.assertEqual(reads.call_count, 1)
        self.assertIn(f"{unavailable.id} {unavailable.name}:", errors)
        self.assertIn("Units", errors)
        self.assertIn("no verified live session", errors)
        self.assertEqual(unit_membership("sdlc", "fallback-unit").worktree,
                         staff.PresentWorktree(remote.local_machine(), folder))
        self.assertEqual(unit_config.read(folder), config)
        following = unit_membership("tools", "following-unit")
        self.assertIsInstance(following.worktree, staff.PresentWorktree)
        self.assertIsInstance(unit_config.read(self.source_root / "worktrees/following-unit"), unit_config.UnitConfig)

    def test_fill_requires_checkout_and_merge_branch_evidence_before_using_a_timer_document(self) -> None:
        for slug, field in (("no-checkout", "Showrunner checkout"), ("no-branch", "Merge branch"),
                            ("feature", "branch")):
            with self.subTest(slug=slug):
                doc = self.fill_production(slug, f"{slug}-unit")
                if slug == "feature":
                    _ = self.fixture_git(doc.parents[1], "checkout", "-b", "feature")
                else:
                    write_text_atomic(doc, "\n".join(line for line in doc.read_text().splitlines()
                                                    if not line.startswith(f"- **{field}:**")) + "\n")
                owner = next(row for row in staff.find(kind="showrunner") if row.name == slug)
                code, output, errors = self.fill_result()
                self.assertEqual(code, 1, output)
                self.assertIn(f"{owner.id} {owner.name}:", errors)
                self.assertIn(field, errors)
                current = staff.by_id(owner.id)
                assert current is not None
                self.assertIsInstance(current.doc, staff.DocUnfilled)
                self.assertIsInstance(unit_membership(slug, f"{slug}-unit").worktree, staff.WorktreeUnfilled)
                self.assertFalse((self.source_root / f"worktrees/{slug}-unit/.unit.toml").exists())

    def test_fill_keeps_a_saved_doc_but_never_uses_it_to_fill_children_without_a_timer(self) -> None:
        doc = self.fill_production("sdlc", "stale-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        saved = staff.LiveDoc(remote.local_machine(), doc)
        _ = staff.set_doc(owner.id, saved)
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc/conf"
        timer.unlink()
        timer.parent.rmdir()
        _ = self.session_record(owner.id, "sdlc")
        self.pane("w1:showrunner", session_id=owner.id, environment={"STAFF_ID": owner.id})
        code, output, errors = self.fill_result()
        child = unit_membership("sdlc", "stale-unit")
        self.assertEqual(code, 1, output)
        self.assertIn(f"{child.id} {child.name}:", errors)
        self.assertIn("no timer names its live production document", errors)
        self.assertNotIn(f"{owner.id} {owner.name}:", errors)
        current = staff.by_id(owner.id)
        assert current is not None
        self.assertEqual(current.doc, saved)
        self.assertIsInstance(child.worktree, staff.WorktreeUnfilled)
        self.assertFalse((self.source_root / "worktrees/stale-unit/.unit.toml").exists())

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
        owner = staff.move(owner.id, root.id)
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

    def test_fill_reports_configuration_recovery_failure_for_an_already_filled_worktree(self) -> None:
        _ = self.fill_production("sdlc", "recovering-unit")
        folder = self.source_root / "worktrees/recovering-unit"
        row = unit_membership("sdlc", "recovering-unit")
        _ = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), folder))
        with mock.patch.object(unit_config, "write", side_effect=OSError("configuration publication refused")):
            code, output, errors = self.fill_result()
        self.assertEqual(code, 1, output)
        self.assertIn(f"recovery {row.id} {row.name}: configuration publication refused", errors)
        self.assertNotIn(f"\n{row.id} {row.name}:", "\n" + errors)
        self.assertFalse((folder / ".unit.toml").exists())
        current = unit_membership("sdlc", "recovering-unit")
        self.assertEqual(current.worktree, staff.PresentWorktree(remote.local_machine(), folder))
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertIsInstance(unit_config.read(folder), unit_config.UnitConfig)
        before = self.staff_file.read_bytes(), (folder / ".unit.toml").read_bytes()
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertEqual(output, "unchanged\n")
        self.assertEqual((self.staff_file.read_bytes(), (folder / ".unit.toml").read_bytes()), before)

    def fill_result(self, *, dry_run: bool = False, retired: bool = False) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = staff_sources.fill(dry_run=dry_run, retired=retired)
        return code, output.getvalue(), errors.getvalue()

    def test_retired_fill_recovers_a_missing_session_from_its_interactive_transcript(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        write_text_atomic(doc, doc.read_text().replace(
            "session session-models-unit, folder " + str(self.root / "original/models-unit"), "session "))
        folder = self.source_root / "worktrees/models-unit"
        _ = self.session_transcript("recovered-director", folder)
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 0, output + errors)
        parked = unit_config.read_parked(row.id)
        assert isinstance(parked, unit_config.UnitConfig)
        assert isinstance(parked.retired, unit_config.RetiredSession)
        self.assertEqual(parked.retired.session_id, "recovered-director")
        self.assertEqual(parked.retired.start_folder, unit_config.RecordedStartFolder(folder))
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual(retained.id, row.id)
        path = unit_config.parked_path(row.id)
        before = path.read_bytes(), path.stat().st_mtime_ns, self.staff_file.read_bytes()
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 0, output + errors)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, self.staff_file.read_bytes()), before)

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

    def test_retired_fill_parks_verified_facts_keeps_identity_and_finishes_unchanged(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        row = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.source_root / "worktrees/models-unit"))
        active_before = staff.active(staff.snapshot(), "unit")
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 0, output + errors)
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current, replace(row, worktree=staff.RemovedWorktree(
            "natedev", self.source_root / "worktrees/models-unit")))
        saved = unit_config.read_parked(row.id)
        self.assertEqual(saved, unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.NoAppPort(), (),
            unit_config.RetiredSession("2026-10-31", "session-models-unit",
                unit_config.RecordedStartFolder(self.root / "original/models-unit"), unit_config.Branch("models-unit"))))
        self.assertEqual(staff.active(staff.snapshot(), "unit"), active_before)
        parked = unit_config.parked_path(row.id)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.staff_file, parked, doc)]
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.staff_file, parked, doc)], before)

    def test_retired_fill_dry_run_has_no_staff_config_or_exclude_writes(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        paths = (self.staff_file, doc, doc.parents[1] / ".git/info/exclude")
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        with (mock.patch.object(staff, "ensure", side_effect=AssertionError("dry-run admission")),
              mock.patch.object(staff, "set_worktree", side_effect=AssertionError("dry-run state change")),
              mock.patch.object(unit_config, "write_parked", side_effect=AssertionError("dry-run parked write"))):
            code, output, errors = self.fill_result(retired=True, dry_run=True)
        self.assertEqual(code, 0, output + errors)
        self.assertIn("source ", output)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)
        self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_retired_fill_lists_a_missing_wrapped_owner_without_admitting_rows(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit", wrapped=True)
        record = staff.snapshot()
        self.forget_rows(*(row.id for row in record.rows))
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc/conf"
        timer.unlink()
        # A different owner's archived location cannot admit this production.
        anchor_parent = fixture_member("showrunner", name="archive", doc=staff.WrappedDoc("natedev", doc))
        anchor = fixture_member("unit", name="anchor", parent=anchor_parent.id,
                              worktree=staff.RemovedWorktree("natedev", doc.parents[1]))
        before = self.staff_file.read_bytes()
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        lines = [line for line in errors.splitlines() if "no current showrunner staff row" in line]
        self.assertEqual(len(lines), 1, errors)
        self.assertIn(str(doc), lines[0])
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertFalse(any(row.name == "sdlc" for row in staff.find(kind="showrunner")))
        self.assertFalse(any(row.name == "models" for row in staff.find(kind="unit")))
        for row in record.rows:
            self.assertFalse(unit_config.parked_path(row.id).exists())
        self.assertEqual(staff.active(staff.snapshot(), "unit"), ())
        self.assertEqual(staff.active(staff.snapshot(), "showrunner"), ())
        self.assertFalse(timer.exists())
        self.assertEqual(staff.by_id(anchor_parent.id), anchor_parent)
        self.assertEqual(staff.by_id(anchor.id), anchor)

    def test_retired_fill_preserves_an_active_location_and_reports_both_conflicting_paths(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        current = staff.set_worktree(row.id, staff.PresentWorktree("natedev", self.root / "current-models"))
        before = self.staff_file.read_bytes()
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        self.assertIn(str(doc), errors)
        self.assertIn(str(self.root / "current-models"), errors)
        self.assertIn(str(self.source_root / "worktrees/models-unit"), errors)
        self.assertEqual(staff.by_id(row.id), current)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_retired_fill_uses_a_unique_transcript_folder_and_reports_each_ambiguous_path(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        text = doc.read_text().replace(f", folder {self.root / 'original/models-unit'}", "")
        write_text_atomic(doc, text)
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        self.assertIn("transcript", errors)
        self.assertFalse(unit_config.parked_path(row.id).exists())
        first = self.session_transcript("session-models-unit", self.root / "first")
        second = self.session_transcript("session-models-unit", self.root / "second")
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        self.assertIn(str(first.parent), errors)
        self.assertIn(str(second.parent), errors)
        self.assertFalse(unit_config.parked_path(row.id).exists())
        second.unlink()
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 0, output + errors)
        saved = unit_config.read_parked(row.id)
        assert isinstance(saved, unit_config.UnitConfig)
        assert isinstance(saved.retired, unit_config.RetiredSession)
        self.assertEqual(saved.retired.start_folder, unit_config.RecordedStartFolder(self.root / "first"))

    def test_retired_fill_does_not_borrow_a_same_named_session_from_another_production(self) -> None:
        doc = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        write_text_atomic(doc, doc.read_text().replace("session session-models-unit", "session "))
        other_doc = self.production("tools")
        self.units_doc(other_doc, ("models-unit",), workspace="tools")
        _ = unit_membership("tools", "models-unit")
        self.pane("w1:other", session_id="unrelated-session", environment={
            "SHOWRUNNER_UNIT": "tools", "SHOWRUNNER_UNIT_ID": "models-unit"})
        _ = self.session_transcript("unrelated-session", self.root / "other-production")
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        self.assertIn("no unique recorded director session", errors)
        self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_retired_fill_reports_missing_branch_and_session_per_row_and_finishes_an_independent_row(self) -> None:
        doc = self.retired_fill_production("sdlc", "bad-branch-unit", "bad-session-unit", "complete-unit")
        text = doc.read_text().replace("| `bad-branch-unit` | (retired", "| — | (retired")
        text = text.replace("session session-bad-session-unit, folder " + str(self.root / "original/bad-session-unit"),
                            "session ")
        write_text_atomic(doc, text)
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual(code, 1, output + errors)
        for name, reason in (("bad-branch-unit", "Branch"), ("bad-session-unit", "session")):
            line = next(index for index, value in enumerate(text.splitlines(), 1) if value.startswith(f"| `{name}` |"))
            self.assertIn(f"{doc}:{line} {name}:", errors)
            self.assertIn(reason, errors)
            self.assertFalse(unit_config.parked_path(unit_membership("sdlc", name).id).exists())
        complete = next(row for row in staff.find(kind="unit") if row.name == "complete")
        self.assertIsInstance(complete.worktree, staff.RemovedWorktree)
        self.assertIsInstance(unit_config.read_parked(complete.id), unit_config.UnitConfig)

    def test_retired_fill_recovers_after_row_and_parked_publication_without_changing_saved_facts(self) -> None:
        _ = self.retired_fill_production("sdlc", "models-unit")
        row = unit_membership("sdlc", "models-unit")
        publisher = unit_config.write_parked
        for after in (False, True):
            def interrupted(identity: staff.StaffId, config: unit_config.UnitConfig) -> None:
                if after:
                    publisher(identity, config)
                raise OSError("stopped at parked publication")
            with mock.patch.object(unit_config, "write_parked", side_effect=interrupted):
                code, output, errors = self.fill_result(retired=True)
            self.assertEqual(code, 1, output + errors)
            self.assertIn("stopped at parked publication", errors)
            current = staff.by_id(row.id)
            assert current is not None
            self.assertIsInstance(current.worktree, staff.RemovedWorktree)
        parked = unit_config.parked_path(row.id)
        before = parked.read_bytes(), parked.stat().st_mtime_ns
        code, output, errors = self.fill_result(retired=True)
        self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
        self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), before)

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
        _ = staff.set_worktree(conflict.id, prior_worktree)
        equal = unit_membership("tools", "equal-unit")
        equal_worktree = staff.PresentWorktree(remote.local_machine(), self.source_root / "worktrees/equal-unit")
        _ = staff.set_worktree(equal.id, equal_worktree)
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
        self.assertFalse((self.source_root / "worktrees/conflict-unit/.unit.toml").exists())
        for name in ("equal-unit", "unfilled-unit"):
            row = unit_membership("tools", name)
            self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), self.source_root / "worktrees" / name))
            self.assertIsInstance(unit_config.read(self.source_root / "worktrees" / name), unit_config.UnitConfig)

    def test_fill_reports_unretired_rows_without_membership_and_never_recreates_retired_members(self) -> None:
        doc = self.fill_production("sdlc", "missing-unit", "retired-unit")
        text = doc.read_text().replace("`retired-unit` | `standby`", "`retired-unit` | retired")
        write_text_atomic(doc, text)
        self.forget_rows(*(unit_membership("sdlc", name).id for name in ("missing-unit", "retired-unit")))
        identities = [row.id for row in staff.snapshot().rows]
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertEqual([row.id for row in staff.snapshot().rows], identities)
        self.assertEqual(staff.find(kind="unit"), [])
        for name in ("missing-unit", "retired-unit"):
            self.assertFalse((self.source_root / "worktrees" / name / ".unit.toml").exists())

    def test_fill_reports_an_invalid_unit_name_and_still_fills_the_following_row(self) -> None:
        doc = self.fill_production("sdlc", "broken-unit", "complete-unit")
        text = doc.read_text().replace("`broken-unit` |", "`invalid/name-unit` |", 1)
        write_text_atomic(doc, text)
        broken = unit_membership("sdlc", "broken-unit")
        code, output, errors = self.fill_result()
        self.assertEqual(code, 1, output)
        self.assertEqual(errors, f"{broken.id} {broken.name}: no verified live session supplies its working folder\n")
        current = staff.by_id(broken.id)
        assert current is not None
        self.assertIsInstance(current.worktree, staff.WorktreeUnfilled)
        complete = unit_membership("sdlc", "complete-unit")
        self.assertEqual(complete.worktree, staff.PresentWorktree(remote.local_machine(), self.source_root / "worktrees/complete-unit"))
        self.assertIsInstance(unit_config.read(self.source_root / "worktrees/complete-unit"), unit_config.UnitConfig)
        self.assertFalse((self.source_root / "worktrees/broken-unit/.unit.toml").exists())

    def test_fill_dry_run_keeps_abandoned_configuration_temporary_files(self) -> None:
        _ = self.fill_production("sdlc", "models-unit")
        folder = self.source_root / "worktrees/models-unit"
        dead = folder / "..unit.toml.999999999.abandoned.tmp"
        write_text_atomic(dead, 'plan = "unfinished')
        before = dead.read_bytes(), dead.stat().st_mtime_ns, self.staff_file.read_bytes()
        code, _, errors = self.fill_result(dry_run=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(dead.exists())
        self.assertEqual((dead.read_bytes(), dead.stat().st_mtime_ns, self.staff_file.read_bytes()), before)
        self.assertFalse((folder / ".unit.toml").exists())

    def test_fill_dry_run_and_apply_both_refuse_a_negated_configuration_exclude(self) -> None:
        doc = self.fill_production("sdlc", "negated-unit")
        folder = self.source_root / "worktrees/negated-unit"
        write_text_atomic(folder / ".gitignore", "!/.unit.toml\n")
        exclude = doc.parents[1] / ".git/info/exclude"
        before = self.staff_file.read_bytes(), exclude.read_bytes()
        code, output, errors = self.fill_result(dry_run=True)
        self.assertEqual(code, 1, output)
        self.assertIn(str(folder / ".unit.toml"), errors)
        self.assertIn("not ignored", errors)
        self.assertEqual((self.staff_file.read_bytes(), exclude.read_bytes()), before)
        code, output, errors = self.fill_result()
        self.assertEqual(code, 1, output)
        self.assertIn("not ignored", errors)
        self.assertIsInstance(unit_membership("sdlc", "negated-unit").worktree, staff.WorktreeUnfilled)
        self.assertFalse((folder / ".unit.toml").exists())

    def test_fill_preview_and_apply_refuse_a_shared_negation_after_an_existing_positive_entry(self) -> None:
        doc = self.fill_production("sdlc", "negated-unit")
        folder = self.source_root / "worktrees/negated-unit"
        exclude = doc.parents[1] / ".git/info/exclude"
        write_text_atomic(exclude, "/.unit.toml\n!/.unit.toml\n")
        owner = unit_membership("sdlc", "negated-unit")
        before = exclude.read_bytes(), exclude.stat().st_mtime_ns
        staff_before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                code, output, errors = self.fill_result(dry_run=dry_run)
                self.assertEqual(code, 1, output)
                self.assertIn(str(folder / ".unit.toml"), errors)
                self.assertIn("not ignored", errors)
                self.assertEqual((exclude.read_bytes(), exclude.stat().st_mtime_ns), before)
                self.assertEqual(staff.by_id(owner.id), owner)
                self.assertFalse((folder / ".unit.toml").exists())
                if dry_run:
                    self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), staff_before)

    def test_fill_writes_live_rows_configs_and_nightly_duties_without_changing_membership(self) -> None:
        environment = {**os.environ,
                       "HERDR_BIN": str(Path(herdr.__file__).with_name("fake_herdr.py")),
                       "HERDR_PROC": str(self.root / "proc"), "HERDR_PLATFORM": "linux",
                       "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
                       "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json")}
        self.write_json(Path(environment["FAKE_HERDR_RESPONSES"]), {"default": {"stdout": "", "code": 1}})
        fake_unit_host.write(environment, [])
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
        doc = self.fill_production("sdlc", "models-unit", "nightly-review-unit", "retired-unit")
        write_text_atomic(doc, doc.read_text().replace("`retired-unit` | `standby`", "`retired-unit` | retired"))
        text = doc.read_text().replace("`models-unit` | `standby` | —", "`models-unit` | `docs/plans/models.md` | 8123")
        write_text_atomic(doc, text)
        record = staff.snapshot()
        before = [(row.id, row.kind, row.name, row.parent) for row in record.rows]
        before_list = [entry.unit for entry in live_units.production_units(doc)]
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshots:
            code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""), output)
        self.assertEqual(snapshots.call_count, 1)
        self.assertIn(str(doc), output)
        self.assertIn(str(doc.parents[1] / ".git"), output)
        runner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        self.assertEqual(runner.doc, staff.LiveDoc(remote.local_machine(), doc))
        for name in ("models-unit", "nightly-review-unit"):
            folder = self.source_root / "worktrees" / name
            row = unit_membership("sdlc", name)
            self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), folder))
            config = unit_config.read(folder)
            assert isinstance(config, unit_config.UnitConfig)
            self.assertEqual(config.responsibilities, ("nightly-review",) if name == "nightly-review-unit" else ())
            if name == "models-unit":
                self.assertEqual(config.plan, unit_config.PlanFile(Path("docs/plans/models.md")))
                self.assertEqual(config.port, unit_config.AppPort(8123))
            self.assertEqual(self.fixture_git(folder, "check-ignore", ".unit.toml"), ".unit.toml")
            self.assertEqual(self.fixture_git(folder, "ls-files", "--", ".unit.toml"), "")
        retired = unit_membership("sdlc", "retired-unit")
        self.assertIsInstance(retired.worktree, staff.WorktreeUnfilled)
        self.assertFalse((self.source_root / "worktrees/retired-unit/.unit.toml").exists())
        self.assertEqual([(row.id, row.kind, row.name, row.parent) for row in staff.snapshot().rows], before)
        self.assertEqual([entry.unit for entry in live_units.production_units(doc)], before_list)

    def test_fill_dry_run_lists_sources_without_changing_staff_excludes_or_config(self) -> None:
        doc = self.fill_production("sdlc", "models-unit")
        exclude = doc.parents[1] / ".git/info/exclude"
        before = self.staff_file.read_bytes(), exclude.read_bytes(), self.staff_file.stat().st_mtime_ns
        code, output, errors = self.fill_result(dry_run=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(str(doc), output)
        self.assertIn(str(self.source_root / "worktrees/models-unit"), output)
        self.assertEqual((self.staff_file.read_bytes(), exclude.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertFalse((self.source_root / "worktrees/models-unit/.unit.toml").exists())

    def test_fill_uses_the_pane_staff_id_after_a_unit_rename(self) -> None:
        _ = self.fill_production("sdlc", "models-unit")
        original = unit_membership("sdlc", "models-unit")
        renamed = staff.rename(original.id, "registry")
        other = fixture_member("showrunner", name="other")
        same_name = fixture_member("unit", name="models", parent=other.id)
        self.pane("w1:models", session_id=original.id, environment={
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

    def test_fill_recovers_after_row_write_and_finished_rerun_changes_no_bytes(self) -> None:
        _ = self.fill_production("sdlc", "models-unit")
        folder = self.source_root / "worktrees/models-unit"
        exclude = Path(self.fixture_git(folder, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
        initial_staff, initial_exclude = self.staff_file.read_bytes(), exclude.read_bytes()
        code, _, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""))
        paths = (self.staff_file, folder / ".unit.toml", exclude)
        clean = [path.read_bytes() for path in paths]
        _ = self.staff_file.write_bytes(initial_staff)
        _ = exclude.write_bytes(initial_exclude)
        (folder / ".unit.toml").unlink()
        with mock.patch.object(unit_config, "write", side_effect=InterruptedError("stop after staff row")):
            code, _, errors = self.fill_result()
        self.assertEqual(code, 1)
        self.assertIn("stop after staff row", errors)
        self.assertIsInstance(unit_membership("sdlc", "models-unit").worktree, staff.PresentWorktree)
        self.assertFalse((folder / ".unit.toml").exists())
        code, _, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(unit_config.read(folder), unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.NoAppPort(), ()))
        self.assertEqual([path.read_bytes() for path in paths], clean)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        code, output, errors = self.fill_result()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("unchanged", output)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_fill_reports_missing_and_conflicting_facts_without_writing_rejected_units(self) -> None:
        doc = self.fill_production("sdlc", "complete-unit", "missing-unit", "wrong-unit", "conflict-unit")
        missing = self.source_root / "worktrees/missing-unit"
        text = doc.read_text().replace(f"`{missing}`", "—").replace("`wrong-unit` | `standby`", "`other-branch` | `standby`")
        write_text_atomic(doc, text)
        duplicate = doc.parent / "other/sdlc-production.md"
        duplicate.parent.mkdir()
        conflicting = text.replace("`conflict-unit` | `standby`", "`conflict-unit` | `permanent`")
        write_text_atomic(duplicate, conflicting)
        absent = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "missing-production"
        absent.mkdir()
        self.write_json(Path(os.environ["SHOWRUNNERS_CONFIG"]), {"always": ["registry-only"]})
        code, output, errors = self.fill_result()
        self.assertEqual(code, 1, output)
        missing_row = unit_membership("sdlc", "missing-unit")
        wrong_row = unit_membership("sdlc", "wrong-unit")
        reports = errors.splitlines()
        self.assertEqual(len(reports), 2)
        self.assertTrue(any(line.startswith(f"{missing_row.id} {missing_row.name}: ")
                            and str(doc) + ":" in line and "Worktree" in line for line in reports))
        self.assertTrue(any(line.startswith(f"{wrong_row.id} {wrong_row.name}: ")
                            and str(doc) + ":" in line and "branch" in line for line in reports))
        self.assertNotIn("missing-production", errors)
        self.assertNotIn("registry-only", errors)
        for name in ("complete-unit", "conflict-unit"):
            folder = self.source_root / "worktrees" / name
            row = unit_membership("sdlc", name)
            self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), folder))
            config = unit_config.read(folder)
            assert isinstance(config, unit_config.UnitConfig)
            self.assertEqual(config.plan, unit_config.StandbyPlan())
        for name in ("missing-unit", "wrong-unit"):
            row = unit_membership("sdlc", name)
            self.assertIsInstance(row.worktree, staff.WorktreeUnfilled)
            self.assertFalse((self.source_root / "worktrees" / name / ".unit.toml").exists())

    def test_fill_reads_branch_tip_and_ignored_production_docs_and_skips_wrapped_ones(self) -> None:
        doc = self.fill_production("sdlc", "models-unit")
        repository = doc.parents[1]
        archived = doc.parent / "branch-only-production.md"
        write_text_atomic(archived, "# Branch only\n- **Showrunner session:** `branch-only`\n\n## Units\n\n| Unit | Worktree | Branch | Plan |\n| --- | --- | --- | --- |\n")
        _ = fixture_member("showrunner", name="branch-only")
        _ = self.fixture_git(repository, "add", str(archived))
        _ = self.fixture_git(repository, "commit", "-m", "production at branch tip")
        ignored = doc.parent / "style-production.md"
        write_text_atomic(repository / ".git/info/exclude", "/docs/style-production.md\n")
        write_text_atomic(ignored, doc.read_text().replace("`sdlc`", "`style`").split("| `models-unit`")[0])
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
        before = (tracked / ".unit.toml").read_bytes()
        code, _, errors = self.fill_result()
        self.assertEqual(code, 1)
        self.assertIn("tracked", errors)
        self.assertIn("ignored", errors)
        for name in ("tracked-unit", "negated-unit"):
            self.assertIsInstance(unit_membership("sdlc", name).worktree, staff.WorktreeUnfilled)
        self.assertEqual((tracked / ".unit.toml").read_bytes(), before)
        self.assertFalse((negated / ".unit.toml").exists())

    def test_serverless_delegate_skips_its_roster_and_counts_once_in_worker_fill(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={"STAFF_ID": unit.id})
        delegate_root = self.source_root / "delegate"
        with (mock.patch.object(unit_runs, "FORMER_RUN_ROOT", delegate_root),
              mock.patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(delegate_root)})):
            run = self.worker_run("sdlc", "models-unit", "session-models")
            (run / "mesh_server.json").unlink()
            roster = self.roster_worker(run, "delegate", "", "thread-paused", status="paused")
            entry = cast(dict[str, dict[str, object]], json.loads(roster.read_text()))
            missing_role = {"delegate": {key: value for key, value in entry["delegate"].items() if key != "role"}}
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            for contents in (roster.read_text(), json.dumps(missing_role), "{broken roster"):
                with self.subTest(roster=contents):
                    write_text_atomic(roster, contents)
                    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                        code = worker_fill.fill()
                    self.assertEqual((code, output.getvalue(), errors.getvalue()),
                                     (0, "skipped 1 not current\nunchanged\n", ""))
                    self.assertEqual(staff.find(kind="worker"), [])
                    self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_serverless_delegate_held_by_ledger_adds_no_worker_and_counts_once(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={"STAFF_ID": unit.id})
        delegate_root = self.source_root / "delegate"
        with (mock.patch.object(unit_runs, "FORMER_RUN_ROOT", delegate_root),
              mock.patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(delegate_root)})):
            run = self.worker_run("sdlc", "models-unit", "session-models")
            _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "thread-held")
            _ = self.roster_worker(run, "test", "test", "thread-held", status="paused")
            (run / "mesh_server.json").unlink()
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                code = worker_fill.fill()
            self.assertEqual((code, output.getvalue(), errors.getvalue()),
                             (0, "skipped 1 not current\nunchanged\n", ""))
            self.assertEqual(staff.find(kind="worker"), [])
            self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_serverless_unit_keeps_uncertain_worker_evidence_out_of_the_skip_count(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        run = self.worker_run("sdlc", "models-unit", "gone-director")
        (run / "mesh_server.json").unlink()
        self.write_json(run / "mesh_roster.json", {
            "test": {"role": "test", "status": "paused", "family": "codex", "launcher_pid": 0},
        })
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker_fill.fill()
        self.assertEqual(code, 1, output.getvalue() + errors.getvalue())
        self.assertIn(str(run), errors.getvalue())
        self.assertIn("source unavailable", errors.getvalue())
        self.assertNotIn("skipped", output.getvalue())
        self.assertEqual(staff.find(kind="worker"), [])

    def test_paused_roster_workers_survive_a_director_restart_or_a_missing_messaging_socket(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        registry = self.session_record(unit.id, "models-unit")
        self.assertEqual(json.loads(registry.read_text())["messagingSocketPath"], "")
        self.pane("w1:models", session_id=unit.id, environment={"STAFF_ID": unit.id})
        run = self.worker_run("sdlc", "models-unit", "ec4e4c1d-6ca3-5651-9d48-74452fc51acc")
        self.write_json(run / "mesh_server.json", {"pid": 0, "codex_home": str(self.root / "codex")})
        roster = self.roster_worker(run, "test", "test", "71280e34-5ba9-5e39-b979-05acf27f3f6a", status="paused")
        history = self.root / "shutdown/account/history/restart.json"
        self.write_json(history, {"session": {
            "session_id": "ec4e4c1d-6ca3-5651-9d48-74452fc51acc", "name": "models-unit", "run_dirs": [str(run)],
            "host": {"kind": "unit", "unit": "models-unit", "session_name": "models-unit"},
        }})
        with mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")}):
            for director in ("ec4e4c1d-6ca3-5651-9d48-74452fc51acc", unit.id):
                with self.subTest(director=director):
                    write_text_atomic(run / "director", director + "\n")
                    with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                        _ = self.seed()
                    self.assertEqual(reads.call_count, 1)
                    rows = staff.find(kind="worker", parent=unit.id)
                    self.assertEqual([row.name for row in rows], ["test"])
                    with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                        value = staff_sources.live()
                    self.assertEqual(reads.call_count, 1)
                    members = cast(list[dict[str, object]], value["members"])
                    worker = next(member for member in members if member["id"] == rows[0].id)
                    self.assertEqual(worker["parent"], unit.id)
                    self.assertEqual(worker["identity"], {"codex": "71280e34-5ba9-5e39-b979-05acf27f3f6a"})
                    self.assertEqual(worker["role"], "test")
                    self.assertIn(str(roster), cast(list[str], value["inputs"]))
                    if director == "ec4e4c1d-6ca3-5651-9d48-74452fc51acc":
                        self.assertIn(str(history), cast(list[str], value["inputs"]))

    def test_legacy_claude_workers_join_board_slots_by_registered_mesh_name(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        for index, (prefix, kind, role) in enumerate((
            ("hana_catalyst", "impl", "test"), ("3D.Project-explicit", "fix", "fix"),
            ("unassigned", "impl", ""),
        )):
            with self.subTest(prefix=prefix, kind=kind, role=role):
                run = self.worker_run("sdlc", f"legacy-{index}", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
                session_id = fixture_runtime_id(f"legacy-worker:{index}")
                _ = self.session_record(session_id, "Claude worker")
                name = self.legacy_claude_worker(run, session_id, prefix=prefix, kind=kind, role=role)
                self.assertEqual(list(run.glob("impl_worker_*.json")), [])
                _, problems = self.seed()
                row = next((row for row in staff.find(kind="worker") if row.name == name), None)
                with redirect_stderr(io.StringIO()):
                    members = cast(list[dict[str, object]], staff_sources.live()["members"])
                if role:
                    self.assertIsNotNone(row, problems)
                    assert row is not None
                    member = next(member for member in members if member["id"] == row.id)
                    self.assertEqual(member["name"], name)
                    self.assertEqual(member["role"], role)
                    self.assertEqual(member["identity"], {"claude": session_id})
                else:
                    self.assertIsNone(row)
                    self.assertIn(str(run), problems)
                    self.assertFalse(any(member["name"] == name for member in members))

    def test_live_inputs_keep_rejected_production_documents_and_read_them_after_correction(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        rejected = self.production("recovering", "later-unit")
        _ = self.session_record("8e2a7bad-d715-5c2d-9ec5-2a8bf9a81289", "recovering")
        _ = self.seed()
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


    def test_finished_and_adopted_rosters_cannot_override_the_live_ledger_worker(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "review", "review", "e63a4770-62e8-5270-91f0-a163996415b9")
        _ = self.roster_worker(run, "review", "review", "e63a4770-62e8-5270-91f0-a163996415b9")
        for number, status in enumerate(("done", "adopted"), start=1):
            old_run = self.worker_run("sdlc", f"history-{status}", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
            _ = self.roster_worker(old_run, "review", "review", f"thread-{status}", status=status)
            _ = self.roster_worker(old_run, f"fix{number}", "fix", f"thread-only-{status}", status=status)
        gone = self.worker_run("sdlc", "history-gone", "3790170d-f8c8-5dbd-ba9b-38eb5179177f")
        self.write_json(gone / "progress_history_state.json", {
            "production": "sdlc", "unit": "models-unit",
            "main_agent": {"family": "claude", "session_id": "3790170d-f8c8-5dbd-ba9b-38eb5179177f"},
        })
        self.write_json(gone / "mesh_server.json", {"pid": 0, "codex_home": str(self.root / "codex")})
        _ = self.roster_worker(gone, "fix3", "fix", "e4481fcb-8cb1-560b-b68f-82663434e61a")
        dead_roster = cast(dict[str, dict[str, object]], json.loads((gone / "mesh_roster.json").read_text()))
        dead_roster["fix3"]["launcher_pid"] = 0
        self.write_json(gone / "mesh_roster.json", dead_roster)
        _, errors = self.seed()
        self.assertNotIn("conflicting worker identities", errors)
        workers = staff.find(kind="worker")
        self.assertEqual([row.name for row in workers], ["review"])
        value = staff_sources.live()
        members = cast(list[dict[str, object]], value["members"])
        reviewer = next(member for member in members if member["id"] == workers[0].id)
        self.assertEqual(reviewer["identity"], {"codex": "e63a4770-62e8-5270-91f0-a163996415b9"})
        self.assertIn(str(ledger), cast(list[str], value["inputs"]))

    def test_adhoc_ledger_of_a_dot_named_worktree_takes_its_holder_run_without_a_name_error(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        # A worktree named `.claude` gives the adhoc ledger stem `.claude-<digest>`.
        _ = self.ledger_worker(run, "adhoc", ".claude-3314ec0bf589", "review", "review", "0783462b-323d-5d81-b61c-ddee74b529d6")
        _, errors = self.seed()
        self.assertNotIn("name must contain", errors)
        with redirect_stderr(io.StringIO()) as live_errors:
            members = cast(list[dict[str, object]], staff_sources.live()["members"])
        self.assertNotIn("name must contain", live_errors.getvalue())
        unit = next(member for member in members if member["kind"] == "unit")
        reviewer = next(member for member in members if member["kind"] == "worker")
        self.assertEqual((reviewer["name"], reviewer["parent"]), ("review", unit["id"]))
        self.assertEqual(reviewer["identity"], {"codex": "0783462b-323d-5d81-b61c-ddee74b529d6"})

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

    def test_live_workers_use_their_producers_without_invented_unit_ledger_entries(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        roster = self.roster_worker(run, "impl", "fix", "936663e1-4213-5af9-b170-de47f5a9e149")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        design = self.worker_design("sdlc", "sdlc-handler-astra-designer", "3d1d0e20-5129-55bd-9516-9b9e8d633777")
        self.assertEqual(list(Path(os.environ["UNIT_WORKERS_STATE"]).rglob("*.json")), [])
        _ = self.seed()
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        workers = {cast(str, member["name"]): member for member in members if member["kind"] == "worker"}
        self.assertEqual(set(workers), {"impl", "sdlc-handler-astra-reviewer", "sdlc-handler-astra-designer"})
        self.assertEqual(workers["impl"]["role"], "fix")
        for name, thread in (("impl", "936663e1-4213-5af9-b170-de47f5a9e149"),
                             ("sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd"),
                             ("sdlc-handler-astra-designer", "3d1d0e20-5129-55bd-9516-9b9e8d633777")):
            self.assertEqual(workers[name]["identity"], {"codex": thread})
        root = next(member for member in members if member["name"] == "sdlc")
        self.assertEqual(workers["sdlc-handler-astra-reviewer"]["parent"], root["id"])
        inputs = cast(list[str], staff_sources.live()["inputs"])
        for path in (roster, astra, design):
            self.assertIn(str(path), inputs)

    def test_unreadable_astra_owner_does_not_seed_a_permanent_unit_owned_duplicate(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        name = "sdlc-handler-astra-reviewer"
        _ = self.ledger_worker(run, "sdlc", "models-unit", name, "review", "1e49a002-64a8-51fb-8291-2828649967bd")
        path = self.astra_worker(run, name, "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        original = path.read_text()
        write_text_atomic(path, "{broken")
        _, errors = self.seed()
        self.assertIn(str(path), errors)
        self.assertEqual(staff.find(kind="worker"), [])
        write_text_atomic(path, original)
        _ = self.seed()
        root = staff.by_path("sdlc")
        assert root is not None
        rows = staff.find(kind="worker")
        self.assertEqual([(row.name, row.parent) for row in rows], [(name, root.id)])
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)


    def test_live_inputs_include_the_remote_owner_record_and_exclude_process_and_system_reads(self) -> None:
        _ = self.tree()
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                if machine == "mac":
                    self.assertNotIn("STAFF_FILE", os.environ)
                _ = self.production("sdlc", "models-unit")
                _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(self.staff_file), inputs)
                self.assert_no_system_inputs(inputs)
        self.assertEqual(len(self.ssh_commands()), 1)

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

    def test_worker_seed_imports_ledger_and_roster_once_and_keeps_replacement_identity(self) -> None:
        _ = self.production("sdlc", "models-unit")
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "ebd83cc0-0d36-5908-86b5-d3e5a4277b05")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf", family="claude")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "fix3", "fix", "d0db35da-1fdb-52f7-8a8d-34398d8f13b3", retired=True)
        roster_path = run / "mesh_roster.json"
        roster = cast(dict[str, object], json.loads(roster_path.read_text()))
        roster["review2"] = {"role": "review", "status": "paused", "thread_id": "936663e1-4213-5af9-b170-de47f5a9e149"}
        self.write_json(roster_path, roster)
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads, \
                mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as writes:
            _ = self.seed()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(writes.call_count, 1)
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        workers = {row.name: row for row in staff.find(kind="worker", parent=unit.id)}
        self.assertEqual(set(workers), {"impl", "test", "review2"})
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        document = cast(dict[str, object], json.loads(ledger.read_text()))
        entries = cast(dict[str, dict[str, object]], document["workers"])
        backend = cast(dict[str, object], entries["impl"]["backend"])
        backend["thread_id"] = "4362efed-e9f3-50e0-b02e-8c33b50288b9"
        self.write_json(ledger, document)
        _ = self.seed()
        self.assertEqual(staff.by_id(workers["impl"].id), workers["impl"])
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_worker_seed_names_each_unreadable_source_instead_of_silently_skipping_it(self) -> None:
        _ = self.production("sdlc", "models-unit")
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        _ = self.seed()
        for path in (ledger, run / "mesh_roster.json", astra):
            original = path.read_text()
            with self.subTest(path=path):
                write_text_atomic(path, "{broken")
                with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                    try:
                        staff_sources.seed()
                    except staff.StaffUnreadable as error:
                        print(error, file=sys.stderr)
                self.assertIn(str(path), errors.getvalue())
                self.assertTrue(errors.getvalue().strip())
                write_text_atomic(path, original)

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

    def test_live_staff_command_keeps_worker_identity_and_all_input_paths(self) -> None:
        doc = self.production("sdlc", "models-unit")
        self.pane("w1:runner", session_id="cdc4420c-ae5d-5a9a-8135-75e8c0875088", environment={})
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        registry = [self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc"),
                    self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit"),
                    self.session_record("0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf", "test")]
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf", family="claude")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        _ = self.seed()
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(staff.main(["show", "--json", "--live"]), 0)
        self.assertEqual(reads.call_count, 1)
        document = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(set(document), {"schema", "inputs", "members", "complete", "unregistered"})
        self.assertEqual(document["complete"], True)
        self.assertEqual(document["unregistered"], [])
        self.assertEqual(document["schema"], 3)
        members = cast(list[dict[str, object]], document["members"])
        by_name = {cast(str, member["name"]): member for member in members}
        by_identity = {tuple(cast(dict[str, str], member["identity"]).items()): member for member in members}
        root = by_name["sdlc"]
        unit = by_name["models"]
        self.assertEqual(root["identity"], {"claude": "cdc4420c-ae5d-5a9a-8135-75e8c0875088"})
        self.assertEqual(unit["parent"], root["id"])
        self.assertEqual(unit["identity"], {"claude": "bae0b3d7-3d25-5a1f-9630-b7991a2f9721"})
        for name, identity in (("impl", {"codex": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b"}), ("test", {"claude": "0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf"})):
            self.assertEqual(by_name[name]["parent"], unit["id"])
            self.assertEqual(by_name[name]["identity"], identity)
        designer = by_identity[(("codex", "1e49a002-64a8-51fb-8291-2828649967bd"),)]
        self.assertEqual(designer["parent"], root["id"])
        self.assertEqual(designer["role"], "astra-reviewer")
        ids = {member["id"] for member in members}
        self.assertEqual(len(ids), len(members))
        for member in members:
            self.assertEqual(set(member), {"id", "kind", "name", "parent", "current_session", "role", "identity"})
            self.assertIn(member["role"], {"showrunner", "unit", "impl", "test", "review", "fix", "astra-designer", "astra-reviewer"})
            self.assertTrue(member["parent"] in ids or member is root)
            self.assertEqual(member["parent"] is None, member is root)
            self.assertEqual(len(cast(dict[str, str], member["identity"])), 1)
        inputs = cast(list[str], document["inputs"])
        self.assertTrue(all(Path(path).is_absolute() for path in inputs))
        self.assertEqual(len(inputs), len(set(inputs)))
        for path in (self.staff_file, doc, ledger, astra, run / "mesh_roster.json", *registry,
                     Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc" / "conf",
                     pane_record.record_path(herdr.shared_session())):
            self.assertIn(str(path), inputs)
        self.assertEqual(self.staff_file.read_bytes(), before)
        ledger_value = cast(dict[str, object], json.loads(ledger.read_text()))
        workers = cast(dict[str, dict[str, object]], ledger_value["workers"])
        cast(dict[str, object], workers["impl"]["backend"])["thread_id"] = "4362efed-e9f3-50e0-b02e-8c33b50288b9"
        self.write_json(ledger, ledger_value)
        replacement_id = staff.native_id("4362efed-e9f3-50e0-b02e-8c33b50288b9")
        _ = staff.update_current_session(staff.StaffId(cast(str, by_name["impl"]["id"])), replacement_id)
        published = self.staff_file.read_bytes()
        replacement = cast(dict[str, object], json.loads(self.cli("show", "--json", "--live").stdout))
        replacement_members = cast(list[dict[str, object]], replacement["members"])
        replaced = next(member for member in replacement_members if member["id"] == by_name["impl"]["id"])
        self.assertEqual(replaced["identity"], {"codex": replacement_id})
        self.assertEqual(replaced["current_session"], replacement_id)
        self.assertNotIn(replacement_id, {member["id"] for member in replacement_members})
        self.assertEqual({member["id"]: member["parent"] for member in replacement_members},
                         {member["id"]: member["parent"] for member in members})
        self.assertEqual(self.staff_file.read_bytes(), published)


    def test_seed_preserves_recorded_moves_despite_stale_sources(self) -> None:
        old = fixture_member("showrunner", name="old")
        new = fixture_member("showrunner", name="new")
        child = fixture_member("showrunner", name="tools", parent=old.id)
        unit = fixture_member("unit", name="models", parent=old.id)
        worker = fixture_member("worker", name="impl1", parent=unit.id)
        _ = self.production("old", "models-unit")
        _ = self.production("new")
        _ = self.production("child", workspace="tools", parent="old")
        moved_unit = staff.move(unit.id, new.id)
        moved_child = staff.move(child.id, new.id)
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
        self.assertIn("models", first.stdout)
        root = staff.by_path("umbrella")
        child = staff.by_path("tools")
        unit = staff.by_path("tools/models")
        assert root is not None and child is not None and unit is not None
        self.assertIsNone(child.parent)
        self.assertEqual(unit.parent, child.id)
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
        receiving = fixture_member("showrunner", name="receiving")
        moved = staff.move(unit.id, receiving.id)
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
        receiving = fixture_member("showrunner", name="control")
        moved = staff.move(unit.id, receiving.id)
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
        receiving = fixture_member("showrunner", name="receiving")
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        other = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="models", parent=other.id)
        self.assertEqual(staff_sources.member(staff_sources.ObservedUnit("models-unit", unit.id), staff.snapshot()),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", unit.id),
                         staff_sources.UnitProduction("receiving", str(new_doc)))

    def test_metadata_from_the_current_parent_wins_over_an_old_same_name_row(self) -> None:
        _original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving", "models-unit")
        receiving = fixture_member("showrunner", name="receiving")
        _ = staff.move(unit.id, receiving.id)
        new_text = new_doc.read_text().replace(str(self.source_root / "worktrees" / "models-unit"),
                                              str(self.source_root / "current-worktree")).replace("active", "current-plan.md")
        write_text_atomic(new_doc, new_text)
        expected = production_units.unit_table(new_text.splitlines())[0]
        actual = live_unit_table(new_text.splitlines(), "receiving")
        self.assertEqual([(row["Worktree"], row["Plan"]) for row in actual],
                         [(expected["Worktree"], expected["Plan"])])
        self.assertNotEqual(expected["Worktree"], production_units.unit_table(old_doc.read_text().splitlines())[0]["Worktree"])

    def test_competing_orphan_metadata_is_unavailable_instead_of_picking_a_document(self) -> None:
        _original, unit, _worker = self.tree()
        for slug in ("sdlc", "former"):
            document = self.production(slug, "models-unit")
            write_text_atomic(document, document.read_text().replace("active", f"{slug}-plan.md"))
        new_doc = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving")
        _ = staff.move(unit.id, receiving.id)
        with redirect_stderr(io.StringIO()) as errors:
            rows = live_unit_table(new_doc.read_text().splitlines(), "receiving")
        self.assertEqual(len(rows), 1)
        self.assertEqual(production_units.cell_value(rows[0]["Worktree"]), "—")
        self.assertEqual(production_units.cell_value(rows[0]["Plan"]), "—")
        self.assertRegex(errors.getvalue(), "(?i)models.*(ambiguous|unavailable)|(ambiguous|unavailable).*models")

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
            receiving = fixture_member("showrunner", name="receiving")
            _ = staff.move(unit.id, receiving.id)
            self.pane("w1:unit", session_id=unit.id, environment={
                "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
            })
            before = len(self.ssh_commands())
            parent = staff_sources.unit_parent("models-unit", unit.id)
            self.assertEqual(parent.slug, "receiving")
            self.assertEqual(len(self.ssh_commands()) - before, 1)

    def test_transferred_membership_keeps_worktree_and_plan_metadata_from_the_old_doc(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving")
        _ = staff.move(unit.id, receiving.id)
        record = staff.snapshot()
        old_lines, new_lines = old_doc.read_text().splitlines(), new_doc.read_text().splitlines()
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("second owner read")):
            old_rows = live_unit_table(old_lines, "sdlc", record=record)
            new_rows = live_unit_table(new_lines, "receiving", record=record)
            retired = retired_units(old_lines, "sdlc", record=record)
            old_marks = ProductionMembers("sdlc", record=record)
            new_marks = ProductionMembers("receiving", record=record)
            cells = production_units.unit_table(old_lines)[0]
            self.assertFalse(unit_is_retired(cells, old_marks))
            self.assertFalse(unit_is_retired(cells, new_marks))
        self.assertEqual(old_rows, [])
        self.assertEqual(len(new_rows), 1)
        self.assertEqual(production_units.cell_value(new_rows[0]["Unit"]), "models-unit")
        self.assertEqual(new_rows[0]["Worktree"], cells["Worktree"])
        self.assertEqual(new_rows[0]["Plan"], cells["Plan"])
        self.assertEqual(retired, set())
        self.assertEqual(record.find(kind="unit", parent=original.id), [])

    def test_unit_parent_agrees_with_showrunner_members_after_transfer(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit", workspace="sdlc")
        new_doc = self.production("receiving", workspace="new-tools")
        destination = fixture_member("showrunner", name="new-tools")
        destination = staff.rename(destination.id, "control")
        self.pane("w1:showrunner", session_id=destination.id, environment={"STAFF_ID": destination.id})
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        for slug, expected_parent, doc in (("sdlc", original, old_doc), ("receiving", destination, new_doc)):
            with self.subTest(slug=slug):
                _ = staff.move(unit.id, expected_parent.id)
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
                self.assertEqual(members[parent.slug].id, recorded_unit.parent)
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
        _ = self.production("owner-only", "owner-unit")
        with self.machine("mac"):
            _ = self.production("mac-only", "laptop-unit", workspace="laptop")
            self.pane("w1:p1", session_id="session-mac-only", environment={})
            _ = self.seed()
            self.assertFalse(self.mac_file.exists())
            self.assertTrue((self.mac_file.parent / "operation.lock").exists())
        self.assertIsNotNone(staff.by_path("laptop/laptop"))
        self.assertIsNone(staff.by_path("owner-only"))
        self.assertEqual({row.name for row in staff.find()}, {"laptop"})
        self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"], ["apply-seed", "--json"]])
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
                        doc = self.production("sdlc")
                        self.units_doc(doc, ("alpha-unit", "beta-unit"), workspace="sdlc")
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
        doc = self.production("sdlc", "models-unit", "empty-unit")
        self.units_doc(doc, ("models-unit", "empty-unit", "remote-unit"), workspace="sdlc", retired=("models-unit", "empty-unit"))
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
        _ = self.production("sdlc", "models-unit", "new-unit")
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as apply:
            output, _errors = self.seed(dry_run=True)
        apply.assert_not_called()
        self.assertIn("new", output)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_path("sdlc/new"))

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
        before = self.write_document({"schema": 3, "rows": []})
        _ = self.production("sdlc", "models-unit")
        failed = self.script(SOURCES_SCRIPT, "seed", code=2)
        self.assertTrue(failed.stderr.strip())
        self.assertNotIn("Traceback", failed.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_waits_for_lifecycle_operation_then_reads_updated_docs(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        old_doc = self.production("old", "models-unit")
        new_doc = self.production("new")
        _ = self.seed()
        unit = staff.by_path("old/models")
        destination = staff.by_path("new")
        assert unit is not None and destination is not None
        lock = self.staff_file.parent / "operation.lock"
        with self.lock_notice(lock) as listener:
            with staff.operation():
                moved = staff.move(unit.id, destination.id)
                pending = self.start_script(SOURCES_SCRIPT, "seed")
                self.assert_operation_blocks(pending, listener)
                self.units_doc(old_doc, (), workspace="old")
                self.units_doc(new_doc, ("models-unit",), workspace="new")
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
        self.assertEqual(record.by_id(second.id), replace(renamed, parent=first.id))
        invalid = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.SeedParent("second")),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff.apply_seed(invalid)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_keeps_a_renamed_unit_whose_identity_is_only_on_the_other_machine(self) -> None:
        showrunner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="beta", parent=showrunner.id)
        renamed = staff.rename(unit.id, "panes")
        doc = self.production("sdlc")
        self.units_doc(doc, ("beta-unit",), workspace="sdlc")
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
        _ = staff.move(child.id, parent.id)
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
        self.assertEqual(errors, "")

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
        moved = staff.move(unit.id, new.id)
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


if __name__ == "__main__":
    _ = unittest.main()
