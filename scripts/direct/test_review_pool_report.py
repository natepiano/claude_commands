#!/usr/bin/env python3
"""Report contracts exercised with isolated history and shared usage stores."""

from __future__ import annotations

import json
import re
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Literal, TypedDict, cast, override
from unittest.mock import patch

from scripts.direct import review_pool_report
from scripts.usage import collect
from scripts.usage.agents_log import RoleLog


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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


class MacReportProcess:
    def __init__(self, output: str) -> None:
        self.stdout: StringIO = StringIO(output)

    def wait(self) -> int:
        return 0


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
               raised: int = 0, history_path: str = "") -> Path:
        record: ReviewTurnJson = {
            "arm": arm, "pool": "production" if arm == "pool" else "",
            "reviewer": "code1", "thread_id": thread, "unit": unit, "phase": phase,
            "phase_instance_id": phase_instance, "history_file": history_path or str(self.history_file),
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

    def finding(self, *, phase_instance: str = "phase-instance", caught_by: str = "worker",
                lens: str = "contract", event_type: str = "finding_opened") -> dict[str, object]:
        self.sequence += 1
        return {"event_type": event_type, "event_id": f"event-{self.sequence}",
                "finding_id": f"finding-{self.sequence}", "phase_instance_id": phase_instance,
                "phase_id": "4", "timestamp": timestamp(15), "timestamp_epoch": EPOCH + 15,
                "caught_by": caught_by, "lens": lens, "round": 1}

    def events(self, *events: dict[str, object]) -> None:
        _ = self.history_file.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                                         encoding="utf-8")

    def holds(self, unit: str, phase: str, count: int, **fields: object) -> None:
        ledger = self.home / ".claude" / "data" / "review_regime.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row: dict[str, object] = {"unit": unit, "phase": phase, "regime": "after", "started": timestamp(0),
                                  "merged": timestamp(30), "holds": count, "merge_defects": 0}
        row.update(fields)
        with ledger.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(row) + "\n")

    def run_started(self, plan: str, *, working_dir: str = "") -> dict[str, object]:
        return {"event_type": "run_started", "plan_doc": plan,
                "working_dir": working_dir or str(self.home)}

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

    def report_result(self, *options: str, explicit_root: bool = True,
                      usage_store: Literal["ready", "absent", "unreadable"] = "ready") -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["UNIT_HISTORY_DIR"] = str(self.history)
        environment["REVIEW_POOL_STATE"] = str(self.home / "review-pool")
        usage_state = self.home / "usage-state"
        self.populate_usage_store(usage_state)
        if usage_store != "ready":
            (usage_state / "usage.db").unlink()
        if usage_store == "unreadable":
            _ = (usage_state / "usage.db").write_text("invalid database", encoding="utf-8")
        environment["USAGE_STATE_DIR"] = str(usage_state)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        _ = environment.pop("CODEX_HOME", None)
        command = [sys.executable, str(SCRIPT), "--since", SINCE, "--no-mac"]
        if explicit_root:
            command.extend(("--root", str(self.history)))
        result = subprocess.run([*command, *options], env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
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
            if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            if cells[:len(key)] == list(key):
                return cells[len(key):]
        self.fail(f"missing {section} row {key!r}:\n{text}")

    def cost_row(self, text: str, kind: str, count: int,
                 median: float, mean: float, cached_share: float = 0.0) -> None:
        cells = self.table_row(text, "Kind Reviews", kind)
        self.assertEqual(int(cells[0]), count, text)
        self.assertAlmostEqual(float(cells[1]), median, delta=0.1, msg=text)
        self.assertAlmostEqual(float(cells[2]), mean, delta=0.1, msg=text)
        self.assertAlmostEqual(float(cells[3].rstrip("%")), cached_share, delta=0.1, msg=text)

    def compaction_counts(self, text: str, began: int = 0) -> None:
        lines = text.splitlines()
        incomplete = next(index for index, line in enumerate(lines)
                          if line.startswith("cost incomplete:"))
        expected = f"began with a compaction: {began}"
        self.assertEqual(lines[incomplete + 1], expected, text)
        self.assertEqual(sum(line.startswith("began with a compaction:") for line in lines), 1, text)

    def test_quality_counts_rerun_findings_as_code_review(self) -> None:
        path = self.record("rerun", raised=1)
        record = cast(dict[str, object], json.loads(path.read_text()))
        record["lens"] = "rerun"
        _ = path.write_text(json.dumps(record))
        _ = self.rollout("thread-pool", usage(15, 100))
        self.events(self.finding(lens="rerun"))
        self.quality_row(self.report("--quality"), 1, 1, 1, 100, 0)

    def test_known_request_costs_group_by_kind_and_lease_ordinal(self) -> None:
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
        # Old records written with "arm": "own" count beside pool records.
        self.cost_row(text, "broad", 3, 260, 450, 100 * 15 / 1350)
        self.cost_row(text, "closure", 2, 300, 300)
        self.cost_row(text, "ux", 2, 475, 475, 100 * 50 / 950)
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
        self.cost_row(self.report(), "broad", 1, 300, 300)

    def test_median_and_mean_use_individual_review_costs(self) -> None:
        for index, cost in enumerate((100, 200, 900)):
            thread = f"thread-{index}"
            _ = self.record(thread, thread=thread)
            _ = self.rollout(thread, usage(15, cost))
        self.cost_row(self.report(), "broad", 3, 200, 400)

    def test_reset_rollout_files_with_same_thread_id_are_all_counted(self) -> None:
        _ = self.record("reset-files")
        _ = self.rollout("thread-pool", usage(12, 100), filename="opaque-first.jsonl")
        _ = self.rollout("thread-pool", usage(18, 200), filename="opaque-second.jsonl")
        self.cost_row(self.report(), "broad", 1, 300, 300)

    def test_unrelated_thread_is_excluded_even_with_identical_request_times(self) -> None:
        _ = self.record("one-thread")
        _ = self.rollout("thread-pool", usage(15, 100))
        _ = self.rollout("unrelated-thread", usage(15, 1000000),
                         filename="rollout-thread-pool.jsonl")
        self.cost_row(self.report(), "broad", 1, 100, 100)

    def test_reviewer_name_reuse_does_not_merge_different_thread_costs(self) -> None:
        _ = self.record("first-lifetime", thread="old-thread")
        _ = self.record("second-lifetime", thread="new-thread", kind="closure")
        _ = self.rollout("old-thread", usage(15, 100))
        _ = self.rollout("new-thread", usage(15, 200))
        text = self.report()
        self.cost_row(text, "broad", 1, 100, 100)
        self.cost_row(text, "closure", 1, 200, 200)

    def test_overlapping_whole_second_windows_assign_request_to_earlier_review_once(self) -> None:
        # Filenames deliberately sort in the opposite order to review starts.
        _ = self.record("z-earlier", start=10, finish=20.8)
        _ = self.record("a-later", start=20, finish=30, kind="closure", ordinal=2)
        _ = self.rollout("thread-pool", usage(20.5, 100), usage(21, 200))
        text = self.report()
        self.cost_row(text, "broad", 1, 100, 100)
        self.cost_row(text, "closure", 1, 200, 200)

    def test_incomplete_costs_are_named_and_excluded_from_means_and_medians(self) -> None:
        missing = self.record("missing-rollout", thread="missing-thread")
        compacted = self.record("compacted-turn", thread="compacted-thread")
        _ = self.record("complete", thread="complete-thread")
        _ = self.rollout("compacted-thread", usage(12, 900000, 900000, 20),
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}})
        _ = self.rollout("complete-thread", usage(15, 100))
        text = self.report()
        self.cost_row(text, "broad", 3, 100, 100)
        self.assertIn("cost incomplete: 2", text)
        self.assertIn(missing.name, text)
        self.assertIn(compacted.name, text)
        self.assertIn(f"{compacted} (a compaction after its first request)", text)
        self.compaction_counts(text)

    def test_compaction_outside_window_does_not_make_review_cost_incomplete(self) -> None:
        _ = self.record("outside-compaction")
        _ = self.rollout("thread-pool", usage(15, 100),
                         {"timestamp": timestamp(21), "type": "compacted", "payload": {}})
        text = self.report()
        self.cost_row(text, "broad", 1, 100, 100)
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
        self.cost_row(text, "broad", 3, 100, 100)
        self.assertIn("outcomes: void 1; canceled 1; error 1", text)
        cells = self.table_row(text, "Phases Raised")
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
        self.cost_row(text, "broad", 2, 150, 150)
        self.quality_row(text, 2, 6, 2, 100 / 3, 0)

    def test_all_four_statuses_are_counted_separately_across_old_and_new_records(self) -> None:
        for arm in ("pool", "own"):
            for status in ("reviewed", "void", "canceled", "error"):
                thread = f"{arm}-{status}"
                _ = self.record(thread, arm=arm, thread=thread, status=status,
                                phase_instance=f"phase-{arm}", raised=2 if status == "reviewed" else 100)
                _ = self.rollout(thread, usage(15, 100 if status == "reviewed" else 100000))
        text = self.report("--quality")
        self.cost_row(text, "broad", 2, 100, 100)
        self.assertIn("outcomes: void 2; canceled 2; error 2", text)
        cells = self.table_row(text, "Phases Raised")
        self.assertEqual(int(cells[1]), 4, text)

    def test_since_filters_turns_and_old_rollout_files(self) -> None:
        _ = self.record("before-date", start=-20, finish=-10, thread="old-thread")
        _ = self.rollout("old-thread", usage(-15, 100000))
        _ = self.record("since-date", thread="thread-pool")
        _ = self.rollout("thread-pool", usage(15, 100))
        obsolete = self.rollout("thread-pool", usage(15, 100000))
        os.utime(obsolete, (EPOCH - 1, EPOCH - 1))
        self.cost_row(self.report(), "broad", 1, 100, 100)

    def test_history_root_environment_is_used_when_root_flag_is_omitted(self) -> None:
        _ = self.record("environment-root")
        _ = self.rollout("thread-pool", usage(15, 100))
        self.cost_row(self.report(explicit_root=False), "broad", 1, 100, 100)

    def quality_row(self, text: str, phases: int, raised: int,
                    confirmed: int, precision: float, escapes: float) -> None:
        cells = self.table_row(text, "Phases Raised")
        self.assertEqual([int(cell) for cell in cells[:3]], [phases, raised, confirmed], text)
        self.assertAlmostEqual(float(cells[3].rstrip("%")), precision, delta=0.1, msg=text)
        self.assertAlmostEqual(float(cells[4]), escapes, delta=0.1, msg=text)

    def test_quality_joins_code_phase_instances_once_and_excludes_ux_only_phases(self) -> None:
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
        self.quality_row(text, 2, 10, 4, 40, 1.5)

    def test_quality_counts_an_old_own_code_record_when_a_pool_ui_record_sorts_first(self) -> None:
        _ = self.record("a-ui", kind="ux", raised=100)
        _ = self.record("z-code", arm="own", thread="own-code", raised=2)
        _ = self.rollout("thread-pool", usage(15, 100))
        _ = self.rollout("own-code", usage(15, 100))
        self.events(self.finding())
        text = self.report("--quality")
        self.quality_row(text, 1, 2, 1, 50, 0)
        self.cost_row(text, "ux", 1, 100, 100)

    def test_quality_helper_ux_findings_change_no_code_quality_cell(self) -> None:
        _ = self.record("code", raised=2)
        _ = self.record("ui", thread="ui-thread", kind="ux", raised=1)
        _ = self.rollout("thread-pool", usage(15, 100))
        _ = self.rollout("ui-thread", usage(15, 100))
        self.events(self.finding(), self.finding(caught_by="main"))
        before = self.report("--quality")
        self.events(self.finding(), self.finding(caught_by="main"),
                    self.finding(lens="ux"), self.finding(lens="ux", caught_by="both"),
                    self.finding(lens="ux", caught_by="main"))
        after = self.report("--quality")
        self.assertEqual(before, after)
        self.quality_row(after, 1, 2, 1, 50, 1)

    def test_quality_prints_code_only_table_and_shadow_direction(self) -> None:
        _ = self.record("code", raised=2)
        _ = self.rollout("thread-pool", usage(15, 100))
        self.events(self.finding())
        text = self.report("--quality")
        table = text.split("Code review quality\n", 1)[1]
        self.assertEqual(table.splitlines(), [
            "| Phases | Raised | Confirmed | Precision | Escapes per phase |",
            "| ---: | ---: | ---: | ---: | ---: |",
            "| 1 | 2 | 1 | 50.0% | 0.0 |",
            "ui checks: compared by --shadow, not in this table",
        ])

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
        self.quality_row(self.report("--quality"), 2, 6, 2, 100 / 3, 2)

    def test_review_plan_returns_known_normalized_path_from_run_started(self) -> None:
        path = self.record("review")
        record = review_pool_report.read_record(path.read_text(encoding="utf-8"), "local", str(path))
        sources = review_pool_report.ReportSources(
            histories={("local", str(self.history_file)): [self.finding(), self.run_started(
                str(self.home / "docs/plans/../plans/alpha.md"))]})
        result = review_pool_report.review_plan(record, sources)
        self.assertIsInstance(result, review_pool_report.PlanKnown)
        if isinstance(result, review_pool_report.PlanKnown):
            self.assertEqual(result.path, "docs/plans/alpha.md")

    def test_review_plan_returns_unavailable_with_reason_for_missing_run_metadata(self) -> None:
        path = self.record("review")
        record = review_pool_report.read_record(path.read_text(encoding="utf-8"), "local", str(path))
        histories: list[dict[tuple[str, str], list[dict[str, object]]]] = [
            {},
            {("local", str(self.history_file)): [self.finding()]},
            {("local", str(self.history_file)): [{"event_type": "run_started"}]},
            {("local", str(self.history_file)): [{"event_type": "run_started", "plan_doc": None}]},
        ]
        for index, history in enumerate(histories):
            with self.subTest(case=index):
                sources = review_pool_report.ReportSources(histories=history)
                result = review_pool_report.review_plan(record, sources)
                self.assertIsInstance(result, review_pool_report.PlanUnavailable)
                if isinstance(result, review_pool_report.PlanUnavailable):
                    self.assertTrue(result.reason.strip())

    def test_quality_matches_each_plan_hold_to_its_own_unit_phase_review(self) -> None:
        second_history = self.history_file.with_name("second-run.jsonl")
        _ = self.record("alpha-review", raised=2, phase="1")
        _ = self.record("beta-review", arm="own", thread="thread-own", raised=2, phase="1",
                        history_path=str(second_history))
        self.events(self.run_started("docs/plans/alpha.md"), self.finding())
        _ = second_history.write_text(
            "\n".join(json.dumps(event) for event in
                      (self.run_started("docs/plans/beta.md"), self.finding())) + "\n",
            encoding="utf-8")
        self.holds("unit", "1", 2, plan="docs/plans/alpha.md")
        self.holds("unit", "1", 5, plan="docs/plans/beta.md")
        self.holds("other-unit", "1", 100, plan="docs/plans/alpha.md")
        self.holds("unit", "2", 100, plan="docs/plans/beta.md")
        text = self.report("--quality")
        self.quality_row(text, 2, 4, 2, 50, 3.5)
        self.assertNotIn("merge holds unmatched:", text)

    def test_quality_keeps_unit_phase_matching_for_rows_without_a_string_plan(self) -> None:
        _ = self.record("review", raised=2)
        self.events(self.run_started("docs/plans/alpha.md"), self.finding())
        self.holds("unit", "4", 2)
        for plan in (None, 17, False, ["docs/plans/alpha.md"], {"path": "docs/plans/alpha.md"}):
            self.holds("unit", "4", 1, plan=plan)
        self.holds("other-unit", "4", 100)
        self.holds("unit", "5", 100)
        text = self.report("--quality")
        self.quality_row(text, 1, 2, 1, 50, 7)
        self.assertNotIn("merge holds unmatched:", text)

    def test_quality_normalizes_relative_and_working_directory_plan_paths(self) -> None:
        spellings = ("docs/plans/alpha.md", "./docs/plans/alpha.md",
                     str(self.home / "docs/plans/alpha.md"),
                     str(self.home / "docs/plans/../plans/alpha.md"))
        for index, plan in enumerate(spellings):
            history = self.history_file.with_name(f"run-{index}.jsonl")
            unit = f"unit-{index}"
            instance = f"phase-{index}"
            _ = self.record(unit, unit=unit, raised=2, phase_instance=instance, history_path=str(history))
            _ = history.write_text(
                "\n".join(json.dumps(event) for event in
                          (self.run_started(plan), self.finding(phase_instance=instance))) + "\n",
                encoding="utf-8")
            self.holds(unit, "4", 2, plan="docs/plans/alpha.md")
            self.holds(unit, "4", 3, plan="./docs/plans/alpha.md")
            self.holds(unit, "4", 4, plan="docs/plans/../plans/alpha.md")
            # The ledger has no working directory for making absolute paths relative.
            self.holds(unit, "4", 100, plan=str(self.home / "docs/plans/alpha.md"))
        text = self.report("--quality")
        self.quality_row(text, 4, 8, 4, 50, 9)
        self.assertNotIn("merge holds unmatched:", text)

    def test_quality_keeps_absolute_plan_outside_working_directory_absolute(self) -> None:
        plan = str(self.home / "other-worktree/docs/plans/alpha.md")
        _ = self.record("review", raised=2)
        self.events(self.run_started(plan, working_dir=str(self.home / "worktree")), self.finding())
        self.holds("unit", "4", 3, plan=plan)
        self.holds("unit", "4", 100, plan="docs/plans/alpha.md")
        text = self.report("--quality")
        self.quality_row(text, 1, 2, 1, 50, 3)

    def test_quality_warns_once_per_phase_when_review_plan_is_unavailable(self) -> None:
        _ = self.record("broad", raised=2)
        _ = self.record("closure", kind="closure", raised=2)
        _ = self.record("second-phase", phase="5", phase_instance="second-phase", raised=2)
        self.holds("unit", "4", 100, plan="docs/plans/alpha.md")
        self.holds("unit", "4", 200, plan="docs/plans/beta.md")
        self.holds("unit", "5", 100, plan="docs/plans/alpha.md")
        self.holds("other-unit", "4", 100, plan="docs/plans/alpha.md")
        self.holds("unit", "6", 100, plan="docs/plans/alpha.md")
        self.holds("unit", "4", 2)
        cases: list[tuple[str, list[dict[str, object]]]] = [
            ("missing history", []),
            ("no run_started", [self.finding(), self.finding(caught_by="main")]),
            ("no plan_doc", [{"event_type": "run_started", "working_dir": str(self.home)},
                             self.finding(), self.finding(caught_by="main")]),
        ]
        for name, events in cases:
            with self.subTest(history=name):
                if name == "missing history":
                    self.history_file.unlink(missing_ok=True)
                else:
                    self.events(*events)
                text = self.report("--quality")
                cells = self.table_row(text, "Phases Raised")
                self.assertEqual(cells[:2], ["2", "6"], text)
                self.assertEqual(float(cells[4]), 1 if name == "missing history" else 1.5, text)
                warnings = [line for line in text.splitlines()
                            if line.startswith("source warning: merge holds unmatched:")]
                self.assertEqual(len(warnings), 2, text)
                for phase in ("4", "5"):
                    prefix = f"source warning: merge holds unmatched: unit phase {phase}: "
                    matches = [line for line in warnings if line.startswith(prefix)]
                    self.assertEqual(len(matches), 1, text)
                    self.assertRegex(matches[0][len(prefix):], r"^review plan unavailable \(.+\)$")

    def test_quality_needs_no_plan_warning_without_matching_plan_rows(self) -> None:
        _ = self.record("review", raised=2)
        self.events(self.finding())
        self.holds("unit", "4", 2)
        self.holds("other-unit", "4", 100, plan="docs/plans/alpha.md")
        self.holds("unit", "5", 100, plan="docs/plans/alpha.md")
        text = self.report("--quality")
        self.quality_row(text, 1, 2, 1, 50, 2)
        self.assertNotIn("merge holds unmatched:", text)

    def test_mac_sources_match_plan_holds_using_remote_history_events(self) -> None:
        remote_history = "/Users/test/history/run.jsonl"
        record = self.record("mac-review", raised=2, history_path=remote_history)
        events = [self.run_started("/Users/test/worktree/docs/plans/alpha.md",
                                   working_dir="/Users/test/worktree"), self.finding()]
        rows: list[dict[str, object]] = [
            {"unit": "unit", "phase": "4", "plan": "./docs/plans/alpha.md", "holds": 3},
            {"unit": "unit", "phase": "4", "plan": "docs/plans/beta.md", "holds": 100},
            {"unit": "unit", "phase": "4", "holds": 2},
        ]
        envelopes = [
            {"kind": "record", "path": "/Users/test/history/review_turns/run/review.json",
             "text": record.read_text(encoding="utf-8")},
            {"kind": "history", "path": remote_history,
             "text": "\n".join(json.dumps(event) for event in events)},
            {"kind": "holds", "path": "/Users/test/.claude/data/review_regime.jsonl",
             "text": "\n".join(json.dumps(row) for row in rows)},
        ]
        process = MacReportProcess("\n".join(json.dumps(envelope) for envelope in envelopes))
        with patch.object(subprocess, "Popen", return_value=process) as launch:
            sources = review_pool_report.mac_sources("", quality=True)
        launch.assert_called_once()
        self.assertEqual(len(sources.records), 1)
        self.assertEqual(sources.records[0].machine, "mac")
        costs = {item.key: review_pool_report.IncompleteCost("fixture") for item in sources.records}
        text = review_pool_report.render_report(sources.records, costs, True, sources=sources)
        self.quality_row(text, 1, 2, 1, 50, 5)
        self.assertNotIn("merge holds unmatched:", text)
        sources.histories[("mac", remote_history)] = [self.finding()]
        text = review_pool_report.render_report(sources.records, costs, True, sources=sources)
        self.quality_row(text, 1, 2, 1, 50, 2)
        warnings = [line for line in text.splitlines()
                    if line.startswith("source warning: merge holds unmatched: unit phase 4:")]
        self.assertEqual(len(warnings), 1, text)
        self.assertRegex(warnings[0], r": review plan unavailable \(.+\)$")

    def test_surviving_earlier_rollout_without_a_request_in_review_window_is_incomplete(self) -> None:
        missing_request = self.record("after-reset", thread="reset-thread")
        _ = self.rollout("reset-thread", usage(5, 1000))
        _ = self.record("complete", thread="complete-thread")
        _ = self.rollout("complete-thread", usage(15, 100))
        text = self.report()
        self.cost_row(text, "broad", 2, 100, 100)
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{missing_request} (no request in its window)", text)

    def test_request_with_unweighted_model_makes_review_cost_incomplete_and_named(self) -> None:
        incomplete = self.record("unknown-model", thread="unknown-thread")
        _ = self.rollout("unknown-thread", usage(15, 100000, 100000, 100),
                         model="fixture-unweighted-model")
        _ = self.record("weighted-review", thread="weighted-thread")
        _ = self.rollout("weighted-thread", usage(15, 60, 100, 5))
        text = self.report()
        self.cost_row(text, "broad", 2, 90, 90, 100 * 5 / 90)
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{incomplete} (unweighted model fixture-unweighted-model)", text)
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
        self.assertEqual(self.table_row(text, "Kind Reviews", "broad"),
                         ["3", "-", "-", "-"], text)
        self.compaction_counts(text)

    def test_compaction_and_its_zero_counter_sample_without_requests_are_incomplete(self) -> None:
        record = self.record("compaction-only")
        _ = self.rollout("thread-pool",
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}},
                         usage(15.003, 0))
        text = self.report()
        self.assertEqual(self.table_row(text, "Kind Reviews", "broad"),
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
        self.cost_row(text, "broad", 1, 350, 350, 100 * 15 / 350)
        self.assertIn("cost incomplete: 0", text)
        self.compaction_counts(text, 1)

    def test_pool_startup_compaction_and_requests_in_separate_files_are_complete(self) -> None:
        _ = self.record("pool-startup-compaction")
        # File order deliberately puts the requests before the compaction file.
        _ = self.rollout("thread-pool", usage(15, 100), usage(18, 200),
                         filename="a-requests.jsonl")
        _ = self.rollout("thread-pool",
                         {"timestamp": timestamp(12), "type": "compacted", "payload": {}},
                         usage(12.003, 0), filename="z-compaction.jsonl")
        text = self.report()
        self.cost_row(text, "broad", 1, 300, 300)
        self.assertIn("cost incomplete: 0", text)
        self.compaction_counts(text, 1)

    def test_compaction_between_owned_requests_is_incomplete_and_not_a_startup_count(self) -> None:
        record = self.record("mid-review-compaction")
        _ = self.rollout("thread-pool", usage(12, 100),
                         {"timestamp": timestamp(15), "type": "compacted", "payload": {}},
                         usage(15.003, 0), usage(18, 200))
        text = self.report()
        self.assertIn("cost incomplete: 1", text)
        self.assertIn(f"{record} (a compaction after its first request)", text)
        self.assertEqual(self.table_row(text, "Kind Reviews", "broad"),
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
        self.assertEqual(self.table_row(text, "Kind Reviews", "broad"),
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

    def test_code_costs_exclude_ux_reviews(self) -> None:
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
        self.cost_row(text, "broad", 11, 100, 109.1)
        self.cost_row(text, "closure", 10, 100, 100)
        self.cost_row(text, "ux", 2, 100000, 100000)

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
                    self.cost_row(result.stdout, "broad", 1, 100, 100)
                    self.assertEqual(self.table_row(result.stdout, "Lease ordinal", "1")[0], "1")
                    self.assertEqual(self.table_row(result.stdout, "Phases Raised")[1], "2")

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
        self.quality_row(text, 1, 2, 1, 50, 1)
        self.cost_row(text, "broad", 1, 100, 100)

    def test_absent_usage_store_names_each_reviewed_record_and_source_warning(self) -> None:
        reviewed = [self.record("pool-review"), self.record("own-review", arm="own"),
                    self.record("ux-review", kind="ux")]
        not_reviewed = self.record("failed-review", status="error")
        text = self.report_result(usage_store="absent").stdout
        for path in reviewed:
            self.assertIn(f"{path} (usage store unavailable)", text)
        self.assertIn("cost incomplete: 3", text)
        self.assertIn("source warning: usage store unavailable:", text)
        self.assertNotIn(f"{not_reviewed} (usage store unavailable)", text)
        self.assertNotIn("missing rollout", text)

    def test_unreadable_usage_store_names_unavailable_instead_of_missing_rollout(self) -> None:
        path = self.record("review")
        text = self.report_result(usage_store="unreadable").stdout
        self.assertIn(f"{path} (usage store unavailable)", text)
        self.assertIn("source warning: usage store unavailable:", text)
        self.assertNotIn("missing rollout", text)

    def test_mac_usage_source_not_ready_names_state_and_last_complete_read(self) -> None:
        self.populate_usage_store(self.home / "usage-state")
        with closing(sqlite3.connect(self.home / "usage-state" / "usage.db")) as db, db:
            _ = db.execute("INSERT INTO turn_sources VALUES (?, ?, ?, ?, ?)",
                           ("mac", "codex", "offline", timestamp(20), timestamp(10)))
        with patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.home / "usage-state")}):
            sources = review_pool_report.ReportSources()
            review_pool_report.stored_usage(sources, include_mac=True)
        text = review_pool_report.render_report([], {}, False, sources=sources)
        self.assertIn(f"source warning: Mac usage source offline; last complete read {timestamp(10)}", text)

    def verdict(self, check: str, arm: str, *rows: str, candidates: str = "") -> Path:
        path = self.turns / f"ux_{check}_{arm}.txt"
        text = "Verdict: " + ("defects" if rows else "pass") + "\n\n"
        text += "| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
        text += "\n".join(rows) + "\n" + candidates
        _ = path.write_text(text, encoding="utf-8")
        return path

    def shadow(self, *options: str, explicit_root: bool = True) -> str:
        environment = os.environ.copy()
        environment.update({"HOME": str(self.home), "UNIT_HISTORY_DIR": str(self.history),
                            "REVIEW_POOL_STATE": str(self.home / "review-pool"),
                            "USAGE_STATE_DIR": str(self.home / "absent-store"),
                            "PYTHONDONTWRITEBYTECODE": "1"})
        # A remote call would fail the check; --shadow must read only the local pairs.
        environment["PATH"] = str(self.home / "no-executables")
        command = [sys.executable, str(SCRIPT), "--shadow"]
        if explicit_root:
            command.extend(("--root", str(self.history)))
        result = subprocess.run([*command, *options], env=environment, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_shadow_lists_helper_pool_and_missed_rows_including_recheck(self) -> None:
        common = "| panel.png | readable-size | Label 5 px (floor 7) | Enlarge label |"
        missed = "| panel.png | even-alignment-and-spacing | Knob off center | Center knob |"
        extra = "| panel.png | one-letter-case | Title in capitals | Use sentence case |"
        _ = self.verdict("6_1", "helper", common, missed)
        _ = self.verdict("6_1", "pool", common, extra)
        _ = self.verdict("6_2", "helper", missed)
        _ = self.verdict("6_2", "pool")
        text = self.shadow()
        first = text.split("check: fixture-run/ux_6_1", 1)[1].split("check: fixture-run/ux_6_2", 1)[0]
        self.assertIn("helper defects: 2", first)
        self.assertIn("pool defects: 2", first)
        self.assertIn("pool missed defects: 1", first)
        missing_rows = first.split("pool missed defects:", 1)[1]
        self.assertIn(missed, missing_rows)
        self.assertNotIn(common, missing_rows)
        self.assertNotIn(extra, missing_rows)
        self.assertIn("complete shadow checks: 2; incomplete: 0; pool missed rows: 2", text)

    def test_shadow_names_short_local_machine_immediately_after_comparison(self) -> None:
        lines = self.shadow().splitlines()
        comparison = next(index for index, line in enumerate(lines) if line.startswith("Comparison:"))
        expected = (f"Machine: {socket.gethostname().split('.')[0]} "
                    "(pairs saved on another machine are not counted)")
        self.assertEqual(lines[comparison + 1], expected)
        self.assertEqual(sum(line.startswith("Machine:") for line in lines), 1)

    def test_shadow_shortens_a_qualified_host_name(self) -> None:
        with patch.object(socket, "gethostname", return_value="fixture.example.test"):
            text = review_pool_report.render_shadow(self.history)
        self.assertIn("Machine: fixture (pairs saved on another machine are not counted)", text)

    def test_other_reports_keep_local_name_and_omit_machine_line(self) -> None:
        record = self.record("review")
        for options in ((), ("--quality",)):
            with self.subTest(options=options):
                text = self.report(*options)
                self.assertNotIn("Machine:", text)
                self.assertIn(f"local:{record}", text)

    def test_shadow_names_unpaired_helper_and_pool_files(self) -> None:
        helper = self.verdict("6_1", "helper")
        pool = self.verdict("6_2", "pool")
        text = self.shadow()
        self.assertIn(f"unpaired file: {helper}; missing {self.turns / 'ux_6_1_pool.txt'}", text)
        self.assertIn(f"unpaired file: {pool}; missing {self.turns / 'ux_6_2_helper.txt'}", text)
        self.assertIn("complete shadow checks: 0; incomplete: 2", text)

    def test_shadow_pass_excludes_headers_separators_and_canonical_candidates(self) -> None:
        candidates = ("\nCanonical candidates\n\n"
                      "| Shot | Rule | Slot | Crop (full-size px) | Why it is clear |\n"
                      "| --- | --- | --- | --- | --- |\n"
                      "| panel.png | readable-size | good | 20x20+1+1 | Large text |\n")
        _ = self.verdict("6_1", "helper", candidates=candidates)
        _ = self.verdict("6_1", "pool", candidates=candidates)
        text = self.shadow()
        self.assertIn("helper defects: 0", text)
        self.assertIn("pool defects: 0", text)
        self.assertIn("pool missed defects: 0", text)
        self.assertNotIn("Large text", text)

    def test_shadow_matches_rule_and_shot_even_when_defect_and_fix_wording_differ(self) -> None:
        _ = self.verdict("6_1", "helper", "| panel.png | simple-words | Long   label | Shorten label |")
        _ = self.verdict("6_1", "pool", "| panel.png | simple-words | Caption is too long | Rename label |")
        text = self.shadow()
        self.assertIn("pool missed defects: 0", text)

    def test_shadow_keeps_escaped_pipes_inside_defect_cells(self) -> None:
        row = r"| panel.png | simple-words | Label says a \| b | Rename |"
        _ = self.verdict("6_1", "helper", row)
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertIn("pool missed defects: 1", text)
        self.assertIn(row, text)

    def test_shadow_keeps_three_defect_rows_with_an_open_last_cell_and_escaped_pipe(self) -> None:
        helper = self.turns / "ux_6_1_helper.txt"
        _ = helper.write_text(
            "Verdict: defects\n\n"
            + "| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
            + "| a.png | cables-drape-smoothly | sag crosses the face | lift the cable |\n"
            + "| b.png | no rule | label cut at the right edge | widen the field\n"
            + "| c.png | widgets-follow-macos | reads `on \\| off` as one word | split the label |\n\n"
            + "Canonical candidates\n\n"
            + "| Shot | Rule | Slot | Crop (full-size px) | Why it is clear |\n"
            + "| --- | --- | --- | --- | --- |\n"
            + "| /tmp/a.png | cables-drape-smoothly | bad | 40x40+1+1 | one cable |\n",
            encoding="utf-8")
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertIn("helper defects: 3", text)
        self.assertIn("pool missed defects: 3", text)
        self.assertNotIn("one cable", text)

    def test_shadow_verdict_without_a_verdict_line_is_incomplete_and_named(self) -> None:
        helper = self.verdict("6_1", "helper")
        _ = helper.write_text("The helper produced no summary.\n", encoding="utf-8")
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertIn(f"verdict unavailable: {helper}: missing verdict line", text)
        self.assertIn("complete shadow checks: 0; incomplete: 1; pool missed rows: 0", text)

    def test_shadow_pass_with_a_defect_row_is_incomplete_and_named(self) -> None:
        helper = self.verdict("6_1", "helper", "| a.png | readable-size | Small | Enlarge |")
        _ = helper.write_text(helper.read_text().replace("Verdict: defects", "Verdict: pass"), encoding="utf-8")
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertIn(f"verdict unavailable: {helper}: pass verdict has defect rows", text)
        self.assertIn("complete shadow checks: 0; incomplete: 1; pool missed rows: 0", text)

    def test_shadow_defects_verdict_without_rows_is_incomplete_and_named(self) -> None:
        _ = self.verdict("6_1", "helper")
        pool = self.verdict("6_1", "pool")
        _ = pool.write_text(pool.read_text().replace("Verdict: pass", "Verdict: defects"), encoding="utf-8")
        text = self.shadow()
        self.assertIn(f"verdict unavailable: {pool}: defects verdict has no defect rows", text)
        self.assertIn("complete shadow checks: 0; incomplete: 1; pool missed rows: 0", text)

    def test_shadow_accepts_bold_verdict_lines_in_any_letter_case(self) -> None:
        row = "| a.png | readable-size | Small | Enlarge |"
        for arm, verdict in (("helper", "**vErDiCt: dEfEcTs**"), ("pool", "**VERDICT: PASS**")):
            path = self.verdict("6_1", arm, *([row] if arm == "helper" else []))
            _ = path.write_text(path.read_text().replace(path.read_text().splitlines()[0], verdict), encoding="utf-8")
        text = self.shadow()
        self.assertIn("complete shadow checks: 1; incomplete: 0; pool missed rows: 1", text)

    def test_shadow_accepts_a_verdict_line_with_only_its_label_or_value_in_bold(self) -> None:
        row = "| a.png | readable-size | Small | Enlarge |"
        for arm, verdict in (("helper", "**Verdict:** defects"), ("pool", "Verdict: **pass**")):
            path = self.verdict("6_1", arm, *([row] if arm == "helper" else []))
            _ = path.write_text(path.read_text().replace(path.read_text().splitlines()[0], verdict), encoding="utf-8")
        text = self.shadow()
        self.assertIn("complete shadow checks: 1; incomplete: 0; pool missed rows: 1", text)

    def test_shadow_pass_without_a_defect_table_is_incomplete_and_named(self) -> None:
        helper = self.verdict("6_1", "helper")
        _ = helper.write_text("Verdict: pass\n", encoding="utf-8")
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertIn(f"verdict unavailable: {helper}: missing defect table", text)
        self.assertIn("complete shadow checks: 0; incomplete: 1", text)

    def test_shadow_multiple_verdict_lines_are_incomplete_and_named(self) -> None:
        helper = self.verdict("6_1", "helper")
        _ = helper.write_text("Verdict: defects\n" + helper.read_text(), encoding="utf-8")
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertIn(f"verdict unavailable: {helper}: multiple verdict lines", text)
        self.assertIn("complete shadow checks: 0; incomplete: 1", text)

    def test_shadow_invalid_defect_tables_are_incomplete_and_named(self) -> None:
        cases = {
            "6_1": ("| Shot | Rule | Defect | Fix |\n", "missing defect table separator"),
            "6_2": ("| Shot | Rule | Defect | Fix |\n| a | b | c | d |\n", "invalid defect table separator"),
            "6_3": ("| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n| a | b | c |\n",
                    "invalid defect table row"),
            "6_4": ("| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n\n"
                    + "| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n", "multiple defect tables"),
        }
        for check, (table, reason) in cases.items():
            path = self.verdict(check, "helper")
            _ = path.write_text("Verdict: pass\n\n" + table, encoding="utf-8")
            _ = self.verdict(check, "pool")
        text = self.shadow()
        for check, (_, reason) in cases.items():
            self.assertIn(f"verdict unavailable: {self.turns / f'ux_{check}_helper.txt'}: {reason}", text)
        self.assertIn("complete shadow checks: 0; incomplete: 4", text)

    def test_shadow_two_unusable_files_count_as_one_incomplete_check(self) -> None:
        for arm in ("helper", "pool"):
            path = self.verdict("6_1", arm)
            _ = path.write_text("No summary.\n", encoding="utf-8")
        text = self.shadow()
        self.assertEqual(text.count("verdict unavailable:"), 2)
        self.assertIn("complete shadow checks: 0; incomplete: 1", text)

    def test_shadow_same_rule_in_another_shot_is_missed(self) -> None:
        _ = self.verdict("6_1", "helper", "| a.png | readable-size | Small | Enlarge |")
        _ = self.verdict("6_1", "pool", "| b.png | readable-size | Small | Enlarge |")
        self.assertIn("pool missed defects: 1", self.shadow())

    def test_shadow_one_helper_row_for_two_shots_is_found_by_two_pool_rows(self) -> None:
        _ = self.verdict("6_1", "helper", "| `a.png`, `/tmp/b.png` | readable-size | Small in both | Enlarge |")
        _ = self.verdict("6_1", "pool", "| /other/a.png | readable-size | Tiny label | Enlarge |",
                         "| b.png | readable-size | Caption too small | Enlarge |")
        self.assertIn("pool missed defects: 0", self.shadow())

    def test_shadow_two_helper_rows_against_one_pool_row_leave_one_missed(self) -> None:
        _ = self.verdict("6_1", "helper", "| a.png | readable-size | Small label | Enlarge |",
                         "| a.png | readable-size | Small caption | Enlarge |")
        _ = self.verdict("6_1", "pool", "| a.png | readable-size | Tiny text | Enlarge |")
        self.assertIn("pool missed defects: 1", self.shadow())

    def test_shadow_matches_rule_case_backticks_and_markdown_suffix(self) -> None:
        _ = self.verdict("6_1", "helper", "| a.png | `Cables-Drape-Smoothly.md` | Sag crosses face | Lift |")
        _ = self.verdict("6_1", "pool", "| a.png | cables-drape-smoothly | Cable over label | Lift |")
        text = self.shadow()
        self.assertIn("pool missed defects: 0", text)
        self.assertIn("Comparison: rule and shot file name, by count; Defect and Fix wording is not compared.", text)

    def test_shadow_missed_multi_shot_row_consumes_no_pairs_needed_by_later_rows(self) -> None:
        _ = self.verdict("6_1", "helper", "| a.png, b.png | readable-size | Small in both | Enlarge |",
                         "| a.png | readable-size | Small label | Enlarge |")
        _ = self.verdict("6_1", "pool", "| a.png | readable-size | Tiny text | Enlarge |")
        self.assertIn("pool missed defects: 1", self.shadow())

    def test_shadow_rejects_a_verdict_line_with_extra_text(self) -> None:
        path = self.verdict("6_1", "helper")
        _ = path.write_text(path.read_text().replace("Verdict: pass", "Verdict: pass pending"), encoding="utf-8")
        _ = self.verdict("6_1", "pool")
        text = self.shadow()
        self.assertEqual(text.count("missing verdict line"), 1)
        self.assertIn("complete shadow checks: 0; incomplete: 1", text)

    def test_shadow_accepts_a_verdict_line_with_stray_emphasis_marks(self) -> None:
        for check, line in (("6_1", "**Verdict: pass"), ("6_2", "Verdict: pass**"), ("6_3", "* Verdict: pass")):
            path = self.verdict(check, "helper")
            _ = path.write_text(path.read_text().replace("Verdict: pass", line), encoding="utf-8")
            _ = self.verdict(check, "pool")
        self.assertIn("complete shadow checks: 3; incomplete: 0", self.shadow())

    def test_shadow_needs_neither_usage_store_nor_records_and_uses_configured_root(self) -> None:
        _ = self.verdict("6_1", "helper")
        _ = self.verdict("6_1", "pool")
        _ = (self.turns / "invalid.json").write_text("invalid record", encoding="utf-8")
        text = self.shadow(explicit_root=False)
        self.assertIn("complete shadow checks: 1; incomplete: 0; pool missed rows: 0", text)
        self.assertNotIn("source warning:", text)
        self.assertFalse((self.home / "absent-store").exists())
        self.assertFalse((self.home / "review-pool").exists())

    def test_shadow_keeps_same_phase_check_numbers_separate_between_sessions(self) -> None:
        row = "| panel.png | simple-words | Long label | Shorten |"
        _ = self.verdict("6_1", "helper", row)
        _ = self.verdict("6_1", "pool")
        other = self.history / "review_turns" / "other-run"
        other.mkdir()
        for arm in ("helper", "pool"):
            _ = (other / f"ux_6_1_{arm}.txt").write_text(
                "Verdict: pass\n\n| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n",
                encoding="utf-8")
        text = self.shadow()
        self.assertIn("check: fixture-run/ux_6_1", text)
        self.assertIn("check: other-run/ux_6_1", text)
        self.assertIn("complete shadow checks: 2; incomplete: 0; pool missed rows: 1", text)

    def test_shadow_names_unreadable_pair_without_counting_it_as_complete(self) -> None:
        _ = self.verdict("6_1", "helper")
        (self.turns / "ux_6_1_pool.txt").mkdir()
        text = self.shadow()
        self.assertIn("verdict unavailable:", text)
        self.assertIn("ux_6_1_pool.txt", text)
        self.assertIn("complete shadow checks: 0; incomplete: 1", text)

    def test_shadow_reads_all_local_pairs_regardless_of_since_or_quality(self) -> None:
        _ = self.verdict("6_1", "helper")
        _ = self.verdict("6_1", "pool")
        text = self.shadow("--since", "2099-01-01", "--quality")
        self.assertIn("all local pairs; --since and --quality do not filter them", text)
        self.assertIn("complete shadow checks: 1", text)

    def test_cost_report_still_requires_since(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.history), "--no-mac"],
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--since is required unless --shadow is used", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
