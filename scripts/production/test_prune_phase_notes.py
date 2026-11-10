"""Pruning every production's phase notes, against stand-in timers and a scratch vault."""

from __future__ import annotations

from scripts.agents import remote

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from scripts.production import staff, staff_test_support
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, unit_membership

SCRIPT = Path(__file__).with_name("prune_phase_notes.py")


def note(unit: staff.StaffRow) -> str:
    return f"---\nphase_table: true\nunit: {unit.id}\n---\n\n# {unit.name}\n"


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
        write_text_atomic(self.root / "staff.json", '{"schema":2,"rows":[]}\n')

    def production(self, name: str) -> Path:
        """A production with one live unit, alpha, and its update timer."""
        checkout = self.root / name
        alpha = self.root / f"{name}-alpha"
        alpha.mkdir(parents=True)
        _ = subprocess.run(["git", "-C", str(alpha), "init", "--quiet", "-b", "alpha"],
                           capture_output=True, check=True)
        _ = subprocess.run(["git", "-C", str(alpha), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "--quiet",
                            "--allow-empty", "-m", "fixture"], capture_output=True, check=True)
        write_text_atomic(alpha / ".unit.toml", 'plan = "docs/alpha.md"\nresponsibilities = []\n')
        doc = checkout / f"docs/{name}-production.md"
        doc.parent.mkdir(parents=True)
        _ = doc.write_text("\n".join((
            f"# Production — {name}", "", "## Production Context", "",
            "- **Merge branch:** `production`", f"- **Showrunner checkout:** `{checkout}`",
            "- **Log:** `production.log`", "",
        )), encoding="utf-8")
        self.timer(name, doc)
        unit = unit_membership(name, "alpha")
        _ = staff.set_worktree(unit.id, staff.PresentWorktree(remote.local_machine(), alpha), expected=unit.worktree)
        retired = unit_membership(name, "beta")
        _ = staff.set_worktree(retired.id, staff.RemovedWorktree(remote.local_machine(), self.root / f"{name}-beta"),
                               expected=retired.worktree)
        return doc

    def timer(self, name: str, doc: Path) -> None:
        owner = recorded_showrunner(name, doc)
        instance = self.notifier / f"showrunner-{owner.id}"
        instance.mkdir()
        _ = (instance / "conf").write_text(
            f"TARGET=session:x\nCHECK=zsh /home/x/.claude/scripts/production/production_check.sh {owner.id}\n",
            encoding="utf-8")

    def planted(self, unit: str, production: str = "example") -> Path:
        path = self.vault / "natedev" / f"{unit}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(note(unit_membership(production, unit)), encoding="utf-8")
        return path

    def run_script(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(SCRIPT)], env=self.env,
                              capture_output=True, text=True, check=False)

    def test_a_retired_units_note_is_archived_and_a_live_ones_stays(self) -> None:
        _ = self.production("example")
        live = self.planted("alpha")
        retired = self.planted("beta")
        # Another production's note for the same unit name is that production's to prune.
        other = self.vault / "elsewhere" / "beta-unit.md"
        other.parent.mkdir(parents=True)
        _ = other.write_text(note(unit_membership("other", "beta")), encoding="utf-8")

        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(live.exists())
        self.assertFalse(retired.exists())
        self.assertTrue((self.root / "vault/archive/natedev/example/beta.md").exists())
        self.assertTrue(other.exists())

    def test_a_production_that_fails_is_reported_and_the_next_is_still_pruned(self) -> None:
        self.timer("aaa-broken", self.root / "missing/aaa-broken-production.md")
        _ = self.production("example")
        retired = self.planted("beta")

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertIn("phase notes not pruned for aaa-broken: cannot read production doc", result.stderr)
        self.assertNotIn("example", result.stderr)
        self.assertFalse(retired.exists())

    def test_a_timer_owner_with_no_document_is_reported(self) -> None:
        owner = staff_test_support.fixture_member("showrunner", name="odd")
        instance = self.notifier / f"showrunner-{owner.id}"
        instance.mkdir()
        _ = (instance / "conf").write_text("TARGET=session:x\n", encoding="utf-8")

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "phase notes not pruned for odd: timer owner has no local staff document\n")

    def test_nothing_to_prune_prints_nothing(self) -> None:
        _ = self.production("example")
        live = self.planted("alpha")
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
