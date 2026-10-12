"""Fixture tests for the long-lived-seat usage report."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast, final, override

from scripts.whoami import seat_usage
from scripts.usage.test_outcomes import OutcomeFixture
from scripts.usage.test_collect import FIXTURES as COLLECT_FIXTURES, SNAPSHOT_COMMIT_SCRIPT, SshBehavior


def record(record_type: str, at: str, payload: dict[str, object]) -> str:
    return json.dumps({"timestamp": at, "type": record_type, "payload": payload})


def tokens(at: str, input_tokens: int, cached: int, output: int) -> str:
    return record("event_msg", at, {
        "type": "token_count",
        "info": {"last_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
        }},
    })


class SeatUsageTests(unittest.TestCase):
    def test_missing_or_single_switch_observation_is_unavailable(self) -> None:
        window = seat_usage.window_from(("2026-10-01", "2026-10-03"))
        only = seat_usage.AccountReading(
            "codex 1", datetime(2026, 10, 2, tzinfo=timezone.utc), seat_usage.UnknownCreditBalance(),
        )
        self.assertEqual(seat_usage.switches_per_week((), window), seat_usage.SwitchHistoryUnavailable())
        self.assertEqual(seat_usage.switches_per_week((only,), window), seat_usage.SwitchHistoryUnavailable())

    def test_unknown_and_invalid_credit_balances_have_no_credit_history(self) -> None:
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        readings = (
            seat_usage.AccountReading("codex 1", start, seat_usage.credit_balance("unlimited")),
            seat_usage.AccountReading("codex 1", start + timedelta(days=1), seat_usage.credit_balance("nan")),
            seat_usage.AccountReading("codex 1", start + timedelta(days=2), seat_usage.credit_balance(-1)),
        )
        window = seat_usage.Window(start, start + timedelta(days=3))
        self.assertEqual(seat_usage.credits_per_day(readings, window), seat_usage.NoCreditHistory())

    def test_one_credit_sample_is_insufficient_instead_of_absent_history(self) -> None:
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        reading = seat_usage.AccountReading("codex 1", start, seat_usage.KnownCreditBalance(100.0))
        window = seat_usage.Window(start, start + timedelta(days=1))
        self.assertEqual(seat_usage.credits_per_day((reading,), window), seat_usage.InsufficientCreditHistory())
        self.assertEqual(seat_usage.format_credits(seat_usage.InsufficientCreditHistory()),
                         "insufficient credit history")

    def test_daily_windows_use_announced_default_and_allow_override(self) -> None:
        args = seat_usage.parse_arguments(["--daily", "--t-live", "2026-10-10T12:00:00-04:00"])
        now = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
        windows = seat_usage.requested_windows(args, now)
        self.assertEqual(windows.before.start, datetime(2026, 10, 3, 16, tzinfo=timezone.utc))
        self.assertEqual(windows.after.end, now)

        default = seat_usage.requested_windows(seat_usage.parse_arguments(["--daily"]), now)
        self.assertEqual(default.before.start, datetime(2026, 10, 3, 18, 17, tzinfo=timezone.utc))
        self.assertEqual(default.before.end, seat_usage.DEFAULT_T_LIVE)
        self.assertEqual(default.after.start, seat_usage.DEFAULT_T_LIVE)


DAY1_FIXTURES = Path(__file__).resolve().parents[1] / "usage/fixtures/day1_seats"
READOUT = Path(__file__).with_name("seat_usage.py")
BEFORE = ("2026-10-03T18:17Z", "2026-10-10T18:17Z")
AFTER = ("2026-10-10T18:17Z", "2026-10-11T15:38Z")


class ReadoutFunctionCalls(TypedDict):
    calls: dict[str, int]
    weighting_registered: bool
    sources_called: int


@final
class StoreReadoutTests(OutcomeFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.maxDiff = None
        for directory in (self.claude_root, self.codex_root, self.history):
            shutil.rmtree(directory)
            directory.mkdir(parents=True)
        (self.claude_root / "project").mkdir()
        _ = shutil.copyfile(DAY1_FIXTURES / "claude.jsonl", self.claude_root / "project/ignored.jsonl")
        codex_day = self.codex_root / "2026/10/10"
        codex_day.mkdir(parents=True)
        for name in ("local-shared", "opening-only", "edit-only", "compaction-only", "unweighted"):
            _ = shutil.copyfile(DAY1_FIXTURES / f"{name}.jsonl", codex_day / f"rollout-{name}.jsonl")
        _ = self.agents_path.write_bytes(b"")
        for name in ("readings", "baseline-readings"):
            _ = shutil.copyfile(DAY1_FIXTURES / f"{name}.jsonl", self.root / f"{name}.jsonl")
        _ = (self.root / "account-roles.jsonl").write_bytes(b"")
        for index, at in enumerate(("2026-10-04T12:00:00Z", "2026-10-05T12:00:00Z",
                                    "2026-10-10T19:40:00Z", "2026-10-10T22:40:00Z", "2026-10-11T00:00:00Z")):
            _ = self.commit(f"checkpoint(seats): phase {index} — Invented", at)
        mac_home = self.root / "fixture-mac-home"
        for suffix in (".codex/sessions/2026/10/10", ".claude/projects", ".local/state/plan-delegate/runs",
                       ".local/state/usage-model"):
            (mac_home / suffix).mkdir(parents=True)
        _ = shutil.copyfile(DAY1_FIXTURES / "mac-shared.jsonl", mac_home / ".codex/sessions/2026/10/10/rollout-shared.jsonl")
        _ = (mac_home / ".local/state/usage-model/agents.jsonl").write_bytes(b"")
        (self.root / "bin").mkdir()
        _ = shutil.copyfile(COLLECT_FIXTURES / "mac/fake_ssh.py", self.root / "bin/ssh")
        (self.root / "bin/ssh").chmod(0o700)
        (self.root / "outside-checkout").mkdir()
        _ = self.collect()

    def mac_pass(self, behavior: SshBehavior = "complete") -> None:
        report = self.root / "ssh-report.json"
        self.assertFalse(report.exists())
        configuration = {"home": str(self.root / "fixture-mac-home"),
                         "execution_directory": str(self.root / "outside-checkout"),
                         "report": str(report), "behavior": behavior, "database": str(self.db_path),
                         "previous_role_read_at": ""}
        _ = (self.root / "ssh-configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        _ = self.collect(mac_pass="fixture")
        self.assertTrue(report.is_file())
        _ = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
        report.unlink()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        _ = environment.pop("PYTHONPATH", None)
        environment.update(HOME=str(self.root), USAGE_STATE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1",
                           PATH=f"{self.root / 'bin'}:{environment.get('PATH', '')}")
        return environment

    def readout(self, *, before: tuple[str, str] = BEFORE, explicit_db: bool = True, after: tuple[str, str] = AFTER, script: str = "") -> subprocess.CompletedProcess[str]:
        command = ([sys.executable, "-c", script] if script else [sys.executable, str(READOUT)])
        database = ["--db", str(self.db_path)] if explicit_db else []
        return subprocess.run([*command, *database, "--before", *before, "--after", *after],
                              env=self.environment(), cwd=self.root / "outside-checkout", capture_output=True,
                              text=True, check=False, timeout=15)

    def expected(self, name: str = "expected.md") -> str:
        expected = (DAY1_FIXTURES / name).read_text(encoding="utf-8")
        for row in self.rows("SELECT machine, family, last_complete_at FROM turn_sources"):
            stamp = cast(object, row["last_complete_at"])
            if isinstance(stamp, str):
                if row["machine"] == "mac" and row["family"] == "codex":
                    expected = expected.replace("{{mac.codex.note}}", datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%MZ"))
                expected = expected.replace("{{" + cast(str, row["machine"]) + "." + cast(str, row["family"]) + ".complete}}", stamp)
        for row in self.rows("SELECT machine, read_at FROM role_log_sources"):
            expected = expected.replace("{{" + cast(str, row["machine"]) + ".roles.read}}", cast(str, row["read_at"]))
        self.assertNotIn("{{", expected)
        return expected

    def assert_readout(self, name: str = "expected.md", *, after: tuple[str, str] = AFTER, script: str = "") -> str:
        result = self.readout(after=after, script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected(name))
        return result.stdout

    def test_readout_without_db_uses_the_collectors_store_path_at_run_time(self) -> None:
        self.mac_pass()
        result = self.readout(explicit_db=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.expected())
        script = f"""
