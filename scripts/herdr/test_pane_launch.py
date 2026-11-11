"""Retain the launch contract and remove retired identity at caller boundaries."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from typing import Literal, override
from unittest.mock import patch


from scripts.herdr import pane_launch
from scripts.production import session_labels, staff
from scripts.production.staff_test_support import isolated_staff_file
from scripts.lib import legacy_identity
from scripts.message import sessions
from scripts.agents import pane_record
from scripts.herdr import herdr


class LaunchEnvironmentTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        _ = self.enterContext(isolated_staff_file(Path(directory) / "staff.json", {}))

    def test_launch_does_not_retain_staff_environment(self) -> None:
        policy = ["--sandbox", "read-only", "-c", 'model_reasoning_effort="high"',
                  "--ask-for-approval", "never", "-c", "notice.hide_rate_limit_model_nudge=true",
                  "-c", 'service_tier="fast"', "-c", 'notify=["python3","notify.py"]']
        rows: tuple[tuple[Literal["claude", "codex"], list[str], list[str]], ...] = (
            ("claude", ["claude"], []),
            ("codex", ["codex", "--model", "sol", *policy], policy),
            ("codex", ["codex", "--sandbox=read-only", '-c=model_reasoning_effort="high"',
                       "--ask-for-approval=never", "-c", "notice.hide_rate_limit_model_nudge=true",
                       '-c=service_tier="fast"', '-c=notify=["python3","notify.py"]'], policy),
            ("codex", ["codex", "-C", "/work", "-c", 'projects={"/old"={trust_level="trusted"}}',
                       '-c=projects={"/older"={trust_level="trusted"}}', "resume", "native", "--model", "m", *policy], policy),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panes.json"
            for kind, arguments, expected in rows:
                with self.subTest(kind=kind, arguments=arguments):
                    entry = pane_launch.launch("pane", "native", "/work", {"SHOWRUNNER_UNIT": "demo"}, arguments, kind=kind)
                    self.assertEqual(entry["environment"], {})
                    pane_launch.save(path, {"pane": entry})
                    retained = pane_launch.read_strict(path)["pane"]
                    if kind == "codex":
                        self.assertEqual(retained.get("codex_policy"), expected)
                    else:
                        self.assertNotIn("codex_policy", retained)

    def test_shutdown_launch_removes_retired_identity_before_the_locked_launch(self) -> None:
        entry = pane_record.launch("pane", "native", "/work", {"SHOWRUNNER_UNIT": "demo"}, ["claude"])
        self.assertEqual(entry["environment"], {})

    def test_readout_keeps_a_staffed_pane_with_an_unknown_home(self) -> None:
        row = staff.StaffRow(staff.StaffId("TEST-unknown-home"), "producer", "TEST-unknown-home", staff.STAFF_ROOT,
                             current_session=staff.StaffId("TEST-unknown-home"))
        entry = pane_launch.launch("TEST-pane", row.id, "/work", {}, ["claude"])
        _ = entry.pop("config_dir", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST-panes.json"
            entries: dict[str, pane_launch.PaneLaunch] = {}
            for index in range(3):
                current = entry.copy()
                current["pane_id"] = "TEST-pane-" + str(index)
                entries[current["pane_id"]] = current
            pane_launch.save(path, entries)
            with patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))) as snapshot:
                self.assertIsInstance(pane_launch.config_home(entry), staff.HomeUnknown)
                snapshot.reset_mock()
                self.assertEqual(pane_launch.read_valid_entries(path), entries)
                snapshot.assert_called_once_with()
                snapshot.reset_mock()
                self.assertEqual(pane_launch.read_valid_entries(path, record=staff.StaffSnapshot((row,))), entries)
                snapshot.assert_not_called()
            self.assertEqual(pane_launch.read_strict(path), entries)

    def test_environment_boundaries_use_the_shared_identity_filter(self) -> None:
        environment = {"STAFF_ID": "old", "SHOWRUNNER_UNIT": "old", "CODEX_THREAD_ID": "native", "PATH": "/bin"}
        expected = {"CODEX_THREAD_ID": "native", "PATH": "/bin"}
        self.assertEqual(legacy_identity.without_legacy_identity(environment), expected)
        self.assertEqual(environment["STAFF_ID"], "old")
        labels = session_labels.StaffLabels(staff.StaffSnapshot((), production=False))
        entry = pane_launch.launch("pane", "native", "/work", {}, ["claude"])
        entry["environment"] = environment
        with (patch.object(legacy_identity, "without_legacy_identity", return_value=expected) as remove,
              patch.object(sessions, "read_session", return_value=sessions.UnreadableSessionRecord.FOUND)):
            self.assertEqual(pane_launch.RecordedLabels().resolve(1, environment), expected)
            self.assertEqual(labels.environment(environment, "native"), expected)
            self.assertEqual(pane_record.without_legacy_identity(entry)["environment"], expected)
            self.assertEqual(herdr.child_environment(environment), expected)
        self.assertEqual(remove.call_count, 4)


if __name__ == "__main__":
    _ = unittest.main()
