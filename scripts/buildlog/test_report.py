#!/usr/bin/env python3
"""Tests for report.py: a section per kind split by caller, then one summary row per kind."""

from __future__ import annotations

import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast, override
from unittest import mock

import disk
import ci
import index
import mac_outcome
import report
import holds
import health
from cgroup_test_support import GIB, slice_reading
from user_zone import user_zone
import rust_release
import sync
import store
from record import TARGET_DIR_UNSET
from test_holds import timed_hold_events, write_holder, write_mac_block
from test_index import COORDINATION_VARIABLES, STAMP, Record, call, ci_job, ci_run, encode, local_day, point_root_at, sample, step
from test_record_rerun import refresh_machine_health

HISTORY_STAMP = "2026-09-30T12:00:00Z"


def cargo_build(record_id: str, **fields: object) -> Record:
    return step(record_id, **{
        "step": "nextest",
        "argv": ["cargo", "nextest", "run", "--workspace", "--bin", "hana"],
        "duration_s": 20.0,
        "finished_s": 10.0,
        "crates_compiled": 1,
        **fields,
    })


class CliWork(TypedDict):
    cpu_seconds: float
    statements: int
    step_reads: int
    instructions: int
    index_updates: int
    launch_collections: int


@dataclass(frozen=True)
class MeasuredCliRun:
    result: subprocess.CompletedProcess[str]
    work: CliWork


def probe_cli() -> None:
    """Instrument the real CLI entry point, including its preparation work."""
    import cli
    import launches

    statements: list[str] = []
    instruction_batches = 0
    instruction_limit = int(os.environ["BUILDLOG_TEST_INSTRUCTION_LIMIT"])
    read_only = index.read_only

    def count_instructions() -> int:
        nonlocal instruction_batches
        instruction_batches += 1
        # Stop pathological plans by work count rather than waiting for a timeout.
        return int(instruction_batches * 1000 > instruction_limit)

    def traced_read_only() -> sqlite3.Connection:
        connection = read_only()
        connection.set_trace_callback(statements.append)
        connection.set_progress_handler(count_instructions, 1000)
        return connection

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    began = time.process_time()
    with mock.patch.object(index, "read_only", traced_read_only), mock.patch.object(index, "update", wraps=index.update) as updating, mock.patch.object(launches, "collect", wraps=launches.collect) as collecting:
        status = cli.main(sys.argv[1:])
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    work: CliWork = {
        "cpu_seconds": time.process_time() - began + after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
        "statements": len(statements),
        "step_reads": sum("JOIN steps" in sql or "FROM steps" in sql for sql in statements),
        "instructions": instruction_batches * 1000,
        "index_updates": updating.call_count,
        "launch_collections": collecting.call_count,
    }
    print("CLI_WORK=" + json.dumps(work), file=sys.stderr)
    raise SystemExit(status)


def measure_cli_work(command: str, environment: dict[str, str], row_count: int) -> MeasuredCliRun:
    # Importing test_index installs its own paths; restore this fixture afterwards.
    result = subprocess.run(
        [sys.executable, "-c", "import os; environment = dict(os.environ); from test_report import probe_cli; os.environ.update(environment); probe_cli()", command],
        cwd=Path(__file__).resolve().parents[2],
        env={**environment, "PYTHONPATH": str(Path(__file__).resolve().parent), "BUILDLOG_TEST_INSTRUCTION_LIMIT": str(row_count * 500)},
        capture_output=True, text=True, check=False, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"CLI failed: {result.stderr}\n{result.stdout}")
    metrics = next(line.removeprefix("CLI_WORK=") for line in result.stderr.splitlines() if line.startswith("CLI_WORK="))
    return MeasuredCliRun(result, cast(CliWork, json.loads(metrics)))


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.addModuleCleanup(time.tzset)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}))
    time.tzset()


class ReportSpeedTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {name: str(temporary / name) for name in COORDINATION_VARIABLES})))

    def test_report_command_from_a_temporary_log_shows_lead_and_targets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="buildlog-report-output-") as temporary:
            root = Path(temporary) / "buildlog"
            with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(root)}):
                path = root / "natedev" / "example.jsonl"
                path.parent.mkdir(parents=True)
                stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                _ = path.write_bytes(b"".join(encode(record) for record in (
                    call("passed", started_at=stamp, ended_at=stamp, mac="passed", mac_reason="",
                         mac_s=12, target="natedev", hold_wait_s=0.4, wall_s=30),
                    call("saved", started_at=stamp, outcome="reused", saved_s=120, hold_wait_s=0),
                    call("blocked", started_at=stamp, mac="declined", mac_reason="blocked",
                         mac_s=1, target="natedev", hold_wait_s=600, wall_s=40),
                    step("confirmation", started_at=stamp, ended_at=stamp, call_id="passed",
                         step="nextest", caller="verify", status=1, tests_flaky=2),
                )))
                _ = index.update()
                result = subprocess.run(
                    [sys.executable, "scripts/buildlog/cli.py", "report"],
                    cwd=Path(__file__).resolve().parents[2],
                    env={**os.environ, "TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles",
                         "BUILDLOG_TRANSCRIPTS": str(Path(temporary) / "transcripts")},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Mac when usable: 1 of 1", result.stdout)
                self.assertIn("| Held target | 10.0 min", result.stdout)
                self.assertIn("| Mac ran | Mac passed | Mac failed |", result.stdout)
                print(result.stdout, flush=True)

    def test_report_cli_work_grows_near_linearly_at_the_live_index_size(self) -> None:
        self.addCleanup(time.tzset)
        with tempfile.TemporaryDirectory(prefix="buildlog-report-") as temporary:
            root = Path(temporary) / "buildlog"
            transcripts = Path(temporary) / "transcripts"
            environment = {**os.environ, "BUILDLOG_DIR": str(root),
                           "BUILDLOG_TRANSCRIPTS": str(transcripts), "TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}
            with mock.patch.dict(os.environ, environment):
                time.tzset()
                midnight = datetime.combine(datetime.now().date(), datetime.min.time()).astimezone(UTC)
                path = root / "natedev" / "timing.jsonl"
                path.parent.mkdir(parents=True)
                with path.open("wb") as output:
                    for number in range(29_138):
                        at = midnight + timedelta(seconds=number * 2)
                        stamp = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                        record = call(f"call-{number}", started_at=stamp,
                                      ended_at=(at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                                      wall_s=1, package=f"package-{number % 20}",
                                      unit_session=f"session-{number % 20}", worker="writer",
                                      target="natedev", hold_wait_s=0.4 if number % 100 == 0 else 0,
                                      token_wait_s=0, build_s=1)
                        if number % 100 == 0:
                            record.update(mac="passed", mac_reason="", mac_s=1)
                        _ = output.write(encode(record))
                    for number in range(36_462):
                        call_number = number % 29_138
                        at = midnight + timedelta(seconds=call_number * 2)
                        _ = output.write(encode(step(
                            f"step-{number}", call_id=f"call-{call_number}",
                            started_at=at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                            ended_at=(at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                            caller="verify", step=report.KIND_ORDER[number % len(report.KIND_ORDER)],
                            tree_key=f"tree-{call_number // 100}", duration_s=1, finished_s=0.5,
                            crates_compiled=1, mem_wait_s=0, tests_flaky=0,
                            mem_stall_some_s=0.2 if number % 100 == 0 else 0,
                        )))
                    hold_rows = timed_hold_events(midnight, datetime.now(UTC))
                    for hold_row in hold_rows:
                        _ = output.write(encode(hold_row))
                self.assertEqual(index.update(), 29_138 + 36_462 + len(hold_rows))
                full_rows = path.read_bytes().splitlines(keepends=True)
                # Preserve the distribution of timestamps, kinds and Mac results.
                quarter_rows = full_rows[:29_138:4] + full_rows[29_138:65_600:4] + full_rows[65_600:]
                runs: list[list[MeasuredCliRun]] = []
                for rows in (quarter_rows, full_rows):
                    _ = path.write_bytes(b"".join(rows))
                    _ = index.reindex()
                    runs.append([measure_cli_work("report", environment, len(rows)) for _ in range(3)])
                for size, measurements in zip((len(quarter_rows), len(full_rows)), runs, strict=True):
                    for measured in measurements:
                        result = measured.result
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("### Tests by target", result.stdout)
                        self.assertIn("Now: Mac blocked by", result.stdout)
                        self.assertIn("Now: natedev held by", result.stdout)
                        self.assertEqual(measured.work["index_updates"], 1)
                        self.assertEqual(measured.work["launch_collections"], 1)
                        self.assertLessEqual(measured.work["statements"], 100)
                        self.assertLessEqual(measured.work["instructions"], size * 500)
                baseline = min(run.work["cpu_seconds"] for run in runs[0])
                full = min(run.work["cpu_seconds"] for run in runs[1])
                self.assertLessEqual(full, baseline * 8, f"CLI CPU grew from {baseline} to {full} for four times the rows")
        time.tzset()


def local_clock(at: str) -> str:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%H:%M %Z")


def token_holder(record_id: str, acquired_at: str, released_at: str, worker: str, unit_session: str) -> Record:
    started_at = "2026-10-01T12:00:00Z"
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    acquired = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
    return call(
        record_id,
        started_at=started_at,
        ended_at=released_at,
        wait_s=int((acquired - started).total_seconds()),
        worker=worker,
        unit_session=unit_session,
    )


class ReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    records: list[Record]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)
        for host in ("natedev", "Mac"):
            _ = refresh_machine_health(self, host, self.root, self.root)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": temporary}))
        _ = self.enterContext(mock.patch.object(subprocess, "run"))
        _ = self.enterContext(mock.patch.object(ci, "gh_get"))
        self.records = []

    def test_recorder_health_action_matches_unfinished_records_errors_or_both(self) -> None:
        day = "2026-10-02"
        at = datetime(2026, 10, 2, 16, tzinfo=UTC).timestamp()
        for machine, location in ((health.Machine.NATEDEV, "natedev"), (health.Machine.MAC, "the Mac")):
            for unfinished, errors in ((2, 0), (0, 3), (2, 3)):
                with self.subTest(machine=machine, unfinished=unfinished, errors=errors):
                    snapshot = health.FreshRecorderHealth(health.RecorderHealthSnapshot(
                        machine, at, unfinished, (at,) * errors))
                    with mock.patch.object(health, "read_snapshots", return_value=[snapshot]):
                        lines = report.recorder_health_lines(day)
                    self.assertEqual(len(lines), 1)
                    line = lines[0]
                    self.assertIn(f"{unfinished} records the recorder has not finished", line)
                    self.assertIn(f"{errors} recorder errors", line)
                    if unfinished:
                        self.assertIn(f"run buildlog launches on {location}", line)
                    else:
                        self.assertNotIn("run buildlog launches", line)
                    if errors:
                        self.assertIn("see ", line)
                        self.assertIn(f"errors.log on {location}", line)
                    else:
                        self.assertNotIn("see ", line)
                        self.assertNotIn("errors.log", line)

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def render(self, day: str | None = None) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.report(connection, day or local_day(STAMP))

    def poll_stamp(self, at: datetime, complete: bool = True) -> None:
        path = self.root / "ci" / "polled.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({ci.REPOS[0]: {"polled_at": at.astimezone(UTC).isoformat(), "complete": complete}}))

    def ci_line(self, day: str) -> str:
        return next(line for line in self.render(day).splitlines() if line.startswith("CI: "))

    def waiting_rows(self, day: str = "2026-10-02") -> dict[str, list[str]]:
        lines = self.render(day).splitlines()
        self.assertLess(lines.index("### Waiting"), lines.index("### Rebuilds"))
        start = lines.index("### Waiting")
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("### "))
        table_indexes = [i for i in range(start, end) if lines[i].startswith("|")]
        table = [lines[i] for i in table_indexes]
        self.assertEqual(
            [cell.strip() for cell in table[0].strip("|").split("|")],
            ["Wait", "Longest", "Over 5 min", "Waited", "Total", "Worst"],
        )
        self.assertEqual(
            lines[table_indexes[-1] + 1],
            "Source: verify.sh calls, memory-gated steps and CI jobs; a worker's own calls run one at a time, so waiting behind its own call adds no delay, behind another worker does.",
        )
        rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table[2:]]
        self.assertEqual(
            [row[0] for row in rows],
            [
                "Build-folder turn, behind another worker",
                "Build-folder turn, behind its own call",
                "Held target",
                "Memory admission",
                "CI queue",
            ],
        )
        return {row[0]: row[1:] for row in rows}

    def rebuilds_lines(self, day: str = "2026-10-02") -> list[str]:
        lines = self.render(day).splitlines()
        start = lines.index("### Rebuilds")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")), len(lines))
        return lines[start:end]

    def mac_tests_lines(self, day: str = "2026-10-02") -> list[str]:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.mac_tests_section(connection, day)

    def section_lines(self, text: str, heading: str) -> list[str]:
        lines = text.splitlines()
        start = lines.index(heading)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")), len(lines))
        return lines[start:end]

    def test_power_profile_section_follows_test_builds_in_full_report(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                   sample(STAMP, power_profile="performance", power_profile_since=STAMP))
        self.write(self.root / "natedev" / "2026-10.jsonl", call("measured", build_s=90))
        lines = self.render().splitlines()
        start = lines.index("### Test builds (temporary)")
        next_section = next(line for line in lines[start + 1:] if line.startswith("### "))
        self.assertEqual(next_section, "### Power profile")

    def test_mac_rows_leave_natedev_sections_and_failed_steps_unchanged(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("local", target="natedev"),
            self.nextest_step(
                "local-pass", "local", duration_s=20, finished_s=10,
                crates_compiled=1, mem_wait_s=5, mem_stall_some_s=2,
                tests_flaky=1, peak_mem_bytes=2**30,
            ),
            self.nextest_step(
                "local-failure", "local", duration_s=40, status=1, finished_s=20,
                crates_compiled=2, mem_wait_s=10, mem_stall_some_s=4,
                tests_flaky=2, peak_mem_bytes=2 * 2**30,
            ),
        )
        before = self.render()
        mac_host = "Nates-MacBook-Pro-2023"
        self.write(
            self.root / mac_host / "2026-10.jsonl",
            step("offload", host=mac_host, caller="verify-mac", step="nextest",
                 duration_s=100, status=1, finished_s=90, crates_compiled=50,
                 mem_wait_s=600, mem_stall_some_s=900, tests_flaky=20, peak_mem_bytes=99 * 2**30,
                 slice="fallback"),
            step("direct", host=mac_host, caller="unknown", step="nextest",
                 duration_s=200, status=1, finished_s=180, crates_compiled=50,
                 mem_wait_s=900, mem_stall_some_s=1000, tests_flaky=30, peak_mem_bytes=100 * 2**30,
                 slice="none"),
            step("mac-kind", host=mac_host, caller="unknown", step="sweep", status=1),
        )
        after = self.render()
        for heading in (
            "### Waiting", "### Rebuilds", "### nextest", "### Memory pressure",
            "### Summary: successes", "### Summary: failures", "### Summary: all",
        ):
            with self.subTest(heading=heading):
                self.assertEqual(self.section_lines(after, heading), self.section_lines(before, heading))
        self.assertIn("Failed steps: 1; flaky tests: 3.", after)
        self.assertNotIn("### sweep", after)
        self.assertNotIn("(natedev)", after)
        mac = self.section_lines(after, "### Mac")
        self.assertTrue(any(line.startswith("| verify.sh offload | nextest | 1 | 1 |") for line in mac))
        self.assertTrue(any(line.startswith("| unknown | nextest | 1 | 1 |") for line in mac))
        self.assertTrue(any(line.startswith("| unknown | sweep | 1 | 1 |") for line in mac))
        _ = index.update()
        with closing(index.read_only()) as connection:
            self.assertEqual(report.kinds(connection, local_day(STAMP)), ["nextest"])

    def test_mac_table_groups_callers_and_kinds_with_counts_times_and_nextest_facts(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("routed", mac="passed", mac_s=30, target="mac"),
            step("local", step="nextest", caller="unknown", duration_s=900, tests_run=900),
        )
        mac_host = "Nates-MacBook-Pro-2023"
        self.write(
            self.root / mac_host / "2026-10.jsonl",
            step("direct", host=mac_host, step="nextest", caller="unknown", duration_s=50,
                 status=1, finished_s=25, tests_run=5, tests_failed=1),
            step("sweep", host=mac_host, step="sweep", caller="verify-mac", duration_s=8),
            step("pass", host=mac_host, step="nextest", caller="verify-mac", duration_s=10,
                 finished_s=5, tests_run=0, tests_failed=0),
            step("no-tests", host=mac_host, step="nextest", caller="verify-mac", duration_s=20,
                 status=4, finished_s=5, tests_run=0, tests_failed=0),
            step("failed", host=mac_host, step="nextest", caller="verify-mac", duration_s=30,
                 status=1, finished_s=5, tests_run=0, tests_failed=0),
            step("other-day", host=mac_host, step="nextest", caller="verify-mac", duration_s=900,
                 started_at="2026-10-01T12:00:00Z"),
        )
        self.write(
            self.root / "Mac" / "2026-10.jsonl",
            step("other-mac", host="Mac", step="doc", caller="agent", duration_s=6),
        )
        text = self.render()
        mac = self.section_lines(text, "### Mac")
        self.assertIn("| Caller | Kind | Runs | Failed | Avg | p95 | Range | Build | Tests | Tests failed |", mac)
        rows = [line for line in mac if line.startswith("| ")][1:]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], "| verify.sh offload | nextest | 3 | 1 | 20.0 s | 30.0 s | 10.0 s – 30.0 s | 5.0 s | 0 | 0 |")
        self.assertIn("| unknown | nextest | 1 | 1 | 50.0 s | 50.0 s | 50.0 s | 25.0 s | 5 | 1 |", rows)
        self.assertIn("| verify.sh offload | sweep | 1 | 0 | 8.0 s | 8.0 s | 8.0 s | — | — | — |", rows)
        self.assertIn("| agent (direct) | doc | 1 | 0 | 6.0 s | 6.0 s | 6.0 s | — | — | — |", rows)
        self.assertIn("Mac total: 6 steps, 2.1 min; 2 failed.", mac)
        self.assertIn(
            "Source: steps recorded on the Mac; failed follows the build log's failure rule, so a Mac nextest that matched no tests is not a failure.",
            mac,
        )
        lines = text.splitlines()
        targets = self.section_lines(text, "### Tests by target")
        self.assertEqual(lines[lines.index("### Tests by target") + len(targets)], "### Mac")
        _ = index.update()
        with closing(index.read_only()) as connection:
            self.assertEqual(report.mac_section(connection, local_day(STAMP)) + ["Mac copies: none started.", ""], mac)
            nextest_runs = cast(int, connection.execute(
                "SELECT count(*) FROM steps WHERE host <> 'natedev' AND step = 'nextest'"
                + " AND date(started_at, 'localtime') = ?", (local_day(STAMP),),
            ).fetchone()[0])
            self.assertEqual(sum(int(row.split("|")[3]) for row in rows if "| nextest |" in row), nextest_runs)

    def test_mac_no_test_match_is_not_failed_but_other_callers_status_four_is(self) -> None:
        self.write(
            self.root / "Mac" / "2026-10.jsonl",
            step("offload", host="Mac", caller="verify-mac", step="nextest", status=4, duration_s=1),
            step("direct", host="Mac", caller="unknown", step="nextest", status=4, duration_s=1),
            step("other-kind", host="Mac", caller="verify-mac", step="sweep", status=4, duration_s=1),
        )
        text = self.render()
        mac = self.section_lines(text, "### Mac")
        self.assertTrue(any(line.startswith("| verify.sh offload | nextest | 1 | 0 |") for line in mac))
        self.assertTrue(any(line.startswith("| unknown | nextest | 1 | 1 |") for line in mac))
        self.assertTrue(any(line.startswith("| verify.sh offload | sweep | 1 | 1 |") for line in mac))
        self.assertIn("Mac total: 3 steps, 3.0 s; 2 failed.", mac)
        self.assertIn("No build steps recorded.", text)
        self.assertNotIn("Failed steps:", text)

    def test_day_without_mac_steps_has_the_empty_mac_section(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("local", caller="verify-mac", step="nextest"),
        )
        self.write(
            self.root / "Mac" / "2026-10.jsonl",
            step("other-day", host="Mac", started_at="2026-10-01T12:00:00Z"),
        )
        text = self.render()
        self.assertEqual(
            self.section_lines(text, "### Mac"),
            ["### Mac", "", "No steps recorded on the Mac.", "", "Mac copies: none started.", ""],
        )
        self.assertIn("### nextest", text)

    def target_cells(self, package: str = "hana", day: str = "2026-10-02") -> dict[str, str]:
        lines = self.mac_tests_lines(day)
        heading = next(line for line in lines if line.startswith("| Package |"))
        row = next(line for line in lines if line.startswith(f"| {package} |"))
        return self.table_cells(heading, [row])[0]

    def test_held_target_waits_keep_fractional_seconds_and_unknown_rows(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("short", hold_wait_s=0.4, worktree="/r/short"),
            call("long", hold_wait_s=600.0, worktree="/r/long"),
            call("zero", hold_wait_s=0.0), call("old"),
            call("other-day", started_at="2026-10-01T12:00:00Z", hold_wait_s=7200.0),
        )
        row = self.waiting_rows()["Held target"]
        self.assertEqual(row, [
            f"10.0 min (long, {local_clock(STAMP)})", "1",
            "2 of 3 calls; not recorded for 1 call", "0.2 worker-hours",
            "long 10.0 min, short 0.4 s",
        ])

    def test_held_target_waits_from_old_calls_are_not_zero(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl", call("old"))
        row = self.waiting_rows()["Held target"]
        self.assertEqual(row[:4], ["not recorded"] * 4)

    def test_only_local_package_and_missing_target_are_reported(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("local-only", package="local-only", wall_s=90),
            self.nextest_step("local-step", "local-only"),
        )
        cells = self.target_cells("local-only")
        self.assertEqual(cells["Mac ran"], "0")
        self.assertEqual(cells["natedev ran"], "1")
        self.assertEqual(cells["natedev p50"], "1.5 min")
        self.assertEqual(cells["Did not reach the Mac"], "no Mac entry 1")
        self.assertIn("Target not recorded for 1 call counted above.", self.mac_tests_lines())

    def test_missing_targets_are_reported_below_the_table_without_changing_cells(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("old-pass", mac="passed", mac_reason="", mac_s=10),
            call("old-busy", mac="declined", mac_reason="busy", mac_s=1),
            call("empty-target", mac="passed", mac_reason="", mac_s=10, target=""),
            call("hidden-off", package="hidden", mac="declined", mac_reason="off", mac_s=0),
            call("cache", outcome="reused", mac="passed", mac_reason="", mac_s=10),
            call("next-day", started_at="2026-10-03T12:00:00Z", mac="passed", mac_reason="", mac_s=10),
        )
        self.assertEqual(self.target_cells()["Did not reach the Mac"], "busy with our test 1")
        section = self.mac_tests_lines()
        note = "Target not recorded for 2 calls counted above."
        self.assertIn(note, section)
        last_row = max(number for number, line in enumerate(section) if line.startswith("|"))
        self.assertEqual(section.index(note), last_row + 1)
        self.assertFalse(any("not recorded" in line for line in section if line.startswith("|")))

    def test_recorded_empty_target_needs_no_missing_target_note(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl",
                   call("passed", mac="passed", mac_reason="", mac_s=1, target=""))
        self.assertFalse(any("not recorded" in line for line in self.mac_tests_lines()))

    def test_unrecorded_local_duration_is_left_out_of_the_median(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("unknown", wall_s=None), call("known", wall_s=90),
            self.nextest_step("unknown-step", "unknown"), self.nextest_step("known-step", "known"),
        )
        cells = self.target_cells()
        self.assertEqual(cells["natedev ran"], "2")
        self.assertEqual(cells["natedev p50"], "1.5 min")

    def test_two_local_steps_for_one_call_count_one_execution(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("local", target="natedev"),
            self.nextest_step("first", "local"), self.nextest_step("second", "local"),
            self.nextest_step("mac", "local", caller="verify-mac", host="Mac"),
        )
        cells = self.target_cells()
        self.assertEqual(cells["natedev ran"], "1")
        self.assertEqual(cells["Mac ran"], "0")

    def test_target_alone_counts_no_execution_or_decline(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("interrupted", outcome="interrupted", status=130, target="mac", mac="", mac_s=30),
        )
        self.assertEqual(self.mac_tests_lines(), [])

    def test_interrupted_retry_keeps_decline_and_has_no_mac_time(self) -> None:
        for reason in ("busy", "blocked"):
            with self.subTest(reason=reason):
                self.write(
                    self.root / "natedev" / "2026-10.jsonl",
                    call("retry", outcome="interrupted", status=130, target="mac",
                         mac="declined", mac_reason=reason, mac_s=0.4),
                )
                cells = self.target_cells()
                self.assertEqual(cells["Mac ran"], "0")
                self.assertEqual(cells["natedev ran"], "0")
                self.assertEqual(cells["Mac p50"], "")
                self.assertIn(f"{report.DECLINE_LABELS[cast(mac_outcome.OffloadDeclineReason, reason)]} 1", cells["Did not reach the Mac"])

    def test_mac_pass_counts_when_local_confirmation_is_interrupted(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("confirmed", outcome="interrupted", status=130, target="natedev",
                 mac="passed", mac_reason="", mac_s=10, wall_s=60),
            self.nextest_step("confirmation", "confirmed", status=130),
        )
        cells = self.target_cells()
        self.assertEqual(cells["Mac ran"], "1")
        self.assertEqual(cells["Mac passed"], "1")
        self.assertEqual(cells["Mac failed"], "0")
        self.assertEqual(cells["natedev ran"], "1")

    def test_reused_call_counts_no_execution_even_with_a_target(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("cached", outcome="reused", target="natedev"),
            self.nextest_step("cached-step", "cached"),
        )
        self.assertEqual(self.mac_tests_lines(), [])

    def test_repository_and_linux_declines_need_no_mac_seconds(self) -> None:
        for reason in ("repo", "linux_only"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    mac_outcome.offload_record("declined", reason, reason, None),
                    mac_outcome.OffloadDeclined(cast(mac_outcome.OffloadDeclineReason, reason), 0.0),
                )

    def test_day_ranges_match_sqlite_on_each_side_of_local_midnight(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}):
            time.tzset()
            before = "2026-10-03T06:59:59.999Z"
            after = "2026-10-03T07:00:00.000Z"
            self.write(
                self.root / "natedev" / "2026-10.jsonl",
                call("before", started_at=before, mac="passed", mac_reason="", mac_s=1),
                call("after", started_at=after, mac="passed", mac_reason="", mac_s=1),
                self.nextest_step("before-step", "before", started_at=after),
                self.nextest_step("after-step", "after", started_at=after),
            )
            _ = index.update()
            with closing(index.read_only()) as connection:
                for day in ("2026-10-02", "2026-10-03"):
                    expected = report.fetch(connection,
                        "SELECT id FROM calls WHERE date(started_at, 'localtime') = ? ORDER BY id", day)
                    selected = report.reported_mac_calls(connection, day)
                    self.assertEqual(sorted(call.call_id for call in selected if call.on_report_day), [row[0] for row in expected])
                    self.assertTrue(all(call.ran_on_natedev for call in selected))
        time.tzset()

    def test_lead_block_has_all_five_facts_before_waiting(self) -> None:
        write_mac_block(datetime(2026, 10, 12, 15, tzinfo=UTC), "mac-holder", "preview")
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("passed", mac="passed", mac_reason="", mac_s=10, hold_wait_s=600),
            call("saved", outcome="reused", saved_s=120),
            call("blocked", started_at="2026-10-02T12:10:00Z", mac="declined", mac_reason="blocked", mac_s=1),
            self.nextest_step("failed-step", "passed", status=1, tests_flaky=2),
        )
        lines = self.render().splitlines()
        lead = [line for line in lines[2:lines.index("### Waiting")] if line]
        self.assertEqual(lead, [
            "Now: Mac blocked by mac-holder for preview since 2026-10-12 08:00 PDT",
            "Mac when usable: 1 of 1 candidates finished there (100%).",
            "Longest wait: 10.0 min for Held target.",
            "Failed steps: 1; flaky tests: 2.", "Pass records saved: 2.0 min.",
        ])

    def test_earlier_day_names_the_hold_in_force_now_and_that_days_unreachable_call(self) -> None:
        now = datetime(2026, 10, 12, 15, tzinfo=UTC)
        write_holder("session", now, "renderer", "screenshot")
        write_mac_block(now, "mac-holder", "preview")
        self.write(self.root / "natedev" / "2026-10.jsonl",
                   call("old-unreachable", mac="declined", mac_reason="unreachable", mac_s=1))
        output = self.render("2026-10-02")
        self.assertIn("Now: natedev held by renderer for screenshot since 2026-10-12 08:00 PDT", output)
        self.assertIn("Now: Mac blocked by mac-holder for preview since 2026-10-12 08:00 PDT", output)
        self.assertIn("Mac unreachable as of 2026-10-02 05:00 PDT", output)
        self.assertLess(output.index("Now: Mac"), output.index("### Waiting"))

    def test_a_historical_blocked_call_does_not_claim_a_current_block(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl",
                   call("old-block", mac="declined", mac_reason="blocked", mac_s=1))
        output = self.render()
        self.assertNotIn("Mac blocked as of", output)
        self.assertNotIn("Now: Mac blocked", output)

    def test_todays_unreachable_call_keeps_the_short_time(self) -> None:
        now = datetime.now(UTC)
        stamp = now.isoformat()
        self.write(self.root / "natedev" / "2026-10.jsonl",
                   call("today-unreachable", started_at=stamp, ended_at=stamp,
                        mac="declined", mac_reason="unreachable", mac_s=1))
        output = self.render(now.astimezone(user_zone()).date().isoformat())
        self.assertIn(f"Mac unreachable as of {now.astimezone(user_zone()):%H:%M}", output.splitlines())

    def test_lead_block_uses_latest_candidate_and_groups_backoff_with_unreachable(self) -> None:
        for reason in ("backoff", "unreachable"):
            with self.subTest(reason=reason):
                self.write(
                    self.root / "natedev" / "2026-10.jsonl",
                    call("declined", mac="declined", mac_reason=reason, mac_s=1),
                    call("local", started_at="2026-10-02T12:10:00Z", mac="declined", mac_reason="local_flag"),
                    call("linux", started_at="2026-10-02T12:20:00Z", mac="declined", mac_reason="linux_only"),
                )
                self.assertIn("Mac unreachable as of " + holds.shown_time(datetime.fromisoformat(STAMP)), self.render())
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("blocked", mac="declined", mac_reason="blocked", mac_s=1),
            call("passed", started_at="2026-10-02T12:10:00Z", mac="passed", mac_reason="", mac_s=1),
        )
        self.assertNotIn("Mac blocked as of", self.render())

    def test_lead_block_uses_the_waiting_rows_names_and_durations(self) -> None:
        cases = ((900, 600, False, "Memory admission", 900),
                 (100, 600, False, "Build-folder turn, behind another worker", 600),
                 (100, 600, True, "Build-folder turn, behind another worker", 300))
        for memory_wait, token_wait, shares_own_turn, name, longest in cases:
            with self.subTest(name=name, longest=longest):
                records = [
                    call("waiter", started_at="2026-10-02T12:00:00Z", token_wait_s=token_wait,
                         wait_s=600, unit_session="group" if shares_own_turn else None, worker="writer"),
                    step("memory", mem_wait_s=memory_wait),
                ]
                if shares_own_turn:
                    records.append(token_holder("holder", "2026-10-02T12:00:00Z",
                                                "2026-10-02T12:05:00Z", "writer", "group"))
                self.write(self.root / "natedev" / "2026-10.jsonl", *records)
                self.assertIn(f"Longest wait: {report.seconds(longest)} for {name}.", self.render())
                self.assertTrue(self.waiting_rows()[name][0].startswith(report.seconds(longest) + " ("))

    def test_lead_block_names_a_wait_behind_its_own_call(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=STAMP, token_wait_s=600, wait_s=600,
                 unit_session="group", worker="writer"),
            token_holder("holder", STAMP, "2026-10-02T12:10:00Z", "writer", "group"),
        )
        name = "Build-folder turn, behind its own call"
        self.assertIn(f"Longest wait: 10.0 min for {name}.", self.render())
        self.assertTrue(self.waiting_rows()[name][0].startswith("10.0 min ("))

    def test_lead_block_omits_longest_wait_when_no_row_waited(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl", call("no-wait", hold_wait_s=0))
        self.assertNotIn("Longest wait:", self.render())

    def test_lead_block_includes_the_ci_queue_when_it_is_the_longest_row(self) -> None:
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", (
                STAMP, "2026-10-02T12:10:00Z", "2026-10-02T12:11:00Z",
            ))]),
        )
        self.assertIn("Longest wait: 10.0 min for CI queue.", self.render())
        self.assertTrue(self.waiting_rows()["CI queue"][0].startswith("10.0 min ("))

    def test_tree_history_uses_step_time_and_id_when_records_arrive_out_of_order(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("history"),
            step("last", call_id="history", tree_key="last-tree", started_at="2026-10-02T12:10:00Z"),
            step("b", call_id="history", tree_key="second-tree", started_at=STAMP),
            step("a", call_id="history", tree_key="first-tree", started_at=STAMP),
        )
        _ = index.update()
        with closing(index.read_only()) as connection:
            trees = report.call_trees(connection, "2026-10-02")
        self.assertEqual(trees["history"], report.KnownCallTrees("first-tree", "last-tree"))

    def nextest_step(
        self,
        record_id: str,
        call_id: str,
        caller: str = "verify",
        **fields: object,
    ) -> Record:
        return step(
            record_id,
            call_id=call_id,
            step="nextest",
            caller=caller,
            argv=["cargo", "nextest", "run", "-E", "package(hana)"],
            **fields,
        )

    def test_waiting_section_shows_each_tail_and_top_three(self) -> None:
        at = "2026-10-02T12:00:00Z"
        calls = [
            call(f"call-{name}-{wait}", started_at=at, worktree=f"/r/{name}", token_wait_s=wait)
            for name, wait in (("alpha", 600), ("alpha", 360), ("beta", 420), ("gamma", 240), ("delta", 0))
        ]
        calls.append(call("other-day", started_at="2026-10-01T12:00:00Z", token_wait_s=7200))
        steps = [
            step(f"step-{name}", started_at=at, worktree=f"/r/{name}", worker=worker, mem_wait_s=wait)
            for name, worker, wait in (("alpha", "impl", 900), ("beta", "test", 600), ("gamma", None, 360), ("delta", None, 0))
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *calls, *steps)
        jobs = [
            ci_job(number, name, "success", (f"2026-10-02T12:{minute:02d}:00Z", f"2026-10-02T12:{minute + wait // 60:02d}:00Z", f"2026-10-02T12:{minute + wait // 60 + 1:02d}:00Z"))
            for number, name, minute, wait in ((1, "Alpha", 0, 600), (2, "Beta", 12, 420), (3, "Gamma", 22, 360), (4, "Zero", 35, 0))
        ]
        jobs.append(ci_job(5, "Unknown", "success", ("2026-10-02T12:40:00Z", "unknown", "unknown")))
        first = ci_run(1, 1, jobs)
        carried = ci_run(2, 2, [ci_job(21, "Carried", "success", (
            "2026-10-02T13:00:00Z", "2026-10-02T12:10:00Z", "2026-10-02T12:11:00Z",
        ))])
        carried.update({"created_at": "2026-10-02T13:00:00Z", "started_at": "2026-10-02T13:00:00Z", "updated_at": "2026-10-02T13:10:00Z"})
        self.write(self.root / "ci" / "2026-10.jsonl", first, carried)

        rows = self.waiting_rows()
        zone = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%H:%M %Z")
        self.assertEqual(rows["Build-folder turn, behind another worker"], [
            f"10.0 min (alpha, {zone})", "3", "4 of 5 calls", "0.5 worker-hours",
            "alpha 16.0 min, beta 7.0 min, gamma 4.0 min",
        ])
        self.assertEqual(rows["Build-folder turn, behind its own call"], [
            "none", "0", "0 of 5 calls", "0.0 worker-hours", "",
        ])
        self.assertEqual(rows["Memory admission"], [
            f"15.0 min (alpha impl, {zone})", "3", "3 of 4 steps", "0.5 worker-hours",
            "alpha 15.0 min, beta 10.0 min, gamma 6.0 min",
        ])
        self.assertEqual(rows["CI queue"], [
            f"10.0 min (CI / Alpha, {zone})", "3", "3 of 4 jobs", "0.4 job-hours",
            "CI / Alpha 10.0 min, CI / Beta 7.0 min, CI / Gamma 6.0 min",
        ])

    def test_waiting_rows_remain_visible_when_no_wait_was_recorded(self) -> None:
        rows = self.waiting_rows()
        self.assertEqual(rows, {name: ["none", "", "", "", ""] for name in
                                ("Build-folder turn, behind another worker", "Build-folder turn, behind its own call",
                                 "Held target", "Memory admission", "CI queue")})

    def test_build_folder_wait_fully_behind_own_call_from_previous_day(self) -> None:
        at = "2026-10-02T12:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T12:11:00Z",
                 wait_s=600, token_wait_s=600, worktree="/r/alpha", worker="impl", unit_session="unit-a"),
            token_holder("holder", "2026-10-02T12:00:00Z", "2026-10-02T12:10:00Z", "impl", "unit-a"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind its own call"][0], f"10.0 min (alpha, {local_clock(at)})")
        self.assertEqual(
            rows["Build-folder turn, behind its own call"][2:],
            ["1 of 1 calls", "0.2 worker-hours", "alpha 10.0 min"],
        )
        self.assertEqual(rows["Build-folder turn, behind another worker"][2], "0 of 1 calls")

    def test_build_folder_wait_fully_behind_another_worker(self) -> None:
        at = "2026-10-02T13:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T13:08:00Z",
                 wait_s=420, token_wait_s=420, worktree="/r/beta", worker="impl", unit_session="unit-a"),
            token_holder("holder", "2026-10-02T13:00:00Z", "2026-10-02T13:07:00Z", "test", "unit-a"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind another worker"][0], f"7.0 min (beta, {local_clock(at)})")
        self.assertEqual(
            rows["Build-folder turn, behind another worker"][2:],
            ["1 of 1 calls", "0.1 worker-hours", "beta 7.0 min"],
        )
        self.assertEqual(rows["Build-folder turn, behind its own call"][2], "0 of 1 calls")

    def test_build_folder_wait_splits_across_own_call_and_another_worker(self) -> None:
        at = "2026-10-02T14:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T14:11:00Z",
                 wait_s=600, token_wait_s=600, worktree="/r/gamma", worker="impl", unit_session="unit-a"),
            token_holder("own", "2026-10-02T14:00:00Z", "2026-10-02T14:04:00Z", "impl", "unit-a"),
            token_holder("other", "2026-10-02T14:04:00Z", "2026-10-02T14:10:00Z", "test", "unit-a"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind another worker"][0], f"6.0 min (gamma, {local_clock(at)})")
        self.assertEqual(rows["Build-folder turn, behind its own call"][0], f"4.0 min (gamma, {local_clock(at)})")
        self.assertEqual(rows["Build-folder turn, behind another worker"][2], "1 of 1 calls")
        self.assertEqual(rows["Build-folder turn, behind its own call"][2], "1 of 1 calls")

    def test_uncovered_build_folder_wait_counts_as_behind_another_worker(self) -> None:
        at = "2026-10-02T15:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T15:11:00Z",
                 wait_s=600, token_wait_s=600, worktree="/r/delta", worker="impl", unit_session="unit-a"),
            token_holder("own", "2026-10-02T15:00:00Z", "2026-10-02T15:02:00Z", "impl", "unit-a"),
            token_holder("other", "2026-10-02T15:02:00Z", "2026-10-02T15:05:00Z", "test", "unit-a"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind another worker"][0], f"8.0 min (delta, {local_clock(at)})")
        self.assertEqual(rows["Build-folder turn, behind its own call"][0], f"2.0 min (delta, {local_clock(at)})")

    def test_build_folder_wait_without_unit_session_counts_as_another_worker(self) -> None:
        at = "2026-10-02T16:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T16:06:00Z",
                 wait_s=300, token_wait_s=300, worktree="/r/epsilon", worker="impl"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind another worker"][0], f"5.0 min (epsilon, {local_clock(at)})")
        self.assertEqual(rows["Build-folder turn, behind its own call"][2], "0 of 1 calls")

    def test_port_lint_calls_neither_count_as_build_folder_calls_nor_hold_the_folder(self) -> None:
        at = "2026-10-02T18:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T18:06:00Z",
                 wait_s=300, token_wait_s=300, worktree="/r/eta", worker="impl", unit_session="unit-a"),
            call("lint", tool="port-lint", started_at=at, ended_at="2026-10-02T18:05:00Z",
                 worktree="/r/eta", worker="impl", unit_session="unit-a"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind another worker"][0], f"5.0 min (eta, {local_clock(at)})")
        self.assertEqual(rows["Build-folder turn, behind another worker"][2], "1 of 1 calls")
        self.assertEqual(rows["Build-folder turn, behind its own call"][2], "0 of 1 calls")

    def test_holder_from_another_unit_session_is_ignored(self) -> None:
        at = "2026-10-02T17:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("waiter", started_at=at, ended_at="2026-10-02T17:06:00Z",
                 wait_s=300, token_wait_s=300, worktree="/r/zeta", worker="impl", unit_session="unit-a"),
            token_holder("unrelated", "2026-10-02T17:00:00Z", "2026-10-02T17:05:00Z", "impl", "unit-b"),
        )

        rows = self.waiting_rows()
        self.assertEqual(rows["Build-folder turn, behind another worker"][0], f"5.0 min (zeta, {local_clock(at)})")
        self.assertEqual(rows["Build-folder turn, behind its own call"][2], "0 of 1 calls")

    def test_rebuild_bins_include_edges_and_exclude_unmeasured_steps(self) -> None:
        at = "2026-10-02T12:00:00Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("none-over", started_at=at, duration_s=5.0, finished_s=10.0, crates_compiled=0),
            step("none-zero", started_at=at, duration_s=15.0, finished_s=0.0, crates_compiled=0),
            step("edited-low", started_at=at, duration_s=10.0, finished_s=5.0, crates_compiled=1),
            step("edited-high", started_at=at, duration_s=30.0, finished_s=15.0, crates_compiled=3),
            step("cascade-low", started_at=at, duration_s=20.0, finished_s=10.0, crates_compiled=4),
            step("cascade-high", started_at=at, duration_s=40.0, finished_s=20.0, crates_compiled=49),
            step("cold", started_at=at, duration_s=90.0, finished_s=40.0, crates_compiled=50),
            step("sweep", started_at=at, step="sweep", duration_s=1000.0, finished_s=1000.0, crates_compiled=100),
            step("unknown-count", started_at=at, duration_s=1000.0, finished_s=1000.0, crates_compiled=None),
            step(
                "other-day",
                started_at="2026-10-01T12:00:00Z",
                duration_s=1000.0,
                finished_s=1000.0,
                crates_compiled=100,
            ),
        )

        all_lines = self.render().splitlines()
        waiting = all_lines.index("### Waiting")
        next_heading = next(line for line in all_lines[waiting + 1:] if line.startswith("### "))
        self.assertEqual(next_heading, "### Rebuilds")
        section = self.rebuilds_lines()
        self.assertEqual(section[2], "| Rebuild | Steps | Compile | Other | Total | Share of compile | Share of time |")
        self.assertIn("| none | 2 | 10.0 s | 15.0 s | 20.0 s | 10% | 10% |", section)
        self.assertIn("| edited crate (1–3 crates) | 2 | 20.0 s | 20.0 s | 40.0 s | 20% | 19% |", section)
        self.assertIn("| cascade (4–49 crates) | 2 | 30.0 s | 30.0 s | 1.0 min | 30% | 29% |", section)
        self.assertIn("| cold (50+ crates) | 1 | 40.0 s | 50.0 s | 1.5 min | 40% | 43% |", section)
        self.assertFalse(any(line.startswith("nextest:") for line in section))
        self.assertNotIn("Edited-crate rebuilds by package (nextest):", section)
        source = next(line for line in section if line.startswith("Source:"))
        self.assertIn("steps with a known crate count", source)
        self.assertIn("compile is cargo's own \"Finished … in\" time, other is the rest of the step", source)
        for requirement in ("folder", "same target", "same features", "before"):
            self.assertIn(requirement, source)

    def test_first_build_row_includes_one_to_forty_nine_crates_but_zero_and_cold_keep_their_bins(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            *(cargo_build(f"first-{crates}", target_dir=f"/fresh/{crates}", crates_compiled=crates)
              for crates in (0, 1, 3, 4, 49, 50)),
        )
        section = self.rebuilds_lines()
        for expected in (
            "| none | 1 |", "| first build here (1–49 crates) | 4 |", "| cold (50+ crates) | 1 |",
            "| edited crate (1–3 crates) | 0 |", "| cascade (4–49 crates) | 0 |",
        ):
            self.assertTrue(any(line.startswith(expected) for line in section), section)
        self.assertNotIn("Edited-crate rebuilds by package (nextest):", section)

    def test_finished_history_in_an_earlier_month_warms_the_same_target_folder_across_worktrees(self) -> None:
        self.write(
            self.root / "natedev" / "2026-09.jsonl",
            cargo_build("previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                        target_dir="/shared/target", worktree="/old/checkout", finished_s=0.0, status=1,
                        argv=["cargo", "test", "--workspace", "--bin", "hana", "--features", "trace,hana/dynamic_linking"]),
        )
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            cargo_build("warm", target_dir="/shared/target", worktree="/new/checkout", branch="new", sha="newsha",
                        argv=["cargo", "nextest", "run", "--all", "--bin=hana", "-F", "hana/dynamic_linking trace"]),
        )
        section = self.rebuilds_lines()
        self.assertIn("| edited crate (1–3 crates) | 1 | 10.0 s | 10.0 s | 20.0 s | 100% | 100% |", section)
        self.assertIn("| first build here (1–49 crates) | 0 | — | — | — | — | — |", section)

    def test_other_folder_host_features_profile_or_unfinished_history_cannot_make_a_build_warm(self) -> None:
        command = ["cargo", "nextest", "run", "--workspace", "--bin", "hana"]
        cases: tuple[tuple[str, Record], ...] = (
            ("other folder", {"target_dir": "/different/target"}),
            ("other host", {"host": "Mac"}),
            ("other features", {"argv": [*command, "--features", "hana/dynamic_linking"]}),
            ("other profile", {"argv": [*command, "--release"]}),
            ("other target", {"argv": [*command, "--target", "aarch64-unknown-linux-gnu"]}),
            ("other package scope", {"argv": ["cargo", "nextest", "run", "-p", "hana", "--bin", "hana"]}),
            ("other command", {"argv": ["cargo", "check", "--workspace", "--bin", "hana"]}),
            ("unfinished", {"finished_s": None}),
            ("same timestamp", {"started_at": STAMP, "ended_at": STAMP}),
            ("future", {"started_at": "2026-10-03T12:00:00Z", "ended_at": "2026-10-03T12:00:00Z"}),
            ("non cargo", {"argv": ["python3", "checker.py", "--workspace", "--bin", "hana"]}),
        )
        for name, changes in cases:
            with self.subTest(history=name):
                previous = cargo_build("previous", **{
                    "started_at": HISTORY_STAMP, "ended_at": HISTORY_STAMP,
                    "target_dir": "/shared/target", **changes,
                })
                for host in ("natedev", "Mac"):
                    self.write(self.root / host / "history.jsonl", *([previous] if previous["host"] == host else []))
                self.write(self.root / "natedev" / "2026-10.jsonl", cargo_build("current", target_dir="/shared/target"))
                section = self.rebuilds_lines()
                expected_count = 2 if name == "same timestamp" else 1
                self.assertTrue(any(line.startswith(f"| first build here (1–49 crates) | {expected_count} |") for line in section), section)
                self.assertIn("| edited crate (1–3 crates) | 0 | — | — | — | — | — |", section)

    def test_target_coverage_combines_finished_history_and_group_selectors(self) -> None:
        prefix = ["cargo", "nextest", "run", "--workspace"]
        cases: tuple[tuple[str, list[list[str]], list[str], bool], ...] = (
            ("union", [["--lib", "--test", "a"], ["--test", "b"]], ["--lib", "--test", "a", "--test", "b"], True),
            ("uncovered", [["--lib", "--test", "a"]], ["--lib", "--test", "a", "--test", "b"], False),
            ("default", [[]], [], True),
            ("explicit does not cover default", [["--bin", "hana"]], [], False),
            ("default does not cover explicit", [[]], ["--bin", "hana"], False),
            ("bins covers named binary", [["--bins"]], ["--bin", "hana"], True),
            ("tests covers named test", [["--tests"]], ["--test", "a"], True),
            ("examples covers named example", [["--examples"]], ["--example", "demo"], True),
            ("benches covers named bench", [["--benches"]], ["--bench", "speed"], True),
            ("bins does not cover tests", [["--bins"]], ["--test", "a"], False),
            ("named binary does not cover bins", [["--bin", "hana"]], ["--bins"], False),
            ("named test does not cover tests", [["--test", "a"]], ["--tests"], False),
            ("named example does not cover examples", [["--example", "demo"]], ["--examples"], False),
            ("named bench does not cover benches", [["--bench", "speed"]], ["--benches"], False),
            ("all targets covers default", [["--all-targets"]], [], True),
            ("all targets covers every non-doc item", [["--all-targets"]],
             ["--lib", "--bin", "hana", "--bins", "--test", "a", "--tests", "--example", "demo",
              "--examples", "--bench", "speed", "--benches", "--all-targets"], True),
            ("all targets does not cover docs", [["--all-targets"]], ["--doc"], False),
            ("all targets plus docs covers docs", [["--all-targets"], ["--doc"]], ["--bin", "hana", "--doc"], True),
        )
        for name, prior_items, requested, warm in cases:
            with self.subTest(coverage=name):
                history = [cargo_build(f"previous-{number}", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                                       argv=[*prefix, *items]) for number, items in enumerate(prior_items)]
                self.write(self.root / "natedev" / "2026-09.jsonl", *history)
                self.write(self.root / "natedev" / "2026-10.jsonl", cargo_build("current", argv=[*prefix, *requested]))
                section = self.rebuilds_lines()
                label = "edited crate (1–3 crates)" if warm else "first build here (1–49 crates)"
                self.assertTrue(any(line.startswith(f"| {label} | 1 |") for line in section), section)

    def test_multi_target_history_warms_only_the_same_complete_target_set(self) -> None:
        prefix = ["cargo", "nextest", "run", "--workspace", "--bin", "hana"]
        cases: tuple[tuple[str, list[str], bool], ...] = (
            ("same order", ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"], True),
            ("reordered with duplicate", ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
                                           "x86_64-unknown-linux-gnu"], True),
            ("last target alone", ["x86_64-unknown-linux-gnu"], False),
            ("different pair with same last target", ["wasm32-unknown-unknown", "x86_64-unknown-linux-gnu"], False),
            ("default target", [], False),
        )
        for name, targets, warm in cases:
            with self.subTest(targets=name):
                self.write(
                    self.root / "natedev" / "2026-09.jsonl",
                    cargo_build("previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                                argv=[*prefix, "--target", "aarch64-unknown-linux-gnu",
                                      "--target", "x86_64-unknown-linux-gnu"]),
                )
                self.write(
                    self.root / "natedev" / "2026-10.jsonl",
                    cargo_build("current", argv=[*prefix, *(f"--target={target}" for target in targets)]),
                )
                section = self.rebuilds_lines()
                warm_count, first_count = (1, 0) if warm else (0, 1)
                self.assertTrue(any(line.startswith(f"| edited crate (1–3 crates) | {warm_count} |") for line in section), section)
                self.assertTrue(any(line.startswith(f"| first build here (1–49 crates) | {first_count} |") for line in section), section)

    def test_warm_history_compares_instants_instead_of_fractional_timestamp_spelling(self) -> None:
        prefix = "2026-10-02T12:08:45"
        cases = (
            ("earlier whole second", "Z", ".123Z", True),
            ("later fractional second", ".123Z", "Z", False),
            ("equal instant fractional history", ".000Z", "Z", False),
            ("equal instant whole-second history", "Z", ".000Z", False),
            ("equal instant longer history fraction", ".123000Z", ".123Z", False),
            ("equal instant shorter history fraction", ".123Z", ".123000Z", False),
            ("earlier microsecond", ".123000Z", ".123001Z", True),
            ("later microsecond", ".123001Z", ".123000Z", False),
        )
        for name, previous_suffix, current_suffix, warm in cases:
            with self.subTest(history=name):
                previous_at, current_at = prefix + previous_suffix, prefix + current_suffix
                self.write(
                    self.root / "natedev" / "2026-10.jsonl",
                    cargo_build("previous", started_at=previous_at, ended_at=previous_at, crates_compiled=None),
                    cargo_build("current", started_at=current_at, ended_at=current_at, caller="verify"),
                )
                section = self.rebuilds_lines(local_day(current_at))
                label = "edited crate (1–3 crates)" if warm else "first build here (1–49 crates)"
                other_label = "first build here (1–49 crates)" if warm else "edited crate (1–3 crates)"
                self.assertTrue(any(line.startswith(f"| {label} | 1 |") for line in section), section)
                self.assertTrue(any(line.startswith(f"| {other_label} | 0 |") for line in section), section)
                with closing(index.read_only()) as connection:
                    groups = report.profile_step_durations(connection, datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC))
                self.assertEqual(groups[("nextest", "verify", label)], [20.0])
                self.assertNotIn(("nextest", "verify", other_label), groups)

    def test_hana_group_history_warms_filtered_binary_in_all_rebuild_callers(self) -> None:
        prefix = ["cargo", "nextest", "run", "--workspace"]
        self.write(
            self.root / "natedev" / "2026-09.jsonl",
            cargo_build("previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                        target_dir=TARGET_DIR_UNSET, worktree="/worktrees/hana", cwd="/worktrees/hana",
                        argv=[*prefix, "--lib", "--bins", "--tests", "--features", "hana/dynamic_linking"]),
        )
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            cargo_build("current", caller="verify", target_dir=TARGET_DIR_UNSET, worktree="/worktrees/hana", cwd="/worktrees/hana",
                        argv=[*prefix, "--bin", "hana", "-E", "package(hana)", "--features", "hana/dynamic_linking"]),
        )
        section = self.rebuilds_lines()
        self.assertIn("| edited crate (1–3 crates) | 1 | 10.0 s | 10.0 s | 20.0 s | 100% | 100% |", section)
        self.assertIn("| first build here (1–49 crates) | 0 | — | — | — | — | — |", section)
        self.assertIn("| hana | 1 | 10.0 s | 10.0 s | 10.0 s |", section)
        with closing(index.read_only()) as connection:
            groups = report.profile_step_durations(connection, datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC))
        self.assertEqual(groups[("nextest", "verify", "edited crate (1–3 crates)")], [20.0])
        self.assertNotIn(("nextest", "verify", "first build here (1–49 crates)"), groups)

    def test_rebuild_package_and_profile_callers_use_the_same_warm_classification(self) -> None:
        self.write(
            self.root / "natedev" / "2026-09.jsonl",
            cargo_build("previous-bin", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP),
            cargo_build("previous-tests", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                        argv=["cargo", "nextest", "run", "--workspace", "--tests"]),
        )
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            cargo_build("warm-edited", caller="verify", argv=["cargo", "nextest", "run", "--workspace", "--bin", "hana", "-E", "package(warm)"]),
            cargo_build("warm-cascade", caller="verify", crates_compiled=4, duration_s=40.0, finished_s=20.0,
                        argv=["cargo", "nextest", "run", "--workspace", "--tests"]),
            cargo_build("first-edited", caller="verify", target_dir="/fresh/one", duration_s=338.0, finished_s=300.0,
                        argv=["cargo", "nextest", "run", "--workspace", "--bin", "hana", "-E", "package(first)"]),
            cargo_build("first-cascade", caller="verify", target_dir="/fresh/two", crates_compiled=4, duration_s=711.0, finished_s=700.0),
        )
        section = self.rebuilds_lines()
        for prefix in ("| edited crate (1–3 crates) | 1 |", "| cascade (4–49 crates) | 1 |", "| first build here (1–49 crates) | 2 |"):
            self.assertTrue(any(line.startswith(prefix) for line in section), section)
        title = section.index("Edited-crate rebuilds by package (nextest):")
        packages = [line for line in section[title + 1:] if line.startswith("|")]
        self.assertEqual(packages[2:], ["| warm | 1 | 10.0 s | 10.0 s | 10.0 s |"])
        with closing(index.read_only()) as connection:
            groups = report.profile_step_durations(connection, datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC))
        self.assertEqual(groups[("nextest", "verify", "edited crate (1–3 crates)")], [20.0])
        self.assertEqual(groups[("nextest", "verify", "cascade (4–49 crates)")], [40.0])
        self.assertCountEqual(groups[("nextest", "verify", "first build here (1–49 crates)")], [338.0, 711.0])

    def test_unfinished_and_future_items_cannot_complete_the_historical_union(self) -> None:
        prefix = ["cargo", "nextest", "run", "--workspace"]
        self.write(
            self.root / "natedev" / "history.jsonl",
            cargo_build("finished-a", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP, argv=[*prefix, "--test", "a"]),
            cargo_build("unfinished-b", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP, finished_s=None,
                        argv=[*prefix, "--test", "b"]),
            cargo_build("future-b", started_at="2026-10-03T12:00:00Z", ended_at="2026-10-03T12:00:00Z",
                        argv=[*prefix, "--test", "b"]),
        )
        self.write(self.root / "natedev" / "2026-10.jsonl", cargo_build("current", argv=[*prefix, "--test", "a", "--test", "b"]))
        section = self.rebuilds_lines()
        self.assertTrue(any(line.startswith("| first build here (1–49 crates) | 1 |") for line in section), section)
        self.assertIn("| edited crate (1–3 crates) | 0 | — | — | — | — | — |", section)

    def test_nextest_rebuilds_show_packages_top_five_and_empty_cold_bin(self) -> None:
        self.write(self.root / "natedev" / "2026-09.jsonl", cargo_build(
            "previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP, argv=["cargo", "nextest", "run"],
        ))
        package_steps = [
            ("hana-short", "package(hana)", 10.0, 1),
            ("hana-complex", "package(hana) & (test(a) | test(b))", 30.0, 3),
            ("unknown", None, 35.0, 2),
            ("alpha", "package(alpha)", 20.0, 1),
            ("beta", "package(beta)", 20.0, 2),
            ("gamma", "package(gamma)", 15.0, 3),
            ("delta", "package(delta)", 10.0, 1),
            ("epsilon", "package(epsilon)", 5.0, 2),
        ]
        records = [
            step(
                record_id,
                step="nextest",
                argv=["cargo", "nextest", "run", "-E", expression] if expression else ["cargo", "nextest", "run"],
                duration_s=finished * 2,
                finished_s=finished,
                crates_compiled=crates,
            )
            for record_id, expression, finished, crates in package_steps
        ]
        records.append(
            step(
                "cascade-package",
                step="nextest",
                argv=["cargo", "nextest", "run", "-E", "package(zeta)"],
                duration_s=200.0,
                finished_s=100.0,
                crates_compiled=4,
            )
        )
        self.write(self.root / "natedev" / "2026-10.jsonl", *records)

        section = self.rebuilds_lines()
        self.assertIn("| cold (50+ crates) | 0 | — | — | — | — | — |", section)
        self.assertEqual(section[2], "nextest: 4.1 min compiling, 4.1 min running tests (50% compiling).")
        self.assertEqual(section[3], "")
        self.assertEqual(section[4], "| Rebuild | Steps | Compile | Other | Total | Share of compile | Share of time |")
        title = section.index("Edited-crate rebuilds by package (nextest):")
        package_source = next(line for line in section[title + 1:] if line.startswith("Source:"))
        self.assertTrue(package_source.startswith("Source: warm nextest steps"), package_source)
        package_table = [line for line in section[title + 1:] if line.startswith("|")]
        self.assertEqual(package_table[0], "| Package | Steps | p50 | p95 | Compile |")
        self.assertEqual(package_table[2:], [
            "| hana | 2 | 10.0 s | 30.0 s | 40.0 s |",
            "| (unknown) | 1 | 35.0 s | 35.0 s | 35.0 s |",
            "| alpha | 1 | 20.0 s | 20.0 s | 20.0 s |",
            "| beta | 1 | 20.0 s | 20.0 s | 20.0 s |",
            "| gamma | 1 | 15.0 s | 15.0 s | 15.0 s |",
        ])
        self.assertFalse(any("delta" in line or "epsilon" in line or "zeta" in line for line in package_table))

    def test_each_rebuild_table_is_followed_immediately_by_its_source_and_a_blank_line(self) -> None:
        self.write(self.root / "natedev" / "2026-09.jsonl", cargo_build(
            "previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP, argv=["cargo", "nextest", "run"],
        ))
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("edited", step="nextest", argv=["cargo", "nextest", "run", "-E", "package(hana)"], duration_s=20.0, finished_s=10.0, crates_compiled=1),
        )

        section = self.rebuilds_lines()
        table_ends = [
            index
            for index, line in enumerate(section[:-1])
            if line.startswith("|") and not section[index + 1].startswith("|")
        ]
        self.assertEqual(len(table_ends), 2)
        for table_end in table_ends:
            self.assertTrue(section[table_end + 1].startswith("Source:"))
            self.assertTrue(table_end + 2 == len(section) or section[table_end + 2] == "")

    def write_release(self, trial: rust_release.TrialOutcome, pin: str | None = "1.99.0") -> None:
        state: rust_release.ReleaseState = {
            "check_day": "2026-11-12",
            "stable_version": "1.100.0",
            "release_date": "2026-11-12",
            "pin": pin,
            "trial": trial,
            "text_sent": True,
        }
        rust_release.write_state(state)

    def test_report_shows_finished_rust_trial_line_once(self) -> None:
        self.write_release({
            "status": "finished", "version": "1.100.0", "warnings": 7, "warning_crates": 3,
            "errors": 0, "error_crates": 0, "mend_builds": True, "target_gib": 1.5, "mend_target_gib": 0.5,
        })
        lines = self.render().splitlines()
        expected = "Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: 7 new warnings in 3 crates, cargo-mend builds."
        self.assertEqual(1, lines.count(expected))

    def test_report_shows_waiting_rust_trial_reason(self) -> None:
        self.write_release({"status": "waiting", "version": "1.100.0", "reason": "513 GiB free, needs 550"})
        self.assertIn(
            "Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: waiting, 513 GiB free, needs 550.",
            self.render().splitlines(),
        )

    def test_report_shows_failed_rust_trial_step_and_reason(self) -> None:
        self.write_release({
            "status": "failed", "version": "1.100.0", "step": "clippy", "reason": "error: clippy broke",
            "target_gib": 1.5, "mend_target_gib": 0,
        })
        self.assertIn(
            "Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: failed, clippy: error: clippy broke.",
            self.render().splitlines(),
        )

    def test_report_omits_rust_line_without_state_or_after_pin_catches_up(self) -> None:
        self.assertNotIn("Rust 1.100.0 out", self.render())
        self.write_release({"status": "waiting", "version": "1.100.0", "reason": "awaiting quiet hours"}, pin="1.100.0")
        self.assertNotIn("Rust 1.100.0 out", self.render())

    def test_report_shows_finished_rust_errors_and_mend_result(self) -> None:
        for mend_builds, mend_text in ((True, "cargo-mend builds"), (False, "cargo-mend does not build")):
            with self.subTest(mend_builds=mend_builds):
                self.write_release({
                    "status": "finished", "version": "1.100.0", "warnings": 2, "warning_crates": 1,
                    "errors": 1, "error_crates": 1, "mend_builds": mend_builds,
                    "target_gib": 1.5, "mend_target_gib": 0.5,
                })
                self.assertIn(
                    f"Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: 2 new warnings in 1 crate, 1 error in 1 crate, {mend_text}.",
                    self.render().splitlines(),
                )

    def test_report_omits_rust_line_while_pin_is_absent(self) -> None:
        self.write_release({
            "status": "finished", "version": "1.100.0", "warnings": 0, "warning_crates": 0,
            "errors": 0, "error_crates": 0, "mend_builds": True,
            "target_gib": 0, "mend_target_gib": 0,
        }, pin=None)
        self.assertNotIn("Rust 1.100.0 out", self.render())

    def test_test_builds_split_whole_package_and_filter_calls_with_daily_hours_and_p75(self) -> None:
        started_at = "2026-10-04T12:00:00Z"
        records = [
            call(f"whole-{build}", started_at=started_at, build_s=build)
            for build in (360, 720, 1080, 1440)
        ] + [
            call(f"filter-{build}", started_at=started_at, command="test hana --filter one", build_s=build)
            for build in (90, 180, 270, 360)
        ]
        records += [
            call("unmeasured", started_at=started_at, command="test hana --filter two", build_s=None),
            call("other-day", build_s=3600),
            call("other-verb", started_at=started_at, command="check hana", verb="check", build_s=3600),
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *records)

        lines = self.render("2026-10-04").splitlines()
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Tests per edit")]
        self.assertIn("| 2026-10-04 | whole-package | 1.0 h | 18.0 min |", section)
        self.assertIn("| 2026-10-04 | --filter | 15.0 min | 4.5 min |", section)
        self.assertEqual(2, sum(line.startswith("| 2026-10-04 |") for line in section))

    def test_test_builds_show_fixed_baselines_and_one_temporary_note(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl", call("measured", started_at="2026-10-04T12:00:00Z", build_s=90))

        lines = self.render("2026-10-04").splitlines()
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Summary")]
        self.assertIn("Source: build log `verify.sh test` calls with measured `build_s`; p75 is the nearest rank.", section)
        self.assertIn("| Baseline 2026-10-01/02 | whole-package | 5.2 h | 2.3 min |", section)
        self.assertIn("| Baseline 2026-10-04 from 00:07 EDT | whole-package | 5.0 h | 1.7 min |", section)
        self.assertIn("| Baseline 2026-10-04 from 00:07 EDT | --filter | 14.4 h | 1.2 min |", section)
        note = "Temporary: kept until the user calls the result settled."
        self.assertEqual(1, section.count(note))
        self.assertEqual("", section[section.index(note) - 1])

    def test_test_builds_count_a_name_that_contains_filter_as_whole_package(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("named", started_at="2026-10-04T12:00:00Z", command="test demo--filter", build_s=90),
        )

        lines = self.render("2026-10-04").splitlines()
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Summary")]
        self.assertIn("| 2026-10-04 | whole-package | 1.5 min | 1.5 min |", section)
        self.assertFalse(any(line.startswith("| 2026-10-04 | --filter |") for line in section))

    def test_recorded_pass_local_call_remains_in_agent_outcomes(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call(
                "recorded-pass",
                outcome="reused",
                saved_s=60,
                mac="declined",
                mac_reason="local_flag",
                mac_s=None,
            ),
        )

        lines = self.render().splitlines()
        section = lines[
            lines.index("### Agent calls (verify.sh)") : lines.index(
                "### Tests per edit"
            )
        ]
        self.assertIn("| reused | 1 |  | 1.0 min |", section)

    def test_disabled_decline_without_step_remains_in_token_waits(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call(
                "waited-with-switch-off",
                wait_s=300,
                token_wait_s=300,
                mac="declined",
                mac_reason="off",
                mac_s=0.0,
            ),
        )

        waits = self.waiting_rows()["Build-folder turn, behind another worker"]
        self.assertEqual(waits[0], f"5.0 min (feature, {local_clock(STAMP)})")
        self.assertEqual(waits[2], "1 of 1 calls")

    def test_mac_completed_calls_are_absent_from_agent_outcomes(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("kept", outcome="reused", saved_s=60),
            call(
                "failed-on-mac",
                outcome="failed",
                status=1,
                mac="failed",
                mac_reason="",
                mac_s=10.0,
            ),
            call(
                "passed-filter-on-mac",
                outcome="ran",
                mac="passed_filter",
                mac_reason="",
                mac_s=10.0,
            ),
        )

        lines = self.render().splitlines()
        section = lines[
            lines.index("### Agent calls (verify.sh)") : lines.index(
                "### Tests by target"
            )
        ]
        self.assertIn("| reused | 1 |  | 1.0 min |", section)
        self.assertFalse(any(line.startswith("| failed |") for line in section))
        self.assertFalse(any(line.startswith("| ran |") for line in section))

    def test_offload_columns_become_domain_values(self) -> None:
        self.assertIsInstance(
            mac_outcome.offload_record(
                mac=None, reason=None, seconds_value=None, local_reason=None
            ),
            mac_outcome.NoOffloadRecord,
        )
        self.assertIsInstance(
            mac_outcome.offload_record(
                mac="", reason="", seconds_value=None, local_reason=""
            ),
            mac_outcome.NoOffloadRecord,
        )
        requested = mac_outcome.offload_record(
            mac="declined",
            reason="local_flag",
            seconds_value=None,
            local_reason="needs Linux filesystem semantics",
        )
        self.assertEqual(
            requested,
            mac_outcome.LocalRunRequested("needs Linux filesystem semantics"),
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(requested, "reason", "changed")
        old_requested = mac_outcome.offload_record(
            mac="declined",
            reason="local_flag",
            seconds_value=None,
            local_reason=None,
        )
        self.assertEqual(old_requested, mac_outcome.LocalRunRequested(mac_outcome.LOCAL_REASON_NOT_RECORDED))
        self.assertIsInstance(
            mac_outcome.offload_record(
                mac="declined",
                reason="runner",
                seconds_value=None,
                local_reason="",
            ),
            mac_outcome.RunnerResultUnavailable,
        )

        decline = mac_outcome.offload_record(
            mac="declined",
            reason="busy",
            seconds_value=2.5,
            local_reason="",
        )
        self.assertIsInstance(decline, mac_outcome.OffloadDeclined)
        if isinstance(decline, mac_outcome.OffloadDeclined):
            self.assertEqual(decline.reason, "busy")
            self.assertEqual(decline.seconds, 2.5)

        completed = mac_outcome.offload_record(
            mac="passed_filter", reason="", seconds_value=3.5, local_reason=""
        )
        self.assertIsInstance(completed, mac_outcome.MacRunCompleted)
        if isinstance(completed, mac_outcome.MacRunCompleted):
            self.assertEqual(completed.outcome, "passed_filter")
            self.assertEqual(completed.seconds, 3.5)

        lost = mac_outcome.offload_record(
            mac="lost", reason="", seconds_value=4.5, local_reason=""
        )
        self.assertIsInstance(lost, mac_outcome.MacRunLost)
        if isinstance(lost, mac_outcome.MacRunLost):
            self.assertEqual(lost.seconds, 4.5)

        no_test = mac_outcome.offload_record(
            mac="declined",
            reason="no_tests",
            seconds_value=5.5,
            local_reason="",
        )
        self.assertIsInstance(no_test, mac_outcome.MacRunMatchedNoTest)
        if isinstance(no_test, mac_outcome.MacRunMatchedNoTest):
            self.assertEqual(no_test.seconds, 5.5)

    def test_invalid_offload_records_become_unrecognized(self) -> None:
        values = (
            mac_outcome.offload_record(
                mac="declined",
                reason="new_reason",
                seconds_value=1.0,
                local_reason="",
            ),
            mac_outcome.offload_record(
                mac="passed", reason="", seconds_value=None, local_reason=""
            ),
            mac_outcome.offload_record(
                mac="new_word", reason="", seconds_value=1.0, local_reason=""
            ),
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsInstance(value, mac_outcome.UnrecognizedOffloadRecord)

    def test_mac_section_omits_empty_records_and_ignores_unrecognized_ones(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("old"),
            call("empty", mac="", mac_reason="", mac_s=None),
        )
        self.assertEqual(self.mac_tests_lines(), [])
        self.assertNotIn("### Tests by target", self.render())

        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("unknown-reason", mac="declined", mac_reason="new_reason", mac_s=1.0),
            call("missing-seconds", mac="passed", mac_reason="", mac_s=None),
        )
        self.assertEqual(self.mac_tests_lines(), [])
        self.assertNotIn("### Tests by target", self.render())

    def test_calls_that_did_not_reach_the_mac_still_have_a_package_row(self) -> None:
        records = [
            call("disabled", mac="declined", mac_reason="off", mac_s=0.0),
            call("unenrolled", mac="declined", mac_reason="repo", mac_s=None),
            call("linux", mac="declined", mac_reason="linux_only", mac_s=None),
            call("local", mac="declined", mac_reason="local_flag", mac_s=None),
            call("runner", mac="declined", mac_reason="runner", mac_s=None),
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *records)
        row = next(line for line in self.mac_tests_lines() if line.startswith("| hana |"))
        for text in ('Linux-only 1', '--local 1', 'runner 1'):
            self.assertIn(text, row)
        self.assertNotIn('off 1', row)
        self.assertNotIn('repository 1', row)

    def test_disabled_counts_are_absent_from_reported_declines(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            *[call(f"blocked-{number}", mac="declined", mac_reason="blocked", mac_s=1.0) for number in range(2)],
            *[call(f"disabled-{number}", mac="declined", mac_reason="off", mac_s=0.0) for number in range(5)],
        )
        row = next(line for line in self.mac_tests_lines() if line.startswith("| hana |"))
        self.assertIn('blocked 2', row)
        self.assertNotIn('off 5', row)

    def test_disabled_and_repository_declines_alone_have_no_package_row(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("disabled", package="disabled", mac="declined", mac_reason="off", mac_s=0),
            call("repository", package="repository", mac="declined", mac_reason="repo", mac_s=None),
        )
        self.assertEqual(self.mac_tests_lines(), [])

    def test_hidden_declines_keep_a_package_row_when_natedev_ran(self) -> None:
        for reason in ("off", "repo"):
            with self.subTest(reason=reason):
                self.write(
                    self.root / "natedev" / "2026-10.jsonl",
                    call("local", mac="declined", mac_reason=reason, mac_s=0),
                    self.nextest_step("local-step", "local"),
                )
                cells = self.target_cells()
                self.assertEqual(cells["natedev ran"], "1")
                self.assertEqual(cells["Did not reach the Mac"], "")

    def test_no_test_match_is_tried_not_failed_or_stayed_local(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call(
                "no-test",
                mac="declined",
                mac_reason="no_tests",
                mac_s=15.0,
                wall_s=120,
                token_wait_s=30,
            ),
            self.nextest_step("local-no-test", "no-test"),
        )

        section = self.mac_tests_lines()
        heading = next(line for line in section if line.startswith("| Package |"))
        row = next(line for line in section if line.startswith("| hana |"))
        names = [cell.strip() for cell in heading.strip("|").split("|")]
        values = [cell.strip() for cell in row.strip("|").split("|")]
        cells = dict(zip(names, values, strict=True))
        self.assertEqual(cells["Mac ran"], "1")
        self.assertEqual(cells["Mac failed"], "0")
        self.assertEqual(cells["Mac passed"], "0")
        self.assertEqual(cells["Did not reach the Mac"], "")
        self.assertIn("No test matched on the Mac: 1 (hana)", section)

    def test_no_test_match_finishes_on_the_mac_in_the_lead_share(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("no-test", mac="declined", mac_reason="no_tests", mac_s=15, target="mac"),
            call("busy", mac="declined", mac_reason="busy", mac_s=1, target="natedev"),
        )
        cells = self.target_cells()
        self.assertEqual(cells["Mac ran"], "1")
        self.assertEqual(cells["Mac passed"], "0")
        self.assertEqual(cells["Mac failed"], "0")
        self.assertEqual(cells["Did not reach the Mac"], "busy with our test 1")
        self.assertIn("Mac when usable: 1 of 2 candidates finished there (50%).", self.render())

    def test_disabled_offload_is_set_aside_in_the_lead_share(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("passed", mac="passed", mac_reason="", mac_s=1, target="mac"),
            call("off", mac="declined", mac_reason="off", mac_s=0, target="natedev"),
            call("state", mac="declined", mac_reason="state", mac_s=1, target="natedev"),
            call("copy", mac="declined", mac_reason="copy", mac_s=1, target="natedev"),
        )
        self.assertIn("Mac when usable: 1 of 3 candidates finished there (33%).", self.render())
        self.assertNotIn("off 1", self.target_cells()["Did not reach the Mac"])

    def test_a_day_with_only_disabled_offload_has_no_lead_share(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl",
                   call("off", mac="declined", mac_reason="off", mac_s=0, target="natedev"))
        self.assertNotIn("Mac when usable:", self.render())
        self.assertEqual(self.mac_tests_lines(), [])

    def test_cached_mac_pass_is_left_out_of_the_lead_share(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("cached", outcome="reused", mac="passed", mac_reason="", mac_s=1),
            call("no-test", mac="declined", mac_reason="no_tests", mac_s=1),
            self.nextest_step("cached-step", "cached"),
        )
        self.assertIn("Mac when usable: 1 of 1 candidates finished there (100%).", self.render())
        self.assertEqual(self.target_cells()["Mac ran"], "1")
        self.assertEqual(self.target_cells()["natedev ran"], "0")

    def test_mac_section_reports_outcomes_estimates_and_pairing(self) -> None:
        at = "2026-10-02T12:00:00Z"
        calls = [
            call("confirmed", started_at=at, mac="passed", mac_reason="", mac_s=10.0,
                 wall_s=120, token_wait_s=12, build_s=120),
            call("filtered", started_at="2026-10-02T12:01:00Z", mac="passed_filter", mac_reason="", mac_s=20.0),
            call("failed-paired", started_at="2026-10-02T12:02:00Z", ended_at="2026-10-02T12:03:00Z",
                 mac="failed", mac_reason="", mac_s=30.0, status=1, outcome="failed"),
            call("failed-avoided", started_at="2026-10-02T12:04:00Z", ended_at="2026-10-02T12:05:00Z",
                 command="test hana --filter other", worktree="/r/other", mac="failed", mac_reason="",
                 mac_s=40.0, status=1, outcome="failed"),
            call("lost", started_at="2026-10-02T12:06:00Z", mac="lost", mac_reason="", mac_s=50.0),
            call("no-test", started_at="2026-10-02T12:07:00Z", mac="declined", mac_reason="no_tests",
                 mac_s=60.0, wall_s=120, token_wait_s=12),
            call("local-pass", started_at="2026-10-02T12:08:00Z", command="test hana", mac="declined",
                 mac_reason="local_flag", mac_s=None, wall_s=120, token_wait_s=12),
        ]
        calls += [
            call(f"blocked-{number}", started_at=f"2026-10-02T12:{10 + number:02d}:00Z", mac="declined",
                 mac_reason="blocked", mac_s=1.0, wall_s=120, token_wait_s=12)
            for number in range(3)
        ]
        calls += [
            call(f"mac-busy-{number}", started_at=f"2026-10-02T12:{20 + number:02d}:00Z", mac="declined",
                 mac_reason="mac_busy", mac_s=1.0, wall_s=120, token_wait_s=12)
            for number in range(2)
        ]
        local_call_ids = ["confirmed", "no-test", "local-pass"] + [
            f"blocked-{number}" for number in range(3)
        ] + [f"mac-busy-{number}" for number in range(2)]
        local_steps = [
            self.nextest_step(f"local-{call_id}", call_id)
            for call_id in local_call_ids
        ]
        mac_call_ids = [
            "confirmed",
            "filtered",
            "failed-paired",
            "failed-avoided",
            "lost",
            "no-test",
        ]
        mac_steps = [
            self.nextest_step(
                f"mac-{call_id}",
                call_id,
                caller="verify-mac",
                host="Mac",
                repo_path=None,
                worktree=None,
                branch=None,
                sha=None,
            )
            for call_id in mac_call_ids
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *calls, *local_steps)
        self.write(self.root / "Mac" / "2026-10.jsonl", *mac_steps)

        lines = self.render().splitlines()
        start = lines.index("### Tests by target")
        end = next(
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("### ")
        )
        section = lines[start:end]
        heading = next(line for line in section if line.startswith("| Package |"))
        row = next(line for line in section if line.startswith("| hana |"))
        names = [cell.strip() for cell in heading.strip("|").split("|")]
        values = [cell.strip() for cell in row.strip("|").split("|")]
        cells = dict(zip(names, values, strict=True))
        self.assertEqual(cells["Mac ran"], "6")
        self.assertEqual(cells["Mac failed"], "2")
        self.assertEqual(cells["Mac passed"], "2")
        self.assertEqual(cells["Mac p50"], "20.0 s")
        self.assertEqual(cells["natedev p50"], "2.0 min")
        self.assertEqual(cells["Did not reach the Mac"], "blocked 3, Mac busy 2, --local 1")
        self.assertEqual(cells["natedev ran"], "8")
        self.assertEqual(
            sum(line.startswith("Source:") for line in section), 1
        )
        avoided = next(line for line in section if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 2", avoided)
        self.assertIn("4.0 min of build and test time", avoided)
        self.assertIn("0.4 min of waiting", avoided)
        self.assertIn(
            "Failed on the Mac, then passed on natedev with --local: 1 (hana)",
            section,
        )
        self.assertIn("No test matched on the Mac: 1 (hana)", section)
        self.assertLess(lines.index("### Test builds (temporary)"), start)
        self.assertLess(start, lines.index("### Tests per edit"))

    def write_copy_day(self) -> None:
        def at(minute: int) -> str:
            return f"2026-10-02T12:{minute:02d}:00Z"

        filtered = "test hana --filter a"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            *(call(f"ref-{number}", started_at=at(number), command=filtered) for number in range(3)),
            call("natedev-won", started_at=at(3), command=filtered, mac="declined", mac_reason="busy", mac_s=1.0,
                 copy="natedev_won", copy_s=20.0, copy_count=1, copy_natedev_s=60.0),
            call("won-a", started_at=at(4), command=filtered, mac="failed", mac_reason="", mac_s=30.0,
                 status=1, outcome="failed", target="mac", copy="won", copy_s=30.0, copy_count=1, copy_natedev_s=90.0),
            call("gave-way", started_at=at(5), mac="declined", mac_reason="busy", mac_s=1.0,
                 copy="gave_way", copy_s=15.0, copy_count=2, copy_natedev_s=40.0),
            call("passed-full", started_at=at(6), package="beta", command="test beta", mac="declined",
                 mac_reason="busy", mac_s=1.0, copy="passed_full", copy_s=50.0, copy_count=1, copy_natedev_s=80.0),
            call("won-b", started_at=at(7), package="beta", command="test beta --filter x", mac="passed_filter",
                 mac_reason="", mac_s=25.0, target="mac", copy="won", copy_s=25.0, copy_count=1, copy_natedev_s=100.0),
            call("won-gamma", started_at=at(8), package="gamma", command="test gamma --filter y", mac="failed",
                 mac_reason="", mac_s=10.0, status=1, outcome="failed", target="mac",
                 copy="won", copy_s=10.0, copy_count=1, copy_natedev_s=500.0),
            call("plain-filter", started_at=at(9), mac="passed_filter", mac_reason="", mac_s=12.0, target="mac"),
            *(self.nextest_step(f"ref-{number}-step", f"ref-{number}", duration_s=duration)
              for number, duration in enumerate((100.0, 120.0, 200.0))),
            self.nextest_step("natedev-won-step", "natedev-won", duration_s=60.0),
            self.nextest_step("won-a-stopped", "won-a", duration_s=90.0, status=143),
            self.nextest_step("gave-way-step", "gave-way", duration_s=300.0),
            self.nextest_step("passed-full-step", "passed-full", duration_s=400.0),
            self.nextest_step("won-b-stopped", "won-b", duration_s=100.0, status=143),
        )

    def test_mac_copies_section_counts_outcomes_and_estimates_the_saving(self) -> None:
        self.write_copy_day()
        text = self.render()
        lines = text.splitlines()
        mac = self.section_lines(text, "### Mac")
        self.assertEqual(lines[lines.index("### Mac") + len(mac)], "### Mac copies")
        section = self.section_lines(text, "### Mac copies")
        self.assertEqual(section[2], "| Package | Copies | Mac won | natedev won | Gave way | Other | Est. saved"
                         + " | Mac time on copies that did not win |")
        self.assertEqual([line for line in section if line.startswith("| ")][1:], [
            # beta: (400 - 100) from the package's one step, as the command has fewer than three.
            "| beta | 2 | 1 | 0 | 0 | 1 | 5.0 min | 50.0 s |",
            "| gamma | 1 | 1 | 0 | 0 | 0 | 0.0 s | — |",
            # hana: the median of 100, 120 and 200 minus 90; the 60 s step ended before t.
            "| hana | 4 | 1 | 1 | 1 | 0 | 30.0 s | 35.0 s |",
        ])
        self.assertTrue(any(line.startswith("Est. saved is an estimate:") for line in section))
        lead = [line for line in lines[2:lines.index("### Waiting")] if line]
        self.assertEqual(lead[-1], "Mac copies: 3 of 7 finished first, about 5.5 min saved (estimated).")
        self.assertNotIn("Mac copies: none started.", text)

    def test_copy_wins_are_not_natedev_runs_or_avoided_runs_or_failed_steps(self) -> None:
        self.write_copy_day()
        text = self.render()
        targets = self.section_lines(text, "### Tests by target")
        rows = {line.split("|")[1].strip(): [cell.strip() for cell in line.strip("|").split("|")]
                for line in targets if line.startswith("| ") and not line.startswith("| Package")}
        # hana: three references, the natedev win and the copy that gave way ran on natedev; won-a did not.
        self.assertEqual(rows["hana"][5], "5")
        self.assertEqual(rows["hana"][1:4], ["2", "1", "1"])
        self.assertEqual(rows["beta"][5], "1")
        avoided = next(line for line in targets if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 1 ", avoided)
        nextest = self.section_lines(text, "### nextest")
        self.assertTrue(any(line.startswith("| verify.sh (agents) | 8 | 0 |") for line in nextest))
        self.assertNotIn("Failed steps:", text)
        failures = self.section_lines(text, "### Summary: failures")
        self.assertTrue(any(line.startswith("| nextest | 2 |") for line in failures))

    def test_day_without_a_copy_says_none_started_and_has_no_lead_line(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("plain", mac="declined", mac_reason="busy", mac_s=1.0),
            self.nextest_step("plain-step", "plain"),
        )
        text = self.render()
        self.assertIn("Mac copies: none started.", text.splitlines())
        self.assertNotIn("### Mac copies", text)
        self.assertNotIn("finished first", text)

    def test_local_pass_pairs_only_nearest_failure_and_reports_its_reason(self) -> None:
        reason = "uses Linux filesystem semantics"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("first", started_at="2026-10-02T12:00:00Z", mac="failed", mac_reason="", mac_s=10.0,
                 status=1, outcome="failed"),
            call("nearest", started_at="2026-10-02T12:05:00Z", mac="failed", mac_reason="", mac_s=10.0,
                 status=1, outcome="failed"),
            call("local", started_at="2026-10-02T12:10:00Z", mac="declined", mac_reason="local_flag",
                 local_reason=reason, mac_s=None, status=0),
            self.nextest_step("local-step", "local"),
        )
        section = self.mac_tests_lines()
        paired = next(
            line
            for line in section
            if line.startswith("Failed on the Mac, then passed on natedev with --local")
        )
        quoted_reason = f'"{reason}"'
        self.assertIn(quoted_reason, paired)
        self.assertLess(paired.index("--local"), paired.index(quoted_reason))
        self.assertIn("1 (hana)", paired)
        avoided = next(line for line in section if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 1", avoided)

    def test_local_pass_reason_with_double_quotes_is_escaped(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("failed", started_at="2026-10-02T12:05:00Z", mac="failed", mac_reason="", mac_s=10.0,
                 status=1, outcome="failed"),
            call("local", started_at="2026-10-02T12:10:00Z", mac="declined", mac_reason="local_flag",
                 local_reason='needs a "quoted" mount', mac_s=None, status=0),
            self.nextest_step("local-step", "local"),
        )
        paired = next(
            line
            for line in self.mac_tests_lines()
            if line.startswith("Failed on the Mac, then passed on natedev with --local")
        )
        self.assertIn('--local "needs a \\"quoted\\" mount": 1 (hana)', paired)

    def test_next_day_local_pass_pairs_with_late_mac_failure(self) -> None:
        failed_at = datetime(2026, 10, 2, 23, 50).astimezone().isoformat()
        passed_at = datetime(2026, 10, 3, 0, 10).astimezone().isoformat()
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call(
                "late-failure",
                started_at=failed_at,
                mac="failed",
                mac_reason="",
                mac_s=10.0,
                status=1,
                outcome="failed",
            ),
            call(
                "next-day-local",
                started_at=passed_at,
                mac="declined",
                mac_reason="local_flag",
                mac_s=None,
                status=0,
            ),
            self.nextest_step(
                "next-day-local-step",
                "next-day-local",
                started_at=passed_at,
            ),
        )

        section = self.mac_tests_lines("2026-10-02")
        avoided = next(line for line in section if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 0", avoided)
        self.assertIn(
            "Failed on the Mac, then passed on natedev with --local: 1 (hana)",
            section,
        )

    def test_avoided_time_uses_each_packages_natedev_medians(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("hana-reference", wall_s=60, token_wait_s=6),
            call("hana-avoided", started_at="2026-10-02T12:01:00Z", mac="passed_filter",
                 mac_reason="", mac_s=10.0),
            call("beta-reference", started_at="2026-10-02T12:02:00Z", package="beta",
                 command="test beta", wall_s=120, token_wait_s=12),
            call("beta-avoided", started_at="2026-10-02T12:03:00Z", package="beta",
                 command="test beta", mac="failed", mac_reason="", mac_s=20.0,
                 status=1, outcome="failed"),
            self.nextest_step("hana-reference-step", "hana-reference"),
            self.nextest_step("beta-reference-step", "beta-reference"),
        )

        section = self.mac_tests_lines()
        avoided = next(line for line in section if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 2", avoided)
        self.assertIn("3.0 min of build and test time", avoided)
        self.assertIn("0.3 min of waiting", avoided)

    def test_local_pass_with_different_command_matches_no_failure(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("failed", started_at="2026-10-02T12:00:00Z", command="test hana --filter one",
                 mac="failed", mac_reason="", mac_s=10.0, status=1, outcome="failed"),
            call("local", started_at="2026-10-02T12:10:00Z", command="test hana --filter two",
                 mac="declined", mac_reason="local_flag", mac_s=None, status=0),
            self.nextest_step("local-step", "local"),
        )
        section = self.mac_tests_lines()
        self.assertFalse(
            any("Failed on the Mac, then passed" in line for line in section)
        )
        avoided = next(line for line in section if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 1", avoided)

    def test_recorded_local_pass_does_not_pair_or_set_natedev_median(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("failed", started_at="2026-10-02T12:00:00Z", mac="failed", mac_reason="", mac_s=10.0,
                 status=1, outcome="failed"),
            call("recorded", started_at="2026-10-02T12:10:00Z", mac="declined", mac_reason="local_flag",
                 mac_s=None, wall_s=900, status=0, outcome="reused"),
            call("ran", started_at="2026-10-02T12:20:00Z", mac="declined", mac_reason="blocked",
                 mac_s=1.0, wall_s=60, status=0),
            self.nextest_step("ran-step", "ran"),
        )
        section = self.mac_tests_lines()
        row = next(line for line in section if line.startswith("| hana |"))
        self.assertIn("| 1.0 min |", row)
        self.assertFalse(
            any("Failed on the Mac, then passed" in line for line in section)
        )
        avoided = next(line for line in section if line.startswith("natedev runs avoided:"))
        self.assertIn("natedev runs avoided: 1", avoided)

    def test_test_builds_keep_confirmed_and_old_calls_but_not_mac_only_calls(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("confirmed", mac="passed", mac_reason="", mac_s=10.0, build_s=60),
            call("mac-only", mac="passed_filter", mac_reason="", mac_s=10.0, build_s=600),
            call("old", build_s=120),
            self.nextest_step("confirmed-local", "confirmed"),
        )
        self.write(
            self.root / "Mac" / "2026-10.jsonl",
            self.nextest_step(
                "confirmed-mac",
                "confirmed",
                caller="verify-mac",
                host="Mac",
            ),
            self.nextest_step(
                "only-mac",
                "mac-only",
                caller="verify-mac",
                host="Mac",
            ),
        )

        lines = self.render().splitlines()
        section = lines[
            lines.index("### Test builds (temporary)") : lines.index("### Tests by target")
        ]
        self.assertIn(
            "| 2026-10-02 | whole-package | 3.0 min | 2.0 min |",
            section,
        )

    def test_offloaded_steps_stay_out_of_every_natedev_query(self) -> None:
        self.write(self.root / "natedev" / "2026-09.jsonl", cargo_build(
            "previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP, argv=["cargo", "nextest", "run"],
        ))
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call(
                "local",
                mac="passed",
                mac_reason="",
                mac_s=10.0,
                build_s=60,
                unit_session="worker-a",
            ),
            call(
                "mac-only",
                started_at="2026-10-02T12:01:00Z",
                mac="passed_filter",
                mac_reason="",
                mac_s=20.0,
                build_s=600,
                unit_session="worker-a",
            ),
            self.nextest_step(
                "local-step",
                "local",
                duration_s=20.0,
                finished_s=10.0,
                crates_compiled=1,
                mem_wait_s=60,
                tree_key="tree-a",
            ),
        )
        self.write(
            self.root / "Mac" / "2026-10.jsonl",
            self.nextest_step(
                "mac-linked",
                "mac-only",
                caller="verify-mac",
                host="Mac",
                duration_s=900.0,
                finished_s=800.0,
                crates_compiled=50,
                mem_wait_s=600,
                tree_key="tree-b",
            ),
            self.nextest_step(
                "mac-orphan",
                "call-with-no-record",
                caller="verify-mac",
                host="Mac",
                duration_s=900.0,
                finished_s=800.0,
                crates_compiled=50,
                mem_wait_s=600,
                tree_key="tree-c",
            ),
            step(
                "mac-only-kind",
                step="fmt",
                caller="verify-mac",
                call_id="mac-only",
                host="Mac",
            ),
        )

        lines = self.render().splitlines()

        waiting_rows = self.waiting_rows()
        waiting = waiting_rows["Memory admission"]
        self.assertEqual(waiting[0], f"1.0 min (feature, {local_clock(STAMP)})")
        self.assertEqual(waiting[2], "1 of 1 steps")
        folder_waiting = waiting_rows[
            "Build-folder turn, behind another worker"
        ]
        self.assertEqual(folder_waiting[2], "0 of 1 calls")

        nextest = lines[lines.index("### nextest") : lines.index("### Memory pressure")]
        self.assertTrue(any(line.startswith("| verify.sh (agents) | 1 |") for line in nextest))
        self.assertFalse(any("(Mac)" in line for line in nextest))
        self.assertNotIn("### fmt", lines)

        builds = lines[
            lines.index("### Test builds (temporary)") : lines.index("### Tests by target")
        ]
        self.assertIn(
            "| 2026-10-02 | whole-package | 1.0 min | 1.0 min |",
            builds,
        )

        agent_calls = lines[
            lines.index("### Agent calls (verify.sh)") : lines.index(
                "### Test builds (temporary)"
            )
        ]
        self.assertTrue(any(line.startswith("| ran | 1 |") for line in agent_calls))

        per_edit = lines[
            lines.index("### Tests per edit") : lines.index("### Summary: successes")
        ]
        self.assertIn("| 2026-10-02 | 1 | 0 | — | — |", per_edit)

        rebuilds = self.rebuilds_lines()
        self.assertIn(
            "| edited crate (1–3 crates) | 1 | 10.0 s | 10.0 s | 20.0 s | 100% | 100% |",
            rebuilds,
        )
        self.assertIn("| cold (50+ crates) | 0 | — | — | — | — | — |", rebuilds)

        successes = lines[
            lines.index("### Summary: successes") : lines.index("### Summary: failures")
        ]
        self.assertIn(
            "| **All steps** | 1 | 20.0 s | 20.0 s | 20.0 s |  |",
            successes,
        )
        self.assertFalse(any(line.startswith("| fmt |") for line in successes))

    def test_verify_step_without_call_record_remains_in_reports(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step(
                "unlinked-local-step",
                step="doc",
                caller="verify",
                call_id="call-record-not-written",
                duration_s=30.0,
            ),
        )

        lines = self.render().splitlines()
        doc = lines[lines.index("### doc") : lines.index("### Memory pressure")]
        self.assertTrue(
            any(line.startswith("| verify.sh (agents) | 1 |") for line in doc)
        )
        successes = lines[
            lines.index("### Summary: successes") : lines.index(
                "### Summary: failures"
            )
        ]
        all_steps = lines[lines.index("### Summary: all") :]
        self.assertTrue(any(line.startswith("| doc | 1 |") for line in successes))
        self.assertTrue(any(line.startswith("| doc | 1 |") for line in all_steps))

    def verify_call(
        self,
        number: int,
        *trees: str | None,
        worker: str | None = "worker-a",
        session: str = "session-a",
        verb: str = "test",
        command: str = "test hana",
        status: int | None = 0,
        outcome: str | None = None,
        minute: int | None = None,
        day: str = "2026-10-02",
    ) -> None:
        at = f"{day}T12:{(number * 5 if minute is None else minute):02d}:00.000Z"
        call_id = f"call-{number}"
        self.records.append(
            call(
                call_id,
                started_at=at,
                ended_at=at,
                unit_session=worker,
                session=session,
                verb=verb,
                command=command,
                status=status,
                outcome=outcome or ("ran" if status == 0 else "failed"),
            )
        )
        for offset, tree in enumerate(trees):
            self.records.append(
                step(
                    f"{call_id}-step-{offset}",
                    started_at=at,
                    ended_at=at,
                    call_id=call_id,
                    tree_key=tree,
                    status=status,
                    caller="verify",
                    step="nextest" if verb == "test" else "clippy",
                )
            )

    def render_calls(self) -> str:
        self.write(self.root / "natedev" / "2026-10.jsonl", *self.records)
        return self.render()

    def per_edit_tables(self, day: str | None = None) -> tuple[list[list[str]], list[list[str]]]:
        self.write(self.root / "natedev" / "2026-10.jsonl", *self.records)
        _ = index.update()
        with closing(index.read_only()) as connection:
            lines = report.tests_per_edit_section(connection, day or local_day(STAMP))
        tables: list[list[list[str]]] = []
        previous = "other"
        for line in lines:
            if line.startswith("|"):
                if previous != "table":
                    tables.append([])
                tables[-1].append([cell.strip() for cell in line.strip("|").split("|")])
                previous = "table"
            else:
                previous = "other"
        self.assertEqual(len(tables), 2)
        return tables[0], tables[1]

    def cell(self, table: list[list[str]], label: str, heading: str) -> str:
        matching = [row for row in table[2:] if row[0] == label]
        self.assertEqual(len(matching), 1, f"row for {label}: {table}")
        return matching[0][table[0].index(heading)]

    def test_same_tree_has_no_edit_and_changed_tree_has_one(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-a")
        self.verify_call(2, "tree-b")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests"), "3")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "1")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests/edit"), "3.00")

    def test_lint_rewrite_inside_a_call_is_not_a_worker_edit(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-a", None, "tree-b", verb="lint", command="lint hana")
        self.verify_call(2, "tree-b")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "0")

    def test_call_without_known_tree_does_not_break_the_chain(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, None, verb="lint", command="lint hana")
        self.verify_call(2, "tree-b")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "1")

    def test_unit_session_then_session_identifies_independent_workers(self) -> None:
        self.verify_call(0, "tree-a", worker="unit-a", session="shared")
        self.verify_call(1, "tree-b", worker="unit-b", session="shared")
        self.verify_call(2, "tree-b", worker="unit-a", session="shared")
        self.verify_call(3, "tree-c", worker=None, session="fallback")
        self.verify_call(4, "tree-d", worker=None, session="fallback")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "2")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests"), "5")

    def test_whole_and_filtered_test_calls_both_count(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-b", command="test hana --filter parser")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests"), "2")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "1")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests/edit"), "2.00")

    def test_daily_trend_sums_workers_and_shows_all_seven_days_newest_first(self) -> None:
        self.verify_call(0, "tree-a", day="2026-10-01", worker="worker-a")
        self.verify_call(1, "tree-b", day="2026-10-04", worker="worker-a")
        self.verify_call(2, "tree-x", day="2026-10-04", worker="worker-b")
        self.verify_call(3, "tree-y", day="2026-10-04", worker="worker-b")
        ratios, _ = self.per_edit_tables("2026-10-04")
        self.assertEqual(ratios[0], ["Day", "Tests", "Edits", "Tests/edit", "Target"])
        self.assertEqual([row[0] for row in ratios[2:]], [
            "2026-10-04", "2026-10-03", "2026-10-02", "2026-10-01",
            "2026-09-30", "2026-09-29", "2026-09-28",
        ])
        self.assertEqual(self.cell(ratios, "2026-10-04", "Tests"), "3")
        self.assertEqual(self.cell(ratios, "2026-10-04", "Edits"), "2")
        self.assertEqual(self.cell(ratios, "2026-10-03", "Tests"), "0")
        self.assertEqual(self.cell(ratios, "2026-10-03", "Target"), "—")

    def test_target_labels_below_on_target_and_above_at_two_decimals(self) -> None:
        self.verify_call(0, "tree-0", day="2026-10-01")
        for number in range(1, 9):
            self.verify_call(number, f"tree-{number}", verb="lint", command="lint hana", day="2026-10-01")
        self.verify_call(9, "tree-a", worker="other", day="2026-10-02", minute=0)
        self.verify_call(10, "tree-b", worker="other", day="2026-10-02", minute=5)
        self.verify_call(11, "tree-x", worker="third", day="2026-10-03", minute=0)
        self.verify_call(12, "tree-y", worker="third", day="2026-10-03", verb="lint", command="lint hana", minute=5)
        self.verify_call(13, "tree-z", worker="third", day="2026-10-03", verb="lint", command="lint hana", minute=10)
        ratios, _ = self.per_edit_tables("2026-10-03")
        self.assertEqual(self.cell(ratios, "2026-10-01", "Tests/edit"), "0.12")
        self.assertEqual(self.cell(ratios, "2026-10-01", "Target"), "below")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Target"), "above")
        self.assertEqual(self.cell(ratios, "2026-10-03", "Target"), "on target")

    def test_failure_tracks_edits_since_green_and_minutes_until_next_green(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-b", verb="lint", command="lint hana", minute=5)
        self.verify_call(2, "tree-c", status=1, minute=10)
        self.verify_call(3, "tree-c", minute=35)
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "2–3", "Failures"), "1")
        self.assertEqual(self.cell(bins, "2–3", "Avg to next green"), "25.0 min")

    def test_failure_at_zero_edits_has_recovery_time_in_zero_bin(self) -> None:
        self.verify_call(0, "tree-a", minute=0, command="test hana --filter parser")
        self.verify_call(1, "tree-a", status=1, minute=10)
        self.verify_call(2, "tree-a", minute=25)
        _, bins = self.per_edit_tables()
        self.assertEqual([row[0] for row in bins[2:]], ["0", "1", "2–3", "4–7", "8+"])
        self.assertEqual(self.cell(bins, "0", "Failures"), "1")
        self.assertEqual(self.cell(bins, "0", "Avg to next green"), "15.0 min")
        self.assertNotIn("outside these bins", self.render_calls())

    def test_failed_outcome_without_status_counts_and_reused_green_recovers(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-b", minute=5, status=None, outcome="failed")
        self.verify_call(2, "tree-b", minute=10, status=0, outcome="interrupted")
        self.verify_call(3, "tree-b", minute=25, status=0, outcome="reused")
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "1", "Failures"), "1")
        self.assertEqual(self.cell(bins, "1", "Avg to next green"), "20.0 min")

    def test_nonzero_test_status_counts_as_failure_without_failed_outcome(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-a", minute=5, status=1, outcome="ran")
        self.verify_call(2, "tree-a", minute=15)
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "0", "Failures"), "1")
        self.assertEqual(self.cell(bins, "0", "Avg to next green"), "10.0 min")

    def test_failures_at_three_four_seven_and_eight_edits_use_correct_bins(self) -> None:
        number = 0
        for edits in (3, 4, 7, 8):
            worker = f"worker-{edits}"
            self.verify_call(number, "tree-0", worker=worker, minute=0)
            number += 1
            for changed in range(1, edits):
                self.verify_call(number, f"tree-{changed}", worker=worker, verb="lint", command="lint hana", minute=changed)
                number += 1
            self.verify_call(number, f"tree-{edits}", worker=worker, status=1, minute=edits)
            number += 1
            self.verify_call(number, f"tree-{edits}", worker=worker, minute=edits + 10)
            number += 1
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "1", "Failures"), "0")
        self.assertEqual(self.cell(bins, "2–3", "Failures"), "1")
        self.assertEqual(self.cell(bins, "4–7", "Failures"), "2")
        self.assertEqual(self.cell(bins, "8+", "Failures"), "1")

    def test_failure_without_later_green_counts_but_not_in_average(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-b", verb="lint", command="lint hana", minute=5)
        self.verify_call(2, "tree-c", status=1, minute=10)
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "2–3", "Failures"), "1")
        self.assertEqual(self.cell(bins, "2–3", "Avg to next green"), "")
        text = self.render_calls()
        self.assertIn("1 without a later green", text)

    def test_rendered_tests_per_edit_section_has_two_tables_target_and_source(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-b")
        text = self.render_calls()
        lines = text.splitlines()
        self.assertIn("### Tests per edit", lines)
        section_start = lines.index("### Tests per edit")
        section_end = next((index for index in range(section_start + 1, len(lines)) if lines[index].startswith("### ")), len(lines))
        section = lines[section_start:section_end]
        self.assertEqual(sum(line.startswith("|---") for line in section), 2)
        self.assertIn("Target", "\n".join(section))
        source_lines = [line for line in section if line.startswith("Source:")]
        self.assertEqual(len(source_lines), 2)
        self.assertRegex(source_lines[0], r"target \d")
        self.assertIn("2026-09-26–2026-10-02", source_lines[0])
        self.assertIn("2026-09-26–2026-10-02", source_lines[1])

    def memory_section(self) -> list[str]:
        lines = self.render().splitlines()
        start = lines.index("### Memory pressure")
        end = next((offset for offset in range(start + 1, len(lines)) if lines[offset].startswith("### ")), len(lines))
        return lines[start:end]

    def test_memory_pressure_ranks_five_stalled_steps_and_counts_same_host_overlap(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("s1", caller="verify", started_at="2026-10-02T12:00:00.000Z", ended_at="2026-10-02T12:10:00.000Z", mem_stall_some_s=12.0),
            step("s2", step="mend", started_at="2026-10-02T12:01:00.000Z", ended_at="2026-10-02T12:09:00.000Z", mem_stall_some_s=9.0),
            step("s3", started_at="2026-10-02T12:02:00.000Z", ended_at="2026-10-02T12:08:00.000Z", mem_stall_some_s=8.0),
            step("s4", started_at="2026-10-02T12:03:00.000Z", ended_at="2026-10-02T12:04:00.000Z", mem_stall_some_s=7.0),
            step("s5", started_at="2026-10-02T12:04:00.000Z", ended_at="2026-10-02T12:05:00.000Z", mem_stall_some_s=6.0),
            step("s6", started_at="2026-10-02T12:05:00.000Z", ended_at="2026-10-02T12:06:00.000Z", mem_stall_some_s=5.0),
        )
        self.write(
            self.root / "mac" / "2026-10.jsonl",
            step("other-host", host="mac", started_at="2026-10-02T12:00:00.000Z", ended_at="2026-10-02T12:10:00.000Z"),
        )
        section = self.memory_section()
        self.assertIn("| Caller | Kind | Stall | At once |", section)
        self.assertIn("| verify.sh (agents) | clippy | 12.0 s | 1 |", section)
        self.assertIn("| agent (direct) | mend | 9.0 s | 2 |", section)
        self.assertTrue(any("| 8.0 s | 3 |" in line for line in section))
        self.assertTrue(any("| 7.0 s | 4 |" in line for line in section))
        self.assertTrue(any("| 6.0 s | 4 |" in line for line in section))
        self.assertFalse(any("5.0 s" in line for line in section))
        self.assertEqual(sum(line.startswith("| ") and ("| clippy |" in line or "| mend |" in line) for line in section), 5)
        self.assertLess(self.render().index("### clippy"), self.render().index("### Memory pressure"))

    def test_memory_pressure_labels_temp_folder_stalls_as_scratch_and_counts_their_overlap(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("scratch", cwd="/tmp/scratch", mem_stall_some_s=30.0, ended_at="2026-10-02T12:10:00.000Z"),
            step("kept", caller="verify", mem_stall_some_s=5.0, ended_at="2026-10-02T12:10:00.000Z"),
        )
        section = self.memory_section()
        self.assertIn("| scratch (temp folders) | clippy | 30.0 s | 2 |", section)
        self.assertIn("| verify.sh (agents) | clippy | 5.0 s | 2 |", section)

    def usage_sample(self, at: str, held: int, *, cpu: int = 0, identity: int = 42,
                     boot: str = "boot-a", free: int = 30 * GIB, tokens: int = 17) -> Record:
        return sample(at, boot_id=boot,
                      slices={"builds": slice_reading(held, cpu=cpu, identity=identity),
                              "ci": slice_reading(40 * GIB, cpu=cpu * 100, identity=99)},
                      filesystem={"state": "measured", "total": 100 * GIB, "used": 60 * GIB,
                                  "free": free, "device": "254:0", "device_name": "dm-0"},
                      jobserver={"builds": {"state": "measured", "free_pool_tokens": tokens},
                                 "ci": {"state": "unavailable"}})

    def write_usage(self, *records: Record) -> None:
        self.write(self.root / "natedev/samples-2026-10.jsonl", *records)

    def usage_cells(self, text: str) -> list[dict[str, str]]:
        table = [line for line in text.splitlines() if line.startswith("|")]
        self.assertGreaterEqual(len(table), 3)
        return self.table_cells(table[0], table[2:])

    def table_cells(self, heading: str, rows: list[str]) -> list[dict[str, str]]:
        headers = [cell.strip() for cell in heading.strip("|").split("|")]
        return [dict(zip(headers, [cell.strip() for cell in line.strip("|").split("|")], strict=True))
                for line in rows]

    def usage_text(self, day: str) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.object(store, "host_name", return_value="natedev"):
            return report.usage_hours(connection, day)

    def test_pressure_interval_counts_only_in_the_day_it_ends(self) -> None:
        self.write_usage(
            sample("2026-10-01T23:59:00Z", stall_some_us=10_000_000, stall_full_us=2_000_000,
                   cpu_stall_some_us=10_000_000),
            sample("2026-10-02T00:01:00Z", stall_some_us=12_000_000, stall_full_us=3_000_000,
                   cpu_stall_some_us=40_000_000))
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.dict(os.environ, {"USER_ZONE": "UTC", "TZ": "UTC"}):
            time.tzset()
            try:
                before = report.memory_pressure_section(connection, "2026-10-01", 0)
                after = report.memory_pressure_section(connection, "2026-10-02", 0)
                cpu_before = report.cpu_disk_pressure_section(connection, "2026-10-01")
                cpu_after = report.cpu_disk_pressure_section(connection, "2026-10-02")
            finally:
                self.addCleanup(time.tzset)
        self.assertTrue(any("some unavailable" in line and "full unavailable" in line for line in before))
        self.assertTrue(any("some 2.0 s" in line and "full 1.0 s" in line for line in after))
        self.assertIn("CPU and IO samples: none.", cpu_before)
        self.assertTrue(any("CPU some 25.0%" in line for line in cpu_after))

    def test_held_components_average_their_own_readings_without_current(self) -> None:
        first = self.usage_sample("2026-10-02T12:00:00Z", 8 * GIB)
        second = self.usage_sample("2026-10-02T13:00:00Z", 24 * GIB)
        cast(Record, cast(Record, second["slices"])["builds"])["current_bytes"] = "unavailable"
        self.write_usage(first, second)
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            line = report.memory_held_line(connection, "2026-10-02")
        self.assertIn("average 8.0 GiB", line)
        self.assertIn("`anon` 4.0 GiB", line)
        self.assertIn("`file` 8.0 GiB", line)
        self.assertIn("`inactive_file` 4.0 GiB", line)

    def test_usage_on_mac_selects_natedev_samples_and_counters(self) -> None:
        self.write_usage(self.usage_sample("2026-10-02T12:00:00Z", 12 * GIB),
                         self.usage_sample("2026-10-02T12:01:00Z", 20 * GIB, cpu=120_000_000))
        self.write(self.root / "mac/samples-2026-10.jsonl",
                   sample("2026-10-02T12:00:00Z", host="mac", slices={"builds": slice_reading(40 * GIB)}))
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.object(store, "host_name", return_value="mac"), \
             mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            row = next(row for row in self.usage_cells(report.usage_hours(connection, "2026-10-02"))
                       if "12:00" in row["Hour"])
        self.assertEqual(row["Average GiB held"], "16.0 (2)")
        self.assertEqual(row["Builds CPU cores"], "2.00 (1)")

    def test_sample_without_snapshot_keeps_memory_disk_tokens_and_counters(self) -> None:
        self.write_usage(self.usage_sample("2026-10-02T12:00:00Z", 12 * GIB),
                         self.usage_sample("2026-10-02T12:01:00Z", 20 * GIB, cpu=120_000_000,
                                           free=25 * GIB, tokens=9))
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM memory_snapshots").fetchone(), (0,))
            line = report.memory_held_line(connection, "2026-10-02")
            rows = self.usage_cells(report.usage_hours(connection, "2026-10-02"))
        row = next(row for row in rows if "12:00" in row["Hour"])
        self.assertIn("average 16.0 GiB", line)
        self.assertEqual(row["Average GiB held"], "16.0 (2)")
        self.assertEqual(row["Builds CPU cores"], "2.00 (1)")
        self.assertEqual(row["Lowest free disk GiB"], "25.0 (2)")
        self.assertEqual(row["Builds free pool tokens"], "13.0 (2)")

    def test_usage_window_edges_filter_and_bucket_the_same_instant(self) -> None:
        self.write_usage(self.usage_sample("2026-10-01T23:59:59.999999Z", 40 * GIB),
                         self.usage_sample("2026-10-02T00:00:00Z", 8 * GIB),
                         self.usage_sample("2026-10-02T23:59:59.999999Z", 12 * GIB),
                         self.usage_sample("2026-10-03T00:00:00Z", 40 * GIB))
        with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            rows = self.usage_cells(self.usage_text("2026-10-02"))
        measured = [row for row in rows if row["Samples"] != "0"]
        self.assertEqual([(row["Hour"], row["Average GiB held"]) for row in measured],
                         [("2026-10-02 00:00 +0000", "8.0 (1)"), ("2026-10-02 23:00 +0000", "12.0 (1)")])

    def test_memory_held_averages_current_charge_without_peaks_children_or_swap(self) -> None:
        self.write_usage(
                   self.usage_sample("2026-10-02T12:00:00Z", 12 * GIB),
                   self.usage_sample("2026-10-02T12:01:00Z", 20 * GIB),
                   sample("2026-10-02T12:02:00Z", builds_anon_bytes=100 * GIB))
        self.write(self.root / "natedev/2026-10.jsonl", step("peak", peak_mem_bytes=1000 * GIB))
        with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}), \
             mock.patch.object(store, "host_name", return_value="natedev"):
            lines = self.render("2026-10-02").splitlines()
        line = next(line for line in lines if line.startswith("Build memory held ("))
        self.assertEqual(line, "Build memory held (60 s samples, last 24 h): average 16.0 GiB "
                         + "(`anon` 4.0 GiB, `file` 8.0 GiB, of which `inactive_file` 4.0 GiB) "
                         + "of the 44.0 GiB cap; busiest hour 12:00 at 16.0 GiB.")

    def test_memory_held_weights_samples_instead_of_hour_means(self) -> None:
        self.write_usage(self.usage_sample("2026-10-02T12:00:00Z", 12 * GIB),
                         self.usage_sample("2026-10-02T12:01:00Z", 20 * GIB),
                         self.usage_sample("2026-10-02T13:00:00Z", 4 * GIB))
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}), \
             mock.patch.object(store, "host_name", return_value="natedev"):
            line = report.memory_held_line(connection, "2026-10-02")
        self.assertIn("average 12.0 GiB", line)
        self.assertIn("busiest hour 12:00 at 16.0 GiB", line)

    def test_memory_held_old_rows_and_missing_current_are_not_zero_readings(self) -> None:
        self.write_usage(
                   sample(STAMP, builds_anon_bytes=8 * GIB),
                   sample("2026-10-02T12:01:00Z", slices={"builds": {"state": "present", "peak_bytes": 44 * GIB}}))
        self.assertIn("Build memory held: no readings yet.", self.render("2026-10-02"))

    def test_usage_hours_counts_missing_components_separately_and_keeps_cap(self) -> None:
        first = self.usage_sample("2026-10-02T12:00:00Z", 12 * GIB, tokens=10)
        second = self.usage_sample("2026-10-02T12:01:00Z", 20 * GIB, tokens=20)
        builds = cast(Record, cast(Record, second["slices"])["builds"])
        del cast(Record, builds["stat"])["anon"]
        second["jobserver"] = {"builds": {"state": "unavailable"}, "ci": {"state": "unavailable"}}
        self.write_usage(first, second)
        with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            text = self.usage_text("2026-10-02")
        row = next(row for row in self.usage_cells(text) if "2026-10-02 12:00" in row["Hour"])
        self.assertEqual(row["Average GiB held"], "16.0 (2)")
        self.assertEqual(row["Processes GiB"], "3.0 (1)")
        self.assertEqual(row["File cache GiB"], "8.0 (2)")
        self.assertEqual(row["Builds free pool tokens"], "10.0 (1)")
        self.assertEqual(row["Samples"], "2")

    def test_usage_hours_separates_repeated_local_hours_by_utc_offset(self) -> None:
        self.write_usage(
                   self.usage_sample("2026-11-01T05:30:00Z", 12 * GIB),
                   self.usage_sample("2026-11-01T06:30:00Z", 20 * GIB))
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            text = self.usage_text("2026-11-01")
        rows = [line for line in text.splitlines() if "2026-11-01 01:00" in line]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("-04:00" in row or "-0400" in row for row in rows))
        self.assertTrue(any("-05:00" in row or "-0500" in row for row in rows))
        self.assertTrue(any("12.0" in row for row in rows))
        self.assertTrue(any("20.0" in row for row in rows))

    def test_usage_hours_splits_cpu_and_top_device_traffic_at_hour_edge(self) -> None:
        self.write_usage(
                   self.usage_sample("2026-10-02T12:59:30Z", 12 * GIB, cpu=0),
                   self.usage_sample("2026-10-02T13:00:30Z", 12 * GIB, cpu=120_000_000,
                                     free=25 * GIB, tokens=9))
        with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            text = self.usage_text("2026-10-02")
        rows = [row for row in self.usage_cells(text) if "2026-10-02 12:00" in row["Hour"] or "2026-10-02 13:00" in row["Hour"]]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["Builds CPU cores"], "2.00 (1)")
            self.assertEqual(row["Builds read bytes"], "60000000.00 (1)")
            self.assertEqual(row["Builds written bytes"], "120000000.00 (1)")
        self.assertEqual(rows[1]["Lowest free disk GiB"], "25.0 (1)")

    def test_usage_hours_unknown_intervals_never_become_zero_cpu(self) -> None:
        cases = (("same instant", 0, "boot-a", 42, 200), ("reset", 60, "boot-a", 42, 50), ("boot", 60, "boot-b", 42, 200),
                 ("cgroup", 60, "boot-a", 43, 200),
                 ("gap", report.MAX_SAMPLE_GAP_S + 1, "boot-a", 42, 200))
        for name, elapsed, boot, identity, counter in cases:
            with self.subTest(case=name):
                start = datetime(2026, 10, 2, 12, tzinfo=UTC)
                self.write_usage(
                           self.usage_sample(start.isoformat(), 12 * GIB, cpu=100),
                           self.usage_sample((start + timedelta(seconds=elapsed)).isoformat(), 12 * GIB,
                                             cpu=counter, identity=identity, boot=boot))
                with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
                    text = self.usage_text("2026-10-02")
                row = next(row for row in self.usage_cells(text) if "2026-10-02 12:00" in row["Hour"])
                self.assertEqual(row["Builds CPU cores"], "— (0)")
                self.assertEqual(row["Builds read bytes"], "— (0)")
                self.assertEqual(row["Builds written bytes"], "— (0)")

    def test_memory_held_rolling_window_ends_now_and_dated_window_ends_local_day(self) -> None:
        self.write_usage(
                   self.usage_sample("2026-10-01T11:59:59Z", 40 * GIB),
                   self.usage_sample("2026-10-01T12:00:01Z", 8 * GIB),
                   self.usage_sample("2026-10-02T11:59:59Z", 12 * GIB),
                   self.usage_sample("2026-10-02T12:00:01Z", 40 * GIB))
        _ = index.update()
        with closing(index.read_only()) as connection, mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}), \
             mock.patch.object(store, "host_name", return_value="natedev"):
            with mock.patch.object(report, "datetime", wraps=datetime,
                                   now=mock.Mock(return_value=datetime(2026, 10, 2, 12, tzinfo=UTC))):
                line = report.memory_held_line(connection)
            dated = report.memory_held_line(connection, "2026-10-02")
        self.assertIn("average 10.0 GiB", line)
        self.assertIn("average 26.0 GiB", dated)

    def test_cpu_disk_pressure_follows_memory_and_reports_two_hosts(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                   sample(STAMP, cpu_stall_some_us=10_000_000, io_stall_some_us=1_000_000,
                          io_stall_full_us=500_000, cpu_idle_ticks=1000, cpu_iowait_ticks=10, cpu_total_ticks=2000),
                   sample("2026-10-02T12:01:00.000Z", cpu_stall_some_us=34_720_000,
                          io_stall_some_us=2_860_000, io_stall_full_us=1_100_000,
                          cpu_idle_ticks=6200, cpu_iowait_ticks=50, cpu_total_ticks=12000))
        self.write(self.root / "mac" / "samples-2026-10.jsonl",
                   sample("2026-10-02T12:00:30.000Z", host="mac", cpu_stall_some_us=80_000_000,
                          cpu_stall_full_us=10_000_000, io_stall_some_us=5_000_000, io_stall_full_us=2_000_000,
                          cpu_idle_ticks=100, cpu_iowait_ticks=20, cpu_total_ticks=200),
                   sample("2026-10-02T12:02:30.000Z", host="mac", cpu_stall_some_us=92_000_000,
                          cpu_stall_full_us=16_000_000, io_stall_some_us=5_000_000, io_stall_full_us=2_000_000,
                          cpu_idle_ticks=1700, cpu_iowait_ticks=220, cpu_total_ticks=2200))
        text = self.render()
        headings = [line for line in text.splitlines() if line.startswith("### ")]
        self.assertEqual(headings[headings.index("### Memory pressure") + 1], "### CPU and disk pressure")
        section = self.section_lines(text, "### CPU and disk pressure")
        self.assertIn("Source: 60 s machine samples (/proc/pressure/cpu, /proc/pressure/io, /proc/stat).", section)
        self.assertIn("natedev: CPU some 41.2%, full —; IO some 3.1%, full 1.0%; CPU idle 52.0%, iowait 0.4% (of 1.0 min sampled)", section)
        self.assertIn("mac: CPU some 10.0%, full 5.0%; IO some 0.0%, full 0.0%; CPU idle 80.0%, iowait 10.0% (of 2.0 min sampled)", section)

    def test_cpu_disk_pressure_does_not_count_across_a_reboot(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                   sample(STAMP, cpu_stall_some_us=99_000_000, io_stall_some_us=9_000_000,
                          cpu_idle_ticks=9000, cpu_iowait_ticks=900, cpu_total_ticks=10000),
                   sample("2026-10-02T12:01:00.000Z", boot_id="new-boot", cpu_stall_some_us=30_000_000,
                          io_stall_some_us=6_000_000, cpu_idle_ticks=200, cpu_iowait_ticks=20, cpu_total_ticks=1000))
        section = self.section_lines(self.render(), "### CPU and disk pressure")
        self.assertIn("CPU and IO samples: none.", section)

    def test_cpu_disk_pressure_long_gap_is_not_sampled_time(self) -> None:
        start = datetime.fromisoformat(STAMP)
        after_gap = start + timedelta(seconds=report.MAX_SAMPLE_GAP_S + 1)
        end = after_gap + timedelta(seconds=60)
        self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                   sample(STAMP, cpu_stall_some_us=0),
                   sample(after_gap.isoformat(), cpu_stall_some_us=900_000_000),
                   sample(end.isoformat(), cpu_stall_some_us=915_000_000))
        section = self.section_lines(self.render(), "### CPU and disk pressure")
        self.assertIn("natedev: CPU some 25.0%, full —; IO some —, full —; CPU idle —, iowait — (of 1.0 min sampled)", section)

    def test_cpu_disk_pressure_includes_maximum_gap_and_previous_day_baseline(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "UTC"}):
            time.tzset()
            midnight = datetime(2026, 10, 2, tzinfo=UTC)
            before = midnight - timedelta(seconds=report.MAX_SAMPLE_GAP_S)
            self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                       sample(before.isoformat(), cpu_stall_some_us=1_000_000),
                       sample(midnight.isoformat(), cpu_stall_some_us=1_000_000 + report.MAX_SAMPLE_GAP_S * 250_000))
            section = self.section_lines(self.render("2026-10-02"), "### CPU and disk pressure")
        self.assertIn("natedev: CPU some 25.0%, full —; IO some —, full —; CPU idle —, iowait — (of 5.0 min sampled)", section)

    def test_cpu_disk_pressure_null_pairs_do_not_dilute_individual_shares(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                   sample(STAMP),
                   sample("2026-10-02T12:01:00.000Z", cpu_stall_some_us=100_000_000,
                          io_stall_some_us=10_000_000, cpu_idle_ticks=100, cpu_iowait_ticks=10, cpu_total_ticks=1000),
                   sample("2026-10-02T12:02:00.000Z", cpu_stall_some_us=130_000_000,
                          io_stall_some_us=16_000_000, cpu_idle_ticks=600, cpu_iowait_ticks=None, cpu_total_ticks=2000),
                   sample("2026-10-02T12:03:00.000Z", cpu_stall_some_us=None,
                          io_stall_some_us=28_000_000, cpu_idle_ticks=2600, cpu_iowait_ticks=30, cpu_total_ticks=6000),
                   sample("2026-10-02T12:04:00.000Z", cpu_stall_some_us=200_000_000,
                          io_stall_some_us=None, cpu_idle_ticks=2700, cpu_iowait_ticks=40, cpu_total_ticks=None))
        section = self.section_lines(self.render(), "### CPU and disk pressure")
        self.assertIn("natedev: CPU some 50.0%, full —; IO some 15.0%, full —; CPU idle 50.0%, iowait — (of 2.0 min sampled)", section)

    def test_cpu_disk_pressure_empty_old_and_single_samples_have_no_pairs(self) -> None:
        section = self.section_lines(self.render(), "### CPU and disk pressure")
        self.assertIn("CPU and IO samples: none.", section)
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", sample(STAMP),
                   sample("2026-10-02T12:01:00.000Z"))
        self.write(self.root / "mac" / "samples-2026-10.jsonl",
                   sample(STAMP, host="mac", cpu_stall_some_us=30_000_000))
        section = self.section_lines(self.render(), "### CPU and disk pressure")
        self.assertIn("CPU and IO samples: none.", section)
        self.assertFalse(any(line.startswith(("natedev:", "mac:")) for line in section))

    def test_cpu_ticks_without_psi_still_report_host(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl",
                   sample(STAMP, cpu_idle_ticks=100, cpu_iowait_ticks=10, cpu_total_ticks=200),
                   sample("2026-10-02T12:01:00.000Z", cpu_idle_ticks=500, cpu_iowait_ticks=20, cpu_total_ticks=1200))
        section = self.section_lines(self.render(), "### CPU and disk pressure")
        self.assertIn("natedev: CPU some —, full —; IO some —, full —; CPU idle 40.0%, iowait 1.0% (of 0.0 s sampled)", section)

    def test_memory_pressure_uses_sample_peaks_and_reboot_counter_deltas(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:00:00.000Z", mem_used_bytes=4 * 2**30, swap_used_bytes=2 * 2**30, stall_some_us=10_000_000, stall_full_us=2_000_000, builds_anon_bytes=2 * 2**30, ci_anon_bytes=3 * 2**30),
            sample("2026-10-02T12:01:00.000Z", mem_used_bytes=8 * 2**30, swap_used_bytes=7 * 2**30, stall_some_us=12_000_000, stall_full_us=3_000_000, builds_anon_bytes=4 * 2**30, ci_anon_bytes=5 * 2**30),
            sample("2026-10-02T12:02:00.000Z", boot_id="boot-b", mem_used_bytes=6 * 2**30, swap_used_bytes=5 * 2**30, stall_some_us=500_000, stall_full_us=100_000),
            sample("2026-10-02T12:03:00.000Z", boot_id="boot-b", mem_used_bytes=5 * 2**30, swap_used_bytes=4 * 2**30, stall_some_us=1_500_000, stall_full_us=500_000),
        )
        section = self.memory_section()
        self.assertTrue(any("Source:" in line and "60 s" in line and "step" in line and "stall" in line for line in section))
        self.assertTrue(any("8.0 GiB" in line and "7.0 GiB" in line for line in section))
        self.assertTrue(any("3.0 s" in line and "1.4 s" in line for line in section))
        self.assertTrue(any("peak builds 4.0 GiB, peak CI 5.0 GiB (process memory)" in line for line in section))
        self.assertIn("memory waits: none", section)

    def test_memory_pressure_sums_admission_waits_and_names_longest_caller(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl", step("s1", mem_wait_s=12, caller="verify"), step("s2", mem_wait_s=4))
        section = self.memory_section()
        self.assertIn("memory waits: 2 steps, total 16.0 s, longest 12.0 s (verify.sh (agents))", section)

    def test_memory_pressure_samples_without_stalled_steps_have_no_table(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", sample(STAMP))
        section = self.memory_section()
        self.assertFalse(any(line.startswith("|") for line in section))
        self.assertTrue(any("4.0 GiB" in line and "2.0 GiB" in line for line in section))
        self.assertTrue(any("some unavailable" in line and "full unavailable" in line for line in section))

    def test_memory_pressure_compares_adjacent_samples_per_host(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:00:00.000Z", stall_some_us=20_000_000, stall_full_us=5_000_000),
            sample("2026-10-02T12:02:00.000Z", stall_some_us=21_000_000, stall_full_us=5_200_000),
        )
        self.write(
            self.root / "mac" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:01:00.000Z", host="mac", stall_some_us=40_000_000, stall_full_us=8_000_000),
            sample("2026-10-02T12:03:00.000Z", host="mac", stall_some_us=42_000_000, stall_full_us=8_300_000),
        )
        section = self.memory_section()
        self.assertTrue(any("some 3.0 s" in line and "full 0.5 s" in line for line in section))

    def test_memory_pressure_ignores_counter_rise_across_a_long_sampling_gap(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-01T12:00:00.000Z", stall_some_us=10_000_000, stall_full_us=2_000_000),
            sample(STAMP, stall_some_us=12_000_000, stall_full_us=3_000_000),
        )
        section = self.memory_section()
        self.assertTrue(any("some unavailable" in line and "full unavailable" in line for line in section))

    def test_memory_pressure_counts_counter_rise_across_midnight_when_samples_are_close(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "UTC"}):
            time.tzset()
            self.write(
                self.root / "natedev" / "samples-2026-10.jsonl",
                sample("2026-10-01T23:59:00.000Z", stall_some_us=10_000_000, stall_full_us=2_000_000),
                sample("2026-10-02T00:01:00.000Z", stall_some_us=12_000_000, stall_full_us=3_000_000),
            )
            _ = index.update()
            with closing(index.read_only()) as connection:
                section = report.memory_pressure_section(connection, "2026-10-02", 0)
        self.assertTrue(any("some 2.0 s" in line and "full 1.0 s" in line for line in section))

    def test_memory_pressure_empty_day_shows_instrument_availability(self) -> None:
        lines = self.render().splitlines()
        self.assertIn("### Memory pressure", lines)
        self.assertIn("memory waits: none", lines)
        self.assertIn("sccache service: unavailable", lines)
        self.assertIn("unsliced steps: none", lines)
        self.assertIn("memory kills: none", lines)

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
        self.assertIn("| verify.sh (agents) | 1 | 0 | 40.0 s | 40.0 s | 40.0 s | 3.0 s | 3.0 s | 37.0 s | 37.0 s | 0 |", lines)
        self.assertIn("| cargo-port | 2 | 1 | 15.0 s | 20.0 s | 10.0 s – 20.0 s | 4.0 s | 4.0 s | 6.0 s | 6.0 s |  |", lines)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| clippy | 1 | 1.0 s | 1.0 s | 1.0 s |  |", successes)
        self.assertIn("| mend | 2 | 50.0 s | 25.0 s | 40.0 s |  |", successes)
        self.assertIn("| **All steps** | 3 | 51.0 s | 17.0 s | 40.0 s |  |", successes)
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| clippy | 1 | 30.0 s | 30.0 s | 30.0 s |  |", failures)
        self.assertIn("| mend | 1 | 20.0 s | 20.0 s | 20.0 s |  |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| mend | 3 | 1 | 1.2 min | 23.3 s | 40.0 s |  |", combined)
        self.assertIn("| **All steps** | 5 | 2 | 1.7 min | 20.2 s | 40.0 s |  |", combined)
        self.assertIn("| reused | 1 |  | 1.0 min |", lines)
        self.assertIn("| CI | 1 | 0 | 0 | 10.0 min | 10.0 min | 10.0 min |", lines)
        self.assertIn("| scratch (temp folders) | 1 | 0 | 1.0 s | 1.0 s | 1.0 s |  |  |  |  |", lines)
        self.assertNotIn("steps under a temp folder", text)

    def test_mac_launch_in_temp_folder_leaves_natedev_caller_without_host(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("local", host="natedev", caller="verify", step="check"),
        )
        self.write(
            self.root / "mac" / "2026-10.jsonl",
            step("launch", host="mac", caller="brp-launch", step="build", cwd="/tmp/app"),
        )

        lines = self.render().splitlines()
        self.assertTrue(any(line.startswith("| verify.sh (agents) |") for line in lines))
        self.assertFalse(any("(natedev)" in line for line in lines))
        self.assertNotIn("### build", lines)
        mac = self.section_lines(self.render(), "### Mac")
        self.assertTrue(any(line.startswith("| example launches (brp) | build | 1 |") for line in mac))

    def test_ci_queue_summary_when_all_queue_times_are_known(self) -> None:
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z"))]),
        )
        summary = next(line for line in self.render().splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "jobs queued 30.0 s on average, p95 30.0 s; CI never polled.")

    def test_ci_queue_p95_uses_the_same_jobs_as_average(self) -> None:
        at = datetime.fromisoformat("2026-10-02T12:00:00+00:00")
        jobs = [
            ci_job(number, f"Job {number}", "success", (
                at.isoformat(),
                (at + timedelta(seconds=wait)).isoformat(),
                (at + timedelta(seconds=wait + 1)).isoformat(),
            ))
            for number, wait in enumerate((10, 20, 30), 1)
        ]
        self.write(self.root / "ci" / "2026-10.jsonl", ci_run(1, 1, jobs))
        self.assertIn("jobs queued 20.0 s on average, p95 30.0 s", self.ci_line("2026-10-02"))

    def test_ci_queue_summary_uses_run_attempt_day_for_job_created_after_midnight(self) -> None:
        first_start = datetime(2026, 10, 1, 12).astimezone().isoformat()
        first_job_start = datetime(2026, 10, 1, 12, 0, 10).astimezone().isoformat()
        first_end = datetime(2026, 10, 1, 12, 2).astimezone().isoformat()
        rerun_start = datetime(2026, 10, 2, 23, 59).astimezone().isoformat()
        job_created = datetime(2026, 10, 3, 0, 1).astimezone().isoformat()
        job_started = datetime(2026, 10, 3, 0, 1, 30).astimezone().isoformat()
        job_completed = datetime(2026, 10, 3, 0, 3).astimezone().isoformat()
        first = ci_run(1, 1, [ci_job(11, "Test", "success", (first_start, first_job_start, first_end))])
        first.update({"created_at": first_start, "started_at": first_start, "updated_at": first_end})
        rerun = ci_run(1, 2, [ci_job(21, "Test", "success", (job_created, job_started, job_completed))])
        rerun.update({"created_at": rerun_start, "started_at": rerun_start, "updated_at": job_completed})
        self.write(self.root / "ci" / "2026-10.jsonl", first, rerun)

        summary = next(line for line in self.render("2026-10-02").splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "jobs queued 30.0 s on average, p95 30.0 s; CI never polled.")
        self.assertEqual(self.waiting_rows()["CI queue"][2], "1 of 1 jobs")
        self.assertIn("CI: no runs; CI never polled.", self.render("2026-10-03"))
        self.assertEqual(self.waiting_rows("2026-10-03")["CI queue"], ["none", "", "", "", ""])

    def test_ci_queue_summary_counts_unknown_times_but_not_skipped_jobs_as_left_out(self) -> None:
        original = ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")
        carried = ("2026-10-02T12:20:00Z", original[1], original[2])
        skipped = ("2026-10-02T12:21:00Z", "2026-10-02T12:21:00Z", "2026-10-02T12:20:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", original)]),
            ci_run(1, 2, [
                ci_job(21, "Test", "success", carried),
                ci_job(22, "Skipped", "skipped", skipped),
                ci_job(23, "Other", "success", ("2026-10-02T12:22:00Z", "unknown", "unknown")),
            ]),
        )
        summary = next(line for line in self.render().splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "jobs queued 30.0 s on average, p95 30.0 s, 2 without a known queue time left out; CI never polled.")

    def test_ci_queue_summary_when_no_job_has_a_known_queue_time(self) -> None:
        original = ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")
        carried = ("2026-10-03T12:20:00Z", original[1], original[2])
        rerun = ci_run(1, 2, [ci_job(21, "Test", "success", carried)])
        rerun.update({"created_at": "2026-10-03T12:20:00Z", "started_at": "2026-10-03T12:20:00Z", "updated_at": "2026-10-03T12:30:00Z"})
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", original)]),
            rerun,
        )
        summary = next(line for line in self.render("2026-10-03").splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "no job has a known queue time (1 left out); CI never polled.")

    def test_ci_queue_summary_with_only_skipped_jobs_has_no_left_out_suffix(self) -> None:
        skipped = ("2026-10-02T12:01:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:00:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Skipped", "skipped", skipped)]),
        )
        summary = next(line for line in self.render().splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "no job has a known queue time; CI never polled.")

    def test_scratch_steps_form_one_caller_per_kind_and_count_toward_peak_memory(self) -> None:
        gib = 1073741824
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("regular", step="fmt", duration_s=30.0, peak_mem_bytes=gib),
            step("scratch-tmp", step="fmt", cwd="/tmp/agent/crate", caller="verify", duration_s=10.0, peak_mem_bytes=2 * gib),
            step("scratch-private", step="doc", cwd="/private/var/folders/agent", status=101, peak_mem_bytes=4 * gib),
        )
        self.write(
            self.root / "macbook" / "2026-10.jsonl",
            step("scratch-mac", host="macbook", step="fmt", cwd="/var/folders/agent", caller="alias", duration_s=20.0),
        )

        lines = self.render().splitlines()
        fmt = lines[lines.index("### fmt") : lines.index("### doc")]
        self.assertIn("| scratch (temp folders) | 1 | 0 | 10.0 s | 10.0 s | 10.0 s |", fmt)
        self.assertEqual(1, sum(line.startswith("| scratch (temp folders) |") for line in fmt))
        doc = lines[lines.index("### doc") : lines.index("### Summary: successes")]
        self.assertIn("| scratch (temp folders) | 1 | 1 | 10.0 s | 10.0 s | 10.0 s |  |  |  |", doc)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| fmt | 2 | 40.0 s | 20.0 s | 30.0 s | 2.0 GiB |", successes)
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| doc | 1 | 10.0 s | 10.0 s | 10.0 s | 4.0 GiB |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| **All steps** | 3 | 1 | 50.0 s | 16.7 s | 30.0 s | 4.0 GiB |", combined)
        mac = self.section_lines(self.render(), "### Mac")
        self.assertTrue(any(line.startswith("| terminal | fmt | 1 | 0 | 20.0 s |") for line in mac))

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
        self.assertIn("CI: no runs; CI never polled.", text)
        self.assertNotIn("cargo-port calls", text)

    def test_disk_section_precedes_summaries_with_steps(self) -> None:
        unit = 2**30
        snapshot: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [
                {"label": "~/rust", "bytes": unit},
                {"label": "/tmp", "bytes": 2 * unit},
                {"label": "CI runner 1", "bytes": unit},
                {"label": "CI runner 2", "bytes": unit},
            ],
            "used": 10 * unit,
            "free": 7 * unit,
            "floor": 500 * unit,
        }
        self.write(self.root / "natedev" / "2026-10.jsonl", step("one"))

        with mock.patch("report.disk.read_snapshot", return_value=snapshot) as read_snapshot:
            with mock.patch("sync.sync_time", return_value="12:14 EDT"):
                lines = self.render().splitlines()

        self.assertLess(lines.index("### Disk: natedev"), lines.index("### Summary: successes"))
        section = lines[lines.index("### Disk: natedev") : lines.index("### Summary: successes")]
        self.assertEqual(
            [
                "### Disk: natedev",
                "",
                "| Where | Size |",
                "|---|--:|",
                "| ~/rust | 1.0 GiB |",
                "| /tmp | 2.0 GiB |",
                "| CI runner 1 | 1.0 GiB |",
                "| CI runner 2 | 1.0 GiB |",
                "| other | 5.0 GiB |",
                "| free (floor 500.0 GiB) | 7.0 GiB |",
                "",
                "Measured by the buildlog disk job at 12:14 EDT: allocated blocks, each hard-linked file once.",
                "",
            ],
            section,
        )
        read_snapshot.assert_called_once_with()

    def test_disk_section_precedes_empty_summary_without_floor(self) -> None:
        snapshot: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [
                {"label": "~/rust", "bytes": 0},
                {"label": "/tmp", "bytes": 0},
                {"label": "CI runner 1", "bytes": 0},
                {"label": "CI runner 2", "bytes": 0},
            ],
            "used": 0,
            "free": 2**30,
            "floor": None,
        }
        with mock.patch("report.disk.read_snapshot", return_value=snapshot):
            lines = self.render().splitlines()

        self.assertLess(lines.index("### Disk: natedev"), lines.index("### Summary"))
        self.assertIn("| free | 1.0 GiB |", lines)
        self.assertNotIn("free (floor", "\n".join(lines))

    def test_disk_section_clamps_other_when_rows_exceed_used(self) -> None:
        unit = 2**30
        snapshot: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [{"label": "~/rust", "bytes": 30 * unit}],
            "used": 10 * unit,
            "free": unit,
            "floor": None,
        }
        with mock.patch("report.disk.read_snapshot", return_value=snapshot):
            lines = report.disk_section()

        self.assertIn("| other | 0.0 GiB |", lines)
        self.assertNotIn("| other | -20.0 GiB |", lines)

    def test_disk_table_reads_old_and_new_snapshot_shapes(self) -> None:
        base: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [{"label": "/tmp", "bytes": 2**30}],
            "used": 2 * 2**30,
            "free": 3 * 2**30,
            "floor": None,
        }
        disk.write_snapshot(base)
        old_lines = report.disk_section()
        expanded: disk.DiskSnapshot = {
            **base,
            "previous_measured_at": "2026-10-04T16:04:00+00:00",
            "outside_build_caches": [{
                "path": "/tmp/traces", "bytes": 2**30, "growth_bytes": 2**30,
                "largest_child_path": None, "largest_child_growth_bytes": None,
            }],
            "outside_build_cache_totals": [
                {"label": "/tmp", "bytes": 2**30, "growth_bytes": 2**30},
            ],
        }
        disk.write_snapshot(expanded)
        self.assertEqual(report.disk_section(), old_lines)
        self.assertIn("| /tmp | 1.0 GiB |", old_lines)

    def test_no_disk_section_without_snapshot(self) -> None:
        with mock.patch("report.disk.read_snapshot", return_value=None):
            lines = self.render().splitlines()

        self.assertFalse(any(line.startswith("### Disk:") for line in lines))
        self.assertIn("### Summary", lines)

    def test_sync_time_names_its_zone_and_an_earlier_day(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/New_York", "USER_ZONE": "America/New_York"}):
            time.tzset()
            now = datetime.fromisoformat("2026-10-03T15:00:00-04:00")
            self.assertEqual(sync.sync_time("2026-10-03T16:14:00+00:00", now), "12:14 EDT")
            self.assertEqual(sync.sync_time("2026-10-02T16:14:00+00:00", now), "2026-10-02 12:14 EDT")

    def test_nearest_rank_matches_p95_boundaries_and_single_value(self) -> None:
        self.assertEqual(report.nearest_rank(list(range(1, 21)), 95), 19)
        self.assertEqual(report.nearest_rank(list(range(1, 22)), 95), 20)
        self.assertEqual(report.nearest_rank([37], 95), 37)
        self.assertEqual(report.nearest_rank([360, 720, 1080, 1440], 75), 1080)

    def test_ci_freshness_for_today_and_a_past_day(self) -> None:
        now = datetime.now(UTC)
        today = now.astimezone().date().isoformat()
        yesterday = (now.astimezone().date() - timedelta(days=1)).isoformat()
        old = now - timedelta(hours=3)
        self.poll_stamp(old)
        stale = self.ci_line(today)
        self.assertTrue(stale.startswith("CI: no runs; "))
        self.assertIn(f"CI recorded through {sync.sync_time(old.isoformat(), now)}, no poll since", stale)

        self.poll_stamp(now - timedelta(minutes=30))
        self.assertEqual(self.ci_line(today), "CI: no runs.")

        self.poll_stamp(now, complete=False)
        capped = self.ci_line(today)
        self.assertIn(f"CI recorded through {sync.sync_time(now.isoformat(), now)}", capped)
        self.assertIn("the last poll was capped", capped)
        self.assertEqual(self.ci_line(yesterday), "CI: no runs.")

    def test_missing_ci_stamp_reports_never_polled(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        self.assertEqual(self.ci_line(today), "CI: no runs; CI never polled.")

    def test_ci_cancellation_count_and_p95_follow_workflow_runs(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        at = datetime.now(UTC).replace(microsecond=0)
        runs: list[Record] = []
        for number, (conclusion, duration) in enumerate((("success", 60), ("failure", 120), ("cancelled", 180)), 1):
            run = ci_run(number, 1, [])
            run.update({
                "conclusion": conclusion,
                "created_at": at.isoformat(),
                "started_at": at.isoformat(),
                "updated_at": (at + timedelta(seconds=duration)).isoformat(),
            })
            runs.append(run)
        self.write(self.root / "ci" / "2026-10.jsonl", *runs)
        lines = self.render(today).splitlines()
        self.assertIn("| Workflow | Runs | Failed | Cancelled | Avg | p95 | Range |", lines)
        self.assertIn("| CI | 3 | 1 | 1 | 2.0 min | 3.0 min | 1.0 min – 3.0 min |", lines)
        summary = next(line for line in lines if line.startswith("CI: "))
        self.assertIn("3 runs, 1 failed, 1 cancelled", summary)

        runs[2]["conclusion"] = "success"
        self.write(self.root / "ci" / "2026-10.jsonl", *runs)
        self.assertNotIn("cancelled", self.ci_line(today))

    def test_pause_note_precedes_last_good_mac_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _ = (self.root / "sync.json").write_text(json.dumps({
            "at": "2026-10-06T10:00:00Z", "peer": "mac", "ok": True,
            "last_ok": "2026-10-06T10:00:00Z",
        }))
        _ = (self.root / "sync_paused.json").write_text(json.dumps({
            "since": "2026-10-06T11:00:00Z", "why": "Mac hold",
        }))
        note = report.mac_note()
        self.assertTrue(note.startswith("Mac: sync paused since "))
        self.assertIn("(Mac hold). Mac rows as of the ", note)

    def test_every_step_kind_and_summary_places_p95_after_average(self) -> None:
        kinds = ("check", "clippy", "doc", "nextest", "mend", "fmt")
        records = [
            step(f"{kind}-{value}", step=kind, duration_s=float(value), finished_s=float(value * 2),
                 mend_s=float(value * 3), mend_check_s=float(value * 4))
            for kind in kinds for value in range(1, 22)
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *records)
        lines = self.render().splitlines()
        for kind in kinds:
            with self.subTest(kind=kind):
                start = lines.index(f"### {kind}")
                head = [cell.strip() for cell in lines[start + 2].strip("|").split("|")]
                row = [cell.strip() for cell in lines[start + 4].strip("|").split("|")]
                self.assertEqual(head[head.index("Avg") + 1], "p95")
                self.assertEqual(row[head.index("Avg")], "11.0 s")
                self.assertEqual(row[head.index("p95")], "20.0 s")
                for title, expected in (("Build", "40.0 s"), ("Mend's own", "1.0 min"), ("Check", "1.3 min")):
                    if title in head:
                        self.assertEqual(head[head.index(title) + 1], f"{title} p95")
                        self.assertEqual(row[head.index(f"{title} p95")], expected)
        for name in ("successes", "failures", "all"):
            start = lines.index(f"### Summary: {name}")
            if name == "failures":
                continue
            head = [cell.strip() for cell in lines[start + 2].strip("|").split("|")]
            self.assertEqual(head[head.index("Avg") + 1], "p95")
            first = [cell.strip() for cell in lines[start + 4].strip("|").split("|")]
            self.assertEqual(first[head.index("p95")], "20.0 s")

    def test_failure_summary_and_edits_since_green_show_p95(self) -> None:
        for number, recovery in enumerate((5, 10, 20)):
            worker = f"worker-{number}"
            base = number * 15
            self.verify_call(number * 3, "tree-a", worker=worker, minute=base)
            self.verify_call(number * 3 + 1, "tree-b", worker=worker, status=1, minute=base + 1)
            self.verify_call(number * 3 + 2, "tree-b", worker=worker, minute=base + 1 + recovery)
        _, bins = self.per_edit_tables()
        self.assertEqual(bins[0][bins[0].index("Avg to next green") + 1], "p95 to next green")
        self.assertEqual(self.cell(bins, "1", "p95 to next green"), "20.0 min")

        self.write(self.root / "natedev" / "2026-10.jsonl", *self.records,
                   step("failed-one", step="fmt", duration_s=10.0, status=1),
                   step("failed-two", step="fmt", duration_s=20.0, status=1))
        lines = self.render().splitlines()
        start = lines.index("### Summary: failures")
        head = [cell.strip() for cell in lines[start + 2].strip("|").split("|")]
        row = [cell.strip() for cell in lines[start + 4].strip("|").split("|")]
        self.assertEqual(head[head.index("Avg") + 1], "p95")
        self.assertEqual(row[head.index("p95")], "20.0 s")


class PowerProfileTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)
        self.addCleanup(time.tzset)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "UTC"})))
        time.tzset()

    def write(self, *records: Record) -> None:
        path = self.root / "natedev" / "profiles.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def changes(self) -> list[report.ProfileChange]:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.profile_changes(connection)

    def section(self, day: str = "2026-10-13") -> list[str]:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.power_profile_section(connection, day)

    def rows(self, lines: list[str]) -> list[list[str]]:
        return [[cell.strip() for cell in line.strip("|").split("|")]
                for line in lines if line.startswith("| ") and not line.startswith("| Kind |")]

    def profiled(self, at: str, profile: str = "performance", since: str | None = None) -> Record:
        return sample(at, power_profile=profile, power_profile_since=since)

    def runs(self, prefix: str, at: str, count: int, duration: float) -> list[Record]:
        return [step(f"{prefix}-{number}", started_at=at, ended_at=at, duration_s=duration,
                     step="nextest", caller="verify", crates_compiled=1)
                for number in range(count)]

    def test_first_profile_uses_since_after_null_samples_in_time_order(self) -> None:
        since = "2026-10-13T12:29:56.000Z"
        self.write(
            self.profiled("2026-10-13T12:31:00.000Z", since=since),
            sample("2026-10-13T12:28:00.000Z"),
            sample("2026-10-13T12:29:00.000Z", power_profile=None, power_profile_since=None),
            self.profiled("2026-10-13T12:30:00.000Z", since=since),
            sample("2026-10-13T12:27:00.000Z", host="macbook", power_profile="balanced"),
        )
        self.assertEqual(self.changes(), [report.ProfileChange(since, "performance")])

    def test_same_profile_rewrite_keeps_original_change_time(self) -> None:
        self.write(
            self.profiled("2026-10-12T12:01:00.000Z", since="2026-10-12T12:00:00.000Z"),
            self.profiled("2026-10-13T12:01:00.000Z", since="2026-10-13T12:00:00.000Z"),
        )
        self.assertEqual(self.changes(), [report.ProfileChange("2026-10-12T12:00:00.000Z", "performance")])

    def test_each_profile_transition_is_recorded_with_sample_time_fallback(self) -> None:
        self.write(
            self.profiled("2026-10-11T12:01:00.000Z", since="2026-10-11T12:00:00.000Z"),
            self.profiled("2026-10-12T12:00:00.000Z", "balanced"),
            self.profiled("2026-10-13T12:01:00.000Z", since="2026-10-13T12:00:00.000Z"),
        )
        self.assertEqual(self.changes(), [
            report.ProfileChange("2026-10-11T12:00:00.000Z", "performance"),
            report.ProfileChange("2026-10-12T12:00:00.000Z", "balanced"),
            report.ProfileChange("2026-10-13T12:00:00.000Z", "performance"),
        ])

    def test_rows_match_kind_caller_and_rebuild_class_with_counts_and_busiest_first(self) -> None:
        before = "2026-10-12T12:00:00.000Z"
        after = "2026-10-13T12:00:00.000Z"
        records = [self.profiled(after), cargo_build("previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                                                  argv=["cargo", "clippy", "--workspace"])]
        groups = (
            ("nextest", "verify", 1, 10, 12),
            ("nextest", "verify", 4, 2, 3),
            ("nextest", "alias", 1, 1, 2),
            ("clippy", "verify", 1, 2, 1),
            ("nextest", "verify", None, 1, 4),
        )
        for group, (kind, caller, crates, before_count, after_count) in enumerate(groups):
            for side, at, count, duration in (("before", before, before_count, 120),
                                             ("after", after, after_count, 90)):
                records.extend(step(f"{group}-{side}-{number}", started_at=at, ended_at=at,
                                    step=kind, caller=caller, crates_compiled=crates, duration_s=duration)
                               for number in range(count))
        # Different crate counts in the same rebuild class belong in one row.
        records[2]["crates_compiled"] = 3
        records.extend((step("only-before", started_at=before, step="fmt"),
                        step("only-after", started_at=after, step="doc")))
        self.write(*records)
        lines = self.section()
        rows = self.rows(lines)
        self.assertEqual(rows, [
            ["nextest", "verify.sh (agents)", "edited crate (1–3 crates)", "10", "2.0 min", "12", "1.5 min", "−25%"],
            ["nextest", "verify.sh (agents)", "—", "1", "2.0 min", "4", "1.5 min", "—"],
            ["nextest", "verify.sh (agents)", "cascade (4–49 crates)", "2", "2.0 min", "3", "1.5 min", "—"],
            ["nextest", "terminal", "edited crate (1–3 crates)", "1", "2.0 min", "2", "1.5 min", "—"],
            ["clippy", "verify.sh (agents)", "edited crate (1–3 crates)", "2", "2.0 min", "1", "1.5 min", "—"],
        ])
        self.assertIn("Before 2026-10-13 12:00:00 UTC (profile not recorded) vs after (performance):", lines)
        self.assertIn("| Kind | Caller | Rebuild | Before runs | Before p50 | After runs | After p50 | Change |", lines)
        self.assertIn("Source: natedev steps that passed, the 7 days before the change against the time since, up to the next change; p50 is the nearest rank; Change is shown only with at least 10 runs on each side. Load and time of day are not matched.", lines)

    def test_change_requires_ten_runs_on_each_side_and_prints_signed_percent(self) -> None:
        self.assertEqual(report.MIN_PROFILE_RUNS, 10)
        for before_count, after_count, expected in ((9, 10, "—"), (10, 9, "—"),
                                                    (10, 10, "+25%")):
            with self.subTest(before=before_count, after=after_count):
                self.write(self.profiled("2026-10-13T12:00:00.000Z"),
                           *self.runs("before", "2026-10-12T12:00:00.000Z", before_count, 40),
                           *self.runs("after", "2026-10-13T12:00:00.000Z", after_count, 50))
                row = self.rows(self.section())[0]
                self.assertEqual(row[3], str(before_count))
                self.assertEqual(row[5], str(after_count))
                self.assertEqual(row[7], expected)

    def test_unmatched_step_groups_explain_comparison_without_table_or_source(self) -> None:
        self.write(
            self.profiled("2026-10-13T12:00:00.000Z"),
            step("before", started_at="2026-10-12T12:00:00.000Z", step="clippy",
                 caller="verify", crates_compiled=1),
            step("after", started_at="2026-10-13T12:00:00.000Z", step="nextest",
                 caller="verify", crates_compiled=1),
        )
        lines = self.section()
        self.assertEqual([line for line in lines if line], [
            "### Power profile",
            "Profile now: performance, since 2026-10-13 12:00:00 UTC.",
            "Before 2026-10-13 12:00:00 UTC (profile not recorded) vs after (performance):",
            "No step ran with the same kind, caller and rebuild class on both sides.",
        ])

    def test_p50_uses_nearest_rank_rather_than_interpolated_median(self) -> None:
        records = [self.profiled("2026-10-13T12:00:00.000Z")]
        for side, at, durations in (("before", "2026-10-12T12:00:00.000Z", (200, 10, 100, 20)),
                                    ("after", "2026-10-13T12:00:00.000Z", (2000, 50, 1000, 40))):
            records.extend(step(f"{side}-{number}", started_at=at, duration_s=duration,
                                step="clippy", caller="verify", crates_compiled=1)
                           for number, duration in enumerate(durations))
        self.write(*records)
        self.assertEqual(self.rows(self.section())[0][3:], ["4", "20.0 s", "4", "50.0 s", "—"])

    def test_mac_failed_and_sweep_steps_do_not_enter_comparisons(self) -> None:
        records = [self.profiled("2026-10-13T12:00:00.000Z"),
                   cargo_build("previous", started_at=HISTORY_STAMP, ended_at=HISTORY_STAMP,
                               argv=["cargo", "clippy", "--workspace"])]
        for side, at in (("before", "2026-10-12T12:00:00.000Z"), ("after", "2026-10-13T12:00:00.000Z")):
            records.extend(self.runs(side, at, 1, 20))
            records.extend((step(f"mac-{side}", started_at=at, host="macbook", step="nextest",
                                 caller="verify", crates_compiled=1, duration_s=500),
                            step(f"failed-{side}", started_at=at, status=1, step="nextest",
                                 caller="verify", crates_compiled=1, duration_s=500),
                            step(f"sweep-{side}", started_at=at, step="sweep", duration_s=500)))
        self.write(*records)
        self.assertEqual(self.rows(self.section()), [["nextest", "verify.sh (agents)",
                         "edited crate (1–3 crates)", "1", "20.0 s", "1", "20.0 s", "—"]])

    def test_previous_and_next_changes_bound_windows_and_newest_is_first(self) -> None:
        self.write(
            self.profiled("2026-10-10T12:00:00.000Z", "power-saver"),
            self.profiled("2026-10-12T12:00:00.000Z", "balanced"),
            self.profiled("2026-10-13T12:00:00.000Z"),
            *self.runs("unknown", "2026-10-09T12:00:00.000Z", 1, 80),
            *self.runs("saver", "2026-10-10T12:00:00.000Z", 2, 60),
            *self.runs("balanced", "2026-10-12T12:00:00.000Z", 3, 40),
            *self.runs("performance", "2026-10-13T12:00:00.000Z", 4, 20),
        )
        lines = self.section()
        rows = self.rows(lines)
        self.assertEqual([row[3:] for row in rows], [
            ["3", "40.0 s", "4", "20.0 s", "—"],
            ["2", "1.0 min", "3", "40.0 s", "—"],
            ["1", "1.3 min", "2", "1.0 min", "—"],
        ])
        comparisons = [line for line in lines if line.startswith("Before ")]
        self.assertEqual(len(comparisons), 3)
        self.assertIn("2026-10-13 12:00:00 UTC", comparisons[0])
        self.assertIn("(balanced) vs after (performance)", comparisons[0])
        self.assertIn("2026-10-12 12:00:00 UTC", comparisons[1])
        self.assertIn("(power-saver) vs after (balanced)", comparisons[1])
        self.assertIn("(profile not recorded)", comparisons[2])

    def test_before_window_is_seven_days_and_after_ends_at_report_day_end(self) -> None:
        self.write(
            self.profiled("2026-10-13T12:00:00.000Z"),
            *self.runs("too-old", "2026-10-06T11:59:59.999Z", 1, 500),
            *self.runs("before-edge", "2026-10-06T12:00:00.000Z", 1, 20),
            *self.runs("after-edge", "2026-10-13T12:00:00.000Z", 1, 30),
            *self.runs("before-end", "2026-10-13T23:59:59.999Z", 1, 30),
            *self.runs("at-end", "2026-10-14T00:00:00.000Z", 1, 500),
        )
        self.assertEqual(self.rows(self.section())[0][3:], ["1", "20.0 s", "2", "30.0 s", "—"])

    def test_change_older_than_seven_days_only_shows_current_profile(self) -> None:
        since = "2026-10-06T00:00:00.000Z"
        self.write(self.profiled("2026-10-06T00:01:00.000Z", since=since),
                   self.profiled("2026-10-13T12:00:00.000Z", since="2026-10-13T11:00:00.000Z"))
        lines = self.section()
        self.assertEqual([line for line in lines if line], [
            "### Power profile", "Profile now: performance, since 2026-10-06 00:00:00 UTC.",
        ])

    def test_no_profiled_natedev_sample_omits_section(self) -> None:
        self.write(sample(STAMP), sample("2026-10-13T12:00:00.000Z", host="macbook", power_profile="balanced"))
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.section(), [])

    def test_report_window_includes_start_and_excludes_change_at_day_end(self) -> None:
        self.write(
            self.profiled("2026-10-07T00:00:00.000Z", "balanced"),
            self.profiled("2026-10-14T00:00:00.000Z"),
            *self.runs("before", "2026-10-06T12:00:00.000Z", 1, 20),
            *self.runs("after", "2026-10-13T12:00:00.000Z", 1, 30),
        )
        lines = self.section()
        self.assertIn("Profile now: balanced, since 2026-10-07 00:00:00 UTC.", lines)
        self.assertEqual(sum(line.startswith("Before ") for line in lines), 1)
        self.assertNotIn("performance", "\n".join(lines))
        self.assertEqual(self.rows(lines)[0][3:], ["1", "20.0 s", "1", "30.0 s", "—"])

    def test_profile_labels_use_user_zone_with_seconds(self) -> None:
        self.write(self.profiled("2026-10-14T02:30:00.000Z", since="2026-10-14T02:29:56.000Z"))
        with mock.patch.dict(os.environ, {"TZ": "America/New_York", "USER_ZONE": "America/New_York"}):
            time.tzset()
            lines = self.section()
        self.assertIn("Profile now: performance, since 2026-10-13 22:29:56 EDT.", lines)
        self.assertIn("Before 2026-10-13 22:29:56 EDT (profile not recorded) vs after (performance):", lines)


if __name__ == "__main__":
    _ = unittest.main()
