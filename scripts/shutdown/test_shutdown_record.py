"""Check restart record migration with private staff and shutdown stores."""
from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch


from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id
from scripts.shutdown import shutdown_record
from scripts.shutdown.test_support import ShutdownTestCase


class ShutdownRecordTests(ShutdownTestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"), "HERDR_BIN": "/nonexistent/herdr"})))

    def test_shutdown_rerun_after_first_worker_stop(self) -> None:
        owner = fixture_member("showrunner", name="record-owner")
        unit = fixture_member("unit", name="retained-unit", parent=owner.id)
        first = fixture_member("worker", name="impl", parent=unit.id)
        second = fixture_member("worker", name="test", parent=unit.id)
        shutdown_record.update(started=[], pending=[unit.id, first.id, second.id, first.id])
        shutdown_record.update(started=[first.id], pending=[unit.id, second.id])
        shutdown_record.update(started=[first.id], pending=[unit.id, second.id])
        self.assertEqual(shutdown_record.read(), [unit.id, second.id])
        self.assertCountEqual(staff.snapshot().find(kind="worker", parent=unit.id), [first, second])

    def test_migration_resumes_after_first_file_and_finished_rerun_changes_nothing(self) -> None:
        owner = fixture_member("showrunner", name="record-owner")
        unit = fixture_member("unit", name="old", parent=owner.id)
        unit = staff.update_current_session(unit.current_session,
            staff.native_id(fixture_runtime_id("record-unit-current")))
        shutdown_record.save(["old", str(owner.current_session), str(unit.current_session)])
        staff_before = staff.snapshot().rows

        changed = shutdown_record.migrate(stop_after_first=True)

        self.assertEqual(len(changed), 1)
        self.assertEqual(shutdown_record.read(), [str(unit.id), str(owner.id)])
        path = shutdown_record.record_path()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(shutdown_record.migrate(), ())
        self.assertEqual(shutdown_record.migrate(), ())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(staff.snapshot().rows, staff_before)

    def test_migration_converts_known_entries_and_preserves_missing_entries_on_rerun(self) -> None:
        owner = fixture_member("showrunner", name="record-owner")
        unit = fixture_member("unit", name="known", parent=owner.id)
        shutdown_record.save([unit.name, "missing"])
        path = shutdown_record.record_path()
        staff_before = staff.snapshot().rows
        errors = io.StringIO()

        with redirect_stderr(errors):
            self.assertEqual(shutdown_record.migrate(), (str(path),))
        self.assertEqual(shutdown_record.read(), [str(unit.id), "missing"])
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        with redirect_stderr(errors):
            self.assertEqual(shutdown_record.migrate(), ())
            self.assertEqual(shutdown_record.migrate(), ())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns), before)
        self.assertEqual(errors.getvalue().splitlines(),
            ["missing: migration anomaly: staff ownership is unavailable"] * 3)
        self.assertEqual(staff.snapshot().rows, staff_before)

    def test_migration_resolves_ownership_again_after_clear_without_a_second_row(self) -> None:
        owner = fixture_member("showrunner", name="record-owner")
        unit = fixture_member("unit", name="clear", parent=owner.id)
        shutdown_record.save([unit.name])
        before = staff.snapshot()
        cleared = replace(unit, current_session=staff.native_id(fixture_runtime_id("cleared-session")))
        after = staff.StaffSnapshot(tuple(cleared if row.id == unit.id else row for row in before.rows))
        saved = shutdown_record.record_path().read_bytes()
        with patch.object(staff, "snapshot", side_effect=[before, after]):
            with self.assertRaisesRegex(ValueError, "ownership changed"):
                _ = shutdown_record.migrate()
        self.assertEqual(shutdown_record.record_path().read_bytes(), saved)

        with patch.object(staff, "snapshot", return_value=after):
            self.assertEqual(len(shutdown_record.migrate()), 1)
        self.assertEqual(shutdown_record.read(), [str(unit.id)])
        self.assertEqual(staff.snapshot().rows, before.rows)

    def test_record_rejects_a_copied_identity_object_without_replacing_it(self) -> None:
        path = shutdown_record.record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps([{"session_id": "native-session", "unit": "copied-name"}])
        _ = path.write_text(content, encoding="utf-8")
        with self.assertRaises(ValueError):
            _ = shutdown_record.read()
        self.assertEqual(path.read_text(encoding="utf-8"), content)
