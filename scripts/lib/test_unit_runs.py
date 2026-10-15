"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs  # noqa: E402


class UnitRunsTest(unittest.TestCase):
    def test_both_roots_are_read_the_new_name_first(self) -> None:
        self.assertEqual(unit_runs.run_roots(), (Path("/tmp/claude/unit"), Path("/tmp/claude/delegate")))
        self.assertEqual(unit_runs.active_dirs(), (Path("/tmp/claude/unit/active"), Path("/tmp/claude/delegate/active")))

    def test_an_override_replaces_both_names(self) -> None:
        self.assertEqual(unit_runs.run_roots("/x/runs"), (Path("/x/runs"),))
        self.assertEqual(unit_runs.active_dirs("/x/active"), (Path("/x/active"),))

    def test_the_marker_is_the_first_that_exists_else_the_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, old = Path(folder) / "unit", Path(folder) / "delegate"
            with patch.object(unit_runs, "RUN_ROOT", new), patch.object(unit_runs, "FORMER_RUN_ROOT", old):
                self.assertEqual(unit_runs.marker("s1"), new / "active/s1")
                (old / "active").mkdir(parents=True)
                _ = (old / "active/s1").write_text("run\n")
                self.assertEqual(unit_runs.marker("s1"), old / "active/s1")
                (new / "active").mkdir(parents=True)
                _ = (new / "active/s1").write_text("run\n")
                self.assertEqual(unit_runs.marker("s1"), new / "active/s1")

    def test_a_run_under_the_former_name_keeps_its_instance(self) -> None:
        self.assertEqual(unit_runs.instance(Path("/tmp/claude/unit/abc")), "unit-abc")
        self.assertEqual(unit_runs.instance(Path("/tmp/claude/delegate/abc")), "delegate-abc")
        self.assertEqual(unit_runs.instance(Path("/scratch/runs/abc")), "unit-abc")
        self.assertTrue(all("unit-abc".startswith(prefix) or "delegate-abc".startswith(prefix)
                            for prefix in unit_runs.INSTANCE_PREFIXES))


if __name__ == "__main__":
    _ = unittest.main()
