"""Pruning every production's phase notes, against stand-in timers and a scratch vault."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from scripts.production.staff_test_support import isolated_staff_file, unit_membership

SCRIPT = Path(__file__).with_name("prune_phase_notes.py")


def note(unit: str, production: str = "example") -> str:
    return f"---\nphase_table: true\nproduction: {production}\nunit: {unit}\n---\n\n# {unit}\n"


class PrunePhaseNotesTests(unittest.TestCase):
    root: Path = Path()
    vault: Path = Path()
    notifier: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.vault = self.root / "vault/showrunners"
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()
        self.env = {**os.environ, "HOME": str(self.root / "home"), "NOTIFIER_STATE_DIR": str(self.notifier),
                    "PHASE_TABLE_VAULT": str(self.vault), "USER_ZONE": "America/New_York",
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "HERDR_BIN": "/nonexistent/herdr"}
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.env))

    def production(self, slug: str) -> Path:
        """A production with one live unit, alpha-unit, and its update timer."""
        checkout = self.root / slug
        alpha = self.root / f"{slug}-alpha"
        alpha.mkdir(parents=True)
        # A linked worktree, where `.git` is a file: herdr is never asked whether its unit is gone.
        _ = (alpha / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        doc = checkout / f"docs/{slug}-production.md"
        doc.parent.mkdir(parents=True)
        _ = doc.write_text("\n".join((
            f"# Production — {slug}", "", "## Production Context", "",
            "- **Merge branch:** `production`", f"- **Showrunner checkout:** `{checkout}`",
            "- **Log:** `production.log`", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| `alpha-unit` | `docs/alpha.md` | `{alpha}` | `alpha` | — | — |", "",
        )), encoding="utf-8")
        self.timer(slug, doc)
        _ = unit_membership(slug, "alpha-unit")
        return doc

    def timer(self, slug: str, doc: Path) -> None:
        instance = self.notifier / f"showrunner-{slug}"
        instance.mkdir()
        _ = (instance / "conf").write_text(
            f"TARGET=session:x\nCHECK=zsh /home/x/.claude/scripts/production/production_check.sh {doc}\n",
            encoding="utf-8")

    def planted(self, unit: str, production: str = "example") -> Path:
        path = self.vault / "natedev" / f"{unit}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(note(unit, production), encoding="utf-8")
        return path

    def run_script(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(SCRIPT)], env=self.env,
                              capture_output=True, text=True, check=False)

    def test_a_retired_units_note_is_archived_and_a_live_ones_stays(self) -> None:
        _ = self.production("example")
        live = self.planted("alpha-unit")
        retired = self.planted("beta-unit")
        # Another production's note for the same unit name is that production's to prune.
        other = self.vault / "elsewhere" / "beta-unit.md"
        other.parent.mkdir(parents=True)
        _ = other.write_text(note("beta-unit", "other"), encoding="utf-8")

        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(live.exists())
        self.assertFalse(retired.exists())
        self.assertTrue((self.root / "vault/archive/natedev/example/beta-unit.md").exists())
        self.assertTrue(other.exists())

    def test_a_production_that_fails_is_reported_and_the_next_is_still_pruned(self) -> None:
        self.timer("aaa-broken", self.root / "missing/aaa-broken-production.md")
        _ = self.production("example")
        retired = self.planted("beta-unit")

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertIn("phase notes not pruned for aaa-broken: cannot read production doc", result.stderr)
        self.assertNotIn("example", result.stderr)
        self.assertFalse(retired.exists())

    def test_a_timer_with_no_production_check_is_reported(self) -> None:
        instance = self.notifier / "showrunner-odd"
        instance.mkdir()
        _ = (instance / "conf").write_text("TARGET=session:x\n", encoding="utf-8")

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "phase notes not pruned for odd: CHECK is missing\n")

    def test_nothing_to_prune_prints_nothing(self) -> None:
        _ = self.production("example")
        live = self.planted("alpha-unit")
        # Only update timers name productions: other notifier jobs are not read.
        (self.notifier / "stall-watch").mkdir()

        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(live.exists())

    def test_no_vault_and_no_timers_is_quiet(self) -> None:
        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    _ = unittest.main()
