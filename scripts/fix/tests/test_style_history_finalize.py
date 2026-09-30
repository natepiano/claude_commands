#!/usr/bin/env python3
"""Lock finalize outcomes: an open `finding` resolves, and one run writes one row.

record-unit stores `{"status": "finding"}` for every finding. Before this test,
finalize-fix and finalize-failure passed any existing outcome through, so each
finding stayed `finding` forever and the report counted fixed=0 for every rule.
A failed build gate followed by the retry path's finalize-fix also appended the
run twice (bevy_brp, 2026-09-10).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override


SCRIPT = Path(__file__).resolve().parents[1] / "style_history.py"
START_TIME = "2026-09-10T15:39:32Z"
GUIDELINES = ("rust/alpha.md", "rust/beta.md", "rust/gamma.md")
FIX_SUMMARY_STATUSES = (
    ("Applied", "0 remaining"),
    ("Partially applied", "2 remaining"),
    ("Skipped", "unchanged"),
)


class FinalizeOutcomeTest(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.nate_style: Path = root / "nate_style"
        self.project_root: Path = root / "rust" / "demo_style_fix"
        self.eval_path: Path = root / "evaluation.md"

    @override
    def setUp(self) -> None:
        (self.nate_style / ".history" / ".pending").mkdir(parents=True)
        self.project_root.mkdir(parents=True)
        reviewed = [
            {"guideline_id": guideline, "outcome": {"status": "finding", "finding_source": "new"}}
            for guideline in GUIDELINES
        ]
        reviewed.append({"guideline_id": "rust/delta.md", "outcome": {"status": "no_findings"}})
        pending = {"start_time": START_TIME, "phase": "evaluation", "reviewed_units": reviewed}
        _ = (self.nate_style / ".history" / ".pending" / "demo.json").write_text(json.dumps(pending))
        lines = ["# Style Evaluation", "", "## Improvements", ""]
        for number, guideline in enumerate(GUIDELINES, start=1):
            lines += [f"### {number}. Finding", f"**Style file**: `{self.nate_style / guideline}`", ""]
        lines += ["## Fix Summary", ""]
        for number, (status, search) in enumerate(FIX_SUMMARY_STATUSES, start=1):
            lines += [f"### Finding {number}: Finding", f"**Status:** {status}", f"**Post-fix search:** {search}", ""]
        _ = self.eval_path.write_text("\n".join(lines))

    @override
    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_helper(self, *args: str) -> None:
        env = dict(os.environ)
        env["STYLE_HISTORY_NATE_STYLE_DIR"] = str(self.nate_style)
        env["STYLE_HISTORY_RUST_DIR"] = str(self.project_root.parent)
        _ = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, check=True)

    def history_rows(self) -> list[dict[str, object]]:
        path = self.nate_style / ".history" / "demo.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def statuses(self, row: dict[str, object]) -> dict[str, str]:
        units = json.loads(json.dumps(row["reviewed_units"]))  # pyright: ignore[reportAny]
        return {unit["guideline_id"]: unit["outcome"]["status"] for unit in units}  # pyright: ignore[reportAny]

    def finalize_fix(self) -> None:
        self.run_helper("finalize-fix", "--project-root", str(self.project_root), "--evaluation", str(self.eval_path))

    def test_finalize_fix_resolves_open_findings(self) -> None:
        self.finalize_fix()
        rows = self.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            self.statuses(rows[0]),
            {
                "rust/alpha.md": "fixed",
                "rust/beta.md": "partial",
                "rust/gamma.md": "skipped",
                "rust/delta.md": "no_findings",
            },
        )

    def test_failure_marks_findings_fix_failed(self) -> None:
        self.run_helper("finalize-failure", "--project", "demo", "--reason", "cargo check failed")
        statuses = self.statuses(self.history_rows()[0])
        self.assertEqual([statuses[guideline] for guideline in GUIDELINES], ["fix_failed"] * 3)
        self.assertEqual(statuses["rust/delta.md"], "no_findings")

    def test_failure_then_fix_keeps_one_row_per_run(self) -> None:
        self.run_helper("finalize-failure", "--project", "demo", "--reason", "cargo check failed")
        self.finalize_fix()
        rows = self.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_time"], START_TIME)
        self.assertEqual(self.statuses(rows[0])["rust/alpha.md"], "fixed")


if __name__ == "__main__":
    _ = unittest.main()
