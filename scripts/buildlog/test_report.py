#!/usr/bin/env python3
"""Tests for report.py: a section per kind split by caller, then one summary row per kind."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import override

import index
import report
from test_index import STAMP, Record, call, ci_job, ci_run, encode, local_day, point_root_at, step


class ReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def render(self) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.report(connection, local_day(STAMP))

    def test_kinds_by_caller_then_one_summary_row_per_kind(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("m1", step="mend", caller="verify", duration_s=40.0, mend_s=3.0, mend_check_s=37.0, mend_fixes=0),
            step("m2", step="mend", caller="cargo-port", duration_s=10.0, mend_s=4.0, mend_check_s=6.0),
            step("m3", step="mend", caller="cargo-port", duration_s=20.0, status=101),
            step("c1", step="clippy", caller="verify", duration_s=30.0, errors=2, status=101),
            step("x1", step="clippy", caller="agent", cwd="/tmp/claude/scratch", duration_s=1.0),
            call("v1", outcome="reused", saved_s=60),
        )
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "test", "success", ("2026-10-02T12:00:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:09:00Z"))]),
        )
        text = self.render()
        lines = text.splitlines()

        self.assertLess(lines.index("### clippy"), lines.index("### mend"))
        self.assertLess(lines.index("### mend"), lines.index("### Summary: successes"))
        self.assertLess(lines.index("### Summary: successes"), lines.index("### Summary: failures"))
        self.assertLess(lines.index("### Summary: failures"), lines.index("### Summary: all"))
        self.assertIn("| verify.sh (agents) | 1 | 0 | 40.0 s | 40.0 s | 3.0 s | 37.0 s | 0 |", lines)
        self.assertIn("| cargo-port | 2 | 1 | 15.0 s | 10.0 s – 20.0 s | 4.0 s | 6.0 s |  |", lines)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| mend | 2 | 50.0 s | 25.0 s |  |", successes)
        self.assertIn("| **All steps** | 2 | 50.0 s | 25.0 s |  |", successes)
        self.assertFalse(any(line.startswith("| clippy") for line in successes))
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| clippy | 1 | 30.0 s | 30.0 s |  |", failures)
        self.assertIn("| mend | 1 | 20.0 s | 20.0 s |  |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| mend | 3 | 1 | 1.2 min | 23.3 s |  |", combined)
        self.assertIn("| **All steps** | 4 | 2 | 1.7 min | 25.0 s |  |", combined)
        self.assertIn("| reused | 1 |  | 1.0 min |", lines)
        self.assertIn("| CI | 1 | 0 | 10.0 min | 10.0 min |", lines)
        self.assertIn("1 steps under a temp folder (scratch and test builds) are left out.", text)
        self.assertNotIn("agent (direct)", text)

    def test_port_lint_calls_have_their_own_section(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("c1", step="clippy", caller="cargo-port", duration_s=30.0),
            call("v1", outcome="reused", saved_s=60),
            call("p1", tool="port-lint", outcome="reused", saved_s=90),
            call("p2", tool="port-lint", outcome="reused", saved_s=30),
            call("p3", tool="port-lint", outcome="deferred", status=75),
        )
        text = self.render()
        lines = text.splitlines()
        verify = lines[lines.index("### Agent calls (verify.sh)") : lines.index("### cargo-port calls (port-lint)")]
        self.assertIn("| reused | 1 |  | 1.0 min |", verify)
        port = lines[lines.index("### cargo-port calls (port-lint)") : lines.index("### CI") if "### CI" in lines else None]
        self.assertIn("| Outcome | Calls | Saved |", port)
        self.assertIn("| reused | 2 | 2.0 min |", port)
        self.assertIn("| deferred | 1 |  |", port)
        self.assertIn("Agent calls: 1 (1 reused), 1.0 min saved by pass records.", lines)
        self.assertIn("cargo-port calls: 3 (2 reused, 1 deferred), 2.0 min saved by recorded steps.", lines)

    def test_empty_day(self) -> None:
        text = self.render()
        self.assertIn("No build steps recorded.", text)
        self.assertIn("Agent calls: none.", text)
        self.assertIn("CI: no runs.", text)
        self.assertNotIn("cargo-port calls", text)


if __name__ == "__main__":
    _ = unittest.main()
