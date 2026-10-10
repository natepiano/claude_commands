#!/usr/bin/env python3
"""Memory admission with only temporary history, cgroups, and ledgers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import memory_admit as gate
store = gate.store

GIB = gate.GIB
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
HISTORY_AT = NOW - timedelta(seconds=1)
INVOKE = Path(__file__).with_name("invoke.sh")
MEMORY_GATE = Path(__file__).with_name("memory_gate.sh")


class DecisionTests(unittest.TestCase):
    def test_alone_has_old_ceiling_and_earlyoom_reserve(self) -> None:
        result = gate.decide(12 * GIB, 60 * GIB, 20 * GIB, [])
        self.assertEqual((result.state, result.threshold, result.reserve), ("admit", 12 * GIB, 3 * GIB))
        self.assertEqual(gate.decide(12 * GIB - 1, 60 * GIB, 20 * GIB, []).state, "hold")

    def test_running_steps_promise_only_future_anon_growth(self) -> None:
        running = [gate.RunningMemory(10 * GIB, 4 * GIB), gate.RunningMemory(7 * GIB, 9 * GIB)]
        result = gate.decide(14 * GIB, 60 * GIB, 5 * GIB, running)
        self.assertEqual((result.promised, result.threshold, result.state), (6 * GIB, 14 * GIB, "admit"))
        self.assertEqual(gate.decide(14 * GIB - 1, 60 * GIB, 5 * GIB, running).state, "hold")

    def test_no_total_has_no_reserve(self) -> None:
        result = gate.decide(5 * GIB, None, 5 * GIB, [])
        self.assertEqual((result.reserve, result.threshold, result.state), (0, 5 * GIB, "admit"))


class HistoryTests(unittest.TestCase):
    root: Path = Path()
    admission: Path = Path()
    host: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.admission = self.root / "admission"
        self.admission.mkdir()
        self.host = "test-host"

    def add_measured(self, count: int, *, host: str = "test-host", repo: str = "hana",
                     at: datetime = HISTORY_AT, test_threads: int | None = None,
                     start_gib: int = 1) -> None:
        with (self.admission / "anon_peaks.jsonl").open("a") as out:
            for index in range(count):
                record: dict[str, object] = {
                    "host": host,
                    "repo": repo,
                    "step": "nextest",
                    "ended_at": store.utc_iso(at.timestamp()),
                    "anon_peak_bytes": (start_gib + index) * GIB,
                }
                if test_threads is not None:
                    record["test_threads"] = test_threads
                _ = out.write(json.dumps(record) + "\n")

    def add_index(self, *, at: datetime = HISTORY_AT) -> None:
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, peak_mem_bytes INTEGER)")
            for index in range(5):
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(at.timestamp()), (index + 1) * GIB))

    def peak(self) -> gate.ExpectedPeak:
        return gate.expected_peak("hana", "nextest", self.host, NOW, self.root)

    def test_measured_overrides_index_at_five(self) -> None:
        self.add_index()
        self.add_measured(4)
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))
        self.add_measured(1)
        self.assertEqual(self.peak(), gate.ExpectedPeak(4 * GIB, "measured", 5))

    def test_stale_other_repo_and_other_host_are_excluded(self) -> None:
        self.add_measured(5, at=NOW - timedelta(days=15))
        self.add_measured(5, repo="other")
        self.add_measured(5, host="other-host")
        self.assertEqual(self.peak(), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))

    def test_missing_index_or_history_is_fallback(self) -> None:
        self.assertEqual(self.peak().source, "fallback")
        self.add_index()
        self.assertEqual(self.peak().source, "buildlog")

    def test_measured_history_is_keyed_by_test_width(self) -> None:
        self.add_measured(5, test_threads=16)
        self.add_measured(5, start_gib=6)
        self.assertEqual(
            gate.expected_peak("hana", "nextest", self.host, NOW, self.root, test_threads=16),
            gate.ExpectedPeak(5 * GIB, "measured", 5),
        )
        self.assertEqual(
            gate.expected_peak("hana", "nextest", self.host, NOW, self.root),
            gate.ExpectedPeak(10 * GIB, "measured", 5),
        )

    def test_future_measured_records_do_not_override_prior_index_history(self) -> None:
        """F002: a historical report excludes measured records beyond its end."""
        self.add_index()
        self.add_measured(5, at=NOW + timedelta(seconds=1), start_gib=10)
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_future_index_rows_do_not_supply_buildlog_history(self) -> None:
        """F002: a historical report excludes index rows beyond its end."""
        self.add_index(at=NOW + timedelta(seconds=1))
        self.assertEqual(self.peak(), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))


class TestThreadParsingTests(unittest.TestCase):
    def test_all_integer_forms_are_recognized(self) -> None:
        cases = (
            (["cargo", "nextest", "run", "--test-threads", "16"], 16),
            (["cargo", "nextest", "run", "--test-threads=32"], 32),
            (["cargo", "nextest", "run", "-j", "12"], 12),
            (["cargo", "nextest", "run", "-j8"], 8),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(gate.test_threads(argv), expected)

    def test_absent_or_non_integer_width_is_unset(self) -> None:
        for argv in (
            ["cargo", "nextest", "run"],
            ["cargo", "nextest", "run", "--test-threads", "num-cpus"],
            ["cargo", "nextest", "run", "--test-threads=num-cpus"],
            ["cargo", "nextest", "run", "-jnum-cpus"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(gate.test_threads(argv))


class LedgerTests(unittest.TestCase):
    root: Path = Path()
    sidecar: Path = Path()
    meminfo: Path = Path()
    pid: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sidecar = self.root / "step.cgroup"
        self.meminfo = self.root / "meminfo"
        _ = self.meminfo.write_text("MemAvailable: 12582912 kB\n")
        self.pid = os.getpid()

    def check(self, available: int, *, force: bool = False, sidecar: str = "",
              argv: list[str] | None = None) -> tuple[gate.AdmissionDecision, Path | None]:
        self.write_meminfo(available)
        return self.admit(force=force, sidecar=sidecar, argv=argv)

    def write_meminfo(self, available: int) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {available // 1024} kB\n")

    def admit(self, *, force: bool = False, sidecar: str = "",
              argv: list[str] | None = None) -> tuple[gate.AdmissionDecision, Path | None]:
        with patch.object(gate, "git_identity", return_value=("hana", "test-worktree")), \
             patch.object(store, "host_name", return_value="test-host"):
            result = gate.check(
                self.meminfo, self.pid, sidecar, argv or ["cargo", "nextest"], force, self.root, NOW
            )
        path = result.reservation.path if isinstance(result.reservation, gate.Reserved) else None
        return result.decision, path

    def test_two_concurrent_checks_admit_one(self) -> None:
        barrier = threading.Barrier(2)
        self.write_meminfo(12 * GIB)

        def arrive() -> tuple[gate.AdmissionDecision, Path | None]:
            _ = barrier.wait()
            return self.admit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(arrive)
            second = pool.submit(arrive)
            results = [first.result(), second.result()]
        self.assertEqual([decision.state for decision, _ in results].count("admit"), 1)
        self.assertEqual(len(list((self.root / "admission").glob("*.reservation"))), 1)

    def test_anon_reduces_promise_and_release_records_maximum(self) -> None:
        scope = self.root / "scope"
        scope.mkdir()
        _ = (scope / "memory.stat").write_text(f"anon {8 * GIB}\nfile 1\n")
        _ = self.sidecar.write_text(str(scope) + "\n")
        with patch.dict(os.environ, {"BUILDLOG_CALL_ID": "call-width-16"}):
            _, path = self.check(
                12 * GIB,
                sidecar=str(self.sidecar),
                argv=["cargo", "nextest", "run", "--test-threads", "16"],
            )
        assert path is not None
        result, _ = self.check(12 * GIB)
        self.assertEqual(result.promised, 4 * GIB)
        _ = path.with_suffix(".max").write_text(str(8 * GIB))
        with patch.object(store, "host_name", return_value="test-host"):
            gate.release(path, 7, self.root)
        self.assertFalse(path.exists())
        self.assertFalse(self.sidecar.exists())
        line = cast(dict[str, object], json.loads((self.root / "admission/anon_peaks.jsonl").read_text()))
        self.assertEqual((line["anon_peak_bytes"], line["status"]), (8 * GIB, 7))
        self.assertEqual((line["test_threads"], line["call_id"]), (16, "call-width-16"))

    def test_unscoped_step_writes_no_peak(self) -> None:
        _, path = self.check(12 * GIB)
        assert path is not None
        _ = path.with_suffix(".max").write_text(str(GIB))
        gate.release(path, 0, self.root)
        self.assertFalse((self.root / "admission/anon_peaks.jsonl").exists())

    def test_dead_and_reused_pid_are_pruned(self) -> None:
        directory = gate.admission_dir(self.root)
        for pid, start in ((999999999, "0"), (self.pid, "wrong")):
            path = directory / f"{pid}.reservation"
            row: gate.Reservation = {"pid": pid, "start_time": start, "need": 12 * GIB,
                                     "repo": "hana", "step": "nextest", "worktree": "old",
                                     "admitted_at": "", "sidecar": "", "test_threads": None,
                                     "call_id": None}
            _ = path.write_text(json.dumps(row))
        decision, _ = self.check(12 * GIB)
        self.assertEqual(decision.state, "admit")
        self.assertFalse((directory / "999999999.reservation").exists())
        self.assertFalse((directory / f"{self.pid}.reservation").exists())

    def test_forced_admission_still_reserves(self) -> None:
        decision, path = self.check(0, force=True)
        self.assertEqual(decision.state, "hold")
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.exists())

    def test_no_argv_reads_live_promise_without_reserving(self) -> None:
        _, path = self.check(12 * GIB)
        assert path is not None
        result = gate.check(self.meminfo, self.pid, "", [], root=self.root, now=NOW)
        self.assertEqual((result.decision.state, result.decision.promised, len(result.live)), ("hold", 12 * GIB, 1))
        self.assertEqual(result.reservation, gate.NotReserved("launch"))
        self.assertEqual(len(list((self.root / "admission").glob("*.reservation"))), 1)

    def test_held_step_has_explicit_reservation_state(self) -> None:
        _ = self.meminfo.write_text("MemAvailable: 0 kB\n")
        result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual(result.reservation, gate.NotReserved("held"))

    def test_unreadable_caller_start_is_untracked(self) -> None:
        with patch.object(gate, "process_start_time", return_value=None):
            result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual(result.reservation, gate.NotReserved("untracked"))

    def test_locked_check_uses_meminfo_after_scope_grows(self) -> None:
        _ = self.meminfo.write_text("MemAvailable: 20971520 kB\n")
        first = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertIsInstance(first.reservation, gate.Reserved)
        _ = self.meminfo.write_text("MemAvailable: 20971520 kB\n")
        def grow(_sidecar: str) -> int:
            _ = self.meminfo.write_text("MemAvailable: 8388608 kB\n")
            return 8 * GIB
        with patch.object(gate, "cgroup_anon", side_effect=grow):
            result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual((result.available, result.decision.state), (8 * GIB, "hold"))

    def test_profile_lookups_run_once_across_three_polls(self) -> None:
        with patch.object(gate, "git_identity", return_value=("hana", "tree")) as identity, \
             patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(12 * GIB, "fallback", 0)) as history:
            profile = None
            for _ in range(3):
                result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW, profile=profile)
                profile = result.profile
        self.assertEqual(identity.call_count, 1)
        self.assertEqual(history.call_count, 1)


class ShellTests(unittest.TestCase):
    def test_invoke_twice_with_temporary_home(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            config = root / "config.sh"
            _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
            environment = {**os.environ, "HOME": str(root), "BUILDLOG_MEMINFO": str(meminfo),
                           "BUILDLOG_OFF": "1", "BUILDLOG_SCOPE": "0",
                           "LINT_CONFIG_READER": str(config)}
            command = 'source "$1"; sweep_after_step() { :; }; run_once true'
            for _ in range(2):
                result = subprocess.run(["bash", "-c", command, "test", str(INVOKE)],
                                        capture_output=True, text=True, timeout=8, check=False,
                                        env=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_gate_with_argv_and_temporary_home_reserves(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            command = 'source "$1"; buildlog_wait_for_memory /bin/true; printf "%s|%s|" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"; if [[ -n "$BUILDLOG_MEM_RESERVATION" && -f "$BUILDLOG_MEM_RESERVATION" ]]; then echo exists; else echo missing; fi; path=$BUILDLOG_MEM_RESERVATION; buildlog_release_memory 0; if [[ -n "$path" && ! -e "$path" ]]; then echo gone; else echo remains; fi'
            result = subprocess.run(
                ["bash", "-c", command, "test", str(MEMORY_GATE)],
                capture_output=True, text=True, timeout=8, check=False,
                env={**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                     "BUILDLOG_MEMINFO": str(meminfo)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^Granted\|[^|\n]+\|exists\ngone\n$")
            self.assertEqual(len(list((root / "log/admission").glob("*.reservation"))), 0)

    def test_sampler_maximum_reaches_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scope = root / "scope"
            scope.mkdir()
            stat = scope / "memory.stat"
            _ = stat.write_text(f"anon {GIB}\n")
            sidecar = root / "step.cgroup"
            _ = sidecar.write_text(str(scope) + "\n")
            with patch.object(gate, "git_identity", return_value=("hana", "test-worktree")), \
                 patch.object(store, "host_name", return_value="test-host"):
                meminfo = root / "meminfo"
                _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
                checked = gate.check(meminfo, os.getpid(), str(sidecar), ["cargo", "nextest"], root=root, now=NOW)
            assert isinstance(checked.reservation, gate.Reserved)
            reservation = checked.reservation.path
            maximum = reservation.with_suffix(".max")
            command = 'source "$1"; buildlog_sample_anon "$2" "$3" & p=$!; sleep 1.2; kill "$p"; wait "$p" 2>/dev/null || true'
            result = subprocess.run(["bash", "-c", command, "test", str(INVOKE), str(sidecar), str(maximum)],
                                    capture_output=True, text=True, timeout=5, check=False,
                                    env={**os.environ, "BUILDLOG_OFF": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(maximum.read_text().strip(), str(GIB))
            with patch.object(store, "host_name", return_value="test-host"):
                gate.release(reservation, 0, root)
            row = cast(dict[str, object], json.loads((root / "admission/anon_peaks.jsonl").read_text()))
            self.assertEqual(row["anon_peak_bytes"], GIB)

    def test_gate_wait_lines_and_no_argv_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 11010048 kB\n")
            command = 'source "$1"; buildlog_wait_for_memory; printf "%s\n" "$BUILDLOG_MEM_RESERVATION"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=5, check=False,
                                    env={**os.environ, "BUILDLOG_DIR": str(root),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_POLL_S": "1",
                                         "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "\n")
            self.assertEqual(result.stderr.count("waiting for memory since "), 1)
            self.assertIn("(build needs ", result.stderr)
            self.assertIn("memory wait limit reached after 15 min; starting anyway", result.stderr)
            self.assertFalse((root / "admission").exists())

    def test_gate_wait_lines_in_order_without_repeated_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
            command = (
                'source "$1"; calls=0; sleep() { calls=$((calls + 1)); '
                'if (( calls == 1 )); then SECONDS=61; else printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; fi; }; '
                'buildlog_wait_for_memory'
            )
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=8, check=False,
                                    env={**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stderr.splitlines()
            self.assertEqual(len(lines), 3, lines)
            self.assertTrue(lines[0].startswith("waiting for memory since "))
            self.assertTrue(lines[1].startswith("still waiting for memory (1 min): "))
            self.assertRegex(lines[2], r"^memory free after 1 min \d+ s: starting$")
            self.assertEqual(result.stderr.count("waiting for memory since "), 1)

    def test_limit_writes_reservation_for_argv(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            command = (
                'source "$1"; sleep() { echo slept >&2; }; buildlog_wait_for_memory true; '
                'printf "%s|%s|" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"; '
                'if [[ -n "$BUILDLOG_MEM_RESERVATION" && -f "$BUILDLOG_MEM_RESERVATION" ]]; then echo exists; else echo missing; fi; '
                'path=$BUILDLOG_MEM_RESERVATION; buildlog_release_memory 0; if [[ -n "$path" && ! -e "$path" ]]; then echo gone; else echo remains; fi'
            )
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=8, check=False,
                                    env={**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_WAIT_LIMIT_S": "0"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^TimedOut\|[^|\n]+\|exists\ngone\n$")
            self.assertNotIn("slept", result.stderr)
            self.assertEqual(len(list((root / "log/admission").glob("*.reservation"))), 0)

    def test_fitting_at_limit_is_granted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            command = ('source "$1"; sleep() { printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; SECONDS=1; }; '
                       'buildlog_wait_for_memory /bin/true; printf "%s\n" "$BUILDLOG_MEM_OUTCOME"; buildlog_release_memory 0')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=8, check=False,
                                    env={**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Granted\n")
            self.assertIn("memory free after", result.stderr)
            self.assertNotIn("memory wait limit reached", result.stderr)

    def test_python_failure_is_reported_and_step_starts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            broken = root / "broken.py"
            _ = broken.write_text('raise RuntimeError("broken lookup")\n')
            command = 'set -e; source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory /bin/true; printf "%s\n" "$BUILDLOG_MEM_OUTCOME"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(broken)],
                                    capture_output=True, text=True, timeout=8, check=False,
                                    env={**os.environ, "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "MeminfoUnavailable\n")
            self.assertIn("memory gate failed (RuntimeError: broken lookup); starting anyway", result.stderr)

    def test_unreadable_meminfo_starts_the_step(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            command = ('set -e; source "$1"; buildlog_wait_for_memory /bin/true; '
                       'printf "%s|%s\n" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"')
            for contents in ("", "MemTotal: 67108864 kB\n"):
                with self.subTest(contents=contents):
                    _ = meminfo.write_text(contents)
                    result = subprocess.run(
                        ["bash", "-c", command, "test", str(MEMORY_GATE)],
                        capture_output=True, text=True, timeout=8, check=False,
                        env={**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                             "BUILDLOG_MEMINFO": str(meminfo)},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "MeminfoUnavailable|\n")

    def test_untracked_admission_prints_warning(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            wrapper = root / "untracked.py"
            _ = wrapper.write_text(f'import sys\nsys.path.insert(0, {str(MEMORY_GATE.parent)!r})\nimport memory_admit as gate\ngate.process_start_time = lambda pid: None\ngate.main()\n')
            command = 'source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory /bin/true; printf "%s|%s\n" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(wrapper)],
                                    capture_output=True, text=True, timeout=8, check=False,
                                    env={**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Granted|\n")
            self.assertIn("memory gate could not track this step; starting without a reservation", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
