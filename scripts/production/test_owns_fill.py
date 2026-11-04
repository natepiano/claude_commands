"""Carry the Owns paths into each unit's private configuration."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
from typing import override

from scripts.production import owns_fill, unit_config
from scripts.production.staff_test_support import StaffTestCase, recorded_showrunner


class OwnsFillTests(StaffTestCase):
    doc: Path = Path()
    folder: Path = Path()
    source: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.doc = self.fill_production("demo", "models")
        _ = recorded_showrunner("demo", self.doc)
        self.folder = self.source_root / "worktrees/models"
        self.source = self.folder / ".unit.toml"
        self.owns_cell("`scripts/models/`, `docs/models.md`")

    def owns_cell(self, cell: str) -> None:
        lines = ("# Production — demo",
                 "- **Showrunner session:** `demo`",
                 f"- **Showrunner checkout:** `{self.doc.parents[1]}`",
                 "- **Merge branch:** `main`",
                 "- **Log:** `production.log`",
                 "",
                 "## Units",
                 "",
                 "| Unit | Owns |",
                 "|---|---|",
                 f"| models | {cell} |")
        _ = self.doc.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def result(self, *arguments: str) -> tuple[int, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = owns_fill.main(list(arguments))
        return code, output.getvalue() + errors.getvalue()

    def responsibilities(self) -> tuple[str, ...]:
        return unit_config.parse(self.source.read_text(encoding="utf-8")).responsibilities

    def file_bytes(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_nonempty_owns_cell_carries_exact_paths(self) -> None:
        self.assertEqual(self.responsibilities(), ())
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.responsibilities(), ("docs/models.md", "scripts/models/"))

    def test_bare_sibling_name_uses_the_backticked_parent_path(self) -> None:
        self.owns_cell("`scripts/production/staff.py`, `staff_sources.py`")
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.responsibilities(),
                         ("scripts/production/staff.py", "scripts/production/staff_sources.py"))

    def test_parenthetical_file_list_is_refused_without_changing_configuration(self) -> None:
        self.owns_cell("production helpers (`staff.py`, `staff_sources.py`)")
        before = self.source.read_bytes()
        code, output = self.result()
        self.assertEqual(code, 1, output)
        self.assertIn("demo", output)
        self.assertIn("models", output)
        self.assertEqual(self.source.read_bytes(), before)

    def test_board_free_cell_carries_the_board_free_responsibility(self) -> None:
        self.owns_cell("none — takes what the board leaves free")
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.responsibilities(), ("board-free",))

    def test_existing_nightly_review_responsibility_stays(self) -> None:
        config = unit_config.parse(self.source.read_text(encoding="utf-8"))
        _ = unit_config.write(self.folder, replace(config, responsibilities=("nightly-review",)))
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.responsibilities(),
                         ("docs/models.md", "nightly-review", "scripts/models/"))

    def test_rerun_reports_already_current_without_changing_bytes(self) -> None:
        code, output = self.result()
        self.assertEqual(code, 0, output)
        before = self.file_bytes()
        code, output = self.result()
        self.assertEqual(code, 0, output)
        self.assertIn("already current", output)
        self.assertEqual(self.file_bytes(), before)

    def test_dry_run_writes_nothing(self) -> None:
        before = self.file_bytes()
        code, output = self.result("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.file_bytes(), before)
