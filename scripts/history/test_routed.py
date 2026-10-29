#!/usr/bin/env python3
"""Routed findings stay visible until the receiving checkpoint closes their key."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("history.py")
BASE = 1_792_454_400


class RoutedHistoryTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    runs: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runs = self.root / "unit-history" / "runs"
        self.runs.mkdir(parents=True)

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_run(self, name: str, records: list[dict[str, object]]) -> Path:
        path = self.runs / f"{name}.jsonl"
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def route_records(
        self, *, instance: str = "origin-phase", finding: str = "F003", at: int = BASE,
    ) -> list[dict[str, object]]:
        return [
            {"event_type": "run_started", "run_id": "embedded-id-is-not-the-file-name",
             "project_plan_doc": "/repo/docs/plans/retry-plan.md", "timestamp_epoch": at - 20},
            {"event_type": "phase_started", "phase_instance_id": instance,
             "phase_id": "5", "phase_title": "Apply retry rules", "timestamp_epoch": at - 10},
            {"event_type": "finding_routed", "phase_instance_id": instance,
             "finding_id": finding, "reason": "spec-gap", "owner": "receiving-unit",
             "work_order": "follow-up.md — Phase 6 — Define ownership", "due": "Phase 6 checkpoint",
             "evidence": "Work Order omits ownership", "repair_round": 4, "round": 2,
             "fix_attempts": 2, "astra_approach": "Replace the owner", "timestamp_epoch": at},
        ]

    def report(self, *arguments: str, format_name: str = "json") -> str:
        environment = {**os.environ, "SKILL_HISTORY_STATE_ROOT": str(self.root), "USER_ZONE": "UTC"}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "routed", "--format", format_name, *arguments],
            capture_output=True, text=True, check=False, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def rows(self, *arguments: str) -> list[dict[str, object]]:
        report = cast(dict[str, object], json.loads(self.report(*arguments)))
        return cast(list[dict[str, object]], report["rows"])

    def test_open_route_lists_the_handoff_identity_age_and_closure(self) -> None:
        path = self.write_run("origin-run", self.route_records())
        before = path.read_bytes()
        row, = self.rows("--open")
        self.assertEqual({key: row[key] for key in (
            "run", "plan", "phase", "finding", "owner", "work_order", "due",
        )}, {"run": "origin-run", "plan": "retry-plan.md", "phase": "5", "finding": "F003",
             "owner": "receiving-unit", "work_order": "follow-up.md — Phase 6 — Define ownership",
             "due": "Phase 6 checkpoint"})
        self.assertIn("age", row)
        self.assertTrue(str(row["age"]))
        self.assertFalse(row["closure"])
        self.assertEqual(path.read_bytes(), before)

    def test_closure_in_a_receiving_run_closes_only_the_full_origin_key(self) -> None:
        _ = self.write_run("origin-run", self.route_records())
        # Same finding id, both a second origin and a restarted phase instance.
        _ = self.write_run("other-run", self.route_records())
        records = self.route_records(instance="restarted-phase", at=BASE + 100)
        with (self.runs / "origin-run.jsonl").open("a") as stream:
            for record in records[1:]:
                _ = stream.write(json.dumps(record) + "\n")
        _ = self.write_run("receiving-run", [
            {"event_type": "run_started", "run_id": "receiving-run", "timestamp_epoch": BASE + 200},
            {"event_type": "finding_route_closed", "phase_instance_id": "receiving-phase",
             "from": "origin-run:origin-phase:F003", "evidence": "abc123; tests pass; review accepted",
             "timestamp_epoch": BASE + 300},
        ])
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        closed = [row for row in rows if row["closure"]]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["run"], "origin-run")
        self.assertIn("abc123", json.dumps(closed[0]["closure"]))
        open_rows = self.rows("--open")
        self.assertEqual(len(open_rows), 2)
        self.assertEqual(sorted(str(row["run"]) for row in open_rows), ["origin-run", "other-run"])

    def test_since_filters_routes_but_still_joins_a_later_closure(self) -> None:
        _ = self.write_run("old-run", self.route_records(at=BASE - 86_400))
        _ = self.write_run("selected-run", self.route_records(at=BASE + 60))
        _ = self.write_run("receiver", [
            {"event_type": "finding_route_closed", "from": "selected-run:origin-phase:F003",
             "evidence": "def456; accepted", "timestamp_epoch": BASE + 86_400},
        ])
        rows = self.rows("--since", "2026-10-20T00:00:00+00:00")
        self.assertEqual([row["run"] for row in rows], ["selected-run"])
        self.assertTrue(rows[0]["closure"])
        self.assertEqual(self.rows("--since", "2026-10-20T00:00:00+00:00", "--open"), [])

    def test_text_output_shows_the_owner_work_order_and_due_checkpoint(self) -> None:
        _ = self.write_run("origin-run", self.route_records())
        output = self.report("--open", format_name="text")
        for text in ("origin-run", "F003", "receiving-unit", "follow-up.md", "Phase 6 checkpoint"):
            self.assertIn(text, output)


if __name__ == "__main__":
    _ = unittest.main()
