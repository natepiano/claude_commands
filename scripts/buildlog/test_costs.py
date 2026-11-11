"""Cost ranking and phase wall time from isolated build and progress records."""

from __future__ import annotations

import unittest
import os
import io
import time
import subprocess
from contextlib import closing, redirect_stderr
from datetime import timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.buildlog import costs
from scripts.buildlog import cli
from scripts.buildlog import index
from scripts.buildlog import moments
from scripts.buildlog import record
from scripts.buildlog import test_moments as fixtures
from scripts.buildlog.test_index import Record, call, ci_job, ci_run
from scripts.buildlog.test_holds import hold_event


class CostTests(unittest.TestCase):
    fixture: fixtures.MomentTests  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.moment_fixture(self)

    def test_machine_aliases_read_sync_configuration_once(self) -> None:
        with (
            mock.patch.object(record, "configured_targets", return_value=("first", "second")),
            mock.patch.object(record, "sync_config", return_value={
                "sync.hosts.first": "first-host", "sync.hosts.second": "second-host",
                "sync.hosts.extra": "extra-host",
            }) as read,
        ):
            aliases: dict[str, str] = {}
            self.assertEqual(costs.machine_name(None, "second-host", aliases), "second")
            self.assertEqual(aliases, {"first-host": "first", "second-host": "second", "extra-host": "extra"})
            read.assert_called_once_with()

    def progress(self, durations: list[tuple[str, float]]) -> None:
        events: list[Record] = []
        for number, (directory, seconds) in enumerate(durations):
            finish = fixtures.START.timestamp() + number + 10
            common: Record = {"phase_instance_id": f"phase-{number}", "working_dir": directory}
            events.extend(({**common, "event_type": "phase_started", "timestamp_epoch": finish - seconds},
                           {**common, "event_type": "phase_finished", "timestamp_epoch": finish, "status": "completed", "phase_started_at": finish - seconds}))
        self.fixture.progress(events)

    def test_first_source_uses_a_recorded_event_instead_of_a_month_boundary(self) -> None:
        self.fixture.fixture.write(fixtures.build("first", -3600, target="mac"),
                                   fixtures.build("later", 10, target="mac"))
        path = self.fixture.fixture.root/"mac/2026-10.jsonl"
        path.parent.mkdir(parents=True)
        _ = (self.fixture.fixture.root/"natedev/profiles.jsonl").replace(path)
        _ = index.update()
        path.unlink()
        with closing(index.read_only()) as connection:
            reading = costs.read_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual(reading.first_sources["mac"], fixtures.START.timestamp()-3600)

    def test_empty_and_eventless_monthly_sources_never_activate_a_machine(self) -> None:
        path = self.fixture.fixture.root/"aws/2026-11.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()
        eventless = path.with_name("2026-10.jsonl")
        _ = eventless.write_text('{}\n')
        with closing(self.fixture.connection(fixtures.build("local", 10, target="natedev"))) as connection:
            reading = costs.read_costs(connection, fixtures.START, fixtures.END)
        self.assertNotIn("aws", reading.first_sources)

    def test_phase_score_uses_matched_wall_time_and_nearest_rank_p90(self) -> None:
        self.progress([("/r/hana", 60), ("/r/hana", 120), ("/r/tool-based-ui", 180), ("/r/other", 240)])
        with closing(self.fixture.connection()) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        durations = [phase.wall_s for phase in result.phases if isinstance(phase.wall_s, float)]
        hana = [phase.wall_s for phase in result.phases
                if moments.hana_name(phase.working_dir) and isinstance(phase.wall_s, float)]
        self.assertEqual((len(hana), moments.nearest_rank(hana, 50), moments.nearest_rank(hana, 90)), (3, 120, 180))
        self.assertEqual((len(durations), moments.nearest_rank(durations, 50), moments.nearest_rank(durations, 90)), (4, 120, 240))
        self.assertEqual(len(result.phases), 4)

    def test_missing_start_preserves_finished_count_without_inventing_duration(self) -> None:
        event: Record = {"event_type": "phase_finished", "timestamp_epoch": fixtures.START.timestamp(),
                         "phase_instance_id": "missing", "working_dir": "/r/hana"}
        self.fixture.progress([event, event, {**event, "phase_instance_id": "outside", "timestamp_epoch": fixtures.END.timestamp()}])
        phases = costs.finished_phases(fixtures.START, fixtures.END)
        self.assertEqual(len(phases), 1)
        self.assertIsInstance(phases[0].wall_s, moments.PhaseStartUnavailable)

    def test_finish_wall_time_needs_no_start_event_or_event_field_substring(self) -> None:
        self.fixture.progress([
            {"event_type": "phase_finished", "phase_instance_id": "kept", "working_dir": "/r/hana",
             "timestamp_epoch": fixtures.START.timestamp() + 10, "phase_started_at": fixtures.START.timestamp() - 3590},
            {"event_type": "unrelated", "phase_started_at": 1, "phase_instance_id": "noise"},
        ])
        self.fixture.progress([
            {"event_type": "phase_finished", "phase_instance_id": "old", "working_dir": "/r/other",
             "timestamp_epoch": 2, "phase_started_at": 1},
        ], filename="old.jsonl")
        phases = costs.finished_phases(fixtures.START, fixtures.END)
        self.assertEqual(len(phases), 1)
        self.assertEqual(phases[0].wall_s, 3600)

    def test_costs_rank_all_hosts_and_divide_by_all_finished_phases(self) -> None:
        self.progress([("/r/hana", 60), ("/r/other", 120)])
        with closing(self.fixture.connection(
            fixtures.build("wait", 100, mem_wait_s=21600),
            fixtures.build("failure", 200, duration_s=10800, status=1),
            fixtures.build("cold", 300, duration_s=7200, crates_compiled=50, host="aws-builder-1"),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([(row.machine, row.cause, row.seconds) for row in result.ranked[:3]], [
            ("natedev", "Memory wait", 21600), ("natedev", "Failed runs", 10800),
            ("aws-builder-1", "Cold rebuilds", 7200),
        ])
        self.assertEqual(len(result.phases), 2)
        self.assertEqual(result.ranked[2].seconds / len(result.phases), 3600)

    def test_costs_preserve_an_empty_phase_denominator(self) -> None:
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=7200))) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual(result.phases, ())
        self.assertEqual(result.ranked[0], costs.RankedCost("natedev", "Memory wait", 7200))

    def test_passed_tree_cost_requires_the_same_tree_build_input_and_worker(self) -> None:
        records = [
            fixtures.build("passed", 100, duration_s=60, session="worker", tree_key="tree"),
            fixtures.build("repeat", 200, duration_s=600, session="worker", tree_key="tree"),
            fixtures.build("edited", 900, duration_s=700, session="worker", tree_key="other-tree"),
            fixtures.build("other-worker", 1800, duration_s=800, session="other", tree_key="other-tree"),
            fixtures.build("missing-tree", 1900, duration_s=900, session="other", tree_key=None),
            fixtures.build("other-input", 3000, duration_s=1000, session="worker", tree_key="other-tree",
                           argv=["cargo", "nextest", "run", "--release"], build_base="release"),
        ]
        with closing(self.fixture.connection(*records)) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Reruns after a pass"], [600])

    def test_idle_estimate_unions_waiters_and_subtracts_mac_execution(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("mac", 3600, host="Nates-MacBook-Pro-2023", duration_s=1800),
            fixtures.build("wait-a", 7200, mem_wait_s=3600, call_id="eligible"),
            fixtures.build("wait-b", 7200, mem_wait_s=3600, call_id="eligible"),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Mac idle while natedev waits"], [1800])
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Memory wait"], [7200])

    def test_mac_execution_covers_waits_without_reading_holds(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("mac", 3600, target="mac", duration_s=3600),
            fixtures.build("wait", 7200, target="natedev", mem_wait_s=3600, call_id="eligible"),
        )) as connection, mock.patch.object(costs, "mac_hold_events", side_effect=AssertionError("unneeded hold read")):
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([row for row in result.ranked if row.cause == "Mac idle while natedev waits"], [])

    def test_recorded_targets_do_not_import_the_recorder(self) -> None:
        with closing(self.fixture.connection(
            hold_event("began", "held", fixtures.START.isoformat(), "mac"),
            hold_event("ended", "held", fixtures.END.isoformat(), "mac"),
        )):
            pass
        command = ("import sys; from scripts.buildlog import costs, holds, index; "
                   + "from datetime import UTC,datetime; "
                   + "assert costs.machine_name('mac','replacement',{}) == 'mac'; "
                   + "current=holds.CurrentHolds('',holds.NatedevHoldsNow(()),holds.MacHoldsNow(())); "
                   + "connection=index.read_only(); "
                   + "history=holds.read_hold_history(connection,current,datetime.now(UTC)); "
                   + "connection.close(); assert len(history.intervals) == 1; "
                   + "assert not any(name in sys.modules for name in ('scripts.buildlog.record', 'scripts.build_targets.targets', 'scripts.build_hold.build_hold', 'scripts.mac_test.mac_test'))")
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", command], env=dict(os.environ),
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cancelled_ci_counts_job_wall_time_once(self) -> None:
        at = fixtures.START.isoformat()
        job = ci_job(1, "build", "cancelled", (at, at, (fixtures.START + timedelta(hours=1)).isoformat()))
        run = ci_run(1, 1, [job])
        run.update(started_at=at, created_at=at, updated_at=(fixtures.START + timedelta(hours=2)).isoformat(), conclusion="cancelled")
        with closing(self.fixture.connection(run)) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([(row.machine, row.cause, row.seconds) for row in result.ranked], [("CI", "Cancelled CI", 3600)])


    def test_recorded_target_wins_and_configured_legacy_hosts_map_to_each_target(self) -> None:
        config = self.fixture.fixture.root / "targets.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        _ = config.write_text("targets=natedev,mac,aws\nkind.natedev=local\nplatform.natedev=linux\n"
                              + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                              + "kind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws.conf\n"
                              + "sync.hosts.mac=replacement-mac\nsync.hosts.aws=cloud-host\nsync.hosts.natedev=local-host\n")
        _ = config.with_name("mac_test.conf").write_text("host=fixture\n")
        _ = config.with_name("aws.conf").write_text("host=fixture\n")
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}), closing(self.fixture.connection(
            fixtures.build("new-mac", 3600, target="mac", host="different-name", duration_s=120),
            fixtures.build("legacy-mac", 3800, host="replacement-mac", mem_wait_s=60),
            fixtures.build("legacy-aws", 3900, host="cloud-host", mem_wait_s=120),
            fixtures.build("legacy-local", 4000, host="local-host", mem_wait_s=180),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([(row.machine, row.seconds) for row in result.ranked],
                         [("natedev", 180), ("aws", 120), ("mac", 60)])

    def test_candidate_waits_exclude_local_jobs_and_recorded_mac_holds(self) -> None:
        at = fixtures.START
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            call("local", mac="declined", mac_reason="local_flag", local_reason="Linux", mac_s=None),
            fixtures.build("eligible-wait", 7200, mem_wait_s=3600, call_id="eligible", target="natedev"),
            fixtures.build("local-wait", 10800, mem_wait_s=3600, call_id="local", target="natedev"),
            hold_event("began", "held", (at + timedelta(seconds=3600)).isoformat(), "mac"),
            hold_event("ended", "held", (at + timedelta(seconds=5400)).isoformat(), "mac"),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Mac idle while natedev waits"], [1800])
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Memory wait"], [7200])

    def test_missing_outcome_never_seeds_a_passed_tree_repeat(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("unknown", 100, status=None, session="same", tree_key="tree"),
            fixtures.build("passed", 200, session="same", tree_key="tree"),
            fixtures.build("repeat", 300, session="same", tree_key="tree"),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Reruns after a pass"], [60])

    def test_missing_hold_ends_preserve_idle_upper_bounds_and_complete_hold_subtraction(self) -> None:
        for later_start, later_end, upper_bound, unknown, held in (
            (None, None, 3600, 3600, 0),
            (9000, 10000, 3600, 3600, 0),
            (5400, 6300, 2700, 1800, 900),
        ):
            with self.subTest(later_start=later_start):
                records = [
                    call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
                    fixtures.build("wait", 7200, mem_wait_s=3600, call_id="eligible", target="natedev"),
                    hold_event("began", "held", (fixtures.START + timedelta(seconds=1800)).isoformat(), "mac"),
                ]
                if later_start is not None and later_end is not None:
                    records.extend((
                        hold_event("began", "held", (fixtures.START + timedelta(seconds=later_start)).isoformat(), "mac"),
                        hold_event("ended", "held", (fixtures.START + timedelta(seconds=later_end)).isoformat(), "mac"),
                    ))
                with closing(self.fixture.connection(*records)) as connection, mock.patch.object(
                    costs, "source_coverage", return_value=(costs.IndexedSourceCoverage(
                        "mac", fixtures.START.timestamp(), fixtures.END.timestamp()),),
                ):
                    result = costs.collect_costs(connection, fixtures.START, fixtures.END)
                self.assertEqual([(row.seconds, row.unknown_seconds) for row in result.ranked
                                  if row.cause == "Mac idle while natedev waits"], [(upper_bound, unknown)])
                self.assertEqual(result.mac_idle, costs.MacIdleEstimate(3600, 0, held, upper_bound, unknown))

    def test_sql_instants_treat_unzoned_times_as_utc(self) -> None:
        self.addCleanup(time.tzset)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"TZ": "America/New_York"})))
        time.tzset()
        with closing(self.fixture.connection(
            fixtures.build("first", 0, started_at="2026-10-10T01:00:00+00:00", duration_s=60,
                           session="same", tree_key="tree"),
            fixtures.build("second", 0, started_at="2026-10-10T01:02:00", duration_s=120,
                           session="same", tree_key="tree"),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual([row.seconds for row in result.ranked if row.cause == "Reruns after a pass"], [120])

    def test_mac_window_rejects_overflow_before_opening_the_index(self) -> None:
        from scripts.buildlog import mac_report

        with mock.patch.object(index, "update", side_effect=AssertionError("index updated")):
            self.assertEqual(mac_report.main(["--hours", "1e300"]), 2)

    def test_removed_commands_print_usage_without_reading_the_index(self) -> None:
        for command in ("costs", "charts"):
            with self.subTest(command=command):
                errors = io.StringIO()
                with redirect_stderr(errors), mock.patch.object(index, "read_only", side_effect=AssertionError("index opened")):
                    self.assertEqual(cli.main([command, "--days", "7"]), 2)
                self.assertEqual(errors.getvalue(), f"{cli.__doc__}\n")
                self.assertNotIn(f"buildlog {command}", errors.getvalue())

    def test_cli_help_does_not_import_collectors_or_the_report(self) -> None:
        command = ("import sys; from scripts.buildlog import cli; cli.main(['help']); "
                   + "assert not any(name in sys.modules for name in ('scripts.buildlog.record', 'scripts.buildlog.report', 'scripts.buildlog.costs', 'scripts.buildlog.launches'))")
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", command], env=dict(os.environ),
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shared_window_bounds_keep_offset_instants_and_exclude_the_end(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("inside", 0, started_at="2026-10-09T23:30:00-04:00", mem_wait_s=120),
            fixtures.build("end", 0, started_at=fixtures.END.isoformat(), mem_wait_s=999),
        )) as connection:
            rows = connection.execute("SELECT mem_wait_s FROM steps WHERE " + moments.window_bounds("started_at"),
                                      moments.window_parameters(fixtures.START, fixtures.END)).fetchall()
        self.assertEqual(rows, [(120.0,)])

    def test_ranking_keeps_every_machine(self) -> None:
        with closing(self.fixture.connection(*(fixtures.build(f"row-{number}", number, host=f"host-{number}", mem_wait_s=number + 1)
                                      for number in range(30)))) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual(len(result.ranked), 30)
        self.assertEqual(result.ranked[0], costs.RankedCost("host-29", "Memory wait", 30))

    def test_partial_mac_coverage_splits_residual_waits_without_using_individual_events(self) -> None:
        origin = fixtures.START.timestamp()
        for coverage, known, unknown in (
            ((), 0, 3600),
            ((costs.IndexedSourceCoverage("mac", origin+3600, origin+4500),), 900, 2700),
        ):
            with self.subTest(coverage=coverage), closing(self.fixture.connection(
                call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
                fixtures.build("wait", 7200, target="natedev", mem_wait_s=3600, call_id="eligible"),
                fixtures.build("mac-later", 8000, target="mac", duration_s=60),
            )) as connection, mock.patch.object(costs, "source_coverage", return_value=coverage):
                result = costs.collect_costs(connection, fixtures.START, fixtures.END)
            self.assertEqual(result.mac_idle.unknown_seconds, unknown)
            self.assertEqual(result.mac_idle.upper_bound_seconds-unknown, known)

    def test_execution_and_complete_holds_reduce_waits_without_proving_other_coverage(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 7200, target="natedev", mem_wait_s=3600, call_id="eligible"),
            fixtures.build("mac", 4500, target="mac", duration_s=900),
            hold_event("began", "complete", (fixtures.START+timedelta(seconds=5400)).isoformat(), "mac"),
            hold_event("ended", "complete", (fixtures.START+timedelta(seconds=6300)).isoformat(), "mac"),
            hold_event("began", "incomplete", (fixtures.START+timedelta(seconds=6300)).isoformat(), "mac"),
        )) as connection:
            result = costs.collect_costs(connection, fixtures.START, fixtures.END)
        self.assertEqual(result.mac_idle, costs.MacIdleEstimate(3600, 900, 900, 1800, 1800))


if __name__ == "__main__":
    _ = unittest.main()
