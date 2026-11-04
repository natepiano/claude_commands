"""Production rename: workers behavior."""

from __future__ import annotations

import os
import unittest
from typing import final
from unittest import mock

from scripts.astra import design_roles
from scripts.astra.design_role_test_support import write_empty_binding_record
from scripts.production import rename_test_support as support, staff
from scripts.production.rename_test_support import NEW, OLD


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_composite_rename_warns_once_across_the_showrunner_and_unit_moves(self) -> None:
        self.prepare_unit_moves()
        self.env['DESIGN_ROLES_STATE'] = str(self.root / 'state/astra/design-roles')
        with mock.patch.dict(os.environ, self.env):
            bad = write_empty_binding_record(self.checkout, design_roles.UnitHolder('unrelated', 'other-unit'),
                                             lifecycle=design_roles.RoleOpen(staff.StaffId('unrelated-thread')))
        with design_roles.record_operation():
            results = [self.run_rename('apply'), self.run_command('unit', NEW, 'alpha-unit'),
                       self.run_command('unit', NEW, 'beta-unit')]
        for result in results:
            self.assert_success(result)
        self.assertEqual([line for result in results for line in result.stderr.splitlines()],
                         [f'warning: {bad}: missing or invalid repository'])
        self.assert_unit_moved('alpha-unit')
        self.assert_unit_moved('beta-unit')
        self.assertTrue(bad.is_file())



    def test_held_roles_move_to_the_new_slug_with_the_showrunner_and_each_unit(self) -> None:
        self.prepare_unit_moves()
        config = self.root / "design_roles.conf"
        self.write(config, "[ownership]\nhost=fixture-host\n")
        self.env.update({"DESIGN_ROLES_CONFIG": str(config), "DESIGN_ROLES_HOST": "fixture-host",
                         "DESIGN_ROLES_STATE": str(self.root / "state/astra/design-roles"),
                         "DESIGN_OWNERSHIP_STATE": str(self.root / "state/astra/design-ownership")})
        old_unit, new_unit = self.unit_worktree(OLD, "alpha-unit"), self.unit_worktree(NEW, "alpha-unit")
        showrunner = self.held_design("showrunner-repo", self.checkout, design_roles.ShowrunnerHolder(OLD))
        unit = self.held_design("unit-repo", old_unit, design_roles.UnitHolder(OLD, "alpha-unit"))
        self.assert_success(self.run_rename("apply"))
        current, cwd = self.read_held(showrunner)
        self.assertEqual(current.holder, design_roles.ShowrunnerHolder(NEW))
        self.assertEqual((current.repository, current.roles, cwd), (str(self.checkout), showrunner.roles, str(self.checkout)))
        self.assertEqual(self.read_held(unit)[0].holder, design_roles.UnitHolder(OLD, "alpha-unit"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")
        current, cwd = self.read_held(unit)
        self.assertEqual(current.holder, design_roles.UnitHolder(NEW, "alpha-unit"))
        self.assertEqual((current.repository, current.document, cwd), (str(new_unit), str(new_unit / "design.md"), str(new_unit)))
        self.assertEqual((current.roles, current.placement.generation), (unit.roles, 2))


if __name__ == "__main__":
    _ = unittest.main()
