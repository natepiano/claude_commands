"""Global unit-name writes reject new collisions while legacy rows stay readable."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase


class StaffUnitNameTests(StaffTestCase):
    def test_alias_collisions_preserve_spelling_and_refuse_new_writes(self) -> None:
        showrunner, unit, _worker = self.tree()
        unit = staff.rename(unit.id, "mul_add")
        other = staff.ensure("showrunner", name="other")
        free = staff.ensure("unit", name="free", parent=other.id)
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff.ensure("unit", name="mul-add", parent=other.id)
        with self.assertRaises(staff.StaffRefused):
            _ = staff.rename(free.id, "mul-add")
        seed = staff.SeedInput((
            staff.SeedMember("parent", "showrunner", other.name, other.id, staff.KeepSeedParent()),
            staff.SeedMember("alias", "unit", "mul-add", staff.NewSeedMember(), staff.SeedParent("parent")),
        ), ())
        with self.assertRaises(staff.StaffRefused):
            _ = staff.apply_seed(seed)
        self.assertEqual(self.staff_file.read_bytes(), before)
        fields = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        rows = [replace(row, name="mul-add") if row.id == free.id else row for row in staff.snapshot().rows]
        fields["rows"] = [row.to_json() for row in rows]
        self.write_json(self.staff_file, fields)
        before = self.staff_file.read_bytes()
        found = self.cli("unit-name-collisions", "--dry-run", "--json")
        self.assertEqual(found.returncode, 0, found.stderr)
        collisions = cast(dict[str, list[dict[str, str]]], json.loads(found.stdout))
        self.assertEqual({entry["name"] for entry in collisions["mul-add"]}, {"mul_add", "mul-add"})
        self.assertEqual(self.staff_file.read_bytes(), before)
        _ = staff.rename(showrunner.id, "renamed")
        self.assertEqual(staff.snapshot().by_id(unit.id), unit)
        with self.assertRaises(staff.StaffRefused):
            _ = staff.ensure("unit", name="mul_add", parent=other.id)
        _ = staff.rename(free.id, "cleared")
        self.assertEqual(staff.unit_name_collisions(staff.snapshot()), {})

    def test_new_global_collision_refuses_ensure_rename_and_seed_without_writes(self) -> None:
        _showrunner, unit, _worker = self.tree()
        other = staff.ensure("showrunner", name="other")
        free = staff.ensure("unit", name="free", parent=other.id)
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff.ensure("unit", name=unit.name, parent=other.id)
        self.assertEqual(self.staff_file.read_bytes(), before)
        with self.assertRaises(staff.StaffRefused):
            _ = staff.rename(free.id, unit.name)
        self.assertEqual(self.staff_file.read_bytes(), before)
        seed = staff.SeedInput((
            staff.SeedMember("parent", "showrunner", other.name, other.id, staff.KeepSeedParent()),
            staff.SeedMember("duplicate", "unit", unit.name, staff.NewSeedMember(), staff.SeedParent("parent")),
        ), ())
        with self.assertRaises(staff.StaffRefused):
            _ = staff.apply_seed(seed)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_legacy_collisions_are_discovered_without_writes_and_can_be_cleared(self) -> None:
        _showrunner, unit, _worker = self.tree()
        other = staff.ensure("showrunner", name="other")
        duplicate = staff.ensure("unit", name="available", parent=other.id)
        fields = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        rows = [replace(row, name=unit.name) if row.id == duplicate.id else row for row in staff.snapshot().rows]
        fields["rows"] = [row.to_json() for row in rows]
        self.write_json(self.staff_file, fields)
        before = self.staff_file.read_bytes()
        record = staff.snapshot()
        self.assertEqual({row.id for row in record.find(kind="unit") if row.name == unit.name},
                         {unit.id, duplicate.id})
        found = self.cli("unit-name-collisions", "--dry-run", "--json")
        collisions = cast(dict[str, list[dict[str, str]]], json.loads(found.stdout))
        self.assertEqual({entry["id"] for entry in collisions[unit.name]}, {unit.id, duplicate.id})
        self.assertEqual(self.staff_file.read_bytes(), before)
        _ = staff.rename(duplicate.id, "cleared")
        self.assertEqual(json.loads(self.cli("unit-name-collisions", "--dry-run", "--json").stdout), {})
        self.assertEqual(staff.snapshot().by_id(unit.id), unit)
