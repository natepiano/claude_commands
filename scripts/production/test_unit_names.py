"""Production unit ids share branch, worktree and session naming."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final

from scripts.production import unit_names


@final
class UnitNamesTests(unittest.TestCase):
    def test_branch_and_worktree_use_the_short_name(self) -> None:
        for stem, expected in (
            ("names", "names"),
            ("cargo-liner-style-names", "cargo-liner-style-names"),
            ("cargo-liner-stylex-names", "cargo-liner-stylex-names"),
        ):
            with self.subTest(stem=stem):
                self.assertEqual(unit_names.branch(stem), expected)
                self.assertEqual(unit_names.worktree(stem), Path.home() / "worktrees" / expected)

    def test_full_unit_id_keeps_its_prefix_through_normalization(self) -> None:
        unit = unit_names.unit_id("cargo-liner-style-mul_add-unit")
        self.assertEqual(unit, "cargo-liner-style-mul_add-unit")
        self.assertEqual(unit_names.branch(unit_names.stem(unit)), "cargo-liner-style-mul_add")
