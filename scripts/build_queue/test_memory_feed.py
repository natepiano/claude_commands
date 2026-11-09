"""Check feed configuration and publication with private queue readings."""
from __future__ import annotations

import json
import io
import os
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import tempfile
import time
import unittest
from typing import cast, override
from unittest import mock

from scripts.buildlog import charts, costs, moments
from scripts.build_queue import admission_client, admission_code, admission_protocol as wire, memory_feed, queue_lines, queue_view
from scripts.build_queue.scope_activity import IdleService, LiveBuildRun, NoLiveBuildRun, ScopeProcess, ScopeProcesses
from scripts.lint import test_memory_admit as memory_tests
from scripts.lib import conf, test_hang_guard
from scripts.lib.build_steps import RECORDED_STEP_NAMES
from scripts.message import unit_labels
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture


class FeedStopped(Exception):
    """The fixture stops the feed after its recorded cycles."""


def captured_reading(*, running: tuple[gate.RunningStep, ...] = ()) -> gate.QueueRead:
    inputs = gate.QueueInputs([], running, (), gate.HostMemory(30 * gate.GIB, 60 * gate.GIB),
        gate.NO_PRESSURE_HOLD, gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), 200_000)
    return gate.queue_read_from(inputs, admission_policy_fixture())


def report_fixture(*, empty: bool = False) -> charts.PageData:
    end = datetime(2026, 11, 8, tzinfo=UTC)
    start = end - timedelta(days=7)
    ranked = () if empty else (costs.RankedCost("fixture", "Memory wait", 120),)
    phases = () if empty else (moments.ProgressFinish(end.timestamp() - 1, "fixture-phase",
        "/fixture/hana", "natedev", 600.0),)
    build_costs = costs.BuildCosts(start, end, phases, ranked, costs.MacIdleEstimate(0, 0, 0, 0, 0))
    bucket = charts.DailyBucket(start, end, False, build_costs, frozenset({"fixture"}),
        {} if empty else {"fixture": start.timestamp()})
    rows = charts.NoDataYet.VALUE if empty else (charts.TopCost("fixture", "Memory wait",
        charts.MeasuredCost(120), charts.WindowDiffers(charts.MeasuredCost(240)),
        charts.CostAction("Reduce the memory wait.", "fixture-owner")),)
    return charts.PageData(charts.ChartSet(start, end, "UTC", (bucket,), ("fixture",), ()),
        charts.TopCosts(end - timedelta(days=3), end, 3, rows))


def reservation_reading(*, project: str = "fixture-project") -> gate.QueueRead:
    reservation = memory_tests.ProgressKeepAsideTests.reservation("nextest")
    memory = gate.MeasuredMemory(2 * gate.GIB, anonymous=gate.AnonymousRunUse(gate.GIB))
    inputs = memory_tests.ProgressKeepAsideTests.inputs((reservation,), (memory,))
    inputs = replace(inputs, running=(replace(inputs.running[0], project=project),))
    return gate.queue_read_from(inputs, admission_policy_fixture())


def with_wait_reason(reading: gate.QueueRead, reason: gate.WaitReason) -> gate.QueueRead:
    waiter = gate.WaitingStep(200, "fixture", "fixture", "nextest", "/fixture", 10,
                              gate.GIB, reason, project="fixture-project")
    return replace(reading, view=replace(reading.view, waiting=(waiter,)))


def leftover_step(minutes: int, *, small: bool = False, members: tuple[ScopeProcess, ...] = ()) -> gate.RunningStep:
    started_at = 200_000 - minutes * 60
    activity = ScopeProcesses(len(members) or 1, "fixture", "fixture", "/fixture", "python3", started_at,
                              True, NoLiveBuildRun(), members=members)
    memory = gate.UnownedSliceMemory(gate.GIB // 4 if small else gate.GIB,
        owner=f"leftover-{minutes}", activity=activity, pid=minutes)
    return gate.RunningStep("fixture", "fixture", f"leftover-{minutes}", "/fixture", 0,
                            memory, pid=minutes, started_at=started_at)


