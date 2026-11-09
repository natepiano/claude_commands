"""Test style configuration with private documents, repositories and staff records."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import override
from unittest import mock

from scripts.production import staff
from scripts.production.staff_test_support import fixture_unit_location, isolated_staff_file, recorded_showrunner
from scripts.style import style_config
from scripts.style.ledger_test_support import DEFAULT_STYLE_ASSIGNMENTS, style_staff_fixture, write_style_units
from scripts.style.style_config import (
    ParentUnitRoute, StyleConfigError, StyleRepositoryConfig, StyleShowrunnerRoute, StyleUnitAssignment,
)


@dataclass(frozen=True)
class AcceptedConfig:
    assignments: tuple[StyleUnitAssignment, ...]


@dataclass(frozen=True)
class RefusedConfig:
    message: str


class StyleConfigTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    doc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.doc = self.repo / "production.md"
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr"}))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.git("init", "-q", "--initial-branch=cargo-liner-style")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Create fixture", "--allow-empty")

    def git(self, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True)

    def document(self, kind: str = "style", table: str = "") -> None:
        _ = self.doc.write_text(
            f"# Production\n- **Kind:** {kind}\n- **Merge branch:** integration/main\n" +
            f"- **Showrunner checkout:** {self.repo}\n- **Branch point:** refs/heads/product/main\n" + table)

    def read(self) -> StyleRepositoryConfig:
        return style_config.read_config(self.doc, unit_branch="cargo-liner-style")

    def test_configuration_table(self) -> None:
        all_families = "names, values, types, modules"
        combined = (StyleUnitAssignment("cargo-liner-style", ("names", "values", "types", "modules")),)
        four = (
            StyleUnitAssignment("cargo-names", ("names",)),
            StyleUnitAssignment("cargo-values", ("values",), 3),
            StyleUnitAssignment("cargo-types", ("types",)),
            StyleUnitAssignment("cargo-modules", ("modules",)),
        )
        cases: tuple[tuple[str, str, AcceptedConfig | RefusedConfig], ...] = (
            ("one unit", f"| cargo-liner-style | {all_families} | |", AcceptedConfig(combined)),
            ("four units", "\n".join(f"| {row.unit} | {row.families[0]} | {row.open_tickets} |" for row in four),
             AcceptedConfig(four)),
            ("missing family", "| cargo | names, values, types | 1 |", RefusedConfig("modules: no style owner")),
            ("two owners", f"| cargo | {all_families} | 1 |\n| other | names | 1 |",
             RefusedConfig("names: two style owners: cargo, other")),
            ("unknown family", f"| cargo | {all_families}, other | 1 |",
             RefusedConfig("cargo: unknown style family 'other'")),
            ("duplicate unit", "| cargo | names, values | 1 |\n| cargo | types, modules | 1 |",
             RefusedConfig("cargo: duplicate style unit")),
            ("zero capacity", f"| cargo | {all_families} | 0 |",
             RefusedConfig("cargo: Open tickets must be a positive integer")),
            ("negative capacity", f"| cargo | {all_families} | -1 |",
             RefusedConfig("cargo: Open tickets must be a positive integer")),
            ("fractional capacity", f"| cargo | {all_families} | 1.5 |",
             RefusedConfig("cargo: Open tickets must be a positive integer")),
            ("text capacity", f"| cargo | {all_families} | many |",
             RefusedConfig("cargo: Open tickets must be a positive integer")),
            ("missing section", "", RefusedConfig("production doc lacks Style units section")),
        )
        for name, rows, expected in cases:
            with self.subTest(name=name):
                table = "\n## Style units\n\n| Unit | Families | Open tickets |\n| --- | --- | --- |\n" + rows if rows else ""
                self.document(table=table)
                if isinstance(expected, RefusedConfig):
                    with self.assertRaises(StyleConfigError) as caught:
                        _ = self.read()
                    self.assertEqual(str(caught.exception), expected.message)
                else:
                    config = self.read()
                    self.assertEqual(config.assignments, expected.assignments)
                    for assignment in expected.assignments:
                        self.assertEqual(config.assignment(assignment.unit), assignment)
                        for family in assignment.families:
                            self.assertEqual(config.owner(family), assignment)

    def test_routes_and_unit_lookup(self) -> None:
        for kind in ("style", "product", ""):
            with self.subTest(kind=kind):
                self.document(kind)
                write_style_units(self.doc, (StyleUnitAssignment("cargo-liner-style", ("names", "values", "types", "modules")),))
                parent = recorded_showrunner("cargo-liner", self.doc)
                unit = fixture_unit_location(parent, "cargo-liner-style", self.repo)
                record = staff.initial_snapshot()
                config = style_config.config_for_unit(unit.name, record)
                route = config.route
                self.assertEqual(route.doc, self.doc)
                self.assertEqual(route.checkout, self.repo)
                self.assertEqual(route.receiving_branch, "integration/main")
                if kind == "style":
                    self.assertIsInstance(route, StyleShowrunnerRoute)
                    self.assertEqual(route.branch_point, "product/main")
                else:
                    self.assertIsInstance(route, ParentUnitRoute)
                    assert isinstance(route, ParentUnitRoute)
                    self.assertEqual(route.branch_point, "integration/main")
                    self.assertEqual(route.unit_branch, "cargo-liner-style")
                self.assertEqual(style_config.config_for_unit(unit, record), config)
                self.assertEqual(style_config.config_for_unit(str(unit.id), record), config)

    def test_document_reads_once_without_cache(self) -> None:
        self.document()
        write_style_units(self.doc)
        read_text = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text) as reader:
            self.assertEqual(self.read().assignments, DEFAULT_STYLE_ASSIGNMENTS)
            self.assertEqual(reader.call_count, 1)
        replacement = (StyleUnitAssignment("all-style", ("names", "values", "types", "modules"), 3),)
        write_style_units(self.doc, replacement)
        self.assertEqual(self.read().assignments, replacement)
        with self.assertRaisesRegex(StyleConfigError, "missing: no style assignment"):
            _ = self.read().assignment("missing")

    def test_ledger_fixture_assignments(self) -> None:
        combined = (StyleUnitAssignment("cargo-liner-style", ("names", "values", "types", "modules")),)
        for assignments in (DEFAULT_STYLE_ASSIGNMENTS, combined):
            with self.subTest(assignments=assignments), style_staff_fixture(self.root, self.repo, assignments):
                config = style_config.read_config(self.repo / ".git/style-production.md", unit_branch="cargo-liner-style")
                self.assertEqual(config.assignments, assignments)
                parent = recorded_showrunner("sample-style", self.repo / ".git/style-production.md")
                for assignment in assignments:
                    self.assertEqual(len([row for row in staff.initial_snapshot().find(kind="unit", parent=parent.id)
                                          if row.name == assignment.unit]), 1)


if __name__ == "__main__":
    _ = unittest.main()
