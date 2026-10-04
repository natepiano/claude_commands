#!/usr/bin/env python3
"""Tests for parse.py: what a step's argv names, and the facts its output shows.

The fixtures in testdata/ are real captured output (cargo clippy, doc, fmt,
mend, nextest --retries 2, lint sweep) from a two-crate scratch workspace;
the synthetic lines cover forms those runs never printed.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import parse
from test_index import use_test_log

use_test_log()

TESTDATA = Path(__file__).with_name("testdata")
GIB = 1 << 30
MIB = 1 << 20

NEXTEST_TIMEOUT = """\
    Starting 3 tests across 1 binary
        SLOW [> 60.000s] (───) crate tests::sleepy
     TIMEOUT [ 120.002s] (1/3) crate tests::sleepy
        PASS [   0.010s] (2/3) crate tests::quick
        PASS [  61.500s] (3/3) crate tests::long
     Summary [ 121.000s] 3 tests run: 2 passed, 1 timed out, 0 skipped
"""

SWEEP_REMOVED = """\
lint sweep: /r/target is 99.5 GiB, over the 96.0 GiB budget
lint sweep: removed 158 orphaned files (1.5 GiB), copied-up output whose build unit is gone; left 98.0 GiB
lint sweep: removed 3 build units and 1 incremental dirs (2.0 GiB), last used 2026-09-01 10:00 to 2026-09-20 11:00; left 96.0 GiB
lint sweep: /r/target/doc index is 300.0 MiB, over the 250.0 MiB budget; removed /r/target/doc (100.0 MiB), rebuilt by the next doc run
"""


def fixture(name: str) -> str:
    return (TESTDATA / f"{name}.txt").read_text()


def facts(step: str, name: str) -> parse.LogFacts:
    return parse.log_facts(step, fixture(name))


class ArgvTests(unittest.TestCase):
    def test_env_prefix_is_skipped(self) -> None:
        argv = [
            "env",
            "-u",
            "CARGO_MAKEFLAGS",
            "-u",
            "MAKEFLAGS",
            "-u",
            "MFLAGS",
            "RUSTDOCFLAGS=-D warnings",
            "cargo",
            "doc",
            "--no-deps",
            "--workspace",
        ]
        self.assertEqual(parse.step_name(argv), "doc")
        self.assertEqual(parse.step_name(["/usr/bin/env", "RUSTC_WRAPPER=", "cargo", "mend", "--fix"]), "mend")
        self.assertEqual(parse.step_name(["env", "FOO=1"]), "unknown")

    def test_toolchain_is_skipped(self) -> None:
        self.assertEqual(parse.step_name(["cargo", "+nightly", "fmt", "--all", "--check"]), "fmt")
        self.assertEqual(parse.toolchain(["cargo", "+nightly", "fmt", "--all", "--check"]), "+nightly")
        self.assertEqual(parse.toolchain(["env", "RUSTC_WRAPPER=", "cargo", "+stable", "mend"]), "+stable")
        self.assertIsNone(parse.toolchain(["cargo", "clippy", "--workspace"]))
        self.assertIsNone(parse.toolchain(["rustup", "+nightly"]))

    def test_sweep_and_other_programs(self) -> None:
        sweep = ["/home/u/.claude/scripts/lib/py", "/home/u/.claude/scripts/lint/sweep.py", "--dry-run"]
        self.assertEqual(parse.step_name(sweep), "sweep")
        self.assertEqual(parse.step_name(["cargo", "nextest", "run", "--workspace"]), "nextest")
        self.assertEqual(parse.step_name(["/nix/store/x/bin/cargo", "check"]), "check")
        self.assertEqual(parse.step_name(["cargo"]), "cargo")
        self.assertEqual(parse.step_name(["/usr/bin/taplo", "fmt"]), "taplo")
        self.assertEqual(parse.step_name([]), "unknown")

    def test_manifest_path(self) -> None:
        self.assertEqual(parse.manifest_path(["cargo", "clippy", "--manifest-path", "a/Cargo.toml"]), "a/Cargo.toml")
        self.assertEqual(parse.manifest_path(["cargo", "doc", "--manifest-path=b/Cargo.toml"]), "b/Cargo.toml")
        self.assertIsNone(parse.manifest_path(["cargo", "clippy", "--manifest-path"]))
        self.assertIsNone(parse.manifest_path(["cargo", "clippy"]))


class CleaningTests(unittest.TestCase):
    def test_carriage_return_keeps_the_last_redraw(self) -> None:
        self.assertEqual(parse.clean_lines("one\rtwo\r\nthree\r\n"), ["two", "three", ""])

    def test_ansi_and_redrawn_progress_bar(self) -> None:
        text = (
            "\x1b[1m\x1b[32m   Compiling\x1b[0m alpha v0.1.0 (/r/alpha)\n"
            + "\x1b[1m\x1b[36m    Building\x1b[0m [=====>   ] 3/5: beta\r\x1b[K"
            + "\x1b[1m\x1b[32m    Finished\x1b[0m `dev` profile [unoptimized] target(s) in 1m 05s\n"
            + "\x1b]8;;https://example.com\x07link\x1b]8;;\x07\n"
        )
        lines = parse.clean_lines(text)
        self.assertEqual(lines[0], "   Compiling alpha v0.1.0 (/r/alpha)")
        self.assertEqual(lines[2], "link")
        result = parse.log_facts("check", text)
        self.assertEqual(result["finished_s"], 65.0)
        self.assertEqual(result["crates_compiled"], 1)


class FixtureTests(unittest.TestCase):
    def test_clippy_error_before_finished(self) -> None:
        result = facts("clippy", "clippy")
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["warnings"], 0)
        self.assertEqual(result["crates_compiled"], 2)
        self.assertIsNone(result["finished_s"])
        self.assertIsNone(result["tests"])
        self.assertIsNone(result["mend_fixes"])
        self.assertIsNone(result["sweep_freed_bytes"])

    def test_doc(self) -> None:
        result = facts("doc", "doc")
        self.assertEqual(result["finished_s"], 0.39)
        self.assertEqual(result["crates_compiled"], 3)
        self.assertEqual(result["errors"], 0)

    def test_fmt_prints_nothing_to_count(self) -> None:
        result = facts("fmt", "fmt")
        self.assertIsNone(result["finished_s"])
        self.assertEqual(result["crates_compiled"], 0)
        self.assertEqual(result["warnings"], 0)
        self.assertIsNone(result["tests_run"])

    def test_nextest_counts_and_rows(self) -> None:
        result = facts("nextest", "nextest")
        self.assertEqual(result["finished_s"], 3.16)
        self.assertEqual(result["tests_run"], 5)
        self.assertEqual(result["tests_passed"], 4)
        self.assertEqual(result["tests_failed"], 1)
        self.assertEqual(result["tests_skipped"], 0)
        self.assertEqual(result["tests_flaky"], 1)
        self.assertEqual(result["tests_retried"], 2)
        # The warning cargo repeats for the lib test target counts once.
        self.assertEqual(result["warnings"], 1)
        rows = {row["test"]: row for row in result["tests"] or []}
        self.assertEqual(set(rows), {"tests::fails", "tests::flaky", "tests::slow"})
        self.assertEqual((rows["tests::fails"]["status"], rows["tests::fails"]["attempts"]), ("failed", 3))
        self.assertEqual((rows["tests::flaky"]["status"], rows["tests::flaky"]["attempts"]), ("flaky", 2))
        self.assertEqual(rows["tests::slow"]["status"], "passed")
        self.assertEqual(rows["tests::slow"]["duration_s"], 1.505)
        self.assertEqual(rows["tests::slow"]["binary"], "alpha")

    def test_nextest_lines_after_summary_are_ignored(self) -> None:
        late = "  TRY 3 FAIL [   9.000s] (───) alpha tests::late\n        PASS [   5.000s] (6/6) beta tests::after\n"
        result = parse.log_facts("nextest", fixture("nextest") + late)
        tests = {row["test"] for row in result["tests"] or []}
        self.assertEqual(tests, {"tests::fails", "tests::flaky", "tests::slow"})
        self.assertEqual(result["tests_run"], 5)

    def test_mend_times_and_fixes(self) -> None:
        result = facts("mend", "mend")
        self.assertEqual(result["mend_check_s"], 0.13)
        self.assertEqual(result["mend_s"], 0.0)
        self.assertEqual(result["finished_s"], 0.13)
        self.assertEqual(result["warnings"], 2)
        self.assertIsNone(result["mend_fixes"])
        self.assertEqual(facts("mend", "mendfix")["mend_fixes"], 1)
        self.assertEqual(facts("mend", "mendfix2")["mend_fixes"], 0)
        self.assertIsNone(facts("mend", "mendfail")["mend_fixes"])
        # The same warning printed three times is one diagnostic.
        self.assertEqual(facts("mend", "mendfix")["warnings"], 1)

    def test_mend_minutes(self) -> None:
        result = parse.log_facts("mend", "    Finished in 1m 05.5s (check: 1m 02.0s, mend: 3.5s)\n")
        self.assertEqual(result["mend_check_s"], 62.0)
        self.assertEqual(result["mend_s"], 3.5)
        self.assertEqual(result["finished_s"], 62.0)


class SweepTests(unittest.TestCase):
    def test_within_budget_dry_run_frees_nothing(self) -> None:
        self.assertEqual(facts("sweep", "sweep")["sweep_freed_bytes"], 0)

    def test_removed_orphans_units_and_doc(self) -> None:
        result = parse.log_facts("sweep", SWEEP_REMOVED)
        self.assertEqual(result["sweep_freed_bytes"], 3 * GIB + GIB // 2 + 100 * MIB)

    def test_would_remove_frees_nothing(self) -> None:
        result = parse.log_facts("sweep", SWEEP_REMOVED.replace("removed", "would remove"))
        self.assertEqual(result["sweep_freed_bytes"], 0)

    def test_only_a_sweep_step_counts(self) -> None:
        self.assertIsNone(parse.log_facts("clippy", SWEEP_REMOVED)["sweep_freed_bytes"])


class NextestFormTests(unittest.TestCase):
    def test_slow_and_timeout(self) -> None:
        result = parse.log_facts("nextest", NEXTEST_TIMEOUT)
        self.assertEqual(result["tests_run"], 3)
        self.assertEqual(result["tests_passed"], 2)
        self.assertEqual(result["tests_failed"], 1)
        rows = {row["test"]: row for row in result["tests"] or []}
        self.assertEqual(set(rows), {"tests::sleepy", "tests::long"})
        sleepy = rows["tests::sleepy"]
        self.assertEqual((sleepy["status"], sleepy["slow"], sleepy["attempts"]), ("timed_out", True, 1))
        self.assertEqual(sleepy["duration_s"], 120.002)
        self.assertEqual((rows["tests::long"]["status"], rows["tests::long"]["slow"]), ("passed", False))

    def test_partial_run_summary(self) -> None:
        text = "     Summary [   0.500s] 2/5 tests run: 1 passed, 1 failed, 3 skipped\n"
        result = parse.log_facts("nextest", text)
        self.assertEqual(
            (result["tests_run"], result["tests_passed"], result["tests_failed"], result["tests_skipped"]),
            (2, 1, 1, 3),
        )

    def test_singular_and_exec_failed(self) -> None:
        text = "     Summary [   0.100s] 1 test run: 0 passed, 1 exec failed, 0 skipped\n"
        result = parse.log_facts("nextest", text)
        self.assertEqual((result["tests_run"], result["tests_failed"]), (1, 1))

    def test_nextest_without_summary_still_lists_rows(self) -> None:
        result = parse.log_facts("nextest", "  TRY 1 FAIL [   0.003s] (───) alpha tests::fails\n")
        self.assertIsNone(result["tests_run"])
        self.assertEqual([row["test"] for row in result["tests"] or []], ["tests::fails"])


if __name__ == "__main__":
    _ = unittest.main()
