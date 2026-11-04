"""Prove the settings migration with temporary files."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from scripts.production import migrate_showrunner_settings as migration, showrunners, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member


class SettingsMigrationTest(StaffTestCase):
    def test_migration_resolves_ids_and_repeats_after_source_deletion(self) -> None:
        row = fixture_member("showrunner", name="director")
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "legacy", Path(folder) / "settings.conf"
            _ = source.write_text(json.dumps(dict(threshold_percent=2, repeat_minutes=30,
                stall_minutes=5, faults_to="director", always=["director"], showrunners=[dict(session="director")])))
            membership = staff.StaffSnapshot((row,))
            migration.migrate(source, destination, membership)
            migration.migrate(source, destination, membership)
            self.assertFalse(source.exists())
            self.assertEqual(showrunners.load_settings_from(destination)["always"], [str(row.id)])
            self.assertEqual(showrunners.load_settings_from(destination)["faults_to"], str(row.id))

    def test_missing_recipient_preserves_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "legacy", Path(folder) / "settings.conf"
            _ = source.write_text(json.dumps(dict(threshold_percent=2, repeat_minutes=30,
                stall_minutes=5, faults_to="missing", always=[])))
            with self.assertRaisesRegex(ValueError, "expected one staff row"):
                migration.migrate(source, destination, staff.StaffSnapshot(()))
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_conflicting_destination_preserves_both_files(self) -> None:
        row = fixture_member("showrunner", name="director")
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "legacy", Path(folder) / "settings.conf"
            _ = source.write_text(json.dumps(dict(threshold_percent=2, repeat_minutes=30,
                stall_minutes=5, faults_to="director", always=[])))
            _ = destination.write_text("other settings\n")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                migration.migrate(source, destination, staff.StaffSnapshot((row,)))
            self.assertTrue(source.exists())
            self.assertEqual(destination.read_text(), "other settings\n")