import sys
sys.path.insert(0, {str(READOUT.resolve().parents[2])!r})
from pathlib import Path
from unittest.mock import patch
from scripts.usage import collect
from scripts.whoami import seat_usage
args = seat_usage.parse_arguments(sys.argv[1:])
with patch.object(collect, 'store_path', return_value=Path({str(self.db_path)!r})) as path:
    with patch.object(seat_usage, 'parse_arguments', return_value=args):
        result = seat_usage.run(sys.argv[1:])
    assert path.call_count == 1, path.call_count
raise SystemExit(result)
"""
        result = self.readout(explicit_db=False, script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.expected())

    def test_readout_figures_freshness_and_sources_share_one_snapshot_during_a_writer_commit(self) -> None:
        self.mac_pass()
        expected = self.expected()
        script = f"import sys\nsys.path.insert(0, {str(READOUT.resolve().parents[2])!r})\n" + SNAPSHOT_COMMIT_SCRIPT + """
from scripts.whoami import seat_usage
with patch.object(sqlite3, 'connect', snapshot_connect):
    result = seat_usage.run(sys.argv[1:])
assert changed
raise SystemExit(result)
"""
        result = self.readout(script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)
        self.assertEqual(self.rows("SELECT count(*) AS turns FROM turns WHERE family = 'codex'")[0]["turns"], 0)
        self.assertEqual(self.rows("SELECT state FROM turn_sources WHERE machine = 'mac' AND family = 'codex'")[0]["state"],
                         "unreachable")

    def test_store_readers_keep_working_in_the_previous_journal_mode(self) -> None:
        self.mac_pass()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        _ = self.assert_readout()

    def test_usage_before_an_edit_at_the_same_timestamp_keeps_its_stored_order(self) -> None:
        path = self.codex_root / "2026/10/10/rollout-same-time.jsonl"
        _ = shutil.copyfile(DAY1_FIXTURES / "same-time-edit.jsonl", path)
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT before_first_edit FROM turns WHERE agent_id = 'same-time' ORDER BY at"),
                                     "before_first_edit"), [(1,), (0,)])
        result = self.readout(before=("2026-10-06", "2026-10-07"), after=("2026-10-08", "2026-10-09"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected("expected-same-time-edit.md"))

    def test_day_one_store_readout_matches_hand_computed_dated_weights_and_account_rates(self) -> None:
        self.mac_pass()
        _ = self.assert_readout()
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE family = 'codex'")), 7)
        self.assertEqual(self.values(self.rows("SELECT machine, agent_id FROM openings WHERE agent_id = 'shared' " +
                                               "ORDER BY machine"), "machine", "agent_id"),
                         [("mac", "shared"), ("natedev", "shared")])

    def test_opening_without_a_turn_counts_as_active_beside_a_compacting_thread(self) -> None:
        path = self.codex_root / "2026/10/10/rollout-local-shared.jsonl"
        self.append_bytes(path, (record("event_msg", "2026-10-10T19:20:00Z",
                                        {"type": "contextCompaction"}) + "\n").encode())
        self.mac_pass()
        _ = self.assert_readout("expected-opening-active.md")

    def test_two_weight_cuts_deduplicate_simultaneous_starts_and_last_piece_without_checkpoint_is_unavailable(self) -> None:
        _ = shutil.copyfile(DAY1_FIXTURES / "extra-cuts.jsonl", self.codex_root / "2026/10/10/rollout-extra-cuts.jsonl")
        self.mac_pass()
        script = f"""
