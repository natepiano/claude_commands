#!/usr/bin/env python3
"""Width-trial scorecard tests using only a temporary build log."""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, override
from unittest.mock import patch

import store
import width_trial as trial
from test_index import use_test_log

use_test_log()

GIB = 1 << 30
BASE = 1_791_504_000.0  # 2026-10-09 00:00 UTC, an even 6,000-second block.
SCRIPT = Path(__file__).with_name("width_trial.py")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class VerdictCase(NamedTuple):
    name: str
    physical_wait: float
    physical_exec: float
    physical_failures: int
    logical_failures: int
    expected: str


class WidthTrialTests(unittest.TestCase):
    root: Path = Path()
    index: Path = Path()
    peaks: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        self.root.mkdir()
        self.index = self.root / store.INDEX_NAME
        self.peaks = self.root / "admission" / "anon_peaks.jsonl"
        self.enterContext(patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root)}))
        with closing(sqlite3.connect(self.index)) as database, database:
            _ = database.executescript(
                """
                CREATE TABLE steps(
                    id TEXT PRIMARY KEY,
                    host TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    duration_s REAL,
                    mem_wait_s REAL,
                    step TEXT,
                    argv TEXT,
                    repo TEXT,
                    worktree_name TEXT,
                    caller TEXT,
                    call_id TEXT,
                    status INTEGER,
                    finished_s REAL,
                    tests_run INTEGER
                );
                CREATE TABLE tests(step_id TEXT, host TEXT, repo TEXT, status TEXT);
                """
            )

    def clear_records(self) -> None:
        with closing(sqlite3.connect(self.index)) as database, database:
            _ = database.execute("DELETE FROM tests")
            _ = database.execute("DELETE FROM steps")
        self.peaks.unlink(missing_ok=True)

    def append_peak(
        self,
        call_id: str | None,
        peak: int,
        width: int | None,
        *,
        host: str = "natedev",
        step: str = "nextest",
        ended: float = BASE + 1_000,
    ) -> None:
        self.peaks.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {
            "host": host,
            "repo": "hana",
            "step": step,
            "worktree": "hana",
            "anon_peak_bytes": peak,
            "status": 0,
            "ended_at": store.utc_iso(ended),
            "test_threads": width,
            "call_id": call_id,
        }
        with self.peaks.open("a") as output:
            _ = output.write(json.dumps(record) + "\n")

    def insert_whole_hana(
        self,
        step_id: str,
        request: float,
        width: int | None,
        wait: float | None,
        exec_s: float,
        failed: bool,
        anon_peak: int | None,
        *,
        timed_out: bool = True,
        call_id: str | None = None,
        peak_ended: float | None = None,
    ) -> None:
        call_id = call_id or f"call-{step_id}"
        started = request + (wait if wait is not None else 0.0)
        finished_s = 10.0
        duration = finished_s + exec_s
        argv = ["cargo", "nextest", "run"]
        if width is not None:
            argv.extend(("--test-threads", str(width)))
        argv.extend(("--no-fail-fast", "--workspace", "--bin", "hana", "-E", "package(hana)"))
        with closing(sqlite3.connect(self.index)) as database, database:
            _ = database.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    step_id,
                    "natedev",
                    store.utc_iso(started),
                    store.utc_iso(started + duration),
                    duration,
                    wait,
                    "nextest",
                    json.dumps(argv),
                    "hana",
                    "hana",
                    "verify",
                    call_id,
                    1 if failed else 0,
                    finished_s,
                    1,
                ),
            )
            if timed_out:
                _ = database.execute(
                    "INSERT INTO tests VALUES (?, ?, ?, ?)",
                    (step_id, "natedev", "hana", "timed_out"),
                )
        if anon_peak is not None:
            self.append_peak(call_id, anon_peak, width, ended=peak_ended or started + duration)

    def insert_step(
        self,
        step_id: str,
        request: float,
        wait: float | None,
        step: str,
        argv: list[str],
        *,
        repo: str = "other",
        caller: str = "direct",
    ) -> None:
        started = request + (wait if wait is not None else 0.0)
        with closing(sqlite3.connect(self.index)) as database, database:
            _ = database.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    step_id,
                    "natedev",
                    store.utc_iso(started),
                    store.utc_iso(started + 1),
                    1.0,
                    wait,
                    step,
                    json.dumps(argv),
                    repo,
                    repo,
                    caller,
                    f"call-{step_id}",
                    0,
                    None,
                    None,
                ),
            )

    def populate_balanced(
        self,
        *,
        physical_wait: float = 10.0,
        logical_wait: float = 20.0,
        physical_exec: float = 90.0,
        logical_exec: float = 100.0,
        physical_failures: int = 1,
        logical_failures: int = 2,
    ) -> None:
        for block_offset in range(30):
            width = 16 if block_offset % 2 == 0 else 32
            wait = physical_wait if width == 16 else logical_wait
            exec_s = physical_exec if width == 16 else logical_exec
            failures = physical_failures if width == 16 else logical_failures
            peak = 5 * GIB if width == 16 else 10 * GIB
            block_start = BASE + block_offset * trial.BLOCK_S
            for run in range(4):
                self.insert_whole_hana(
                    f"step-{block_offset}-{run}",
                    block_start + 1_000 + run,
                    width,
                    wait,
                    exec_s,
                    run < failures,
                    peak,
                )

    def report(self, blocks: int = 30) -> str:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "report",
                "--since",
                iso(BASE),
                "--until",
                iso(BASE + blocks * trial.BLOCK_S),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "BUILDLOG_DIR": str(self.root), "TZ": "America/Los_Angeles"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def metric_line(self, output: str, prefix: str) -> str:
        return next(line for line in output.splitlines() if line.lstrip().startswith(prefix))

    def test_load_steps_recognizes_width_population_and_request_time(self) -> None:
        request = BASE + 1_234
        self.insert_whole_hana("focused", request, 16, 42, 100, False, 5 * GIB)

        steps = trial.load_steps(self.index, BASE, BASE + trial.BLOCK_S, "natedev")

        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertAlmostEqual(step["request"], request, places=3)
        self.assertEqual(step["block"], int(request // trial.BLOCK_S))
        self.assertEqual(step["width"], 16)
        self.assertTrue(step["whole_hana"])
        self.assertTrue(step["hana"])
        self.assertEqual(step["wait"], 42)
        self.assertTrue(step["wait_recorded"])
        self.assertEqual(step["exec_s"], 100)
        self.assertFalse(step["failed"])
        self.assertEqual((step["call_id"], step["step_id"]), ("call-focused", "focused"))

    def test_anon_peaks_join_by_call_and_ignore_other_hosts_and_steps(self) -> None:
        self.append_peak("kept", 5 * GIB, 16)
        self.append_peak("other-host", 6 * GIB, 16, host="macbook")
        self.append_peak("other-step", 7 * GIB, 16, step="clippy")
        self.append_peak(None, 8 * GIB, 16)

        self.assertEqual(
            trial.anon_by_call(self.peaks, "natedev"),
            {"kept": [trial.AnonPeak(BASE + 1_000, 5 * GIB)]},
        )

    def test_sufficient_trial_arms_ignore_short_observational_arms(self) -> None:
        """F001: only registered trial arms control the minimum sample."""
        self.populate_balanced()
        self.insert_step("unset", BASE + 1_100, 0, "nextest", ["cargo", "nextest", "run"])
        self.insert_step(
            "narrow", BASE + 1_200, 0, "nextest",
            ["cargo", "nextest", "run", "-j", "8"],
        )

        output = self.report()

        self.assertRegex(output, r"(?m)^unset\s+1\s+0\s+0$")
        self.assertRegex(output, r"(?m)^8\s+1\s+0\s+0$")
        self.assertIn("sample: ok", output)

    def test_retried_call_pairs_each_attempt_with_its_peak(self) -> None:
        """F003: attempt and peak order disambiguates a shared call id."""
        call_id = "retried-call"
        self.insert_whole_hana(
            "first-attempt", BASE + 1_000, 16, 10, 100, True, 5 * GIB,
            call_id=call_id, peak_ended=BASE + 1_200,
        )
        self.insert_whole_hana(
            "second-attempt", BASE + 2_000, 16, 10, 100, False, 9 * GIB,
            call_id=call_id, peak_ended=BASE + 2_200,
        )

        output = self.report(blocks=1)
        peaks_by_step = trial.anon_by_step(
            trial.load_steps(self.index, BASE, BASE + trial.BLOCK_S, "natedev"),
            trial.anon_by_call(self.peaks, "natedev"),
        )

        self.assertEqual(peaks_by_step, {"first-attempt": 5 * GIB, "second-attempt": 9 * GIB})
        self.assertRegex(self.metric_line(output, "P whole-hana"), r"7\.00\s+—")

    def test_retried_call_with_missing_peak_excludes_every_attempt(self) -> None:
        """F003: unequal attempt and peak counts leave the call unpaired."""
        call_id = "ambiguous-call"
        self.insert_whole_hana(
            "first-attempt", BASE + 1_000, 16, 10, 100, True, 5 * GIB,
            call_id=call_id,
        )
        self.insert_whole_hana(
            "second-attempt", BASE + 2_000, 16, 10, 100, False, None,
            call_id=call_id,
        )

        output = self.report(blocks=1)

        self.assertRegex(self.metric_line(output, "P whole-hana"), r"—\s+—")

    def test_anon_peak_requires_a_text_end_time(self) -> None:
        """F006: the typed anon record boundary validates every consumed field."""
        self.append_peak("valid", 5 * GIB, 16)
        invalid = {
            "host": "natedev",
            "step": "nextest",
            "call_id": "invalid",
            "anon_peak_bytes": 6 * GIB,
            "ended_at": 123,
        }
        with self.peaks.open("a") as output:
            _ = output.write(json.dumps(invalid) + "\n")

        self.assertEqual(set(trial.anon_by_call(self.peaks, "natedev")), {"valid"})

    def test_scorecard_computes_every_registered_metric(self) -> None:
        self.populate_balanced()

        output = self.report()

        digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:12]
        self.assertRegex(output.splitlines()[0], rf"^Width trial natedev .* PDT .* \(50\.0 h\) · width_trial\.py sha256 {digest}$")
        self.assertRegex(output, r"(?m)^physical\s+15\s+60\s+60$")
        self.assertRegex(output, r"(?m)^logical\s+15\s+60\s+60$")
        self.assertIn("sample: ok", output)
        self.assertRegex(
            self.metric_line(output, "W memory wait"),
            r"10\.0\s+20\.0\s+0\.50\s+0\.50–0\.50.*proven$",
        )
        self.assertRegex(
            self.metric_line(output, "P whole-hana"),
            r"5\.00\s+10\.00\s+0\.50\s+0\.50–0\.50.*met$",
        )
        self.assertRegex(
            self.metric_line(output, "R gate reservation"),
            r"5\.00\s+10\.00\s+—\s+—.*met$",
        )
        self.assertRegex(
            self.metric_line(output, "T whole-hana"),
            r"90\s+100\s+0\.90\s+0\.90–0\.90.*pass$",
        )
        self.assertRegex(
            self.metric_line(output, "F whole-hana"),
            r"25\.0\s+50\.0\s+−25\.0pt\s+−25\.0–−25\.0pt.*pass$",
        )
        self.assertRegex(
            self.metric_line(output, "timed-out tests"),
            r"100\.0\s+100\.0$",
        )
        self.assertRegex(
            self.metric_line(output, "memory wait per gated"),
            r"10\.0\s+20\.0$",
        )
        self.assertIn("verdict: keep — W proven; T and F pass", output)

    def test_washout_applies_only_to_memory_wait_and_one_arm_has_no_verdict(self) -> None:
        self.insert_whole_hana("washout", BASE + 100, 16, 900, 1_000, False, 5 * GIB)
        self.insert_whole_hana("included", BASE + 1_000, 16, 100, 100, False, 5 * GIB)

        output = self.report(blocks=1)

        self.assertRegex(output, r"(?m)^physical\s+1\s+2\s+2$")
        self.assertRegex(self.metric_line(output, "W memory wait"), r"100\.0\s+—")
        self.assertRegex(self.metric_line(output, "T whole-hana"), r"550\s+—")
        self.assertIn("sample: short — physical has 2 whole-hana runs (< 60)", output)
        self.assertIn("verdict: no verdict — needs both arms", output)

    def test_formatting_step_does_not_change_gated_wait_mean(self) -> None:
        """F004: an exempt cargo fmt wait is absent from the gated-step row."""
        self.populate_balanced()
        self.insert_step("fmt", BASE + 1_500, 1_000, "fmt", ["cargo", "fmt"])

        output = self.report()

        self.assertRegex(self.metric_line(output, "memory wait per gated"), r"10\.0\s+20\.0$")

    def test_unknown_hana_wait_is_not_averaged_as_zero(self) -> None:
        """F004: a NULL wait counts as a run but not as a wait observation."""
        self.insert_whole_hana("known", BASE + 1_000, 16, 100, 100, False, 5 * GIB)
        self.insert_whole_hana("unknown", BASE + 2_000, 16, None, 100, False, 5 * GIB)

        output = self.report(blocks=1)

        self.assertRegex(output, r"(?m)^physical\s+1\s+2\s+2$")
        self.assertRegex(self.metric_line(output, "W memory wait"), r"100\.0\s+—")
        self.assertRegex(self.metric_line(output, "memory wait per gated"), r"100\.0$")

    def test_zero_logical_denominator_is_a_named_uncompared_metric(self) -> None:
        """F005: a zero denominator has a named state and prevents a verdict."""
        physical = [self.step(1, 16, 10.0)]
        logical = [self.step(2, 32, 0.0)]

        metric = trial.compare_metric(
            (physical, logical), self.mean_wait, self.ratio, random.Random(20261009)
        )

        self.assertIsInstance(metric, trial.UncomparedMetric)
        assert isinstance(metric, trial.UncomparedMetric)
        self.assertEqual(metric.reason, "zero denominator")
        compared = trial.ComparedMetric(1.0, 1.0, 1.0, trial.Interval(1.0, 1.0))
        self.assertEqual(trial.verdict(metric, compared, compared), "no verdict")

    def test_block_bootstrap_is_repeatable_for_the_registered_seed(self) -> None:
        physical = [self.step(block, 16, float(block + 1)) for block in range(4)]
        logical = [self.step(block, 32, float((block + 1) * 2)) for block in range(4, 8)]

        first = trial.block_bootstrap(
            (physical, logical), self.mean_wait, self.ratio, random.Random(20261009), n=100
        )
        second = trial.block_bootstrap(
            (physical, logical), self.mean_wait, self.ratio, random.Random(20261009), n=100
        )

        self.assertEqual(first, second)

    def test_each_verdict_branch_is_reported(self) -> None:
        cases = (
            VerdictCase("proven", 8.0, 90.0, 1, 2, "verdict: keep — W proven"),
            VerdictCase("partial", 15.0, 90.0, 1, 2, "verdict: keep — W partial"),
            VerdictCase("none", 20.0, 90.0, 1, 2, "verdict: revert — W none"),
            VerdictCase("failed T", 8.0, 120.0, 1, 2, "verdict: trade-off"),
            VerdictCase("failed F", 8.0, 90.0, 4, 0, "verdict: trade-off"),
        )
        for case in cases:
            with self.subTest(name=case.name):
                self.clear_records()
                self.populate_balanced(
                    physical_wait=case.physical_wait,
                    physical_exec=case.physical_exec,
                    physical_failures=case.physical_failures,
                    logical_failures=case.logical_failures,
                )
                output = self.report()
                self.assertIn(case.expected, output)
                if case.name == "failed T":
                    self.assertRegex(self.metric_line(output, "T whole-hana"), r"fail$")
                if case.name == "failed F":
                    self.assertRegex(self.metric_line(output, "F whole-hana"), r"fail$")

    def test_usage_error_exits_two(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False,
            env={**os.environ, "BUILDLOG_DIR": str(self.root)},
        )
        self.assertEqual(result.returncode, 2)

    @staticmethod
    def step(block: int, width: int, wait: float) -> trial.Step:
        return {
            "request": float(block * trial.BLOCK_S + 1_000),
            "ended": float(block * trial.BLOCK_S + 1_100),
            "block": block,
            "width": width,
            "whole_hana": True,
            "hana": True,
            "wait": wait,
            "wait_recorded": True,
            "exec_s": 100.0,
            "failed": False,
            "call_id": f"call-{block}",
            "step_id": f"step-{block}",
        }

    @staticmethod
    def mean_wait(steps: list[trial.Step]) -> float | None:
        return sum(step["wait"] for step in steps) / len(steps) if steps else None

    @staticmethod
    def ratio(physical: float, logical: float) -> float:
        return physical / logical


if __name__ == "__main__":
    _ = unittest.main()
