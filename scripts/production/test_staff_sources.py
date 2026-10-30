"""Seed real fixture sources without losing identity or another machine's staff."""
from __future__ import annotations

import io
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, staff_sources
from scripts.production.staff_test_support import SOURCES_SCRIPT, FixtureMachine, StaffTestCase
from scripts.shutdown import pane_record
import herdr
import showrunners
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class StaffSourcesTests(StaffTestCase):
    def seed(self, *, dry_run: bool = False) -> tuple[str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            staff_sources.seed(dry_run=dry_run)
        return output.getvalue(), errors.getvalue()

    def test_seed_rerun_uses_timers_parent_files_docs_and_pane_records(self) -> None:
        _ = self.production("parent", workspace="umbrella")
        _ = self.production("sdlc", "models-unit", workspace="tools", parent="parent")
        self.pane("w1:p1", session_id="session-sdlc", environment={})
        self.pane("w1:p2", session_id="unit-session", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
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
        self.assertEqual(child.parent, root.id)
        self.assertEqual(unit.parent, child.id)
        self.assertEqual(len(staff.find()), 3)
        self.assertEqual(staff.find(kind="worker"), [])

    def test_seed_keeps_renamed_unit_and_showrunner_by_pane_staff_ids(self) -> None:
        showrunner, unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        renamed_unit = staff.rename(unit.id, "panes")
        renamed_showrunner = staff.rename(showrunner.id, "control")
        self.pane("w1:p1", session_id="session-sdlc", environment={"STAFF_ID": showrunner.id})
        self.pane("w1:p2", session_id="models-session", environment={
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
        fallback = staff.ensure("unit", name="models", parent=showrunner.id)
        self.pane("w1:old", session_id="old-unit", written_at="2026-10-29T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:new", session_id="new-unit", environment={
            "STAFF_ID": renamed.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:other", session_id="other-unit", written_at="2026-10-31T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "other", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        result = staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), staff.snapshot())
        self.assertEqual(result, staff_sources.MemberFound(renamed))

    def test_member_with_no_staff_id_matches_unit_by_short_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        self.pane("w1:p2", session_id="unit-session", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        record = staff.snapshot()
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))
        self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))

    def test_member_with_unresolved_or_wrong_kind_id_does_not_take_an_unrelated_name(self) -> None:
        showrunner, _unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for staff_id in ("s00000000000", showrunner.id, worker.id):
            with self.subTest(staff_id=staff_id):
                self.pane("w1:p2", session_id="unit-session", environment={
                    "STAFF_ID": staff_id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
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
                showrunner = staff.ensure("showrunner", name="sdlc")
                alpha = staff.ensure("unit", name="alpha", parent=showrunner.id)
                beta = staff.ensure("unit", name="beta", parent=showrunner.id)
                alpha_worker = staff.ensure("worker", name="impl1", parent=alpha.id)
                beta_worker = staff.ensure("worker", name="impl1", parent=beta.id)
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
        empty = staff.ensure("unit", name="empty", parent=showrunner.id)
        omitted = staff.ensure("unit", name="omitted", parent=showrunner.id)
        remote = staff.ensure("unit", name="remote", parent=showrunner.id)
        unregistered = staff.ensure("showrunner", name="other-machine")
        doc = self.production("sdlc", "models-unit", "empty-unit")
        self.units_doc(doc, ("models-unit", "empty-unit", "remote-unit"), workspace="sdlc", retired=("models-unit", "empty-unit"))
        _output, errors = self.seed()
        self.assertIsNone(staff.by_id(empty.id))
        self.assertIsNone(staff.by_id(omitted.id))
        self.assertEqual(staff.by_id(remote.id), remote)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(staff.by_id(unregistered.id), unregistered)
        self.assertEqual(len([line for line in errors.splitlines() if "models" in line]), 1, errors)

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
        before = self.write_document({"schema": 2, "rows": []})
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
        first = staff.ensure("showrunner", name="first")
        second = staff.ensure("showrunner", name="second")
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
        showrunner = staff.ensure("showrunner", name="sdlc")
        unit = staff.ensure("unit", name="beta", parent=showrunner.id)
        renamed = staff.rename(unit.id, "panes")
        doc = self.production("sdlc")
        self.units_doc(doc, ("beta-unit",), workspace="sdlc")
        with self.machine("mac"):
            _ = self.production("sdlc", "beta-unit")
            self.pane("w1:beta", session_id="beta-session", environment={
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
        showrunner = staff.ensure("showrunner", name="sdlc")
        _ = staff.ensure("unit", name="old", parent=showrunner.id)
        _ = self.production("sdlc", "new-unit")
        session = herdr.shared_session()
        path = pane_record.record_path(session)
        before = self.staff_file.read_bytes()
        valid: dict[str, object] = {
            "session_id": "session-sdlc", "environment": {}, "written_at": "2026-10-30T09:00:00+00:00",
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
        parent = staff.ensure("showrunner", name="remote")
        child = staff.ensure("showrunner", name="local", parent=parent.id)
        _ = self.production("local", parent="remote")
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_roots_a_showrunner_only_when_parent_evidence_says_root(self) -> None:
        parent = staff.ensure("showrunner", name="parent")
        child = staff.ensure("showrunner", name="child", parent=parent.id)
        _ = self.production("parent", parent="child")
        _ = self.production("child", parent="parent")
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        self.assertEqual(staff.by_id(parent.id), parent)
        _ = staff.move(child.id, parent.id)
        parent_file = showrunners.SHOWRUNNER_STATE_DIR / "child" / showrunners.PARENT_FILE
        parent_file.unlink()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))

    def test_unreadable_parent_evidence_leaves_its_production_unchanged(self) -> None:
        showrunner = self.tree()[0]
        _ = self.production("sdlc", "models-unit", "new-unit")
        path = showrunners.SHOWRUNNER_STATE_DIR / "sdlc" / showrunners.PARENT_FILE
        path.mkdir(parents=True)
        before = self.staff_file.read_bytes()
        with self.assertRaises(OSError):
            _ = showrunners.stored_parent("sdlc")
        self.assertEqual(showrunners.parent_of("sdlc"), "")
        _output, errors = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(showrunner.id), showrunner)
        self.assertEqual(len([line for line in errors.splitlines() if str(path) in line]), 1, errors)

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
        for slug in ("one", "two"):
            paths.append(showrunners.SHOWRUNNER_STATE_DIR / slug / showrunners.PARENT_FILE)
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
        self.assertEqual(len([line for line in errors.splitlines() if str(bad) in line]), 1, errors)
        for path in ("one/alpha", "one/beta", "two/gamma"):
            self.assertIsNotNone(staff.by_path(path))

    def test_an_unreadable_timer_does_not_hide_other_productions(self) -> None:
        old = staff.ensure("showrunner", name="bad")
        old_unit = staff.ensure("unit", name="kept", parent=old.id)
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
        with mock.patch.object(showrunners, "socket_for", side_effect=failure):
            with self.assertRaises(OSError) as raised:
                _ = showrunners.registered_showrunners()
        self.assertIs(raised.exception, failure)

    def test_a_stale_mac_retirement_keeps_a_unit_transferred_since_the_snapshot(self) -> None:
        old = staff.ensure("showrunner", name="old")
        new = staff.ensure("showrunner", name="new")
        unit = staff.ensure("unit", name="models", parent=old.id)
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
        parent = staff.ensure("showrunner", name="sdlc")
        unit = staff.ensure("unit", name="models", parent=parent.id)
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