import sys
sys.path.insert(0, {str(READOUT.resolve().parents[2])!r})
from datetime import datetime, timezone
from scripts.usage import weights
from scripts.whoami import seat_usage
start = datetime(2026, 10, 10, 20, tzinfo=timezone.utc)
weights.MODEL_WEIGHTS = (*weights.MODEL_WEIGHTS,
    weights.ModelWeights('gpt-5.6-sol', start, 0.2, 0, 0, 9),
    weights.ModelWeights('gpt-6.1-sol', start, 0.07, 0, 0, 6))
raise SystemExit(seat_usage.run(sys.argv[1:]))
"""
        _ = self.assert_readout("expected-two-cuts.md", after=(AFTER[0], "2026-10-10T21:36Z"), script=script)

    def test_readout_uses_only_the_store_from_another_directory_without_pythonpath(self) -> None:
        self.mac_pass()
        before = self.db_path.read_bytes()
        for directory in (self.claude_root, self.codex_root, self.history, self.root / "fixture-mac-home",
                          self.repository, self.production_docs):
            shutil.rmtree(directory)
        for path in (self.agents_path, self.root / "readings.jsonl", self.root / "baseline-readings.jsonl"):
            path.unlink()
        for binary in ("ssh", "git"):
            _ = (self.root / "bin" / binary).write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            (self.root / "bin" / binary).chmod(0o700)
        _ = self.assert_readout()
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_nonready_mac_keeps_stored_usage_and_the_last_complete_time_then_recovers_once(self) -> None:
        self.mac_pass()
        query = "SELECT family, last_complete_at FROM turn_sources WHERE machine = 'mac' ORDER BY family"
        complete = self.all_values(self.rows(query))
        turns = self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' ORDER BY turn_key"))
        cases: tuple[tuple[SshBehavior, str], ...] = (("unreachable", "unreachable"), ("cut_before_status", "partial"))
        for behavior, state in cases:
            with self.subTest(state=state):
                if state == "partial":
                    path = self.root / "fixture-mac-home/.codex/sessions/2026/10/10/rollout-shared.jsonl"
                    self.append_bytes(path, (record("turn_context", "2026-10-11T00:59:00Z",
                                                    {"model": "gpt-6.1-sol"}) + "\n").encode())
                self.mac_pass(behavior)
                self.assertEqual(self.all_values(self.rows(query)), complete)
                self.assertEqual(self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' ORDER BY turn_key")), turns)
                _ = self.assert_readout(f"expected-{state}.md")
        _ = self.collect()
        self.assertEqual(self.all_values(self.rows(query)), complete)
        _ = self.assert_readout("expected-skipped.md")
        path = self.root / "fixture-mac-home/.codex/sessions/2026/10/10/rollout-shared.jsonl"
        self.append_bytes(path, (tokens("2026-10-11T01:00:00Z", 10, 0, 0) + "\n").encode())
        self.mac_pass()
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE machine = 'mac'")), 3)
        recovered = self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' ORDER BY turn_key"))
        self.mac_pass()
        self.assertEqual(self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' ORDER BY turn_key")), recovered)
        _ = self.assert_readout("expected-recovered.md")

    def test_never_completed_mac_read_is_named_without_discarding_local_figures(self) -> None:
        self.mac_pass("unreachable")
        _ = self.assert_readout("expected-never.md")


    def function_calls(self) -> ReadoutFunctionCalls:
        script = f"""
