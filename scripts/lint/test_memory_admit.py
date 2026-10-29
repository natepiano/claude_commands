#!/usr/bin/env python3
"""Memory admission with only temporary history, cgroups, and ledgers."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_admit as gate
import nextest_budget as budget
store = gate.store

GIB = gate.GIB
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
HISTORY_AT = NOW - timedelta(seconds=1)
INVOKE = Path(__file__).with_name("invoke.sh")
MEMORY_GATE = Path(__file__).with_name("memory_gate.sh")
HANA = gate.RepositoryIdentity("https://example.org/nate/hana", "hana", "hana")


def setUpModule() -> None:
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {
        "BUILDLOG_MEMORY_PRESSURE": str(root / "pressure"),
        "BUILDLOG_BUILD_BUDGET_GIB": "20",
        "USER_ZONE": "America/New_York",
    })))


class DecisionTests(unittest.TestCase):
    def test_alone_covers_need_and_earlyoom_reserve(self) -> None:
        result = gate.decide(23 * GIB, 60 * GIB, 20 * GIB, [])
        self.assertEqual((result.state, result.threshold, result.reserve), ("admit", 23 * GIB, 3 * GIB))
        self.assertEqual(gate.decide(23 * GIB - 1, 60 * GIB, 20 * GIB, []).state, "hold")

    def test_measured_use_is_not_charged_again_but_growth_is_reserved(self) -> None:
        running: list[gate.StepMemory] = [gate.MeasuredMemory(3 * GIB, 3 * GIB), gate.PromisedMemory(2 * GIB)]
        result = gate.decide(10 * GIB, 60 * GIB, 5 * GIB, running)
        self.assertEqual((result.promised, result.measured, result.threshold, result.state),
                         (2 * GIB, 3 * GIB, 10 * GIB, "admit"))
        self.assertEqual(gate.decide(10 * GIB - 1, 60 * GIB, 5 * GIB, running).state, "hold")

    def test_ramping_step_counts_promise_or_higher_measured_use(self) -> None:
        for used, headroom, state in ((1, 11, "hold"), (11, 1, "admit"), (13, 0, "hold")):
            with self.subTest(measured_gib=used):
                result = gate.decide(15 * GIB, 60 * GIB, 8 * GIB,
                                     [gate.MeasuredMemory(used * GIB, 12 * GIB)])
                self.assertEqual((result.state, result.promised, result.measured),
                                 (state, headroom * GIB, used * GIB))

    def test_no_total_has_no_reserve(self) -> None:
        result = gate.decide(5 * GIB, None, 5 * GIB, [])
        self.assertEqual((result.reserve, result.threshold, result.state), (0, 5 * GIB, "admit"))

    def test_positive_budget_override_changes_admission_and_invalid_values_use_21(self) -> None:
        for setting, state in (("24", "admit"), ("20", "hold"), ("0", "admit"), ("-1", "admit"),
                               ("bad", "admit"), ("nan", "admit"), ("inf", "admit"), ("", "admit")):
            with self.subTest(setting=setting), patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": setting}):
                decision = gate.decide(40 * GIB, 60 * GIB, 10 * GIB, [gate.MeasuredMemory(11 * GIB)])
                self.assertEqual(decision.state, state)
        with patch.dict(os.environ):
            del os.environ["BUILDLOG_BUILD_BUDGET_GIB"]
            self.assertEqual(gate.decide(40 * GIB, 60 * GIB, 10 * GIB, [gate.MeasuredMemory(11 * GIB)]).state, "admit")
            self.assertEqual(gate.decide(40 * GIB, 60 * GIB, 10 * GIB + 1, [gate.MeasuredMemory(11 * GIB)]).state, "hold")

    def test_psi_11_holds_a_step_that_fits_memory_and_budget(self) -> None:
        decision = gate.decide(40 * GIB, 60 * GIB, GIB, [], pressure=gate.MemoryPressure(11))
        self.assertEqual(decision.state, "hold")

    def test_empty_machine_cannot_skip_the_budget_reserved_for_a_head(self) -> None:
        decision = gate.decide(40 * GIB, 60 * GIB, int(10.8 * GIB), [], head_need=int(19.3 * GIB))
        self.assertEqual(decision.state, "hold")



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
        return gate.expected_peak(HANA, "nextest", self.host, NOW, self.root)

    def test_measured_overrides_index_at_five(self) -> None:
        self.add_index()
        self.add_measured(4)
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))
        self.add_measured(1)
        self.assertEqual(self.peak(), gate.ExpectedPeak(4 * GIB, "measured", 5))

    def test_stale_and_other_host_are_excluded(self) -> None:
        self.add_measured(5, at=NOW - timedelta(days=15))
        self.add_measured(5, host="other-host")
        self.assertEqual(self.peak(), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))

    def test_a_repo_with_few_runs_uses_the_step_p90_over_every_repo(self) -> None:
        self.add_measured(3, repo="other")
        self.add_measured(4, start_gib=10)
        self.assertEqual(self.peak(), gate.ExpectedPeak(13 * GIB, "pooled", 7))

    def test_own_buildlog_history_comes_before_the_pooled_p90(self) -> None:
        self.add_index()
        self.add_measured(5, repo="other")
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_missing_index_or_history_is_fallback(self) -> None:
        self.assertEqual(self.peak().source, "fallback")
        self.add_index()
        self.assertEqual(self.peak().source, "buildlog")

    def test_measured_history_pools_every_width_unless_one_is_named(self) -> None:
        self.add_measured(5, test_threads=16)
        self.add_measured(5, test_threads=4, start_gib=6)
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, test_threads=16),
            gate.ExpectedPeak(5 * GIB, "measured", 5),
        )
        self.assertEqual(
            gate.expected_peak(HANA, "nextest", self.host, NOW, self.root),
            gate.ExpectedPeak(9 * GIB, "measured", 10),
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

    def test_buildlog_tier_keeps_filtered_runs_separate(self) -> None:
        self.add_index()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("ALTER TABLE steps ADD COLUMN argv TEXT")
            _ = db.execute("UPDATE steps SET argv = ?", (json.dumps(["cargo", "nextest", "run"]),))
            for _ in range(5):
                _ = db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                               (self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 2 * GIB,
                                json.dumps(["cargo", "nextest", "run", "-E", "test(one)"])))
        self.assertEqual(self.peak(), gate.ExpectedPeak(int(5 * GIB * gate.ANON_SHARE), "buildlog", 5))
        self.assertEqual(gate.expected_peak(HANA, "nextest", self.host, NOW, self.root, scope="filtered"),
                         gate.ExpectedPeak(int(2 * GIB * gate.ANON_SHARE), "buildlog", 5))

    def test_scratch_checkout_cannot_take_legacy_buildlog_samples_by_repository_name(self) -> None:
        self.add_index()
        scratch = gate.RepositoryIdentity(HANA.key, HANA.name)
        self.assertEqual(gate.expected_peak(scratch, "nextest", self.host, NOW, self.root).source,
                         "fallback")


class RepositoryHistoryTests(unittest.TestCase):
    root: Path = Path()
    admission: Path = Path()
    host: str = "test-host"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.admission = self.root / "admission"
        self.admission.mkdir()

    def checkout(self, folder: str, remote: str = "", content: str = "first") -> Path:
        directory = self.root / folder
        directory.mkdir(parents=True)
        for arguments in (("init", "-q"), ("config", "user.email", "test@example.org"),
                          ("config", "user.name", "Test"), ("config", "commit.gpgSign", "false"),
                          ("config", "core.hooksPath", "/dev/null")):
            _ = subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True)
        _ = (directory / "source").write_text(content)
        _ = subprocess.run(["git", "-C", str(directory), "add", "source"], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(directory), "commit", "-qm", "first"], check=True, capture_output=True)
        if remote:
            _ = subprocess.run(["git", "-C", str(directory), "remote", "add", "origin", remote], check=True, capture_output=True)
        return directory

    def identity(self, directory: Path) -> gate.RepositoryIdentity:
        repository, worktree = gate.git_identity(["cargo", "nextest", "run"], str(directory))
        self.assertEqual(worktree, directory.name)
        self.assertIsInstance(repository, gate.RepositoryIdentity)
        assert isinstance(repository, gate.RepositoryIdentity)
        return repository

    def samples(self, repository: gate.RepositoryIdentity, gib: int, *, scope: gate.TestScope = "full") -> None:
        with (self.admission / "anon_peaks.jsonl").open("a") as out:
            for _ in range(5):
                _ = out.write(json.dumps({"host": self.host, "repo": "repo", "step": "nextest",
                                          "repo_identity": repository.key, "test_scope": scope,
                                          "anon_peak_bytes": gib * GIB,
                                          "ended_at": store.utc_iso(HISTORY_AT.timestamp())}) + "\n")

    def estimate(self, repository: gate.Repository, scope: gate.TestScope = "full") -> gate.ExpectedPeak:
        return gate.expected_peak(repository, "nextest", self.host, NOW, self.root, scope=scope)

    def test_unrelated_repositories_named_repo_use_their_own_peaks(self) -> None:
        first = self.identity(self.checkout("one/repo", "git@example.org:nate/first.git"))
        second = self.identity(self.checkout("two/repo", "https://example.org/nate/second.git"))
        self.samples(first, 19)
        self.samples(second, 2)
        self.assertEqual(self.estimate(first), gate.ExpectedPeak(19 * GIB, "measured", 5))
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(2 * GIB, "measured", 5))

    def test_same_repository_shares_peaks_across_folders_and_remote_forms(self) -> None:
        first = self.identity(self.checkout("one/repo", "git@example.org:nate/first.git"))
        second = self.identity(self.checkout("two/different", "https://user@example.org/nate/first/"))
        self.samples(first, 2)
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(2 * GIB, "measured", 5))

    def test_worktree_uses_main_checkout_name_for_old_anon_and_buildlog_rows(self) -> None:
        checkout = self.checkout("hana", "https://example.org/nate/hana.git")
        worktree = self.root / "hana-fix"
        _ = subprocess.run(["git", "-C", str(checkout), "worktree", "add", "-q", "--detach", str(worktree)],
                           check=True, capture_output=True)
        identity = self.identity(worktree)
        peaks = self.admission / "anon_peaks.jsonl"
        rows = [{"host": self.host, "repo": "hana", "step": "nextest",
                 "anon_peak_bytes": 3 * GIB, "ended_at": store.utc_iso(HISTORY_AT.timestamp())}] * 5
        _ = peaks.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(3 * GIB, "measured", 5))
        peaks.unlink()
        with closing(sqlite3.connect(self.root / "index.sqlite")) as db, db:
            _ = db.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, peak_mem_bytes INTEGER)")
            _ = db.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
                               [(self.host, "hana", "nextest", store.utc_iso(HISTORY_AT.timestamp()), 4 * GIB)] * 5)
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(int(4 * GIB * gate.ANON_SHARE), "buildlog", 5))
        scratch = self.identity(self.checkout("repo", "https://example.org/nate/hana.git"))
        self.assertEqual(self.estimate(scratch).source, "fallback")

    def test_filtered_and_full_runs_never_use_each_others_samples(self) -> None:
        identity = self.identity(self.checkout("repo", "https://example.org/nate/first"))
        self.samples(identity, 19)
        self.assertEqual(self.estimate(identity, "filtered").source, "fallback")
        self.samples(identity, 2, scope="filtered")
        self.assertEqual(self.estimate(identity, "filtered"), gate.ExpectedPeak(2 * GIB, "measured", 5))
        self.assertEqual(self.estimate(identity), gate.ExpectedPeak(19 * GIB, "measured", 5))

    def test_generic_legacy_rows_cannot_supply_an_estimate_to_a_scratch_clone(self) -> None:
        identity = self.identity(self.checkout("repo", "https://example.org/nate/first"))
        with (self.admission / "anon_peaks.jsonl").open("w") as out:
            for _ in range(7):
                _ = out.write(json.dumps({"host": self.host, "repo": "repo", "step": "nextest",
                                          "anon_peak_bytes": 19 * GIB,
                                          "ended_at": store.utc_iso(HISTORY_AT.timestamp())}) + "\n")
        self.assertEqual(self.estimate(identity).source, "fallback")

    def test_clone_uses_other_repositories_pooled_samples_without_a_basename_key(self) -> None:
        first = self.identity(self.checkout("one/repo", "https://example.org/nate/first"))
        second = self.identity(self.checkout("two/checkout", "https://example.org/nate/second"))
        self.samples(first, 3)
        self.assertEqual(self.estimate(second), gate.ExpectedPeak(3 * GIB, "pooled", 5))

    def test_no_origin_uses_root_commit_across_clones(self) -> None:
        original = self.checkout("original")
        clone = self.root / "copy"
        _ = subprocess.run(["git", "clone", "-q", str(original), str(clone)], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(clone), "remote", "remove", "origin"], check=True, capture_output=True)
        self.samples(self.identity(original), 4)
        self.assertEqual(self.estimate(self.identity(clone)), gate.ExpectedPeak(4 * GIB, "measured", 5))
        unrelated = self.identity(self.checkout("other", content="different"))
        self.assertEqual(self.estimate(unrelated).source, "pooled")

    def test_missing_identity_uses_only_the_fallback(self) -> None:
        self.samples(HANA, 19)
        self.assertEqual(self.estimate(gate.UnknownRepository()), gate.ExpectedPeak(gate.FALLBACK, "fallback", 0))

    def test_test_selection_is_carried_from_cargo_arguments(self) -> None:
        full = (["cargo", "nextest", "run", "-p", "hana", "--test-threads", "16"],
                ["cargo", "test", "--manifest-path", "/test/Cargo.toml", "--", "--nocapture"])
        filtered = (["cargo", "nextest", "run", "one_test"],
                    ["cargo", "test", "run"],
                    ["cargo", "nextest", "run", "run"],
                    ["cargo", "+nightly", "nextest", "run", "-E", "test(one)"],
                    ["cargo", "nextest", "run", "--filterset=test(one)"],
                    ["cargo", "test", "-p", "hana", "one_test"],
                    ["cargo", "test", "--", "--skip", "one_test"])
        for arguments in full:
            with self.subTest(arguments=arguments):
                self.assertEqual(gate.test_scope(arguments), "full")
        for arguments in filtered:
            with self.subTest(arguments=arguments):
                self.assertEqual(gate.test_scope(arguments), "filtered")

    def test_verify_package_and_target_runs_share_full_history_but_name_filter_does_not(self) -> None:
        identity = self.identity(self.checkout("hana", "https://example.org/nate/hana.git"))
        self.samples(identity, 9)
        self.samples(identity, 2, scope="filtered")
        cases = ((["-E", "package(hana)"], 9),
                 (["--test", "render", "-E", "package(hana)"], 9),
                 (["-E", "package(hana) & test(render)"], 2))
        for arguments, gib in cases:
            with self.subTest(arguments=arguments):
                scope = gate.test_scope(["cargo", "nextest", "run", *arguments])
                self.assertEqual(self.estimate(identity, scope), gate.ExpectedPeak(gib * GIB, "measured", 5))

    def test_filter_expressions_and_option_values_do_not_become_test_names(self) -> None:
        for option in ("-E", "--filter-expr", "--filterset"):
            for predicate, expected in (("package(hana)", "full"), ("kind(lib)", "full"),
                                        ("package(hana) & test(render)", "filtered")):
                for arguments in ([option, predicate], [option + "=" + predicate]):
                    with self.subTest(arguments=arguments):
                        self.assertEqual(gate.test_scope(["cargo", "nextest", "run", *arguments]), expected)
        for option in ("-F", "--exclude", "-P", "--run-ignored", "-Z", "--no-tests",
                       "--max-fail", "--tool-config-file", "--test-group"):
            for arguments in ([option, "value"], [option + "=value"]):
                with self.subTest(arguments=arguments):
                    argv = ["cargo", "nextest", "run", *arguments]
                    self.assertEqual(gate.test_scope(argv), "full")
                    self.assertEqual(gate.test_scope([*argv, "--", "render"]), "filtered")
        for arguments in (["--exact"], ["--skip", "render"], ["--", "render"], ["-Etest(render)"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(gate.test_scope(["cargo", "nextest", "run", *arguments]), "filtered")


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
        self.pid = 700
        def caller_start(pid: int) -> str | None:
            return "start" if pid == self.pid else None
        _ = self.enterContext(patch.object(gate, "process_start_time", side_effect=caller_start))
        _ = self.enterContext(patch.object(gate, "process_table", return_value={self.pid: gate.ProcessIdentity(1, "start")}))
        _ = self.enterContext(patch.object(gate, "tree_memory", return_value=gate.MeasuredMemory(8 * GIB)))
        _ = self.enterContext(patch.object(gate, "git_identity", return_value=(HANA, "test-worktree")))
        _ = self.enterContext(patch.object(store, "host_name", return_value="test-host"))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root / "log")})))

    def check(self, available: int, *, force: bool = False, sidecar: str = "",
              argv: list[str] | None = None) -> tuple[gate.AdmissionDecision, Path | None]:
        self.write_meminfo(available)
        return self.admit(force=force, sidecar=sidecar, argv=argv)

    def write_meminfo(self, available: int) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: {available // 1024} kB\n")

    def admit(self, *, force: bool = False, sidecar: str = "",
              argv: list[str] | None = None) -> tuple[gate.AdmissionDecision, Path | None]:
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

    def test_ramping_tree_keeps_growth_and_release_records_maximum(self) -> None:
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
        with patch.object(gate, "step_memory", return_value=gate.MeasuredMemory(8 * GIB, 12 * GIB)):
            result, _ = self.check(12 * GIB)
        self.assertEqual((result.promised, result.measured), (4 * GIB, 8 * GIB))
        _ = path.with_suffix(".max").write_text(str(8 * GIB))
        with patch.object(store, "host_name", return_value="test-host"):
            gate.release(path, 7, self.root)
        self.assertFalse(path.exists())
        self.assertFalse(self.sidecar.exists())
        line = cast(dict[str, object], json.loads((self.root / "admission/anon_peaks.jsonl").read_text()))
        self.assertEqual((line["anon_peak_bytes"], line["status"]), (8 * GIB, 7))
        self.assertEqual((line["test_threads"], line["call_id"]), (16, "call-width-16"))
        self.assertEqual((line["repo_identity"], line["test_scope"]), (HANA.key, "full"))

    def test_a_run_is_admitted_on_the_history_of_every_width(self) -> None:
        # One width's history is thin; a run asking for 32 threads may claim 4 after admission.
        (self.root / "admission").mkdir()
        with (self.root / "admission/anon_peaks.jsonl").open("a") as out:
            for _ in range(5):
                _ = out.write(json.dumps({"host": "test-host", "repo": "hana", "step": "nextest",
                                          "ended_at": store.utc_iso(HISTORY_AT.timestamp()),
                                          "anon_peak_bytes": 2 * GIB, "test_threads": 4}) + "\n")
        decision, path = self.check(4 * GIB, argv=["cargo", "nextest", "run", "--test-threads", "32"])
        self.assertEqual(decision.state, "admit")
        self.assertIsNotNone(path)

    def test_the_width_claimed_after_admission_is_the_one_recorded(self) -> None:
        _ = self.sidecar.write_text("unused\n")
        _, path = self.check(12 * GIB, sidecar=str(self.sidecar),
                             argv=["cargo", "nextest", "run", "--test-threads", "32"])
        assert path is not None
        gate.set_width(path, 8, self.root)
        _ = path.with_suffix(".max").write_text(str(GIB))
        with patch.object(store, "host_name", return_value="test-host"):
            gate.release(path, 0, self.root)
        line = cast(dict[str, object], json.loads((self.root / "admission/anon_peaks.jsonl").read_text()))
        self.assertEqual(line["test_threads"], 8)

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

    def held_reservation(self, sidecar: str, admitted: datetime) -> None:
        """A live reservation of this process, admitted at admitted, for 12 GiB."""
        row: gate.Reservation = {"pid": self.pid, "start_time": gate.process_start_time(self.pid) or "",
                                 "need": 12 * GIB, "repo": "hana", "step": "clippy", "worktree": "old",
                                 "admitted_at": store.utc_iso(admitted.timestamp()), "sidecar": sidecar,
                                 "test_threads": None, "call_id": None}
        _ = (gate.admission_dir(self.root) / f"{self.pid}-held.reservation").write_text(json.dumps(row))

    def test_step_reserves_growth_only_inside_the_ramp_window(self) -> None:
        self.held_reservation(str(self.sidecar), NOW - timedelta(seconds=gate.RAMP_SECONDS - 1))
        decision, _ = self.check(12 * GIB)
        self.assertEqual((decision.promised, decision.measured), (4 * GIB, 8 * GIB))
        self.held_reservation(str(self.sidecar), NOW - timedelta(seconds=gate.RAMP_SECONDS))
        decision, _ = self.check(12 * GIB)
        self.assertEqual((decision.promised, decision.measured), (0, 8 * GIB))

    def test_unscoped_step_counts_only_measured_use_after_the_ramp_window(self) -> None:
        self.held_reservation("", NOW - timedelta(hours=1))
        decision, _ = self.check(12 * GIB)
        self.assertEqual((decision.promised, decision.measured), (0, 8 * GIB))

    def test_held_step_waits_in_line_and_an_old_waiter_goes_first(self) -> None:
        _ = self.meminfo.write_text("MemAvailable: 0 kB\n")
        held = gate.check(self.meminfo, self.pid, "", ["cargo", "check"], NOW.timestamp() - 1000, root=self.root, now=NOW)
        self.assertEqual((held.decision.state, held.reservation), ("hold", gate.NotReserved("held")))
        waiting = list((self.root / "admission").glob("*.waiting"))
        self.assertEqual(len(waiting), 1)
        older = cast(dict[str, object], json.loads(waiting[0].read_text()))
        _ = waiting[0].rename(waiting[0].with_name("other.waiting"))
        _ = self.meminfo.write_text(f"MemAvailable: {20 * GIB // 1024} kB\n")
        with mock.patch.object(gate, "process_start_time", return_value=str(older["start_time"])):
            younger = gate.check(self.meminfo, self.pid + 1, "", ["cargo", "check"], root=self.root, now=NOW)
        self.assertEqual(younger.decision.threshold - younger.peak.bytes - younger.decision.reserve, older["need"])

    def test_no_argv_reads_live_promise_without_reserving(self) -> None:
        _, path = self.check(12 * GIB)
        assert path is not None
        result = gate.check(self.meminfo, self.pid, "", [], root=self.root, now=NOW)
        self.assertEqual((result.decision.state, result.decision.promised, len(result.live)), ("hold", 4 * GIB, 1))
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
        def grow(_row: gate.Reservation, _now: datetime, _processes: dict[int, gate.ProcessIdentity],
                 _proc: Path) -> gate.StepMemory:
            _ = self.meminfo.write_text("MemAvailable: 8388608 kB\n")
            return gate.MeasuredMemory(8 * GIB)
        with patch.object(gate, "step_memory", side_effect=grow):
            result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW)
        self.assertEqual((result.available, result.decision.state), (8 * GIB, "hold"))

    def test_profile_lookups_run_once_across_three_polls(self) -> None:
        with patch.object(gate, "git_identity", return_value=(HANA, "tree")) as identity, \
             patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(12 * GIB, "fallback", 0)) as history:
            profile = None
            for _ in range(3):
                result = gate.check(self.meminfo, self.pid, "", ["cargo", "nextest"], root=self.root, now=NOW, profile=profile)
                profile = result.profile
        self.assertEqual(identity.call_count, 1)
        self.assertEqual(history.call_count, 1)


class MemoryQueueFixture(unittest.TestCase):
    root: Path = Path()
    meminfo: Path = Path()
    processes: dict[int, str] = {}
    readings: dict[int, gate.MeasuredMemory | gate.GoneMemory] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.meminfo = self.root / "meminfo"
        self.processes = {100: "first", 200: "second", 300: "head", 400: "warmup"}
        self.readings = {100: gate.MeasuredMemory(GIB), 200: gate.MeasuredMemory(2 * GIB)}
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root), "BUILDLOG_PROC": str(self.root / "proc"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        _ = self.enterContext(patch.object(gate, "process_start_time", side_effect=self.processes.get))
        def table(_proc: Path) -> dict[int, gate.ProcessIdentity]:
            return {pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}
        _ = self.enterContext(patch.object(gate, "process_table", side_effect=table))
        def reading(pid: int, _start: str, _processes: dict[int, gate.ProcessIdentity],
                    _proc: Path) -> gate.MeasuredMemory | gate.GoneMemory:
            return self.readings[pid]
        _ = self.enterContext(patch.object(gate, "tree_memory", side_effect=reading))

    def running(self, pid: int, need: float, age: float = 180) -> Path:
        row: gate.Reservation = {"pid": pid, "start_time": self.processes[pid], "need": int(need * GIB),
                                 "repo": "test", "step": "nextest", "worktree": "test", "sidecar": "",
                                 "admitted_at": store.utc_iso(NOW.timestamp() - age),
                                 "test_threads": None, "call_id": None}
        path = gate.admission_dir(self.root) / f"{pid}.reservation"
        _ = path.write_text(json.dumps(row))
        return path

    def head(self, need: float = 10.9) -> Path:
        row: gate.Waiter = {"pid": 300, "start_time": "head", "need": int(need * GIB),
                            "since": NOW.timestamp() - 2400, "label": "clone88ci nextest (verify pid 300)"}
        path = gate.admission_dir(self.root) / "300-head.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def check(self, pid: int = 400, need: float = 11.6, free: float = 26,
              since: float = 0) -> gate.CheckResult:
        _ = self.meminfo.write_text(f"MemAvailable: {int(free * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        return gate.check(self.meminfo, pid, "", ["cargo", "nextest"], since,
                          root=self.root, now=NOW,
                          profile=gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(int(need * GIB), "measured", 5)))



class MeasuredAdmissionTests(MemoryQueueFixture):
    def test_five_mature_steps_with_43_point_4_promised_admit_10_point_8_on_6_point_8_used(self) -> None:
        for pid in (100, 200, 500, 600, 700):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.36 * GIB))
            _ = self.running(pid, 8.68)
        _ = self.meminfo.write_text(f"MemAvailable: {int(18.1 * GIB) // 1024} kB\nMemTotal: {int(60.5 * GIB) // 1024} kB\n")
        result = gate.check(self.meminfo, 400, "", ["cargo", "check"], root=self.root, now=NOW,
                            profile=gate.StepProfile("test", "check", "test", gate.ExpectedPeak(int(10.8 * GIB), "measured", 5)))
        self.assertIsInstance(result.reservation, gate.Reserved)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))
        self.assertAlmostEqual(result.decision.charged / GIB, 6.8)

    def test_mature_steps_using_12_gib_hold_10_point_8_against_the_budget(self) -> None:
        _ = self.running(100, 43.4)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        result = self.check(need=10.8, free=18.1)
        self.assertEqual((result.decision.state, result.decision.charged), ("hold", 12 * GIB))
        self.assertGreater(result.available, result.decision.threshold)
        with patch.dict(os.environ, {"BUILDLOG_MEMINFO": str(self.meminfo), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")}):
            view = gate.read_queue(gate.admission_dir(self.root), gate.process_table(gate.PROC), NOW.timestamp())[0]
        self.assertEqual(view.waiting[0].reason, result.head_reason)
        self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)

    def test_two_10_point_8_steps_cannot_start_together_inside_ramp(self) -> None:
        self.readings[100] = gate.MeasuredMemory(0)
        first = self.check(pid=100, need=10.8, free=40)
        self.assertIsInstance(first.reservation, gate.Reserved)
        second = self.check(pid=200, need=10.8, free=40)
        self.assertEqual((second.decision.state, second.decision.charged), ("hold", int(10.8 * GIB)))

    def test_22_gib_step_runs_alone_despite_the_budget(self) -> None:
        result = self.check(need=22, free=30)
        self.assertIsInstance(result.reservation, gate.Reserved)

    def test_memory_floor_still_holds_with_room_in_the_budget(self) -> None:
        _ = self.running(100, 12)
        result = self.check(need=10.8, free=9)
        self.assertEqual(result.decision.state, "hold")
        self.assertLess(result.decision.charged + result.peak.bytes, result.decision.budget)

    def test_ramp_charge_covers_measured_growth_above_the_promise(self) -> None:
        _ = self.running(100, 2, age=gate.RAMP_SECONDS - 1)
        self.readings[100] = gate.MeasuredMemory(12 * GIB)
        result = self.check(need=10, free=40)
        self.assertEqual((result.decision.state, result.decision.charged, result.decision.promised), ("hold", 12 * GIB, 0))

    def test_head_budget_keeps_room_before_warmup(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.head()
        result = self.check()
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual((result.decision.promised, result.decision.measured), (0, 3 * GIB))
        self.assertEqual((result.head_need, result.head_label), (int(10.9 * GIB), "clone88ci nextest (verify pid 300)"))

    def test_dead_steps_promise_leaves_the_sum_at_once(self) -> None:
        path = self.running(100, 23.5, age=0)
        del self.processes[100]
        result = self.check(free=15)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))
        self.assertFalse(path.exists())

    def test_fresh_step_keeps_its_promise_then_uses_its_first_reading(self) -> None:
        _ = self.running(100, 23.5, age=0)
        self.assertEqual(self.check().decision.promised, int(22.5 * GIB))
        _ = self.running(100, 23.5, age=gate.RAMP_SECONDS)
        result = self.check()
        self.assertEqual((result.decision.promised, result.decision.measured), (0, GIB))

    def test_clone88ci_head_starts_when_measured_free_memory_covers_it(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        head = self.head()
        result = self.check(pid=300, need=10.9, free=35, since=NOW.timestamp() - 2400)
        self.assertEqual((result.decision.state, result.head_need), ("admit", 0))
        self.assertFalse(head.exists())

    def test_head_beyond_memory_released_by_running_steps_does_not_hold_smaller_steps(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(20)
        result = self.check(need=2, free=8)
        self.assertEqual((result.decision.state, result.head_need), ("admit", 0))

    def test_head_that_fits_after_measured_steps_end_holds_smaller_steps(self) -> None:
        _ = self.running(100, 30)
        self.readings[100] = gate.MeasuredMemory(int(21.5 * GIB))
        _ = self.head(19.3)
        result = self.check(need=2, free=15)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual((result.head_need, result.head_label),
                         (int(19.3 * GIB), "clone88ci nextest (verify pid 300)"))
        self.assertIsInstance(result.reservation, gate.NotReserved)

    def test_pending_start_promise_cannot_make_a_head_fit_after_drain(self) -> None:
        _ = self.running(100, 12, age=0)
        _ = self.head(20)
        result = self.check(need=2, free=18)
        self.assertEqual((result.decision.state, result.head_need, result.decision.promised),
                         ("admit", 0, 11 * GIB))

    def test_dead_head_is_removed_without_charging_its_need(self) -> None:
        head = self.head()
        del self.processes[300]
        result = self.check(free=15)
        self.assertEqual((result.decision.state, result.head_need), ("admit", 0))
        self.assertFalse(head.exists())

    def test_reading_a_finished_tree_does_not_keep_a_promise(self) -> None:
        _ = self.running(100, 23.5)
        self.readings[100] = gate.GoneMemory()
        result = self.check(free=15)
        self.assertEqual((result.decision.state, result.decision.promised), ("admit", 0))

    def test_check_charges_only_measured_use_after_ramp(self) -> None:
        path = self.running(100, 12)
        for used, state in ((1, "admit"), (11, "admit"), (13, "hold")):
            with self.subTest(measured_gib=used):
                self.readings[100] = gate.MeasuredMemory(used * GIB)
                result = self.check(need=8, free=15)
                self.assertEqual((result.decision.state, result.decision.promised), (state, 0))
                for reservation in (self.root / "admission").glob("*.reservation"):
                    if reservation != path:
                        reservation.unlink()


class PressureAdmissionTests(MemoryQueueFixture):
    pressure: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.pressure = self.root / "pressure"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMORY_PRESSURE": str(self.pressure),
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
        })))

    def assert_admission(self, held: bool) -> None:
        result = self.check(need=8, free=40)
        self.assertEqual(result.decision.state, "hold" if held else "admit")
        self.assertEqual(isinstance(result.reservation, gate.Reserved), not held)
        if isinstance(result.reservation, gate.Reserved):
            result.reservation.path.unlink()
        else:
            view, _ = gate.read_queue(self.root / "admission", gate.process_table(self.root / "proc"), NOW.timestamp())
            self.assertEqual(view.waiting[0].reason, result.head_reason)

    def test_full_memory_stall_holds_only_above_ten(self) -> None:
        for full in (11, 9, 10):
            with self.subTest(full_avg10=full):
                _ = self.pressure.write_text(f"some avg10=71.0 avg60=1.0 total=1\nfull avg10={full} avg60=1.0 total=1\n")
                self.assert_admission(full > 10)

    def test_idle_swap_at_ninety_nine_percent_does_not_hold_low_pressure_admission(self) -> None:
        _ = self.pressure.write_text("full avg10=4.0 avg60=1.0 total=1\n")
        _ = self.meminfo.write_text(
            f"MemAvailable: {40 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n"
            + "SwapTotal: 100 kB\nSwapFree: 1 kB\n")
        profile = gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(8 * GIB, "measured", 5))
        result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root, now=NOW, profile=profile)
        self.assertEqual(result.decision.state, "admit")
        self.assertIsInstance(result.reservation, gate.Reserved)
        self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)

    def test_unreadable_pressure_does_not_hold_but_memory_still_does(self) -> None:
        self.pressure.mkdir()
        self.assert_admission(False)
        result = self.check(need=8, free=1)
        self.assertEqual(result.decision.state, "hold")
        self.assertIsInstance(result.decision.pressure, gate.NoPressureHold)

    def test_pressure_is_in_checker_summary_even_with_no_running_step(self) -> None:
        _ = self.pressure.write_text("full avg10=23.4 avg60=1.0 total=1\n")
        _ = self.meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\n")
        arguments = ["memory_admit.py", "check", str(self.meminfo), "400", "", "0", "repo", "check",
                     "tree", str(8 * GIB), "fallback", "0", "cargo", "check"]
        result = self.check(need=8, free=40)
        output = io.StringIO()
        with patch.object(gate, "check", return_value=result), patch.object(sys, "argv", arguments), \
             patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": "1"}), redirect_stdout(output):
            gate.main()
        fields = output.getvalue().strip().split("|")
        self.assertEqual(fields[0], "hold")
        self.assertEqual(fields[8], "head: memory pressure: full avg10 23.4; younger steps may start until 18:10")
        self.assertNotIn("swap", fields[8])
        self.assertEqual(fields[6], "0")


class QueueApiTests(MemoryQueueFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "BUILDLOG_MEMINFO": str(self.meminfo), "BUILDLOG_PROC": str(self.root / "proc"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        self.processes[500] = "third"
        processes = {pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}
        _ = self.enterContext(patch.object(gate, "process_table", return_value=processes))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        def reading(pid: int, _start: str, _table: dict[int, gate.ProcessIdentity], _proc: Path) -> gate.MeasuredMemory | gate.GoneMemory:
            return self.readings[pid]
        _ = self.enterContext(patch.object(gate, "tree_memory", side_effect=reading))
        _ = self.meminfo.write_text(f"MemAvailable: {int(23.1 * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def waiter(self, pid: int, since: float, folder: str, name: str) -> Path:
        row: gate.Waiter = {"pid": pid, "start_time": self.processes[pid], "since": since, "need": int(10.9 * GIB),
                            "session_id": f"session-{pid}", "session_name": name, "step": "nextest", "folder": folder}
        path = gate.admission_dir(self.root) / f"{pid}-{self.processes[pid]}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def queue(self) -> tuple[Path, Path, Path]:
        return (self.waiter(300, NOW.timestamp() - 2400, "/tmp/clone88ci", "clone88ci"),
                self.waiter(400, NOW.timestamp() - 300, "/tmp/warmup-tree", "warmup"),
                self.waiter(500, NOW.timestamp() - 60, "/tmp/third", "third"))

    def test_waiter_identity_keeps_the_bare_step_and_caller_identity(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker-id", "CLAUDE_CODE_SESSION_ID": "parent-id",
                                     "CLAUDE_SESSION_NAME": "recorded-name"}), \
             patch("os.getcwd", return_value="/tmp/hana-tree"):
            for step in ("nextest", "clippy", "mend", "doc"):
                with self.subTest(step=step):
                    self.assertEqual(gate.waiter_identity(step, "hana-tree"), {
                        "session_id": "worker-id", "session_name": "recorded-name",
                        "step": step, "folder": "/tmp/hana-tree",
                    })

    def test_held_step_is_stored_and_read_without_the_repository_prefix(self) -> None:
        _ = self.meminfo.write_text(f"MemAvailable: 0 kB\nMemTotal: {60 * GIB // 1024} kB\n")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker-id", "CLAUDE_SESSION_NAME": "recorded-name"}):
            result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root, now=NOW,
                                profile=gate.StepProfile("hana", "nextest", "hana-tree",
                                                         gate.ExpectedPeak(12 * GIB, "measured", 5), HANA))
        self.assertEqual(result.decision.state, "hold")
        row = cast(gate.Waiter, json.loads((self.root / "admission/400-warmup.waiting").read_text()))
        self.assertEqual(row.get("step"), "nextest")
        self.assertEqual(row.get("repository_name"), "hana")
        view = gate.queue_view()
        self.assertEqual([(step.session_id, step.step) for step in view.waiting], [("worker-id", "nextest")])

    def test_behind_head_carries_head_identity_and_unmatched_name(self) -> None:
        for name, expected_name in (("clone88ci", "clone88ci"), ("", "clone88ci")):
            with self.subTest(recorded_name=name):
                _ = self.waiter(300, NOW.timestamp() - 2400, "/tmp/clone88ci", name)
                _ = self.waiter(400, NOW.timestamp() - 300, "/tmp/warmup-tree", "warmup")
                view = gate.queue_view()
                self.assertEqual(view.waiting[1].reason, gate.BehindHead(
                    head_name=expected_name, head_since=NOW.timestamp() - 2400, head_session_id="session-300"))

    def test_view_counts_measured_use_after_ramp_and_keeps_the_head(self) -> None:
        _ = self.running(100, 12)
        _ = self.running(200, 11.5)
        _ = self.queue()
        with patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             patch("time.sleep", side_effect=AssertionError("no wait")), \
             patch.object(gate, "process_table", wraps=gate.process_table) as read:
            view = gate.queue_view()
        self.assertEqual(read.call_count, 1)
        self.assertEqual((view.promised, sum(step.promise for step in view.running)), (0, int(23.5 * GIB)))
        self.assertCountEqual([step.measured for step in view.running],
                              [gate.MeasuredMemory(GIB), gate.MeasuredMemory(2 * GIB)])
        self.assertEqual(view.waiting[0].session_name, "clone88ci")
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "admit")
        self.assertTrue(all(isinstance(row.reason, gate.BehindHead) for row in view.waiting[1:]))

    def test_queue_view_names_budget_before_free_memory_when_both_hold_the_head(self) -> None:
        _ = self.running(100, 12, age=0)
        _ = self.running(200, 11.5, age=0)
        _ = self.queue()
        view = gate.queue_view()
        self.assertEqual(view.promised, int(20.5 * GIB))
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if isinstance(reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York")),
                             "head: needs 10.9 GiB; builds use 23.5 of the 20.0 GiB budget "
                             + "(starts when build use falls to 9.1 GiB); needs 34.4 GiB free; 23.1 free; younger steps wait until 18:10")
        self.assertCountEqual([row.measured for row in view.running],
                              [gate.MeasuredMemory(GIB, 12 * GIB), gate.MeasuredMemory(2 * GIB, int(11.5 * GIB))])

    def test_backfill_at_nine_minutes_becomes_sticky_at_ten_minutes(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.waiter(300, NOW.timestamp() - 540, "/tmp/head", "head")
        head = self.check(pid=300, need=19.3, free=20)
        self.assertEqual(head.decision.state, "hold")
        self.assertEqual(gate.queue_view().waiting[0].reason, head.head_reason)
        self.assertIsInstance(head.head_reason, gate.HeadAdmissionStatus)
        if isinstance(head.head_reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(head.head_reason, ZoneInfo("America/New_York")),
                             "head: needs 19.3 GiB; builds use 3.0 of the 20.0 GiB budget "
                             + "(starts when build use falls to 0.7 GiB); needs 22.3 GiB free; 20.0 free; "
                             + "younger steps may start until 18:01")
        first = self.check(need=1, free=20)
        self.assertIsInstance(first.reservation, gate.Reserved)
        if isinstance(first.reservation, gate.Reserved):
            first.reservation.path.unlink()
        second = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root,
                            now=NOW + timedelta(seconds=60), profile=first.profile)
        self.assertEqual(second.decision.state, "hold")
        self.assertTrue(cast(gate.Waiter, json.loads(path.read_text())).get("holds_younger", False))
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")

    def test_strict_backfill_strict_windows_keep_queue_and_admission_in_agreement(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        self.readings[400] = gate.MeasuredMemory(0)
        _ = self.meminfo.write_text(f"MemAvailable: {20 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        for elapsed, state, deadline, window in ((0, "hold", "18:10", "wait"),
                                                  (600, "admit", "18:12", "may start"),
                                                  (720, "hold", "18:22", "wait")):
            clock = NOW + timedelta(seconds=elapsed)
            if elapsed == 600:
                with patch.object(time, "time", return_value=clock.timestamp()):
                    before = gate.queue_view()
                self.assertIsInstance(before.waiting[1].reason, gate.ReadyForAdmission)
            profile = gate.StepProfile("test", "nextest", "test", gate.ExpectedPeak(GIB, "measured", 5))
            result = gate.check(self.meminfo, 400, "", ["cargo", "nextest"], root=self.root,
                                now=clock, profile=profile)
            self.assertEqual(result.decision.state, state)
            with patch.object(time, "time", return_value=clock.timestamp()):
                view = gate.queue_view()
            text = gate.head_wait_text(cast(gate.HeadAdmissionStatus, view.waiting[0].reason),
                                       ZoneInfo("America/New_York"))
            self.assertTrue(text.endswith(f"younger steps {window} until {deadline}"), text)
            if state == "hold":
                self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
            else:
                # While this child runs, the head still names its backfill window.
                self.assertIsInstance(result.reservation, gate.Reserved)
                if isinstance(result.reservation, gate.Reserved):
                    result.reservation.path.unlink()
            row = cast(gate.Waiter, json.loads(path.read_text()))
            self.assertEqual(row.get("priority_since"), NOW.timestamp())

    def test_bump_restarts_strict_window_during_backfill(self) -> None:
        path = self.waiter(300, NOW.timestamp() - 2400, "/tmp/head", "head")
        row = cast(gate.Waiter, json.loads(path.read_text()))
        row["holds_younger"], row["priority_since"] = True, NOW.timestamp() - 600
        _ = path.write_text(json.dumps(row))
        self.assertIsInstance(gate.head_window(row, NOW.timestamp()), gate.BackfillUntil)
        _ = gate.bump_waiter("head", "actor")
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        row = cast(gate.Waiter, json.loads(path.read_text()))
        self.assertEqual(row.get("priority_since"), NOW.timestamp())
        self.assertIsInstance(gate.head_window(row, NOW.timestamp()), gate.StrictUntil)

    def test_psi_11_holds_backfill_without_making_the_young_head_strict(self) -> None:
        _ = self.waiter(300, NOW.timestamp() - 540, "/tmp/head", "head")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            result = self.check(need=1, free=40)
            self.assertEqual(result.decision.state, "hold")
            self.assertEqual(result.decision.pressure, gate.MemoryPressure(11))
            self.assertIsInstance(gate.queue_view().waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(result.head_need, 0)

    def test_head_hold_survives_rising_free_memory_and_falling_measured_use(self) -> None:
        _ = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        first = self.check(need=1, free=20)
        self.assertEqual(first.decision.state, "hold")
        self.assertTrue(cast(gate.Waiter, json.loads(path.read_text())).get("holds_younger", False))
        self.readings[100] = gate.MeasuredMemory(0)
        second = self.check(need=1, free=21.9)
        self.assertEqual(second.decision.state, "hold")
        view = gate.queue_view()
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if isinstance(reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(reason, ZoneInfo("America/New_York")), "head: needs 22.3 GiB free; 21.9 free; younger steps wait until 18:10")
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)

    def test_mature_builds_hold_head_on_13_point_2_used_not_72_point_3_promised(self) -> None:
        for pid in range(500, 510):
            self.processes[pid] = str(pid)
            self.readings[pid] = gate.MeasuredMemory(int(1.32 * GIB))
            _ = self.running(pid, 7.23)
        _ = self.enterContext(patch.object(gate, "process_table", return_value={
            pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()
        }))
        _ = self.head(15.1)
        result = self.check(pid=300, need=15.1, free=19.5)
        self.assertEqual(result.decision.state, "hold")
        self.assertAlmostEqual(sum(row["need"] for row in result.live) / GIB, 72.3)
        self.assertAlmostEqual(result.decision.charged / GIB, 13.2)
        self.assertGreater(result.available, result.decision.threshold)
        view = gate.queue_view()
        self.assertEqual(view.waiting[0].reason, result.head_reason)
        self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)
        if isinstance(result.head_reason, gate.HeadAdmissionStatus):
            text = gate.head_wait_text(result.head_reason, ZoneInfo("America/New_York"))
            self.assertEqual(text, "head: needs 15.1 GiB; builds use 13.2 of the 20.0 GiB budget "
                             + "(starts when build use falls to 4.9 GiB); younger steps wait until 18:10")
            arguments = ["memory_admit.py", "check", str(self.meminfo), "300", "", "0",
                         "test", "nextest", "test", str(int(15.1 * GIB)), "measured", "5", "cargo", "nextest"]
            for detail in ("", "1"):
                output = io.StringIO()
                with patch.object(gate, "check", return_value=result), patch.object(sys, "argv", arguments), \
                     patch.dict(os.environ, {"BUILDLOG_MEM_DETAIL": detail}), redirect_stdout(output):
                    gate.main()
                self.assertIn(text, output.getvalue().split("|")[8])

    def test_head_names_only_free_memory_when_budget_fits(self) -> None:
        _ = self.running(100, 1)
        _ = self.head(15.1)
        result = self.check(pid=300, need=15.1, free=18)
        self.assertEqual(result.decision.state, "hold")
        self.assertLess(result.decision.charged + result.peak.bytes, result.decision.budget)
        self.assertEqual(gate.queue_view().waiting[0].reason, result.head_reason)
        self.assertIsInstance(result.head_reason, gate.HeadAdmissionStatus)
        if isinstance(result.head_reason, gate.HeadAdmissionStatus):
            self.assertEqual(gate.head_wait_text(result.head_reason, ZoneInfo("America/New_York")), "head: needs 18.1 GiB free; 18.0 free; younger steps wait until 18:10")

    def test_drained_head_starts_below_memory_floor_and_a_younger_check_cannot_take_its_place(self) -> None:
        running = self.running(100, 12)
        self.readings[100] = gate.MeasuredMemory(3 * GIB)
        path = self.head(19.3)
        self.assertEqual(self.check(need=10.8, free=20).decision.state, "hold")
        running.unlink()
        younger = self.check(need=10.8, free=21.9)
        self.assertEqual(younger.decision.state, "hold")
        view = gate.queue_view()
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "admit")
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)
        head = self.check(pid=300, need=19.3, free=21.9)
        self.assertLess(head.available, head.decision.threshold)
        self.assertIsInstance(head.reservation, gate.Reserved)
        self.assertFalse(path.exists())

    def test_head_start_removes_priority_so_younger_steps_use_normal_limits(self) -> None:
        path = self.head(2)
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        self.readings[300] = gate.MeasuredMemory(0)
        head = self.check(pid=300, need=2, free=40)
        self.assertIsInstance(head.reservation, gate.Reserved)
        self.assertFalse(path.exists())
        younger = self.check(need=1, free=40)
        self.assertEqual((younger.decision.state, younger.head_need), ("admit", 0))

    def test_removed_head_row_releases_younger_steps_to_normal_limits(self) -> None:
        path = self.head(19.3)
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        path.unlink()
        younger = self.check(need=1, free=21.9)
        self.assertEqual((younger.decision.state, younger.head_need), ("admit", 0))

    def test_psi_11_holds_a_drained_head_without_releasing_younger_steps(self) -> None:
        _ = self.head(19.3)
        self.assertEqual(self.check(need=1, free=40).decision.state, "hold")
        pressure = self.root / "pressure"
        _ = pressure.write_text("full avg10=11.0 avg60=1.0 total=1\n")
        with patch.dict(os.environ, {"BUILDLOG_MEMORY_PRESSURE": str(pressure)}):
            head = self.check(pid=300, need=19.3, free=21.9)
            self.assertEqual(head.decision.state, "hold")
            view = gate.queue_view()
        self.assertEqual(view.waiting[0].reason, head.head_reason)
        self.assertIsInstance(head.head_reason, gate.HeadAdmissionStatus)
        self.assertIsInstance(view.waiting[1].reason, gate.BehindHead)

    def test_bump_by_position_session_id_session_name_and_folder_starts_the_selected_waiter_first(self) -> None:
        for selector in ("2", "session-400", "warmup", "/tmp/warmup-tree", "warmup-tree"):
            with self.subTest(selector=selector):
                for path in gate.admission_dir(self.root).glob("*.reservation"):
                    path.unlink()
                _ = self.queue()
                moved = gate.bump_waiter(selector, "actor")
                self.assertEqual(moved.pid, 400)
                self.assertEqual(gate.queue_view().waiting[0].pid, 400)
                older = self.check(pid=300, free=40, since=NOW.timestamp() - 2400)
                self.assertEqual(older.decision.state, "hold")
                selected = self.check(pid=400, free=40, since=NOW.timestamp() - 300)
                self.assertEqual(selected.decision.state, "admit")
                self.assertFalse((self.root / "admission/400-warmup.waiting").exists())

    def test_moved_waiter_keeps_its_wait_time_and_head_position_after_another_check(self) -> None:
        paths = self.queue()
        original_waiters = gate.queue_view().waiting
        original = original_waiters[-1]
        _ = gate.bump_waiter("warmup", "actor")
        moved = gate.bump_waiter("third", "actor")
        self.assertEqual((moved.pid, moved.since), (original.pid, original.since))
        waiting = gate.queue_view().waiting
        self.assertEqual([row.pid for row in waiting], [500, 400, 300])
        self.assertEqual({row.pid: row.since for row in waiting}, {row.pid: row.since for row in original_waiters})
        head = waiting[0]
        self.assertEqual((head.pid, head.since), (original.pid, original.since))
        self.assertIsInstance(head.reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, head.reason).decision.state, "admit")
        with patch.object(gate, "read_pressure", return_value=gate.MemoryPressure(11)):
            held = self.check(pid=original.pid, free=0, since=original.since)
        self.assertEqual(held.decision.state, "hold")
        row = cast(dict[str, object], json.loads(paths[-1].read_text()))
        self.assertEqual(row["since"], original.since)
        head = gate.queue_view().waiting[0]
        self.assertEqual((head.pid, head.since), (original.pid, original.since))
        previous_head = self.check(pid=400, free=40, since=original_waiters[1].since)
        self.assertEqual(previous_head.decision.state, "hold")
        older = self.check(pid=300, free=40, since=NOW.timestamp() - 2400)
        self.assertEqual(older.decision.state, "hold")
        selected = self.check(pid=original.pid, free=40, since=original.since)
        self.assertEqual(selected.decision.state, "admit")

    def test_unknown_and_ambiguous_selectors_name_matches_and_move_nothing(self) -> None:
        _ = self.waiter(300, NOW.timestamp() - 2400, "/one/shared", "first-session")
        _ = self.waiter(400, NOW.timestamp() - 300, "/two/shared", "second-session")
        before = {path: path.read_text() for path in (self.root / "admission").glob("*.waiting")}
        for selector, word in (("missing", "unknown"), ("99", "unknown"), ("shared", "ambiguous")):
            with self.assertRaisesRegex(ValueError, word) as result:
                _ = gate.bump_waiter(selector, "actor")
            if selector == "shared":
                self.assertIn("first-session", str(result.exception))
                self.assertIn("second-session", str(result.exception))
            self.assertEqual({path: path.read_text() for path in before}, before)
        self.assertEqual(list(self.root.glob("*/*.jsonl")), [])

    def test_bump_writes_one_row_in_the_private_hold_event_log(self) -> None:
        _ = self.queue()
        _ = gate.bump_waiter("2", "actor-id")
        rows = [cast(dict[str, object], json.loads(line)) for path in self.root.glob("*/*.jsonl")
                for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({key: row[key] for key in ("kind", "actor", "session", "step", "folder", "old_position", "at")},
                         {"kind": "queue_bump", "actor": "actor-id", "session": "session-400", "step": "nextest",
                          "folder": "/tmp/warmup-tree", "old_position": 2, "at": store.utc_iso(NOW.timestamp())})

    def test_queue_view_and_check_agree_on_heads_that_can_and_cannot_fit_after_drain(self) -> None:
        _ = self.running(100, 30)
        self.readings[100] = gate.MeasuredMemory(int(21.5 * GIB))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "30"})))
        for need, held in ((19.3, True), (38, False)):
            with self.subTest(head_need=need):
                path = self.head(need)
                row = cast(gate.Waiter, json.loads(path.read_text()))
                row["session_name"] = "clone88ci"
                _ = path.write_text(json.dumps(row))
                result = self.check(need=2, free=15)
                if not held:
                    assert isinstance(result.reservation, gate.Reserved)
                    result.reservation.path.unlink()
                    _ = self.waiter(400, NOW.timestamp(), "/tmp/warmup-tree", "warmup")
                    waiter_path = self.root / "admission/400-warmup.waiting"
                    follower = cast(gate.Waiter, json.loads(waiter_path.read_text()))
                    follower["need"] = 2 * GIB
                    _ = waiter_path.write_text(json.dumps(follower))
                view = gate.queue_view()
                reason = next(step.reason for step in view.waiting if step.pid == 400)
                if held:
                    self.assertEqual(result.decision.state, "hold")
                    self.assertEqual(reason, gate.BehindHead("clone88ci", NOW.timestamp() - 2400, ""))
                else:
                    self.assertEqual(result.decision.state, "admit")
                    self.assertIsInstance(reason, gate.ReadyForAdmission)

    def test_queue_view_waits_for_lock_and_keeps_real_reasons_without_pruning(self) -> None:
        paths = self.queue()
        del self.processes[300]
        _ = self.enterContext(patch.object(gate, "process_table", return_value={
            pid: gate.ProcessIdentity(1, start) for pid, start in self.processes.items()}))
        lock_path = self.root / "admission/lock"
        released = threading.Event()
        previous = signal.getsignal(signal.SIGALRM)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            def release() -> None:
                released.set()
                fcntl.flock(lock, fcntl.LOCK_UN)
            timer = threading.Timer(0.05, release)
            timer.start()
            try:
                view = gate.queue_view()
                self.assertTrue(released.is_set())
            finally:
                timer.cancel()
                timer.join()
        self.assertEqual([row.pid for row in view.waiting], [400, 500])
        self.assertTrue(paths[0].exists())
        self.assertIsInstance(view.waiting[0].reason, gate.HeadAdmissionStatus)
        self.assertEqual(cast(gate.HeadAdmissionStatus, view.waiting[0].reason).decision.state, "admit")
        self.assertIsInstance(view.waiting[1].reason, gate.ReadyForAdmission)
        self.assertEqual(view.available, int(23.1 * GIB) // 1024 * 1024)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_queue_view_times_out_on_held_lock_and_restores_alarm(self) -> None:
        _ = self.queue()
        previous = signal.getsignal(signal.SIGALRM)
        with (self.root / "admission/lock").open("a+") as lock, \
             patch.object(gate, "QUEUE_LOCK_TIMEOUT_SECONDS", 0.03):
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(TimeoutError, "admission lock busy for 0.03 s; try again"):
                _ = gate.queue_view()
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertEqual(len(gate.queue_view().waiting), 3)

    def test_missing_meminfo_is_a_reading_unavailable_reason(self) -> None:
        _ = self.queue()
        self.meminfo.unlink()
        self.assertTrue(all(isinstance(row.reason, gate.ReadingUnavailable) for row in gate.queue_view().waiting))



class ProcessMemoryTests(unittest.TestCase):
    def test_tree_counts_resident_and_swap_and_excludes_other_processes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for pid, parent, state, start, resident, swap in (
                (100, 1, "S", "start", 10, 2), (101, 100, "S", "child", 20, 3),
                (102, 101, "S", "leaf", 30, 4), (103, 1, "S", "other", 500, 400),
                (104, 100, "Z", "dead", 200, 100),
            ):
                path = proc / str(pid)
                path.mkdir()
                fields = [state, str(parent), *(["0"] * 17), start]
                _ = (path / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
                _ = (path / "status").write_text(f"VmRSS: {resident} kB\nVmSwap: {swap} kB\n")
            processes = gate.process_table(proc)
            self.assertEqual(gate.tree_memory(100, "start", processes, proc), gate.MeasuredMemory(69 * 1024))
            self.assertEqual(gate.tree_memory(100, "reused", processes, proc), gate.GoneMemory())
            self.assertEqual(gate.tree_memory(999, "gone", processes, proc), gate.GoneMemory())

    def test_check_reads_one_table_for_promised_measured_and_reused_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / "proc"
            for pid, parent, start, resident, swap in (
                (100, 1, "fresh", 900, 0), (200, 1, "running", 20, 3),
                (201, 200, "child", 30, 4), (300, 1, "reused", 800, 0),
            ):
                path = proc / str(pid)
                path.mkdir(parents=True)
                fields = ["S", str(parent), *(["0"] * 17), start]
                _ = (path / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
                _ = (path / "status").write_text(f"VmRSS: {resident} kB\nVmSwap: {swap} kB\n")
            directory = gate.admission_dir(root)
            for pid, start, age in ((100, "fresh", 0), (200, "running", gate.RAMP_SECONDS), (300, "old", 0)):
                row: gate.Reservation = {
                    "pid": pid, "start_time": start, "need": 4 * GIB, "repo": "test",
                    "step": "check", "worktree": "test", "sidecar": "",
                    "admitted_at": store.utc_iso(NOW.timestamp() - age),
                    "test_threads": None, "call_id": None,
                }
                _ = (directory / f"{pid}.reservation").write_text(json.dumps(row))
            _ = (directory / "300.max").write_text("100")
            meminfo = root / "meminfo"
            _ = meminfo.write_text(f"MemAvailable: {8 * GIB // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
            starts = {100: "fresh", 200: "running", 300: "old", 400: "caller"}
            with patch.dict(os.environ, {"BUILDLOG_DIR": str(root), "BUILDLOG_PROC": str(proc)}), \
                 patch.object(gate, "process_start_time", side_effect=starts.get), \
                 patch.object(gate, "process_table", wraps=gate.process_table) as tables:
                result = gate.check(meminfo, 400, "", ["cargo", "check"], root=root, now=NOW,
                                    profile=gate.StepProfile("test", "check", "test", gate.ExpectedPeak(2 * GIB, "fallback", 0)))
            tables.assert_called_once_with(proc)
            self.assertEqual((result.decision.state, result.decision.promised, result.decision.measured),
                             ("hold", 4 * GIB - 900 * 1024, 957 * 1024))
            self.assertCountEqual([row["pid"] for row in result.live], [100, 200])
            self.assertFalse((directory / "300.reservation").exists())
            self.assertFalse((directory / "300.max").exists())



class ShellTests(unittest.TestCase):
    def test_wait_line_carries_head_need_step_and_measured_memory(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=120; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line "$2" 0 49000000000 11600000000 measured 5 0 2 3000000000 '
                   + '"test steps" warmup nextest 3221225472 11703785881 "clone88ci nextest (verify pid 300)"')
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("first", "periodic"):
                result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), kind],
                                        env={**os.environ, "BUILDLOG_DIR": folder}, capture_output=True,
                                        text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn("head clone88ci nextest (verify pid 300) needs 10.9 GiB", result.stderr)
                self.assertTrue(result.stderr.endswith(
                    "0.0 GiB promised, 3.0 GiB measured across 2 running steps: test steps; "
                    + "2.8 GiB kept for earlyoom)\n"))

    def test_wait_line_shows_reason_in_place_of_zero_running_steps(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=120; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line "$2" 19327352832 19434727014 16213501542 measured 5 0 0 '
                   + '3221225472 "$3" test nextest 0')
        reasons = ("memory pressure: full avg10 11.0",
                   "head: needs 18.1 GiB free; 18.0 free; younger steps may start until 18:10")
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("first", "periodic"):
                for reason in reasons:
                    with self.subTest(kind=kind, reason=reason):
                        result = subprocess.run(
                            ["bash", "-c", command, "test", str(MEMORY_GATE), kind, reason],
                            env={**os.environ, "BUILDLOG_DIR": folder}, capture_output=True,
                            text=True, check=False, timeout=300)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(len(result.stderr.splitlines()), 1)
                        self.assertTrue(result.stderr.endswith(f"; {reason}; 3.0 GiB kept for earlyoom)\n"))
                        self.assertNotIn("0 running steps", result.stderr)
                        self.assertNotIn("GiB promised", result.stderr)
                        self.assertNotIn("GiB measured", result.stderr)

    def test_wait_line_keeps_zero_running_text_when_reason_is_empty(self) -> None:
        command = ('source "$1"; BUILDLOG_MEM_WAIT_S=120; buildlog_memory_python() { echo UTC; }; '
                   + 'buildlog_memory_line "$2" 0 0 0 fallback 0 0 0 0 "" test check 0')
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("first", "periodic"):
                with self.subTest(kind=kind):
                    result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), kind],
                                            env={**os.environ, "BUILDLOG_DIR": folder}, capture_output=True,
                                            text=True, check=False, timeout=300)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertTrue(result.stderr.endswith(
                        "; 0.0 GiB promised, 0.0 GiB measured across 0 running steps)\n"))

    def test_old_shell_keeps_its_field_count_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            directory = gate.admission_dir(root)
            _ = (directory / "400-start.waiting").write_text(json.dumps({
                "pid": 400, "start_time": "start", "need": 12 * GIB, "since": time.time(),
                "repo_identity": "", "test_scope": "full", "estimated_at": time.time(),
            }))
            arguments = ["memory_admit.py", "check", str(meminfo), "400", "", "0", "repo", "nextest",
                         "clone88ci", str(12 * GIB), "fallback", "0", "cargo", "nextest"]
            for detail, count in (("", 15), ("1", 18)):
                output = io.StringIO()
                with patch.object(sys, "argv", arguments), \
                     patch.dict(os.environ, {"BUILDLOG_DIR": folder, "BUILDLOG_MEM_DETAIL": detail}), \
                     patch.object(gate, "process_start_time", return_value="start"), \
                     redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(len(fields), count)
                self.assertEqual(fields[14], "clone88ci")

    def test_shell_replaces_old_need_and_source_with_refreshed_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proc").mkdir()
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            calls = root / "calls.jsonl"
            wrapper = root / "checker.py"
            _ = wrapper.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                + f"sys.path.insert(0, {str(MEMORY_GATE.parent)!r})\nimport memory_admit as gate\n"
                + f"calls = Path({str(calls)!r})\n"
                + "previous = calls.read_text().splitlines() if calls.exists() else []\n"
                + "with calls.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                + "if not previous:\n"
                + "    pid = int(sys.argv[3]); start = gate.process_start_time(pid)\n"
                + "    row = {'pid': pid, 'start_time': start, 'need': int(19.3 * gate.GIB), 'since': float(sys.argv[5])}\n"
                + "    (gate.admission_dir(gate.store.root()) / f'{pid}-{start}.waiting').write_text(json.dumps(row))\n"
                + "    print('|'.join(map(str, ['hold', 23 * gate.GIB, row['need'], 'measured', 7, 0, 0, 3 * gate.GIB, '', '', 'repo', 'nextest', 0, 'held', 'repo', 0, 0, ''])))\n"
                + "else:\n"
                + "    gate.git_identity = lambda argv, cwd: (gate.RepositoryIdentity('https://example.org/hana', 'hana'), 'repo')\n"
                + "    gate.expected_peak = lambda *args, **kwargs: gate.ExpectedPeak(2 * gate.GIB, 'pooled', 5)\n"
                + "    gate.main()\n")
            command = ('source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; sleeps=0; '
                       'sleep() { sleeps=$((sleeps + 1)); if (( sleeps == 2 )); then '
                       'printf "MemAvailable: 41943040 kB\\n" > "$BUILDLOG_MEMINFO"; fi; }; '
                       'buildlog_wait_for_memory cargo nextest')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(wrapper)],
                                    capture_output=True, text=True, check=False, timeout=300,
                                    env={**os.environ, "HOME": folder, "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_PROC": str(root / "proc"),
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"), "UNIT_RUN_DIR": "",
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_HOLD_WAIT_FILE": "",
                                         "BUILDLOG_HOLD_WAITER_FILE": "", "LINT_QUEUE_LEAVE_HOOK": "",
                                         "BUILDLOG_MAC_IDLE_HOOK": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = [cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
            self.assertEqual(len(arguments), 3)
            self.assertEqual((arguments[1][8], arguments[1][9]), (str(int(19.3 * GIB)), "measured"))
            self.assertEqual((arguments[2][8], arguments[2][9]), (str(2 * GIB), "pooled"))
            row = cast(gate.Reservation, json.loads(next((root / "log/admission").glob("*.reservation")).read_text()))
            self.assertEqual((row["need"], row.get("repo_identity")), (2 * GIB, "https://example.org/hana"))

    def test_old_waiter_reestimates_and_current_waiter_refreshes_after_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            path = gate.admission_dir(root) / "400-start.waiting"
            _ = path.write_text(json.dumps({"pid": 400, "start_time": "start",
                                            "need": int(19.3 * GIB), "since": NOW.timestamp()}))
            arguments = ["memory_admit.py", "check", str(meminfo), "400", "", str(NOW.timestamp()),
                         "repo", "nextest", "repo", str(int(19.3 * GIB)), "measured", "7", "cargo", "nextest"]
            with patch.dict(os.environ, {"BUILDLOG_DIR": folder}), \
                 patch.object(gate, "process_start_time", return_value="start"), \
                 patch.object(gate, "process_table", return_value={}), \
                 patch.object(gate, "git_identity", return_value=(HANA, "repo")) as identity, \
                 patch.object(gate, "expected_peak", side_effect=[gate.ExpectedPeak(2 * GIB, "pooled", 5),
                                                                gate.ExpectedPeak(3 * GIB, "measured", 6)]) as history:
                for elapsed, expected_need, expected_source, lookups in (
                        (0, 2 * GIB, "pooled", 1), (599, 2 * GIB, "pooled", 1),
                        (600, 3 * GIB, "measured", 2)):
                    clock = NOW + timedelta(seconds=elapsed)
                    output = io.StringIO()
                    with patch.object(sys, "argv", arguments), redirect_stdout(output), \
                         patch.object(time, "time", return_value=clock.timestamp()), \
                         patch.object(gate, "datetime", wraps=datetime) as dates:
                        dates.configure_mock(**{"now.return_value": clock})
                        gate.main()
                    fields = output.getvalue().strip().split("|")
                    self.assertEqual((int(fields[2]), fields[3]), (expected_need, expected_source))
                    row = cast(gate.Waiter, json.loads(path.read_text()))
                    self.assertEqual((row["need"], row.get("repo_identity"), row.get("test_scope")),
                                     (expected_need, HANA.key, "full"))
                    self.assertEqual(history.call_count, lookups)
                    self.assertEqual(identity.call_count, lookups)
                    arguments = [*arguments[:6], fields[10], fields[11], fields[14],
                                 fields[2], fields[3], fields[4], "cargo", "nextest"]

    def test_cached_poll_keeps_repository_and_filter_scope_in_the_recorded_peak(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            sidecar = root / "step.cgroup"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            _ = sidecar.write_text("unused")
            argv = ["cargo", "nextest", "run", "-E", "test(one)"]
            first = ["memory_admit.py", "check", str(meminfo), "400", str(sidecar), "0",
                     "", "", "", "-", "", "0", *argv]
            output = io.StringIO()
            with patch.dict(os.environ, {"BUILDLOG_DIR": folder}), \
                 patch.object(gate, "process_start_time", return_value="start"), \
                 patch.object(gate, "process_table", return_value={}), \
                 patch.object(gate, "git_identity", return_value=(HANA, "clone")) as identity, \
                 patch.object(gate, "expected_peak", return_value=gate.ExpectedPeak(2 * GIB, "measured", 5)) as history:
                with patch.object(sys, "argv", first), redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(fields[0], "hold")
                cached = [*first[:6], fields[10], fields[11], fields[14], fields[2], fields[3], fields[4], *argv]
                _ = meminfo.write_text(f"MemAvailable: {40 * GIB // 1024} kB\n")
                output = io.StringIO()
                with patch.object(sys, "argv", cached), redirect_stdout(output):
                    gate.main()
                fields = output.getvalue().strip().split("|")
                self.assertEqual(fields[0], "admit")
                reservation = Path(fields[9])
                _ = reservation.with_suffix(".max").write_text(str(GIB))
                gate.release(reservation, 0, root)
            self.assertEqual((identity.call_count, history.call_count), (1, 1))
            sample = cast(dict[str, object], json.loads((root / "admission/anon_peaks.jsonl").read_text()))
            self.assertEqual((sample["repo_identity"], sample["test_scope"]), (HANA.key, "filtered"))

    def test_invoke_twice_with_temporary_home(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            config = root / "config.sh"
            _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
            environment = {**os.environ,
                           "BUILD_HOLD_DIR": str(root / "holders"),
                           "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                           "UNIT_ACTIVE_DIR": str(root / "active"),
                           "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                           "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                           "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_MEMINFO": str(meminfo),
                           "BUILDLOG_OFF": "1", "BUILDLOG_SCOPE": "0",
                           "LINT_CONFIG_READER": str(config)}
            command = 'source "$1"; sweep_after_step() { :; }; run_once true'
            for _ in range(2):
                result = subprocess.run(["bash", "-c", command, "test", str(INVOKE)],
                                        capture_output=True, text=True, timeout=300, check=False,
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
                capture_output=True, text=True, timeout=300, check=False,
                env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
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
            with patch.object(gate, "git_identity", return_value=(HANA, "test-worktree")), \
                 patch.object(store, "host_name", return_value="test-host"):
                meminfo = root / "meminfo"
                _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
                checked = gate.check(meminfo, os.getpid(), str(sidecar), ["cargo", "nextest"], root=root, now=NOW)
            assert isinstance(checked.reservation, gate.Reserved)
            reservation = checked.reservation.path
            maximum = reservation.with_suffix(".max")
            command = 'source "$1"; buildlog_sample_anon "$2" "$3" & p=$!; sleep 1.2; kill "$p"; wait "$p" 2>/dev/null || true'
            result = subprocess.run(["bash", "-c", command, "test", str(INVOKE), str(sidecar), str(maximum)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "BUILDLOG_OFF": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(maximum.read_text().strip(), str(GIB))
            with patch.object(store, "host_name", return_value="test-host"):
                gate.release(reservation, 0, root)
            row = cast(dict[str, object], json.loads((root / "admission/anon_peaks.jsonl").read_text()))
            self.assertEqual(row["anon_peak_bytes"], GIB)

    def test_launch_wait_limit_refuses_without_a_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 11010048 kB\n")
            command = ('set -e; source "$1"; sleep() { SECONDS=$((SECONDS + 1)); }; '
                       'buildlog_wait_for_memory; printf "step started\\n"')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "BUILDLOG_DIR": str(root),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_POLL_S": "1",
                                         "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr.count("waiting for memory since "), 1)
            self.assertIn("(build needs ", result.stderr)
            self.assertIn("memory admission check failed (launch wait limit reached without admission); step refused", result.stderr)
            self.assertFalse((root / "admission").exists())

    def test_gate_wait_lines_in_order_without_repeated_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
            # Advance a test clock so checker runtime cannot change notice order.
            command = (
                'source "$1"; unset SECONDS; SECONDS=0; calls=0; sleep() { calls=$((calls + 1)); '
                'if (( calls == 1 )); then SECONDS=61; else printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; fi; }; '
                'buildlog_wait_for_memory'
            )
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stderr.splitlines()
            self.assertEqual(len(lines), 3, lines)
            self.assertTrue(lines[0].startswith("waiting for memory since "))
            self.assertTrue(lines[1].startswith("still waiting for memory (1 min): "))
            self.assertRegex(lines[2], r"^memory free after 1 min \d+ s: starting$")
            self.assertEqual(result.stderr.count("waiting for memory since "), 1)

    def test_build_never_starts_at_the_limit_and_waits_first_in_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            command = (
                'source "$1"; n=0; sleep() { n=$((n + 1)); echo slept >&2; ls "$BUILDLOG_DIR/admission" | grep -c "waiting$" >&2; '
                'if (( n >= 2 )); then echo "MemAvailable: 67108864 kB" > "$BUILDLOG_MEMINFO"; fi; }; buildlog_wait_for_memory true; '
                'printf "%s|%s|" "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_RESERVATION"; '
                'if [[ -n "$BUILDLOG_MEM_RESERVATION" && -f "$BUILDLOG_MEM_RESERVATION" ]]; then echo exists; else echo missing; fi; '
                'path=$BUILDLOG_MEM_RESERVATION; buildlog_release_memory 0; if [[ -n "$path" && ! -e "$path" ]]; then echo gone; else echo remains; fi'
            )
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_WAIT_LIMIT_S": "0"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^Granted\|[^|\n]+\|exists\ngone\n$")
            self.assertEqual(result.stderr.count("slept\n1\n"), 2, result.stderr)
            self.assertEqual(result.stderr.count("this step is now first in line"), 1, result.stderr)
            self.assertNotIn("starting anyway", result.stderr)
            self.assertEqual(list((root / "log/admission").glob("*.waiting")), [])
            self.assertEqual(len(list((root / "log/admission").glob("*.reservation"))), 0)

    def test_fitting_at_limit_is_granted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 0 kB\n")
            command = ('source "$1"; sleep() { printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; SECONDS=1; }; '
                       'buildlog_wait_for_memory /bin/true; printf "%s\n" "$BUILDLOG_MEM_OUTCOME"; buildlog_release_memory 0')
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo), "BUILDLOG_MEM_WAIT_LIMIT_S": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "Granted\n")
            self.assertIn("memory free after", result.stderr)
            self.assertNotIn("memory wait limit reached", result.stderr)

    def test_python_failure_refuses_the_step_and_names_the_checker_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            broken = root / "broken.py"
            _ = broken.write_text('raise RuntimeError("broken lookup")\n')
            command = 'set -e; source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory /bin/true; printf "step started\\n"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(broken)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "BUILDLOG_MEMINFO": str(meminfo),
                                         "HOME": str(root), "BUILDLOG_DIR": str(root / "log")})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("memory admission check failed (RuntimeError: broken lookup); step refused", result.stderr)

    def test_missing_memory_reading_refuses_the_step_and_names_meminfo(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            command = ('set -e; source "$1"; buildlog_wait_for_memory /bin/true; '
                       'printf "step started\\n"')
            for contents in ("", "MemTotal: 67108864 kB\n"):
                with self.subTest(contents=contents):
                    _ = meminfo.write_text(contents)
                    result = subprocess.run(
                        ["bash", "-c", command, "test", str(MEMORY_GATE)],
                        capture_output=True, text=True, timeout=300, check=False,
                        env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                             "BUILDLOG_MEMINFO": str(meminfo)},
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(f"memory admission check failed (cannot read MemAvailable from {meminfo}); step refused", result.stderr)

    def test_untracked_admission_refuses_the_step_and_names_the_answer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meminfo = root / "meminfo"
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            wrapper = root / "untracked.py"
            _ = wrapper.write_text(f'import sys\nsys.path.insert(0, {str(MEMORY_GATE.parent)!r})\nimport memory_admit as gate\ngate.process_start_time = lambda pid: None\ngate.main()\n')
            command = 'set -e; source "$1"; BUILDLOG_MEM_ADMIT_SCRIPT=$2; buildlog_wait_for_memory /bin/true; printf "step started\\n"'
            result = subprocess.run(["bash", "-c", command, "test", str(MEMORY_GATE), str(wrapper)],
                                    capture_output=True, text=True, timeout=300, check=False,
                                    env={**os.environ,
                                         "BUILD_HOLD_DIR": str(root / "holders"),
                                         "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
                                         "UNIT_ACTIVE_DIR": str(root / "active"),
                                         "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                                         "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                                         "MAC_TEST_STATE_DIR": str(root / "mac-state"), "HOME": str(root), "BUILDLOG_DIR": str(root / "log"),
                                         "BUILDLOG_MEMINFO": str(meminfo)})
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("memory admission check failed (invalid admission answer admit:untracked); step refused", result.stderr)


class QueueLeaveTests(unittest.TestCase):
    """LINT_QUEUE_LEAVE_HOOK on run_once's memory wait, with a stand-in build log recorder."""

    root: Path = Path()
    meminfo: Path = Path()
    calls: Path = Path()
    ran: Path = Path()
    recorded: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.meminfo = self.root / "meminfo"
        self.calls = self.root / "hook-calls"
        self.ran = self.root / "ran"
        self.recorded = self.root / "recorded"
        scripts = self.root / ".claude/scripts"
        (scripts / "lib").mkdir(parents=True)
        (scripts / "buildlog").mkdir()
        py = scripts / "lib/py"
        _ = py.write_text('#!/bin/sh\nexec python3 "$@"\n')
        py.chmod(0o755)
        _ = (scripts / "buildlog/record.py").write_text(
            f"import sys\nwith open({str(self.recorded)!r}, 'a') as log:\n    log.write(sys.argv[1] + '\\n')\n")
        config = self.root / "config.sh"
        _ = config.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
        self.environment = {**os.environ,
                            "BUILD_HOLD_DIR": str(self.root / "holders"),
                            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
                            "UNIT_ACTIVE_DIR": str(self.root / "active"),
                            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
                            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
                            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"), "HOME": str(self.root),
                            "BUILDLOG_DIR": str(self.root / "log"), "BUILDLOG_MEMINFO": str(self.meminfo),
                            "BUILDLOG_SCOPE": "0", "BUILDLOG_SYNC": "1", "LINT_SKIP_SWEEP": "1",
                            "LINT_CONFIG_READER": str(config)}
        _ = self.environment.pop("LINT_QUEUE_LEAVE_HOOK", None)

    def run_step(self, hook: str | None, sleep: str, step: str = 'run_once touch "$2"') -> subprocess.CompletedProcess[str]:
        _ = self.meminfo.write_text("MemAvailable: 0 kB\n")
        environment = dict(self.environment)
        if hook is not None:
            environment["LINT_QUEUE_LEAVE_HOOK"] = f'echo poll >> "{self.calls}"; {hook}'
        command = (f'source "$1"; sleeps=0; sleep() {{ sleeps=$((sleeps + 1)); {sleep} }}; '
                   + f'status=0; {step} || status=$?; '
                   + 'printf "%s|%s|%s\\n" "$status" "$BUILDLOG_MEM_OUTCOME" "${BUILDLOG_START:-}"')
        return subprocess.run(["bash", "-c", command, "test", str(INVOKE), str(self.ran)],
                              capture_output=True, text=True, timeout=300, check=False, env=environment)

    def hook_calls(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def recorder_actions(self) -> list[str]:
        return self.recorded.read_text().splitlines() if self.recorded.exists() else []

    def reservations(self) -> list[Path]:
        return list((self.root / "log/admission").glob("*.reservation"))

    def test_hook_leaves_a_held_step_before_it_starts(self) -> None:
        result = self.run_step(":", ":;")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "75|LeftQueue|")
        self.assertFalse(self.ran.exists())
        self.assertEqual(self.hook_calls(), 1)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.recorder_actions(), ["key"])
        # The wait is measured in whole seconds of SECONDS, so a loaded machine
        # can cross a second between the first check and the leave.
        self.assertRegex(result.stderr, r"another machine is free after \d+ min \d+ s: leaving the memory wait\n")

    def test_step_that_can_start_never_asks_the_hook(self) -> None:
        free = 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO";'
        result = self.run_step("exit 0", free)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1].split("|")[:2], ["0", "Granted"])
        self.assertTrue(self.ran.exists())
        self.assertEqual(self.hook_calls(), 0)
        self.assertEqual(self.recorder_actions(), ["key", "step"])

    def test_declining_hook_and_no_hook_wait_alike(self) -> None:
        free_on_second = 'if (( sleeps == 2 )); then printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"; fi;'
        outputs: list[tuple[str, list[str]]] = []
        for hook in ("exit 1", None):
            with self.subTest(hook=hook):
                self.ran.unlink(missing_ok=True)
                result = self.run_step(hook, free_on_second)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.ran.exists())
                self.assertEqual(self.reservations(), [])
                lines = result.stderr.splitlines()
                outputs.append((result.stdout.splitlines()[-1].split("|")[1], [re.sub(r"\d+", "N", line) for line in lines]))
        self.assertEqual(self.hook_calls(), 1)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0][0], "Granted")

    def test_nextest_helper_returns_the_status_and_drops_its_width_claim(self) -> None:
        step = ('LINT_TEST_WIDTH=logical; cargo() { [[ "$*" == "nextest --version" ]] && return 0; touch "$2"; }; '
                + 'run_nextest --workspace')
        result = self.run_step("exit 0", ":;", step)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "75|LeftQueue|")
        self.assertFalse(self.ran.exists())
        # A run still waiting for memory claims no share, so it leaves no marker behind.
        self.assertEqual(list((self.root / "log/admission/nextest").iterdir()), [])
        self.assertEqual(self.reservations(), [])

    def nextest_step(self, ran_args: Path, reservation: Path) -> str:
        return ('LINT_TEST_WIDTH=logical; cargo() { [[ "$*" == "nextest --version" ]] && return 0; '
                + f'printf "%s\\n" "$*" > "{ran_args}"; cat "$BUILDLOG_MEM_RESERVATION" > "{reservation}"; }}; '
                + 'run_nextest --workspace')

    def test_a_run_waiting_for_memory_claims_its_share_only_once_admitted(self) -> None:
        listing, ran_args, reservation = self.root / "listing", self.root / "args", self.root / "reservation"
        free = (f'ls -A "$BUILDLOG_DIR/admission/nextest" >> "{listing}"; echo waited >> "{listing}"; '
                + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO";')
        result = self.run_step(None, free, self.nextest_step(ran_args, reservation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1].split("|")[:2], ["0", "Granted"])
        self.assertEqual(listing.read_text().splitlines(), ["waited"])
        width = os.sysconf("SC_NPROCESSORS_ONLN")
        self.assertEqual(ran_args.read_text().split()[:4], ["nextest", "run", "--test-threads", str(width)])
        self.assertEqual(json.loads(reservation.read_text())["test_threads"], width)
        self.assertEqual([path.name for path in (self.root / "log/admission/nextest").iterdir()], ["lock"])

    def test_an_admitted_run_shares_the_threads_with_a_run_in_flight(self) -> None:
        width = os.sysconf("SC_NPROCESSORS_ONLN")
        shared = budget.shared_width(width, 2)
        if shared == width:
            self.skipTest("this machine has too few cores to share")
        ran_args, reservation = self.root / "args", self.root / "reservation"
        markers = self.root / "log/admission/nextest"
        markers.mkdir(parents=True)
        free = 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO";'
        with (markers / "other.run").open("a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            result = self.run_step(None, free, self.nextest_step(ran_args, reservation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"nextest: {shared} of {width} test threads, shared with the runs in flight", result.stdout)
        self.assertEqual(ran_args.read_text().split()[:4], ["nextest", "run", "--test-threads", str(shared)])
        self.assertEqual(json.loads(reservation.read_text())["test_threads"], shared)
        self.assertEqual(sorted(path.name for path in markers.iterdir()), ["lock", "other.run"])


if __name__ == "__main__":
    _ = unittest.main()
