"""Native-id migration finishes each store before staff and replays interruptions."""
from __future__ import annotations

from functools import cached_property
from dataclasses import dataclass

from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
from typing import cast, override
import unittest
from unittest import mock
from types import SimpleNamespace

from scripts.production import completion_receipt, migrate_staff_ids, rename_state, showrunners, staff, staff_sources, unit_config
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id
from scripts.message import roster, sessions


@dataclass
class MigrationFixture:
    document: Path
    repository: Path
    old_owner: staff.StaffId
    old_unit: staff.StaffId
    native_owner: staff.StaffId
    native_unit: staff.StaffId
    owner: staff.StaffRow
    unit: staff.StaffRow
    configuration: unit_config.UnitConfig
    reference: Path
    tip: str


class NativeMigrationTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.fixture

    @cached_property
    def fixture(self) -> MigrationFixture:
        _ = self.enterContext(mock.patch.object(showrunners, "SCRATCH_ROOT", self.root / "scratch"))
        document = self.fill_production("demo")
        repository = document.parents[1]
        old_owner = staff.StaffId("s00000000001")
        old_unit = staff.StaffId("s00000000002")
        native_owner = staff.StaffId(fixture_runtime_id("migration-Claude-owner"))
        native_unit = staff.StaffId(fixture_runtime_id("migration-Claude-unit"))
        owner = staff.StaffRow(old_owner, "showrunner", "demo", None,
                                   doc=staff.LiveDoc("natedev", document), current_session=old_owner)
        unit = staff.StaffRow(old_unit, "unit", "models", old_owner,
                                  staff.RemovedWorktree("natedev", self.root / "gone"), current_session=old_unit)
        _ = self.write_document({"schema": 2, "rows": [owner.to_json(), unit.to_json()]})
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-demo" / "conf"
        target = showrunners.read_bound_source(timer.parent, document).timer.target
        _ = timer.write_text(timer.read_text().replace("TARGET=" + target, "TARGET=session:" + native_owner))
        configuration = unit_config.UnitConfig(unit_config.StandbyPlan(), unit_config.AppPort(8123), ("nightly",),
            unit_config.RetiredSession("2026-10-31", native_unit, unit_config.Branch("models")))
        _ = unit_config.write_parked(old_unit, configuration)
        reference = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "demo" / "rename.json"
        self.write_json(reference, {"old": "former", "new": "demo", "owner": old_owner, "unit": old_unit})
        tip = self.fixture_git(repository, "rev-parse", "HEAD")
        _ = self.fixture_git(repository, "update-ref", "refs/retired/" + old_unit, tip)

        return MigrationFixture(document, repository, old_owner, old_unit, native_owner, native_unit, owner, unit, configuration, reference, tip)

    def result(self, *, dry_run: bool = False) -> tuple[int, str]:
        with redirect_stdout(io.StringIO()) as output:
            code = migrate_staff_ids.migrate(dry_run=dry_run)
        return code, output.getvalue()

    def assert_finished(self) -> None:
        self.assertIsNone(staff.by_id(self.fixture.old_owner))
        self.assertIsNone(staff.by_id(self.fixture.old_unit))
        self.assertEqual(staff.by_id(self.fixture.native_owner), replace(self.fixture.owner, id=self.fixture.native_owner, current_session=self.fixture.native_owner))
        self.assertEqual(staff.by_id(self.fixture.native_unit), replace(self.fixture.unit, id=self.fixture.native_unit, current_session=self.fixture.native_unit, parent=self.fixture.native_owner))
        self.assertTrue(all(row.current_session == row.id for row in staff.snapshot().rows))
        self.assertEqual(unit_config.read_parked(self.fixture.native_unit, cleanup=False), self.fixture.configuration)
        self.assertEqual(rename_state.read_json(self.fixture.reference), {
            "old": "former", "new": "demo", "owner": self.fixture.native_owner, "unit": self.fixture.native_unit})
        self.assertEqual(self.fixture_git(self.fixture.repository, "rev-parse", "refs/retired/" + self.fixture.native_unit), self.fixture.tip)

    def assert_replay(self, boundary: str) -> None:
        before = self.staff_file.read_bytes()
        interrupted = False

        def stop_once() -> None:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise OSError("interrupted after " + boundary)

        with ExitStack() as contexts:
            if boundary == "parked file":
                write_parked = unit_config.write_parked

                def parked(identity: staff.StaffId, config: unit_config.UnitConfig) -> None:
                    _ = write_parked(identity, config)
                    if identity == self.fixture.native_unit:
                        stop_once()

                _ = contexts.enter_context(mock.patch.object(unit_config, "write_parked", side_effect=parked))
            elif boundary == "journal":
                write_json = rename_state.write_json

                def journal(path: Path, value: object) -> None:
                    write_json(path, value)
                    stop_once()

                _ = contexts.enter_context(mock.patch.object(rename_state, "write_json", side_effect=journal))
            elif boundary == "retired ref":
                run = subprocess.run

                def ref(arguments: list[str], *, capture_output: bool, text: bool,
                        check: bool = False) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(text)
                    result = run(arguments, capture_output=capture_output, text=True, check=check)
                    if "update-ref" in arguments:
                        stop_once()
                    return result

                instrumented = SimpleNamespace(run=ref)
                _ = contexts.enter_context(mock.patch.object(migrate_staff_ids, "subprocess", instrumented))
            else:
                publish = staff.publish_native_ids

                def graph(expected: staff.StaffSnapshot, identities: dict[staff.StaffId, staff.StaffId],
                          additions: tuple[staff.StaffRow, ...] = ()) -> staff.StaffSnapshot:
                    if boundary == "before staff":
                        stop_once()
                    result = publish(expected, identities, additions)
                    stop_once()
                    return result

                _ = contexts.enter_context(mock.patch.object(staff, "publish_native_ids", side_effect=graph))
            with self.assertRaisesRegex(OSError, "interrupted after"):
                _ = self.result()
        self.assertTrue(interrupted)
        if boundary != "after staff":
            self.assertEqual(self.staff_file.read_bytes(), before)
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assert_finished()
        stored = {path: path.read_bytes() for path in (
            self.staff_file, self.fixture.reference, unit_config.parked_path(self.fixture.native_unit))}
        self.assertEqual(self.result(), (0, "unchanged\n"))
        self.assertEqual({path: path.read_bytes() for path in stored}, stored)
        self.assertEqual(self.staff_file.with_name(self.staff_file.name + ".before-native-ids").read_bytes(), before)

    def test_interrupted_backup_cannot_publish_a_partial_backup_or_any_reference(self) -> None:
        backup = self.staff_file.with_name(self.staff_file.name + ".before-native-ids")
        paths = (self.staff_file, self.fixture.reference, unit_config.parked_path(self.fixture.old_unit))
        before = {path: path.read_bytes() for path in paths}
        replace_file = os.replace

        def interrupted(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == backup:
                self.assertEqual(Path(source).read_bytes(), before[self.staff_file])
                raise OSError("interrupted backup publication")
            replace_file(source, destination)

        with mock.patch.object(os, "replace", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted backup publication"):
                _ = self.result()
        self.assertFalse(backup.exists())
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertFalse(unit_config.parked_path(self.fixture.native_unit).exists())
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertEqual(backup.read_bytes(), before[self.staff_file])
        self.assert_finished()
        self.assertEqual(self.result(), (0, "unchanged\n"))

    def test_preexisting_truncated_backup_refuses_before_any_reference_or_staff_write(self) -> None:
        backup = self.staff_file.with_name(self.staff_file.name + ".before-native-ids")
        partial = self.staff_file.read_bytes()[:37]
        _ = backup.write_bytes(partial)
        paths = (self.staff_file, self.fixture.reference, unit_config.parked_path(self.fixture.old_unit))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        with self.assertRaises((ValueError, staff.StaffRefused)):
            _ = self.result()
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual(backup.read_bytes(), partial)
        self.assertFalse(unit_config.parked_path(self.fixture.native_unit).exists())
        self.assertEqual(self.fixture_git(self.fixture.repository, "rev-parse", "refs/retired/" + self.fixture.old_unit), self.fixture.tip)

    def remote_unit_record(self) -> staff.StaffSnapshot:
        remote_unit = replace(self.fixture.unit, current_session=self.fixture.native_unit,
                              worktree=staff.RemovedWorktree("mac", Path("/private/retained/models")))
        _ = self.write_document({"schema": 2, "rows": [self.fixture.owner.to_json(), remote_unit.to_json()]})
        unit_config.parked_path(self.fixture.old_unit).unlink()
        return staff.snapshot()

    def peer_unit(self, native: str) -> roster.RosterEntry:
        return roster.RosterEntry(machine="mac", kind="codex", role="unit director", name="display-renamed",
            status="running", reach=roster.CannotReceiveMessages("read-only evidence"),
            cwd="/private/retained/models", showrunner=roster.NoShowrunner(),
            director=roster.DirectsUnit("unrelated-display"), assignment=roster.NotAWorker(),
            thread=roster.CodexThread(native))

    def test_peer_snapshot_proves_remote_unit_from_exact_location_and_machine(self) -> None:
        record = self.remote_unit_record()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        matching = self.peer_unit(self.fixture.native_unit)
        foreign = replace(matching, machine="natedev", thread=roster.CodexThread(fixture_runtime_id("foreign-machine")))
        elsewhere = replace(matching, cwd="/another/worktree", thread=roster.CodexThread(fixture_runtime_id("foreign-folder")))
        with mock.patch.object(roster, "peer_snapshot", return_value=roster.Roster([matching, foreign, elsewhere], [])) as peer:
            migration = migrate_staff_ids.plan(record)
        peer.assert_called_once()
        self.assertEqual(migration.identities.get(self.fixture.old_unit), self.fixture.native_unit)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_unavailable_peer_lists_uncertainty_and_never_publishes_an_unproved_remote_unit(self) -> None:
        record = self.remote_unit_record()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(roster, "peer_snapshot", return_value=roster.PeerNotReached("mac", "fixture connection unavailable")) as peer:
            migration = migrate_staff_ids.plan(record)
        peer.assert_called_once()
        self.assertNotIn(self.fixture.old_unit, migration.identities)
        self.assertTrue(any("mac" in reason and "uncertain" in reason and "fixture connection unavailable" in reason
                            for reason in migration.missing), migration.missing)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_conflicting_peer_sessions_withhold_the_remote_unit(self) -> None:
        record = self.remote_unit_record()
        before = self.staff_file.read_bytes()
        matching = self.peer_unit(self.fixture.native_unit)
        conflicting = replace(matching, session=roster.ClaudeSession(fixture_runtime_id("conflicting-peer")))
        entries = [matching, conflicting]
        with mock.patch.object(roster, "peer_snapshot", return_value=roster.Roster(entries, [])):
            migration = migrate_staff_ids.plan(record)
        self.assertNotIn(self.fixture.old_unit, migration.identities)
        self.assertTrue(any(str(self.fixture.old_unit) in reason and "conflicting" in reason
                            for reason in migration.missing), migration.missing)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_peer_worker_with_same_name_under_another_parent_is_not_adopted(self) -> None:
        record = self.remote_unit_record()
        worker = staff.StaffRow(staff.StaffId("s00000000003"), "worker", "review", self.fixture.old_unit,
                               current_session=staff.StaffId("s00000000003"))
        _ = self.write_document({"schema": 2, "rows": [*(row.to_json() for row in record.rows), worker.to_json()]})
        before = self.staff_file.read_bytes()
        parent = self.peer_unit(self.fixture.native_unit)
        wrong_parent = roster.RosterEntry(machine="mac", kind="claude", role="worker", name=worker.name,
            status="running", reach=roster.CannotReceiveMessages("read-only evidence"), cwd=parent.cwd,
            showrunner=roster.NoShowrunner(), director=roster.UnderDirector("models", "renamed", "codex:" + fixture_runtime_id("other-parent")),
            assignment=roster.NotAWorker(), session=roster.ClaudeSession(fixture_runtime_id("foreign-reviewer")))
        with mock.patch.object(roster, "peer_snapshot", return_value=roster.Roster([parent, wrong_parent], [])):
            migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(migration.identities.get(self.fixture.old_unit), self.fixture.native_unit)
        self.assertNotIn(worker.id, migration.identities)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def origin_retired_ref(self) -> Path:
        origin = self.root / "origin.git"
        _ = self.fixture_git(self.fixture.repository, "init", "--bare", str(origin))
        _ = self.fixture_git(self.fixture.repository, "remote", "add", "origin", str(origin))
        _ = self.fixture_git(self.fixture.repository, "push", "origin",
                             self.fixture.tip + ":refs/retired/" + self.fixture.old_unit)
        _ = self.fixture_git(self.fixture.repository, "update-ref", "-d", "refs/retired/" + self.fixture.old_unit)
        return origin

    def test_origin_only_retired_reference_is_published_before_staff_and_replays(self) -> None:
        origin = self.origin_retired_ref()
        before = self.staff_file.read_bytes()
        def interrupted(expected: staff.StaffSnapshot, identities: dict[staff.StaffId, staff.StaffId],
                        additions: tuple[staff.StaffRow, ...] = ()) -> staff.StaffSnapshot:
            del expected, identities, additions
            self.assertEqual(self.fixture_git(origin, "rev-parse", "refs/retired/" + self.fixture.native_unit), self.fixture.tip)
            self.assertEqual(self.staff_file.read_bytes(), before)
            raise OSError("interrupted before staff after origin push")

        with mock.patch.object(staff, "publish_native_ids", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted before staff after origin push"):
                _ = self.result()
        self.assertEqual(self.staff_file.read_bytes(), before)
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.fixture_git(origin, "rev-parse", "refs/retired/" + self.fixture.native_unit), self.fixture.tip)
        migrated = staff.by_id(self.fixture.native_unit)
        assert migrated is not None
        self.assertEqual(migrated.current_session, self.fixture.native_unit)
        self.assertEqual(self.result(), (0, "unchanged\n"))

    def test_unavailable_configured_origin_refuses_staff_publication_without_claiming_remote_absence(self) -> None:
        missing = self.root / "unavailable-origin.git"
        _ = self.fixture_git(self.fixture.repository, "remote", "add", "origin", str(missing))
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with self.assertRaisesRegex(staff.StaffRefused, "origin retired refs unavailable"):
            _ = self.result()
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertEqual(self.fixture_git(self.fixture.repository, "rev-parse", "refs/retired/" + self.fixture.old_unit), self.fixture.tip)
        self.assertIsNotNone(staff.by_id(self.fixture.old_unit))
        self.assertIsNone(staff.by_id(self.fixture.native_unit))

    def test_unequal_origin_retired_destination_refuses_before_staff_publication(self) -> None:
        origin = self.origin_retired_ref()
        _ = self.fixture_git(self.fixture.repository, "commit", "--allow-empty", "-m", "different retired target")
        different = self.fixture_git(self.fixture.repository, "rev-parse", "HEAD")
        _ = self.fixture_git(self.fixture.repository, "push", "origin", different + ":refs/retired/" + self.fixture.native_unit)
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "unequal.*retired|retired.*unequal"):
            _ = self.result()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.fixture_git(origin, "rev-parse", "refs/retired/" + self.fixture.native_unit), different)
        self.assertEqual(self.fixture_git(origin, "rev-parse", "refs/retired/" + self.fixture.old_unit), self.fixture.tip)

    def test_migration_sets_current_session_to_first_native_id_and_rerun_writes_nothing(self) -> None:
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assert_finished()
        saved = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n"))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), saved)

    def test_preview_proves_native_ids_without_writing_any_store(self) -> None:
        paths = (self.staff_file, self.fixture.reference, unit_config.parked_path(self.fixture.old_unit))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        code, output = self.result(dry_run=True)
        self.assertEqual(code, 0, output)
        for old, native in ((self.fixture.old_owner, self.fixture.native_owner), (self.fixture.old_unit, self.fixture.native_unit)):
            self.assertIn(f"proved {old} -> {native}", output)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertFalse(unit_config.parked_path(self.fixture.native_unit).exists())
        self.assertFalse(self.staff_file.with_name(self.staff_file.name + ".before-native-ids").exists())

    def test_interruption_after_parked_file_publication_replays_without_losing_configuration(self) -> None:
        self.assert_replay("parked file")

    def test_interruption_after_journal_publication_replays_before_rekeying_staff(self) -> None:
        self.assert_replay("journal")

    def test_interruption_after_retired_ref_publication_replays_before_rekeying_staff(self) -> None:
        self.assert_replay("retired ref")

    def assert_receipt_replay(self, path: Path) -> None:
        fields: dict[str, object] = {"session_id": self.fixture.native_unit, "request": "saved opening"}
        completion_receipt.write_receipt(path, self.fixture.old_unit, fields)
        before = self.staff_file.read_bytes()
        write_json = rename_state.write_json

        def interrupt(receipt: Path, value: object) -> None:
            write_json(receipt, value)
            if receipt == path:
                raise OSError("interrupted after receipt publication")

        with mock.patch.object(rename_state, "write_json", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "interrupted after receipt publication"):
                _ = self.result()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(rename_state.read_json(path), {**fields, "owner": self.fixture.native_unit})
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assert_finished()
        recovered = completion_receipt.read_receipt(path, staff.snapshot())
        self.assertIsInstance(recovered, completion_receipt.OwnedReceipt)
        assert isinstance(recovered, completion_receipt.OwnedReceipt)
        self.assertEqual(recovered.owner.id, self.fixture.native_unit)
        self.assertEqual(recovered.fields, {**fields, "owner": self.fixture.native_unit})
        saved = path.read_bytes(), self.staff_file.read_bytes()
        self.assertEqual(self.result(), (0, "unchanged\n"))
        self.assertEqual((path.read_bytes(), self.staff_file.read_bytes()), saved)

    def test_interrupted_promotion_receipt_replays_its_owner_before_staff_publication(self) -> None:
        self.assert_receipt_replay(self.fixture.repository / ".git/promotions/models.json")

    def test_interrupted_unit_opening_receipt_replays_its_owner_before_staff_publication(self) -> None:
        self.assert_receipt_replay(self.fixture.repository / ".git/add-unit-openings/models.json")

    def test_interrupted_showrunner_receipt_replays_its_owner_before_staff_publication(self) -> None:
        self.assert_receipt_replay(Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "demo/add_showrunner.json")

    def test_interruption_before_staff_publication_reuses_all_published_references(self) -> None:
        self.assert_replay("before staff")

    def test_interruption_after_staff_publication_finishes_without_duplicate_rows(self) -> None:
        self.assert_replay("after staff")

    def test_an_unequal_parked_destination_refuses_without_changing_staff(self) -> None:
        other = replace(self.fixture.configuration, responsibilities=("other",))
        _ = unit_config.write_parked(self.fixture.native_unit, other)
        before = self.staff_file.read_bytes(), unit_config.parked_path(self.fixture.native_unit).read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "unequal migration destination"):
            _ = self.result()
        self.assertEqual((self.staff_file.read_bytes(), unit_config.parked_path(self.fixture.native_unit).read_bytes()), before)

    def test_unavailable_worker_evidence_leaves_proved_retired_and_owner_migrations_able_to_finish(self) -> None:
        with mock.patch.object(staff_sources, "observe_workers", side_effect=staff.StaffUnreadable("worker backend offline")):
            code, output = self.result()
        self.assertEqual(code, 1, output)
        self.assertIn("worker runtime evidence unavailable: worker backend offline", output)
        self.assert_finished()
        self.assertEqual(len(staff.snapshot().rows), 2)

    def test_unreadable_unrelated_run_keeps_a_proved_workers_native_migration_and_strict_checks_refuse(self) -> None:
        old_worker = staff.StaffRow(staff.StaffId("s00000000003"), "worker", "impl", self.fixture.old_owner, current_session=staff.StaffId("s00000000003"))
        native_worker = staff.StaffId(fixture_runtime_id("independently-proved-Codex-worker"))
        _ = self.write_document({"schema": 2, "rows": [
            self.fixture.owner.to_json(), self.fixture.unit.to_json(), old_worker.to_json()]})
        good_run = self.worker_run("demo", "", self.fixture.native_owner)
        _ = self.roster_worker(good_run, "impl", "impl", native_worker)
        self.pane("owner:p1", session_id=self.fixture.native_owner, environment={})
        bad_run = self.worker_run("unrelated", "broken-unit", "unproved-director")
        _ = self.roster_worker(bad_run, "unproved", "impl", fixture_runtime_id("unproved-worker"))
        unreadable = bad_run / "progress_history_state.json"
        _ = unreadable.write_text('{"production":')
        record = staff.snapshot()
        sources, _ = staff_sources.migration_sources(record)
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(staff.StaffUnreadable, str(unreadable)):
            _ = staff_sources.observe_workers(record, sources)
        self.assertEqual(self.staff_file.read_bytes(), before)

        code, output = self.result()
        self.assertEqual(code, 1, output)
        self.assertIn(str(unreadable), output)
        self.assertIn(f"proved {old_worker.id} -> {native_worker}", output)
        self.assert_finished()
        self.assertEqual(staff.by_id(native_worker), replace(old_worker, id=native_worker, current_session=native_worker,
                                                           parent=self.fixture.native_owner))
        self.assertIsNone(staff.by_id(old_worker.id))
        self.assertIsNone(staff.by_id(staff.StaffId(fixture_runtime_id("unproved-worker"))))
        saved = self.staff_file.read_bytes()
        code, output = self.result()
        self.assertEqual(code, 1, output)
        self.assertIn(str(unreadable), output)
        self.assertEqual(self.staff_file.read_bytes(), saved)

    def test_proved_child_waits_for_its_missing_native_parent_instead_of_publishing_a_legacy_link(self) -> None:
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-demo" / "conf"
        configured = timer.read_text()
        timer.unlink()
        before = self.staff_file.read_bytes(), self.fixture.reference.read_bytes()
        code, output = self.result()
        self.assertEqual(code, 1, output)
        self.assertIn(f"{self.fixture.old_unit} models: missing proved native parent {self.fixture.old_owner}", output)
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.reference.read_bytes()), before)
        self.assertIsNone(staff.by_id(self.fixture.native_owner))
        self.assertIsNone(staff.by_id(self.fixture.native_unit))
        self.assertFalse(unit_config.parked_path(self.fixture.native_unit).exists())
        _ = timer.write_text(configured)
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assert_finished()

    def test_missing_runtime_evidence_stays_listed_without_inventing_a_row(self) -> None:
        unit_config.parked_path(self.fixture.old_unit).unlink()
        before = self.staff_file.read_bytes()
        code, output = self.result(dry_run=True)
        self.assertEqual(code, 1, output)
        self.assertIn(f"missing {self.fixture.old_unit} models: missing native session evidence", output)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_id(self.fixture.native_unit))


    def historical_worker(self, name: str = "review") -> staff.StaffRow:
        worker = staff.StaffRow(staff.StaffId("s00000000003"), "worker", name, self.fixture.old_owner, current_session=staff.StaffId("s00000000003"))
        _ = self.write_document({"schema": 2, "rows": [
            self.fixture.owner.to_json(), self.fixture.unit.to_json(), worker.to_json()]})
        return worker

    def test_done_roster_proves_the_native_worker_under_its_recorded_director(self) -> None:
        worker = self.historical_worker()
        native = staff.native_id(fixture_runtime_id("completed-review-thread"))
        run = self.worker_run("demo", "", self.fixture.native_owner)
        _ = self.roster_worker(run, worker.name, "review", native, status="done")
        before = self.staff_file.read_bytes()
        code, output = self.result(dry_run=True)
        self.assertEqual(code, 0, output)
        self.assertIn(f"proved {worker.id} -> {native}", output)
        self.assertEqual(self.staff_file.read_bytes(), before)
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assert_finished()
        self.assertEqual(staff.by_id(native), replace(worker, id=native, current_session=native, parent=self.fixture.native_owner))
        self.assertIsNone(staff.by_id(worker.id))
        saved = self.staff_file.read_bytes()
        self.assertEqual(self.result(), (0, "unchanged\n"))
        self.assertEqual(self.staff_file.read_bytes(), saved)

    def test_same_named_done_workers_are_joined_only_to_their_own_proved_parents(self) -> None:
        worker = self.historical_worker()
        other_doc = self.fill_production("other")
        other_owner = staff.StaffRow(staff.native_id(fixture_runtime_id("other-owner")), "showrunner", "other", None,
                                    doc=staff.LiveDoc("natedev", other_doc), current_session=staff.native_id(fixture_runtime_id("other-owner")))
        other_worker = staff.StaffRow(staff.StaffId("s00000000004"), "worker", worker.name, other_owner.id, current_session=staff.StaffId("s00000000004"))
        _ = self.write_document({"schema": 2, "rows": [row.to_json() for row in (
            self.fixture.owner, self.fixture.unit, worker, other_owner, other_worker)]})
        native = staff.native_id(fixture_runtime_id("demo-review-thread"))
        other_native = staff.native_id(fixture_runtime_id("other-review-thread"))
        for slug, parent, identity in (("demo", self.fixture.native_owner, native), ("other", other_owner.id, other_native)):
            run = self.worker_run(slug, "", parent)
            _ = self.roster_worker(run, worker.name, "review", identity, status="done")
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-other/conf"
        target = showrunners.read_bound_source(timer.parent, other_doc).timer.target
        _ = timer.write_text(timer.read_text().replace("TARGET=" + target, "TARGET=session:" + other_owner.id))
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(migration.identities[worker.id], native)
        self.assertEqual(migration.identities[other_worker.id], other_native)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_conflicting_done_rosters_list_both_parent_owned_candidates_without_rekeying(self) -> None:
        worker = self.historical_worker()
        candidates: list[tuple[Path, str]] = []
        for suffix in ("first", "second"):
            run = self.worker_run("demo", suffix, self.fixture.native_owner)
            native = fixture_runtime_id("conflicting-review-" + suffix)
            path = self.roster_worker(run, worker.name, "review", native, status="done")
            candidates.append((path, native))
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(worker.id, migration.identities)
        diagnostic = "\n".join(migration.missing)
        self.assertIn("conflicting parent-owned roster candidates", diagnostic)
        for path, native in candidates:
            self.assertIn(str(path), diagnostic)
            self.assertIn(native, diagnostic)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_ambiguous_native_director_cannot_assign_a_done_worker_to_either_parent(self) -> None:
        worker = self.historical_worker()
        other_doc = self.fill_production("other")
        other_owner = staff.StaffRow(staff.StaffId("s00000000004"), "showrunner", "other", None,
                                    doc=staff.LiveDoc("natedev", other_doc), current_session=staff.StaffId("s00000000004"))
        other_worker = staff.StaffRow(staff.StaffId("s00000000005"), "worker", worker.name, other_owner.id, current_session=staff.StaffId("s00000000005"))
        _ = self.write_document({"schema": 2, "rows": [row.to_json() for row in (
            self.fixture.owner, self.fixture.unit, worker, other_owner, other_worker)]})
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-other/conf"
        target = showrunners.read_bound_source(timer.parent, other_doc).timer.target
        _ = timer.write_text(timer.read_text().replace("TARGET=" + target, "TARGET=session:" + self.fixture.native_owner))
        run = self.worker_run("demo", "", self.fixture.native_owner)
        native = fixture_runtime_id("ambiguous-parent-review")
        _ = self.roster_worker(run, worker.name, "review", native, status="done")
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        for row in (self.fixture.owner, other_owner, worker, other_worker):
            self.assertNotIn(row.id, migration.identities)
            self.assertIn(row.id, "\n".join(migration.missing))
        self.assertEqual(migration.additions, ())
        self.assertIsNone(staff.by_id(staff.native_id(native)))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_missing_exact_review_name_lists_parent_owned_candidates_without_role_aliases(self) -> None:
        worker = self.historical_worker("review")
        candidates: list[tuple[str, str, Path]] = []
        for suffix, name in (("first", "reviewer"), ("second", "style-review")):
            run = self.worker_run("demo", suffix, self.fixture.native_owner)
            native = fixture_runtime_id("not-an-alias-" + suffix)
            path = self.roster_worker(run, name, "review", native, status="done")
            candidates.append((name, native, path))
        foreign = fixture_runtime_id("foreign-parent-review")
        foreign_run = self.worker_run("unrelated", "", fixture_runtime_id("unproved-foreign-parent"))
        _ = self.roster_worker(foreign_run, "review", "review", foreign, status="done")
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(worker.id, migration.identities)
        diagnostics = [line for line in migration.missing if worker.id in line and "parent-owned rosters" in line]
        self.assertEqual(len(diagnostics), 1, migration.missing)
        self.assertEqual([line for line in migration.missing if worker.id in line], diagnostics)
        for name, native, path in candidates:
            self.assertIn(name + "=" + native, diagnostics[0])
            self.assertIn(str(path), diagnostics[0])
        self.assertNotIn(foreign, diagnostics[0])
        self.assertNotIn(str(foreign_run), diagnostics[0])
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_current_worker_backend_proof_takes_precedence_over_an_older_done_roster(self) -> None:
        worker = self.historical_worker()
        old_native = fixture_runtime_id("older-completed-review")
        current_native = staff.native_id(fixture_runtime_id("current-running-review"))
        old_run = self.worker_run("demo", "old", self.fixture.native_owner)
        _ = self.roster_worker(old_run, worker.name, "review", old_native, status="done")
        current_run = self.worker_run("demo", "", self.fixture.native_owner)
        _ = self.roster_worker(current_run, worker.name, "review", current_native)
        self.pane("owner:current", session_id=self.fixture.native_owner, environment={})
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(migration.identities[worker.id], current_native)
        self.assertNotIn(old_native, migration.identities.values())
        self.assertFalse(any(worker.id in line for line in migration.missing), migration.missing)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_one_valid_transcript_in_the_exact_worktree_proves_the_missing_unit_id(self) -> None:
        unit_config.parked_path(self.fixture.old_unit).unlink()
        path = self.session_transcript(self.fixture.native_unit, self.root / "gone")
        _ = path.write_text(json.dumps({"sessionId": self.fixture.native_unit, "cwd": str(self.root / "gone")}) + "\n")
        unrelated = self.session_transcript(fixture_runtime_id("other-worktree-session"), self.root / "unrelated")
        _ = unrelated.write_text(json.dumps({"sessionId": unrelated.stem, "cwd": str(self.root / "unrelated")}) + "\n")
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(migration.identities[self.fixture.old_unit], self.fixture.native_unit)
        self.assertFalse(any(self.fixture.old_unit in line for line in migration.missing), migration.missing)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unfinished_transcript_tail_including_partial_utf8_keeps_complete_native_proof(self) -> None:
        unit_config.parked_path(self.fixture.old_unit).unlink()
        path = self.session_transcript(self.fixture.native_unit, self.root / "gone")
        valid = (json.dumps({"sessionId": self.fixture.native_unit}) + "\n").encode()
        before = self.staff_file.read_bytes()
        for tail in (b'{"sessionId":', b'{"text":"partial \xf0\x9f'):
            with self.subTest(tail=tail):
                original = valid + tail
                _ = path.write_bytes(original)
                migration = migrate_staff_ids.plan(staff.snapshot())
                self.assertEqual(migration.identities.get(self.fixture.old_unit), self.fixture.native_unit)
                self.assertFalse(any(self.fixture.old_unit in line for line in migration.missing), migration.missing)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(self.staff_file.read_bytes(), before)

    def test_malformed_complete_middle_transcript_line_keeps_native_proof_and_bytes(self) -> None:
        unit_config.parked_path(self.fixture.old_unit).unlink()
        path = self.session_transcript(self.fixture.native_unit, self.root / "gone")
        valid = json.dumps({"sessionId": self.fixture.native_unit}) + "\n"
        original = (valid + '{malformed middle line}\n' + valid).encode()
        _ = path.write_bytes(original)
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(migration.identities.get(self.fixture.old_unit), self.fixture.native_unit)
        self.assertFalse(any(self.fixture.old_unit in line for line in migration.missing), migration.missing)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_proved_launch_with_malformed_historical_transcript_lists_only_the_proved_member(self) -> None:
        self.pane("unit:proved", session_id=self.fixture.native_unit, environment={})
        path = self.session_transcript(self.fixture.native_unit, self.root / "gone")
        original = b"{malformed historical record}\n"
        _ = path.write_bytes(original)
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(migration.identities.get(self.fixture.old_unit), self.fixture.native_unit)
        self.assertFalse(any(self.fixture.old_unit in line for line in migration.missing), migration.missing)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_worktree_transcript_conflicting_with_parked_identity_withholds_the_unit(self) -> None:
        other = fixture_runtime_id("conflicting-worktree-session")
        path = self.session_transcript(other, self.root / "gone")
        _ = path.write_text(json.dumps({"sessionId": other}) + "\n")
        before = self.staff_file.read_bytes(), unit_config.parked_path(self.fixture.old_unit).read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, migration.identities)
        self.assertTrue(any(self.fixture.old_unit in line and "conflicting native session evidence" in line
                            for line in migration.missing), migration.missing)
        self.assertEqual((self.staff_file.read_bytes(), unit_config.parked_path(self.fixture.old_unit).read_bytes()), before)

    def test_transcript_session_id_must_match_its_native_filename(self) -> None:
        unit_config.parked_path(self.fixture.old_unit).unlink()
        path = self.session_transcript(self.fixture.native_unit, self.root / "gone")
        _ = path.write_text(json.dumps({"sessionId": fixture_runtime_id("wrong-transcript-body")}) + "\n")
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, migration.identities)
        self.assertTrue(any(str(path) in line and "session ids disagree" in line for line in migration.missing),
                        migration.missing)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_multiple_worktree_transcripts_stay_missing_without_choosing_the_latest(self) -> None:
        unit_config.parked_path(self.fixture.old_unit).unlink()
        paths: list[Path] = []
        for suffix in ("first", "second"):
            native = fixture_runtime_id("worktree-history-" + suffix)
            path = self.session_transcript(native, self.root / "gone")
            _ = path.write_text(json.dumps({"sessionId": native}) + "\n")
            paths.append(path)
        before = self.staff_file.read_bytes()
        migration = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, migration.identities)
        diagnostic = "\n".join(migration.missing)
        self.assertIn("multiple worktree transcripts", diagnostic)
        for path in paths:
            self.assertIn(str(path), diagnostic)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_bound_legacy_owner_resolves_devtools_unit_before_worker_migration(self) -> None:
        document = self.fill_production("tools", "devtools-unit")
        owner = staff.StaffRow(staff.StaffId("s00000000004"), "showrunner", "tools", None,
                              doc=staff.LiveDoc("natedev", document), current_session=staff.StaffId("s00000000004"))
        unit = staff.StaffRow(staff.StaffId("s00000000005"), "unit", "devtools", owner.id,
                             staff.PresentWorktree("natedev", self.source_root / "worktrees/devtools-unit"), current_session=staff.StaffId("s00000000005"))
        worker = staff.StaffRow(staff.StaffId("s00000000006"), "worker", "review", unit.id, current_session=staff.StaffId("s00000000006"))
        _ = self.write_document({"schema": 2, "rows": [row.to_json() for row in (owner, unit, worker)]})
        native_owner = staff.native_id(fixture_runtime_id("tools-native-owner"))
        native_unit = staff.native_id(fixture_runtime_id("devtools-native-unit"))
        native_worker = staff.native_id(fixture_runtime_id("devtools-review"))
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-tools/conf"
        target = showrunners.read_bound_source(timer.parent, document).timer.target
        _ = timer.write_text(timer.read_text().replace("TARGET=" + target, "TARGET=session:" + native_owner))
        self.pane("tools:unit", session_id=native_unit, environment={"STAFF_ID": unit.id})
        run = self.worker_run("tools", "devtools-unit", native_unit)
        _ = self.roster_worker(run, worker.name, "review", native_worker)
        before = self.staff_file.read_bytes()
        record = staff.snapshot()
        sources, problems = staff_sources.migration_sources(record)
        self.assertEqual(problems, ())
        self.assertEqual(sources.member(staff_sources.UnitMember("tools", "devtools-unit"), record),
                         staff_sources.MemberFound(unit))
        migration = migrate_staff_ids.plan(record)
        self.assertEqual(migration.identities, {owner.id: native_owner, unit.id: native_unit, worker.id: native_worker})
        self.assertEqual(self.staff_file.read_bytes(), before)


