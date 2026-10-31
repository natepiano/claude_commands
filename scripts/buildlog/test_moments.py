#!/usr/bin/env python3
"""Build-moment comparisons over fixture logs and fixture progress history."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

import index
import moments
import report
import store
import test_report as report_fixtures
from test_index import Record, call, sample, step


START = datetime(2026, 10, 10, tzinfo=UTC)
END = START + timedelta(days=2)


def build(record_id: str, seconds: float, **fields: object) -> Record:
    """A neutral repository unless the case explicitly supplies Hana evidence."""
    at = START + timedelta(seconds=seconds)
    return step(record_id, **{
        "started_at": at.isoformat(), "ended_at": at.isoformat(),
        "step": "nextest", "duration_s": 60.0, "mem_wait_s": 0.0,
        "repo": "neutral", "repo_path": "/r/neutral", "repo_identity": "neutral",
        "cwd": "/r/neutral", "worktree": "/r/neutral", "branch": "main",
        "target_folder": "/r/neutral/target", "build_base": "neutral",
        "argv": ["cargo", "nextest", "run", "--workspace"],
        "session": record_id, "call_id": record_id, **fields,
    })


class MomentTests(unittest.TestCase):
    fixture: report_fixtures.PowerProfileTests  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        # Reuse the p50 fixtures, including index, timezone and log isolation.
        self.fixture = report_fixtures.PowerProfileTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_PROGRESS_DIR": str(self.fixture.root / "progress"),
        })))

    def connection(self, *records: Record) -> sqlite3.Connection:
        self.fixture.write(*records)
        _ = index.update()
        # These cases exercise the indexed Hana predicate, including each
        # evidence column in isolation. The index derives these three columns
        # from other source fields, so bind their fixture values after indexing.
        with closing(sqlite3.connect(self.fixture.root / "index.sqlite")) as fixture_index:
            with fixture_index:
                for record in records:
                    if record.get("kind") == "step":
                        _ = fixture_index.execute(
                            "UPDATE steps SET repo = ?, target_folder = ?, build_base = ? WHERE id = ?",
                            (record.get("repo"), record.get("target_folder"),
                             record.get("build_base"), record["id"]),
                        )
        return index.read_only()

    def progress(self, events: list[Record], *, root: Path | None = None) -> None:
        path = (root if root is not None else self.fixture.root / "progress") / "fixture.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(event) + "\n" for event in events))

    def section(self, *records: Record, day: str = "2026-10-13") -> list[str]:
        with closing(self.connection(*records)) as connection:
            return report.moments_section(connection, day)

    def test_hana_membership_uses_direct_fields_majority_sessions_calls_and_exclusions(self) -> None:
        direct_fields = ("repo", "repo_path", "repo_identity", "cwd", "worktree", "branch",
                         "target_folder", "argv", "build_base")
        direct = [build(f"direct-{field}", number, **{field: ["CaTaLySt"] if field == "argv" else "FaIrY_DuSt"})
                  for number, field in enumerate(direct_fields)]
        records = [*direct,
                   build("majority-a", 20, repo="hana", session="shared"),
                   build("majority-b", 21, repo="tool-based-ui", session="shared"),
                   build("inherited-session", 22, session="shared"),
                   build("unit-a", 23, repo="hana", unit_session="director"),
                   build("unit-b", 24, repo="hana", unit_session="director"),
                   build("inherited-unit", 25, unit_session="director"),
                   call("hana-call", started_at=(START + timedelta(seconds=10)).isoformat()),
                   build("inherited-call", 26, call_id="hana-call"),
                   build("neutral", 27),
                   build("tie-direct", 28, repo="hana", session="tie"),
                   build("tie-neutral", 29, session="tie"),
                   build("mac", 30, host="macbook", repo="hana"),
                   build("outside", -1, repo="hana")]
        excluded = ("cargo-liner", "cargo-handler", "cargo-tile", "cargo-berth", "bevy_brp", ".claude")
        records.extend(build(f"excluded-{name}", 40 + number, repo=name, call_id="hana-call")
                       for number, name in enumerate(excluded))
        with closing(self.connection(*records)) as connection:
            self.assertEqual(moments.hana_step_ids(connection, START, END), frozenset({
                *(f"direct-{field}" for field in direct_fields),
                "majority-a", "majority-b", "inherited-session", "unit-a", "unit-b",
                "inherited-unit", "inherited-call", "tie-direct",
            }))

    def rate_records(self) -> list[Record]:
        return [
            build("pass-a", 3600, repo="hana", cwd="/r/pass", session="pass", duration_s=3600, crates_compiled=300),
            build("pass-b", 7500, repo="hana", cwd="/r/pass/target/verify-doc/work", session="pass",
                  duration_s=1800, mem_wait_s=600, crates_compiled=0),
            build("fail-a", 21600, repo="hana", cwd="/r/fail", session="fail", status=100,
                  duration_s=1200, crates_compiled=301),
            build("fail-b", 23100, repo="hana", cwd="/r/fail", session="fail", status=101,
                  duration_s=600, mem_wait_s=600, crates_compiled=300),
            build("independent", 90000, repo="hana", cwd="/r/other", duration_s=3600, crates_compiled=0),
            build("clippy-a", 10000, repo="hana", step="clippy", duration_s=3600, mem_wait_s=1800),
            build("clippy-b", 100000, repo="hana", step="clippy", duration_s=1800),
            build("sweep", 110000, repo="hana", step="sweep", duration_s=1800, mem_wait_s=1800),
            build("other-repo", 120000, step="fmt", duration_s=3600, mem_wait_s=3600),
            build("mac", 130000, host="macbook", repo="hana", duration_s=99999),
            build("end-exclusive", 172800, repo="hana", duration_s=99999),
        ]

    def test_each_rate_uses_actual_window_days_and_the_documented_pair_rules(self) -> None:
        self.progress([
            {"event_type": "phase_finished", "status": "completed", "timestamp_epoch": START.timestamp() + 1,
             "phase_instance_id": "phase-a", "working_dir": "/r/hana", "host": "natedev"},
            {"event_type": "phase_finished", "status": "stopped", "timestamp_epoch": START.timestamp() + 2,
             "phase_instance_id": "phase-b", "working_dir": "/r/tool-based-ui", "host": "natedev"},
            {"event_type": "phase_finished", "status": "completed", "timestamp_epoch": START.timestamp() + 3,
             "phase_instance_id": "neutral", "working_dir": "/r/other", "host": "natedev"},
            {"event_type": "phase_finished", "status": "completed", "timestamp_epoch": START.timestamp() + 4,
             "phase_instance_id": "mac", "working_dir": "/r/hana", "host": "macbook"},
        ])
        with closing(self.connection(*self.rate_records())) as connection:
            rates = moments.window_rates(connection, START, END, phases=moments.phase_events())
        self.assertEqual(rates.days, 2)
        self.assertEqual(rates.phase_count, 2)
        expected: dict[moments.MetricKey, float] = {
            "run_hours_all": 3.0, "run_hours_hana": 2.5,
            "wait_hours_all": 7 / 6, "wait_hours_hana": 2 / 3,
            "nextest": 2.5, "failed_pct": 40.0, "reruns_pct": 40.0, "queue_hidden_pct": 40.0,
            "pass_reruns": 0.5, "pass_hours": 0.25, "fail_reruns": 0.5, "fail_hours": 1 / 12,
            "clippy": 1.0, "nextest_per_phase": 2.5, "clippy_per_phase": 1.0,
            "clone_rebuilds": 1.5, "clone_hours": 0.75, "clone_repeats_pct": 100 / 3,
        }
        self.assertEqual(set(rates.rates), set(expected))
        for key, value in expected.items():
            with self.subTest(rate=key):
                actual = rates.rates[key]
                self.assertIsInstance(actual, (int, float))
                self.assertAlmostEqual(cast(float, actual), value)
        self.assertEqual(rates.totals["nextest"], 5)
        self.assertEqual(rates.totals["pass_reruns"], 1)
        self.assertAlmostEqual(rates.totals["run_hours_hana"], 5)

    def test_missing_phases_are_unknown_rather_than_zero_load(self) -> None:
        with closing(self.connection(build("hana", 10, repo="hana"))) as connection:
            rates = moments.window_rates(connection, START, END)
        self.assertEqual(rates.phase_count, 0)
        self.assertIsInstance(rates.rates["nextest_per_phase"], moments.RateUnavailable)
        self.assertIsInstance(rates.rates["clippy_per_phase"], moments.RateUnavailable)

    def test_redirected_log_uses_fixture_progress_when_no_progress_override_is_set(self) -> None:
        self.progress([{
            "event_type": "phase_finished", "status": "completed", "timestamp_epoch": START.timestamp(),
            "phase_instance_id": "fixture-only", "working_dir": "/r/hana",
        }])
        with mock.patch.dict(os.environ):
            _ = os.environ.pop("BUILDLOG_PROGRESS_DIR", None)
            with mock.patch.object(Path, "home", side_effect=AssertionError("live progress was requested")):
                phases = moments.phase_events()
        self.assertEqual(phases, (moments.PhaseFinished(START.timestamp(), "fixture-only", "/r/hana"),))

    def test_previous_step_must_be_inside_the_measured_window(self) -> None:
        with closing(self.connection(
            build("before", -60, repo="hana", session="same", duration_s=120, crates_compiled=300),
            build("inside", 60, repo="hana", session="same", crates_compiled=300),
        )) as connection:
            rates = moments.window_rates(connection, START, END)
        self.assertEqual(rates.rates["reruns_pct"], 0)
        self.assertEqual(rates.rates["queue_hidden_pct"], 0)
        self.assertEqual(rates.rates["pass_reruns"], 0)
        self.assertEqual(rates.rates["clone_repeats_pct"], 0)

    def test_today_comparison_ends_at_now_for_rates_and_p50(self) -> None:
        at = START + timedelta(days=1, hours=12)
        now = at + timedelta(hours=6)
        moment = moments.BuildMoment(at, "today's change", "all")
        with (
            mock.patch.object(moments, "MOMENTS", (moment,)),
            mock.patch.object(report, "datetime", wraps=datetime,
                              now=mock.Mock(return_value=now)),
            closing(self.connection(
                build("before-now", 86400, repo="hana", duration_s=20),
                build("after-change", 133200, repo="hana", duration_s=40),
                build("not-yet", 154800, repo="hana", duration_s=999),
            )) as connection,
            mock.patch.object(moments, "window_rates", wraps=moments.window_rates) as measured,
            mock.patch.object(report, "profile_step_durations", wraps=report.profile_step_durations) as grouped,
        ):
            lines = report.moments_section(connection, "2026-10-11")
        self.assertEqual([(entry.args[1], entry.args[2]) for entry in measured.call_args_list],
                         [(at - timedelta(days=7), at), (at, now)])
        self.assertEqual([(entry.args[1], entry.args[2]) for entry in grouped.call_args_list],
                         [(at - timedelta(days=7), at), (at, now)])
        nextest = next(line for line in lines if line.startswith("| Hana nextest steps |"))
        self.assertEqual([cell.strip() for cell in nextest.strip("|").split("|")][2], "4.0")
        self.assertEqual(self.fixture.rows(lines)[0][3:], ["1", "20.0 s", "1", "40.0 s", "—"])
        self.assertIn("Before: 7.000 days; after: 0.250 days (thin).", lines)

    def test_progress_history_uses_the_writers_configured_history_root(self) -> None:
        history = self.fixture.root / "writer-history"
        self.progress([{
            "event_type": "phase_finished", "status": "completed", "timestamp_epoch": START.timestamp(),
            "phase_instance_id": "writer-root", "working_dir": "/r/hana",
        }], root=history / "runs")
        with mock.patch.dict(os.environ, {"UNIT_HISTORY_DIR": str(history)}):
            _ = os.environ.pop("BUILDLOG_PROGRESS_DIR", None)
            _ = os.environ.pop("BUILDLOG_DIR", None)
            with (
                mock.patch.object(Path, "home", return_value=self.fixture.root / "home"),
                mock.patch.object(store, "host_name", return_value="natedev"),
            ):
                phases = moments.phase_events()
        self.assertEqual(phases, (moments.PhaseFinished(START.timestamp(), "writer-root", "/r/hana"),))

    def test_fixed_moment_remains_visible_while_its_after_window_is_recent(self) -> None:
        first = moments.BuildMoment(START, "older rule", "all")
        middle = moments.BuildMoment(END, "replaced rule", "all")
        latest = moments.BuildMoment(END + timedelta(days=1), "current rule", "all")
        observed = moments.BuildMoment(END + timedelta(hours=12), "old observed profile", "all")
        end = END + timedelta(days=10)
        with mock.patch.object(moments, "MOMENTS", (first, middle, latest)):
            windows = moments.comparison_windows((first, middle, latest, observed), end)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].moment, latest)
        self.assertEqual(windows[0].before_start, observed.at)
        self.assertEqual(windows[0].after_end, end)

    def test_offset_timestamps_use_their_instant_in_membership_rates_and_p50(self) -> None:
        inside = "2026-10-09T23:30:00-04:00"
        outside = "2026-10-10T01:00:00+04:00"
        with closing(self.connection(
            build("direct-offset", 0, repo="hana", started_at=inside, duration_s=60),
            call("offset-call", started_at=inside),
            build("call-offset", 0, call_id="offset-call", started_at=inside, duration_s=120),
            build("outside-offset", 0, repo="hana", started_at=outside, duration_s=999),
        )) as connection:
            ids = moments.hana_step_ids(connection, START, END)
            rates = moments.window_rates(connection, START, END)
            groups = report.profile_step_durations(connection, START, END, hana_only=True)
        self.assertEqual(ids, frozenset({"direct-offset", "call-offset"}))
        self.assertEqual(rates.totals["nextest"], 2)
        self.assertAlmostEqual(rates.totals["run_hours_hana"], 0.05)
        self.assertEqual(sorted(value for group in groups.values() for value in group), [60, 120])

    def test_missing_worker_and_folder_identities_count_without_pairing_unrelated_steps(self) -> None:
        records = [build(f"unknown-worker-{number}", 100 + number * 200, repo="hana", session=None,
                         cwd=f"/r/distinct-{number}", duration_s=100, crates_compiled=300,
                         status=0 if number < 2 else 1) for number in range(4)]
        records.extend(build(f"unknown-folder-{number}", 1000 + number * 200, repo="hana", cwd=None,
                             duration_s=100, crates_compiled=300) for number in range(2))
        records.extend(build(f"known-{number}", 2000 + number * 200, repo="hana", session="known",
                             cwd="/r/known", duration_s=100, crates_compiled=300) for number in range(2))
        with closing(self.connection(*records)) as connection:
            rates = moments.window_rates(connection, START, END)
        self.assertEqual(rates.totals["nextest"], 8)
        self.assertEqual(rates.totals["clone_rebuilds"], 8)
        self.assertAlmostEqual(rates.totals["run_hours_hana"], 800 / 3600)
        self.assertEqual(rates.totals["pass_reruns"], 1)
        self.assertEqual(rates.totals["fail_reruns"], 0)
        self.assertAlmostEqual(rates.totals["pass_hours"], 100 / 3600)
        self.assertEqual(rates.totals["fail_hours"], 0)
        self.assertEqual(rates.totals["clone_repeats"], 1)
        self.assertEqual(rates.rates["reruns_pct"], 12.5)
        self.assertEqual(rates.rates["queue_hidden_pct"], 12.5)
        self.assertEqual(rates.rates["clone_repeats_pct"], 12.5)

    def test_folder_pairs_distinguish_overlaps_same_calls_and_submit_time(self) -> None:
        with closing(self.connection(
            build("first", 3600, repo="hana", duration_s=1200),
            build("overlap", 4200, repo="hana", duration_s=600, call_id="shared"),
            build("same-call", 5400, repo="hana", duration_s=600, call_id="shared"),
            build("late-start", 6601, repo="hana", duration_s=600, mem_wait_s=1),
        )) as connection:
            rates = moments.window_rates(connection, START, END)
        self.assertEqual(rates.rates["reruns_pct"], 25)
        self.assertEqual(rates.rates["queue_hidden_pct"], 50)

    def test_clone_repeat_requires_the_previous_nextest_to_be_a_clone_within_four_hours(self) -> None:
        with closing(self.connection(
            build("clone-a", 100, repo="hana", session="worker", crates_compiled=300, duration_s=100),
            build("warm", 300, repo="hana", session="worker", crates_compiled=299, duration_s=100),
            build("clone-b", 500, repo="hana", session="worker", crates_compiled=300, duration_s=100),
            build("clone-c", 15001, repo="hana", session="worker", crates_compiled=300, duration_s=100),
            build("clone-d", 29501, repo="hana", session="worker", crates_compiled=300),
        )) as connection:
            rates = moments.window_rates(connection, START, END)
        self.assertEqual(rates.totals["clone_rebuilds"], 4)
        self.assertEqual(rates.rates["clone_repeats_pct"], 25)

    def test_observed_profile_merges_with_fixed_moments_and_cuts_both_windows(self) -> None:
        first = moments.BuildMoment(START, "first", "all", moments.NoSdlcBaseline())
        last = moments.BuildMoment(END, "last", "all", moments.NoSdlcBaseline())
        profile_at = START + timedelta(days=1)
        with mock.patch.object(moments, "MOMENTS", (first, last)):
            with closing(self.connection(sample(profile_at.isoformat(), power_profile="performance"))) as connection:
                with (
                    mock.patch.object(moments, "window_rates", wraps=moments.window_rates) as rates,
                    mock.patch.object(moments, "phase_events", wraps=moments.phase_events) as phases,
                ):
                    lines = report.moments_section(connection, "2026-10-12")
        phases.assert_called_once()
        comparisons = [line for line in lines if line.startswith("Before ")]
        self.assertEqual(len(comparisons), 3)
        self.assertIn("last", comparisons[0])
        self.assertIn("performance", comparisons[1])
        self.assertIn("first", comparisons[2])
        bounds = [(entry.args[1], entry.args[2]) for entry in rates.call_args_list]
        self.assertIn((profile_at, END), bounds)
        self.assertIn((START, profile_at), bounds)
        self.assertIn((START - timedelta(days=7), START), bounds)
        self.assertNotIn((END - timedelta(days=7), END), bounds)

    def test_hana_scoped_moment_limits_both_rates_and_p50_to_hana(self) -> None:
        at = START + timedelta(days=1)
        moment = moments.BuildMoment(at, "hana optimization", "hana", moments.NoSdlcBaseline())
        with mock.patch.object(moments, "MOMENTS", (moment,)):
            lines = self.section(
                build("hana-before", 3600, repo="hana", step="clippy", duration_s=20),
                build("hana-after", 90000, repo="hana", step="clippy", duration_s=40),
                build("other-before", 7200, step="clippy", duration_s=900),
                build("other-after", 92000, step="clippy", duration_s=999),
                day="2026-10-11",
            )
        text = "\n".join(lines)
        self.assertIn("hana optimization", text)
        for rate in moments.RATE_ROWS:
            with self.subTest(row=rate.key):
                if rate.scope == "all":
                    self.assertNotIn("| " + rate.label + " |", text)
                else:
                    self.assertIn("| " + rate.label + " |", text)
        p50 = self.fixture.rows(lines)
        self.assertEqual(len(p50), 1)
        self.assertEqual(p50[0][3:], ["1", "20.0 s", "1", "40.0 s", "—"])

    def test_definitions_thin_and_sdlc_baseline_are_printed_once(self) -> None:
        moment = next(item for item in moments.MOMENTS if "Moment 1" in item.name)
        with mock.patch.object(moments, "MOMENTS", (moment,)):
            text = "\n".join(self.section(day="2026-10-31"))
        self.assertIn("### Before and after", text)
        self.assertIn("thin", text)
        self.assertRegex(text, r"(?i)7.day.*baseline|baseline.*7.day")
        self.assertRegex(text, r"\(sdlc 246(?:\.0)?\)")
        self.assertIn("50–55", text)
        self.assertIn("134", text)
        self.assertIn("Mac", text)
        for definition in ("natedev only", "hana set", "sweep", "300", "progress", "window days"):
            with self.subTest(definition=definition):
                self.assertIn(definition, text.lower())
        for threshold in (r"600\s*s|10\s*min", r"3600\s*s|60\s*min", r"4\s*h"):
            self.assertRegex(text.lower(), threshold)
        self.assertEqual(text.lower().count("hana set:"), 1)
        for rate in moments.RATE_ROWS:
            self.assertIn("| " + rate.label + " |", text)

    def test_fixed_moments_render_in_utc_order_with_their_recorded_times(self) -> None:
        lines = self.section(day="2026-10-31")
        comparisons = [line for line in lines if line.startswith("Before ")]
        expected = (
            ("Build rules 1 (Moment 1)", "2026-10-31 10:50:00 UTC"),
            ("Floor sweep (e3b8e391)", "2026-10-29 18:26:00 UTC"),
            ("hana Bevy dylib", "2026-10-29 09:33:00 UTC"),
            ("hana opt-level 1", "2026-10-29 06:59:00 UTC"),
            ("baloo content indexing off", "2026-10-28 13:24:00 UTC"),
            ("herdr cgroup move", "2026-10-28 13:01:00 UTC"),
        )
        self.assertEqual(len(comparisons), len(expected))
        for comparison, (name, at) in zip(comparisons, expected, strict=True):
            self.assertIn(name, comparison)
            self.assertIn(at, comparison)
        text = "\n".join(lines)
        self.assertIn("52.7", text)
        self.assertIn("29.8", text)
        floor = lines.index("#### Floor sweep (e3b8e391)")
        next_moment = next(position for position in range(floor + 1, len(lines)) if lines[position].startswith("#### "))
        clones = next(line for line in lines[floor:next_moment] if line.startswith("| Hana clone rebuilds |"))
        # The quoted 124 rebuilds cover 40 h 24 min until Moment 1.
        self.assertIn("(sdlc 73.7)", clones)

    def test_sdlc_annotation_marks_only_relative_differences_above_five_percent(self) -> None:
        baseline = moments.SdlcBaseline((
            moments.BaselineFigure("nextest", 5 / 7 * 1.04),
            moments.BaselineFigure("clippy", 1.0),
        ))
        moment = moments.BuildMoment(END, "quoted baseline", "all", baseline)
        with mock.patch.object(moments, "MOMENTS", (moment,)):
            lines = self.section(*self.rate_records(), day="2026-10-12")
        nextest = next(line for line in lines if line.startswith("| Hana nextest steps |"))
        clippy = next(line for line in lines if line.startswith("| Hana clippy steps |"))
        self.assertNotIn("(sdlc", nextest)
        self.assertIn("(sdlc 1", clippy)

    def test_moments_command_prints_the_same_section_from_fixture_index(self) -> None:
        with closing(self.connection(build("measured", 3600, repo="hana"))) as connection:
            expected = report.moments_section(connection, "2026-10-30")
        database = self.fixture.root / "index.sqlite"
        before = database.read_bytes()
        result = subprocess.run(
            [sys.executable, str(Path(report.__file__).with_name("cli.py")), "moments", "2026-10-30"],
            env={**os.environ, "BUILDLOG_PROGRESS_DIR": str(self.fixture.root / "progress")},
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), expected)
        self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
