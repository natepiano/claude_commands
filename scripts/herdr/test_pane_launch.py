"""Retain the launch contract and remove retired identity at caller boundaries."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from typing import Literal
from unittest.mock import patch


from scripts.herdr import pane_launch
from scripts.production import session_labels, staff
from scripts.lib import legacy_identity
from scripts.message import sessions
from scripts.shutdown import pane_record
from scripts.herdr import herdr


class LaunchEnvironmentTests(unittest.TestCase):
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