import sys
sys.path.insert(0, {str(READOUT.resolve().parents[2])!r})
import json
import sqlite3
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from scripts.whoami import seat_usage
from scripts.usage import usage_report, weights
calls = Counter()
class FunctionCountingConnection(sqlite3.Connection):
    def create_function(self, name, narg, func, *, deterministic=False):
        if func is None:
            return super().create_function(name, narg, None, deterministic=deterministic)
        calls.setdefault(name, 0)
        def counted(*values):
            calls[name] += 1
            return func(*values)
        return super().create_function(name, narg, counted, deterministic=deterministic)
    def create_aggregate(self, name, narg, aggregate_class):
        if aggregate_class is None:
            return super().create_aggregate(name, narg, None)
        class CountedAggregate:
            def __init__(self):
                self.aggregate = aggregate_class()
            def step(self, *values):
                calls[name + '.step'] += 1
                return self.aggregate.step(*values)
            def finalize(self):
                calls[name + '.finalize'] += 1
                return self.aggregate.finalize()
        return super().create_aggregate(name, narg, CountedAggregate)
connect = sqlite3.connect
def counting_connect(*args, **kwargs):
    kwargs['factory'] = FunctionCountingConnection
    return connect(*args, **kwargs)
with patch.object(sqlite3, 'connect', counting_connect), \\
     patch.object(weights, 'register', side_effect=AssertionError('readout must use grouped SQL')) as registration, \\
     patch.object(usage_report, 'sources', wraps=usage_report.sources) as sources:
    with redirect_stdout(StringIO()) as output:
        result = seat_usage.run(sys.argv[1:])
    assert result == 0, result
    assert '| Before |' in output.getvalue()
    print(json.dumps({{'calls': dict(calls), 'weighting_registered': registration.called,
                      'sources_called': sources.call_count}}))
"""
        result = self.readout(script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return cast(ReadoutFunctionCalls, json.loads(result.stdout))

    def test_readout_python_sql_calls_do_not_grow_with_turn_count_and_sources_are_shared(self) -> None:
        self.mac_pass()
        initial = self.function_calls()
        self.assertFalse(initial["weighting_registered"])
        self.assertEqual(initial["sources_called"], 1)
        before = len(self.rows("SELECT * FROM turns WHERE family = 'codex'"))
        self.assertEqual(before, 7)
        for path in self.codex_root.rglob("*.jsonl"):
            expanded: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                expanded.append(line)
                value = cast(dict[str, object], json.loads(line))
                payload = cast(dict[str, object], value.get("payload", {}))
                if payload.get("type") == "token_count":
                    expanded.extend([line] * 3)
            _ = path.write_text("\n".join(expanded) + "\n", encoding="utf-8")
        path = self.root / "fixture-mac-home/.codex/sessions/2026/10/10/rollout-shared.jsonl"
        expanded_mac: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            expanded_mac.append(line)
            value = cast(dict[str, object], json.loads(line))
            payload = cast(dict[str, object], value.get("payload", {}))
            if payload.get("type") == "token_count":
                expanded_mac.extend([line] * 3)
        _ = path.write_text("\n".join(expanded_mac) + "\n", encoding="utf-8")
        self.db_path.unlink()
        self.mac_pass()
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE family = 'codex'")), before * 4)
        expanded_calls = self.function_calls()
        self.assertEqual(expanded_calls["sources_called"], 1)
        self.assertEqual(set(expanded_calls["calls"]), set(initial["calls"]))
        for name, count in initial["calls"].items():
            self.assertLessEqual(expanded_calls["calls"][name], count, name)


if __name__ == "__main__":
    _ = unittest.main()
