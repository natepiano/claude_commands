"""Fixture tests for the long-lived-seat usage report."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import seat_usage


def record(record_type: str, at: str, payload: dict[str, object]) -> str:
    return json.dumps({"timestamp": at, "type": record_type, "payload": payload})


def session(at: str) -> str:
    return record("session_meta", at, {"timestamp": at})


def tokens(at: str, input_tokens: int, cached: int, output: int) -> str:
    return record("event_msg", at, {
        "type": "token_count",
        "info": {"last_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
        }},
    })


def edit(at: str) -> str:
    return record("response_item", at, {
        "type": "custom_tool_call",
        "input": '{"command":"apply_patch","patch":"*** Begin Patch"}',
    })


def inspection(at: str) -> str:
    return record("response_item", at, {
        "type": "custom_tool_call",
        "input": '{"command":"rg -n apply_patch scripts"}',
    })


class FakeRemoteProcess:
    stdout: StringIO
    returncode: int

    def __init__(self, output: str, returncode: int) -> None:
        self.stdout = StringIO(output)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class SeatUsageTests(unittest.TestCase):
    root: Path
    sessions: Path

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.sessions = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sessions = self.root / "sessions"

    def write_rollout(self, day: str, name: str, lines: list[str]) -> Path:
        year, month, date = day.split("-")
        directory = self.sessions / year / month / date
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.jsonl"
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_rollout_metrics_match_startup_parser_and_count_compactions(self) -> None:
        _ = self.write_rollout("2026-01-01", "before", [
            session("2026-01-01T00:00:00Z"),
            tokens("2026-01-01T01:00:00Z", 100, 20, 10),
            edit("2026-01-01T02:00:00Z"),
            tokens("2026-01-01T03:00:00Z", 50, 50, 0),
            record("compacted", "2026-01-01T04:00:00Z", {}),
        ])
        _ = self.write_rollout("2026-01-02", "after-edited", [
            session("2026-01-02T00:00:00Z"),
            tokens("2026-01-02T01:00:00Z", 200, 100, 0),
            edit("2026-01-02T02:00:00Z"),
            tokens("2026-01-02T03:00:00Z", 100, 100, 10),
            record("response_item", "2026-01-02T04:00:00Z", {
                "type": "item_completed", "item": {"type": "contextCompaction"},
            }),
        ])
        _ = self.write_rollout("2026-01-02", "after-unedited", [
            session("2026-01-02T00:30:00Z"),
            tokens("2026-01-02T01:30:00Z", 10, 0, 0),
        ])
        before_window = seat_usage.window_from(("2026-01-01", "2026-01-02"))
        after_window = seat_usage.window_from(("2026-01-02", "2026-01-03"))
        threads = seat_usage.local_rollouts(self.sessions, before_window.start)

        before = seat_usage.window_usage(threads, before_window)
        after = seat_usage.window_usage(threads, after_window)

        self.assertEqual(before.threads_opened, 1)
        self.assertEqual(before.total, seat_usage.TokenUsage(80, 70, 10))
        self.assertEqual(before.before_first_edit, seat_usage.TokenUsage(80, 20, 10))
        self.assertEqual(before.compactions, 1)
        self.assertEqual(after.threads_opened, 2)
        self.assertEqual(after.active_threads, 2)
        self.assertEqual(after.total, seat_usage.TokenUsage(110, 200, 10))
        self.assertEqual(after.edited_thread_total.weighted(), 200.0)
        self.assertEqual(after.before_first_edit.weighted(), 110.0)
        self.assertEqual(after.compactions, 1)

        report = seat_usage.make_window_report("After", after, 2, (), after_window)
        self.assertEqual(report.before_edit_share, seat_usage.KnownRate(0.55))
        self.assertEqual(report.compactions_per_thread, seat_usage.KnownRate(0.5))
        self.assertEqual(report.usage_per_phase, seat_usage.KnownRate(105.0))

    def test_text_mentioning_apply_patch_does_not_mark_the_first_edit(self) -> None:
        thread = seat_usage.parse_rollout((
            session("2026-01-01T00:00:00Z"),
            inspection("2026-01-01T00:01:00Z"),
            tokens("2026-01-01T00:02:00Z", 20, 0, 0),
            edit("2026-01-01T00:03:00Z"),
            tokens("2026-01-01T00:04:00Z", 10, 0, 0),
        ))
        self.assertTrue(thread.ever_edited)
        self.assertEqual([sample.before_first_edit for sample in thread.usage], [True, False])
        self.assertEqual(thread.edits, (datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc),))

    def test_remote_stream_is_split_at_each_session_metadata_record(self) -> None:
        lines = [
            session("2026-01-01T00:00:00Z"),
            tokens("2026-01-01T01:00:00Z", 20, 10, 1),
            session("2026-01-02T00:00:00Z"),
            tokens("2026-01-02T01:00:00Z", 40, 20, 2),
        ]
        threads = seat_usage.parse_concatenated_rollouts(lines)
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0].usage[0].usage, seat_usage.TokenUsage(10, 10, 1))
        self.assertEqual(threads[1].usage[0].usage, seat_usage.TokenUsage(20, 20, 2))

    def test_mac_rollouts_streams_successful_fake_ssh_without_real_network(self) -> None:
        output = "\n".join((
            session("2026-01-01T00:00:00Z"),
            tokens("2026-01-01T01:00:00Z", 20, 10, 1),
            session("2026-01-02T00:00:00Z"),
            tokens("2026-01-02T01:00:00Z", 40, 20, 2),
        )) + "\n"
        fake = cast(subprocess.Popen[str], cast(object, FakeRemoteProcess(output, 0)))
        with patch("seat_usage.subprocess.Popen", return_value=fake) as launch:
            result = seat_usage.mac_rollouts(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIsInstance(result, seat_usage.MacIncluded)
        assert isinstance(result, seat_usage.MacIncluded)
        self.assertEqual(len(result.threads), 2)
        self.assertEqual(launch.call_count, 1)

    def test_mac_rollouts_reports_failed_fake_ssh_without_real_network(self) -> None:
        fake = cast(subprocess.Popen[str], cast(object, FakeRemoteProcess("", 255)))
        with patch("seat_usage.subprocess.Popen", return_value=fake):
            result = seat_usage.mac_rollouts(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result, seat_usage.MacUnreachable("ssh exit 255"))

    def test_live_readings_override_exact_duplicates_without_dropping_partial_days(self) -> None:
        baseline = self.root / "baseline.jsonl"
        live = self.root / "live.jsonl"
        _ = baseline.write_text("\n".join((
            json.dumps({
                "account": "codex 1", "at": "2026-10-01T00:00:00Z",
                "remaining": 60, "credits": 100,
            }),
            json.dumps({
                "account": "codex 1", "at": "2026-10-02T00:00:00Z",
                "remaining": 50, "credits": 90,
            }),
            json.dumps({
                "account": "codex 2", "at": "2026-10-02T12:00:00Z",
                "remaining": 90, "credits": 50,
            }),
        )) + "\n", encoding="utf-8")
        _ = live.write_text("\n".join((
            json.dumps({
                "account": "codex 1", "at": "2026-10-02T00:00:00Z",
                "remaining": 55, "credits": 80,
            }),
        )) + "\n", encoding="utf-8")

        readings = seat_usage.merged_readings(
            seat_usage.reading_records(live),
            seat_usage.reading_records(baseline),
        )
        window = seat_usage.window_from(("2026-10-01", "2026-10-03"))

        self.assertEqual([reading.account for reading in readings], ["codex 1", "codex 1", "codex 2"])
        self.assertEqual(readings[1].credits, seat_usage.KnownCreditBalance(80.0))
        switch_rate = seat_usage.switches_per_week(readings, window)
        self.assertIsInstance(switch_rate, seat_usage.KnownSwitchRate)
        assert isinstance(switch_rate, seat_usage.KnownSwitchRate)
        self.assertAlmostEqual(switch_rate.per_week, 7.0 / 1.5)
        self.assertEqual(seat_usage.credits_per_day(readings, window), seat_usage.KnownCreditRate(20.0))

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

    def test_checkpoint_count_uses_every_branch(self) -> None:
        window = seat_usage.window_from(("2026-10-01", "2026-10-03"))
        log = "\n".join((
            "1790812800\x00checkpoint(alpha): phase 1",
            "1790899200\x00ordinary commit",
            "1790985599\x00checkpoint(beta): phase 2",
        ))
        with patch.object(seat_usage, "git_text", return_value=log) as git:
            self.assertEqual(seat_usage.checkpoint_count((self.root,), window), 2)
            git.assert_called_once_with(self.root, "log", "--all", "--format=%ct%x00%s")

    def test_production_documents_contribute_repositories_and_worktrees(self) -> None:
        document = self.root / "sample-production.md"
        _ = document.write_text(
            "".join((
                "# Production\n\n- **Repository:** `/repo/main`\n\n",
                "## Units\n\n| Unit | Worktree |\n| --- | --- |\n",
                "| a | `/repo/worktree` |\n",
            )),
            encoding="utf-8",
        )
        self.assertEqual(
            seat_usage.production_paths(document),
            (Path("/repo/main"), Path("/repo/worktree")),
        )

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

    def test_markdown_is_short_complete_and_hides_machine_details(self) -> None:
        usage = seat_usage.WindowUsage(2, 2, seat_usage.TokenUsage(100, 100, 10),
                                       seat_usage.TokenUsage(100, 100, 10),
                                       seat_usage.TokenUsage(50, 0, 0), 1)
        window = seat_usage.window_from(("2026-10-01", "2026-10-02"))
        before = seat_usage.make_window_report("Before", usage, 2, (), window)
        after = seat_usage.make_window_report("After", usage, 4, (), window)
        output = seat_usage.render_report(before, after, seat_usage.MacIncluded(()))
        for heading in (
            "Threads opened", "Weighted usage", "Before first edit", "Compactions (per thread)",
            "Checkpointed phases", "Account switches/week", "Credits used/day",
        ):
            self.assertIn(heading, output)
        self.assertIn("gpt-5.6-sol, OpenAI published prices read 2026-10-10", output)
        self.assertIn("uncached input 1 · cached input 0.1 · output 8", output)
        self.assertIn("1 (0.50)", output)
        self.assertIn("down 50.0%", output)
        self.assertNotIn("/home/", output)
        self.assertNotIn("natemccoy", output)

    def test_unreachable_mac_makes_the_verdict_unavailable(self) -> None:
        usage = seat_usage.WindowUsage(1, 1, seat_usage.TokenUsage(100, 0, 0),
                                       seat_usage.TokenUsage(100, 0, 0),
                                       seat_usage.TokenUsage(50, 0, 0), 0)
        window = seat_usage.window_from(("2026-10-01", "2026-10-02"))
        before = seat_usage.make_window_report("Before", usage, 1, (), window)
        after = seat_usage.make_window_report("After", usage, 2, (), window)
        output = seat_usage.render_report(before, after, seat_usage.MacUnreachable("connection timed out"))
        self.assertIn("Mac: unreachable (connection timed out)", output)
        self.assertIn("Weighted usage per checkpointed phase: unavailable", output)
        self.assertNotIn("down ", output)
        self.assertNotIn("up ", output)


if __name__ == "__main__":
    _ = unittest.main()