class LiveMigrationEvidenceTests(unittest.TestCase):
    @cached_property
    def state(self) -> NativeMigrationTests:
        state = NativeMigrationTests("test_one_valid_transcript_in_the_exact_worktree_proves_the_missing_unit_id")
        self.addCleanup(state.doCleanups)
        state.setUp()
        return state

    @property
    def fixture(self) -> MigrationFixture:
        return self.state.fixture

    @property
    def other(self) -> str:
        return fixture_runtime_id("older-worktree-conversation")

    @property
    def launch_file(self) -> Path:
        return Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json"

    @override
    def setUp(self) -> None:
        transcript = self.state.session_transcript(self.other, self.state.root / "gone")
        _ = transcript.write_text(json.dumps({"sessionId": self.other}) + "\n")
        registry = self.state.session_record(self.fixture.native_unit, "models")
        _ = registry.rename(registry.with_name(str(os.getpid()) + ".json"))
        self.state.pane("live:unit", session_id=self.fixture.native_unit,
                        environment={"STAFF_ID": self.fixture.old_unit})
        launches = cast(dict[str, dict[str, object]], json.loads(self.launch_file.read_text()))
        launches["live:unit"].update(pid=os.getpid(), proc_start=sessions.process_start(os.getpid()))
        self.state.write_json(self.launch_file, launches)

    def change_launch(self, **fields: object) -> None:
        launches = cast(dict[str, dict[str, object]], json.loads(self.launch_file.read_text()))
        launches["live:unit"].update(fields)
        self.state.write_json(self.launch_file, launches)

    def test_live_registry_and_launch_prove_parent_and_child(self) -> None:
        old = staff.StaffId("s00000000003")
        child = staff.StaffRow(old, "worker", "impl", self.fixture.old_unit, current_session=old)
        native = staff.native_id(fixture_runtime_id("accounts-child-thread"))
        _ = self.state.write_document({"schema": 2, "rows": [
            self.fixture.owner.to_json(), self.fixture.unit.to_json(), child.to_json()]})
        run = self.state.worker_run("demo", "models", self.fixture.native_unit)
        _ = self.state.roster_worker(run, "impl", "impl", native, status="done")
        before = self.state.staff_file.read_bytes(), self.launch_file.read_bytes()
        result = migrate_staff_ids.plan(staff.snapshot())
        self.assertEqual(result.identities[self.fixture.old_unit], self.fixture.native_unit)
        self.assertEqual(result.identities[old], native)
        self.assertEqual(before, (self.state.staff_file.read_bytes(), self.launch_file.read_bytes()))

    def test_reused_pid_does_not_confirm_old_launch(self) -> None:
        self.change_launch(proc_start="old-process-start")
        result = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, result.identities)
        self.assertTrue(any("conflicting native session evidence" in line for line in result.missing), result.missing)

    def test_missing_registry_does_not_confirm_launch(self) -> None:
        (Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json").unlink()
        result = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, result.identities)

    def test_ended_launch_does_not_confirm_session(self) -> None:
        self.change_launch(ended=True)
        result = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, result.identities)

    def test_independent_launch_conflict_still_refuses(self) -> None:
        self.state.pane("other:unit", session_id=self.other, environment={"STAFF_ID": self.fixture.old_unit})
        result = migrate_staff_ids.plan(staff.snapshot())
        self.assertNotIn(self.fixture.old_unit, result.identities)
        detail = "\n".join(result.missing)
        self.assertIn(self.fixture.native_unit, detail)
        self.assertIn(self.other, detail)


