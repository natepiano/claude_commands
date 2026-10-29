"""Check the replay against small stores and arithmetic independent of its code."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from typing import override

from scripts.usage import collect
from scripts.usage.test_collect import CollectorFixture, REPOSITORY, SNAPSHOT_COMMIT_SCRIPT


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

class CompactionReplayTests(CollectorFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        with closing(sqlite3.connect(self.db_path)) as db:
            _ = db.executescript(collect.SCHEMA)
            collect.migrate_store(db)

    def request(self, thread: str, minute: int, size: int, cached: int = 0, *,
                key: str = "rollout-hand.jsonl:100", model: str = "gpt-5.6-sol",
                day: int = 10, output: int = 10) -> None:
        at = f"2026-10-{day:02d}T22:{minute:02d}:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("natedev", "codex", "worker", "", thread, key, at, model, "medium",
                            size - cached, 0, 0, cached, output, 1, "impl", None,
                            "recorded", "not_applicable", "work", None))

    def compaction(self, thread: str, minute: int, *, day: int = 10) -> None:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("INSERT INTO compactions VALUES (?,?,?,?,?)",
                           ("natedev", "codex", "", thread, f"2026-10-{day:02d}T22:{minute:02d}:00+00:00"))

    def run_replay(self, *arguments: str, script: str = "") -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(HOME=str(self.root), USAGE_STATE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        command = ([sys.executable, "-c", script] if script else
                   [sys.executable, "-m", "scripts.usage.compaction_replay"])
        return subprocess.run([*command, "--db", str(self.db_path), *arguments],
                              cwd=REPOSITORY, env=environment, capture_output=True,
                              text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def replay(self, *arguments: str, script: str = "") -> subprocess.CompletedProcess[str]:
        result = self.run_replay(*arguments, script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result

    def assert_replay_refused(self, *arguments: str, exit_status: int, reason: str) -> None:
        result = self.run_replay(*arguments)
        self.assertEqual(result.returncode, exit_status, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, f"compaction_replay: {reason}\n")
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def assert_store_preserved_on_refusal(self, *arguments: str, exit_status: int, reason: str) -> None:
        before = self.db_path.read_bytes()
        self.assert_replay_refused(*arguments, exit_status=exit_status, reason=reason)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_equal_bounds_are_refused_without_changing_the_store(self) -> None:
        self.assert_store_preserved_on_refusal(
            "--since", "2026-10-11", "--until", "2026-10-11", exit_status=2,
            reason="invalid bounds; accepted: --since earlier than --until (inclusive start, exclusive end)")

    def test_reversed_bounds_are_refused_without_changing_the_store(self) -> None:
        self.assert_store_preserved_on_refusal(
            "--since", "2026-10-12", "--until", "2026-10-11", exit_status=2,
            reason="invalid bounds; accepted: --since earlier than --until (inclusive start, exclusive end)")

    def test_negative_follow_up_prompt_is_refused_without_changing_the_store(self) -> None:
        self.assert_store_preserved_on_refusal(
            "--follow-up-prompt", "-1", exit_status=2,
            reason="follow-up prompt must be a finite nonnegative token count")

    def test_nan_follow_up_prompt_is_refused_without_changing_the_store(self) -> None:
        self.assert_store_preserved_on_refusal(
            "--follow-up-prompt", "nan", exit_status=2,
            reason="follow-up prompt must be a finite nonnegative token count")

    def test_infinite_follow_up_prompt_is_refused_without_changing_the_store(self) -> None:
        self.assert_store_preserved_on_refusal(
            "--follow-up-prompt", "inf", exit_status=2,
            reason="follow-up prompt must be a finite nonnegative token count")

    def test_older_store_is_refused_without_changing_its_bytes(self) -> None:
        version = collect.STORE_VERSION - 1
        with closing(sqlite3.connect(self.db_path)) as db:
            _ = db.execute(f"PRAGMA user_version = {version}")
        self.assert_store_preserved_on_refusal(
            exit_status=1,
            reason=f"store version {version} is older than report version {collect.STORE_VERSION}; "
            + "run the collector once")

    def test_non_sqlite_file_is_refused_without_changing_its_bytes(self) -> None:
        _ = self.db_path.write_bytes(b"invented file, not a SQLite store\n")
        self.assert_store_preserved_on_refusal(exit_status=1, reason="file is not a database")

    def test_missing_store_is_refused_without_creating_a_file(self) -> None:
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())
        self.assert_replay_refused(exit_status=1, reason="unable to open database file")
        self.assertFalse(self.db_path.exists())

    def hand_thread(self, thread: str = "hand", *, day: int = 10) -> None:
        # At one timestamp, :100 precedes :20 as TEXT. Insert them in reverse order.
        self.request(thread, 0, 60000, 50000, key="rollout-hand.jsonl:20", day=day)
        self.request(thread, 0, 20000, key="rollout-hand.jsonl:100", day=day)
        self.compaction(thread, 1, day=day)
        self.request(thread, 2, 0, key="rollout-hand.jsonl:200", day=day, output=999)
        self.request(thread, 3, 80000, key="rollout-hand.jsonl:300", day=day)
        self.request(thread, 4, 200000, 1000, key="rollout-hand.jsonl:400", day=day)
        self.compaction(thread, 5, day=day)
        self.request(thread, 6, 150000, key="rollout-hand.jsonl:500", day=day)
        self.request(thread, 7, 300000, 250000, key="rollout-hand.jsonl:600", day=day)
        self.compaction(thread, 8, day=day)
        self.request(thread, 9, 200000, key="rollout-hand.jsonl:700", day=day)
        self.request(thread, 10, 280000, 260000, key="rollout-hand.jsonl:800", day=day)

    def table(self, output: str) -> dict[str, list[str]]:
        rows = [line for line in output.splitlines() if line.startswith("| ")]
        return {cells[0]: cells[1:] for line in rows[2:]
                if (cells := [cell.strip() for cell in line.strip("|").split("|")])}

    def assert_rule(self, rows: dict[str, list[str]], rule: str, compactions: int,
                    workers: int, tokens: float, over: int) -> None:
        self.assertIn(rule, rows)
        cells = rows[rule]
        self.assertEqual(len(cells), 5)
        self.assertEqual(int(cells[0].replace(",", "")), compactions)
        self.assertEqual(int(cells[1].replace(",", "")), workers)
        self.assertAlmostEqual(float(cells[2].replace(",", "")), tokens, delta=0.5)
        self.assertEqual(int(cells[4].replace(",", "")), over)

    def test_rules_match_hand_arithmetic_and_count_cache_floor_and_new_workers(self) -> None:
        self.hand_thread()
        output = self.replay().stdout
        rows = self.table(output)
        self.assertTrue(output.startswith("Covered: 1 threads, 3 compactions, 8 requests.\n"))
        self.assertIn("Fit: kept = 50000.000000 + 0.500000 × before.", output)
        self.assertIn("Median first-request size: 20,000 tokens.", output)
        self.assertIn("Replay excludes compaction-request cost and does not simulate Codex compaction of a full context.", output)
        # Pairs (60000,80000), (200000,150000), (300000,200000): a=50000, b=.5.
        # Initial segment costs 20080+15080=35160. All rules carry 60000+11000
        # into segment 2: 17080 + (199180-100) = 216160, subtotal 251320.
        # d=-9000 exceeds that later request's cache of 1000: exactly one floor.
        # Limit 100k: starts 145500,197750; later deltas -4500,-2250.
        baseline = 251320 + 145580 + 74630 + 197830 + 45855
        # Limit 200k: carry to 202000 (d=52000), then compact 352000 to 226000.
        higher = 251320 + 30180 + 80280 + 226080 + 48680
        # Never: same segment 3, then carry 352000+11000, d=163000.
        never = 251320 + 30180 + 80280 + 46280 + 62380
        # Estimate: both later boundaries choose median first size 20000.
        estimate = 251320 + 20080 + 62080 + 20080 + 28080
        self.assert_rule(rows, "limit 100,000", 2, 0, baseline, 2)
        self.assert_rule(rows, "limit 200,000", 1, 0, higher, 2)
        self.assert_rule(rows, "never", 0, 0, never, 3)
        self.assert_rule(rows, "new worker when kept is over 90,000 (estimate)", 0, 2, estimate, 0)
        self.assertEqual(list(rows), ["limit 100,000", "limit 150,000", "limit 200,000",
                                     "limit 250,000", "never", "new worker when kept is over 90,000 (estimate)"])
        self.assertAlmostEqual(float(rows["limit 200,000"][3].rstrip("%").replace(",", "")),
                               (higher / baseline - 1) * 100, delta=0.05)
        self.assertAlmostEqual(float(rows["never"][3].rstrip("%").replace(",", "")),
                               (never / baseline - 1) * 100, delta=0.05)
        self.assertIn("785,740", output)  # Stored c_i; the zero-size row's output is excluded.
        self.assertIn("715,215", output)
        self.assertIn("Threads left out: no compaction=0; request with no weight entry=0.", output)
        self.assertIn("Rows of size 0 skipped: 1.", output)
        for rule in rows:
            self.assertIn(f"- {rule}: 1.", output)

    def test_replay_is_read_only_and_uses_one_snapshot_during_a_commit(self) -> None:
        self.hand_thread()
        before = self.db_path.read_bytes()
        expected = self.replay().stdout
        self.assertEqual(self.db_path.read_bytes(), before)
        script = SNAPSHOT_COMMIT_SCRIPT.replace("startswith('SELECT')", "startswith(('SELECT', 'WITH'))") + """
