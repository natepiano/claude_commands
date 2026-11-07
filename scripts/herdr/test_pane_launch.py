"""Retain the launch contract and remove retired identity at caller boundaries."""
from __future__ import annotations

import unittest
from unittest.mock import patch


from scripts.herdr import pane_launch
from scripts.production import session_labels, staff
from scripts.lib import legacy_identity
from scripts.message import sessions
from scripts.shutdown import pane_record
from scripts.herdr import herdr


class LaunchEnvironmentTests(unittest.TestCase):
    def test_launch_retains_prefixed_environment_for_its_existing_contract(self) -> None:
        entry = pane_launch.launch("pane", "native", "/work", {"SHOWRUNNER_UNIT": "demo"}, ["claude"])
        self.assertEqual(entry["environment"], {"SHOWRUNNER_UNIT": "demo"})

    def test_shutdown_launch_removes_retired_identity_before_the_locked_launch(self) -> None:
        entry = pane_record.launch("pane", "native", "/work", {"SHOWRUNNER_UNIT": "demo"}, ["claude"])
        self.assertEqual(entry["environment"], {})

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