class RetiredNativeMigrationTests(StaffTestCase):
    def test_native_launch_of_a_legacy_owner_publishes_its_missing_retired_child_in_the_same_graph(self) -> None:
        _ = self.enterContext(mock.patch.object(showrunners, "SCRATCH_ROOT", self.root / "scratch"))
        document = self.retired_fill_production("demo", "models-unit")
        native_owner = staff.StaffId(fixture_runtime_id("native-launch-legacy-owner"))
        native_unit = staff.StaffId(fixture_runtime_id("missing-retired-Claude"))
        legacy_owner = staff.StaffRow(staff.StaffId("s00000000001"), "showrunner", "demo", None,
                                     doc=staff.LiveDoc("natedev", document), current_session=staff.StaffId("s00000000001"))
        retired = next(row for row in staff.find(kind="unit") if row.name == "models")
        retired = replace(retired, id=staff.StaffId("s00000000002"),
                          parent=legacy_owner.id, current_session=native_unit)
        _ = self.write_document({"schema": 2, "rows": [legacy_owner.to_json(), retired.to_json()]})
        _ = document.write_text(document.read_text().replace("session session-models-unit", "session " + native_unit))
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-demo" / "conf"
        target = showrunners.read_bound_source(timer.parent, document).timer.target
        _ = timer.write_text(timer.read_text().replace("TARGET=" + target, "TARGET=session:" + native_owner))
        self.pane("w1:owner", session_id=native_owner, environment={})
        before = self.staff_file.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            preview = migrate_staff_ids.migrate(dry_run=True)
        self.assertEqual(preview, 0, output.getvalue())
        self.assertIn(f"proved {retired.id} -> {native_unit}", output.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertFalse(unit_config.parked_path(native_unit).exists())
        publish = staff.publish_native_ids
        snapshots: list[staff.StaffSnapshot] = []

        def observe(expected: staff.StaffSnapshot, identities: dict[staff.StaffId, staff.StaffId],
                    additions: tuple[staff.StaffRow, ...] = ()) -> staff.StaffSnapshot:
            snapshots.append(staff.snapshot())
            self.assertEqual(self.staff_file.read_bytes(), before)
            return publish(expected, identities, additions)

        with mock.patch.object(staff, "publish_native_ids", side_effect=observe), redirect_stdout(io.StringIO()) as output:
            code = migrate_staff_ids.migrate()
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(snapshots, [staff.StaffSnapshot((legacy_owner, retired))])
        self.assertEqual(staff.by_id(native_owner), replace(legacy_owner, id=native_owner, current_session=native_owner))
        child = staff.by_id(native_unit)
        assert child is not None
        self.assertEqual((child.name, child.parent), ("models", native_owner))
        self.assertIsInstance(child.worktree, staff.RemovedWorktree)
        self.assertIsInstance(unit_config.read_parked(native_unit, cleanup=False), unit_config.UnitConfig)
        self.assertIsNone(staff.by_id(legacy_owner.id))
        saved = self.staff_file.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            code = migrate_staff_ids.migrate()
        self.assertEqual((code, output.getvalue()), (0, "unchanged\n"))
        self.assertEqual(self.staff_file.read_bytes(), saved)

    def test_proved_retired_members_finish_while_missing_history_stays_unfilled_and_rerun_finishes(self) -> None:
        _ = self.enterContext(mock.patch.object(showrunners, "SCRATCH_ROOT", self.root / "scratch"))
        document = self.retired_fill_production("demo", "first-unit", "second-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        owner = staff.set_doc(owner.id, staff.LiveDoc("natedev", document))
        old_units = staff.find(kind="unit", parent=owner.id)
        first_id = staff.StaffId(fixture_runtime_id("retired-first-Claude"))
        second_id = staff.StaffId(fixture_runtime_id("retired-second-Claude"))
        first_old = replace(next(row for row in old_units if row.name == "first"),
                            id=staff.StaffId("s00000000002"), current_session=first_id)
        second_old = replace(next(row for row in old_units if row.name == "second"),
                             id=staff.StaffId("s00000000003"),
                             current_session=staff.StaffId(fixture_runtime_id("retired-second-without-evidence")))
        _ = self.write_document({"schema": 2, "rows": [row.to_json() for row in (owner, first_old, second_old)]})
        full_text = document.read_text().replace("session session-first-unit", "session " + first_id)
        full_text = full_text.replace("session session-second-unit", "session " + second_id)
        second_retirement = (f"(retired 2026-10-31, worktree removed, session {second_id}, "
                             f"folder {self.root / 'original' / 'second-unit'}) ")
        _ = document.write_text(full_text.replace(second_retirement, ""))
        timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-demo" / "conf"
        target = showrunners.read_bound_source(timer.parent, document).timer.target
        _ = timer.write_text(timer.read_text().replace("TARGET=" + target, "TARGET=session:" + owner.id))
        with redirect_stdout(io.StringIO()) as output:
            code = migrate_staff_ids.migrate()
        self.assertEqual(code, 1, output.getvalue())
        self.assertIn(f"{second_old.id} second: missing native session evidence", output.getvalue())
        first = staff.by_id(first_id)
        assert first is not None
        self.assertEqual((first.id, first.name, first.parent), (first_id, "first", owner.id))
        self.assertIsInstance(first.worktree, staff.RemovedWorktree)
        self.assertIsInstance(unit_config.read_parked(first_id, cleanup=False), unit_config.UnitConfig)
        self.assertIsNone(staff.by_id(second_id))
        self.assertEqual(staff.find(kind="unit"), [first, second_old])
        _ = document.write_text(full_text)
        _ = staff.update_current_session(second_old.current_session, second_id)
        with redirect_stdout(io.StringIO()) as output:
            code = migrate_staff_ids.migrate()
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(staff.by_id(first_id), first)
        second = staff.by_id(second_id)
        assert second is not None
        self.assertEqual((second.id, second.name, second.parent), (second_id, "second", owner.id))
        self.assertIsInstance(second.worktree, staff.RemovedWorktree)
        self.assertIsInstance(unit_config.read_parked(second_id, cleanup=False), unit_config.UnitConfig)
        before = self.staff_file.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            code = migrate_staff_ids.migrate()
        self.assertEqual((code, output.getvalue()), (0, "unchanged\n"))
        self.assertEqual(self.staff_file.read_bytes(), before)