from scripts.usage import compaction_replay
with patch.object(sqlite3, 'connect', snapshot_connect):
    result = compaction_replay.main(sys.argv[1:])
assert changed
raise SystemExit(result)
"""
        self.assertEqual(self.replay(script=script).stdout, expected)
        self.assertEqual(self.rows("SELECT count(*) FROM turns")[0][0], 0)

    def test_bounds_select_complete_threads_by_their_first_request(self) -> None:
        self.hand_thread("earlier", day=10)
        self.hand_thread("later", day=11)
        expected = self.replay("--since", "2026-10-11", "--until", "2026-10-12").stdout
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("DELETE FROM turns WHERE agent_id = 'earlier'")
            _ = db.execute("DELETE FROM compactions WHERE agent_id = 'earlier'")
        self.assertEqual(self.replay().stdout, expected)
        unavailable = self.replay("--since", "2026-10-11T22:00:01Z").stdout
        self.assertIn("fit unavailable:", unavailable)
        self.assertNotIn("| limit", unavailable)

    def test_fit_without_neighboring_segments_has_a_reason_and_no_rule_table(self) -> None:
        self.request("one-side", 0, 10000)
        self.compaction("one-side", 1)
        output = self.replay().stdout
        self.assertIn("fit unavailable: no neighboring segments", output)
        self.assertNotIn("| limit", output)
        self.assertNotIn("| never", output)
        self.assertIn("Requests with simulated cache read held at 0: none.", output)

    def test_fit_with_equal_before_sizes_has_a_reason_and_no_rule_table(self) -> None:
        for thread, kept in (("first", 40000), ("second", 80000)):
            self.request(thread, 0, 100000)
            self.compaction(thread, 1)
            self.request(thread, 2, kept, key="rollout-hand.jsonl:200")
        output = self.replay().stdout
        self.assertIn("fit unavailable: all before sizes are equal", output)
        self.assertNotIn("| limit", output)

    def test_missing_weights_exclude_a_whole_thread_and_model_changes_are_covered(self) -> None:
        self.hand_thread()
        self.request("no-compaction", 0, 20000)
        self.request("missing-weight", 0, 60000)
        self.compaction("missing-weight", 1)
        self.request("missing-weight", 2, 80000, key="rollout-hand.jsonl:200", model="invented-no-weight")
        self.hand_thread("model-switch")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("UPDATE turns SET model = 'gpt-6.1-sol' WHERE agent_id = 'model-switch' AND at > ?",
                           ("2026-10-10T22:05:00+00:00",))
        output = self.replay().stdout
        self.assertIn("Covered: 2 threads, 6 compactions, 16 requests.", output)
        self.assertIn("Threads left out: no compaction=1; request with no weight entry=1.", output)
        self.assertIn("Rows of size 0 skipped: 2.", output)
        # Model-switch has 5x output and .05 cached input in its last two segments.
        # Its baseline differs by -30*4 -12500 -13000 +225 +112.5.
        switched = 715215 - 120 - 12500 - 13000 + 225 + 112.5
        self.assert_rule(self.table(output), "limit 100,000", 4, 0, 715215 + switched, 4)

    def test_follow_up_prompt_flag_changes_carried_context_cost(self) -> None:
        self.hand_thread()
        output = self.replay("--follow-up-prompt", "0").stdout
        # Never: segment starts 60000,180000,330000; differences -20000,30000,130000.
        total = 35160 + 6080 + 199080 + 18080 + 78080 + 33080 + 59080
        self.assert_rule(self.table(output), "never", 0, 0, total, 3)

    def test_zero_size_rows_are_counted_only_for_covered_threads(self) -> None:
        self.hand_thread()
        self.request("no-compaction", 0, 20000)
        self.request("no-compaction", 1, 0, key="rollout-hand.jsonl:200")
        self.request("missing-weight", 0, 60000)
        self.compaction("missing-weight", 1)
        self.request("missing-weight", 2, 80000, key="rollout-hand.jsonl:200", model="invented-no-weight")
        self.request("missing-weight", 3, 0, key="rollout-hand.jsonl:300")
        self.request("only-zero", 0, 0)
        self.compaction("only-zero", 1)
        output = self.replay().stdout
        self.assertIn("Covered: 1 threads, 3 compactions, 8 requests.\n", output)
        self.assertIn("Threads left out: no compaction=1; request with no weight entry=1.\n", output)
        self.assertIn("Rows of size 0 skipped: 1.\n", output)

    def test_compactions_without_positive_requests_create_no_replay_segments(self) -> None:
        self.hand_thread()
        before = self.table(self.replay().stdout)
        self.compaction("hand", 2)  # Only a zero-size row since the preceding boundary.
        self.compaction("hand", 11)  # No request after the last boundary.
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("UPDATE turns SET model = 'invented-no-weight' WHERE input + cache_read = 0")
        output = self.replay().stdout
        self.assertEqual(self.table(output), before)
        self.assertIn("Covered: 1 threads, 5 compactions, 8 requests.", output)
        self.assertIn("Rows of size 0 skipped: 1.", output)


if __name__ == "__main__":
    _ = unittest.main()
