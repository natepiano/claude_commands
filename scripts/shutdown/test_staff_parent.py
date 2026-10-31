"""Unit restarts read the current staff edge, never saved parent fields."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff
from scripts.accounts import accounts
from scripts.whoami.account import Account
from scripts.production.staff_test_support import StaffTestCase
import restart
import shutdown_record
from test_restart import entry, unit, STOPPED


class StaffParentTests(StaffTestCase):
    def saved_unit(self) -> shutdown_record.SessionShutdown:
        old = self.production("demo", "owner-unit")
        parent = staff.ensure("showrunner", name="demo")
        member = staff.ensure("unit", name="owner", parent=parent.id)
        self.pane("w1:p1", session_id="owner", environment={
            "STAFF_ID": member.id, "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "owner-unit"})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")})))
        return entry(unit("owner", doc=str(old)))

    def test_restart_uses_current_staff_parent_after_transfer(self) -> None:
        saved = self.saved_unit()
        destination = self.production("other", "owner-unit")
        parent = staff.ensure("showrunner", name="other")
        member = staff.snapshot().by_path("demo/owner")
        assert member is not None
        _ = staff.move(member.id, parent.id)
        with patch.object(accounts, "account_for_group_launch", return_value=Account("claude", "fixture@example.com", "claude2")):
            argv = restart._unit_argv(saved)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(argv[argv.index("--production") + 1], str(destination))
        self.assertNotIn("production", saved["session"]["host"])
        self.assertNotIn("doc", saved["session"]["host"])

    def test_legacy_record_parent_fields_load_and_are_ignored(self) -> None:
        saved = self.saved_unit()
        wire = cast(dict[str, object], json.loads(json.dumps(saved)))
        owner = cast(dict[str, object], wire["session"])
        host = cast(dict[str, object], owner["host"])
        host.update(production={"obsolete": "ignored"}, doc=["obsolete", "ignored"])
        parsed = shutdown_record.parse_session_records(json.dumps([wire]))[0]
        self.assertNotIn("production", parsed["session"]["host"])
        self.assertNotIn("doc", parsed["session"]["host"])
        self.assertEqual(parsed, saved)

    def test_missing_staff_entry_reports_one_line_without_launch(self) -> None:
        saved = self.saved_unit()
        member = staff.snapshot().by_path("demo/owner")
        assert member is not None
        _ = staff.retire(member.id)
        output = io.StringIO()
        def launch(*_args: object, **_kwargs: object) -> restart.SessionLaunched:
            _ = restart._unit_argv(saved)  # pyright: ignore[reportPrivateUsage]
            self.fail("a unit without a staff entry must not launch")
        with (patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_launch_recovery_record", side_effect=launch), redirect_stdout(output)):
            _ = restart.launch_entry(saved, STOPPED, restart.DesktopLaunchState(), dry_run=True)
        self.assertEqual(output.getvalue().splitlines(), ["cannot restart owner: owner-unit: no unique staff entry"])
