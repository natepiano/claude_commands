"""Read phase dependencies without treating prose numbers as phase IDs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.direct import plan_history


class PlanDependencyTests(unittest.TestCase):
    def read_dependencies(self, body: str) -> list[str] | None:
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.md"
            _ = plan.write_text(
                "# Plan\n\n### Phase 100 — Delivery · status: todo\n" + body + "\n",
                encoding="utf-8",
            )
            phases = plan_history.plan_phases(plan)
        self.assertEqual(len(phases), 1)
        return phases[0]["depends_on"]

    def test_depends_on_examples_keep_only_phase_references(self) -> None:
        cases: list[tuple[str, list[str]]] = [
            ("Phase 13", ["13"]),
            ("Phase 94b (order only)", ["94b"]),
            ("Phase 7.", ["7"]),
            ("Phase 7: reason", ["7"]),
            ("Phase 7:", ["7"]),
            ("Phase 7 - note", ["7"]),
            ("Phases 3, 7.", ["3", "7"]),
            ("Phase 7; Phase 9.", ["7", "9"]),
            ("none", []),
            ("Phases 10, 11 and 12", ["10", "11", "12"]),
            ("Phases 15 and 16 (the assembly, ...)", ["15", "16"]),
            ("Phase 9, Phase 10, Phase 12", ["9", "10", "12"]),
            ("Phase 64, Phase 62 (sequenced after it; no file overlap)", ["64", "62"]),
            ("Phase 8, and organon's b926ca727 (or its successor) landed on init/catalyst", ["8"]),
            ("Phase 86; shutdown-unit's hold API (eb30ce4a)", ["86"]),
            (
                "Phases 5 and 7 (showrunner returns of C443, C446 and C447, "
                + "2026-10-29: runs next, ...)",
                ["5", "7"],
            ),
            (
                "the checkpoints of Phases 3, 4 and 5, and the three review records: "
                + "the Phase 3 and Phase 4 ...",
                ["3", "4", "5"],
            ),
            ("Phase 3 (resources-unit waits until 2026-10-29 00:00)", ["3"]),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(self.read_dependencies("**Depends on:** " + line), expected)

    def test_phase_lists_stop_at_prose_and_keep_first_seen_order(self) -> None:
        cases: list[tuple[str, list[str]]] = [
            ("Phases 4, 2, and 9b & 6", ["4", "2", "9b", "6"]),
            ("Phases 2 & 3", ["2", "3"]),
            ("Phase 9, Phases 4 and 9, Phase 4, Phase 2a", ["9", "4", "2a"]),
            ("Phases 3, review 4 and 5; Phase 8", ["3", "8"]),
            ("Phases 3 and 2026-10-29, 00:00 and 8; Phase 6", ["3", "6"]),
            ("release 13 after 2026-10-29 00:00; checkpoint C443", []),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(self.read_dependencies("**Depends on:** " + line), expected)

    def test_date_time_and_word_tokens_cannot_supply_a_phase_id(self) -> None:
        for token in ("2026-10-29", "00:00", "1.2", "3/4", "12abc", "9B", "7_name"):
            for reference in ("Phase " + token, "Phases " + token + ", 8"):
                with self.subTest(reference=reference):
                    self.assertEqual(self.read_dependencies("**Depends on:** " + reference), [])

    def test_a_missing_dependency_line_stays_distinct_from_no_dependencies(self) -> None:
        self.assertIsNone(self.read_dependencies("Run after Phase 3; no dependency line is set."))


if __name__ == "__main__":
    _ = unittest.main()
