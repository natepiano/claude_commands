"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit
from scripts.production import staff
from scripts.production.staff_test_support import fixture_showrunner_name, unit_membership


from add_unit_test_support import AddUnitTestCase, configure_test_zone


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitRestoreTests(AddUnitTestCase):


    def test_named_designer_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_named_unit_launch_and_restart("designers")

    def test_named_reviewer_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_named_unit_launch_and_restart("reviewers")

    def test_named_showrunner_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_named_unit_launch_and_restart("showrunners")

    def test_named_unit_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_named_unit_launch_and_restart("units")


    def test_exact_table_row_is_adopted_without_suffix_in_another_production(self) -> None:
        row = self.unit_row("designers", branch="build-followups-designers",
                            worktree=self.root / "project-designers").replace("| designers-unit |", "| `designers` |")
        self.commit_prepared_row(row)
        member = unit_membership(fixture_showrunner_name("build-followups", self.doc), "designers")
        _ = staff.set_worktree(member.id, staff.PresentWorktree("natedev", self.root / "project-designers"), expected=member.worktree)
        request = add_unit.launch_request(argparse.Namespace(
            production=str(self.doc), name="designers", plan="docs/plans/given.md", brief=None,
            standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
            resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
        ))
        self.assertEqual(request.unit, "designers")
        self.assertIsInstance(add_unit.row_is_present(request, self.doc.read_text().splitlines(), staff.snapshot()),
                              add_unit.ExistingUnitRow)

    def test_a_name_ending_in_unit_launches_as_the_name_without_it(self) -> None:
        def request(name: str) -> tuple[str, str, Path, str]:
            launched = add_unit.launch_request(argparse.Namespace(
                production=str(self.doc), name=name, plan="docs/plans/given.md", brief=None,
                standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
                resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
            ))
            return launched.unit, launched.branch, launched.worktree, launched.requested_name

        expected = request("feature-log")
        self.assertEqual(expected[0], "feature-log-unit")
        self.assertEqual(request("feature-log-unit"), expected)
        self.assertEqual(request("feature_log_unit"), request("feature_log"))
        self.assertEqual(request("unit")[0], "unit-unit")





if __name__ == "__main__":
    _ = unittest.main()
