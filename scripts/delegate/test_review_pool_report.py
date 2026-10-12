#!/usr/bin/env python3
"""Report contracts exercised with isolated history and shared usage stores."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast, override

from scripts.usage import collect
from scripts.usage.agents_log import RoleLog


SCRIPT = Path(__file__).with_name("review_pool_report.py")
SINCE = "2026-10-12"
EPOCH = datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()


class ReviewTurnJson(TypedDict):
    arm: Literal["pool", "own"]
    pool: str
    reviewer: str
    thread_id: str
    unit: str
    phase: str
    phase_instance_id: str
    history_file: str
    kind: Literal["broad", "closure", "ux"]
    lens: str
    pass_number: int
    started_at: int
    finished_at: float
    lease_ordinal: int
    prime_hash: str
    status: Literal["reviewed", "void", "canceled", "error"]
    raised: int


def timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(EPOCH + seconds, timezone.utc).isoformat()


def usage(seconds: float, uncached: int, cached: int = 0, output: int = 0) -> dict[str, object]:
    return {
        "timestamp": timestamp(seconds), "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": uncached + cached,
                                 "cached_input_tokens": cached, "output_tokens": output},
            "total_token_usage": {"input_tokens": 999999, "cached_input_tokens": 0,
                                  "output_tokens": 999999},
        }},
    }


class ReviewPoolReportTests(unittest.TestCase):
    # unittest initializes these fixtures through setUp before each test.
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    turns: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    rollouts: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    sequence: int  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.history = self.home / "history"
        self.turns = self.history / "review_turns" / "fixture-run"
        self.turns.mkdir(parents=True)
        self.history_file = self.history / "runs" / "fixture-run.jsonl"
        self.history_file.parent.mkdir()
        self.rollouts = self.home / "rollout-fixtures"
        self.rollouts.mkdir(parents=True)
        self.sequence = 0

    def record(self, name: str, *, arm: Literal["pool", "own"] = "pool",
               thread: str = "thread-pool", kind: Literal["broad", "closure", "ux"] = "broad",
               start: int = 10, finish: float = 20.5, ordinal: int = 1,
               status: Literal["reviewed", "void", "canceled", "error"] = "reviewed",
               phase_instance: str = "phase-instance", unit: str = "unit", phase: str = "4",
               raised: int = 0) -> Path:
        record: ReviewTurnJson = {
            "arm": arm, "pool": "production" if arm == "pool" else "",
            "reviewer": "code1", "thread_id": thread, "unit": unit, "phase": phase,
            "phase_instance_id": phase_instance, "history_file": str(self.history_file),
            "kind": kind, "lens": "ux" if kind == "ux" else "contract" if kind == "broad" else "",
            "pass_number": 1 if kind != "closure" else 2,
            "started_at": int(EPOCH) + start, "finished_at": EPOCH + finish,
            "lease_ordinal": ordinal if arm == "pool" else 0,
            "prime_hash": "standing-contract" if arm == "pool" else "",
            "status": status, "raised": raised,
        }
        encoded: dict[str, object] = dict(record)
        encoded["pass"] = encoded.pop("pass_number")
        path = self.turns / f"{name}.json"
        _ = path.write_text(json.dumps(encoded), encoding="utf-8")
        return path

    def rollout(self, thread: str, *events: dict[str, object], filename: str = "",
                model: str = "gpt-6.1-sol") -> Path:
        self.sequence += 1
        path = self.rollouts / (filename or f"rollout-{self.sequence}.jsonl")
        header: dict[str, object] = {
            "timestamp": timestamp(0), "type": "session_meta",
            "payload": {"id": thread, "session_id": "different-session-id",
                        "timestamp": timestamp(0), "cwd": str(self.home)},
        }
        context: dict[str, object] = {
            "timestamp": timestamp(0), "type": "turn_context", "payload": {"model": model},
        }
        _ = path.write_text("\n".join(json.dumps(event) for event in (header, context, *events)) + "\n",
                            encoding="utf-8")
        return path

    def finding(self, *, phase_instance: str = "phase-instance", caught_by: str = "delegate",
                lens: str = "contract", event_type: str = "finding_opened") -> dict[str, object]:
        self.sequence += 1
        return {"event_type": event_type, "event_id": f"event-{self.sequence}",
                "finding_id": f"finding-{self.sequence}", "phase_instance_id": phase_instance,
                "phase_id": "4", "timestamp": timestamp(15), "timestamp_epoch": EPOCH + 15,
                "caught_by": caught_by, "lens": lens, "round": 1}

    def events(self, *events: dict[str, object]) -> None:
        _ = self.history_file.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                                         encoding="utf-8")

    def holds(self, unit: str, phase: str, count: int) -> None:
        ledger = self.home / ".claude" / "data" / "review_regime.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row = {"unit": unit, "phase": phase, "regime": "after", "started": timestamp(0),
               "merged": timestamp(30), "holds": count, "merge_defects": 0}
        with ledger.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(row) + "\n")

    def populate_usage_store(self, directory: Path) -> None:
        """Collect fixture logs into the store the report reads without log access."""
        directory.mkdir(exist_ok=True)
        path = directory / "usage.db"
        path.unlink(missing_ok=True)
        resolver = collect.RoleResolver(RoleLog({}, ()), set())
        with closing(sqlite3.connect(path)) as db, db:
            _ = db.executescript(collect.SCHEMA)
            collect.migrate_store(db)
            for source in sorted(self.rollouts.glob("*.jsonl")):
                if source.stat().st_mtime < EPOCH:
                    continue
                for measurement in collect.read_file("natedev", source, "codex", 0):
                    if isinstance(measurement, collect.FileCursor):
                        continue
                    if isinstance(measurement, collect.Turn):
                        # A reset's files can place distinct requests at the same byte offset.
                        measurement = replace(measurement, key=f"{source}:{measurement.key}")
                    _ = collect.save_measurement(db, measurement, resolver)

    def report_result(self, *options: str, explicit_root: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history)
        usage_state = self.home / "usage-state"
        self.populate_usage_store(usage_state)
        environment["USAGE_STATE_DIR"] = str(usage_state)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        _ = environment.pop("CODEX_HOME", None)
        command = [sys.executable, str(SCRIPT), "--since", SINCE, "--no-mac"]
        if explicit_root:
            command.extend(("--root", str(self.history)))
        result = subprocess.run([*command, *options], env=environment, capture_output=True,
                                text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def report(self, *options: str, explicit_root: bool = True) -> str:
        return self.report_result(*options, explicit_root=explicit_root).stdout

    def table_row(self, text: str, section: str, *key: str) -> list[str]:
        """Read a public table row, allowing spacing used for alignment."""
        active = False
        for line in text.splitlines():
            if " ".join(line.replace("|", " ").split()).startswith(section):
                active = True
                continue
            if not active:
                continue
            cells = line.strip().strip("|").split("|") if "|" in line else line.split()
            cells = [cell.strip() for cell in cells]
            if cells[:len(key)] == list(key):
                return cells[len(key):]
        self.fail(f"missing {section} row {key!r}:\n{text}")

    def cost_row(self, text: str, arm: str, kind: str, count: int,
                 median: float, mean: float, cached_share: float = 0.0) -> None:
        cells = self.table_row(text, "Arm Kind Reviews", arm, kind)
        self.assertEqual(int(cells[0]), count, text)
        self.assertAlmostEqual(float(cells[1]), median, delta=0.1, msg=text)
        self.assertAlmostEqual(float(cells[2]), mean, delta=0.1, msg=text)
        self.assertAlmostEqual(float(cells[3].rstrip("%")), cached_share, delta=0.1, msg=text)

    def compaction_counts(self, text: str, *, pool: int = 0, own: int = 0) -> None:
        lines = text.splitlines()
        incomplete = next(index for index, line in enumerate(lines)
                          if line.startswith("cost incomplete:"))
        expected = f"began with a compaction: pool {pool}; own {own}"
        self.assertEqual(lines[incomplete + 1], expected, text)
        self.assertEqual(sum(line.startswith("began with a compaction:") for line in lines), 1, text)

    def test_known_request_costs_group_by_arm_kind_and_lease_ordinal(self) -> None:
        cases: list[tuple[str, Literal["pool", "own"], Literal["broad", "closure", "ux"], int, int, int, int]] = [
            ("pool-broad-a", "pool", "broad", 1, 60, 100, 5),
            ("pool-broad-b", "pool", "broad", 2, 200, 200, 10),
            ("pool-closure", "pool", "closure", 3, 400, 0, 0),
            ("pool-ux", "pool", "ux", 5, 600, 1000, 0),
            ("own-broad", "own", "broad", 0, 1000, 0, 0),
            ("own-closure", "own", "closure", 0, 200, 0, 0),
            ("own-ux", "own", "ux", 0, 300, 0, 0),
        ]
        for name, arm, kind, ordinal, uncached, cached, output in cases:
            _ = self.record(name, arm=arm, kind=kind, thread=name, ordinal=ordinal)
            _ = self.rollout(name, usage(15, uncached, cached, output))
        text = self.report()
        self.cost_row(text, "pool", "broad", 2, 175, 175, 100 * 15 / 350)
        self.cost_row(text, "pool", "closure", 1, 400, 400)
        self.cost_row(text, "pool", "ux", 1, 650, 650, 100 * 50 / 650)
        self.cost_row(text, "own", "broad", 1, 1000, 1000)
        self.cost_row(text, "own", "closure", 1, 200, 200)
        self.cost_row(text, "own", "ux", 1, 300, 300)
        for ordinal, count, median, mean in (("1", 1, 90, 90), ("2", 1, 260, 260),
                                           ("3+", 2, 525, 525)):
            cells = self.table_row(text, "Lease ordinal", ordinal)
            self.assertEqual(int(cells[0]), count, text)
            self.assertAlmostEqual(float(cells[1]), median, delta=0.1)
            self.assertAlmostEqual(float(cells[2]), mean, delta=0.1)

    def test_request_window_includes_both_bounds_and_excludes_outside_usage(self) -> None:
        _ = self.record("bounds")
        _ = self.rollout("thread-pool", usage(9.999, 100000), usage(10, 100),
                         usage(20.5, 200), usage(20.501, 100000))
        self.cost_row(self.report(), "pool", "broad", 1, 300, 300)

    def test_median_and_mean_use_individual_review_costs(self) -> None:
        for index, cost in enumerate((100, 200, 900)):
            thread = f"thread-{index}"
            _ = self.record(thread, thread=thread)
            _ = self.rollout(thread, usage(15, cost))
        self.cost_row(self.report(), "pool", "broad", 3, 200, 400)

    def test_reset_rollout_files_with_same_thread_id_are_all_counted(self) -> None:
        _ = self.record("reset-files")
        _ = self.rollout("thread-pool", usage(12, 100), filename="opaque-first.jsonl")
        _ = self.rollout("thread-pool", usage(18, 200), filename="opaque-second.jsonl")
        self.cost_row(self.report(), "pool", "broad", 1, 300, 300)

    def test_unrelated_thread_is_excluded_even_with_identical_request_times(self) -> None:
        _ = self.record("one-thread")
        _ = self.rollout("thread-pool", usage(15, 100))
        _ = self.rollout("unrelated-thread", usage(15, 1000000),
                         filename="rollout-thread-pool.jsonl")
        self.cost_row(self.report(), "pool", "broad", 1, 100, 100)

    def test_reviewer_name_reuse_does_not_merge_different_thread_costs(self) -> None:
        _ = self.record("first-lifetime", thread="old-thread")
        _ = self.record("second-lifetime", thread="new-thread", kind="closure")
        _ = self.rollout("old-thread", usage(15, 100))
        _ = self.rollout("new-thread", usage(15, 200))
        text = self.report()
        self.cost_row(text, "pool", "broad", 1, 100, 100)
        self.cost_row(text, "pool", "closure", 1, 200, 200)

    def test_overlapping_whole_second_windows_assign_request_to_earlier_review_once(self) -> None:
        # Filenames deliberately sort in the opposite order to review starts.
        _ = self.record("z-earlier", start=10, finish=20.8)
        _ = self.record("a-later", start=20, finish=30, kind="closure", ordinal=2)
        _ = self.rollout("thread-pool", usage(20.5, 100), usage(21, 200))
        text = self.report()
        self.cost_row(text, "pool", "broad", 1, 100, 100)
        self.cost_row(text, "pool", "closure", 1, 200, 200)

    def test_incomplete_costs_are_named_and_excluded_from_means_and_medians(self) -> None:
        missing = self.record("missing-rollout", thread="missing-thread")
        compacted = self.record("compacted-turn", thread="compacted-thread")
        _ = self.record("complete", thread="complete-thread")
        _ = self.rollout("compacted-thread", usage(12, 900000, 900000, 20),
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}})
        _ = self.rollout("complete-thread", usage(15, 100))
        text = self.report()
        self.cost_row(text, "pool", "broad", 3, 100, 100)
        self.assertIn("cost incomplete: 2", text)
        self.assertIn(missing.name, text)
        self.assertIn(compacted.name, text)
        self.assertIn(f"{compacted} (a compaction after its first request)", text)
        self.compaction_counts(text)
        self.assertIn("pool reviews: 3 of 20 for the first reading", text)

    def test_compaction_outside_window_does_not_make_review_cost_incomplete(self) -> None:
        _ = self.record("outside-compaction")
        _ = self.rollout("thread-pool", usage(15, 100),
                         {"timestamp": timestamp(21), "type": "compacted", "payload": {}})
        text = self.report()
        self.cost_row(text, "pool", "broad", 1, 100, 100)
        self.assertIn("cost incomplete: 0", text)
        self.compaction_counts(text)

    def test_noncompleted_turns_own_overlap_tokens_without_entering_completed_statistics(self) -> None:
        for index, status in enumerate(("void", "canceled", "error")):
            _ = self.record(f"{status}-earlier", thread=status, start=10, finish=20.8, status=status,
                            raised=100, ordinal=10)
            _ = self.record(f"{status}-later", thread=status, start=20, finish=30, ordinal=index + 1,
                            raised=1)
            _ = self.rollout(status, usage(20.5, 100000), usage(21, 100))
        text = self.report("--quality")
        self.cost_row(text, "pool", "broad", 3, 100, 100)
        self.assertIn("pool reviews: 3 of 20 for the first reading", text)
        self.assertIn("pool outcomes: void 1; canceled 1; error 1", text)
        cells = self.table_row(text, "Arm Kind Phases", "pool", "code")
        self.assertEqual(int(cells[1]), 3, text)
        for ordinal in ("1", "2", "3+"):
            cells = self.table_row(text, "Lease ordinal", ordinal)
            self.assertEqual(int(cells[0]), 1, text)
            self.assertEqual(float(cells[1]), 100, text)
            self.assertEqual(float(cells[2]), 100, text)

    def test_history_subdirectories_are_read_without_combining_distinct_phase_instances(self) -> None:
        first = self.record("first-run", raised=2)
        second = self.record("second-run", thread="thread-two", phase_instance="phase-two", raised=4)
        another_run = self.history / "review_turns" / "another-run"
        another_run.mkdir()
        _ = second.rename(another_run / second.name)
        self.assertTrue(first.is_file())
        _ = self.rollout("thread-pool", usage(15, 100))
        _ = self.rollout("thread-two", usage(15, 200))
        self.events(self.finding(), self.finding(phase_instance="phase-two", caught_by="both"))
        text = self.report("--quality")
        self.cost_row(text, "pool", "broad", 2, 150, 150)
        self.quality_row(text, "pool", "code", 2, 6, 2, 100 / 3, 0)

    def test_all_four_statuses_are_counted_separately_for_each_arm(self) -> None:
        for arm in ("pool", "own"):
            for status in ("reviewed", "void", "canceled", "error"):
                thread = f"{arm}-{status}"
                _ = self.record(thread, arm=arm, thread=thread, status=status,
                                phase_instance=f"phase-{arm}", raised=2 if status == "reviewed" else 100)
                _ = self.rollout(thread, usage(15, 100 if status == "reviewed" else 100000))
        text = self.report("--quality")
        for arm in ("pool", "own"):
            self.cost_row(text, arm, "broad", 1, 100, 100)
            self.assertIn(f"{arm} outcomes: void 1; canceled 1; error 1", text)
            cells = self.table_row(text, "Arm Kind Phases", arm, "code")
            self.assertEqual(int(cells[1]), 2, text)
        self.assertIn("pool reviews: 1 of 20 for the first reading", text)

    def test_since_filters_turns_and_old_rollout_files(self) -> None:
        _ = self.record("before-date", start=-20, finish=-10, thread="old-thread")
        _ = self.rollout("old-thread", usage(-15, 100000))
        _ = self.record("since-date", thread="thread-pool")
        _ = self.rollout("thread-pool", usage(15, 100))
        obsolete = self.rollout("thread-pool", usage(15, 100000))
        os.utime(obsolete, (EPOCH - 1, EPOCH - 1))
        self.cost_row(self.report(), "pool", "broad", 1, 100, 100)

    def test_history_root_environment_is_used_when_root_flag_is_omitted(self) -> None:
        _ = self.record("environment-root")
        _ = self.rollout("thread-pool", usage(15, 100))
        self.cost_row(self.report(explicit_root=False), "pool", "broad", 1, 100, 100)

    def quality_row(self, text: str, arm: str, kind: str, phases: int, raised: int,
                    confirmed: int, precision: float, escapes: float) -> None:
        cells = self.table_row(text, "Arm Kind Phases", arm, kind)
        self.assertEqual([int(cell) for cell in cells[:3]], [phases, raised, confirmed], text)
        self.assertAlmostEqual(float(cells[3].rstrip("%")), precision, delta=0.1, msg=text)
        self.assertAlmostEqual(float(cells[4]), escapes, delta=0.1, msg=text)

    def test_quality_joins_phase_instances_once_and_splits_code_and_ux_findings(self) -> None:
        cases: list[tuple[str, Literal["pool", "own"], Literal["broad", "closure", "ux"], str, int]] = [
            ("broad", "pool", "broad", "pool-code", 4),
            ("closure", "pool", "closure", "pool-code", 2),
            ("ux", "pool", "ux", "pool-ux", 3),
            ("own", "own", "broad", "own-code", 4),
        ]
        for name, arm, kind, phase_instance, raised in cases:
            _ = self.record(name, arm=arm, kind=kind, thread=name,
                            phase_instance=phase_instance, raised=raised)
            _ = self.rollout(name, usage(15, 100))
        self.events(
            self.finding(phase_instance="pool-code"),
            self.finding(phase_instance="pool-code"),
            self.finding(phase_instance="pool-code", caught_by="both"),
            self.finding(phase_instance="pool-code", caught_by="main"),
            self.finding(phase_instance="pool-code", caught_by="main"),
            self.finding(phase_instance="pool-ux", lens="ux"),
            self.finding(phase_instance="pool-ux", lens="ux", caught_by="both"),
            self.finding(phase_instance="pool-ux", lens="ux", caught_by="main"),
            self.finding(phase_instance="own-code"),
            self.finding(phase_instance="own-code", caught_by="main"),
            self.finding(phase_instance="unrelated-phase"),
            self.finding(phase_instance="pool-code", event_type="finding_verdict"),
        )
        text = self.report("--quality")
        self.quality_row(text, "pool", "code", 1, 6, 3, 50, 2)
        self.quality_row(text, "pool", "ux", 1, 3, 2, 100 * 2 / 3, 1)
        self.quality_row(text, "own", "code", 1, 4, 1, 25, 1)

    def test_quality_adds_merge_holds_to_main_findings_per_phase(self) -> None:
        _ = self.record("held-phase", raised=4, unit="held-unit", phase="4")
        _ = self.record("unheld-phase", thread="thread-two", phase_instance="phase-two",
                        raised=2, unit="unheld-unit", phase="5")
        _ = self.rollout("thread-pool", usage(15, 100))
        _ = self.rollout("thread-two", usage(15, 100))
        self.events(self.finding(), self.finding(caught_by="main"),
                    self.finding(phase_instance="phase-two", caught_by="both"))
        self.holds("held-unit", "4", 3)
        self.holds("another-unit", "4", 100)
        self.holds("held-unit", "different-phase", 100)
        self.quality_row(self.report("--quality"), "pool", "code", 2, 6, 2, 100 / 3, 2)

    def test_mixed_phase_is_named_and_excluded_from_quality_comparison(self) -> None:
        _ = self.record("pool-mixed", phase_instance="mixed", raised=100)
        _ = self.record("own-mixed", arm="own", thread="mixed-own", phase_instance="mixed", raised=100)
        _ = self.record("pool-only", thread="pool-only", phase_instance="pool-only", raised=2)
        _ = self.record("own-only", arm="own", thread="own-only", phase_instance="own-only", raised=4)
        for thread in ("thread-pool", "mixed-own", "pool-only", "own-only"):
            _ = self.rollout(thread, usage(15, 100))
        self.events(self.finding(phase_instance="mixed"),
                    self.finding(phase_instance="pool-only"),
                    self.finding(phase_instance="own-only"))
        text = self.report("--quality")
        self.assertIn("mixed phases: 1 (excluded from comparison)", text)
        self.quality_row(text, "pool", "code", 1, 2, 1, 50, 0)
        self.quality_row(text, "own", "code", 1, 4, 1, 25, 0)

    def test_failed_pool_attempt_makes_own_retry_phase_mixed(self) -> None:
        _ = self.record("failed-pool", phase_instance="mixed", thread="", status="error", raised=100)
        _ = self.record("own-retry", arm="own", thread="own-retry", phase_instance="mixed", raised=100)
        _ = self.record("pool-only", thread="pool-only", phase_instance="pool-only", raised=2)
        _ = self.record("own-only", arm="own", thread="own-only", phase_instance="own-only", raised=4)
        for thread in ("own-retry", "pool-only", "own-only"):
            _ = self.rollout(thread, usage(15, 100))
        self.events(self.finding(phase_instance="mixed"),
                    self.finding(phase_instance="pool-only"),
                    self.finding(phase_instance="own-only"))
        text = self.report("--quality")
        self.assertIn("mixed phases: 1 (excluded from comparison)", text)
        self.assertIn("pool outcomes: void 0; canceled 0; error 1", text)
        self.assertIn("cost incomplete: 0", text)
        self.quality_row(text, "pool", "code", 1, 2, 1, 50, 0)
        self.quality_row(text, "own", "code", 1, 4, 1, 25, 0)

    def test_first_reading_line_counts_completed_pool_reviews_below_twenty(self) -> None:
        for index in range(19):
            thread = f"pool-{index}"
            _ = self.record(thread, thread=thread, phase_instance=thread)
            _ = self.rollout(thread, usage(15, 100))
        _ = self.record("twentieth-void", thread="void-thread", status="void")
        _ = self.rollout("void-thread", usage(15, 100000))
        text = self.report()
        self.assertIn("pool reviews: 19 of 20 for the first reading", text)
        self.assertNotIn("pool is at", text)

    def test_twenty_completed_reviews_switch_to_weighted_per_review_comparison(self) -> None:
        for index in range(20):
            thread = f"pool-{index}"
            _ = self.record(thread, thread=thread, phase_instance=thread)
            _ = self.rollout(thread, usage(15, 100))
        _ = self.record("own", arm="own", thread="own-thread", phase_instance="own")
        _ = self.rollout("own-thread", usage(15, 200))
        text = self.report()
        self.assertIn("pool is at 50.0% of own-seat tokens per review", text)
        self.assertNotIn("for the first reading", text)

    def test_above_twenty_compares_per_review_means_instead_of_totals(self) -> None:
        for index in range(21):
            thread = f"pool-{index}"
            _ = self.record(thread, thread=thread, phase_instance=thread)
            _ = self.rollout(thread, usage(15, 100))
        for index in range(2):
            thread = f"own-{index}"
            _ = self.record(thread, arm="own", thread=thread, phase_instance=thread)
            _ = self.rollout(thread, usage(15, 400))
        text = self.report()
        self.assertIn("pool is at 25.0% of own-seat tokens per review", text)
        self.assertNotIn("for the first reading", text)

    def test_surviving_earlier_rollout_without_a_request_in_review_window_is_incomplete(self) -> None:
        missing_request = self.record("after-reset", thread="reset-thread")
        _ = self.rollout("reset-thread", usage(5, 1000))
        _ = self.record("complete", thread="complete-thread")
        _ = self.rollout("complete-thread", usage(15, 100))
        text = self.report()
        self.cost_row(text, "pool", "broad", 2, 100, 100)
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{missing_request} (no request in its window)", text)
        self.assertIn("pool reviews: 2 of 20 for the first reading", text)

    def test_request_with_unweighted_model_makes_review_cost_incomplete_and_named(self) -> None:
        incomplete = self.record("unknown-model", thread="unknown-thread")
        _ = self.rollout("unknown-thread", usage(15, 100000, 100000, 100),
                         model="fixture-unweighted-model")
        _ = self.record("weighted-review", thread="weighted-thread")
        _ = self.rollout("weighted-thread", usage(15, 60, 100, 5))
        text = self.report()
        self.cost_row(text, "pool", "broad", 2, 90, 90, 100 * 5 / 90)
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{incomplete} (unweighted model fixture-unweighted-model)", text)
        self.assertIn("pool reviews: 2 of 20 for the first reading", text)
        self.compaction_counts(text)

    def test_incomplete_reason_prefers_missing_rollout_then_no_request_then_later_compaction(self) -> None:
        missing = self.record("missing", thread="missing-thread")
        compacted = self.record("compacted-after-request", thread="compacted-thread")
        _ = self.rollout("compacted-thread", usage(12, 100),
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}})
        no_request = self.record("compacted-without-request", thread="empty-thread")
        _ = self.rollout("empty-thread",
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}},
                         usage(15.003, 0))
        text = self.report()
        self.assertIn("cost incomplete: 3", text)
        self.assertIn(f"{missing} (missing rollout)", text)
        self.assertIn(f"{compacted} (a compaction after its first request)", text)
        self.assertIn(f"{no_request} (no request in its window)", text)
        self.assertEqual(self.table_row(text, "Arm Kind Reviews", "pool", "broad"),
                         ["3", "-", "-", "-"], text)
        self.compaction_counts(text)

    def test_compaction_and_its_zero_counter_sample_without_requests_are_incomplete(self) -> None:
        record = self.record("compaction-only")
        _ = self.rollout("thread-pool",
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}},
                         usage(15.003, 0))
        text = self.report()
        self.assertEqual(self.table_row(text, "Arm Kind Reviews", "pool", "broad"),
                         ["1", "-", "-", "-"], text)
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{record} (no request in its window)", text)
        self.compaction_counts(text)

    def test_own_review_starting_with_compaction_sums_requests_and_names_startup_count(self) -> None:
        _ = self.record("own-startup-compaction", arm="own")
        _ = self.rollout("thread-pool",
                         {"timestamp": timestamp(12), "type": "compacted", "payload": {}},
                         usage(12.003, 0), usage(15, 60, 100, 5), usage(18, 200, 200, 10))
        text = self.report()
        self.cost_row(text, "own", "broad", 1, 350, 350, 100 * 15 / 350)
        self.assertIn("cost incomplete: 0", text)
        self.compaction_counts(text, own=1)

    def test_pool_startup_compaction_and_requests_in_separate_files_are_complete(self) -> None:
        _ = self.record("pool-startup-compaction")
        # File order deliberately puts the requests before the compaction file.
        _ = self.rollout("thread-pool", usage(15, 100), usage(18, 200),
                         filename="a-requests.jsonl")
        _ = self.rollout("thread-pool",
                         {"timestamp": timestamp(12), "type": "compacted", "payload": {}},
                         usage(12.003, 0), filename="z-compaction.jsonl")
        text = self.report()
        self.cost_row(text, "pool", "broad", 1, 300, 300)
        self.assertIn("cost incomplete: 0", text)
        self.compaction_counts(text, pool=1)

    def test_compaction_between_owned_requests_is_incomplete_and_not_a_startup_count(self) -> None:
        record = self.record("mid-review-compaction")
        _ = self.rollout("thread-pool", usage(12, 100),
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}},
                         usage(15.003, 0), usage(18, 200))
        text = self.report()
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{record} (a compaction after its first request)", text)
        self.assertEqual(self.table_row(text, "Arm Kind Reviews", "pool", "broad"),
                         ["1", "-", "-", "-"], text)
        self.compaction_counts(text)

    def test_compaction_in_second_thread_file_after_owned_request_is_incomplete(self) -> None:
        record = self.record("mid-review-compaction-in-second-file", arm="own")
        _ = self.rollout("thread-pool", usage(12, 100), filename="a-first-request.jsonl")
        _ = self.rollout("thread-pool",
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}},
                         usage(15.003, 0), usage(18, 200), filename="z-second-file.jsonl")
        text = self.report()
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{record} (a compaction after its first request)", text)
        self.assertEqual(self.table_row(text, "Arm Kind Reviews", "own", "broad"),
                         ["1", "-", "-", "-"], text)
        self.compaction_counts(text)

    def test_noncompleted_reviews_beginning_with_compaction_are_not_in_startup_counts(self) -> None:
        for arm in ("pool", "own"):
            for status in ("void", "canceled", "error"):
                thread = f"{arm}-{status}"
                _ = self.record(thread, arm=arm, thread=thread, status=status)
                _ = self.rollout(thread,
                                 {"timestamp": timestamp(12), "type": "compacted", "payload": {}},
                                 usage(12.003, 0), usage(15, 100))
        text = self.report()
        self.assertIn("cost incomplete: 0", text)
        self.compaction_counts(text)

    def test_ux_reviews_do_not_reach_the_code_trial_threshold(self) -> None:
        for index in range(20):
            thread = f"ux-{index}"
            _ = self.record(thread, thread=thread, kind="ux")
            _ = self.rollout(thread, usage(15, 1000))
        _ = self.record("code-review", thread="code-thread")
        _ = self.rollout("code-thread", usage(15, 100))
        text = self.report()
        self.cost_row(text, "pool", "ux", 20, 1000, 1000)
        self.assertIn("pool reviews: 1 of 20 for the first reading", text)
        self.assertNotIn("pool is at", text)

    def test_code_comparison_excludes_ux_costs_on_both_arms(self) -> None:
        for index in range(20):
            thread = f"code-{index}"
            _ = self.record(thread, thread=thread, kind="broad" if index % 2 else "closure")
            _ = self.rollout(thread, usage(15, 100))
        _ = self.record("own-code", arm="own", thread="own-code")
        _ = self.rollout("own-code", usage(15, 200))
        for arm in ("pool", "own"):
            thread = f"{arm}-costly-ux"
            _ = self.record(thread, arm=arm, thread=thread, kind="ux")
            _ = self.rollout(thread, usage(15, 100000))
        text = self.report()
        self.assertIn("pool is at 50.0% of own-seat tokens per review", text)
        for arm in ("pool", "own"):
            self.cost_row(text, arm, "ux", 1, 100000, 100000)

    def test_noninteger_ordinals_and_raised_counts_are_skipped_and_named(self) -> None:
        _ = self.record("valid", thread="valid-thread", raised=2)
        _ = self.rollout("valid-thread", usage(15, 100))
        for field in ("lease_ordinal", "raised"):
            for value in (1.5, True):
                with self.subTest(field=field, value=value):
                    invalid = self.record("invalid", thread="invalid-thread", raised=100)
                    data = cast(dict[str, object], cast(object, json.loads(invalid.read_text())))
                    data[field] = value
                    _ = invalid.write_text(json.dumps(data), encoding="utf-8")
                    _ = self.rollout("invalid-thread", usage(15, 100000))
                    result = self.report_result("--quality")
                    self.assertIn(str(invalid), result.stderr)
                    self.assertIn("skipped", result.stderr)
                    self.assertIn(field, result.stderr)
                    self.cost_row(result.stdout, "pool", "broad", 1, 100, 100)
                    self.assertEqual(self.table_row(result.stdout, "Lease ordinal", "1")[0], "1")
                    self.assertEqual(self.table_row(result.stdout, "Arm Kind Phases", "pool", "code")[1], "2")
                    self.assertIn("pool reviews: 1 of 20 for the first reading", result.stdout)

    def test_twenty_code_reviews_without_complete_costs_on_either_arm_name_no_comparison(self) -> None:
        for incomplete_arm in ("pool", "own"):
            with self.subTest(incomplete_arm=incomplete_arm):
                for path in self.rollouts.glob("*.jsonl"):
                    path.unlink()
                for index in range(20):
                    thread = f"pool-{index}"
                    _ = self.record(thread, thread=thread)
                    if incomplete_arm != "pool":
                        _ = self.rollout(thread, usage(15, 100))
                _ = self.record("own", arm="own", thread="own-thread")
                if incomplete_arm != "own":
                    _ = self.rollout("own-thread", usage(15, 200))
                text = self.report()
                self.assertEqual(text.splitlines()[-1],
                                 "pool tokens per review: no comparison yet (complete costs needed for both arms)")
                self.assertNotIn("unavailable%", text)

    def test_malformed_merge_hold_ledger_warns_and_preserves_quality_without_holds(self) -> None:
        _ = self.record("quality-review", raised=2)
        _ = self.rollout("thread-pool", usage(15, 100))
        self.events(self.finding(), self.finding(caught_by="main"))
        self.holds("unit", "4", 100)
        ledger = self.home / ".claude" / "data" / "review_regime.jsonl"
        with ledger.open("a", encoding="utf-8") as stream:
            _ = stream.write("{malformed ledger line\n")
        text = self.report("--quality")
        self.assertIn("source warning: merge holds unavailable:", text)
        self.quality_row(text, "pool", "code", 1, 2, 1, 50, 1)
        self.cost_row(text, "pool", "broad", 1, 100, 100)


if __name__ == "__main__":
    _ = unittest.main()