class MemoryFeedTests(unittest.TestCase):
    root: Path = Path()
    output: Path = Path()
    configuration: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.root / "page"
        self.configuration = self.root / "memory_feed.conf"
        self.configure()
        proc = self.root / "proc"
        proc.mkdir()
        _ = (proc / "meminfo").write_text("SwapTotal: 8388608 kB\nSwapFree: 8388608 kB\n")
        _ = (self.root / "buildlog.conf").write_text("# whole: 1..\nTOP_COST_RECENT_DAYS=3\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.root),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "USER_ZONE": "UTC",
        })))
        _ = self.enterContext(mock.patch.object(admission_client, "view",
            side_effect=AssertionError("The test must supply a private queue reading")))
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={}))
        _ = self.enterContext(mock.patch.object(memory_feed, "host_name", return_value="fixture"))
        _ = self.enterContext(mock.patch.object(memory_feed, "PROC", self.root / "proc"))
        _ = self.enterContext(mock.patch.object(memory_feed, "CI_CGROUP", self.root / "ci"))
        _ = self.enterContext(mock.patch.object(memory_feed, "CGROUP", self.root / "cgroup"))
        _ = self.enterContext(mock.patch.object(charts, "page_data", return_value=report_fixture()))

    def configure(self, *, seconds: float = 0.125) -> None:
        _ = self.configuration.write_text(
            f"output_folder={self.output}\n# range: 5e-324..\ninterval_seconds={seconds}\n" +
            "# whole: 1..90\nhistory_days=7\n# whole: 1..50\nswap_rows=6\n# whole: 30..3600\nreport_refresh_seconds=300\n")

    def expected_page(self, seconds: float = 0.125) -> str:
        return Path(memory_feed.__file__).with_name("memory_live.html").read_text().replace(
            "__MEMORY_FEED_INTERVAL_SECONDS__", json.dumps(seconds))

    def test_configuration_reads_all_keys_from_the_feed_config(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as read:
            configuration = memory_feed.read_configuration()
        self.assertIn(mock.call("memory_feed"), read.call_args_list)
        self.assertEqual(configuration, memory_feed.FeedConfiguration(self.output, 0.125, 7, 6, 300))

    def test_configuration_expands_the_output_folder(self) -> None:
        _ = self.configuration.write_text("output_folder=~/page\n# range: 5e-324..\ninterval_seconds=7\n# whole: 1..90\nhistory_days=7\n# whole: 1..50\nswap_rows=6\n# whole: 30..3600\nreport_refresh_seconds=300\n")
        self.assertEqual(memory_feed.read_configuration().output_folder, Path.home() / "page")
        _ = self.configuration.write_text("output_folder=$FEED_TEST_ROOT/page\n# range: 5e-324..\ninterval_seconds=7\n# whole: 1..90\nhistory_days=7\n# whole: 1..50\nswap_rows=6\n# whole: 30..3600\nreport_refresh_seconds=300\n")
        with mock.patch.dict(os.environ, {"FEED_TEST_ROOT": str(self.root)}):
            self.assertEqual(memory_feed.read_configuration().output_folder, self.output)

    def test_a_missing_key_fails_with_its_name(self) -> None:
        original = self.configuration.read_text()
        for key in ("output_folder", "interval_seconds", "history_days", "swap_rows", "report_refresh_seconds"):
            remaining = "\n".join(line for line in original.splitlines()
                                  if not line.startswith(key + "="))
            with self.subTest(key=key):
                _ = self.configuration.write_text(remaining)
                with conf.diagnostics(report=False), self.assertRaisesRegex(conf.Unusable, key):
                    _ = memory_feed.read_configuration(conf.read(self.configuration))

    def test_link_prints_the_configured_folder_with_the_review_host_and_port(self) -> None:
        root = self.root / "review-links"
        folder = root / "fixture folder"
        _ = self.configuration.write_text(f"output_folder={folder}\n")
        _ = (self.root / "review_links.conf").write_text("# whole: 1..65535\nserver_port=9432\n")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(root), "REVIEW_LINKS_HOST": "fixture.example."}),
              mock.patch.object(memory_feed, "publish_page", side_effect=AssertionError("The link must not publish the page")),
              redirect_stdout(output)):
            self.assertEqual(memory_feed.main(["link"]), 0)
        self.assertEqual(output.getvalue(), "http://fixture.example:9432/fixture%20folder/memory_live.html\n")
        self.assertFalse(root.exists())

    def test_link_refuses_a_folder_outside_the_review_root_with_one_plain_line(self) -> None:
        root = self.root / "review-links"
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(root), "REVIEW_LINKS_HOST": "fixture.example"}),
              redirect_stdout(output)):
            self.assertEqual(memory_feed.main(["link"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(output.getvalue().startswith("Build page link: "))
        self.assertFalse(self.output.exists())

    def test_start_publishes_the_page_before_the_first_queue_reading(self) -> None:
        def view() -> gate.QueueRead:
            self.assertEqual((self.output / "memory_live.html").read_text(), self.expected_page())
            return captured_reading()

        with mock.patch.object(admission_client, "view", side_effect=view) as read:
            self.assertEqual(memory_feed.main(["--once"]), 0)
        read.assert_called_once_with()
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertEqual(page["read_at"], 200_000)
        self.assertEqual(page["total"], 60)
        self.assertEqual(page["available"], 30)
        self.assertEqual(page["walk"], [])
        self.assertEqual(set(path.name for path in self.output.iterdir()), {"memory_live.html", "memory.json"})
        for name in ("memory_live.html", "memory.json"):
            with self.subTest(name=name):
                self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o644)

    def test_restart_replaces_the_served_page_with_configured_timing(self) -> None:
        self.output.mkdir()
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        for seconds, stale in ((0.125, 15), (60.0, 300)):
            with self.subTest(seconds=seconds):
                self.configure(seconds=seconds)
                _ = (self.output / "memory_live.html").write_text("previous page")
                with mock.patch.object(admission_client, "view", return_value=captured_reading()):
                    self.assertEqual(memory_feed.main(["--once"]), 0)
                page = (self.output / "memory_live.html").read_text()
                self.assertEqual(page, self.expected_page(seconds))
                constants = page[page.index("const SECONDS ="):page.index("const $ =")]
                result = subprocess.run(
                    [executable, "-e", constants + "\nprocess.stdout.write(String(STALE));"],
                    text=True, capture_output=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(float(result.stdout), stale)

    def test_an_interrupted_page_publication_preserves_the_previous_page(self) -> None:
        self.output.mkdir()
        target = self.output / "memory_live.html"
        _ = target.write_text("previous page")
        with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                memory_feed.publish_page(memory_feed.read_configuration())
        self.assertEqual(target.read_text(), "previous page")
        self.assertEqual(list(self.output.iterdir()), [target])

    def test_the_first_failed_reading_publishes_an_error(self) -> None:
        with mock.patch.object(admission_client, "view",
                               side_effect=wire.AdmissionUnavailable("private socket unavailable")):
            self.assertEqual(memory_feed.main(["--once"]), 0)
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], False)
        self.assertEqual(page["error"], "private socket unavailable")

    def test_a_failed_cycle_retains_the_last_successful_reading(self) -> None:
        with (mock.patch.object(admission_client, "view",
                                side_effect=[captured_reading(), RuntimeError("queue unavailable")]),
              mock.patch.object(time, "sleep", side_effect=[None, FeedStopped]) as sleep):
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        self.assertEqual(sleep.call_args_list, [mock.call(0.125), mock.call(0.125)])
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertEqual(page["read_at"], 200_000)
        self.assertEqual(page["read_error"], "queue unavailable")

    def test_missing_pressure_has_a_named_state(self) -> None:
        pressure = memory_feed.read_stalls()
        self.assertIsInstance(pressure["some"], memory_feed.PressureNotRecorded)
        self.assertIsInstance(pressure["full"], memory_feed.PressureNotRecorded)

    def test_report_configuration_uses_private_values(self) -> None:
        text = self.configuration.read_text().replace("history_days=7", "history_days=11").replace("swap_rows=6", "swap_rows=4").replace(
            "report_refresh_seconds=300", "report_refresh_seconds=600")
        _ = self.configuration.write_text(text)
        configuration = memory_feed.read_configuration()
        self.assertEqual(configuration.history_days, 11)
        self.assertEqual(configuration.swap_rows, 4)
        self.assertEqual(configuration.report_refresh_seconds, 600)

    def test_report_reads_follow_the_configured_refresh_window(self) -> None:
        report_calls_per_cycle: list[int] = []
        with (mock.patch.object(admission_client, "view", return_value=captured_reading()) as queue,
              mock.patch.object(charts, "page_data", return_value=report_fixture()) as provider,
              mock.patch.object(time, "monotonic", side_effect=[0.0, 100.0, 301.0]),
              mock.patch.object(time, "sleep", side_effect=[None, None, FeedStopped])):
            def view() -> gate.QueueRead:
                report_calls_per_cycle.append(provider.call_count)
                return captured_reading()

            queue.side_effect = view
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        self.assertEqual(queue.call_count, 3)
        self.assertEqual(report_calls_per_cycle, [1, 1, 2])
        self.assertEqual(provider.call_args_list, [mock.call(7, n=3), mock.call(7, n=3)])

    def test_report_dates_use_the_configured_user_zone(self) -> None:
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            report = memory_feed.report_reading(7)
        assert isinstance(report, memory_feed.ReportSections)
        self.assertEqual(report.zone, "America/New_York")
        self.assertEqual(report.start, "2026-11-04T19:00:00-05:00")
        self.assertEqual(report.end, "2026-11-07T19:00:00-05:00")

    def cgroup(self, name: str, swapped: int) -> Path:
        path = self.root / "cgroup" / name
        path.mkdir(parents=True, exist_ok=True)
        _ = (path / "memory.swap.current").write_text(str(swapped))
        return path

    def host_swap(self, total: int, used: int) -> None:
        _ = (self.root / "proc/meminfo").write_text(
            f"SwapTotal: {total // 1024} kB\nSwapFree: {(total - used) // 1024} kB\n")

    def test_swap_accounts_for_the_host_and_names_the_agent_owner_once(self) -> None:
        self.host_swap(32 * gate.GIB, 20 * gate.GIB)
        _ = self.cgroup("user.slice", 16 * gate.GIB)
        user = "user.slice/user@1000.service"
        _ = self.cgroup(user, 15 * gate.GIB)
        _ = self.cgroup(user + "/app.slice", 12 * gate.GIB)
        _ = self.cgroup(user + "/app.slice/app-agents.slice", 10 * gate.GIB)
        agent = self.cgroup(user + "/app.slice/app-agents.slice/first.scope", 4 * gate.GIB)
        _ = self.cgroup(user + "/app.slice/app-agents.slice/second.scope", 6 * gate.GIB)
        _ = self.cgroup(user + "/app.slice/app-browser.scope", 2 * gate.GIB)
        _ = self.cgroup(user + "/session.slice", 2 * gate.GIB)
        _ = self.cgroup(user + "/builds.slice", gate.GIB)
        scope = self.cgroup(user + "/builds.slice/run.scope", gate.GIB)
        _ = self.cgroup("system.slice", 2 * gate.GIB)
        _ = self.cgroup("hana.slice", gate.GIB)
        queued = reservation_reading().view.running[0]
        memory = queued.measured
        assert isinstance(memory, gate.MeasuredMemory)
        queued = replace(queued, measured=replace(memory, anonymous_source=gate.ScopeSource(scope, frozenset())))
        reads: Counter[Path] = Counter()
        original = Path.read_text

        def read(path: Path) -> str:
            reads[path] += 1
            return original(path)

        with mock.patch.object(Path, "read_text", new=read):
            swap = memory_feed.read_swap((queued, queued), {}, 6)
        assert isinstance(swap, memory_feed.SwapReading)
        rows = {row["name"]: row for row in swap.rows}
        self.assertEqual(rows["agent sessions (2)"]["bytes"], 10 * gate.GIB)
        self.assertEqual(rows["other apps: browser"]["bytes"], 2 * gate.GIB)
        self.assertEqual(rows["desktop session"]["bytes"], 2 * gate.GIB)
        self.assertEqual(rows["system services"]["bytes"], 2 * gate.GIB)
        self.assertEqual(rows["hana CI"]["bytes"], gate.GIB)
        self.assertEqual(rows[queue_lines.step_text(queued, RECORDED_STEP_NAMES)]["bytes"], gate.GIB)
        self.assertEqual(rows["not in a group"]["bytes"], gate.GIB)
        self.assertEqual(rows["not attributed"]["bytes"], gate.GIB)
        self.assertEqual(rows["Total"]["bytes"], swap.used)
        self.assertEqual(sum(row["bytes"] for row in swap.rows if row["kind"] != "total"), swap.used)
        self.assertEqual(swap.files_read, sum(reads.values()))
        self.assertTrue(all(count == 1 for count in reads.values()))
        self.assertNotIn(agent / "memory.swap.current", reads)

    def test_swap_keeps_the_unattributed_bytes_without_rounding_them_away(self) -> None:
        self.host_swap(2 ** 20, 2 ** 20)
        _ = self.cgroup("system.slice", 2 ** 20 - 7)
        swap = memory_feed.read_swap((), {}, 6)
        assert isinstance(swap, memory_feed.SwapReading)
        residual = next(row for row in swap.rows if row["kind"] == "unattributed")
        self.assertEqual(residual["bytes"], 7)
        self.assertEqual(sum(row["bytes"] for row in swap.rows[:-1]), swap.used)
        self.assertIn("not attributed: 7 B", memory_feed.swap_text(swap.rows))

    def test_swap_exposes_a_signed_counter_difference_without_changing_owner_values(self) -> None:
        self.host_swap(2 ** 20, 2 ** 20)
        _ = self.cgroup("system.slice", 2 ** 20 + 7)
        swap = memory_feed.read_swap((), {}, 6)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap.rows[0]["bytes"], 2 ** 20 + 7)
        residual = next(row for row in swap.rows if row["kind"] == "unattributed")
        self.assertEqual(residual["bytes"], -7)
        self.assertEqual(sum(row["bytes"] for row in swap.rows[:-1]), swap.used)

    def test_swap_names_ci_instead_of_the_raw_service_step(self) -> None:
        self.host_swap(2 * gate.GIB, gate.GIB)
        _ = self.cgroup("hana.slice", gate.GIB)
        path = self.cgroup("hana.slice/hana-ci.slice", gate.GIB)
        step = gate.RunningStep("", "hana-ci.slice", "service", str(path), 0,
            gate.UnownedSliceMemory(gate.GIB, cgroup=str(path), owner="hana-ci.slice",
                                    pid=gate.PidNotRecorded(), job_state=IdleService()), pid=gate.PidNotRecorded())
        swap = memory_feed.read_swap((step,), {}, 6)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap.rows[0]["name"], "hana CI")
        self.assertEqual(swap.rows[0]["bytes"], gate.GIB)

    def test_swap_names_between_steps_owners_in_the_shared_rows(self) -> None:
        self.host_swap(4 * gate.GIB, 2 * gate.GIB)
        _ = self.cgroup("builds.slice", 2 * gate.GIB)
        steps: list[gate.RunningStep] = []
        labels = {name: unit_labels.UnitLabels(name) for name in ("owner-a", "owner-b")}
        for owner in labels:
            path = self.cgroup("builds.slice/" + owner + ".scope", gate.GIB)
            steps.append(gate.RunningStep(owner, owner, "between steps", str(path), 0,
                gate.UnownedSliceMemory(gate.GIB, cgroup=str(path), owner=owner, pid=gate.PidNotRecorded()),
                pid=gate.PidNotRecorded()))
        swap = memory_feed.read_swap(steps, labels, 6)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual({row["name"] for row in swap.rows if row["kind"] == "owner"}, set(labels))
        self.assertTrue(all("between steps" in row["detail"] for row in swap.rows if row["kind"] == "owner"))

    def test_swap_small_groups_and_total_use_the_same_current_host_reading(self) -> None:
        self.host_swap(4 * gate.GIB, 3 * gate.GIB)
        for number in range(3):
            _ = self.cgroup(f"owner-{number}.slice", gate.GIB // 2)
        with mock.patch.object(admission_client, "view", return_value=captured_reading()):
            page = memory_feed.reading(replace(memory_feed.read_configuration(), swap_rows=1))
        self.assertEqual(page["swap_used"], 3)
        self.assertEqual(page["swap_total"], 4)
        self.assertEqual([row["kind"] for row in page["swap_by"]], ["owner", "smaller", "unattributed", "total"])
        self.assertEqual(sum(row["bytes"] for row in page["swap_by"][:-1]), 3 * gate.GIB)
        self.assertEqual(page["swap_by"][1]["bytes"], gate.GIB)

    def test_between_steps_holds_name_their_distinct_owners(self) -> None:
        reading = captured_reading()
        room = reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        holds = tuple(gate.RunKeepAside(gate.RunMemory(owner, gate.GIB, gate.AnonymousRunUse(0)), gate.GIB)
                      for owner in ("owner-a", "owner-b", "owner-c"))
        reading = replace(reading, explanation=replace(reading.explanation, room=replace(room, runs=holds)))
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        self.assertEqual({row["name"] for row in page["walk"]}, {"owner-a", "owner-b", "owner-c"})
        self.assertTrue(all(row["detail"] == "between steps" for row in page["walk"]))

    def test_pressure_failure_names_the_path_without_the_python_error(self) -> None:
        values = memory_feed.read_stalls()
        for value in values.values():
            assert isinstance(value, memory_feed.PressureNotRecorded)
            self.assertEqual(value.reason, f"Pressure is unreadable: {self.root / 'proc/pressure/memory'}")
            self.assertNotIn("Errno", value.reason)

    def test_history_shorter_than_the_recent_window_names_the_configuration_error(self) -> None:
        _ = (self.root / "buildlog.conf").write_text("# whole: 1..\nTOP_COST_RECENT_DAYS=8\n")
        with (mock.patch.object(charts, "page_data") as provider,
              mock.patch.object(admission_client, "view", return_value=reservation_reading())):
            page = memory_feed.reading()
        provider.assert_not_called()
        text = memory_feed.page_json(page)
        self.assertIn("history_days", text)
        self.assertIn("TOP_COST_RECENT_DAYS", text)
        self.assertTrue(page["walk"])

    def test_an_installed_schema_change_requires_a_restart_before_the_next_read(self) -> None:
        installed = self.root / "installed"
        source = installed / "scripts" / "build_queue" / "admission_records.py"
        for relative in admission_code.ADMISSION_CODE_PATHS:
            target = installed / "scripts" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text("fixture\n")
        _ = source.write_text("schema = 'before'\n")
        before = memory_feed.installed_code_version(installed)

        def install_schema(seconds: float) -> None:
            self.assertEqual(seconds, 0.125)
            _ = source.write_text("schema = 'after'\n")
            self.assertNotEqual(memory_feed.installed_code_version(installed), before)

        with (mock.patch.object(admission_client, "view", return_value=captured_reading()) as view,
              mock.patch.object(memory_feed, "LOADED_ADMISSION_CODE_VERSION", before),
              mock.patch.object(time, "sleep", side_effect=install_schema)):
            self.assertEqual(memory_feed.main(["--code-root", str(installed)]), 1)
        view.assert_called_once_with()
        self.assertNotEqual(memory_feed.installed_code_version(installed), before)
        changed = replace(captured_reading(), explanation=replace(captured_reading().explanation, now=200_001))
        with (mock.patch.object(admission_client, "view", return_value=changed) as restarted,
              mock.patch.object(memory_feed, "LOADED_ADMISSION_CODE_VERSION", memory_feed.installed_code_version(installed))):
            self.assertEqual(memory_feed.main(["--once", "--code-root", str(installed)]), 0)
        restarted.assert_called_once_with()
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertEqual(page["read_at"], 200_001)

    def test_each_wait_state_keeps_its_reason_and_the_other_readings(self) -> None:
        reasons = (
            (gate.PressureUnavailable("cannot read /private/proc/pressure/memory"), "/private/proc/pressure/memory"),
            (gate.StartSpacingWait(200_010), "start spacing"),
            (gate.BehindHead("first", 1, "head-session"), "behind"),
        )
        for reason, expected in reasons:
            with self.subTest(reason=reason):
                reading = with_wait_reason(reservation_reading(), reason)
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                self.assertIn(expected, page["waiters"][0]["reason"])
                self.assertTrue(page["walk"])
                self.assertGreater(page["room"], 0)

    def test_a_schema_install_during_import_restarts_a_fresh_interpreter(self) -> None:
        installed = self.root / "installed"
        source = Path(memory_feed.__file__).resolve().parents[1]
        _ = shutil.copytree(source, installed / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        probe = textwrap.dedent("""
            import builtins
            import json
            import sys
            from pathlib import Path
            from unittest import mock

            root = Path(sys.argv[1])
            source = root / 'scripts/build_queue/snapshot_codec.py'
            original_import = builtins.__import__
            changed = False
            def install_during_import(name, globals=None, locals=None, fromlist=(), level=0):
                global changed
                result = original_import(name, globals, locals, fromlist, level)
                if name == 'scripts.build_queue' and 'snapshot_codec' in fromlist and not changed:
                    changed = True
                    source.write_text(source.read_text() + '\\nfrom dataclasses import dataclass\\n'
                        '@dataclass(frozen=True)\\nclass InstalledSchemaRevision:\\n    revision: str\\n')
                return result
            builtins.__import__ = install_during_import
            from scripts.build_queue import memory_feed
            builtins.__import__ = original_import
            assert changed
            assert not hasattr(memory_feed.codec, 'InstalledSchemaRevision')
            with mock.patch.object(memory_feed.admission_client, 'view',
                    side_effect=AssertionError('The old decoder must not read the queue')):
                status = memory_feed.main(['--once', '--code-root', str(root)])
            print(json.dumps({'status': status, 'changed':
                memory_feed.LOADED_ADMISSION_CODE_VERSION != memory_feed.installed_code_version(root)}))
            sys.exit(status)
        """)
        result = subprocess.run([sys.executable, "-c", probe, str(installed)], cwd=installed,
            env={**os.environ, "PYTHONPATH": str(installed)}, capture_output=True, text=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": 1, "changed": True})
        restarted = textwrap.dedent("""
            import sys
            from unittest import mock
            from scripts.build_queue import memory_feed
            decoded = memory_feed.codec.parse_snapshot_value(
                {'kind': 'InstalledSchemaRevision', 'revision': 'after'}, memory_feed.codec.InstalledSchemaRevision)
            with (mock.patch.object(memory_feed, 'report_reading',
                    return_value=memory_feed.charts.IndexUnavailable('private report fixture')),
                  mock.patch.object(memory_feed, 'reading', return_value={
                    'ok': True, 'read_at': 200001, 'decoder_revision': decoded.revision})):
                sys.exit(memory_feed.main(['--once', '--code-root', sys.argv[1]]))
        """)
        result = subprocess.run([sys.executable, "-c", restarted, str(installed)], cwd=installed,
            env={**os.environ, "PYTHONPATH": str(installed)}, capture_output=True, text=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertEqual(page["decoder_revision"], "after")

    def test_project_names_follow_the_queue_formatter_for_running_and_waiting_steps(self) -> None:
        for project, folder in (("known-project", "/fixture"), ("", "/fixture"),
                                ("", "/absolute/repository")):
            with self.subTest(project=project, folder=folder):
                reading = reservation_reading(project=project)
                step = replace(reading.view.running[0], folder=folder)
                explanation_step = replace(reading.explanation.running[0], step=step)
                reading = replace(reading, view=replace(reading.view, running=(step,)),
                    explanation=replace(reading.explanation, running=(explanation_step,)))
                reading = with_wait_reason(reading, gate.StartSpacingWait(110))
                waiter = replace(reading.view.waiting[0], project=project, folder=folder)
                reading = replace(reading, view=replace(reading.view, waiting=(waiter,)))
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                self.assertEqual(page["walk"][0]["name"],
                    queue_lines.step_text(reading.view.running[0], RECORDED_STEP_NAMES))
                self.assertEqual(page["waiters"][0]["name"],
                    queue_lines.step_text(waiter, RECORDED_STEP_NAMES))
                expected = "known-project nextest" if project else "nextest"
                self.assertEqual(page["walk"][0]["name"], expected)
                self.assertEqual(page["waiters"][0]["name"], expected)
                self.assertNotIn(folder, page["walk"][0]["name"])

    def test_leftover_ages_use_the_queue_formatter_for_rows_and_group_members(self) -> None:
        for small in (False, True):
            with self.subTest(grouped=small):
                reading = captured_reading(running=tuple(leftover_step(minutes, small=small)
                    for minutes in (45, 19 * 60, 2 * 24 * 60)))
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                text = memory_feed.page_json(page)
                for minutes in (45, 19 * 60, 2 * 24 * 60):
                    self.assertIn(queue_view.leftover_age(minutes), text)

    def test_the_leftover_list_uses_the_queue_rule_and_keeps_all_scope_members(self) -> None:
        ended = leftover_step(45)
        memory = ended.measured
        assert isinstance(memory, gate.UnownedSliceMemory)
        activity = memory.activity
        assert isinstance(activity, ScopeProcesses)
        ended = replace(ended, measured=replace(memory, activity=replace(activity, count=2,
            members=(ScopeProcess(45, "python3"), ScopeProcess(46, "helper")))))
        live = replace(ended, pid=60, measured=replace(memory,
            activity=replace(activity, run=LiveBuildRun(60, "live", 199_000))))
        compiler = replace(ended, pid=70, measured=gate.CompilerServiceMemory(gate.GIB, pid=70))
        service = replace(ended, pid=80, measured=replace(memory, job_state=IdleService()))
        reading = captured_reading(running=(ended, live, compiler, service))
        with (mock.patch.object(queue_view, "leftover_processes", wraps=queue_view.leftover_processes) as shared_rule,
              mock.patch.object(admission_client, "view", return_value=reading)):
            page = memory_feed.reading()
        shared_rule.assert_called_once_with(reading.view.running)
        self.assertEqual(len(page["leftovers"]), 1)
        self.assertEqual(page["leftovers"][0]["members"], [
            {"pid": 45, "program": "python3"}, {"pid": 46, "program": "helper"}])
        self.assertEqual(page["leftovers"][0]["age"], "45 min")

    def test_an_unavailable_cap_keeps_rows_room_and_reason(self) -> None:
        reading = reservation_reading()
        room = reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        missing_run = replace(room.runs[0].memory, use=gate.RunUseUnavailable("private memory.stat is missing"))
        for cap in (gate.CapUnavailable(()), gate.CapUnavailable((missing_run,))):
            with self.subTest(cap=cap):
                changed = replace(reading, explanation=replace(reading.explanation, room=replace(room, cap=cap)))
                with mock.patch.object(admission_client, "view", return_value=changed):
                    page = memory_feed.reading()
                self.assertIsInstance(page["cap"], gate.CapUnavailable)
                self.assertEqual(page["room"], room.room / gate.GIB)
                self.assertTrue(page["walk"])
                self.assertIn("unavailable", page["cap_text"])
                if cap.runs:
                    self.assertIn("private memory.stat is missing", page["cap_text"])

    def test_an_unavailable_run_use_keeps_its_named_reason(self) -> None:
        reading = reservation_reading()
        room = reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        missing = gate.RunUseUnavailable("private memory.stat is missing")
        run = replace(room.runs[0], memory=replace(room.runs[0].memory, use=missing))
        room = replace(room, runs=(run,), cap=gate.CapUnavailable((run.memory,)))
        reading = replace(reading, explanation=replace(reading.explanation, room=room))
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        self.assertEqual(page["walk"][0]["used"], missing)
        self.assertIn("RunUseUnavailable", memory_feed.page_json(page))

    def test_one_report_call_supplies_both_sections(self) -> None:
        with (mock.patch.object(charts, "page_data", return_value=report_fixture()) as provider,
              mock.patch.object(admission_client, "view", return_value=captured_reading())):
            page = memory_feed.reading()
        provider.assert_called_once_with(7, n=3)
        text = memory_feed.page_json(page)
        for expected in ("Memory wait", "fixture-owner", "Reduce the memory wait."):
            self.assertIn(expected, text)

    def test_report_failures_keep_the_queue_reading(self) -> None:
        for failure in (charts.IndexUnavailable("private index is missing"),
                        conf.Unusable("private report configuration is invalid")):
            with self.subTest(failure=failure):
                provider = (mock.patch.object(charts, "page_data", side_effect=failure)
                            if isinstance(failure, Exception) else
                            mock.patch.object(charts, "page_data", return_value=failure))
                with (provider,
                      mock.patch.object(admission_client, "view", return_value=reservation_reading())):
                    page = memory_feed.reading()
                text = memory_feed.page_json(page)
                self.assertTrue(page["walk"])
                self.assertIn(failure.reason if isinstance(failure, charts.IndexUnavailable) else str(failure), text)

    def test_an_empty_report_keeps_its_recent_window(self) -> None:
        with (mock.patch.object(charts, "page_data", return_value=report_fixture(empty=True)),
              mock.patch.object(admission_client, "view", return_value=captured_reading())):
            page = memory_feed.reading()
        text = memory_feed.page_json(page)
        self.assertIn("2026-11-05", text)
        self.assertIn("2026-11-08", text)

    def test_the_json_boundary_preserves_named_missing_states(self) -> None:
        with mock.patch.object(admission_client, "view", return_value=captured_reading()):
            payload = memory_feed.reading()
        payload["cap"] = gate.CapUnavailable(())
        page = cast(dict[str, object], json.loads(memory_feed.page_json(payload)))
        self.assertEqual(page["cap"], {"kind": "CapUnavailable", "runs": []})
        self.assertIn("PressureNotRecorded", json.dumps(page["psi"]))


def write_browser_fixture(folder: Path) -> None:
    """Publish private feed readings for the browser proof."""
    fixture = MemoryFeedTests()
    fixture.setUp()
    _ = cast(object, fixture.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"})))
    pressure_missing = memory_feed.PressureNotRecorded("Pressure is unreadable: /private/proc/pressure/memory")
    _ = cast(object, fixture.enterContext(mock.patch.object(memory_feed, "read_stalls",
        return_value={"some": pressure_missing, "full": pressure_missing})))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        configuration = replace(memory_feed.read_configuration(), output_folder=folder)
        memory_feed.publish_page(configuration)
        base = reservation_reading()
        inputs = base.explanation.decided_from
        live = leftover_step(30)
        memory = live.measured
        assert isinstance(memory, gate.UnownedSliceMemory)
        activity = memory.activity
        assert isinstance(activity, ScopeProcesses)
        live = replace(live, pid=70, measured=replace(memory,
            activity=replace(activity, run=LiveBuildRun(70, "live", 199_000))))
        compiler = replace(live, pid=80, measured=gate.CompilerServiceMemory(gate.GIB, pid=80))
        inputs = replace(inputs, now=200_000, running=(*inputs.running, leftover_step(45,
            members=(ScopeProcess(45, "python3"), ScopeProcess(46, "helper"))),
            leftover_step(19 * 60, small=True), leftover_step(2 * 24 * 60, small=True), live, compiler))
        base = gate.queue_read_from(inputs, admission_policy_fixture())
        readings: dict[str, gate.QueueRead] = {"good": base}
        for name, reason in (
            ("pressure", gate.PressureUnavailable("cannot read /private/proc/pressure/memory")),
            ("spacing", gate.StartSpacingWait(110)),
            ("head", gate.BehindHead("first", 1, "head-session")),
        ):
            readings[name] = with_wait_reason(base, reason)
        room = base.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        missing = replace(room.runs[0].memory, use=gate.RunUseUnavailable("private memory.stat is missing"))
        for name, cap in (("old_cap", gate.CapUnavailable(())), ("missing_cap", gate.CapUnavailable((missing,)))):
            readings[name] = replace(base, explanation=replace(base.explanation, room=replace(room, cap=cap)))
        missing_room = replace(room, cap=gate.CapUnavailable((missing,)),
            runs=(replace(room.runs[0], memory=missing),))
        readings["missing_use"] = replace(base, explanation=replace(base.explanation, room=missing_room))
        pages: dict[str, object] = {}
        for name, reading in readings.items():
            with mock.patch.object(admission_client, "view", return_value=reading):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        for name, report in (("empty_report", report_fixture(empty=True)),
                             ("missing_report", charts.IndexUnavailable("private index is missing"))):
            with (mock.patch.object(charts, "page_data", return_value=report),
                  mock.patch.object(admission_client, "view", return_value=base)):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        _ = (folder / "fixtures.json").write_text(json.dumps(pages))
    finally:
        _ = fixture.doCleanups()
